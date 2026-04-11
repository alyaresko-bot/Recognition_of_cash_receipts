from __future__ import annotations

import re
from typing import List, Any, Optional

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

from config import (
    GOOGLE_SHEETS_SPREADSHEET_ID,
    GOOGLE_SHEETS_ITEMS_SHEET_NAME,
    GOOGLE_SHEETS_SHEET_NAME,
    GOOGLE_SERVICE_ACCOUNT_FILE,
)


SCOPES: List[str] = ["https://www.googleapis.com/auth/spreadsheets"]


def _format_decimal_comma(value: float) -> str:
    """Форматирует число в виде, привычном для RU-локали: десятичная запятая."""
    txt = f"{value:.6f}".rstrip("0").rstrip(".")
    if txt in ("", "-0"):
        txt = "0"
    return txt.replace(".", ",")


def _to_sheet_numeric_text(value: Any) -> Any:
    """
    Возвращает число в строковом виде с запятой (например, 12,34),
    чтобы при USER_ENTERED Google Sheets в RU-локали распознал значение как numeric.
    """
    if value is None:
        return ""
    if isinstance(value, (int, float)):
        return _format_decimal_comma(float(value))
    if not isinstance(value, str):
        return value

    txt = value.strip()
    if not txt:
        return ""

    normalized = txt.replace("\u00A0", "").replace(" ", "")
    for ch in ["₽", "р", "RUB", "руб.", "руб"]:
        normalized = normalized.replace(ch, "")
    normalized = normalized.replace(",", ".").strip()
    try:
        return _format_decimal_comma(float(normalized))
    except ValueError:
        return value


def _extract_row_bounds(updated_range: str) -> tuple[Optional[int], Optional[int]]:
    """
    Извлекает диапазон строк из updatedRange вида:
    "Товар!A12:G30" -> (12, 30)
    """
    if not updated_range:
        return (None, None)
    m = re.search(r"![A-Z]+(\d+):[A-Z]+(\d+)$", updated_range)
    if not m:
        return (None, None)
    return (int(m.group(1)), int(m.group(2)))


def get_sheets_service():
    credentials = Credentials.from_service_account_file(
        GOOGLE_SERVICE_ACCOUNT_FILE,
        scopes=SCOPES,
    )
    return build("sheets", "v4", credentials=credentials)


def append_receipt_row(
    receipt_data: dict,
    sheet_name: Optional[str] = None,
) -> Any:
    """
    Добавляет одну агрегированную строку с данными чека в гугл-таблицу.

    Ожидается структура, соответствующая system_prompt.txt:

    {
      "receipt_info": {
        "organization": "",
        "inn": "",
        "date": "",
        "time": "",
        "receipt_number": "",
        "shift_number": "",
        "fiscal_sign": "",
        "payment_type": "",
        "total_discount": "",
        "total_amount": "",
        "vat": ""
      },
      "items": [
        {...}
      ]
    }

    В базовом варианте записываем одну строку с общими данными чека,
    без развернутых позиций items. Столбцы O–P: ФН из фискального QR и признак
    «QR распознан» (Да/Нет).
    """
    if sheet_name is None:
        sheet_name = GOOGLE_SHEETS_SHEET_NAME

    receipt_info = receipt_data.get("receipt_info") or {}
    items = receipt_data.get("items") or []

    organization = receipt_info.get("organization") or ""
    inn = receipt_info.get("inn") or ""
    date = receipt_info.get("date") or ""
    time = receipt_info.get("time") or ""
    receipt_number = receipt_info.get("receipt_number") or ""
    shift_number = receipt_info.get("shift_number") or ""
    fiscal_storage_number = receipt_info.get("fiscal_storage_number") or ""
    fiscal_sign = receipt_info.get("fiscal_sign") or ""
    payment_type = receipt_info.get("payment_type") or ""
    total_discount = _to_sheet_numeric_text(receipt_info.get("total_discount") or "")
    total_amount = _to_sheet_numeric_text(receipt_info.get("total_amount") or "")
    vat = _to_sheet_numeric_text(receipt_info.get("vat") or "")
    items_count = len(items)

    # Валидация арифметики: сравниваем итог по чеку
    # и сумму позиций. Чек всегда записываем, но
    # добавляем флаг и комментарий.

    def _to_number(value: Any) -> Optional[float]:
        if value is None:
            return None
        if isinstance(value, (int, float)):
            return float(value)
        if not isinstance(value, str):
            return None
        txt = value.strip()
        if not txt:
            return None
        # Убираем возможные валютные символы
        for ch in ["₽", "р", "RUB", "руб.", "руб"]:
            txt = txt.replace(ch, "")
        txt = txt.replace("\u00A0", "").replace(" ", "")
        txt = txt.replace(",", ".").strip()
        try:
            return float(txt)
        except ValueError:
            return None

    receipt_total_num = _to_number(total_amount)

    items_total_num: Optional[float] = None
    items_discount_num: Optional[float] = 0.0
    unparsed_items = 0
    unparsed_discounts = 0
    for item in items:
        item_total = item.get("item_total")
        num = _to_number(item_total)
        if num is None:
            unparsed_items += 1
            continue
        if items_total_num is None:
            items_total_num = 0.0
        items_total_num += num

        disc = _to_number(item.get("item_discount"))
        if disc is None:
            if item.get("item_discount") not in (None, "", 0):
                unparsed_discounts += 1
        else:
            items_discount_num += disc

    validation_status = ""
    validation_comment = ""
    mismatch_delta: Optional[float] = None

    if receipt_total_num is not None and items_total_num is not None:
        # Старый подход с abs-разницей помечал любое расхождение как ошибку.
        # Теперь мы допускаем дубли из-за перекрытия кадров, поэтому важно
        # различать "недостачу" (потеря позиций) и "избыточность" (возможные дубли).
        # Для чеков используем строгий порог в 1 копейку:
        # даже небольшое расхождение должно быть отмечено предупреждением.
        tol = 0.01
        net_sum = float(items_total_num) - float(items_discount_num or 0.0)
        delta = float(net_sum) - float(receipt_total_num)
        mismatch_delta = delta

        if abs(delta) <= tol:
            validation_status = "ok"
        elif delta < -tol:
            # net_sum меньше total_amount => вероятно потеря позиций
            # или недовытащили скидки.
            validation_status = "check"
            validation_comment = (
                f"Недостача (net): по чеку {receipt_total_num}, "
                f"по позициям gross {items_total_num} минус скидки {items_discount_num}, "
                f"разница {-delta:.2f}."
            )
        else:
            # items_total больше total_amount => возможно задвоение из-за перекрытия.
            validation_status = "duplicate_possible"
            validation_comment = (
                f"Возможные дубли/проблема со скидками: по чеку {receipt_total_num}, "
                f"по позициям net {net_sum}, разница {delta:.2f}."
            )
    else:
        validation_status = "check"
        reasons = []
        if receipt_total_num is None:
            reasons.append("итог по чеку не разобран")
        if items_total_num is None:
            reasons.append("сумма по позициям не посчитана")
        if unparsed_items:
            reasons.append(f"у {unparsed_items} позиций не разобрана сумма")
        if reasons:
            validation_comment = "; ".join(reasons)

    # Усиливаем валидацию по метаданным OCR-этапа:
    # если модель сама сигнализирует риск потерь/ошибок, вносим предупреждение в таблицу.
    meta = receipt_data.get("_meta") or {}
    qr_sub = meta.get("qr") if isinstance(meta, dict) else None
    qr_recognized_yes_no = (
        "Да"
        if isinstance(qr_sub, dict) and bool(qr_sub.get("used"))
        else "Нет"
    )
    if isinstance(meta, dict):
        meta_needs_review = bool(meta.get("needs_review"))
        meta_math_errors = int(meta.get("math_error_count") or 0)
        meta_unparsed_items = int(meta.get("unparsed_items") or 0)
        meta_diff_raw = meta.get("sum_check_diff")
        meta_diff = None
        if isinstance(meta_diff_raw, (int, float)):
            meta_diff = float(meta_diff_raw)

        warn_parts = []
        if meta_needs_review:
            warn_parts.append("OCR пометил чек как требующий проверки")
        if meta_math_errors > 0:
            warn_parts.append(f"арифметических ошибок в позициях: {meta_math_errors}")
        if meta_unparsed_items > 0:
            warn_parts.append(f"неразобранных item_total: {meta_unparsed_items}")
        if meta_diff is not None and abs(meta_diff) > 0.01:
            warn_parts.append(
                f"несоответствие суммы чека и позиций (по OCR): {meta_diff:.2f}"
            )
        elif mismatch_delta is not None and abs(mismatch_delta) > 0.01:
            warn_parts.append(
                f"несоответствие суммы чека и позиций: {mismatch_delta:.2f}"
            )

        if warn_parts:
            if validation_status in ("", "ok"):
                validation_status = "check"
            if validation_comment:
                validation_comment = f"{validation_comment}; {'; '.join(warn_parts)}"
            else:
                validation_comment = "; ".join(warn_parts)

    service = get_sheets_service()
    sheet = service.spreadsheets()

    # Проверка на дубликат:
    # если комбинация (receipt_number + date + total_amount) уже есть в таблице,
    # повторно строку не добавляем (защита от повторной загрузки одного и того же чека).
    composite_new = f"{receipt_number}|{date}|{total_amount}"

    if composite_new != "||":
        existing = (
            sheet.values()
            .get(
                spreadsheetId=GOOGLE_SHEETS_SPREADSHEET_ID,
                range=f"{sheet_name}!A:N",
            )
            .execute()
        )
        for row_idx, row in enumerate(existing.get("values", []), start=1):
            # A:org, B:inn, C:date, D:time, E:receipt_number, ..., J:total_amount
            existing_date = row[2] if len(row) > 2 else ""
            existing_receipt_number = row[4] if len(row) > 4 else ""
            existing_total_amount = row[9] if len(row) > 9 else ""
            composite_existing = (
                f"{existing_receipt_number}|{existing_date}|{existing_total_amount}"
            )
            if composite_existing == composite_new:
                # Дубликат найден. Если текущий прогон выявил предупреждения,
                # обновляем статус/комментарий существующей строки.
                if validation_status and validation_status != "ok":
                    sheet.values().update(
                        spreadsheetId=GOOGLE_SHEETS_SPREADSHEET_ID,
                        range=f"{sheet_name}!M{row_idx}:N{row_idx}",
                        valueInputOption="USER_ENTERED",
                        body={"values": [[validation_status, validation_comment]]},
                    ).execute()
                return {
                    "skipped": True,
                    "reason": "duplicate_receipt",
                    "receipt_number": receipt_number,
                    "date": date,
                    "total_amount": total_amount,
                    "warning_updated": bool(validation_status and validation_status != "ok"),
                    "validation_status": validation_status,
                    "validation_comment": validation_comment,
                }

    values = [
        [
            organization,
            inn,
            date,
            time,
            receipt_number,
            shift_number,
            fiscal_sign,
            payment_type,
            total_discount,
            total_amount,
            vat,
            items_count,
            validation_status,
            validation_comment,
            fiscal_storage_number,
            qr_recognized_yes_no,
        ]
    ]

    body = {"values": values}

    # A:organization, B:inn, C:date, D:time, E:receipt_number,
    # F:shift_number, G:fiscal_sign, H:payment_type,
    # I:total_discount, J:total_amount, K:vat, L:items_count,
    # M:validation_status, N:validation_comment, O:fiscal_storage_number (ФН из QR),
    # P:qr распознан (Да/Нет, по _meta.qr.used)
    range_name = f"{sheet_name}!A:P"

    result = (
        sheet.values()
        .append(
            spreadsheetId=GOOGLE_SHEETS_SPREADSHEET_ID,
            range=range_name,
            valueInputOption="USER_ENTERED",
            body=body,
        )
        .execute()
    )

    # Записываем позиции в лист "Товар"
    # receipt_id = composite key для связи с чеком
    receipt_id = composite_new if composite_new != "||" else f"{organization}|{date}|{time}"
    items_sheet = GOOGLE_SHEETS_ITEMS_SHEET_NAME

    if items:
        item_rows = []
        for idx, item in enumerate(items, start=1):
            name = item.get("name") or ""
            quantity = _to_sheet_numeric_text(item.get("quantity") or "")
            unit_price = _to_sheet_numeric_text(item.get("unit_price") or "")
            item_discount = _to_sheet_numeric_text(item.get("item_discount") or "")
            item_total = _to_sheet_numeric_text(item.get("item_total") or "")
            item_rows.append(
                [
                    receipt_id,
                    idx,
                    name,
                    quantity,
                    unit_price,
                    item_discount,
                    item_total,
                ]
            )

        items_body = {"values": item_rows}
        items_range = f"{items_sheet}!A:G"
        items_append_result = sheet.values().append(
            spreadsheetId=GOOGLE_SHEETS_SPREADSHEET_ID,
            range=items_range,
            valueInputOption="USER_ENTERED",
            body=items_body,
        ).execute()

        # Добавляем визуальный итог по текущему чеку:
        # в последнюю строку его позиций в колонку H пишем формулу
        # =СУММ(Gx:Gxx), где x..xx — добавленный блок item_total.
        updated_range = (items_append_result.get("updates") or {}).get("updatedRange", "")
        start_row, end_row = _extract_row_bounds(updated_range)
        if start_row is not None and end_row is not None and end_row >= start_row:
            # Заголовок колонки итогов.
            sheet.values().update(
                spreadsheetId=GOOGLE_SHEETS_SPREADSHEET_ID,
                range=f"{items_sheet}!H1",
                valueInputOption="USER_ENTERED",
                body={"values": [["total"]]},
            ).execute()

            # Итог только в последней строке текущего чека.
            total_formula = f"=СУММ(G{start_row}:G{end_row})"
            sheet.values().update(
                spreadsheetId=GOOGLE_SHEETS_SPREADSHEET_ID,
                range=f"{items_sheet}!H{end_row}",
                valueInputOption="USER_ENTERED",
                body={"values": [[total_formula]]},
            ).execute()

    result["validation_status"] = validation_status
    result["validation_comment"] = validation_comment
    return result


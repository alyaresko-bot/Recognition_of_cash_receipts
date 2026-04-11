from __future__ import annotations

import base64
import json
import os
import shutil
import tempfile
from typing import Any, Dict, List, Optional, Sequence

from openai import OpenAI

from config import (
    OPENAI_API_KEY,
    OPENAI_MODEL,
    RECEIPT_PREPROCESS_ENABLED,
    RECEIPT_PREPROCESS_CROP_ENABLED,
    RECEIPT_PREPROCESS_MAX_SIDE,
    RECEIPT_QR_ENABLED,
    SYSTEM_PROMPT_PATH,
)
from image_preprocess import preprocess_receipt_image
from receipt_qr import (
    apply_qr_patch_to_receipt,
    extract_fiscal_data_from_receipt_images,
    qr_context_for_prompt,
)


def load_system_prompt() -> str:
    if not os.path.exists(SYSTEM_PROMPT_PATH):
        return ""
    with open(SYSTEM_PROMPT_PATH, "r", encoding="utf-8") as f:
        return f.read()


def _get_openai_client() -> OpenAI:
    if not OPENAI_API_KEY:
        raise RuntimeError(
            "OPENAI_API_KEY не задан. Укажите его в файле .env или переменных окружения."
        )
    return OpenAI(api_key=OPENAI_API_KEY)


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
    for ch in ["₽", "р", "RUB", "руб.", "руб"]:
        txt = txt.replace(ch, "")
    txt = txt.replace(",", ".").strip()
    try:
        return float(txt)
    except ValueError:
        return None


def _extract_json_object(text: str) -> Optional[str]:
    if not text:
        return None
    s = text.strip()
    # Убираем возможные markdown-кодфенсы
    if s.startswith("```"):
        s = s.strip("`")
        # на случай ```json ... ```
        if "\n" in s:
            s = s.split("\n", 1)[-1].rsplit("\n", 1)[0]
    start = s.find("{")
    end = s.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    return s[start : end + 1]


def _parse_model_json(content: str) -> Dict[str, Any]:
    try:
        return json.loads(content)
    except Exception:
        pass
    extracted = _extract_json_object(content)
    if extracted is None:
        raise ValueError("Не удалось извлечь JSON-объект из ответа модели.")
    try:
        return json.loads(extracted)
    except json.JSONDecodeError as exc:
        raise ValueError("Некорректный JSON в ответе модели.") from exc


def _image_paths_to_content_parts(
    image_paths: Sequence[str],
    user_text: str,
) -> List[Dict[str, Any]]:
    parts: List[Dict[str, Any]] = [{"type": "text", "text": user_text}]
    for path in image_paths:
        with open(path, "rb") as f:
            img_bytes = f.read()
        img_b64 = base64.b64encode(img_bytes).decode("utf-8")
        parts.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"},
            }
        )
    return parts


def analyze_receipt_image(
    image_path: Optional[str] = None,
    image_paths: Optional[Sequence[str]] = None,
    extra_context: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Анализирует одну или несколько фотографий одного чека (vision) и возвращает
    JSON-структуру в формате, описанном в system_prompt.txt.
    """
    paths: List[str] = []
    if image_paths is not None:
        paths = list(image_paths)
    elif image_path is not None:
        paths = [image_path]
    if not paths:
        raise ValueError("Укажите image_path или непустой image_paths.")

    client = _get_openai_client()
    system_prompt = load_system_prompt()

    with tempfile.TemporaryDirectory() as prep_dir:
        processed_paths: List[str] = []
        for i, p in enumerate(paths):
            outp = os.path.join(prep_dir, f"prep_{i}.jpg")
            if RECEIPT_PREPROCESS_ENABLED:
                # Для длинных чеков не обрезаем кадры эвристикой:
                # важнее сохранить все строки, чем агрессивно резать поля.
                crop_enabled = RECEIPT_PREPROCESS_CROP_ENABLED and len(paths) == 1
                preprocess_receipt_image(
                    p,
                    outp,
                    max_side=RECEIPT_PREPROCESS_MAX_SIDE,
                    crop_enabled=crop_enabled,
                )
            else:
                shutil.copy2(p, outp)
            processed_paths.append(outp)
        paths = processed_paths

        qr_bundle: Optional[Dict[str, Any]] = None
        if RECEIPT_QR_ENABLED:
            qr_bundle = extract_fiscal_data_from_receipt_images(paths)
        qr_prompt_extra = (
            qr_context_for_prompt(qr_bundle) if qr_bundle else ""
        )

        def build_user_text(*, attempt: int) -> str:
            if len(paths) == 1:
                user_text_parts = [
                    "Это фотография кассового чека. Выполни распознавание и верни строго JSON в формате из системного промпта.",
                ]
            else:
                user_text_parts = [
                    f"Ниже передано {len(paths)} фрагментов одного длинного кассового чека "
                    "(сверху вниз, в порядке отправки). Объедини информацию в один логический чек "
                    "и верни строго JSON в формате из системного промпта.",
                    "Ожидается, что пользователь передал кадры без пересечений позиций между соседними фото. "
                    "Не пропускай ни одной позиции: верни все строки товаров из всех кадров. "
                    "Нельзя обрезать, сокращать или объединять строки товаров.",
                    "Если на стыке кадров всё же встретились одинаковые строки, удаляй только явные полные дубли, "
                    "но не удаляй похожие, но разные товары.",
                    "Если названия отличаются составом (телятина/ягненок/курица/индейка и т.п.), это разные строки и их нужно вернуть отдельно.",
                ]

            if extra_context:
                user_text_parts.append(
                    f"Дополнительный контекст от пользователя: {extra_context}"
                )

            if qr_prompt_extra:
                user_text_parts.append(qr_prompt_extra)

            if attempt >= 2:
                user_text_parts.append(
                    "В этой повторной попытке обязательно сделай самопроверку: "
                    "total_amount (итог по чеку) должен быть равен "
                    "(gross_sum - discount_sum), где gross_sum = сумма item_total по всем items, "
                    "discount_sum = сумма item_discount по всем items. "
                    "Если не совпадает — добавь пропущенные позиции и/или исправь item_total и item_discount. "
                    "Если видишь выражение вида `цена,цц*количество = сумма`, то unit_price = левая часть "
                    "(до `*`), quantity = правая часть (после `*`), item_total = сумма после `=`. "
                    "Не подставляй quantity=1, если в выражении явно стоит другое число. "
                    "Строка `СКИДКА ... = X` всегда относится только к непосредственно предыдущей позиции "
                    "(последней строке `цена*количество = сумма`) и не бывает общей на несколько позиций. "
                    "Не объединяй разные варианты состава в одну строку даже если основная часть названия совпадает. "
                    "Возвращай полный список позиций без пропусков, удаления и обрезания строк товаров. "
                    "Проверь по каждой позиции арифметику: unit_price * quantity должно совпасть с item_total "
                    "(скидка идёт отдельной строкой и не входит в item_total). "
                    "Не переносить числа между соседними позициями."
                )
            return "\n".join(user_text_parts)

        def should_recheck(receipt_data: Dict[str, Any]) -> bool:
            receipt_info = receipt_data.get("receipt_info") or {}
            total_amount = receipt_info.get("total_amount")
            receipt_total_num = _to_number(total_amount)

            items = receipt_data.get("items") or []
            if not isinstance(items, list):
                return True

            unparsed_items = 0
            items_total_num: Optional[float] = 0.0
            items_discount_num: Optional[float] = 0.0
            unparsed_discounts = 0
            math_error_count = 0
            for item in items:
                if not isinstance(item, dict):
                    return True
                num = _to_number(item.get("item_total"))
                if num is None:
                    unparsed_items += 1
                else:
                    items_total_num += num

                disc = _to_number(item.get("item_discount"))
                if disc is None and item.get("item_discount") not in (None, "", 0):
                    unparsed_discounts += 1
                elif disc is not None:
                    items_discount_num += disc

                # Арифметическая валидация по каждой позиции:
                # unit_price * quantity (до скидки) должен совпадать с item_total.
                unit_price_num = _to_number(item.get("unit_price"))
                quantity_num = _to_number(item.get("quantity"))
                item_total_num = _to_number(item.get("item_total"))

                if (
                    unit_price_num is not None
                    and quantity_num is not None
                    and item_total_num is not None
                ):
                    base = unit_price_num * quantity_num
                    tol = max(0.01, abs(item_total_num) * 0.01)
                    ok_before_discount = abs(base - item_total_num) <= tol
                    # Если арифметика не сходится — модель, вероятно,
                    # перенесла числа между строками.
                    if not ok_before_discount:
                        math_error_count += 1

            # Если итог чека не распознан — трудно сравнивать, но часто это признак неполной обработки.
            if receipt_total_num is None:
                return True

            # Если по позициям не удаётся посчитать — повторим.
            if unparsed_items > 0 and not items_total_num:
                return True

            # Если есть явное недостающее по сумме — повторяем.
            if items_total_num is None:
                return True

            # total_amount в чеке уже после скидок.
            # По позиции: item_total = цена*количество (до скидки),
            # item_discount = скидка отдельной строкой.
            net_sum = float(items_total_num) - float(items_discount_num or 0.0)
            missing = float(receipt_total_num) - net_sum
            tol_missing = max(1.0, float(receipt_total_num) * 0.01)
            if missing > tol_missing:
                return True
            # Избыточная сумма часто означает задвоение строк на стыках кадров.
            if missing < -tol_missing:
                return True

            if math_error_count > 0:
                return True

            # Если много позиций с неразобранной суммой — тоже повторяем.
            if unparsed_items >= max(2, int(len(items) * 0.25)):
                return True

            # Если много строк скидок распознаны не как числа — тоже повторим.
            if unparsed_discounts >= max(2, int(len(items) * 0.25)):
                return True

            return False

        def call_llm(attempt: int) -> Dict[str, Any]:
            user_text = build_user_text(attempt=attempt)
            content_parts = _image_paths_to_content_parts(paths, user_text)
            response = client.chat.completions.create(
                model=OPENAI_MODEL,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": content_parts},
                ],
                temperature=0,
            )
            content = response.choices[0].message.content or ""
            try:
                return _parse_model_json(content)
            except Exception:
                return {
                    "receipt_info": {
                        "organization": None,
                        "inn": None,
                        "date": None,
                        "time": None,
                        "receipt_number": None,
                        "shift_number": None,
                        "fiscal_sign": None,
                        "payment_type": None,
                        "total_discount": None,
                        "total_amount": None,
                        "vat": None,
                    },
                    "items": [],
                    "_meta": {"raw_response": content, "parse_error": True},
                }

        parsed = call_llm(attempt=1)
        needs_retry = should_recheck(parsed)
        attempts_used = 1
        if needs_retry:
            parsed = call_llm(attempt=2)
            attempts_used = 2
        if qr_bundle:
            apply_qr_patch_to_receipt(parsed, qr_bundle)
        # Флаг для пользователя/таблицы: только по финальному JSON после всех попыток.
        # Иначе при needs_retry=True первая попытка оставляла needs_review=True навсегда,
        # даже если вторая попытка всё исправила — бот показывал ложное предупреждение.
        needs_review = should_recheck(parsed)
        # Добавляем мета-информацию для бота (не влияет на запись в Sheets)
        receipt_info = parsed.get("receipt_info") or {}
        receipt_total_num = _to_number(receipt_info.get("total_amount"))
        items = parsed.get("items") or []
        items_total_num: Optional[float] = 0.0
        items_discount_num: Optional[float] = 0.0
        unparsed_items = 0
        unparsed_discounts = 0
        math_error_count = 0
        if isinstance(items, list):
            for item in items:
                num = _to_number(item.get("item_total") if isinstance(item, dict) else None)
                if num is None:
                    unparsed_items += 1
                else:
                    items_total_num += num

                if isinstance(item, dict):
                    disc = _to_number(item.get("item_discount"))
                    if disc is None and item.get("item_discount") not in (None, "", 0):
                        unparsed_discounts += 1
                    elif disc is not None:
                        items_discount_num += disc

                if not isinstance(item, dict):
                    continue
                unit_price_num = _to_number(item.get("unit_price"))
                quantity_num = _to_number(item.get("quantity"))
                item_total_num = _to_number(item.get("item_total"))
                if (
                    unit_price_num is not None
                    and quantity_num is not None
                    and item_total_num is not None
                ):
                    base = unit_price_num * quantity_num
                    tol = max(0.01, abs(item_total_num) * 0.01)
                    ok_before_discount = abs(base - item_total_num) <= tol
                    if not ok_before_discount:
                        math_error_count += 1

        if receipt_total_num is not None and items_total_num is not None:
            gross_sum = float(items_total_num)
            discount_sum = float(items_discount_num or 0.0)
            net_sum = gross_sum - discount_sum
            diff = float(receipt_total_num) - net_sum
        else:
            diff = None
        parsed["_meta"] = parsed.get("_meta") or {}
        qr_meta: Dict[str, Any] = {"used": bool(qr_bundle)}
        if qr_bundle:
            qr_meta["decoder"] = qr_bundle.get("decoder")
            qr_meta["raw"] = qr_bundle.get("raw")
        parsed["_meta"].update(
            {
                "needs_review": bool(needs_review),
                "attempts_used": attempts_used,
                "sum_check_diff": diff,
                "unparsed_items": unparsed_items if isinstance(unparsed_items, int) else 0,
                "math_error_count": math_error_count,
                "unparsed_discounts": unparsed_discounts,
                "qr": qr_meta,
            }
        )

        return parsed


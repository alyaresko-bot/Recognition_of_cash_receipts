from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Sequence
from urllib.parse import parse_qsl, unquote, urlparse

import cv2

logger = logging.getLogger(__name__)

# Ленивая инициализация (тяжёлые веса YOLO при первом импорте/создании).
_qreader_instance: Any = None
_qreader_failed: bool = False


def _get_qreader() -> Any:
    global _qreader_instance, _qreader_failed
    if _qreader_failed:
        return None
    if _qreader_instance is not None:
        return _qreader_instance
    try:
        from qreader import QReader  # type: ignore[import-untyped]

        _qreader_instance = QReader()
        return _qreader_instance
    except Exception as exc:  # noqa: BLE001
        _qreader_failed = True
        logger.info("QReader недоступен (%s), для QR используется OpenCV.", exc)
        return None


def _flatten_decoder_output(res: Any) -> List[str]:
    if res is None:
        return []
    if isinstance(res, str):
        s = res.strip()
        return [s] if s else []
    if isinstance(res, (list, tuple)):
        out: List[str] = []
        for x in res:
            out.extend(_flatten_decoder_output(x))
        return out
    return []


def _decode_strings_qreader(image_bgr: Any) -> List[str]:
    reader = _get_qreader()
    if reader is None:
        return []
    try:
        res = reader.detect_and_decode(image=image_bgr)
    except Exception as exc:  # noqa: BLE001
        logger.debug("QReader detect_and_decode: %s", exc)
        return []
    return _flatten_decoder_output(res)


def _decode_strings_opencv(image_bgr: Any) -> List[str]:
    det = cv2.QRCodeDetector()
    out: List[str] = []
    try:
        ok, decoded, _pts, _straight = det.detectAndDecodeMulti(image_bgr)
        if ok and decoded is not None:
            for s in decoded:
                if s:
                    out.append(str(s))
    except Exception:  # noqa: BLE001
        pass
    if not out:
        try:
            ok, s = det.detectAndDecode(image_bgr)
            if ok and s:
                out.append(str(s))
        except Exception:  # noqa: BLE001
            pass
    return out


def _extract_query_string(raw: str) -> str:
    s = raw.strip()
    if not s:
        return ""
    s = unquote(s)
    low = s.lower()
    if low.startswith("http://") or low.startswith("https://"):
        parsed = urlparse(s)
        if parsed.query:
            return parsed.query
        if parsed.fragment and "=" in parsed.fragment:
            return parsed.fragment
        return ""
    if "?" in s:
        return s.split("?", 1)[-1]
    return s


def _parse_t_datetime(t_raw: str) -> tuple[Optional[str], Optional[str]]:
    t_raw = str(t_raw).strip()
    if not t_raw:
        return None, None
    if "T" in t_raw:
        dpart, tpart = t_raw.split("T", 1)
    elif len(t_raw) >= 12 and t_raw[:8].isdigit():
        dpart, tpart = t_raw[:8], t_raw[8:]
    else:
        dpart, tpart = t_raw, ""

    date_out: Optional[str] = None
    if len(dpart) == 8 and dpart.isdigit():
        date_out = f"{dpart[6:8]}.{dpart[4:6]}.{dpart[0:4]}"

    time_out: Optional[str] = None
    t_digits = re.sub(r"\D", "", tpart)
    if len(t_digits) >= 4:
        hh, mm = t_digits[0:2], t_digits[2:4]
        if len(t_digits) >= 6:
            ss = t_digits[4:6]
            time_out = f"{hh}:{mm}:{ss}"
        else:
            time_out = f"{hh}:{mm}"
    elif tpart.strip():
        time_out = tpart.strip()

    if date_out and time_out is None:
        time_out = "00:00"

    return date_out, time_out


def _amount_to_total_amount_str(s_raw: str) -> Optional[str]:
    s_raw = str(s_raw).strip().replace("\u00A0", "").replace(" ", "")
    if not s_raw:
        return None
    if "," in s_raw or "." in s_raw:
        normalized = s_raw.replace(",", ".")
        try:
            rub_float = float(normalized)
        except ValueError:
            return None
        neg = rub_float < 0
        kop_total = int(round(abs(rub_float) * 100 + 1e-6))
        rub = kop_total // 100
        kop = kop_total % 100
        body = f"{rub},{kop:02d}"
        return f"-{body}" if neg else body

    try:
        kopecks = int(s_raw, 10)
    except ValueError:
        return None
    neg = kopecks < 0
    k = abs(kopecks)
    rub = k // 100
    kop = k % 100
    body = f"{rub},{kop:02d}"
    return f"-{body}" if neg else body


def parse_fiscal_qr_payload(raw: str) -> Optional[Dict[str, str]]:
    """
    Парсит полезную нагрузку QR ФНС (строка query-параметров или URL с ними).

    Ожидаемые ключи: t, s, fn, i (или fd), fp.
    - t — дата/время
    - s — сумма (обычно в копейках)
    - fn — номер фискального накопителя
    - i / fd — номер фискального документа (уникальный номер чека в разрезе ФН)
    - fp — фискальный признак (криптографическая подпись в терминах ФФД)
    """
    qs = _extract_query_string(raw)
    if not qs or "=" not in qs:
        return None
    params = {k.lower(): v for k, v in parse_qsl(qs, keep_blank_values=True)}
    if not params and "=" in raw:
        params = {k.lower(): v for k, v in parse_qsl(raw.strip(), keep_blank_values=True)}

    fn = (params.get("fn") or "").strip()
    fp_val = (params.get("fp") or "").strip()
    doc_id = (params.get("i") or params.get("fd") or "").strip()
    t_raw = (params.get("t") or "").strip()
    s_raw = (params.get("s") or "").strip()

    if not (fn and fp_val and doc_id and t_raw and s_raw):
        return None

    date_s, time_s = _parse_t_datetime(t_raw)
    if not date_s or not time_s:
        return None

    total_s = _amount_to_total_amount_str(s_raw)
    if not total_s:
        return None

    return {
        "date": date_s,
        "time": time_s,
        "receipt_number": doc_id,
        "fiscal_sign": fp_val,
        "total_amount": total_s,
        "fiscal_storage_number": fn,
    }


def extract_fiscal_data_from_receipt_images(
    image_paths: Sequence[str],
) -> Optional[Dict[str, Any]]:
    """
    Пытается извлечь фискальные поля из QR на одном из изображений.
    Сначала QReader (если установлен), затем OpenCV QRCodeDetector.
    """
    for path in image_paths:
        img = cv2.imread(path)
        if img is None:
            continue

        seen_raw: set[str] = set()
        ordered_strings: List[tuple[str, str]] = []

        for s in _decode_strings_qreader(img):
            if s and s not in seen_raw:
                seen_raw.add(s)
                ordered_strings.append(("qreader", s))

        for s in _decode_strings_opencv(img):
            if s and s not in seen_raw:
                seen_raw.add(s)
                ordered_strings.append(("opencv", s))

        for decoder, raw in ordered_strings:
            patch = parse_fiscal_qr_payload(raw)
            if patch:
                return {
                    "decoder": decoder,
                    "raw": raw,
                    "receipt_info_patch": patch,
                }
    return None


def qr_context_for_prompt(bundle: Dict[str, Any]) -> str:
    """Текст для user-сообщения к LLM (подсказка + согласование суммы позиций)."""
    patch = bundle.get("receipt_info_patch") or {}
    lines = [
        "Из QR-кода кассового чека (данные ФНС) извлечено:",
        f"- дата: {patch.get('date', '')}",
        f"- время: {patch.get('time', '')}",
        f"- номер фискального накопителя (fn): {patch.get('fiscal_storage_number', '')}",
        f"- номер фискального документа / уникальный номер чека (fd/i): {patch.get('receipt_number', '')}",
        f"- фискальный признак (fp): {patch.get('fiscal_sign', '')}",
        f"- итоговая сумма (s): {patch.get('total_amount', '')}",
        "Используй эти значения в receipt_info для соответствующих полей (дата, время, номер чека = ФД, "
        "фискальный признак, итог). Номер ФН хранится отдельно — в JSON его указывать не нужно, "
        "он будет подставлен программно.",
        "Сумма позиций (net_sum) должна сходиться с итогом из QR.",
    ]
    return "\n".join(lines)


def apply_qr_patch_to_receipt(parsed: Dict[str, Any], bundle: Dict[str, Any]) -> None:
    """Перезаписывает в ответе LLM поля, извлечённые из QR (эталон)."""
    patch = bundle.get("receipt_info_patch")
    if not isinstance(patch, dict):
        return
    info = parsed.get("receipt_info")
    if not isinstance(info, dict):
        info = {}
        parsed["receipt_info"] = info
    for key in (
        "date",
        "time",
        "receipt_number",
        "fiscal_sign",
        "total_amount",
        "fiscal_storage_number",
    ):
        val = patch.get(key)
        if val is not None and str(val).strip():
            info[key] = str(val).strip()

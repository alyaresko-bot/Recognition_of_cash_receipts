"""
Предобработка фото чека перед отправкой в vision API:
- автоповорот по EXIF Orientation;
- ограничение длинной стороны (меньше шума и размера запроса);
- обрезка полей: область с телом чека (адаптивный порог + контуры; запасной вариант — «небелый» фон).
"""

from __future__ import annotations

import logging
import shutil
from typing import Optional, Tuple

import cv2
import numpy as np
from PIL import Image, ImageOps

logger = logging.getLogger(__name__)


def _resize_pil_max_side(img: Image.Image, max_side: int) -> Image.Image:
    w, h = img.size
    m = max(w, h)
    if m <= max_side:
        return img
    scale = max_side / m
    nw, nh = int(w * scale), int(h * scale)
    return img.resize((nw, nh), Image.Resampling.LANCZOS)


def _content_bbox_adaptive(gray: np.ndarray) -> Optional[Tuple[int, int, int, int]]:
    """Область чека по адаптивному порогу и морфологии."""
    h, w = gray.shape[:2]
    area_img = float(h * w)
    blur = cv2.GaussianBlur(gray, (5, 5), 0)
    th = cv2.adaptiveThreshold(
        blur,
        255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY_INV,
        35,
        11,
    )
    kx = max(15, w // 40)
    ky = max(5, h // 120)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (kx, ky))
    th = cv2.morphologyEx(th, cv2.MORPH_CLOSE, kernel, iterations=2)
    contours, _ = cv2.findContours(th, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None

    best_area = 0.0
    best: Optional[np.ndarray] = None
    for c in contours:
        a = cv2.contourArea(c)
        if a < area_img * 0.04:
            continue
        if a > area_img * 0.97:
            continue
        if a > best_area:
            best_area = a
            best = c
    if best is None:
        return None
    x, y, bw, bh = cv2.boundingRect(best)
    return int(x), int(y), int(bw), int(bh)


def _content_bbox_fallback(gray: np.ndarray, white_thresh: int = 248) -> Optional[Tuple[int, int, int, int]]:
    """Запасной вариант: bbox вокруг небелых пикселей."""
    _, bin_inv = cv2.threshold(gray, white_thresh, 255, cv2.THRESH_BINARY_INV)
    coords = cv2.findNonZero(bin_inv)
    if coords is None:
        return None
    x, y, bw, bh = cv2.boundingRect(coords)
    h, w = gray.shape[:2]
    if bw * bh > 0.99 * w * h:
        return None
    if bw * bh < 0.03 * w * h:
        return None
    return int(x), int(y), int(bw), int(bh)


def _pad_bbox(
    x: int,
    y: int,
    bw: int,
    bh: int,
    frame_w: int,
    frame_h: int,
    pad_ratio: float = 0.02,
) -> Tuple[int, int, int, int]:
    pad = max(8, int(round(pad_ratio * max(frame_w, frame_h))))
    x0 = max(0, x - pad)
    y0 = max(0, y - pad)
    x1 = min(frame_w, x + bw + pad)
    y1 = min(frame_h, y + bh + pad)
    return x0, y0, x1 - x0, y1 - y0


def preprocess_receipt_image(
    src_path: str,
    dst_path: str,
    *,
    max_side: int = 3000,
    crop_enabled: bool = True,
) -> None:
    """
    EXIF-поворот, уменьшение по длинной стороне, обрезка полей, сохранение JPEG.
    При ошибке чтения — копирование исходного файла; при ошибке обрезки — сохранение без неё.
    """
    try:
        pil = Image.open(src_path)
        pil = ImageOps.exif_transpose(pil)
        pil = pil.convert("RGB")
    except Exception as exc:  # noqa: BLE001
        logger.warning("PIL не смог открыть %s: %s — копируем файл как есть", src_path, exc)
        shutil.copy2(src_path, dst_path)
        return

    try:
        pil = _resize_pil_max_side(pil, max_side)
        rgb = np.array(pil)
        gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)

        h, w = rgb.shape[:2]
        crop = rgb
        if crop_enabled:
            bbox = _content_bbox_adaptive(gray)
            if bbox is None:
                bbox = _content_bbox_fallback(gray)

            if bbox is not None:
                x, y, bw, bh = bbox
                # Увеличиваем отступ, чтобы случайно не отрезать последние строки.
                x, y, bw, bh = _pad_bbox(x, y, bw, bh, w, h, pad_ratio=0.04)

                # Если bbox покрывает заметно меньшую часть по высоте/ширине, вероятно
                # контур «сел» на верхнюю/среднюю/боковую зону. Тогда лучше не обрезать,
                # чтобы не терять строки в середине чека.
                height_ratio = bh / float(h) if h else 1.0
                width_ratio = bw / float(w) if w else 1.0
                if height_ratio < 0.85 or width_ratio < 0.65:
                    crop = rgb
                elif bw * bh >= 0.18 * w * h:
                    crop = rgb[y : y + bh, x : x + bw]
                else:
                    crop = rgb

        out = Image.fromarray(crop)
        out.save(dst_path, format="JPEG", quality=92, optimize=True)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Обрезка не удалась (%s), сохраняем без неё", exc)
        try:
            pil = Image.open(src_path)
            pil = ImageOps.exif_transpose(pil)
            pil = pil.convert("RGB")
            pil = _resize_pil_max_side(pil, max_side)
            pil.save(dst_path, format="JPEG", quality=92, optimize=True)
        except Exception as exc2:  # noqa: BLE001
            logger.warning("Запасное сохранение не удалось: %s — копируем исходник", exc2)
            shutil.copy2(src_path, dst_path)

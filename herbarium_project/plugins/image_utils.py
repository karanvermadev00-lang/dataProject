import json
import os
import uuid
from typing import List, Tuple
import cv2
import numpy as np
from PIL import Image
import os, uuid
from PIL import Image
import pytesseract
# Default LWG sheet: English block from "HERBARIUM NATIONAL BOTANIC GARDENS..." through NOTES.
# Normalized [ymin, xmin, ymax, xmax] on 0–1000 scale (per image height/width).
DEFAULT_LABEL_BBOX_NORM_1000: Tuple[int, int, int, int] = (615, 583, 1000, 1000)


def create_optimized_icon(
    original_image_path: str,
    output_icon_path: str,
    size: Tuple[int, int] = (200, 200),
    quality: int = 85,
) -> str:
    """
    Create a 200x200 optimized JPEG icon from the original image.

    The icon is generated as a square canvas (no distortion) with the resized image
    centered on a white background.
    """
    if not os.path.exists(original_image_path):
        raise FileNotFoundError(f"Original image not found: {original_image_path}")

    output_dir = os.path.dirname(output_icon_path)
    os.makedirs(output_dir, exist_ok=True)

    # Atomic write: generate to a temporary file and swap in place.
    # This prevents partial/corrupted icons when multiple workers run concurrently.
    tmp_output_path = os.path.join(
        output_dir,
        f".{os.path.basename(output_icon_path)}.{uuid.uuid4().hex}.tmp",
    )

    try:
        with Image.open(original_image_path) as img:
            # Fail fast on pathological inputs so we don't decode massive/invalid images
            # (which can OOM the container).
            max_source_pixels = int(os.getenv("HERBARIUM_MAX_SOURCE_PIXELS", "80000000"))
            w, h = img.size
            src_pixels = int(w) * int(h)
            if src_pixels > max_source_pixels:
                raise ValueError(f"Image too large for icon generation: {w}x{h} ({src_pixels} pixels)")

            img = img.convert("RGB")

            # Resize while preserving aspect ratio.
            img.thumbnail(size, Image.LANCZOS)

            # Create square canvas and paste resized image centered.
            canvas = Image.new("RGB", size, (255, 255, 255))
            x = (size[0] - img.size[0]) // 2
            y = (size[1] - img.size[1]) // 2
            canvas.paste(img, (x, y))

            canvas.save(tmp_output_path, format="JPEG", quality=quality, optimize=True)

        # Replace after successful save.
        os.replace(tmp_output_path, output_icon_path)
    except Exception:
        # Best-effort cleanup of partial tmp file.
        try:
            if os.path.exists(tmp_output_path):
                os.remove(tmp_output_path)
        finally:
            raise

    return output_icon_path


def resolve_icon_output_path(
    icon_dir: str,
    filename: str,
) -> str:
    # Keep output filename stable so reruns don't rewrite unnecessarily.
    base = os.path.basename(filename)
    if not base.lower().endswith((".jpg", ".jpeg")):
        base = f"{base}.jpg"
    return os.path.join(icon_dir, base)


def _denormalize_box_1000(
    ymin: int,
    xmin: int,
    ymax: int,
    xmax: int,
    width: int,
    height: int,
) -> Tuple[int, int, int, int]:
    """Map 0–1000 normalized box to PIL crop box (left, upper, right, lower)."""
    left = int(xmin / 1000.0 * width)
    upper = int(ymin / 1000.0 * height)
    right = int(xmax / 1000.0 * width)
    lower = int(ymax / 1000.0 * height)
    left = max(0, min(left, width - 1))
    upper = max(0, min(upper, height - 1))
    right = max(left + 1, min(right, width))
    lower = max(upper + 1, min(lower, height))
    return left, upper, right, lower


def _parse_bbox_norm_csv(raw: str) -> Tuple[int, int, int, int]:
    parts: List[int] = [int(x.strip()) for x in raw.split(",") if x.strip()]
    if len(parts) != 4:
        raise ValueError("Expected HERBARIUM_LABEL_BBOX_NORM as ymin,xmin,ymax,xmax (4 integers, 0–1000)")
    return parts[0], parts[1], parts[2], parts[3]


def _resolve_label_bbox_norm_1000(original_image_path: str) -> Tuple[int, int, int, int]:
    """
    Resolution order:
    1) JSON map file (HERBARIUM_LABEL_BBOX_MAP_JSON): key = basename, else \"default\"
    2) HERBARIUM_LABEL_BBOX_NORM env (ymin,xmin,ymax,xmax)
    3) Built-in DEFAULT_LABEL_BBOX_NORM_1000 (LWG English block)
    """
    basename = os.path.basename(original_image_path)
    map_path = os.getenv("HERBARIUM_LABEL_BBOX_MAP_JSON", "").strip()
    if map_path and os.path.isfile(map_path):
        with open(map_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        for key in (basename, basename.lower()):
            if key in data and isinstance(data[key], (list, tuple)) and len(data[key]) == 4:
                return tuple(int(x) for x in data[key])  # type: ignore[return-value]
        if "default" in data and isinstance(data["default"], (list, tuple)) and len(data["default"]) == 4:
            return tuple(int(x) for x in data["default"])  # type: ignore[return-value]

    raw = os.getenv("HERBARIUM_LABEL_BBOX_NORM", "").strip()
    if raw:
        return _parse_bbox_norm_csv(raw)

    return DEFAULT_LABEL_BBOX_NORM_1000


def create_label_image(
    original_image_path: str,
    output_label_path: str,
    quality: int = 85,
) -> str:

    if not os.path.exists(original_image_path):
        raise FileNotFoundError(f"Original image not found: {original_image_path}")

    output_dir = os.path.dirname(output_label_path)
    os.makedirs(output_dir, exist_ok=True)

    tmp_output_path = os.path.join(
        output_dir,
        f".{os.path.basename(output_label_path)}.{uuid.uuid4().hex}.tmp",
    )

    try:
        # 🔥 TRY DYNAMIC SEGMENTATION FIRST
        img_cv = cv2.imread(original_image_path)

        if img_cv is not None:
            gray = cv2.cvtColor(img_cv, cv2.COLOR_BGR2GRAY)

    # OCR detection
            data = pytesseract.image_to_data(gray, output_type=pytesseract.Output.DICT)

            h_img, w_img = gray.shape
            boxes = []

            for i in range(len(data["text"])):
                text = data["text"][i].strip()
                try:
                    conf = int(data["conf"][i])
                except :
                    continue
        # 🔥 filter real text only
                if conf > 60 and len(text) > 2:
                    x = data["left"][i]
                    y = data["top"][i]
                    w = data["width"][i]
                    h = data["height"][i]

            # bottom region bias
                    if y < h_img * 0.3:
                        continue

                    boxes.append((x, y, w, h))

            if boxes:
                x_min = min(x for x, y, w, h in boxes)
                y_min = min(y for x, y, w, h in boxes)
                x_max = max(x + w for x, y, w, h in boxes)
                y_max = max(y + h for x, y, w, h in boxes)

        # padding
                pad = 20
                x_min = max(0, x_min - pad)
                y_min = max(0, y_min - pad)
                x_max = min(w_img, x_max + pad)
                y_max = min(h_img, y_max + pad)

                label_crop_cv = img_cv[y_min:y_max, x_min:x_max]

                label_crop = Image.fromarray(
                cv2.cvtColor(label_crop_cv, cv2.COLOR_BGR2RGB)
                )
                label_crop.save(tmp_output_path, format="JPEG", quality=quality, optimize=True)

                os.replace(tmp_output_path, output_label_path)
                return output_label_path

        # 🔁 FALLBACK TO YOUR ORIGINAL LOGIC
        with Image.open(original_image_path) as img:
            max_source_pixels = int(os.getenv("HERBARIUM_MAX_SOURCE_PIXELS", "80000000"))
            w, h = img.size
            src_pixels = int(w) * int(h)

            if src_pixels > max_source_pixels:
                raise ValueError(f"Image too large: {w}x{h}")

            img = img.convert("RGB")

            mode = os.getenv("HERBARIUM_LABEL_CROP_MODE", "bbox_norm_1000").strip().lower()

            if mode == "bottom_ratio":
                label_height_ratio = float(os.getenv("HERBARIUM_LABEL_CROP_RATIO", "0.28"))
                label_h = max(1, int(h * label_height_ratio))
                top = max(0, h - label_h)
                label_crop = img.crop((0, top, w, h))
            else:
                ymin, xmin, ymax, xmax = _resolve_label_bbox_norm_1000(original_image_path)
                box = _denormalize_box_1000(ymin, xmin, ymax, xmax, w, h)
                label_crop = img.crop(box)

            label_crop.save(tmp_output_path, format="JPEG", quality=quality, optimize=True)

            os.replace(tmp_output_path, output_label_path)

    except Exception:
        try:
            if os.path.exists(tmp_output_path):
                os.remove(tmp_output_path)
        finally:
            raise

    return output_label_path

    

def resolve_label_output_path(
    label_dir: str,
    filename: str,
) -> str:
    base = os.path.basename(filename)
    if not base.lower().endswith((".jpg", ".jpeg")):
        base = f"{base}.jpg"
    return os.path.join(label_dir, base)


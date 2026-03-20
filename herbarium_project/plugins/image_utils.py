import os
import uuid
from typing import Tuple

from PIL import Image


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


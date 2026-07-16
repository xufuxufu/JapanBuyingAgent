from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageFilter, ImageOps, ImageStat


@dataclass(slots=True)
class ProcessingResult:
    status: str
    method: str
    warning: str | None
    width: int
    height: int
    high_confidence_crop: bool = False


def exif_correct(image: Image.Image) -> Image.Image:
    return ImageOps.exif_transpose(image).convert("RGB")


def detect_receipt_bbox(image: Image.Image) -> tuple[int, int, int, int] | None:
    """Find a conservative receipt rectangle from contrast against border color."""
    working = image.copy()
    working.thumbnail((1200, 1800))
    gray = ImageOps.grayscale(working).filter(ImageFilter.GaussianBlur(2))
    width, height = gray.size
    if width < 40 or height < 40:
        return None
    border = max(2, min(width, height) // 40)
    samples = [
        gray.crop((0, 0, width, border)), gray.crop((0, height - border, width, height)),
        gray.crop((0, 0, border, height)), gray.crop((width - border, 0, width, height)),
    ]
    background = sum(ImageStat.Stat(sample).mean[0] for sample in samples) / len(samples)
    difference = gray.point(lambda value: 255 if abs(value - background) >= 18 else 0)
    difference = difference.filter(ImageFilter.MaxFilter(9)).filter(ImageFilter.MinFilter(5))
    bbox = difference.getbbox()
    if not bbox:
        return None
    left, top, right, bottom = bbox
    area_ratio = ((right - left) * (bottom - top)) / (width * height)
    if area_ratio < 0.35 or area_ratio > 0.96:
        return None
    pad_x = max(4, int((right - left) * 0.025))
    pad_y = max(4, int((bottom - top) * 0.015))
    left, top = max(0, left - pad_x), max(0, top - pad_y)
    right, bottom = min(width, right + pad_x), min(height, bottom + pad_y)
    scale_x, scale_y = image.width / width, image.height / height
    result = (int(left * scale_x), int(top * scale_y), int(right * scale_x), int(bottom * scale_y))
    if result == (0, 0, image.width, image.height):
        return None
    return result


def is_high_confidence_bbox(image: Image.Image, bbox: tuple[int, int, int, int]) -> bool:
    """Reject edge-touching and implausibly narrow crops before changing recognition pixels."""
    left, top, right, bottom = bbox
    width, height = right - left, bottom - top
    area_ratio = (width * height) / (image.width * image.height)
    has_margin = left > image.width * 0.01 or right < image.width * 0.99 or top > image.height * 0.01 or bottom < image.height * 0.99
    return 0.35 <= area_ratio <= 0.96 and width >= image.width * 0.35 and height >= image.height * 0.45 and has_margin


def _edge_positions(mask: Image.Image, y: int) -> tuple[int, int] | None:
    row = mask.crop((0, y, mask.width, min(mask.height, y + 1)))
    bbox = row.getbbox()
    return (bbox[0], bbox[2] - 1) if bbox else None


def lightly_correct_perspective(image: Image.Image) -> tuple[Image.Image, bool]:
    """Apply a conservative four-corner correction only when side drift is clear."""
    gray = ImageOps.grayscale(image.copy())
    gray.thumbnail((1000, 1600))
    background = ImageStat.Stat(gray.crop((0, 0, gray.width, max(2, gray.height // 50)))).mean[0]
    mask = gray.point(lambda value: 255 if abs(value - background) >= 16 else 0).filter(ImageFilter.MaxFilter(7))
    y_top, y_bottom = int(mask.height * 0.08), int(mask.height * 0.92)
    top = _edge_positions(mask, y_top)
    bottom = _edge_positions(mask, y_bottom)
    if not top or not bottom:
        return image, False
    scale_x, scale_y = image.width / mask.width, image.height / mask.height
    tl, tr = top[0] * scale_x, top[1] * scale_x
    bl, br = bottom[0] * scale_x, bottom[1] * scale_x
    drift = max(abs(tl - bl), abs(tr - br))
    if drift < image.width * 0.015 or drift > image.width * 0.18:
        return image, False
    quad = (tl, y_top * scale_y, bl, y_bottom * scale_y, br, y_bottom * scale_y, tr, y_top * scale_y)
    target_height = max(1, int((y_bottom - y_top) * scale_y))
    corrected = image.transform((image.width, target_height), Image.Transform.QUAD, quad, Image.Resampling.BICUBIC)
    return corrected, True


def prepare_receipt_image(source: Path, destination: Path, rotation_degrees: int = 0) -> ProcessingResult:
    """Process one image; any auto-processing error falls back to EXIF-corrected content."""
    with Image.open(source) as opened:
        fallback = exif_correct(opened)
    method = ["exif"]
    warning: str | None = None
    status = "processed"
    try:
        processed = fallback.copy()
        bbox = detect_receipt_bbox(processed)
        if bbox and is_high_confidence_bbox(processed, bbox):
            processed = processed.crop(bbox)
            method.append("auto_crop")
            processed, perspective_applied = lightly_correct_perspective(processed)
            if perspective_applied:
                method.append("light_perspective")
        else:
            warning = "未可靠检测到小票边界，已使用 EXIF 修正后的完整图片"
            method.append("safe_fallback")
    except Exception as exc:
        processed = fallback.copy()
        status = "fallback"
        method = ["exif", "processing_fallback"]
        warning = f"自动处理失败，已安全回退：{exc}"
    if rotation_degrees % 360:
        processed = processed.rotate(-(rotation_degrees % 360), expand=True, resample=Image.Resampling.BICUBIC)
        method.append(f"rotate_{rotation_degrees % 360}")
    max_edge = 7000
    if max(processed.width, processed.height) > max_edge:
        ratio = max_edge / max(processed.width, processed.height)
        processed = processed.resize((max(1, int(processed.width * ratio)), max(1, int(processed.height * ratio))), Image.Resampling.LANCZOS)
        method.append("oversize_resize")
    destination.parent.mkdir(parents=True, exist_ok=True)
    processed.save(destination, format="JPEG", quality=95, subsampling=0, optimize=True)
    return ProcessingResult(status=status, method="+".join(method), warning=warning, width=processed.width, height=processed.height, high_confidence_crop="auto_crop" in method)


def recognition_jpeg_bytes(source: Path, rotation_degrees: int = 0) -> bytes:
    from io import BytesIO
    with Image.open(source) as opened:
        image = exif_correct(opened)
    if rotation_degrees % 360:
        image = image.rotate(-(rotation_degrees % 360), expand=True, resample=Image.Resampling.BICUBIC)
    output = BytesIO()
    if max(image.width, image.height) > 7000:
        ratio = 7000 / max(image.width, image.height)
        image = image.resize((max(1, int(image.width * ratio)), max(1, int(image.height * ratio))), Image.Resampling.LANCZOS)
    image.save(output, format="JPEG", quality=95, subsampling=0, optimize=True)
    return output.getvalue()

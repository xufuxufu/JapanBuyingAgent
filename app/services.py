from __future__ import annotations

import hashlib
import io
import json
import logging
import re
import secrets
import zipfile
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass
from pathlib import Path

from fastapi import HTTPException, UploadFile
from PIL import Image, ImageOps, UnidentifiedImageError
from pydantic import ValidationError
from sqlalchemy import delete, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import ORIGINAL_DIR, PREVIEW_DIR, PROJECT_ROOT, ensure_data_directories
from app.image_processing import ProcessingResult, prepare_receipt_image, recognition_jpeg_bytes
from app.models import (
    AiRecognitionRun, DuplicateDetectionLog, Receipt, ReceiptBatch, ReceiptImage, ReceiptItem,
    ZipPackageItem, ZipPackageJob,
)
from app.receipt_pricing import purchase_unit_price
from app.schemas import BatchReceiptInput, PurchaseConfirmationInput, RecognitionBatchInput, RecognitionInput, ReceiptDraftInput, ReceiptItemDraftInput

ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif"}
ALLOWED_FORMATS = {"JPEG", "PNG", "WEBP", "HEIF", "HEIC"}
JPEG_CONTAINER_FORMATS = {"JPEG", "MPO"}
MAX_FILE_SIZE = 25 * 1024 * 1024
logger = logging.getLogger("uvicorn.error")
IMAGE_DEDUP_ALGORITHM = "image-v1"
BUSINESS_DEDUP_ALGORITHM = "receipt-v1"


@dataclass(slots=True)
class UploadFailure:
    filename: str
    reason: str
    code: str = "UPLOAD_FAILED"


@dataclass(slots=True)
class UploadResult:
    batch: ReceiptBatch | None
    failures: list[UploadFailure]
    replayed: bool = False
    duplicates: list[DuplicateUpload] | None = None
    duplicate_only: bool = False

    @property
    def success_count(self) -> int:
        return 0 if self.duplicate_only else (self.batch.image_count if self.batch else 0)

    @property
    def failure_count(self) -> int:
        return len(self.failures)

    @property
    def duplicate_count(self) -> int:
        return len(self.duplicates or [])


@dataclass(slots=True)
class DuplicateUpload:
    filename: str
    matched_image_id: int
    matched_batch_id: int
    matched_page_no: int
    matched_at: datetime
    score: float
    reason: str


@dataclass(slots=True)
class RecognitionZipResult:
    content: bytes
    filename: str
    job: ZipPackageJob
    selected_image_count: int
    excluded_duplicate_count: int


@dataclass(slots=True)
class GptBatchPreview:
    batch_id: int
    batch_no: str
    expected_image_count: int
    matched_image_count: int
    item_count: int
    warning_count: int
    missing_source_files: list[str]


@dataclass(slots=True)
class GptImportPreview:
    parsed: RecognitionBatchInput
    matched_image_count: int
    unmatched_image_count: int
    batch_count: int
    item_count: int
    warning_count: int
    batch_details: list[GptBatchPreview]
    unknown_source_files: list[str]
    cross_job_source_files: list[str]
    duplicate_source_files: list[str]
    missing_source_files: list[str]
    page_mismatches: list[str]

    @property
    def can_import(self) -> bool:
        return not any((
            self.unknown_source_files,
            self.cross_job_source_files,
            self.duplicate_source_files,
            self.missing_source_files,
            self.page_mismatches,
        ))


class RecognitionValidationError(ValueError):
    def __init__(self, summaries: list[str], technical_details: list[str]):
        self.summaries = summaries
        self.technical_details = technical_details
        visible = summaries[:5]
        message = "；".join(visible)
        if len(summaries) > 5:
            message += f"；另有{len(summaries) - 5}条错误"
        super().__init__(message)


_VALIDATION_FIELD_NAMES = {
    "schema_version": "Schema版本",
    "receipts": "小票列表",
    "source_file": "来源文件名",
    "source_page_no": "来源页码",
    "store": "店铺信息",
    "raw_name": "小票实际名称",
    "purchased_at": "购买时间",
    "receipt_number": "小票编号",
    "totals": "金额合计",
    "subtotal": "小计",
    "discount_total": "折扣合计",
    "tax_total": "税额",
    "paid_total": "实付金额",
    "items": "商品列表",
    "line_no": "商品行号",
    "recognized_name": "整理后商品名",
    "jan_candidate": "JAN候选",
    "quantity": "数量",
    "unit_price": "单价",
    "discount_amount": "折扣金额",
    "tax_rate": "税率",
    "line_total": "行金额",
    "confidence": "置信度",
    "warnings": "警告列表",
}


def _recognition_validation_error(exc: ValidationError) -> RecognitionValidationError:
    summaries: list[str] = []
    technical: list[str] = []
    for error in exc.errors():
        loc = list(error["loc"])
        receipt_position = loc.index("receipts") + 1 if "receipts" in loc else len(loc)
        item_position = loc.index("items") + 1 if "items" in loc else len(loc)
        receipt_no = loc[receipt_position] + 1 if receipt_position < len(loc) and isinstance(loc[receipt_position], int) else 1
        item_no = loc[item_position] + 1 if item_position < len(loc) and isinstance(loc[item_position], int) else None
        field = next((part for part in reversed(loc) if isinstance(part, str)), "数据")
        label = _VALIDATION_FIELD_NAMES.get(field, field)
        prefix = f"第{receipt_no}张小票"
        if item_no is not None:
            prefix += f"第{item_no}项"
        raw_message = error.get("msg", "")
        if "item.source_file" in raw_message:
            summaries.append(f"{prefix}：商品来源文件名（item.source_file）与所属小票不一致")
        elif "item.source_page_no" in raw_message:
            summaries.append(f"{prefix}：商品来源页码（item.source_page_no）与所属小票不一致")
        else:
            action = "缺失" if error.get("type") == "missing" else "格式错误"
            summaries.append(f"{prefix}：{label}{action}")
        path = ".".join(map(str, loc)) or "JSON"
        technical.append(f"{path}: {error['msg']} ({error.get('type', 'validation_error')})")
    return RecognitionValidationError(summaries, technical)


@dataclass(slots=True)
class _PreparedUpload:
    original_name: str
    original_path: Path
    preview_path: Path
    content: bytes
    mime_type: str
    width: int
    height: int
    sha256: str
    normalized_image_hash: str
    perceptual_hash: str


class _UploadRejected(Exception):
    def __init__(self, reason: str, stage: str, cause: Exception | None = None, detected_format: str | None = None):
        super().__init__(reason)
        self.reason = reason
        self.stage = stage
        self.cause = cause
        self.detected_format = detected_format


TOKYO = timezone(timedelta(hours=9), "Asia/Tokyo")


def new_batch_no(now: datetime | None = None) -> str:
    instant = now or datetime.now(timezone.utc)
    local = instant.astimezone(TOKYO)
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    suffix = "".join(secrets.choice(alphabet) for _ in range(4))
    return f"RCPT-{local:%Y%m%d-%H%M}-{suffix}"


def make_recognition_filename(batch_no: str, page_no: int) -> str:
    return f"{batch_no}_P{page_no:02d}.jpg"


def _safe_original_name(name: str | None) -> str:
    raw = name or "upload"
    return Path(raw.replace("\\", "/")).name[:255] or "upload"


def _relative(path: Path) -> str:
    return path.resolve().relative_to(PROJECT_ROOT.resolve()).as_posix()


def _header_format(content: bytes) -> str | None:
    if content.startswith(b"\xff\xd8\xff"):
        return "JPEG"
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "PNG"
    if len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "WEBP"
    if len(content) >= 12 and content[4:8] == b"ftyp":
        brand = content[8:12].decode("ascii", "ignore").upper()
        if brand in {"HEIC", "HEIX", "HEVC", "HEVX", "MIF1", "MSF1", "HEIF"}:
            return "HEIF"
    return None


def _log_upload(original_name: str, extension: str, content_type: str, detected_format: str | None,
                file_size: int | None, stage: str, exception_type: str | None = None) -> None:
    logger.info(
        "receipt_upload filename=%r extension=%r content_type=%r detected_format=%r file_size=%r stage=%r exception_type=%r",
        original_name, extension, content_type, detected_format, file_size, stage, exception_type,
    )


def _validate_image(content: bytes, extension: str) -> tuple[str, int, int]:
    detected_format = _header_format(content)
    def supported(actual: str) -> bool:
        return actual in ALLOWED_FORMATS or (actual in JPEG_CONTAINER_FORMATS and detected_format == "JPEG")

    try:
        with Image.open(io.BytesIO(content)) as opened:
            actual_format = (opened.format or detected_format or "").upper()
            if not supported(actual_format):
                raise _UploadRejected("实际图片格式不受支持", "format_check", detected_format=actual_format)
            opened.verify()
    except _UploadRejected:
        raise
    except (UnidentifiedImageError, SyntaxError) as exc:
        if extension in {".heic", ".heif"} or detected_format == "HEIF":
            raise _UploadRejected("HEIC 当前环境无法解码", "pillow_open", exc) from exc
        raise _UploadRejected("文件内容不是有效图片", "pillow_open", exc) from exc
    except (OSError, ValueError) as exc:
        raise _UploadRejected("图片已损坏或无法解码", "image_verify", exc) from exc

    try:
        with Image.open(io.BytesIO(content)) as reopened:
            reopened.load()
            reopened_format = (reopened.format or actual_format).upper()
            if not supported(reopened_format):
                raise _UploadRejected("实际图片格式不受支持", "format_check", detected_format=reopened_format)
            corrected = ImageOps.exif_transpose(reopened).convert("RGB")
            corrected.load()
            width, height = corrected.size
    except _UploadRejected:
        raise
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError) as exc:
        raise _UploadRejected("图片已损坏或无法解码", "full_decode", exc) from exc
    return reopened_format, width, height


def image_hashes(content: bytes) -> tuple[str, str, str]:
    """Return byte SHA, EXIF-normalized pixel SHA and a compact visual signature."""
    sha256 = hashlib.sha256(content).hexdigest()
    with Image.open(io.BytesIO(content)) as opened:
        image = ImageOps.exif_transpose(opened).convert("RGB")
    normalized = image.copy()
    normalized.thumbnail((512, 512), Image.Resampling.LANCZOS)
    canvas = Image.new("RGB", (512, 512), "white")
    canvas.paste(normalized, ((512 - normalized.width) // 2, (512 - normalized.height) // 2))
    normalized_hash = hashlib.sha256(canvas.tobytes()).hexdigest()

    def dhash(source: Image.Image) -> str:
        gray = ImageOps.grayscale(source).resize((9, 8), Image.Resampling.LANCZOS)
        pixels = list(gray.get_flattened_data())
        value = 0
        for y in range(8):
            for x in range(8):
                value = (value << 1) | int(pixels[y * 9 + x] > pixels[y * 9 + x + 1])
        return f"{value:016x}"

    left, top = image.width // 6, image.height // 6
    center = image.crop((left, top, image.width - left, image.height - top)) if image.width > 12 and image.height > 12 else image
    color = image.resize((1, 1), Image.Resampling.BOX).getpixel((0, 0))
    perceptual = f"{dhash(image)}{dhash(center)}-{color[0]:02x}{color[1]:02x}{color[2]:02x}"
    return sha256, normalized_hash, perceptual


def perceptual_distance(left: str | None, right: str | None) -> tuple[int | None, int | None]:
    if not left or not right:
        return None, None
    try:
        left_bits, left_color = left.split("-", 1)
        right_bits, right_color = right.split("-", 1)
        distance = (int(left_bits, 16) ^ int(right_bits, 16)).bit_count()
        colors_left = [int(left_color[index:index + 2], 16) for index in (0, 2, 4)]
        colors_right = [int(right_color[index:index + 2], 16) for index in (0, 2, 4)]
        color_distance = max(abs(a - b) for a, b in zip(colors_left, colors_right, strict=True))
        return distance, color_distance
    except (ValueError, AttributeError):
        return None, None


def images_are_highly_similar(prepared: _PreparedUpload, existing: ReceiptImage) -> tuple[bool, float, str, int | None]:
    existing_sha = existing.sha256 or existing.file_hash
    if prepared.sha256 == existing_sha:
        return True, 1.0, "SHA-256 完全一致", 0
    if existing.normalized_image_hash and prepared.normalized_image_hash == existing.normalized_image_hash:
        return True, 0.995, "EXIF方向修正后的标准化图片哈希一致", 0
    distance, color_distance = perceptual_distance(prepared.perceptual_hash, existing.perceptual_hash)
    if distance is None or color_distance is None or not existing.width or not existing.height:
        return False, 0.0, "", distance
    prepared_ratio = prepared.width / max(1, prepared.height)
    existing_ratio = existing.width / max(1, existing.height)
    ratio_delta = abs(prepared_ratio - existing_ratio) / max(prepared_ratio, existing_ratio, 0.001)
    highly_similar = distance <= 5 and color_distance <= 8 and ratio_delta <= 0.015
    score = max(0.0, 1.0 - distance / 128 - color_distance / 255 - ratio_delta)
    return highly_similar, score, "感知哈希、尺寸比例和主要内容区域高度一致" if highly_similar else "", distance


def prepared_images_are_highly_similar(left: _PreparedUpload, right: _PreparedUpload) -> tuple[bool, float, str, int | None]:
    if left.sha256 == right.sha256:
        return True, 1.0, "SHA-256 完全一致", 0
    if left.normalized_image_hash == right.normalized_image_hash:
        return True, 0.995, "EXIF方向修正后的标准化图片哈希一致", 0
    distance, color_distance = perceptual_distance(left.perceptual_hash, right.perceptual_hash)
    if distance is None or color_distance is None:
        return False, 0.0, "", distance
    left_ratio = left.width / max(1, left.height)
    right_ratio = right.width / max(1, right.height)
    ratio_delta = abs(left_ratio - right_ratio) / max(left_ratio, right_ratio, 0.001)
    matched = distance <= 5 and color_distance <= 8 and ratio_delta <= 0.015
    score = max(0.0, 1.0 - distance / 128 - color_distance / 255 - ratio_delta)
    return matched, score, "感知哈希、尺寸比例和主要内容区域高度一致" if matched else "", distance


def _upload_error_code(stage: str) -> str:
    if stage in {"extension_check", "image_verify", "format_check", "pillow_open", "full_decode"}:
        return "IMAGE_INVALID"
    if stage == "size_check":
        return "FILE_TOO_LARGE"
    if stage == "save_original":
        return "IMAGE_SAVE_FAILED"
    return "UPLOAD_FAILED"


def _stored_failures(batch: ReceiptBatch) -> list[UploadFailure]:
    if not batch.upload_errors_json:
        return []
    try:
        return [UploadFailure(item["filename"], item["reason"], item.get("code", "UPLOAD_FAILED")) for item in json.loads(batch.upload_errors_json)]
    except (TypeError, ValueError, KeyError):
        return []


async def _prepare_upload(upload: UploadFile) -> _PreparedUpload:
    original_name = _safe_original_name(upload.filename)
    extension = Path(original_name).suffix.lower()
    content_type = upload.content_type or "application/octet-stream"
    file_size: int | None = None
    detected_format: str | None = None
    stage = "extension_check"
    try:
        if extension not in ALLOWED_EXTENSIONS:
            raise _UploadRejected("不支持的扩展名", stage)
        stage = "size_check"
        try:
            content = await upload.read(MAX_FILE_SIZE + 1)
        except Exception as exc:
            raise _UploadRejected("读取上传文件失败", "read_upload", exc) from exc
        file_size = len(content)
        if not content:
            raise _UploadRejected("文件内容不是有效图片", stage)
        if file_size > MAX_FILE_SIZE:
            raise _UploadRejected("文件过大（单张最大 25MB）", stage)
        detected_format = _header_format(content)
        stage = "image_verify"
        actual_format, width, height = _validate_image(content, extension)
        detected_format = actual_format
        sha256, normalized_hash, visual_hash = image_hashes(content)

        token = secrets.token_hex(20)
        original_path = ORIGINAL_DIR / f"{token}{extension}"
        preview_path = PREVIEW_DIR / f"{token}.jpg"
        _log_upload(original_name, extension, content_type, detected_format, file_size, "validated")
        return _PreparedUpload(
            original_name, original_path, preview_path, content, content_type, width, height,
            sha256, normalized_hash, visual_hash,
        )
    except _UploadRejected as exc:
        error_type = type(exc.cause or exc).__name__
        _log_upload(original_name, extension, content_type, exc.detected_format or detected_format, file_size, exc.stage, error_type)
        raise


async def upload_receipt_images(session: Session, files: list[UploadFile], source_type: str = "unknown", request_id: str | None = None) -> UploadResult:
    if not files:
        raise HTTPException(400, "请至少选择一张图片")
    if source_type not in {"mobile", "pc", "unknown"}:
        source_type = "unknown"
    if request_id:
        request_id = request_id.strip()
        if not re.fullmatch(r"[A-Za-z0-9._:-]{8,100}", request_id):
            raise HTTPException(422, "request_id 格式无效")
        existing = session.scalar(select(ReceiptBatch).where(ReceiptBatch.request_id == request_id))
        if existing:
            logger.info("receipt_upload request_id=%r stage='idempotent_replay' batch_id=%s", request_id, existing.id)
            return UploadResult(existing, _stored_failures(existing), replayed=True)
    ensure_data_directories()
    prepared: list[_PreparedUpload] = []
    failures: list[UploadFailure] = []
    for upload in files:
        original_name = _safe_original_name(upload.filename)
        try:
            prepared.append(await _prepare_upload(upload))
        except _UploadRejected as exc:
            failures.append(UploadFailure(original_name, exc.reason, _upload_error_code(exc.stage)))
        except Exception as exc:
            extension = Path(original_name).suffix.lower()
            _log_upload(original_name, extension, upload.content_type or "application/octet-stream", None, None, "unexpected", type(exc).__name__)
            failures.append(UploadFailure(original_name, "上传处理失败", "UPLOAD_FAILED"))

    if not prepared:
        return UploadResult(None, failures)

    existing_images = list(session.scalars(select(ReceiptImage).order_by(ReceiptImage.created_at.asc())))
    accepted: list[tuple[_PreparedUpload, ReceiptImage | None, float]] = []
    duplicates: list[DuplicateUpload] = []
    local_duplicates: list[tuple[_PreparedUpload, Path, float, str, int | None]] = []
    for item in prepared:
        duplicate_match: tuple[ReceiptImage, float, str, int | None] | None = None
        possible_match: tuple[ReceiptImage, float] | None = None
        for existing_image in existing_images:
            matched, score, reason, distance = images_are_highly_similar(item, existing_image)
            if matched:
                duplicate_match = (existing_image, score, reason, distance)
                break
            if score >= 0.75 and (possible_match is None or score > possible_match[1]):
                possible_match = (existing_image, score)
        if duplicate_match:
            matched_image, score, reason, distance = duplicate_match
            duplicates.append(DuplicateUpload(
                item.original_name, matched_image.id, matched_image.batch_id, matched_image.page_no,
                matched_image.created_at, score, reason,
            ))
            session.add(DuplicateDetectionLog(
                entity_type="image", new_entity_id=None, matched_entity_id=matched_image.id,
                algorithm_version=IMAGE_DEDUP_ALGORITHM,
                sha_match=item.sha256 == (matched_image.sha256 or matched_image.file_hash),
                perceptual_distance=distance, business_score=None, decision="auto_duplicate",
                reason=f"{reason}；上传文件未写入永久目录",
            ))
            logger.info(
                "duplicate_detection entity_type='image' matched_image_id=%s decision='auto_duplicate' algorithm=%r",
                matched_image.id, IMAGE_DEDUP_ALGORITHM,
            )
        else:
            local_match = None
            for accepted_item, _, _ in accepted:
                matched, score, reason, distance = prepared_images_are_highly_similar(item, accepted_item)
                if matched:
                    local_match = (accepted_item.original_path, score, reason, distance)
                    break
            if local_match:
                local_duplicates.append((item, *local_match))
            else:
                accepted.append((item, possible_match[0] if possible_match else None, possible_match[1] if possible_match else 0.0))

    saved: list[tuple[_PreparedUpload, ReceiptImage | None, float]] = []
    for item, possible_match, possible_score in accepted:
        try:
            item.original_path.write_bytes(item.content)
            _log_upload(item.original_name, item.original_path.suffix, item.mime_type, None, len(item.content), "original_saved")
            saved.append((item, possible_match, possible_score))
        except OSError as exc:
            _log_upload(item.original_name, item.original_path.suffix, item.mime_type, None, len(item.content), "save_original", type(exc).__name__)
            failures.append(UploadFailure(item.original_name, "保存文件失败", "IMAGE_SAVE_FAILED"))

    if not saved:
        session.commit()
        first = duplicates[0] if duplicates else None
        matched_batch = session.get(ReceiptBatch, first.matched_batch_id) if first else None
        return UploadResult(matched_batch, failures, duplicates=duplicates, duplicate_only=bool(duplicates))

    error_payload = [{"filename": item.filename, "reason": item.reason, "code": item.code} for item in failures]
    batch = ReceiptBatch(
        batch_no=new_batch_no(), request_id=request_id, status="uploaded", current_stage="uploaded",
        upload_errors_json=json.dumps(error_payload, ensure_ascii=False) if error_payload else None,
        image_status="uploaded", gpt_status="not_packaged", product_status="not_matched", qinsi_status="not_exported",
        image_count=0, source_type=source_type, recognition_engine="none",
    )
    created_paths = [item.original_path for item, _, _ in saved]
    session.add(batch)
    try:
        session.flush()
        created_by_path: dict[Path, ReceiptImage] = {}
        for page_no, (item, possible_match, possible_score) in enumerate(saved, start=1):
            image = ReceiptImage(
                original_filename=item.original_name,
                recognition_filename=make_recognition_filename(batch.batch_no, page_no),
                stored_filename=item.original_path.name,
                original_path=_relative(item.original_path),
                processed_path=_relative(item.preview_path),
                page_no=page_no,
                file_hash=item.sha256,
                sha256=item.sha256,
                normalized_image_hash=item.normalized_image_hash,
                perceptual_hash=item.perceptual_hash,
                duplicate_of_image_id=possible_match.id if possible_match else None,
                duplicate_score=possible_score if possible_match else None,
                duplicate_status="possible_duplicate" if possible_match else "none",
                mime_type=item.mime_type,
                file_size=len(item.content),
                width=item.width,
                height=item.height,
                preprocessing_status="uploaded",
                processing_method=None,
                processing_warning=None,
                processed_width=None,
                processed_height=None,
                recognition_source="original",
                rotation_degrees=0,
            )
            batch.images.append(image)
            session.flush()
            created_by_path[item.original_path] = image
            if possible_match:
                session.add(DuplicateDetectionLog(
                    entity_type="image", new_entity_id=image.id, matched_entity_id=possible_match.id,
                    algorithm_version=IMAGE_DEDUP_ALGORITHM, sha_match=False,
                    perceptual_distance=perceptual_distance(item.perceptual_hash, possible_match.perceptual_hash)[0],
                    business_score=None, decision="possible_duplicate",
                    reason="图片仅一般相似，已保留并等待业务字段检测",
                ))
        for item, matched_path, score, reason, distance in local_duplicates:
            matched_image = created_by_path.get(matched_path)
            if not matched_image:
                failures.append(UploadFailure(item.original_name, "同组原图保存失败", "IMAGE_SAVE_FAILED"))
                continue
            duplicates.append(DuplicateUpload(
                item.original_name, matched_image.id, batch.id, matched_image.page_no,
                matched_image.created_at, score, reason,
            ))
            session.add(DuplicateDetectionLog(
                entity_type="image", new_entity_id=None, matched_entity_id=matched_image.id,
                algorithm_version=IMAGE_DEDUP_ALGORITHM, sha_match=item.sha256 == matched_image.sha256,
                perceptual_distance=distance, business_score=None, decision="auto_duplicate",
                reason=f"{reason}；同次上传的重复文件未写入永久目录",
            ))
        batch.image_count = len(batch.images)
        session.commit()
        session.refresh(batch)
        return UploadResult(batch, failures, duplicates=duplicates)
    except IntegrityError:
        session.rollback()
        for path in created_paths:
            path.unlink(missing_ok=True)
        if request_id:
            existing = session.scalar(select(ReceiptBatch).where(ReceiptBatch.request_id == request_id))
            if existing:
                return UploadResult(existing, _stored_failures(existing), replayed=True)
        raise
    except Exception:
        session.rollback()
        for path in created_paths:
            path.unlink(missing_ok=True)
        raise


def process_receipt_batch(batch_id: int, bind) -> None:
    """Lightweight background processor; original files are never removed on failure."""
    with Session(bind=bind, expire_on_commit=False) as session:
        batch = session.scalar(select(ReceiptBatch).where(ReceiptBatch.id == batch_id))
        if not batch or batch.status in {"review", "confirmed"}:
            return
        batch.status = "processing"
        batch.current_stage = "processing"
        batch.image_status = "processing"
        session.commit()
        logger.info("receipt_processing batch_id=%s stage='start' image_count=%s", batch.id, batch.image_count)
        for image in list(batch.images):
            try:
                image.preprocessing_status = "processing"
                session.commit()
                reprocess_receipt_image(session, image)
                logger.info("receipt_processing batch_id=%s image_id=%s stage='complete' status=%r", batch.id, image.id, image.preprocessing_status)
            except Exception as exc:
                session.rollback()
                current = session.get(ReceiptImage, image.id)
                if current:
                    current.preprocessing_status = "failed"
                    current.processing_warning = f"后台处理失败：{type(exc).__name__}"
                    session.commit()
                logger.error("receipt_processing batch_id=%s image_id=%s stage='failed' exception_type=%r", batch.id, image.id, type(exc).__name__)
        session.refresh(batch)
        failed = sum(1 for image in batch.images if image.preprocessing_status == "failed")
        processed = sum(1 for image in batch.images if image.preprocessing_status in {"processed", "fallback"})
        batch.status = "failed" if failed == batch.image_count else "uploaded"
        batch.current_stage = "failed" if failed == batch.image_count else "complete"
        batch.image_status = "failed" if failed == batch.image_count else "ready"
        session.commit()
        logger.info("receipt_processing batch_id=%s stage='finish' processed=%s failed=%s", batch.id, processed, failed)


def parse_recognition_json(raw_text: str) -> RecognitionInput:
    try:
        raw_data = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"JSON 格式错误：第 {exc.lineno} 行第 {exc.colno} 列") from exc
    try:
        return RecognitionInput.model_validate(raw_data)
    except ValidationError as exc:
        raise _recognition_validation_error(exc) from exc


def _parse_gpt_job_json(raw_text: str, job: ZipPackageJob) -> RecognitionBatchInput:
    try:
        raw_data = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"JSON 格式错误：第 {exc.lineno} 行第 {exc.colno} 列") from exc
    try:
        if isinstance(raw_data, dict) and raw_data.get("schema_version") == "1.0":
            included = [item for item in job.items if not item.excluded]
            if len(included) != 1:
                raise ValueError("schema 1.0 单张格式只能导入仅含一张图片的 GPT 任务")
            legacy = RecognitionInput.model_validate(raw_data)
            source = included[0]
            page_no = int(source.recognition_filename.rsplit("_P", 1)[1].split(".", 1)[0])
            items = []
            for item in legacy.items:
                value = item.model_dump(mode="json")
                value.update(source_file=source.recognition_filename, source_page_no=page_no)
                items.append(value)
            return RecognitionBatchInput.model_validate({
                "schema_version": "1.1",
                "receipts": [{
                    "source_file": source.recognition_filename,
                    "source_page_no": page_no,
                    "store": {"raw_name": legacy.store.raw_name},
                    "purchased_at": legacy.store.purchased_at,
                    "receipt_number": legacy.store.receipt_number,
                    "totals": legacy.totals.model_dump(mode="json"),
                    "items": items,
                    "warnings": legacy.warnings,
                }],
            })
        return RecognitionBatchInput.model_validate(raw_data)
    except ValidationError as exc:
        raise _recognition_validation_error(exc) from exc


def preview_gpt_job_import(session: Session, job: ZipPackageJob, raw_text: str) -> GptImportPreview:
    parsed = _parse_gpt_job_json(raw_text, job)
    included = [item for item in job.items if not item.excluded]
    allowed = {item.recognition_filename: item for item in included}
    names = [receipt.source_file for receipt in parsed.receipts]
    duplicate_names = sorted({name for name in names if names.count(name) > 1})
    payload_names = set(names)
    outside_names = sorted(payload_names - set(allowed))
    globally_known = set(session.scalars(select(ReceiptImage.recognition_filename).where(ReceiptImage.recognition_filename.in_(outside_names)))) if outside_names else set()
    cross_job = sorted(globally_known)
    unknown = sorted(set(outside_names) - globally_known)
    missing = sorted(set(allowed) - payload_names)
    page_mismatches: list[str] = []
    matched_names: set[str] = set()
    for receipt in parsed.receipts:
        package_item = allowed.get(receipt.source_file)
        if not package_item or receipt.source_file in duplicate_names:
            continue
        image = session.get(ReceiptImage, package_item.image_id)
        if image is None or image.page_no != receipt.source_page_no:
            expected = image.page_no if image else "不存在"
            page_mismatches.append(f"{receipt.source_file}: source_page_no={receipt.source_page_no}，任务图片页码={expected}")
            continue
        matched_names.add(receipt.source_file)

    details: list[GptBatchPreview] = []
    for batch_id in sorted({item.batch_id for item in included}):
        batch = session.get(ReceiptBatch, batch_id)
        expected_names = {item.recognition_filename for item in included if item.batch_id == batch_id}
        receipts = [receipt for receipt in parsed.receipts if receipt.source_file in expected_names]
        details.append(GptBatchPreview(
            batch_id=batch_id,
            batch_no=batch.batch_no if batch else str(batch_id),
            expected_image_count=len(expected_names),
            matched_image_count=len(expected_names & matched_names),
            item_count=sum(len(receipt.items) for receipt in receipts),
            warning_count=sum(len(receipt.warnings) for receipt in receipts),
            missing_source_files=sorted(expected_names - payload_names),
        ))
    return GptImportPreview(
        parsed=parsed,
        matched_image_count=len(matched_names),
        unmatched_image_count=len(unknown) + len(cross_job) + len(page_mismatches),
        batch_count=len({allowed[name].batch_id for name in matched_names}),
        item_count=sum(len(receipt.items) for receipt in parsed.receipts),
        warning_count=sum(len(receipt.warnings) for receipt in parsed.receipts),
        batch_details=details,
        unknown_source_files=unknown,
        cross_job_source_files=cross_job,
        duplicate_source_files=duplicate_names,
        missing_source_files=missing,
        page_mismatches=page_mismatches,
    )


def _normalize_text(value: str | None) -> str:
    return re.sub(r"[^0-9a-z\u3040-\u30ff\u3400-\u9fff]+", "", (value or "").casefold())


def _item_signature(receipt: Receipt) -> tuple[tuple[str, int, int | None], ...]:
    return tuple(sorted((_normalize_text(item.raw_name), item.quantity, item.line_total) for item in receipt.items if item.review_status != "ignored"))


def make_business_fingerprint(receipt: Receipt) -> str:
    purchased = receipt.purchased_at.isoformat(timespec="minutes") if receipt.purchased_at else None
    facts = {
        "store": _normalize_text(receipt.raw_store_name), "number": _normalize_text(receipt.receipt_number),
        "purchased": purchased, "subtotal": receipt.subtotal, "discount": receipt.discount_total,
        "tax": receipt.tax_total, "paid": receipt.paid_total, "items": _item_signature(receipt),
    }
    return hashlib.sha256(json.dumps(facts, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def _receipt_completeness(receipt: Receipt) -> int:
    values = (receipt.raw_store_name, receipt.purchased_at, receipt.receipt_number, receipt.subtotal, receipt.tax_total, receipt.paid_total)
    return sum(value is not None and value != "" for value in values) + len(receipt.items) * 2


def _receipt_clarity(session: Session, receipt: Receipt) -> int:
    image = session.get(ReceiptImage, receipt.source_image_id) if receipt.source_image_id else None
    return (image.width or 0) * (image.height or 0) if image else 0


def _master_rank(session: Session, receipt: Receipt) -> tuple[int, int, int, float]:
    reviewed = int(receipt.confirmation_status == "confirmed" or receipt.batch.gpt_status == "reviewed")
    created = receipt.created_at if receipt.created_at.tzinfo else receipt.created_at.replace(tzinfo=timezone.utc)
    return reviewed, _receipt_completeness(receipt), _receipt_clarity(session, receipt), -created.timestamp()


def _source_similarity(session: Session, left: Receipt, right: Receipt) -> tuple[bool, int | None]:
    if left.source_image_id and left.source_image_id == right.source_image_id:
        return True, 0
    left_image = session.get(ReceiptImage, left.source_image_id) if left.source_image_id else None
    right_image = session.get(ReceiptImage, right.source_image_id) if right.source_image_id else None
    if not left_image or not right_image:
        return False, None
    if left_image.duplicate_of_image_id == right_image.id or right_image.duplicate_of_image_id == left_image.id:
        return True, 0
    distance, color_distance = perceptual_distance(left_image.perceptual_hash, right_image.perceptual_hash)
    if distance is None or color_distance is None or not left_image.width or not left_image.height or not right_image.width or not right_image.height:
        return False, distance
    left_ratio = left_image.width / max(1, left_image.height)
    right_ratio = right_image.width / max(1, right_image.height)
    ratio_delta = abs(left_ratio - right_ratio) / max(left_ratio, right_ratio, 0.001)
    return distance <= 12 and color_distance <= 15 and ratio_delta <= 0.02, distance


def _compare_receipts(session: Session, new: Receipt, existing: Receipt) -> tuple[str, float, str, int | None]:
    store_new, store_old = _normalize_text(new.raw_store_name), _normalize_text(existing.raw_store_name)
    number_new, number_old = _normalize_text(new.receipt_number), _normalize_text(existing.receipt_number)
    same_store = bool(store_new and store_new == store_old)
    same_number = bool(number_new and number_new == number_old)
    if number_new and number_old and number_new != number_old:
        return "distinct", 0.0, "小票编号不同", None
    if same_store and same_number:
        return "auto_duplicate", 100.0, "店铺和小票编号一致", None
    time_close = False
    if new.purchased_at and existing.purchased_at:
        left = new.purchased_at if new.purchased_at.tzinfo else new.purchased_at.replace(tzinfo=timezone.utc)
        right = existing.purchased_at if existing.purchased_at.tzinfo else existing.purchased_at.replace(tzinfo=timezone.utc)
        seconds = abs((left - right).total_seconds())
        if seconds > 1800:
            return "distinct", 0.0, "购买时间明显不同", None
        time_close = seconds <= 300
    same_amount = new.paid_total is not None and new.paid_total == existing.paid_total
    new_items, old_items = _item_signature(new), _item_signature(existing)
    same_items = bool(new_items and new_items == old_items)
    items_conflict = bool(new_items and old_items and new_items != old_items)
    image_similar, image_distance = _source_similarity(session, new, existing)

    score = min(100.0, (15 if same_store else 0) + (45 if same_number else 0) + (15 if time_close else 0) + (15 if same_amount else 0) + (25 if same_items else 0) + (35 if image_similar else 0))
    if left_same_source(new, existing):
        return "auto_duplicate", 100.0, "同一原始图片已被另一条小票识别", image_distance
    if image_similar and same_store and same_amount:
        if items_conflict:
            return "review_required", score, "图片高度相似，但商品行或数量存在冲突", image_distance
        return "auto_duplicate", score, "图片高度相似，且店铺、实付金额一致", image_distance
    if same_store and time_close and same_amount and same_items:
        return "auto_duplicate", score, "店铺、购买时间、实付金额和商品摘要高度一致", image_distance
    if items_conflict and not image_similar:
        return "distinct", score, "商品行或数量不同，且图片内容不同", image_distance
    if same_store and same_amount and not image_similar and not same_items:
        return "distinct", score, "同店同金额但图片内容或商品摘要不同", image_distance
    if image_similar and (not same_store or not same_amount):
        return "review_required", score, "图片证据与业务字段冲突", image_distance
    if score >= 55:
        return "likely_duplicate", score, "部分业务特征相似，但证据不足以自动合并", image_distance
    return "none", score, "未发现足够重复证据", image_distance


def left_same_source(left: Receipt, right: Receipt) -> bool:
    return bool(left.source_image_id and left.source_image_id == right.source_image_id)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def detect_business_duplicate(session: Session, receipt: Receipt) -> Receipt:
    receipt.business_fingerprint = make_business_fingerprint(receipt)
    candidates = list(session.scalars(select(Receipt).where(Receipt.id != receipt.id).order_by(Receipt.created_at.asc())))
    best: tuple[Receipt, str, float, str, int | None] | None = None
    priority = {"auto_duplicate": 4, "review_required": 3, "likely_duplicate": 2, "distinct": 1, "none": 0}
    for candidate in candidates:
        decision, score, reason, distance = _compare_receipts(session, receipt, candidate)
        if best is None or (priority[decision], score) > (priority[best[1]], best[2]):
            best = candidate, decision, score, reason, distance
    if best is None:
        receipt.duplicate_status = "none"
        receipt.duplicate_score = 0.0
        receipt.duplicate_reason = "没有可比较的历史小票"
        return receipt

    candidate, decision, score, reason, distance = best
    matched_master = (session.get(Receipt, candidate.duplicate_of_receipt_id) if candidate.duplicate_of_receipt_id else candidate) or candidate
    if decision == "auto_duplicate":
        master = max((receipt, matched_master), key=lambda item: _master_rank(session, item))
        duplicate = matched_master if master is receipt else receipt
        duplicate.duplicate_status = "auto_duplicate"
        duplicate.duplicate_of_receipt_id = master.id
        duplicate.duplicate_score = score
        duplicate.duplicate_reason = reason
        master.duplicate_status = "distinct"
        master.duplicate_of_receipt_id = None
        if master is receipt:
            for linked in session.scalars(select(Receipt).where(Receipt.duplicate_of_receipt_id == matched_master.id)):
                linked.duplicate_of_receipt_id = receipt.id
        matched_id = master.id
    else:
        receipt.duplicate_status = decision
        receipt.duplicate_of_receipt_id = None
        receipt.duplicate_score = score
        receipt.duplicate_reason = reason
        matched_id = matched_master.id
    session.add(DuplicateDetectionLog(
        entity_type="receipt", new_entity_id=receipt.id, matched_entity_id=matched_id,
        algorithm_version=BUSINESS_DEDUP_ALGORITHM, sha_match=False,
        perceptual_distance=distance, business_score=score, decision=decision, reason=reason,
    ))
    logger.info(
        "duplicate_detection entity_type='receipt' new_receipt_id=%s matched_receipt_id=%s decision=%r score=%.1f algorithm=%r",
        receipt.id, matched_id, decision, score, BUSINESS_DEDUP_ALGORITHM,
    )
    return receipt


def is_receipt_export_eligible(receipt: Receipt) -> bool:
    return receipt.duplicate_status != "auto_duplicate"


def _normalized_unit_price(quantity: int, unit_price: int | None, line_total: int | None) -> int | None:
    return purchase_unit_price(quantity, unit_price, line_total)


def import_recognition_json(session: Session, batch: ReceiptBatch, raw_text: str) -> Receipt:
    payload = parse_recognition_json(raw_text)

    images_by_name = {image.recognition_filename: image for image in batch.images}

    def resolve_source(source_file: str | None, source_page_no: int | None) -> ReceiptImage | None:
        if source_file is None and source_page_no is None:
            return None
        image = images_by_name.get(source_file or "")
        if image is None or image.page_no != source_page_no:
            raise ValueError(f"source_file/source_page_no 不属于当前批次：{source_file or 'null'} / {source_page_no or 'null'}")
        return image

    receipt_source = resolve_source(payload.source_file, payload.source_page_no)
    item_sources = [resolve_source(item.source_file, item.source_page_no) for item in payload.items]

    if any(receipt.confirmation_status == "confirmed" for receipt in batch.receipts):
        raise ValueError("已确认批次不可覆盖识别结果")
    try:
        for existing in list(batch.receipts):
            session.delete(existing)
        session.flush()
        receipt = Receipt(
            batch=batch,
            source_image_id=receipt_source.id if receipt_source else None,
            raw_store_name=payload.store.raw_name,
            raw_store_code=payload.store.store_code,
            raw_store_phone=payload.store.phone,
            raw_store_postal_code=payload.store.postal_code,
            raw_store_address=payload.store.address,
            raw_store_branch_name=payload.store.branch_name,
            purchased_at=_as_utc(payload.store.purchased_at),
            receipt_number=payload.store.receipt_number,
            subtotal=payload.totals.subtotal,
            discount_total=payload.totals.discount_total,
            tax_total=payload.totals.tax_total,
            paid_total=payload.totals.paid_total,
            currency="JPY",
            recognition_status="imported",
            confirmation_status="pending",
        )
        session.add(receipt)
        session.flush()
        from app.store_service import match_receipt_store
        match_receipt_store(session, receipt)
        for item, source_image in zip(payload.items, item_sources, strict=True):
            session.add(ReceiptItem(
                receipt=receipt,
                line_no=item.line_no,
                raw_name=item.raw_name,
                recognized_name=item.recognized_name or None,
                jan_candidate=item.jan_candidate,
                quantity=item.quantity,
                unit_price=_normalized_unit_price(item.quantity, item.unit_price, item.line_total),
                discount_amount=item.discount_amount,
                tax_rate=item.tax_rate,
                line_total=item.line_total,
                confidence=item.confidence,
                match_status="unmatched",
                review_status="pending",
                source_image_id=source_image.id if source_image else None,
            ))
        normalized = payload.model_dump(mode="json")
        session.add(AiRecognitionRun(
            batch=batch,
            provider="manual_chatgpt",
            prompt_version=payload.schema_version,
            raw_response_json=raw_text,
            normalized_json=json.dumps(normalized, ensure_ascii=False),
            status="success",
        ))
        detect_business_duplicate(session, receipt)
        now = datetime.now(timezone.utc)
        batch.status = "review"
        batch.recognition_engine = "manual_chatgpt"
        batch.gpt_status = "json_imported"
        batch.gpt_sent_at = batch.gpt_sent_at or now
        batch.json_imported_at = now
        session.commit()
        session.refresh(receipt)
        # Enrichment is deliberately NOT triggered here -- same reasoning as
        # import_gpt_job_json(): the import is already fully committed, so
        # the caller (the route) schedules enrichment as a background task
        # once it has the created receipt, instead of blocking this request.
        return receipt
    except Exception:
        session.rollback()
        raise


def import_gpt_job_json(session: Session, job: ZipPackageJob, raw_text: str) -> list[Receipt]:
    preview = preview_gpt_job_import(session, job, raw_text)
    if not preview.can_import:
        problems = []
        if preview.unknown_source_files:
            problems.append("陌生 source_file：" + "、".join(preview.unknown_source_files))
        if preview.cross_job_source_files:
            problems.append("不属于当前 GPT 任务：" + "、".join(preview.cross_job_source_files))
        if preview.duplicate_source_files:
            problems.append("重复 source_file：" + "、".join(preview.duplicate_source_files))
        if preview.missing_source_files:
            problems.append("缺少图片结果：" + "、".join(preview.missing_source_files))
        if preview.page_mismatches:
            problems.append("source_page_no 不一致：" + "；".join(preview.page_mismatches))
        raise ValueError("；".join(problems))

    included = {item.recognition_filename: item for item in job.items if not item.excluded}
    batch_ids = sorted({item.batch_id for item in included.values()})
    batches = {batch_id: session.get(ReceiptBatch, batch_id) for batch_id in batch_ids}
    if any(batch is None for batch in batches.values()):
        raise ValueError("GPT 任务关联的批次不存在")
    if any(receipt.confirmation_status == "confirmed" for batch in batches.values() for receipt in batch.receipts):
        raise ValueError("涉及批次已有确认结果，不可覆盖当前草稿")

    try:
        existing_receipt_ids = list(session.scalars(select(Receipt.id).where(Receipt.batch_id.in_(batch_ids))))
        if existing_receipt_ids:
            session.execute(delete(ReceiptItem).where(ReceiptItem.receipt_id.in_(existing_receipt_ids)), execution_options={"synchronize_session": "fetch"})
            session.execute(delete(Receipt).where(Receipt.id.in_(existing_receipt_ids)), execution_options={"synchronize_session": "fetch"})
        session.flush()

        created: list[Receipt] = []
        for payload in preview.parsed.receipts:
            package_item = included[payload.source_file]
            image = session.get(ReceiptImage, package_item.image_id)
            receipt = Receipt(
                batch_id=package_item.batch_id,
                source_image_id=image.id,
                raw_store_name=payload.store.raw_name or None,
                raw_store_code=payload.store.store_code,
                raw_store_phone=payload.store.phone,
                raw_store_postal_code=payload.store.postal_code,
                raw_store_address=payload.store.address,
                raw_store_branch_name=payload.store.branch_name,
                purchased_at=_as_utc(payload.purchased_at),
                receipt_number=payload.receipt_number,
                subtotal=payload.totals.subtotal,
                discount_total=payload.totals.discount_total,
                tax_total=payload.totals.tax_total,
                paid_total=payload.totals.paid_total,
                currency="JPY",
                recognition_status="imported",
                confirmation_status="pending",
            )
            session.add(receipt)
            session.flush()
            from app.store_service import match_receipt_store
            match_receipt_store(session, receipt)
            for item in payload.items:
                session.add(ReceiptItem(
                    receipt=receipt,
                    line_no=item.line_no,
                    raw_name=item.raw_name,
                    recognized_name=item.recognized_name or None,
                    jan_candidate=item.jan_candidate,
                    quantity=item.quantity,
                    unit_price=_normalized_unit_price(item.quantity, item.unit_price, item.line_total),
                    discount_amount=item.discount_amount,
                    tax_rate=item.tax_rate,
                    line_total=item.line_total,
                    confidence=item.confidence,
                    match_status="unmatched",
                    review_status="pending",
                    source_image_id=image.id,
                ))
            session.flush()
            detect_business_duplicate(session, receipt)
            created.append(receipt)

        normalized = json.dumps(preview.parsed.model_dump(mode="json"), ensure_ascii=False)
        now = datetime.now(timezone.utc)
        for batch_id, batch in batches.items():
            first_image_id = next((receipt.source_image_id for receipt in created if receipt.batch_id == batch_id), None)
            session.add(AiRecognitionRun(
                batch_id=batch_id,
                image_id=first_image_id,
                zip_job_id=job.id,
                provider="manual_chatgpt",
                prompt_version="1.1",
                raw_response_json=raw_text,
                normalized_json=normalized,
                status="success",
            ))
            batch.status = "review"
            batch.recognition_engine = "manual_chatgpt"
            batch.gpt_status = "json_imported"
            batch.gpt_sent_at = batch.gpt_sent_at or now
            batch.json_imported_at = now
        job.gpt_status = "review_pending"
        job.gpt_sent_at = job.gpt_sent_at or now
        job.json_imported_at = now
        session.commit()
        for receipt in created:
            session.refresh(receipt)
        # Enrichment (Yahoo/Rakuten lookups, image downloads, DeepSeek
        # translation) is deliberately NOT triggered here. The JSON import is
        # already fully committed at this point -- receipts/receipt_items are
        # durably saved -- so the caller (the route) is responsible for
        # scheduling enrichment as a background task once it has the created
        # receipts. Running it synchronously here used to block the request
        # for several minutes on a large receipt (dozens of new JANs each
        # needing real network calls), even though nothing about that work
        # can fail the import itself.
        return created
    except Exception:
        session.rollback()
        raise


def reprocess_receipt_image(session: Session, image: ReceiptImage, force_processed: bool = False) -> ReceiptImage:
    original = PROJECT_ROOT / image.original_path
    destination = PROJECT_ROOT / image.processed_path if image.processed_path else PREVIEW_DIR / f"{secrets.token_hex(20)}.jpg"
    try:
        result = prepare_receipt_image(original, destination, image.rotation_degrees)
    except Exception as exc:
        try:
            fallback = recognition_jpeg_bytes(original, image.rotation_degrees)
            destination.write_bytes(fallback)
            with Image.open(io.BytesIO(fallback)) as opened:
                fallback_width, fallback_height = opened.size
            result = ProcessingResult("fallback", "exif+manual_fallback", f"重新处理失败，已安全回退：{exc}", fallback_width, fallback_height)
        except Exception as fallback_exc:
            result = ProcessingResult("failed", "failed", f"{exc}; 回退失败：{fallback_exc}", image.processed_width or image.width or 0, image.processed_height or image.height or 0)
    image.processed_path = _relative(destination)
    image.preprocessing_status = result.status
    image.processing_method = result.method
    image.processing_warning = result.warning
    image.processed_width = result.width
    image.processed_height = result.height
    image.recognition_source = "processed" if force_processed or result.high_confidence_crop else "original"
    session.commit()
    session.refresh(image)
    return image


def selected_recognition_bytes(image: ReceiptImage) -> bytes:
    if image.recognition_source == "original" or not image.processed_path:
        return recognition_jpeg_bytes(PROJECT_ROOT / image.original_path, image.rotation_degrees)
    return (PROJECT_ROOT / image.processed_path).read_bytes()


def recognition_filename(batch: ReceiptBatch, image: ReceiptImage) -> str:
    return image.recognition_filename


def build_recognition_zip(batch: ReceiptBatch) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for image in sorted(batch.images, key=lambda item: item.page_no):
            archive.writestr(recognition_filename(batch, image), selected_recognition_bytes(image))
    return output.getvalue()


def create_recognition_zip(session: Session, batches: list[ReceiptBatch]) -> RecognitionZipResult:
    if not batches:
        raise ValueError("请至少选择一个批次")
    ordered_batches = sorted({batch.id: batch for batch in batches}.values(), key=lambda item: (item.created_at, item.id))
    if any(batch.image_status != "ready" for batch in ordered_batches):
        raise ValueError("只能选择图片已就绪的批次")

    selected: list[tuple[ReceiptBatch, ReceiptImage, bool, str | None]] = []
    for batch in ordered_batches:
        duplicate_receipts = [receipt for receipt in batch.receipts if receipt.duplicate_status == "auto_duplicate"]
        duplicate_source_ids = {receipt.source_image_id for receipt in duplicate_receipts if receipt.source_image_id}
        exclude_entire_batch = any(receipt.source_image_id is None for receipt in duplicate_receipts)
        for image in sorted(batch.images, key=lambda item: item.page_no):
            if image.preprocessing_status not in {"processed", "fallback"}:
                raise ValueError(f"批次 {batch.batch_no[-4:]} 仍有图片未就绪")
            excluded = image.duplicate_status == "auto_duplicate" or exclude_entire_batch or image.id in duplicate_source_ids
            reason = "图片级完全重复" if image.duplicate_status == "auto_duplicate" else ("业务小票已自动判重" if excluded else None)
            selected.append((batch, image, excluded, reason))

    included = [(batch, image) for batch, image, excluded, _ in selected if not excluded]
    if not included:
        raise ValueError("所选图片全部被自动排除，无法生成识别ZIP")
    names = [image.recognition_filename for _, image in included]
    if len(names) != len(set(names)):
        raise ValueError("识别文件名重复，已阻止生成ZIP")
    selection_payload = [(batch.id, image.id, excluded) for batch, image, excluded, _ in selected]
    selection_key = hashlib.sha256(json.dumps(selection_payload, separators=(",", ":")).encode()).hexdigest()
    job = session.scalar(select(ZipPackageJob).where(ZipPackageJob.selection_key == selection_key))
    now = datetime.now(timezone.utc)
    if not job:
        local = now.astimezone(TOKYO)
        job = ZipPackageJob(
            job_no=f"ZIP-{local:%Y%m%d-%H%M}-{secrets.token_hex(2).upper()}",
            selection_key=selection_key, batch_count=len(ordered_batches), image_count=len(included),
            excluded_duplicate_count=len(selected) - len(included), created_at=now,
        )
        session.add(job)
        session.flush()
        for batch, image, excluded, reason in selected:
            session.add(ZipPackageItem(
                job=job, batch_id=batch.id, image_id=image.id,
                recognition_filename=image.recognition_filename, excluded=excluded, exclusion_reason=reason,
            ))
    job.first_downloaded_at = job.first_downloaded_at or now
    job.last_downloaded_at = now
    job.download_count += 1
    if job.gpt_status == "zip_ready":
        job.gpt_status = "zip_downloaded"
    for batch in ordered_batches:
        batch.zip_first_downloaded_at = batch.zip_first_downloaded_at or now
        batch.zip_last_downloaded_at = now
        batch.zip_download_count += 1
        if batch.gpt_status == "not_packaged":
            batch.gpt_status = "zip_downloaded"
    session.commit()

    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for batch, image in included:
            archive.writestr(image.recognition_filename, selected_recognition_bytes(image))
    local = now.astimezone(TOKYO)
    filename = f"GPT识别_{local:%Y%m%d}_{len(ordered_batches)}批次_{len(included)}张.zip"
    return RecognitionZipResult(output.getvalue(), filename, job, len(selected), len(selected) - len(included))


def mark_batch_sent_to_gpt(session: Session, batch: ReceiptBatch) -> ReceiptBatch:
    if batch.gpt_status in {"json_imported", "reviewed"}:
        return batch
    batch.gpt_status = "sent_to_gpt"
    batch.gpt_sent_at = batch.gpt_sent_at or datetime.now(timezone.utc)
    session.commit()
    session.refresh(batch)
    return batch


def gpt_job_batches(session: Session, job: ZipPackageJob) -> list[ReceiptBatch]:
    batch_ids = sorted({item.batch_id for item in job.items if not item.excluded})
    return [batch for batch_id in batch_ids if (batch := session.get(ReceiptBatch, batch_id)) is not None]


def mark_gpt_job_sent(session: Session, job: ZipPackageJob) -> ZipPackageJob:
    if job.gpt_status in {"review_pending", "reviewed"}:
        return job
    now = datetime.now(timezone.utc)
    job.gpt_status = "sent_to_gpt"
    job.gpt_sent_at = job.gpt_sent_at or now
    for batch in gpt_job_batches(session, job):
        if batch.gpt_status not in {"json_imported", "reviewed"}:
            batch.gpt_status = "sent_to_gpt"
            batch.gpt_sent_at = batch.gpt_sent_at or now
    session.commit()
    session.refresh(job)
    return job


def download_gpt_job_zip(session: Session, job: ZipPackageJob) -> RecognitionZipResult:
    included = []
    for item in job.items:
        if item.excluded:
            continue
        batch = session.get(ReceiptBatch, item.batch_id)
        image = session.get(ReceiptImage, item.image_id)
        if batch is None or image is None or image.recognition_filename != item.recognition_filename:
            raise ValueError("GPT 任务中的图片关联已失效")
        included.append((batch, image))
    if not included:
        raise ValueError("GPT 任务没有可下载图片")
    now = datetime.now(timezone.utc)
    job.first_downloaded_at = job.first_downloaded_at or now
    job.last_downloaded_at = now
    job.download_count += 1
    if job.gpt_status == "zip_ready":
        job.gpt_status = "zip_downloaded"
    for batch in {batch.id: batch for batch, _ in included}.values():
        batch.zip_first_downloaded_at = batch.zip_first_downloaded_at or now
        batch.zip_last_downloaded_at = now
        batch.zip_download_count += 1
        if batch.gpt_status == "not_packaged":
            batch.gpt_status = "zip_downloaded"
    session.commit()
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for _, image in included:
            archive.writestr(image.recognition_filename, selected_recognition_bytes(image))
    local = now.astimezone(TOKYO)
    filename = f"GPT识别_{local:%Y%m%d}_{job.batch_count}批次_{job.image_count}张.zip"
    return RecognitionZipResult(output.getvalue(), filename, job, len(job.items), job.excluded_duplicate_count)


def sync_gpt_jobs_reviewed(session: Session, batch: ReceiptBatch) -> None:
    session.flush()
    job_ids = set(session.scalars(select(ZipPackageItem.job_id).where(ZipPackageItem.batch_id == batch.id)))
    for job_id in job_ids:
        job = session.get(ZipPackageJob, job_id)
        batches = gpt_job_batches(session, job) if job else []
        if job and batches and all(item.receipts and all(receipt.confirmation_status == "confirmed" for receipt in item.receipts) for item in batches):
            job.gpt_status = "reviewed"


def backfill_image_hashes(session: Session, limit: int | None = None) -> int:
    query = select(ReceiptImage).where(or_(ReceiptImage.sha256.is_(None), ReceiptImage.normalized_image_hash.is_(None), ReceiptImage.perceptual_hash.is_(None))).order_by(ReceiptImage.id)
    if limit:
        query = query.limit(limit)
    updated = 0
    for image in session.scalars(query):
        content = (PROJECT_ROOT / image.original_path).read_bytes()
        sha256, normalized_hash, visual_hash = image_hashes(content)
        image.sha256 = sha256
        image.file_hash = sha256
        image.normalized_image_hash = normalized_hash
        image.perceptual_hash = visual_hash
        updated += 1
    session.commit()
    return updated


def amount_warnings(receipt: Receipt) -> list[str]:
    active = [item for item in receipt.items if item.review_status != "ignored"]
    warnings: list[str] = []
    known_lines = [item.line_total for item in active if item.line_total is not None]
    if receipt.paid_total is not None and known_lines:
        line_sum = sum(known_lines)
        if line_sum != receipt.paid_total:
            warnings.append(f"未忽略商品行金额合计 {line_sum} 円，与实付金额 {receipt.paid_total} 円不一致")
    return warnings


def save_receipt_draft(session: Session, receipt: Receipt, data: ReceiptDraftInput) -> Receipt:
    receipt.raw_store_name = data.raw_store_name or None
    receipt.raw_store_code = data.raw_store_code or None
    receipt.raw_store_phone = data.raw_store_phone or None
    receipt.raw_store_postal_code = data.raw_store_postal_code or None
    receipt.raw_store_address = data.raw_store_address or None
    receipt.raw_store_branch_name = data.raw_store_branch_name or None
    receipt.purchased_at = _as_utc(data.purchased_at)
    receipt.receipt_number = data.receipt_number or None
    receipt.subtotal = data.subtotal
    receipt.discount_total = data.discount_total
    receipt.tax_total = data.tax_total
    receipt.paid_total = data.paid_total
    from app.store_service import match_receipt_store
    match_receipt_store(session, receipt)
    session.commit()
    session.refresh(receipt)
    return receipt


def apply_item_draft(item: ReceiptItem, data: ReceiptItemDraftInput) -> None:
    item.raw_name = data.raw_name
    item.recognized_name = data.recognized_name or None
    item.jan_candidate = data.jan_candidate
    item.quantity = data.quantity
    item.unit_price = _normalized_unit_price(data.quantity, data.unit_price, data.line_total)
    item.discount_amount = data.discount_amount
    item.tax_rate = data.tax_rate
    item.line_total = data.line_total
    item.confidence = data.confidence
    item.review_status = data.review_status


def confirm_receipt(
    session: Session, batch: ReceiptBatch, receipt: Receipt,
    purchase_settings: PurchaseConfirmationInput | None = None,
    execution_matches: list | None = None,
) -> list[str]:
    active = [item for item in receipt.items if item.review_status != "ignored"]
    if not active:
        raise ValueError("最终确认前至少需要一条未忽略商品行")
    warnings = amount_warnings(receipt)
    try:
        receipt.confirmation_warning = json.dumps(warnings, ensure_ascii=False) if warnings else None
        receipt.confirmation_status = "confirmed"
        receipt.review_status = "reviewed"
        receipt.confirmed_at = datetime.now(timezone.utc)
        for item in active:
            item.review_status = "confirmed"
        session.flush()
        if all(item is receipt or item.confirmation_status == "confirmed" for item in batch.receipts):
            batch.status = "confirmed"
            batch.gpt_status = "reviewed"
            batch.reviewed_at = receipt.confirmed_at
            sync_gpt_jobs_reviewed(session, batch)
        else:
            batch.status = "review"
            batch.gpt_status = "json_imported"
        from app.product_matching import match_receipt
        from app.purchase_service import ensure_purchase_batch_for_receipt
        from app.store_service import match_receipt_store
        match_receipt(session, receipt, commit=False)
        if receipt.store_match_status != "confirmed":
            match_receipt_store(session, receipt)
        ensure_purchase_batch_for_receipt(session, receipt, purchase_settings)
        # Same transaction as the PurchaseBatch creation above: either both
        # land or neither does (see procurement_service.confirm_execution_receipt_matches).
        # A receipt with no matches submitted (the common "wasn't planned" case)
        # is untouched -- this never blocks an ordinary receipt confirm.
        if execution_matches:
            from app.procurement_service import confirm_execution_receipt_matches
            confirm_execution_receipt_matches(session, receipt, execution_matches)
        session.commit()
        from app.product_enrichment import safe_trigger_receipt_items
        safe_trigger_receipt_items(session, active, "receipt_confirmation")
        return warnings
    except Exception:
        session.rollback()
        raise


def repair_confirmed_review_statuses(session: Session) -> dict[str, int]:
    """Idempotently repairs historical confirmed receipts without changing purchase facts."""
    counts = {"receipts": 0, "items": 0, "batches": 0, "gpt_jobs": 0}
    confirmed = list(session.scalars(select(Receipt).where(Receipt.confirmation_status == "confirmed")))
    batches: dict[int, ReceiptBatch] = {}
    for receipt in confirmed:
        batches[receipt.batch_id] = receipt.batch
        if receipt.review_status != "reviewed":
            receipt.review_status = "reviewed"
            counts["receipts"] += 1
        for item in receipt.items:
            if item.review_status != "ignored" and item.review_status != "confirmed":
                item.review_status = "confirmed"
                counts["items"] += 1
    session.flush()
    for batch in batches.values():
        if batch.receipts and all(receipt.confirmation_status == "confirmed" for receipt in batch.receipts):
            if batch.status != "confirmed" or batch.gpt_status != "reviewed":
                counts["batches"] += 1
            batch.status = "confirmed"
            batch.gpt_status = "reviewed"
            batch.reviewed_at = batch.reviewed_at or max((receipt.confirmed_at for receipt in batch.receipts if receipt.confirmed_at), default=datetime.now(timezone.utc))
            job_ids = set(session.scalars(select(ZipPackageItem.job_id).where(ZipPackageItem.batch_id == batch.id)))
            before = {job_id: session.get(ZipPackageJob, job_id).gpt_status for job_id in job_ids if session.get(ZipPackageJob, job_id)}
            sync_gpt_jobs_reviewed(session, batch)
            counts["gpt_jobs"] += sum(session.get(ZipPackageJob, job_id).gpt_status == "reviewed" and status != "reviewed" for job_id, status in before.items())
    session.commit()
    return counts


def delete_unconfirmed_batch(session: Session, batch: ReceiptBatch) -> None:
    if batch.status == "confirmed" or any(receipt.confirmation_status == "confirmed" for receipt in batch.receipts):
        raise HTTPException(409, "已确认批次禁止删除")
    batch.status = "deleted"
    batch.current_stage = "deleted"
    session.commit()


def get_batch_or_404(session: Session, batch_id: int) -> ReceiptBatch:
    batch = session.scalar(select(ReceiptBatch).where(ReceiptBatch.id == batch_id))
    if not batch or batch.status == "deleted":
        raise HTTPException(404, "批次不存在")
    return batch

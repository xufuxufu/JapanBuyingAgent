from __future__ import annotations

import hashlib
import json
import mimetypes
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, func, or_, select, update
from sqlalchemy.orm import Session, selectinload, sessionmaker

from app.config import PRODUCT_IMAGE_DIR, PROJECT_ROOT, TAG_EVIDENCE_DIR, env_int
from app.local_product import is_valid_jan, resolve_local_product_by_jan
from app.models import (
    DurableBackgroundJob,
    EnrichmentAuditLog,
    FieldPurchaseBatch,
    FieldPurchaseItem,
    FieldPurchaseSyncRequest,
    Product,
    ProductEnrichmentTask,
    Store,
    TagEvidence,
)
from app.product_identity import assert_jan_available, format_product_display_name, normalize_product_name_whitespace
from app.product_image_localization import (
    JOB_TYPE as PRODUCT_IMAGE_JOB_TYPE,
    mark_product_image_failure,
    process_product_image_job,
    product_image_summary,
)


OPEN_ITEM_STATUSES = {
    "LOCAL_DRAFT",
    "UPLOAD_PENDING",
    "ENRICHMENT_PENDING",
    "ENRICHING",
    "NEEDS_REVIEW",
    "READY",
    "FAILED_RETRYABLE",
    "FAILED_MANUAL",
}
ALLOWED_IMAGE_TYPES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/heic": ".heic",
    "image/heif": ".heif",
}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _json_response(request: FieldPurchaseSyncRequest) -> dict[str, Any]:
    payload = json.loads(request.response_json)
    payload["replayed"] = True
    return payload


def _batch_or_error(session: Session, batch_id: int) -> FieldPurchaseBatch:
    batch = session.get(FieldPurchaseBatch, batch_id)
    if batch is None:
        raise LookupError("现场采购批次不存在")
    if batch.status != "ACTIVE":
        raise ValueError("现场采购批次已结束，不能继续扫码")
    return batch


def get_field_batch(session: Session, batch_id: int) -> FieldPurchaseBatch:
    batch = session.scalar(
        select(FieldPurchaseBatch)
        .where(FieldPurchaseBatch.id == batch_id)
        .options(
            selectinload(FieldPurchaseBatch.store),
            selectinload(FieldPurchaseBatch.items).selectinload(FieldPurchaseItem.product),
        )
    )
    if batch is None:
        raise LookupError("现场采购批次不存在")
    return batch


def list_active_field_batches(session: Session) -> list[FieldPurchaseBatch]:
    return list(
        session.scalars(
            select(FieldPurchaseBatch)
            .where(FieldPurchaseBatch.status == "ACTIVE")
            .options(selectinload(FieldPurchaseBatch.store), selectinload(FieldPurchaseBatch.items))
            .order_by(FieldPurchaseBatch.started_at.desc(), FieldPurchaseBatch.id.desc())
        )
    )


def create_field_batch(
    session: Session,
    *,
    store_id: int | None,
    operator_name: str,
    client_request_id: str,
) -> FieldPurchaseBatch:
    request_id = client_request_id.strip()[:100]
    if not request_id:
        raise ValueError("client_request_id 不能为空")
    existing = session.scalar(
        select(FieldPurchaseBatch).where(FieldPurchaseBatch.client_request_id == request_id)
    )
    if existing is not None:
        return existing
    store = session.get(Store, store_id) if store_id is not None else None
    if store_id is not None and (store is None or not store.is_active):
        raise ValueError("请选择有效门店或暂不填写")
    operator = operator_name.strip()[:128]
    if not operator:
        raise ValueError("采购人员不能为空")
    batch = FieldPurchaseBatch(
        batch_no=f"FP-PENDING-{uuid.uuid4().hex[:12]}",
        client_request_id=request_id,
        store_id=store.id if store else None,
        operator_name=operator,
        status="ACTIVE",
    )
    session.add(batch)
    session.flush()
    batch.batch_no = f"FP-{utcnow():%Y%m%d}-{batch.id:06d}"
    session.commit()
    session.refresh(batch)
    return batch


def update_field_batch_store(
    session: Session,
    batch_id: int,
    *,
    store_id: int | None,
) -> FieldPurchaseBatch:
    batch = session.get(FieldPurchaseBatch, batch_id)
    if batch is None:
        raise LookupError("现场采购批次不存在")
    store = session.get(Store, store_id) if store_id is not None else None
    if store_id is not None and (store is None or not store.is_active):
        raise ValueError("请选择有效门店或暂不填写")
    batch.store_id = store.id if store else None
    session.commit()
    session.refresh(batch)
    return batch


def complete_field_batch(session: Session, batch_id: int) -> FieldPurchaseBatch:
    batch = _batch_or_error(session, batch_id)
    batch.status = "COMPLETED"
    batch.completed_at = utcnow()
    session.commit()
    session.refresh(batch)
    return batch


def lookup_local_product(session: Session, code: str | None) -> Product | None:
    return resolve_local_product_by_jan(session, code).product


def product_lookup_payload(session: Session, code: str | None, batch_id: int | None = None) -> dict[str, Any]:
    value = (code or "").strip()
    if not value:
        return {"status": "EMPTY", "jan": None, "message": "条码为空，可重扫、OCR、手输或登记无JAN商品"}
    resolution = resolve_local_product_by_jan(session, value)
    if resolution.status == "INVALID":
        return {"status": "INVALID", "jan": value, "message": "无法识别有效JAN-8/JAN-13，可重扫、OCR、手输或登记无JAN商品"}
    if resolution.is_conflict:
        return {
            "status": "AMBIGUOUS",
            "jan": value,
            "candidate_product_ids": list(resolution.candidate_product_ids),
            "candidates": [product_image_summary(product) for product in resolution.candidate_products],
            "message": "本地存在多个商品匹配此JAN，已停止自动匹配；请选择商品或暂存待审核",
        }
    product = resolution.product
    if product is None:
        return {"status": "NOT_FOUND", "jan": value, "message": "本地未登记；拍摄吊牌后即可继续"}
    quantity = 0
    if batch_id is not None:
        quantity = session.scalar(
            select(FieldPurchaseItem.quantity).where(
                FieldPurchaseItem.batch_id == batch_id,
                FieldPurchaseItem.product_id == product.id,
            )
        ) or 0
    return {
        "status": "UNIQUE",
        "jan": value,
        "product": product_image_summary(product),
        "batch_quantity": quantity,
        "match_source": resolution.match_method,
        "message": "商品已登记",
    }


def _item_payload(item: FieldPurchaseItem, *, replayed: bool = False) -> dict[str, Any]:
    product = item.product
    return {
        "replayed": replayed,
        "item_id": item.id,
        "batch_id": item.batch_id,
        "status": item.status,
        "jan": item.jan,
        "temporary_id": item.temporary_id,
        "quantity": item.quantity,
        "product": None
        if product is None
        else product_image_summary(product),
    }


def record_existing_scan(
    session: Session,
    *,
    batch_id: int,
    jan: str,
    client_request_id: str,
    quantity: int = 1,
    selected_product_id: int | None = None,
) -> dict[str, Any]:
    request_id = client_request_id.strip()[:100]
    replay = session.scalar(
        select(FieldPurchaseSyncRequest).where(
            FieldPurchaseSyncRequest.client_request_id == request_id
        )
    )
    if replay is not None:
        return _json_response(replay)
    if not request_id:
        raise ValueError("client_request_id 不能为空")
    if quantity < 1 or quantity > 999:
        raise ValueError("数量必须为 1～999")
    batch = _batch_or_error(session, batch_id)
    resolution = resolve_local_product_by_jan(session, jan)
    if resolution.status == "INVALID":
        raise ValueError("不是合法 JAN-8/JAN-13")
    if resolution.is_conflict:
        if selected_product_id not in set(resolution.candidate_product_ids):
            raise ValueError("JAN 对应多个商品，必须明确选择候选商品")
        product = session.get(Product, selected_product_id)
    else:
        product = resolution.product
        if selected_product_id is not None and product is not None and selected_product_id != product.id:
            raise ValueError("所选商品不是此 JAN 的唯一匹配")
    if product is None:
        raise LookupError("商品尚未登记，请改用新品吊牌草稿")
    item = session.scalar(
        select(FieldPurchaseItem).where(
            FieldPurchaseItem.batch_id == batch.id,
            FieldPurchaseItem.product_id == product.id,
        )
    )
    now = utcnow()
    if item is None:
        unmatched = session.scalar(
            select(FieldPurchaseItem).where(
                FieldPurchaseItem.batch_id == batch.id,
                FieldPurchaseItem.product_id.is_(None),
                FieldPurchaseItem.jan == jan,
            )
        )
        if unmatched is not None:
            item = unmatched
            item.product_id = product.id
            item.status = "CONFIRMED"
            item.confirmed_at = now
            item.quantity += quantity
        else:
            item = FieldPurchaseItem(
                batch_id=batch.id,
                product_id=product.id,
                jan=jan,
                quantity=quantity,
                status="CONFIRMED",
                captured_by=batch.operator_name,
                confirmed_at=now,
            )
            session.add(item)
    else:
        item.quantity += quantity
        item.last_scanned_at = now
    session.flush()
    session.refresh(item, attribute_names=["product"])
    payload = _item_payload(item)
    session.add(
        FieldPurchaseSyncRequest(
            client_request_id=request_id,
            request_type="EXISTING_SCAN",
            batch_id=batch.id,
            item_id=item.id,
            response_json=json.dumps(payload, ensure_ascii=False),
        )
    )
    session.commit()
    return payload


def record_ambiguous_scan_for_review(
    session: Session,
    *,
    batch_id: int,
    jan: str,
    client_request_id: str,
    quantity: int = 1,
) -> dict[str, Any]:
    request_id = client_request_id.strip()[:100]
    replay = session.scalar(
        select(FieldPurchaseSyncRequest).where(
            FieldPurchaseSyncRequest.client_request_id == request_id
        )
    )
    if replay is not None:
        return _json_response(replay)
    if not request_id:
        raise ValueError("client_request_id 不能为空")
    if quantity < 1 or quantity > 999:
        raise ValueError("数量必须为 1～999")
    batch = _batch_or_error(session, batch_id)
    resolution = resolve_local_product_by_jan(session, jan)
    if not resolution.is_conflict:
        raise ValueError("只有多匹配 JAN 可以暂存待审核")
    item = session.scalar(
        select(FieldPurchaseItem).where(
            FieldPurchaseItem.batch_id == batch.id,
            FieldPurchaseItem.product_id.is_(None),
            FieldPurchaseItem.jan == jan,
        )
    )
    if item is None:
        item = FieldPurchaseItem(
            batch_id=batch.id,
            jan=jan,
            quantity=quantity,
            status="NEEDS_REVIEW",
            captured_by=batch.operator_name,
        )
        session.add(item)
    else:
        item.quantity += quantity
        item.status = "NEEDS_REVIEW"
        item.last_scanned_at = utcnow()
    session.flush()
    payload = _item_payload(item)
    payload["resolution_status"] = "AMBIGUOUS"
    payload["candidates"] = [product_image_summary(product) for product in resolution.candidate_products]
    session.add(
        FieldPurchaseSyncRequest(
            client_request_id=request_id,
            request_type="AMBIGUOUS_REVIEW",
            batch_id=batch.id,
            item_id=item.id,
            response_json=json.dumps(payload, ensure_ascii=False),
        )
    )
    session.commit()
    return payload


def bind_field_item_to_product(
    session: Session,
    item_id: int,
    product_id: int,
    *,
    actor: str,
) -> FieldPurchaseItem:
    item = get_field_item(session, item_id)
    product = session.get(Product, product_id)
    if product is None:
        raise LookupError("候选商品不存在")
    if not item.jan:
        raise ValueError("此待审核项没有 JAN，不能按 JAN 候选绑定")
    resolution = resolve_local_product_by_jan(session, item.jan)
    allowed_ids = set(resolution.candidate_product_ids)
    if product.id not in allowed_ids:
        raise ValueError("所选商品不是此 JAN 的候选")
    existing = session.scalar(
        select(FieldPurchaseItem).where(
            FieldPurchaseItem.batch_id == item.batch_id,
            FieldPurchaseItem.product_id == product.id,
            FieldPurchaseItem.id != item.id,
        )
    )
    target = item
    if existing is not None:
        existing.quantity += item.quantity
        existing.last_scanned_at = utcnow()
        existing.status = "CONFIRMED"
        existing.confirmed_at = existing.confirmed_at or utcnow()
        target = existing
        session.delete(item)
    else:
        item.product_id = product.id
        item.status = "CONFIRMED"
        item.confirmed_at = utcnow()
    session.add(
        EnrichmentAuditLog(
            field_purchase_item_id=target.id,
            enrichment_task_id=target.enrichment_task_id,
            action="BIND_EXISTING",
            actor=(actor or "人工审核")[:128],
            before_json=json.dumps({"jan": item.jan, "product_id": None}, ensure_ascii=False),
            after_json=json.dumps({"product_id": product.id}, ensure_ascii=False),
            source="web",
        )
    )
    session.commit()
    session.refresh(target)
    return target


def assign_field_item_jan(
    session: Session,
    item_id: int,
    jan: str,
    *,
    actor: str,
) -> FieldPurchaseItem:
    item = get_field_item(session, item_id)
    normalized = (jan or "").strip()
    if not is_valid_jan(normalized):
        raise ValueError("JAN 必须是校验位正确的 JAN-8/JAN-13")
    if item.jan == normalized:
        return item
    resolution = resolve_local_product_by_jan(session, normalized)
    if resolution.is_conflict:
        raise ValueError("此 JAN 对应多个商品，请先处理 JAN 冲突")
    product = resolution.product
    target = session.scalar(
        select(FieldPurchaseItem).where(
            FieldPurchaseItem.batch_id == item.batch_id,
            FieldPurchaseItem.id != item.id,
            (
                (FieldPurchaseItem.product_id == product.id)
                if product is not None
                else (
                    FieldPurchaseItem.product_id.is_(None)
                    & (FieldPurchaseItem.jan == normalized)
                )
            ),
        )
    )
    before = {"jan": item.jan, "temporary_id": item.temporary_id, "product_id": item.product_id}
    if target is not None:
        target.quantity += item.quantity
        target.last_scanned_at = utcnow()
        target.name_cn = target.name_cn or item.name_cn
        target.name_ja = target.name_ja or item.name_ja
        target.unit_price = target.unit_price if target.unit_price is not None else item.unit_price
        target.product_id = product.id if product is not None else target.product_id
        target.status = "CONFIRMED" if product is not None else "ENRICHMENT_PENDING"
        target.confirmed_at = utcnow() if product is not None else target.confirmed_at
        existing_hashes = {evidence.sha256 for evidence in target.tag_evidence}
        for evidence in list(item.tag_evidence):
            if evidence.sha256 in existing_hashes:
                session.delete(evidence)
            else:
                evidence.item = target
                existing_hashes.add(evidence.sha256)
        for request in item.sync_requests:
            request.item_id = target.id
        old_job = session.scalar(
            select(DurableBackgroundJob).where(
                DurableBackgroundJob.dedupe_key == f"field-enrich:{item.id}"
            )
        )
        target_job = session.scalar(
            select(DurableBackgroundJob).where(
                DurableBackgroundJob.dedupe_key == f"field-enrich:{target.id}"
            )
        )
        if old_job is not None:
            if target_job is not None or product is not None:
                session.delete(old_job)
            else:
                old_job.dedupe_key = f"field-enrich:{target.id}"
                old_job.payload_json = json.dumps({"field_purchase_item_id": target.id})
        session.delete(item)
        result = target
    else:
        item.jan = normalized
        item.product_id = product.id if product is not None else None
        item.status = "CONFIRMED" if product is not None else "ENRICHMENT_PENDING"
        item.confirmed_at = utcnow() if product is not None else None
        result = item
    session.flush()
    session.add(
        EnrichmentAuditLog(
            field_purchase_item_id=result.id,
            enrichment_task_id=result.enrichment_task_id,
            action="MANUAL_EDIT",
            actor=(actor or "人工审核")[:128],
            before_json=json.dumps(before, ensure_ascii=False),
            after_json=json.dumps(
                {"jan": normalized, "product_id": result.product_id, "merged": target is not None},
                ensure_ascii=False,
            ),
        )
    )
    session.commit()
    session.refresh(result)
    return result


def _evidence_path(batch_id: int, content_type: str, original_filename: str) -> Path:
    extension = ALLOWED_IMAGE_TYPES.get(content_type)
    if extension is None:
        guessed = mimetypes.guess_type(original_filename)[0]
        extension = ALLOWED_IMAGE_TYPES.get(guessed or "")
    if extension is None:
        raise ValueError("吊牌照片仅支持 JPEG、PNG、WEBP、HEIC 或 HEIF")
    directory = TAG_EVIDENCE_DIR / str(batch_id)
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{uuid.uuid4().hex}{extension}"


def _save_tag_evidence(
    batch_id: int,
    *,
    content: bytes,
    content_type: str,
    original_filename: str,
) -> tuple[Path, str]:
    max_bytes = env_int("JBA_TAG_EVIDENCE_MAX_UPLOAD_MB", 20, 1, 50) * 1024 * 1024
    if not content:
        raise ValueError("吊牌照片不能为空")
    if len(content) > max_bytes:
        raise ValueError(f"吊牌照片不能超过 {max_bytes // 1024 // 1024} MB")
    path = _evidence_path(batch_id, content_type, original_filename)
    path.write_bytes(content)
    return path, hashlib.sha256(content).hexdigest()


def create_new_product_draft(
    session: Session,
    *,
    batch_id: int,
    client_request_id: str,
    photo_content: bytes,
    photo_content_type: str,
    photo_filename: str,
    jan: str | None,
    temporary_id: str | None,
    name: str | None = None,
    unit_price: int | None = None,
    quantity: int = 1,
) -> tuple[dict[str, Any], DurableBackgroundJob]:
    request_id = client_request_id.strip()[:100]
    replay = session.scalar(
        select(FieldPurchaseSyncRequest).where(
            FieldPurchaseSyncRequest.client_request_id == request_id
        )
    )
    if replay is not None:
        item = session.get(FieldPurchaseItem, replay.item_id) if replay.item_id else None
        job = session.scalar(
            select(DurableBackgroundJob).where(
                DurableBackgroundJob.dedupe_key == f"field-enrich:{replay.item_id}"
            )
        )
        if item is None or job is None:
            raise RuntimeError("幂等记录不完整")
        return _json_response(replay), job
    if not request_id:
        raise ValueError("client_request_id 不能为空")
    batch = _batch_or_error(session, batch_id)
    normalized_jan = (jan or "").strip() or None
    if normalized_jan and not is_valid_jan(normalized_jan):
        raise ValueError("JAN 校验位不正确；无条码商品请使用临时ID")
    if normalized_jan:
        resolution = resolve_local_product_by_jan(session, normalized_jan)
        if resolution.is_conflict:
            raise ValueError("本地存在多个商品匹配此JAN，请人工处理")
        if resolution.product is not None:
            raise ValueError("商品已登记，请使用已有商品扫码接口")
    temp_id = (temporary_id or "").strip()[:80] or None
    if normalized_jan is None:
        temp_id = temp_id or f"TMP-{uuid.uuid4()}"
        if not temp_id.startswith("TMP-"):
            raise ValueError("无JAN商品必须使用系统临时ID")
    if quantity < 1 or quantity > 999:
        raise ValueError("数量必须为 1～999")
    if unit_price is not None and unit_price < 0:
        raise ValueError("价格不能为负数")
    path, digest = _save_tag_evidence(
        batch.id,
        content=photo_content,
        content_type=photo_content_type,
        original_filename=photo_filename,
    )
    try:
        item = None
        if normalized_jan:
            item = session.scalar(
                select(FieldPurchaseItem).where(
                    FieldPurchaseItem.batch_id == batch.id,
                    FieldPurchaseItem.product_id.is_(None),
                    FieldPurchaseItem.jan == normalized_jan,
                )
            )
        elif temp_id:
            item = session.scalar(
                select(FieldPurchaseItem).where(FieldPurchaseItem.temporary_id == temp_id)
            )
        if item is None:
            item = FieldPurchaseItem(
                batch_id=batch.id,
                jan=normalized_jan,
                temporary_id=temp_id,
                quantity=quantity,
                status="ENRICHMENT_PENDING",
                name_ja=(normalize_product_name_whitespace(name) or "")[:128] or None,
                unit_price=unit_price,
                captured_by=batch.operator_name,
            )
            session.add(item)
            session.flush()
        else:
            item.quantity += quantity
            item.last_scanned_at = utcnow()
            item.status = "ENRICHMENT_PENDING"
            if name and not item.name_ja:
                item.name_ja = (normalize_product_name_whitespace(name) or "")[:128] or None
            if unit_price is not None and item.unit_price is None:
                item.unit_price = unit_price
        evidence = session.scalar(
            select(TagEvidence).where(
                TagEvidence.field_purchase_item_id == item.id,
                TagEvidence.sha256 == digest,
            )
        )
        if evidence is None:
            relative_path = path.resolve().relative_to(PROJECT_ROOT.resolve()).as_posix()
            evidence = TagEvidence(
                field_purchase_item_id=item.id,
                original_filename=(photo_filename or "tag-photo")[:255],
                content_type=photo_content_type[:100],
                file_path=relative_path,
                sha256=digest,
                byte_size=len(photo_content),
                ocr_status="PENDING",
            )
            session.add(evidence)
        else:
            path.unlink(missing_ok=True)
        max_attempts = env_int("JBA_FIELD_JOB_MAX_ATTEMPTS", 3, 1, 10)
        job = session.scalar(
            select(DurableBackgroundJob).where(
                DurableBackgroundJob.dedupe_key == f"field-enrich:{item.id}"
            )
        )
        if job is None:
            job = DurableBackgroundJob(
                dedupe_key=f"field-enrich:{item.id}",
                job_type="FIELD_PRODUCT_ENRICHMENT",
                payload_json=json.dumps({"field_purchase_item_id": item.id}),
                status="PENDING",
                max_attempts=max_attempts,
            )
            session.add(job)
        elif job.status in {"COMPLETED", "FAILED_MANUAL"} and item.status != "CONFIRMED":
            job.status = "PENDING"
            job.attempts = 0
            job.available_at = utcnow()
            job.completed_at = None
            job.last_error = None
        session.flush()
        session.refresh(item)
        payload = _item_payload(item)
        payload["tag_evidence_id"] = evidence.id
        session.add(
            FieldPurchaseSyncRequest(
                client_request_id=request_id,
                request_type="NEW_PRODUCT_DRAFT",
                batch_id=batch.id,
                item_id=item.id,
                response_json=json.dumps(payload, ensure_ascii=False),
            )
        )
        session.commit()
        return payload, job
    except Exception:
        session.rollback()
        path.unlink(missing_ok=True)
        raise


def _complete_job(session: Session, job: DurableBackgroundJob) -> None:
    job.status = "COMPLETED"
    job.completed_at = utcnow()
    job.locked_at = None
    job.last_error = None


def process_durable_job(job_id: int, engine: Engine) -> None:
    SessionMaker = sessionmaker(bind=engine, expire_on_commit=False)
    with SessionMaker() as session:
        now = utcnow()
        claimed = session.execute(
            update(DurableBackgroundJob)
            .where(
                DurableBackgroundJob.id == job_id,
                DurableBackgroundJob.status.in_({"PENDING", "FAILED_RETRYABLE"}),
                DurableBackgroundJob.available_at <= now,
            )
            .values(
                status="RUNNING",
                locked_at=now,
                attempts=DurableBackgroundJob.attempts + 1,
            )
        )
        session.commit()
        if claimed.rowcount != 1:
            return
        job = session.get(DurableBackgroundJob, job_id)
        try:
            payload = json.loads(job.payload_json)
            if job.job_type == PRODUCT_IMAGE_JOB_TYPE:
                process_product_image_job(session, job)
                job = session.get(DurableBackgroundJob, job_id)
                _complete_job(session, job)
                session.commit()
                return
            if job.job_type != "FIELD_PRODUCT_ENRICHMENT":
                raise ValueError(f"不支持的任务类型：{job.job_type}")
            item = session.scalar(
                select(FieldPurchaseItem)
                .where(FieldPurchaseItem.id == int(payload["field_purchase_item_id"]))
                .options(selectinload(FieldPurchaseItem.tag_evidence))
            )
            if item is None:
                raise LookupError("现场采购草稿不存在")
            if item.status == "CONFIRMED":
                _complete_job(session, job)
                session.commit()
                return
            item.status = "ENRICHING"
            for evidence in item.tag_evidence:
                if evidence.ocr_status == "PENDING":
                    evidence.ocr_status = "UNCONFIGURED"
            session.commit()
            if item.jan:
                product = lookup_local_product(session, item.jan)
                if product is not None:
                    item.product_id = product.id
                    item.status = "CONFIRMED"
                    item.confirmed_at = utcnow()
                else:
                    from app.product_enrichment import ensure_enrichment_task, process_enrichment_task

                    task = ensure_enrichment_task(
                        session,
                        item.jan,
                        "field_purchase",
                        source_type="field_purchase_item",
                        source_id=item.id,
                    )
                    if task is not None:
                        item = session.get(FieldPurchaseItem, item.id)
                        item.enrichment_task_id = task.id
                        session.commit()
                        process_enrichment_task(session, task)
                        item = session.get(FieldPurchaseItem, item.id)
                        task = session.get(ProductEnrichmentTask, task.id)
                        if item.product_id:
                            item.status = "CONFIRMED"
                            item.confirmed_at = item.confirmed_at or utcnow()
                        elif task and task.status in {"pending", "running"}:
                            item.status = "ENRICHMENT_PENDING"
                        elif task and task.status == "failed":
                            item.status = "FAILED_RETRYABLE"
                        else:
                            item.status = "NEEDS_REVIEW"
                    else:
                        item.status = "NEEDS_REVIEW"
            else:
                item.status = "NEEDS_REVIEW"
            job = session.get(DurableBackgroundJob, job_id)
            _complete_job(session, job)
            session.commit()
        except Exception as exc:
            session.rollback()
            job = session.get(DurableBackgroundJob, job_id)
            job.last_error = f"{type(exc).__name__}: {str(exc)[:300]}"
            job.locked_at = None
            if job.attempts >= job.max_attempts:
                job.status = "FAILED_MANUAL"
            else:
                job.status = "FAILED_RETRYABLE"
                job.available_at = utcnow() + timedelta(minutes=min(60, 2 ** job.attempts))
            try:
                item_id = int(json.loads(job.payload_json).get("field_purchase_item_id"))
                item = session.get(FieldPurchaseItem, item_id)
                if item is not None and item.status != "CONFIRMED":
                    item.status = job.status
            except (TypeError, ValueError, json.JSONDecodeError):
                pass
            if job.job_type == PRODUCT_IMAGE_JOB_TYPE:
                mark_product_image_failure(session, job, exc)
            session.commit()


def recover_stale_jobs(session: Session, *, now: datetime | None = None) -> int:
    now = now or utcnow()
    stale_minutes = env_int("JBA_FIELD_JOB_STALE_MINUTES", 10, 1, 1440)
    cutoff = now - timedelta(minutes=stale_minutes)
    jobs = list(
        session.scalars(
            select(DurableBackgroundJob).where(
                DurableBackgroundJob.status == "RUNNING",
                or_(
                    DurableBackgroundJob.locked_at.is_(None),
                    DurableBackgroundJob.locked_at < cutoff,
                ),
            )
        )
    )
    for job in jobs:
        job.locked_at = None
        if job.attempts >= job.max_attempts:
            job.status = "FAILED_MANUAL"
        else:
            job.status = "PENDING"
            job.available_at = now
        job.last_error = "应用重启或任务超时，已自动回收"
    session.commit()
    return len(jobs)


def process_pending_jobs(
    engine: Engine,
    limit: int = 20,
    *,
    job_type: str | None = None,
    concurrency: int | None = None,
) -> int:
    SessionMaker = sessionmaker(bind=engine, expire_on_commit=False)
    with SessionMaker() as session:
        recover_stale_jobs(session)
        now = utcnow()
        query = select(DurableBackgroundJob.id).where(
            DurableBackgroundJob.status.in_({"PENDING", "FAILED_RETRYABLE"}),
            DurableBackgroundJob.available_at <= now,
        )
        if job_type:
            query = query.where(DurableBackgroundJob.job_type == job_type)
        ids = list(
            session.scalars(
                query.order_by(DurableBackgroundJob.available_at, DurableBackgroundJob.id)
                .limit(max(1, min(limit, 100)))
            )
        )
    workers = concurrency if concurrency is not None else env_int(
        "JBA_BACKGROUND_JOB_CONCURRENCY", 1, 1, 4,
    )
    if workers <= 1 or len(ids) <= 1:
        for job_id in ids:
            process_durable_job(job_id, engine)
    else:
        with ThreadPoolExecutor(max_workers=min(workers, len(ids))) as executor:
            list(executor.map(lambda job_id: process_durable_job(job_id, engine), ids))
    return len(ids)


def pending_sync_count(session: Session) -> int:
    return session.scalar(
        select(func.count())
        .select_from(FieldPurchaseItem)
        .where(FieldPurchaseItem.status.in_(OPEN_ITEM_STATUSES))
    ) or 0


def list_field_review_items(
    session: Session,
    *,
    missing: str | None = None,
    low_confidence: bool = False,
    failed: bool = False,
) -> list[FieldPurchaseItem]:
    query = (
        select(FieldPurchaseItem)
        .where(FieldPurchaseItem.status != "CONFIRMED")
        .options(
            selectinload(FieldPurchaseItem.batch).selectinload(FieldPurchaseBatch.store),
            selectinload(FieldPurchaseItem.tag_evidence),
            selectinload(FieldPurchaseItem.enrichment_task).selectinload(ProductEnrichmentTask.candidates),
        )
        .order_by(FieldPurchaseItem.created_at.desc(), FieldPurchaseItem.id.desc())
    )
    if missing == "name":
        query = query.where(FieldPurchaseItem.name_cn.is_(None), FieldPurchaseItem.name_ja.is_(None))
    elif missing == "price":
        query = query.where(FieldPurchaseItem.unit_price.is_(None))
    elif missing == "image":
        query = query.where(FieldPurchaseItem.product_image_path.is_(None))
    if low_confidence:
        query = query.join(
            ProductEnrichmentTask,
            ProductEnrichmentTask.id == FieldPurchaseItem.enrichment_task_id,
            isouter=True,
        ).where(or_(ProductEnrichmentTask.confidence.is_(None), ProductEnrichmentTask.confidence < 0.85))
    if failed:
        query = query.where(FieldPurchaseItem.status.in_({"FAILED_RETRYABLE", "FAILED_MANUAL"}))
    return list(session.scalars(query).unique())


def get_field_item(session: Session, item_id: int) -> FieldPurchaseItem:
    item = session.scalar(
        select(FieldPurchaseItem)
        .where(FieldPurchaseItem.id == item_id)
        .options(
            selectinload(FieldPurchaseItem.batch).selectinload(FieldPurchaseBatch.store),
            selectinload(FieldPurchaseItem.product),
            selectinload(FieldPurchaseItem.tag_evidence),
            selectinload(FieldPurchaseItem.enrichment_task).selectinload(ProductEnrichmentTask.candidates),
            selectinload(FieldPurchaseItem.audit_logs),
        )
    )
    if item is None:
        raise LookupError("现场采购草稿不存在")
    return item


def update_field_item(
    session: Session,
    item_id: int,
    *,
    actor: str,
    name_cn: str | None,
    name_ja: str | None,
    unit_price: int | None,
    brand: str | None,
    category: str | None,
    unit_name: str | None,
) -> FieldPurchaseItem:
    item = get_field_item(session, item_id)
    before = {
        "name_cn": item.name_cn,
        "name_ja": item.name_ja,
        "unit_price": item.unit_price,
        "brand": item.brand,
        "category": item.category,
        "unit_name": item.unit_name,
    }
    item.name_cn = (normalize_product_name_whitespace(name_cn) or "")[:128] or None
    item.name_ja = (normalize_product_name_whitespace(name_ja) or "")[:128] or None
    item.unit_price = unit_price
    item.brand = (brand or "").strip()[:128] or None
    item.category = (category or "").strip()[:128] or None
    item.unit_name = (unit_name or "").strip()[:128] or None
    if item.status not in {"CONFIRMED", "FAILED_MANUAL"}:
        item.status = "READY" if item.name_cn and item.name_ja else "NEEDS_REVIEW"
    after = {
        "name_cn": item.name_cn,
        "name_ja": item.name_ja,
        "unit_price": item.unit_price,
        "brand": item.brand,
        "category": item.category,
        "unit_name": item.unit_name,
    }
    session.add(
        EnrichmentAuditLog(
            field_purchase_item_id=item.id,
            enrichment_task_id=item.enrichment_task_id,
            action="MANUAL_EDIT",
            actor=(actor or "人工审核")[:128],
            before_json=json.dumps(before, ensure_ascii=False),
            after_json=json.dumps(after, ensure_ascii=False),
        )
    )
    session.commit()
    session.refresh(item)
    return item


def save_field_product_image(
    session: Session,
    item_id: int,
    *,
    actor: str,
    content: bytes,
    content_type: str,
    original_filename: str,
) -> FieldPurchaseItem:
    item = get_field_item(session, item_id)
    path, digest = _save_tag_evidence(
        item.batch_id,
        content=content,
        content_type=content_type,
        original_filename=original_filename,
    )
    before = item.product_image_path
    item.product_image_path = path.resolve().relative_to(PROJECT_ROOT.resolve()).as_posix()
    session.add(
        EnrichmentAuditLog(
            field_purchase_item_id=item.id,
            enrichment_task_id=item.enrichment_task_id,
            action="MANUAL_EDIT",
            actor=(actor or "人工审核")[:128],
            before_json=json.dumps({"product_image_path": before}, ensure_ascii=False),
            after_json=json.dumps(
                {"product_image_path": item.product_image_path, "sha256": digest},
                ensure_ascii=False,
            ),
        )
    )
    session.commit()
    session.refresh(item)
    return item


def bulk_edit_field_items(
    session: Session,
    item_ids: set[int],
    *,
    actor: str,
    brand: str | None,
    category: str | None,
    unit_name: str | None,
) -> int:
    items = list(
        session.scalars(
            select(FieldPurchaseItem).where(
                FieldPurchaseItem.id.in_(item_ids),
                FieldPurchaseItem.status != "CONFIRMED",
            )
        )
    )
    changes = {
        key: value.strip()[:128]
        for key, value in {"brand": brand or "", "category": category or "", "unit_name": unit_name or ""}.items()
        if value.strip()
    }
    for item in items:
        before = {key: getattr(item, key) for key in changes}
        for key, value in changes.items():
            setattr(item, key, value)
        session.add(
            EnrichmentAuditLog(
                field_purchase_item_id=item.id,
                enrichment_task_id=item.enrichment_task_id,
                action="BULK_EDIT",
                actor=(actor or "批量审核")[:128],
                before_json=json.dumps(before, ensure_ascii=False),
                after_json=json.dumps(changes, ensure_ascii=False),
            )
        )
    session.commit()
    return len(items)


def confirm_field_item(session: Session, item_id: int, *, actor: str) -> Product:
    item = get_field_item(session, item_id)
    if item.product is not None:
        product = item.product
    else:
        name_cn = normalize_product_name_whitespace(item.name_cn) or ""
        name_ja = normalize_product_name_whitespace(item.name_ja) or ""
        if not name_cn or not name_ja:
            raise ValueError("确认正式商品前必须填写中文名和日文名")
        resolution = resolve_local_product_by_jan(session, item.jan) if item.jan else None
        if resolution is not None and resolution.is_conflict:
            raise ValueError("此 JAN 对应多个商品，请先在候选中人工选择，不能自动建新品")
        existing = resolution.product if resolution is not None else None
        if existing is not None:
            product = existing
        else:
            promoted_image = None
            if item.product_image_path:
                source_path = (PROJECT_ROOT / item.product_image_path).resolve()
                if source_path.is_relative_to(TAG_EVIDENCE_DIR.resolve()) and source_path.is_file():
                    PRODUCT_IMAGE_DIR.mkdir(parents=True, exist_ok=True)
                    digest = hashlib.sha256(source_path.read_bytes()).hexdigest()
                    destination = PRODUCT_IMAGE_DIR / f"{digest}{source_path.suffix.casefold()}"
                    if not destination.exists():
                        destination.write_bytes(source_path.read_bytes())
                    promoted_image = destination.resolve().relative_to(PROJECT_ROOT.resolve()).as_posix()
            product = Product(
                jan=assert_jan_available(session, item.jan),
                name_cn=name_cn[:128],
                name_ja=name_ja[:128],
                display_name=format_product_display_name(name_cn[:128], name_ja[:128]),
                brand=item.brand,
                category=item.category,
                unit_name=item.unit_name,
                purchase_price=item.unit_price,
                main_image_path=promoted_image,
                main_image_locked=bool(promoted_image),
                product_data_confirmed=True,
                name_locked=True,
                source="field_purchase",
                product_origin="manual",
            )
            session.add(product)
            session.flush()
            if promoted_image:
                product.display_image_url = f"/product-images/{product.id}"
        item.product_id = product.id
    item.status = "CONFIRMED"
    item.confirmed_at = utcnow()
    session.add(
        EnrichmentAuditLog(
            field_purchase_item_id=item.id,
            enrichment_task_id=item.enrichment_task_id,
            action="CONFIRM",
            actor=(actor or "人工审核")[:128],
            after_json=json.dumps({"product_id": product.id}, ensure_ascii=False),
        )
    )
    session.commit()
    session.refresh(product)
    return product


def retry_field_items(session: Session, item_ids: set[int], *, actor: str) -> list[int]:
    jobs = list(
        session.scalars(
            select(DurableBackgroundJob).where(
                DurableBackgroundJob.dedupe_key.in_({f"field-enrich:{item_id}" for item_id in item_ids})
            )
        )
    )
    job_ids: list[int] = []
    for job in jobs:
        item_id = int(job.dedupe_key.rsplit(":", 1)[-1])
        item = session.get(FieldPurchaseItem, item_id)
        if item is None or item.status == "CONFIRMED":
            continue
        job.status = "PENDING"
        job.available_at = utcnow()
        job.locked_at = None
        job.last_error = None
        if job.attempts >= job.max_attempts:
            job.attempts = 0
        item.status = "ENRICHMENT_PENDING"
        session.add(
            EnrichmentAuditLog(
                field_purchase_item_id=item.id,
                enrichment_task_id=item.enrichment_task_id,
                action="RETRY",
                actor=(actor or "人工审核")[:128],
            )
        )
        job_ids.append(job.id)
    session.commit()
    return job_ids

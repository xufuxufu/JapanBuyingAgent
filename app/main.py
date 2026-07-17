from __future__ import annotations

import io
import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import quote

from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sqlalchemy import func, or_, select, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, selectinload

from app.config import PREVIEW_DIR, PRODUCT_IMAGE_DIR, PROJECT_ROOT, ensure_data_directories
from app.db import SessionLocal, get_db
from app.models import ImportJob, ImportRow, Product, ProductAlias, ProductWatchConfig, ProductWatchSnapshot, PurchaseBatch, PurchaseBatchItem, QinsiPurchaseExportJob, Receipt, ReceiptBatch, ReceiptImage, ReceiptItem, RestockList, RestockListItem, Store, StoreBrand, ZipPackageItem, ZipPackageJob
from app.analytics_service import analytics_dashboard, resolve_date_range
from app.product_matching import bind_product, create_product_from_item, match_batch, match_date, match_receipt, normalize_alias
from app.product_identity import create_product_record, format_product_display_name, normalize_product_name, update_product_identifiers
from app.price_service import build_lookup_view, query_prices, recent_price_lookup_histories
from app.product_enrichment import (
    accept_task, bind_task_to_existing, enrichment_summary_for_receipt, get_task,
    list_review_tasks, process_enrichment_task, safe_trigger_receipt_items,
)
from app.location_service import get_default_physical_location, initialize_default_locations, list_locations
from app.monitor_scheduler import scheduler_running, start_monitor_scheduler, stop_monitor_scheduler
from app.monitor_service import (
    NOTIFICATION_TYPE_LABELS, archive_read_notifications, bulk_mark_notifications_read,
    list_notifications, mark_notification_read, monitor_dashboard, run_due_monitor_cycle,
    run_single_monitor_cycle, unread_notification_count,
)
from app.purchase_service import get_purchase_batch, list_purchase_batches
from app.qinsi_export import (
    confirm_qinsi_export, generate_purchase_batch_exports, get_qinsi_export_job,
    list_qinsi_export_jobs, purchase_item_export_states, retry_failed_qinsi_lines,
)
from app.qinsi_import import confirm_import, create_import_preview
from app.qinsi_inventory import (
    INVENTORY_STATUS_LABELS, MATCH_METHOD_LABELS, MATCH_STATUS_LABELS,
    available_qinsi_warehouses, create_inventory_snapshot, get_inventory_snapshot,
    ignore_snapshot_lines, inventory_settings, latest_inventory_for_product,
    latest_snapshot_statistics, list_inventory_snapshots, manual_match_line,
    map_line_warehouse, purchase_assistance, retry_snapshot_matching,
    update_product_low_stock_threshold, watched_inventory_status_distribution,
)
from app.schemas import LocationOutput, PriceLookupInput, ProductCreateInput, ProductOutput, ProductUpdateInput, PurchaseBatchOutput, PurchaseConfirmationInput, QinsiExportConfirmationInput, ReceiptDraftInput, ReceiptItemDraftInput, StoreBrandCreateInput, StoreCreateInput
from app.store_service import confirm_receipt_store, create_store, create_store_brand, product_store_summaries, product_trend_points, purchase_facts, store_monthly_trend_points, store_overview_summaries, store_product_summaries
from app.restock_service import (
    ITEM_STATUS_LABELS, LIST_STATUS_LABELS, SOURCE_LABELS, active_lists_for_product,
    add_product_to_list, copy_restock_list, create_restock_list, get_restock_list,
    link_purchase_item, list_restock_lists, list_statistics, lists_for_product,
    recent_lists_for_store, restock_candidates, trace_rows, update_restock_item,
    update_restock_list_status,
)
from app.watch_service import (
    FREQUENCY_HOURS, REASON_LABELS, accept_recommendations, add_watch, bulk_enable_watches,
    generate_watch_recommendations, get_watch, ignore_recommendation, list_watch_groups,
    set_watch_enabled, update_watch,
)
from app.services import (
    amount_warnings, apply_item_draft, confirm_receipt, create_recognition_zip, download_gpt_job_zip,
    delete_unconfirmed_batch, get_batch_or_404, gpt_job_batches, import_gpt_job_json, import_recognition_json,
    logger, mark_batch_sent_to_gpt, mark_gpt_job_sent, parse_recognition_json, preview_gpt_job_import, process_receipt_batch, recognition_filename, reprocess_receipt_image,
    save_receipt_draft, selected_recognition_bytes, upload_receipt_images,
)

ensure_data_directories()
app = FastAPI(title="Japan Buying Agent", version="0.2.0")
templates = Jinja2Templates(directory=str(PROJECT_ROOT / "app" / "templates"))
TOKYO = timezone(timedelta(hours=9), "Asia/Tokyo")
IMAGE_STATUS_CN = {"uploaded": "已上传", "processing": "处理中", "ready": "图片就绪", "failed": "处理失败"}
GPT_STATUS_CN = {"not_packaged": "待识别", "zip_downloaded": "ZIP已下载", "sent_to_gpt": "已上传GPT", "json_imported": "待审核", "reviewed": "已审核"}
GPT_JOB_STATUS_CN = {"zip_ready": "ZIP已就绪", "zip_downloaded": "ZIP已下载", "sent_to_gpt": "等待JSON", "json_imported": "JSON已导入", "review_pending": "待审核", "reviewed": "已审核"}
RECEIPT_STATUS_CN = {"pending": "待确认", "confirmed": "已确认", "reviewed": "已审核"}
ITEM_STATUS_CN = {"pending": "待确认", "reviewed": "已复核", "confirmed": "已确认", "ignored": "已忽略"}
MATCH_STATUS_CN = {"unmatched": "未匹配", "matched_existing": "已有商品", "new_product": "新商品", "needs_review": "待确认", "conflict": "冲突", "invalid_jan": "JAN无效"}
IMPORT_STATUS_CN = {"previewed": "待确认导入", "completed": "导入完成", "completed_with_issues": "导入完成（有冲突或错误）", "ready": "可导入", "warning": "警告", "skipped": "跳过", "conflict": "冲突", "error": "错误", "imported": "成功"}
BATCH_STATUS_CN = {"uploaded": "已上传", "processing": "处理中", "ready": "图片就绪", "review": "待审核", "confirmed": "已确认", "failed": "失败", "deleted": "已删除"}
PREPROCESS_STATUS_CN = {"previewed": "待处理", "processing": "处理中", "processed": "处理完成", "fallback": "已回退原图", "failed": "处理失败"}
AI_STATUS_CN = {"accepted": "已接受", "imported": "已导入", "failed": "失败", "error": "错误"}
LOCATION_TYPE_CN = {"qinsi_warehouse": "秦丝仓库", "local_physical": "本地物理位置", "transit": "在途位置", "system_status": "系统状态"}
PURCHASE_STATUS_CN = {"confirmed": "已确认", "pending_qinsi_submission": "待提交秦丝", "cancelled": "已取消"}
QINSI_EXPORT_STATUS_CN = {"generated": "已导出待确认", "imported": "全部导入成功", "partially_failed": "部分失败", "failed": "全部失败", "cancelled": "已取消"}
QINSI_EXPORT_TYPE_CN = {"new_product": "新商品导入", "restock": "已有商品补货"}
QINSI_LINE_STATUS_CN = {"generated": "待确认", "imported": "已提交秦丝", "failed": "失败待重试", "cancelled": "已取消"}
QINSI_SNAPSHOT_STATUS_CN = {"completed": "导入完成", "completed_with_issues": "导入完成（需处理）", "failed": "导入失败"}
PURCHASE_EXPORT_STATE_CN = {"pending": "待导出", "awaiting_confirmation": "已导出待确认", "failed_retry": "失败待重试", "submitted": "已提交秦丝"}
PRICE_PROVIDER_STATUS_CN = {
    "success": "查询成功", "empty": "未找到结果", "timeout": "查询超时", "error": "查询失败",
    "unconfigured": "未配置", "manual_only": "仅手动核对",
}
PRICE_COMPARISON_CN = {
    "store_cheaper": "店内更便宜", "online_cheaper": "线上更便宜", "same": "价格相同",
}


def tokyo_datetime(value: datetime, seconds: bool = False) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    local = value.astimezone(TOKYO)
    suffix = f" {local:%H:%M:%S}" if seconds else f" {local:%H:%M}"
    return f"{local.year}年{local.month}月{local.day}日{suffix}"


templates.env.filters["tokyo_datetime"] = tokyo_datetime
app.mount("/static", StaticFiles(directory=str(PROJECT_ROOT / "app" / "static")), name="static")
app.mount("/media/preview", StaticFiles(directory=str(PREVIEW_DIR)), name="preview")
PROMPT_PATH = PROJECT_ROOT / "docs" / "GPT_RECEIPT_PROMPT.md"


def _nav_unread_count() -> int:
    try:
        with SessionLocal() as session:
            return unread_notification_count(session)
    except Exception:
        return 0


templates.env.globals["nav_unread_count"] = _nav_unread_count


@app.on_event("startup")
async def start_price_monitor() -> None:
    start_monitor_scheduler()


@app.on_event("shutdown")
async def stop_price_monitor() -> None:
    await stop_monitor_scheduler()


@app.middleware("http")
async def upload_request_log(request: Request, call_next):
    if request.url.path != "/api/receipt-batches/upload":
        return await call_next(request)
    scheme = (request.headers.get("x-forwarded-proto") or request.url.scheme).split(",", 1)[0].strip()[:16]
    host = (request.headers.get("host") or "")[:200]
    content_length = (request.headers.get("content-length") or "unknown")[:30]
    user_agent = (request.headers.get("user-agent") or "")[:300]
    logger.info("receipt_upload_request stage='start' host=%r scheme=%r content_length=%r user_agent=%r", host, scheme, content_length, user_agent)
    try:
        response = await call_next(request)
    except Exception as exc:
        logger.error("receipt_upload_request stage='request_failed' exception_type=%r", type(exc).__name__)
        raise
    logger.info("receipt_upload_request stage='response' status_code=%s", response.status_code)
    return response


def batch_query():
    return select(ReceiptBatch).where(ReceiptBatch.status != "deleted").options(
        selectinload(ReceiptBatch.images),
        selectinload(ReceiptBatch.receipts).selectinload(Receipt.items),
        selectinload(ReceiptBatch.recognition_runs),
    )


def load_batch(db: Session, batch_id: int) -> ReceiptBatch:
    batch = db.scalars(batch_query().where(ReceiptBatch.id == batch_id)).unique().one_or_none()
    if not batch:
        raise HTTPException(404, "批次不存在")
    return batch


def load_gpt_job(db: Session, job_id: int) -> ZipPackageJob:
    job = db.scalar(select(ZipPackageJob).where(ZipPackageJob.id == job_id).options(
        selectinload(ZipPackageJob.items), selectinload(ZipPackageJob.recognition_runs),
    ))
    if not job:
        raise HTTPException(404, "GPT 识别任务不存在")
    return job


def gpt_job_batch_rows(db: Session, job: ZipPackageJob) -> list[dict]:
    warning_by_batch: dict[int, int] = {}
    latest = max(job.recognition_runs, key=lambda run: run.id, default=None)
    if latest and latest.normalized_json:
        try:
            normalized = json.loads(latest.normalized_json)
            item_batch = {item.recognition_filename: item.batch_id for item in job.items if not item.excluded}
            for receipt in normalized.get("receipts", []):
                batch_id = item_batch.get(receipt.get("source_file"))
                if batch_id:
                    warning_by_batch[batch_id] = warning_by_batch.get(batch_id, 0) + len(receipt.get("warnings") or [])
        except (TypeError, ValueError):
            pass
    rows = []
    for batch in gpt_job_batches(db, job):
        image_count = sum(1 for item in job.items if not item.excluded and item.batch_id == batch.id)
        rows.append({
            "batch": batch,
            "image_count": image_count,
            "item_count": sum(len(receipt.items) for receipt in batch.receipts),
            "warning_count": warning_by_batch.get(batch.id, 0),
            "status_text": GPT_STATUS_CN.get(batch.gpt_status, batch.gpt_status),
        })
    return rows


def current_receipt(batch: ReceiptBatch, receipt_id: int | None = None) -> Receipt:
    if not batch.receipts:
        raise HTTPException(409, "请先导入 GPT JSON")
    if receipt_id is None:
        return batch.receipts[0]
    receipt = next((item for item in batch.receipts if item.id == receipt_id), None)
    if not receipt:
        raise HTTPException(404, "小票不存在")
    return receipt


def _review_url(batch_id: int, receipt: Receipt) -> str:
    return f"/receipts/{batch_id}/review?receipt_id={receipt.id}"


def load_image(db: Session, batch_id: int, image_id: int) -> tuple[ReceiptBatch, ReceiptImage]:
    batch = load_batch(db, batch_id)
    image = next((item for item in batch.images if item.id == image_id), None)
    if not image:
        raise HTTPException(404, "图片不存在")
    return batch, image


def safe_project_path(relative: str) -> Path:
    path = (PROJECT_ROOT / relative).resolve()
    path.relative_to(PROJECT_ROOT.resolve())
    return path


def serialize_batch(batch: ReceiptBatch) -> dict:
    return {
        "id": batch.id, "batch_no": batch.batch_no, "request_id": batch.request_id, "status": batch.status,
        "current_stage": batch.current_stage,
        "image_status": batch.image_status, "gpt_status": batch.gpt_status,
        "product_status": batch.product_status, "qinsi_status": batch.qinsi_status,
        "zip_download_count": batch.zip_download_count,
        "image_count": batch.image_count, "source_type": batch.source_type,
        "recognition_engine": batch.recognition_engine, "created_at": batch.created_at.isoformat(),
        "recognition_run_count": len(batch.recognition_runs),
        "images": [{
            "id": image.id, "page_no": image.page_no, "original_filename": image.original_filename,
            "recognition_filename": image.recognition_filename,
            "stored_filename": image.stored_filename, "file_hash": image.file_hash,
            "mime_type": image.mime_type, "file_size": image.file_size,
            "width": image.width, "height": image.height,
            "processed_width": image.processed_width, "processed_height": image.processed_height,
            "preprocessing_status": image.preprocessing_status,
            "processing_method": image.processing_method, "processing_warning": image.processing_warning,
            "recognition_source": image.recognition_source,
            "rotation_degrees": image.rotation_degrees,
            "duplicate_status": image.duplicate_status, "duplicate_of_image_id": image.duplicate_of_image_id,
            "preview_url": f"/receipts/{batch.id}/images/{image.id}/processed?v={image.rotation_degrees}" if image.processed_path else None,
        } for image in batch.images],
        "receipts": [{
            "id": receipt.id, "raw_store_name": receipt.raw_store_name,
            "confirmation_status": receipt.confirmation_status,
            "recognition_status": receipt.recognition_status, "item_count": len(receipt.items),
            "duplicate_status": receipt.duplicate_status, "duplicate_of_receipt_id": receipt.duplicate_of_receipt_id,
        } for receipt in batch.receipts],
    }


def serialize_upload_result(result) -> dict:
    payload = serialize_batch(result.batch) if result.batch else {
        "id": None, "batch_no": None, "status": "failed", "image_count": 0,
        "source_type": None, "recognition_engine": "none", "images": [], "receipts": [],
    }
    payload.update({
        "success_count": result.success_count,
        "failure_count": result.failure_count,
        "failures": [{"filename": item.filename, "reason": item.reason, "code": item.code} for item in result.failures],
        "idempotent_replay": result.replayed,
        "duplicate_count": result.duplicate_count,
        "duplicate_only": result.duplicate_only,
        "duplicates": [{
            "filename": item.filename, "matched_image_id": item.matched_image_id,
            "matched_batch_id": item.matched_batch_id, "matched_page_no": item.matched_page_no,
            "matched_at": item.matched_at.isoformat(), "score": item.score, "reason": item.reason,
        } for item in (result.duplicates or [])],
    })
    return payload


def serialize_image_action(batch_id: int, image: ReceiptImage) -> dict:
    version = f"{image.rotation_degrees}-{int(image.updated_at.timestamp()) if hasattr(image, 'updated_at') and image.updated_at else image.id}"
    return {
        "id": image.id, "preprocessing_status": image.preprocessing_status,
        "processing_warning": image.processing_warning, "processed_width": image.processed_width,
        "processed_height": image.processed_height, "recognition_source": image.recognition_source,
        "rotation_degrees": image.rotation_degrees,
        "processed_url": f"/receipts/{batch_id}/images/{image.id}/processed?v={version}",
    }


def serialize_batch_status(batch: ReceiptBatch) -> dict:
    processed = sum(1 for image in batch.images if image.preprocessing_status in {"processed", "fallback"})
    failed = sum(1 for image in batch.images if image.preprocessing_status == "failed")
    errors = []
    if batch.upload_errors_json:
        try:
            errors.extend(json.loads(batch.upload_errors_json))
        except (TypeError, ValueError):
            errors.append({"code": "SERVER_ERROR", "reason": "上传错误记录无法读取"})
    errors.extend({"filename": image.original_filename, "code": "PROCESS_FAILED", "reason": image.processing_warning or "图片处理失败"} for image in batch.images if image.preprocessing_status == "failed")
    return {
        "batch_id": batch.id, "batch_status": batch.status, "image_status": batch.image_status,
        "gpt_status": batch.gpt_status, "total_images": batch.image_count,
        "uploaded_images": batch.image_count, "processed_images": processed, "failed_images": failed,
        "current_stage": batch.current_stage, "errors": errors,
    }


def _blank_to_none(value):
    return None if value is None or (isinstance(value, str) and not value.strip()) else value


def _validation_message(exc: ValidationError) -> str:
    return "; ".join(f"{'.'.join(map(str, error['loc']))}: {error['msg']}" for error in exc.errors())


def history_tags(batch: ReceiptBatch, latest_ready_id: int | None) -> list[str]:
    today = datetime.now(timezone.utc).astimezone(TOKYO).date()
    created = batch.created_at if batch.created_at.tzinfo else batch.created_at.replace(tzinfo=timezone.utc)
    tags: list[str] = []
    if batch.image_status == "processing":
        tags.append("处理中")
    elif batch.id == latest_ready_id:
        tags.append("刚上传")
    elif batch.gpt_status == "reviewed":
        tags.append("已审核")
    elif batch.gpt_status == "json_imported":
        tags.append("待审核")
    elif batch.gpt_status == "not_packaged":
        tags.append("待识别")
    if created.astimezone(TOKYO).date() == today:
        tags.append("今日")
    return tags[:2]


async def parse_receipt_form(request: Request) -> ReceiptDraftInput:
    form = await request.form()
    values = {
        "raw_store_name": form.get("raw_store_name") or "",
        "raw_store_code": form.get("raw_store_code") or "",
        "raw_store_phone": form.get("raw_store_phone") or "",
        "raw_store_postal_code": form.get("raw_store_postal_code") or "",
        "raw_store_address": form.get("raw_store_address") or "",
        "raw_store_branch_name": form.get("raw_store_branch_name") or "",
        "receipt_number": form.get("receipt_number") or "",
        **{key: _blank_to_none(form.get(key)) for key in ("purchased_at", "subtotal", "discount_total", "tax_total", "paid_total")},
    }
    values["discount_total"] = values["discount_total"] or 0
    return ReceiptDraftInput.model_validate(values)


async def parse_item_form(request: Request) -> ReceiptItemDraftInput:
    form = await request.form()
    values = {
        "raw_name": form.get("raw_name") or "",
        "recognized_name": form.get("recognized_name") or "",
        "jan_candidate": _blank_to_none(form.get("jan_candidate")),
        "review_status": form.get("review_status") or "pending",
        **{key: _blank_to_none(form.get(key)) for key in ("quantity", "unit_price", "discount_amount", "tax_rate", "line_total", "confidence")},
    }
    values["discount_amount"] = values["discount_amount"] or 0
    values["review_status"] = values["review_status"] or "pending"
    return ReceiptItemDraftInput.model_validate(values)


@app.get("/health")
def health(db: Session = Depends(get_db)):
    try:
        db.execute(text("SELECT 1"))
        return {"status": "ok", "database": "ok", "service": "japan-buying-agent"}
    except SQLAlchemyError as exc:
        return JSONResponse({"status": "error", "database": "unavailable", "detail": str(exc)}, status_code=503)


@app.get("/", response_class=HTMLResponse)
def home(request: Request, db: Session = Depends(get_db)):
    recent = list(db.scalars(batch_query().order_by(ReceiptBatch.created_at.desc()).limit(5)).unique())
    pending = db.scalar(select(func.count()).select_from(Receipt).where(Receipt.confirmation_status != "confirmed")) or 0
    return templates.TemplateResponse(request, "home.html", {"recent": recent, "pending": pending, "batch_status_cn": BATCH_STATUS_CN})


@app.get("/receipts/upload", response_class=HTMLResponse)
def upload_page(request: Request):
    return templates.TemplateResponse(request, "upload.html", {})


@app.get("/price-check", response_class=HTMLResponse)
def price_check_page(request: Request, jan: str = Query(""), db: Session = Depends(get_db)):
    return templates.TemplateResponse(request, "price_check.html", {
        "histories": recent_price_lookup_histories(db, jan=jan.strip() or None),
        "error": None, "jan": jan.strip(), "current_store_price": "",
    })


@app.post("/price-check", response_class=HTMLResponse)
def price_check_submit(
    request: Request, jan: str = Form(...), current_store_price: str = Form(""),
    force_refresh: bool = Form(False), db: Session = Depends(get_db),
):
    try:
        lookup = PriceLookupInput.model_validate({
            "jan": jan, "current_store_price": current_store_price.strip() or None, "force_refresh": force_refresh,
        })
    except ValidationError as exc:
        return templates.TemplateResponse(request, "price_check.html", {
            "histories": recent_price_lookup_histories(db), "error": _validation_message(exc),
            "jan": jan, "current_store_price": current_store_price,
        }, status_code=422)
    view = query_prices(db, lookup)
    return RedirectResponse(f"/price-check/results/{view.history.id}", status_code=303)


@app.get("/price-check/results/{history_id}", response_class=HTMLResponse)
def price_check_result(history_id: int, request: Request, db: Session = Depends(get_db)):
    try:
        view = build_lookup_view(db, history_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    return templates.TemplateResponse(request, "price_check_result.html", {
        "view": view, "watch": get_watch(db, view.product.id) if view.product else None,
        "provider_status_cn": PRICE_PROVIDER_STATUS_CN, "comparison_cn": PRICE_COMPARISON_CN,
    })


@app.post("/receipts/upload")
async def upload_page_post(background_tasks: BackgroundTasks, files: list[UploadFile] = File(...), source_type: str = Form("unknown"), request_id: str | None = Form(None), db: Session = Depends(get_db)):
    result = await upload_receipt_images(db, files, source_type, request_id)
    if not result.batch:
        detail = "；".join(f"{item.filename}：{item.reason}" for item in result.failures)
        raise HTTPException(415, detail or "没有可上传的有效图片")
    if not result.replayed and not result.duplicate_only:
        background_tasks.add_task(process_receipt_batch, result.batch.id, db.get_bind())
    return RedirectResponse(f"/receipts/{result.batch.id}", status_code=303)


@app.get("/receipts", response_class=HTMLResponse)
def receipt_history(request: Request, db: Session = Depends(get_db)):
    batches = list(db.scalars(batch_query().order_by(ReceiptBatch.created_at.desc())).unique())
    latest_ready_id = next((batch.id for batch in batches if batch.image_status == "ready"), None)
    rows = []
    for batch in batches:
        ready_images = [image for image in batch.images if image.preprocessing_status in {"processed", "fallback"}]
        latest_gpt_job = db.scalar(
            select(ZipPackageJob).join(ZipPackageItem).where(ZipPackageItem.batch_id == batch.id).order_by(ZipPackageJob.created_at.desc()).limit(1)
        )
        rows.append({
            "batch": batch, "tags": history_tags(batch, latest_ready_id),
            "image_status_text": IMAGE_STATUS_CN.get(batch.image_status, "状态未知"),
            "gpt_status_text": GPT_STATUS_CN.get(batch.gpt_status, "状态未知"),
            "eligible": bool(batch.images) and len(ready_images) == len(batch.images) and batch.image_status == "ready",
            "ready_count": len(ready_images), "will_reidentify": batch.gpt_status in {"json_imported", "reviewed"},
            "auto_duplicate": any(receipt.duplicate_status == "auto_duplicate" for receipt in batch.receipts),
            "latest_gpt_job": latest_gpt_job,
        })
    return templates.TemplateResponse(request, "history.html", {"batches": batches, "rows": rows})


@app.get("/receipts/gpt-prompt", response_class=PlainTextResponse)
def gpt_prompt():
    return PROMPT_PATH.read_text(encoding="utf-8")


def _gpt_job_context(db: Session, job: ZipPackageJob, **extra) -> dict:
    included = [item for item in job.items if not item.excluded]
    context = {
        "job": job,
        "status_text": GPT_JOB_STATUS_CN.get(job.gpt_status, job.gpt_status),
        "batch_rows": gpt_job_batch_rows(db, job),
        "source_files": [item.recognition_filename for item in included],
        "error": None,
        "error_summaries": [],
        "error_details": [],
        "payload": None,
    }
    context.update(extra)
    return context


@app.get("/gpt-jobs", response_class=HTMLResponse)
def gpt_jobs_page(request: Request, db: Session = Depends(get_db)):
    jobs = list(db.scalars(select(ZipPackageJob).options(selectinload(ZipPackageJob.items)).order_by(ZipPackageJob.created_at.desc())))
    return templates.TemplateResponse(request, "gpt_jobs.html", {
        "jobs": jobs,
        "status_names": GPT_JOB_STATUS_CN,
    })


@app.get("/gpt-jobs/{job_id}", response_class=HTMLResponse)
def gpt_job_detail(job_id: int, request: Request, db: Session = Depends(get_db)):
    job = load_gpt_job(db, job_id)
    return templates.TemplateResponse(request, "gpt_job_detail.html", _gpt_job_context(db, job))


@app.get("/gpt-jobs/{job_id}/zip")
def gpt_job_zip(job_id: int, db: Session = Depends(get_db)):
    job = load_gpt_job(db, job_id)
    try:
        result = download_gpt_job_zip(db, job)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return StreamingResponse(
        io.BytesIO(result.content), media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(result.filename)}"},
    )


@app.post("/gpt-jobs/{job_id}/sent-to-gpt")
def gpt_job_sent(job_id: int, request: Request, db: Session = Depends(get_db)):
    job = mark_gpt_job_sent(db, load_gpt_job(db, job_id))
    if "application/json" in request.headers.get("accept", ""):
        return {"gpt_status": job.gpt_status, "status_text": "✓ 已交给GPT · 等待JSON"}
    return RedirectResponse(f"/gpt-jobs/{job.id}", status_code=303)


@app.post("/gpt-jobs/{job_id}/recognition-preview", response_class=HTMLResponse)
def gpt_job_recognition_preview(job_id: int, request: Request, payload: str = Form(...), db: Session = Depends(get_db)):
    job = load_gpt_job(db, job_id)
    try:
        preview = preview_gpt_job_import(db, job, payload)
    except ValueError as exc:
        return templates.TemplateResponse(
            request, "gpt_job_detail.html", _gpt_job_context(
                db,
                job,
                error=str(exc),
                error_summaries=getattr(exc, "summaries", [str(exc)]),
                error_details=getattr(exc, "technical_details", []),
                payload=payload,
            ), status_code=422,
        )
    return templates.TemplateResponse(request, "gpt_job_preview.html", {
        "job": job,
        "preview": preview,
        "payload": payload,
        "is_repeat": bool(job.recognition_runs),
    })


@app.post("/gpt-jobs/{job_id}/recognition-json")
def gpt_job_recognition_import(job_id: int, payload: str = Form(...), db: Session = Depends(get_db)):
    job = load_gpt_job(db, job_id)
    try:
        import_gpt_job_json(db, job, payload)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return RedirectResponse(f"/gpt-jobs/{job.id}", status_code=303)


@app.get("/receipts/recognition-images.zip")
def download_multi_batch_recognition_zip(batch_ids: list[int] = Query(...), db: Session = Depends(get_db)):
    try:
        result = create_recognition_zip(db, [load_batch(db, batch_id) for batch_id in dict.fromkeys(batch_ids)])
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    disposition = f"attachment; filename*=UTF-8''{quote(result.filename)}"
    return StreamingResponse(
        io.BytesIO(result.content), media_type="application/zip",
        headers={
            "Content-Disposition": disposition,
            "X-Zip-Job": result.job.job_no,
            "X-Selected-Images": str(result.selected_image_count),
            "X-Excluded-Duplicates": str(result.excluded_duplicate_count),
            "X-Zip-Images": str(result.job.image_count),
            "X-GPT-Job-URL": f"/gpt-jobs/{result.job.id}",
        },
    )


@app.get("/receipts/{batch_id}", response_class=HTMLResponse)
def receipt_detail(batch_id: int, request: Request, db: Session = Depends(get_db)):
    batch = load_batch(db, batch_id)
    receipt = batch.receipts[0] if batch.receipts else None
    master = db.get(Receipt, receipt.duplicate_of_receipt_id) if receipt and receipt.duplicate_of_receipt_id else None
    linked = list(db.scalars(select(Receipt).where(Receipt.duplicate_of_receipt_id == receipt.id))) if receipt else []
    gpt_jobs = list(db.scalars(
        select(ZipPackageJob).join(ZipPackageItem).where(ZipPackageItem.batch_id == batch_id).order_by(ZipPackageJob.created_at.desc())
    ).unique())
    return templates.TemplateResponse(request, "detail.html", {
        "batch": batch, "error": None, "payload": None, "rotated": request.query_params.get("rotated"),
        "duplicate_master": master, "duplicate_links": linked, "gpt_jobs": gpt_jobs,
        "batch_status_cn": BATCH_STATUS_CN, "preprocess_status_cn": PREPROCESS_STATUS_CN, "ai_status_cn": AI_STATUS_CN,
    })


@app.post("/receipts/{batch_id}/recognition-preview", response_class=HTMLResponse)
def recognition_preview(batch_id: int, request: Request, payload: str = Form(...), db: Session = Depends(get_db)):
    batch = load_batch(db, batch_id)
    try:
        parsed = parse_recognition_json(payload)
    except ValueError as exc:
        return templates.TemplateResponse(request, "detail.html", {"batch": batch, "error": str(exc), "payload": payload}, status_code=422)
    return templates.TemplateResponse(request, "recognition_preview.html", {
        "batch": batch, "parsed": parsed, "payload": payload,
        "is_repeat": bool(batch.recognition_runs),
    })


@app.post("/receipts/{batch_id}/recognition-json")
def recognition_page_post(batch_id: int, payload: str = Form(...), db: Session = Depends(get_db)):
    batch = load_batch(db, batch_id)
    try:
        import_recognition_json(db, batch, payload)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return RedirectResponse(f"/receipts/{batch_id}/review", status_code=303)


@app.get("/receipts/{batch_id}/images/{image_id}/original")
def original_image(batch_id: int, image_id: int, db: Session = Depends(get_db)):
    _, image = load_image(db, batch_id, image_id)
    return FileResponse(safe_project_path(image.original_path), media_type=image.mime_type)


@app.get("/receipts/{batch_id}/images/{image_id}/processed")
def processed_image(batch_id: int, image_id: int, db: Session = Depends(get_db)):
    _, image = load_image(db, batch_id, image_id)
    if not image.processed_path:
        raise HTTPException(404, "处理图不存在")
    return FileResponse(safe_project_path(image.processed_path), media_type="image/jpeg", headers={"Cache-Control": "no-store, max-age=0"})


@app.get("/receipts/{batch_id}/images/{image_id}/download")
def download_recognition_image(batch_id: int, image_id: int, db: Session = Depends(get_db)):
    batch, image = load_image(db, batch_id, image_id)
    return StreamingResponse(io.BytesIO(selected_recognition_bytes(image)), media_type="image/jpeg", headers={"Content-Disposition": f'attachment; filename="{recognition_filename(batch, image)}"'})


@app.get("/receipts/{batch_id}/recognition-images.zip")
def download_recognition_zip(batch_id: int, db: Session = Depends(get_db)):
    batch = load_batch(db, batch_id)
    try:
        result = create_recognition_zip(db, [batch])
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return StreamingResponse(
        io.BytesIO(result.content), media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(result.filename)}"},
    )


@app.post("/receipts/{batch_id}/gpt-sent")
def mark_gpt_sent(batch_id: int, request: Request, db: Session = Depends(get_db)):
    batch = mark_batch_sent_to_gpt(db, load_batch(db, batch_id))
    if "application/json" in request.headers.get("accept", ""):
        return {"message": "已标记上传GPT", "gpt_status": batch.gpt_status, "gpt_status_text": GPT_STATUS_CN[batch.gpt_status]}
    return RedirectResponse("/receipts", status_code=303)


@app.post("/receipts/{batch_id}/images/{image_id}/rotate")
def rotate_image(batch_id: int, image_id: int, request: Request, db: Session = Depends(get_db)):
    _, image = load_image(db, batch_id, image_id)
    image.rotation_degrees = (image.rotation_degrees + 90) % 360
    reprocess_receipt_image(db, image, force_processed=True)
    if "application/json" in request.headers.get("accept", ""):
        return {"message": "已旋转90°", "image": serialize_image_action(batch_id, image)}
    return RedirectResponse(f"/receipts/{batch_id}?rotated={image_id}", status_code=303)


@app.post("/receipts/{batch_id}/images/{image_id}/reprocess")
def reprocess_image(batch_id: int, image_id: int, request: Request, db: Session = Depends(get_db)):
    _, image = load_image(db, batch_id, image_id)
    reprocess_receipt_image(db, image)
    if "application/json" in request.headers.get("accept", ""):
        return {"message": "处理完成", "image": serialize_image_action(batch_id, image)}
    return RedirectResponse(f"/receipts/{batch_id}", status_code=303)


@app.post("/receipts/{batch_id}/images/{image_id}/source")
def select_image_source(batch_id: int, image_id: int, request: Request, source: str = Form(...), db: Session = Depends(get_db)):
    _, image = load_image(db, batch_id, image_id)
    if source not in {"original", "processed"}:
        raise HTTPException(422, "识别图来源无效")
    image.recognition_source = source
    db.commit()
    db.refresh(image)
    if "application/json" in request.headers.get("accept", ""):
        return {"message": "识别图已更新", "image": serialize_image_action(batch_id, image)}
    return RedirectResponse(f"/receipts/{batch_id}", status_code=303)


@app.get("/receipts/{batch_id}/review", response_class=HTMLResponse)
def review_page(batch_id: int, request: Request, receipt_id: int | None = Query(None), db: Session = Depends(get_db)):
    batch = load_batch(db, batch_id)
    receipt = current_receipt(batch, receipt_id)
    products = list(db.scalars(select(Product).order_by(Product.name_cn, Product.id).limit(500)))
    product_by_id = {product.id: product for product in products}
    for item in receipt.items:
        if item.product_id and item.product_id not in product_by_id:
            product_by_id[item.product_id] = db.get(Product, item.product_id)
    recommendations = {}
    fuzzy_candidates = {}
    for item in receipt.items:
        key = normalize_alias(item.recognized_name or item.raw_name)
        alias = db.scalar(select(ProductAlias).where(ProductAlias.normalized_alias == key, ProductAlias.confirmed.is_(True))) if key else None
        if alias:
            recommendations[item.id] = product_by_id.get(alias.product_id) or db.get(Product, alias.product_id)
        elif key:
            scored = sorted(
                ((SequenceMatcher(None, key, normalize_alias(product.name_cn)).ratio(), product) for product in products if product.name_cn),
                key=lambda candidate: candidate[0], reverse=True,
            )
            fuzzy_candidates[item.id] = [product for score, product in scored[:3] if score >= 0.55]
    active_locations = list_locations(db, active_only=True)
    if not active_locations:
        active_locations = initialize_default_locations(db)
    physical_locations = [location for location in active_locations if location.location_type in {"local_physical", "qinsi_warehouse"}]
    qinsi_warehouses = [location for location in active_locations if location.is_qinsi_warehouse]
    default_physical = get_default_physical_location(db)
    purchase_batch = db.scalar(select(PurchaseBatch).where(PurchaseBatch.receipt_id == receipt.id))
    stores = list(db.scalars(select(Store).where(Store.is_active.is_(True)).order_by(Store.name_cn, Store.name_ja, Store.id)))
    return templates.TemplateResponse(request, "review.html", {
        "batch": batch, "receipt": receipt, "warnings": amount_warnings(receipt), "error": None,
        "products": products, "product_by_id": product_by_id, "recommendations": recommendations,
        "fuzzy_candidates": fuzzy_candidates,
        "physical_locations": physical_locations, "qinsi_warehouses": qinsi_warehouses,
        "default_physical": default_physical, "purchase_batch": purchase_batch, "stores": stores,
        "receipt_status_cn": RECEIPT_STATUS_CN, "item_status_cn": ITEM_STATUS_CN, "match_status_cn": MATCH_STATUS_CN,
        "enrichment_summary": enrichment_summary_for_receipt(db, receipt.id),
    })


@app.post("/receipts/{batch_id}/review/receipt")
async def save_receipt(batch_id: int, request: Request, receipt_id: int | None = Form(None), db: Session = Depends(get_db)):
    batch = load_batch(db, batch_id)
    receipt = current_receipt(batch, receipt_id)
    if receipt.confirmation_status == "confirmed":
        raise HTTPException(409, "已确认小票不可编辑")
    try:
        data = await parse_receipt_form(request)
    except ValidationError as exc:
        raise HTTPException(422, _validation_message(exc)) from exc
    save_receipt_draft(db, receipt, data)
    return RedirectResponse(_review_url(batch_id, receipt), status_code=303)


@app.post("/receipts/{batch_id}/review/store")
def confirm_review_store(
    batch_id: int, receipt_id: int = Form(...), store_id: int = Form(...), db: Session = Depends(get_db),
):
    batch = load_batch(db, batch_id)
    receipt = current_receipt(batch, receipt_id)
    store = db.get(Store, store_id)
    if store is None:
        raise HTTPException(404, "门店不存在")
    try:
        confirm_receipt_store(db, receipt, store)
    except ValueError as exc:
        db.rollback()
        raise HTTPException(409, str(exc)) from exc
    return RedirectResponse(_review_url(batch_id, receipt), status_code=303)


@app.post("/receipts/{batch_id}/review/items/{item_id}")
async def save_item(batch_id: int, item_id: int, request: Request, db: Session = Depends(get_db)):
    batch = load_batch(db, batch_id)
    receipt = next((candidate for candidate in batch.receipts if any(row.id == item_id for row in candidate.items)), None)
    if not receipt:
        raise HTTPException(404, "商品行不存在")
    if receipt.confirmation_status == "confirmed":
        raise HTTPException(409, "已确认小票不可编辑")
    item = next((row for row in receipt.items if row.id == item_id), None)
    if not item:
        raise HTTPException(404, "商品行不存在")
    try:
        data = await parse_item_form(request)
    except ValidationError as exc:
        raise HTTPException(422, _validation_message(exc)) from exc
    apply_item_draft(item, data)
    db.commit()
    safe_trigger_receipt_items(db, [item], "manual_jan")
    return RedirectResponse(_review_url(batch_id, receipt), status_code=303)


@app.post("/receipts/{batch_id}/review/items")
async def add_item(batch_id: int, request: Request, receipt_id: int | None = Form(None), db: Session = Depends(get_db)):
    batch = load_batch(db, batch_id)
    receipt = current_receipt(batch, receipt_id)
    if receipt.confirmation_status == "confirmed":
        raise HTTPException(409, "已确认小票不可编辑")
    try:
        data = await parse_item_form(request)
    except ValidationError as exc:
        raise HTTPException(422, _validation_message(exc)) from exc
    line_no = max((item.line_no for item in receipt.items), default=0) + 1
    item = ReceiptItem(receipt=receipt, line_no=line_no, match_status="unmatched")
    apply_item_draft(item, data)
    db.add(item)
    db.commit()
    safe_trigger_receipt_items(db, [item], "manual_jan")
    return RedirectResponse(_review_url(batch_id, receipt), status_code=303)


@app.post("/receipts/{batch_id}/review/items/{item_id}/ignore")
def ignore_item(batch_id: int, item_id: int, db: Session = Depends(get_db)):
    batch = load_batch(db, batch_id)
    receipt = next((candidate for candidate in batch.receipts if any(row.id == item_id for row in candidate.items)), None)
    if not receipt:
        raise HTTPException(404, "商品行不存在")
    if receipt.confirmation_status == "confirmed":
        raise HTTPException(409, "已确认小票不可编辑")
    item = next((row for row in receipt.items if row.id == item_id), None)
    if not item:
        raise HTTPException(404, "商品行不存在")
    item.review_status = "ignored" if item.review_status != "ignored" else "pending"
    db.commit()
    return RedirectResponse(_review_url(batch_id, receipt), status_code=303)


@app.post("/receipts/{batch_id}/review/items/{item_id}/delete")
def delete_item(batch_id: int, item_id: int, db: Session = Depends(get_db)):
    batch = load_batch(db, batch_id)
    receipt = next((candidate for candidate in batch.receipts if any(row.id == item_id for row in candidate.items)), None)
    if not receipt:
        raise HTTPException(404, "商品行不存在")
    if receipt.confirmation_status == "confirmed":
        raise HTTPException(409, "已确认小票不可编辑")
    item = next((row for row in receipt.items if row.id == item_id), None)
    if not item:
        raise HTTPException(404, "商品行不存在")
    db.delete(item)
    db.commit()
    return RedirectResponse(_review_url(batch_id, receipt), status_code=303)


@app.post("/receipts/{batch_id}/review/confirm")
async def confirm_review(batch_id: int, request: Request, db: Session = Depends(get_db)):
    batch = load_batch(db, batch_id)
    form = await request.form()
    receipt_id = int(form["receipt_id"]) if form.get("receipt_id") else None
    receipt = current_receipt(batch, receipt_id)
    if receipt.confirmation_status == "confirmed":
        raise HTTPException(409, "小票已经确认")
    try:
        overrides = {
            int(key.removeprefix("line_qinsi_target_")): int(value)
            for key, value in form.items()
            if key.startswith("line_qinsi_target_") and str(value).strip()
        }
        settings = PurchaseConfirmationInput.model_validate({
            "initial_location_id": int(form["initial_location_id"]) if form.get("initial_location_id") else None,
            "qinsi_target_warehouse_id": int(form["qinsi_target_warehouse_id"]) if form.get("qinsi_target_warehouse_id") else None,
            "line_qinsi_target_overrides": overrides,
        })
        confirm_receipt(db, batch, receipt, settings)
    except ValidationError as exc:
        raise HTTPException(422, _validation_message(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return RedirectResponse(_review_url(batch_id, receipt), status_code=303)


def _batch_item(batch: ReceiptBatch, item_id: int) -> ReceiptItem:
    item = next((item for receipt in batch.receipts for item in receipt.items if item.id == item_id), None)
    if not item:
        raise HTTPException(404, "商品行不存在")
    return item


@app.post("/receipts/{batch_id}/review/match")
def match_one_receipt(batch_id: int, receipt_id: int | None = Form(None), rematch: bool = Form(False), db: Session = Depends(get_db)):
    batch = load_batch(db, batch_id)
    receipt = current_receipt(batch, receipt_id)
    match_receipt(db, receipt, force=rematch)
    return RedirectResponse(_review_url(batch_id, receipt), status_code=303)


@app.post("/receipts/{batch_id}/match")
def match_whole_batch(batch_id: int, rematch: bool = Form(False), db: Session = Depends(get_db)):
    match_batch(db, load_batch(db, batch_id), force=rematch)
    return RedirectResponse(f"/receipts/{batch_id}/review", status_code=303)


@app.post("/receipts/{batch_id}/review/items/{item_id}/bind")
def bind_receipt_product(batch_id: int, item_id: int, product_id: int = Form(...), db: Session = Depends(get_db)):
    item = _batch_item(load_batch(db, batch_id), item_id)
    if item.review_status != "confirmed":
        raise HTTPException(409, "请先最终确认小票")
    product = db.get(Product, product_id)
    if not product:
        raise HTTPException(404, "商品不存在")
    bind_product(db, item, product)
    return RedirectResponse(_review_url(batch_id, item.receipt) + f"#receipt-item-{item.id}", status_code=303)


@app.post("/receipts/{batch_id}/review/items/{item_id}/new-product")
def create_receipt_product(batch_id: int, item_id: int, db: Session = Depends(get_db)):
    item = _batch_item(load_batch(db, batch_id), item_id)
    if item.review_status != "confirmed":
        raise HTTPException(409, "请先最终确认小票")
    try:
        create_product_from_item(db, item)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return RedirectResponse(_review_url(batch_id, item.receipt) + f"#receipt-item-{item.id}", status_code=303)


@app.get("/products/import", response_class=HTMLResponse)
def product_import_page(request: Request, job_id: int | None = Query(None), db: Session = Depends(get_db)):
    jobs = list(db.scalars(select(ImportJob).where(ImportJob.job_type == "qinsi_products").order_by(ImportJob.created_at.desc()).limit(20)))
    job = db.get(ImportJob, job_id) if job_id else (jobs[0] if jobs else None)
    rows = list(db.scalars(select(ImportRow).where(ImportRow.import_job_id == job.id, ImportRow.status != "skipped").order_by(ImportRow.row_no))) if job else []
    inventory_headers = {"盘点库存数量", "当前库存（导入时不需要录入）", "盘点仓库:", "新日本仓库"}
    for row in rows:
        raw = json.loads(row.raw_json)
        row.display_raw_json = json.dumps({key: value for key, value in raw.items() if key not in inventory_headers}, ensure_ascii=False)
    return templates.TemplateResponse(request, "product_import.html", {"jobs": jobs, "job": job, "rows": rows, "status_cn": IMPORT_STATUS_CN})


@app.post("/products/import/preview")
async def product_import_preview(file: UploadFile | None = File(None), use_reference: str | None = Form(None), db: Session = Depends(get_db)):
    reference_name = "goodsImportTemplate已有商品模版-可到导入到本地数据库.xlsx"
    if file and file.filename:
        filename, content = Path(file.filename).name, await file.read()
    elif use_reference == reference_name:
        filename = reference_name
        content = (PROJECT_ROOT / "reference" / "qinsi" / reference_name).read_bytes()
    else:
        raise HTTPException(422, "请选择Excel文件或项目内已有商品模板")
    try:
        job = create_import_preview(db, filename, content)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return RedirectResponse(f"/products/import?job_id={job.id}", status_code=303)


@app.post("/products/import/{job_id}/confirm")
def product_import_confirm(job_id: int, db: Session = Depends(get_db)):
    job = db.get(ImportJob, job_id)
    if not job:
        raise HTTPException(404, "导入任务不存在")
    try:
        confirm_import(db, job)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return RedirectResponse(f"/products/import?job_id={job.id}", status_code=303)


@app.post("/products/match-by-date")
def product_match_by_date(purchased_date: date = Form(...), rematch: bool = Form(False), db: Session = Depends(get_db)):
    match_date(db, purchased_date, force=rematch)
    return RedirectResponse(f"/products?matched_date={purchased_date.isoformat()}", status_code=303)


@app.get("/products", response_class=HTMLResponse)
def products_page(request: Request, q: str = Query(""), db: Session = Depends(get_db)):
    query = select(
        Product,
        func.count(ReceiptItem.id),
        func.coalesce(func.sum(ReceiptItem.quantity), 0),
        func.max(Receipt.purchased_at),
    ).outerjoin(ReceiptItem, ReceiptItem.product_id == Product.id).outerjoin(Receipt, Receipt.id == ReceiptItem.receipt_id)
    if q.strip():
        value = f"%{q.strip()}%"
        query = query.where(or_(Product.internal_sku.like(value), Product.jan.like(value), Product.qinsi_product_code.like(value), Product.name_cn.like(value)))
    rows = db.execute(query.group_by(Product.id).order_by(Product.updated_at.desc())).all()
    return templates.TemplateResponse(request, "products.html", {"rows": rows, "q": q, "matched_date": request.query_params.get("matched_date")})


@app.get("/product-images/{product_id}")
def product_main_image(product_id: int, db: Session = Depends(get_db)):
    product = db.get(Product, product_id)
    if product is None or not product.main_image_path:
        raise HTTPException(404, "商品主图不存在")
    path = Path(product.main_image_path)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    resolved = path.resolve()
    allowed = PRODUCT_IMAGE_DIR.resolve()
    if not resolved.is_relative_to(allowed) or not resolved.is_file():
        raise HTTPException(404, "商品主图不存在")
    return FileResponse(resolved)


@app.get("/product-enrichment", response_class=HTMLResponse)
def product_enrichment_page(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(request, "product_enrichment.html", {"tasks": list_review_tasks(db)})


@app.post("/product-enrichment/batch-accept")
async def product_enrichment_batch_accept(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    selected = {int(value) for value in form.getlist("task_ids") if str(value).isdigit()}
    tasks = [task for task in list_review_tasks(db) if task.id in selected and (task.confidence or 0) >= .85]
    for task in tasks:
        try:
            accept_task(db, task.id)
        except ValueError:
            db.rollback()
    return RedirectResponse("/product-enrichment", status_code=303)


@app.get("/product-enrichment/{task_id}", response_class=HTMLResponse)
def product_enrichment_detail(task_id: int, request: Request, db: Session = Depends(get_db)):
    try:
        task = get_task(db, task_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    products = list(db.scalars(select(Product).order_by(Product.updated_at.desc()).limit(500)))
    return templates.TemplateResponse(request, "product_enrichment_detail.html", {
        "task": task, "products": products, "warnings": json.loads(task.warnings_json or "[]"),
        "selected": json.loads(task.selected_data_json or "{}"),
        "watch": get_watch(db, task.product_id) if task.product_id else None,
    })


@app.post("/product-enrichment/{task_id}/retry")
def product_enrichment_retry(task_id: int, db: Session = Depends(get_db)):
    try:
        task = get_task(db, task_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    process_enrichment_task(db, task, force=True)
    return RedirectResponse(f"/product-enrichment/{task_id}", status_code=303)


@app.post("/product-enrichment/{task_id}/accept")
def product_enrichment_accept(
    task_id: int, name_cn: str = Form(""), name_ja: str = Form(""),
    candidate_id: int | None = Form(None), db: Session = Depends(get_db),
):
    try:
        product = accept_task(
            db, task_id, name_cn=name_cn.strip() or None, name_ja=name_ja.strip() or None,
            candidate_id=candidate_id,
        )
    except (LookupError, ValueError) as exc:
        db.rollback()
        raise HTTPException(422, str(exc)) from exc
    return RedirectResponse(f"/products/{product.id}", status_code=303)


@app.post("/product-enrichment/{task_id}/existing")
def product_enrichment_existing(task_id: int, product_id: int = Form(...), db: Session = Depends(get_db)):
    try:
        product = bind_task_to_existing(db, task_id, product_id)
    except (LookupError, ValueError) as exc:
        db.rollback()
        raise HTTPException(422, str(exc)) from exc
    return RedirectResponse(f"/products/{product.id}", status_code=303)


@app.get("/locations", response_class=HTMLResponse)
def locations_page(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(request, "locations.html", {
        "locations": list_locations(db), "location_type_cn": LOCATION_TYPE_CN,
    })


@app.get("/purchase-analytics", response_class=HTMLResponse)
def purchase_analytics_page(
    request: Request, range_key: str = Query("30d", alias="range"),
    start_date: date | None = Query(None), end_date: date | None = Query(None),
    bucket: str | None = Query(None), db: Session = Depends(get_db),
):
    period = resolve_date_range(range_key, start_date, end_date)
    context = analytics_dashboard(db, period, selected_bucket=bucket)
    context["range_options"] = (
        ("30d", "最近30天"), ("90d", "最近90天"), ("month", "本月"),
        ("last_month", "上月"), ("year", "今年"), ("custom", "自定义日期"),
    )
    return templates.TemplateResponse(request, "purchase_analytics.html", context)


@app.get("/restock-lists", response_class=HTMLResponse)
def restock_lists_page(
    request: Request, store_id: int | None = Query(None), status_filter: str | None = Query(None, alias="status"),
    db: Session = Depends(get_db),
):
    rows = list_restock_lists(db, store_id=store_id, status=status_filter)
    stores = list(db.scalars(select(Store).where(Store.is_active.is_(True)).order_by(Store.name_cn, Store.name_ja, Store.id)))
    return templates.TemplateResponse(request, "restock_lists.html", {
        "rows": [(row, list_statistics(row)) for row in rows], "stores": stores,
        "store_id": store_id, "status_filter": status_filter or "",
        "list_status_labels": LIST_STATUS_LABELS, "source_labels": SOURCE_LABELS,
    })


@app.get("/restock-lists/new", response_class=HTMLResponse)
def restock_list_new_page(
    request: Request, store_id: int | None = Query(None), source: str = Query("store_history"),
    product_id: list[int] | None = Query(None), db: Session = Depends(get_db),
):
    stores = list(db.scalars(select(Store).where(Store.is_active.is_(True)).order_by(Store.name_cn, Store.name_ja, Store.id)))
    selected_store = db.get(Store, store_id) if store_id else None
    candidates = restock_candidates(db, store_id) if selected_store else []
    products = list(db.scalars(select(Product).where(Product.status == "active").order_by(Product.updated_at.desc()).limit(1000)))
    return templates.TemplateResponse(request, "restock_list_new.html", {
        "stores": stores, "selected_store": selected_store, "candidates": candidates,
        "products": products, "selected_product_ids": set(product_id or []), "source": source,
        "error": request.query_params.get("error"),
    })


@app.post("/restock-lists")
async def restock_list_create(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    store_id = int(form.get("store_id") or 0)
    product_ids = {
        int(value) for value in [*form.getlist("product_ids"), *form.getlist("manual_product_ids")]
        if str(value).isdigit()
    }
    try:
        row = create_restock_list(
            db, name=str(form.get("name") or ""), store_id=store_id, product_ids=product_ids,
            source_type=str(form.get("source_type") or "manual"), notes=str(form.get("notes") or "") or None,
            status=str(form.get("status") or "active"),
        )
    except (LookupError, ValueError, IntegrityError) as exc:
        db.rollback()
        return RedirectResponse(f"/restock-lists/new?store_id={store_id}&error={quote(str(exc))}", status_code=303)
    return RedirectResponse(f"/restock-lists/{row.id}", status_code=303)


@app.post("/restock-lists/from-watches")
async def restock_list_from_watches(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    product_ids = {int(value) for value in form.getlist("restock_product_ids") if str(value).isdigit()}
    try:
        row = create_restock_list(
            db, name=str(form.get("name") or ""), store_id=int(form.get("store_id") or 0),
            product_ids=product_ids, source_type="watched_products", status="active",
            notes=str(form.get("notes") or "") or None,
        )
    except (LookupError, ValueError, IntegrityError) as exc:
        db.rollback()
        return RedirectResponse(f"/watched-products?error={quote(str(exc))}", status_code=303)
    return RedirectResponse(f"/restock-lists/{row.id}", status_code=303)


@app.get("/restock-lists/{list_id}", response_class=HTMLResponse)
def restock_list_detail_page(list_id: int, request: Request, db: Session = Depends(get_db)):
    row = get_restock_list(db, list_id)
    if row is None:
        raise HTTPException(404, "补货清单不存在")
    return templates.TemplateResponse(request, "restock_list_detail.html", {
        "restock_list": row, "stats": list_statistics(row), "trace_rows": trace_rows(db, row),
        "list_status_labels": LIST_STATUS_LABELS, "item_status_labels": ITEM_STATUS_LABELS,
        "source_labels": SOURCE_LABELS, "readonly": row.status in {"completed", "cancelled"},
        "products": list(db.scalars(select(Product).where(Product.status == "active").order_by(Product.updated_at.desc()).limit(1000))),
        "error": request.query_params.get("error"),
    })


@app.post("/restock-lists/{list_id}/status")
def restock_list_status_update(list_id: int, status: str = Form(...), db: Session = Depends(get_db)):
    try:
        update_restock_list_status(db, list_id, status)
    except (LookupError, ValueError) as exc:
        db.rollback()
        return RedirectResponse(f"/restock-lists/{list_id}?error={quote(str(exc))}", status_code=303)
    return RedirectResponse(f"/restock-lists/{list_id}", status_code=303)


@app.post("/restock-lists/{list_id}/copy")
def restock_list_copy(list_id: int, db: Session = Depends(get_db)):
    try:
        row = copy_restock_list(db, list_id)
    except (LookupError, ValueError) as exc:
        db.rollback()
        raise HTTPException(422, str(exc)) from exc
    return RedirectResponse(f"/restock-lists/{row.id}", status_code=303)


@app.post("/restock-lists/{list_id}/items")
def restock_list_add_item(list_id: int, product_id: int = Form(...), db: Session = Depends(get_db)):
    try:
        add_product_to_list(db, list_id, product_id)
    except (LookupError, ValueError, IntegrityError) as exc:
        db.rollback()
        return RedirectResponse(f"/restock-lists/{list_id}?error={quote(str(exc))}", status_code=303)
    return RedirectResponse(f"/restock-lists/{list_id}", status_code=303)


@app.post("/restock-list-items/{item_id}")
def restock_list_item_update(
    item_id: int, status: str = Form(...), planned_quantity: str = Form(""),
    actual_purchase_quantity: str = Form(""), actual_purchase_price: str = Form(""),
    notes: str = Form(""), db: Session = Depends(get_db),
):
    item = db.get(RestockListItem, item_id)
    list_id = item.restock_list_id if item else 0
    try:
        update_restock_item(
            db, item_id, status=status, planned_quantity=planned_quantity,
            actual_quantity=actual_purchase_quantity, actual_price=actual_purchase_price, notes=notes,
        )
    except (LookupError, ValueError) as exc:
        db.rollback()
        return RedirectResponse(f"/restock-lists/{list_id}?error={quote(str(exc))}", status_code=303)
    return RedirectResponse(f"/restock-lists/{list_id}#restock-item-{item_id}", status_code=303)


@app.post("/restock-list-items/{item_id}/link-purchase")
def restock_list_item_link_purchase(item_id: int, purchase_batch_item_id: int = Form(...), db: Session = Depends(get_db)):
    item = db.get(RestockListItem, item_id)
    list_id = item.restock_list_id if item else 0
    try:
        link_purchase_item(db, item_id, purchase_batch_item_id)
    except (LookupError, ValueError) as exc:
        db.rollback()
        return RedirectResponse(f"/restock-lists/{list_id}?error={quote(str(exc))}", status_code=303)
    return RedirectResponse(f"/restock-lists/{list_id}#restock-item-{item_id}", status_code=303)


@app.get("/stores", response_class=HTMLResponse)
def stores_page(request: Request, db: Session = Depends(get_db)):
    brands = list(db.scalars(select(StoreBrand).order_by(StoreBrand.name_cn, StoreBrand.name_ja, StoreBrand.id)))
    stores = list(db.scalars(select(Store).options(selectinload(Store.brand)).order_by(Store.is_active.desc(), Store.name_cn, Store.name_ja, Store.id)))
    summaries = store_overview_summaries(db)
    rows = []
    for store in stores:
        summary = summaries.get(store.id, {"purchase_count": 0, "quantity": 0, "latest_date": None})
        rows.append({
            "store": store, **summary,
        })
    return templates.TemplateResponse(request, "stores.html", {
        "brands": brands, "rows": rows, "error": request.query_params.get("error"),
    })


@app.post("/stores/brands")
def create_store_brand_page(name_cn: str = Form(""), name_ja: str = Form(""), db: Session = Depends(get_db)):
    try:
        create_store_brand(db, StoreBrandCreateInput.model_validate({"name_cn": name_cn or None, "name_ja": name_ja or None}))
    except (ValidationError, ValueError) as exc:
        db.rollback()
        return RedirectResponse(f"/stores?error={quote(str(exc))}", status_code=303)
    return RedirectResponse("/stores", status_code=303)


@app.post("/stores")
def create_store_page(
    brand_id: int | None = Form(None), name_cn: str = Form(""), name_ja: str = Form(""),
    raw_name: str = Form(""), phone: str = Form(""), postal_code: str = Form(""), address: str = Form(""),
    receipt_store_code: str = Form(""), is_active: bool = Form(False), is_online: bool = Form(False),
    db: Session = Depends(get_db),
):
    try:
        store = create_store(db, StoreCreateInput.model_validate({
            "brand_id": brand_id, "name_cn": name_cn or None, "name_ja": name_ja or None,
            "raw_name": raw_name or None, "phone": phone or None, "postal_code": postal_code or None,
            "address": address or None, "receipt_store_code": receipt_store_code or None,
            "is_active": is_active, "is_online": is_online,
        }))
    except (ValidationError, ValueError, IntegrityError) as exc:
        db.rollback()
        return RedirectResponse(f"/stores?error={quote(str(exc))}", status_code=303)
    return RedirectResponse(f"/stores/{store.id}", status_code=303)


@app.get("/stores/{store_id}", response_class=HTMLResponse)
def store_detail(store_id: int, request: Request, db: Session = Depends(get_db)):
    store = db.scalar(select(Store).where(Store.id == store_id).options(selectinload(Store.brand), selectinload(Store.aliases)))
    if store is None:
        raise HTTPException(404, "门店不存在")
    products, facts = store_product_summaries(db, store_id)
    stats = {
        "purchase_count": len({fact.batch.id for fact in facts}),
        "quantity": sum(fact.item.quantity for fact in facts),
        "total_amount": sum(fact.item.actual_line_amount or 0 for fact in facts),
        "product_count": len({fact.item.product_id for fact in facts}),
        "latest_date": max((fact.batch.purchased_at for fact in facts if fact.batch.purchased_at), default=None),
    }
    receipt_rows, seen = [], set()
    for fact in facts:
        if fact.receipt.id not in seen:
            receipt_rows.append(fact)
            seen.add(fact.receipt.id)
    return templates.TemplateResponse(request, "store_detail.html", {
        "store": store, "products": products, "facts": facts, "receipt_rows": receipt_rows, "stats": stats,
        "monthly_trend": store_monthly_trend_points(facts),
        "recent_restock_lists": recent_lists_for_store(db, store_id),
        "restock_status_labels": LIST_STATUS_LABELS,
    })


@app.get("/api/locations", response_model=list[LocationOutput])
def api_locations(db: Session = Depends(get_db)):
    return list_locations(db)


@app.get("/purchase-batches", response_class=HTMLResponse)
def purchase_batches_page(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(request, "purchase_batches.html", {
        "purchase_batches": list_purchase_batches(db), "purchase_status_cn": PURCHASE_STATUS_CN,
    })


@app.get("/purchase-batches/{purchase_batch_id}", response_class=HTMLResponse)
def purchase_batch_detail(purchase_batch_id: int, request: Request, db: Session = Depends(get_db)):
    purchase_batch = get_purchase_batch(db, purchase_batch_id)
    if purchase_batch is None:
        raise HTTPException(404, "采购批次不存在")
    export_states = purchase_item_export_states(db, purchase_batch.id)
    item_states = {item.id: export_states.get(item.id, "pending") for item in purchase_batch.items}
    return templates.TemplateResponse(request, "purchase_batch_detail.html", {
        "purchase_batch": purchase_batch, "purchase_status_cn": PURCHASE_STATUS_CN,
        "item_states": item_states, "purchase_export_state_cn": PURCHASE_EXPORT_STATE_CN,
        "pending_export_count": sum(state == "pending" for state in item_states.values()),
    })


@app.post("/purchase-batches/{purchase_batch_id}/qinsi-exports")
def create_purchase_batch_qinsi_exports(purchase_batch_id: int, db: Session = Depends(get_db)):
    try:
        jobs = generate_purchase_batch_exports(db, purchase_batch_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    if not jobs:
        raise HTTPException(409, "当前采购批次没有可导出的明细")
    destination = f"/qinsi-exports/{jobs[0].id}" if len(jobs) == 1 else f"/qinsi-exports?purchase_batch_id={purchase_batch_id}"
    return RedirectResponse(destination, status_code=303)


@app.get("/qinsi-exports", response_class=HTMLResponse)
def qinsi_exports_page(request: Request, purchase_batch_id: int | None = Query(None), db: Session = Depends(get_db)):
    jobs = list_qinsi_export_jobs(db)
    if purchase_batch_id is not None:
        jobs = [job for job in jobs if job.purchase_batch_id == purchase_batch_id]
    return templates.TemplateResponse(request, "qinsi_exports.html", {
        "jobs": jobs, "purchase_batch_id": purchase_batch_id,
        "qinsi_export_status_cn": QINSI_EXPORT_STATUS_CN, "qinsi_export_type_cn": QINSI_EXPORT_TYPE_CN,
    })


def _qinsi_export_or_404(db: Session, export_job_id: int) -> QinsiPurchaseExportJob:
    job = get_qinsi_export_job(db, export_job_id)
    if job is None:
        raise HTTPException(404, "秦丝导出记录不存在")
    return job


@app.get("/qinsi-exports/{export_job_id}", response_class=HTMLResponse)
def qinsi_export_detail(export_job_id: int, request: Request, db: Session = Depends(get_db)):
    job = _qinsi_export_or_404(db, export_job_id)
    return templates.TemplateResponse(request, "qinsi_export_detail.html", {
        "job": job, "qinsi_export_status_cn": QINSI_EXPORT_STATUS_CN,
        "qinsi_export_type_cn": QINSI_EXPORT_TYPE_CN, "qinsi_line_status_cn": QINSI_LINE_STATUS_CN,
    })


@app.get("/qinsi-exports/{export_job_id}/download")
def qinsi_export_download(export_job_id: int, db: Session = Depends(get_db)):
    job = _qinsi_export_or_404(db, export_job_id)
    return StreamingResponse(
        io.BytesIO(job.file_content),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(job.filename)}"},
    )


@app.post("/qinsi-exports/{export_job_id}/confirm")
async def qinsi_export_confirm(export_job_id: int, request: Request, db: Session = Depends(get_db)):
    job = _qinsi_export_or_404(db, export_job_id)
    form = await request.form()
    try:
        confirmation = QinsiExportConfirmationInput.model_validate({
            "result": str(form.get("result") or ""),
            "failed_line_ids": {int(value) for value in form.getlist("failed_line_ids")},
        })
        confirm_qinsi_export(db, job, confirmation)
    except ValidationError as exc:
        raise HTTPException(422, _validation_message(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return RedirectResponse(f"/qinsi-exports/{export_job_id}", status_code=303)


@app.post("/qinsi-exports/{export_job_id}/retry")
async def qinsi_export_retry(export_job_id: int, request: Request, db: Session = Depends(get_db)):
    job = _qinsi_export_or_404(db, export_job_id)
    form = await request.form()
    try:
        line_ids = {int(value) for value in form.getlist("line_ids")}
        retry_job = retry_failed_qinsi_lines(db, job, line_ids)
    except (TypeError, ValueError) as exc:
        raise HTTPException(409, str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    return RedirectResponse(f"/qinsi-exports/{retry_job.id}", status_code=303)


@app.get("/qinsi-inventory-snapshots", response_class=HTMLResponse)
def qinsi_inventory_snapshot_list(request: Request, db: Session = Depends(get_db)):
    latest, warehouse_stats = latest_snapshot_statistics(db)
    return templates.TemplateResponse(request, "qinsi_inventory_snapshots.html", {
        "snapshots": list_inventory_snapshots(db),
        "status_labels": QINSI_SNAPSHOT_STATUS_CN,
        "latest": latest,
        "warehouse_stats": warehouse_stats,
        "watch_status_stats": watched_inventory_status_distribution(db),
        "inventory_status_labels": INVENTORY_STATUS_LABELS,
        "message": request.query_params.get("message"),
    })


@app.get("/qinsi-inventory-snapshots/upload", response_class=HTMLResponse)
def qinsi_inventory_snapshot_upload_page(request: Request):
    return templates.TemplateResponse(request, "qinsi_inventory_snapshot_upload.html", {
        "settings": inventory_settings(), "error": None,
    })


@app.post("/qinsi-inventory-snapshots/upload", response_class=HTMLResponse)
async def qinsi_inventory_snapshot_upload(
    request: Request,
    file: UploadFile = File(...),
    data_at: str = Form(""),
    db: Session = Depends(get_db),
):
    try:
        parsed_data_at = datetime.fromisoformat(data_at) if data_at.strip() else None
        if parsed_data_at is not None and parsed_data_at.tzinfo is None:
            parsed_data_at = parsed_data_at.replace(tzinfo=TOKYO).astimezone(timezone.utc)
        snapshot, reused = create_inventory_snapshot(
            db, file.filename or "inventory.xlsx", await file.read(), data_at=parsed_data_at,
        )
    except ValueError as exc:
        db.rollback()
        return templates.TemplateResponse(request, "qinsi_inventory_snapshot_upload.html", {
            "settings": inventory_settings(), "error": str(exc),
        }, status_code=422)
    message = "重复文件，已返回原快照" if reused else "库存快照导入完成"
    return RedirectResponse(f"/qinsi-inventory-snapshots/{snapshot.id}?message={quote(message)}", status_code=303)


def _qinsi_inventory_snapshot_or_404(db: Session, snapshot_id: int):
    snapshot = get_inventory_snapshot(db, snapshot_id)
    if snapshot is None:
        raise HTTPException(404, "库存快照不存在")
    return snapshot


@app.get("/qinsi-inventory-snapshots/{snapshot_id}", response_class=HTMLResponse)
def qinsi_inventory_snapshot_detail(snapshot_id: int, request: Request, db: Session = Depends(get_db)):
    snapshot = _qinsi_inventory_snapshot_or_404(db, snapshot_id)
    warehouse_distribution: dict[str, int] = {}
    for line in snapshot.lines:
        name = line.warehouse.display_name if line.warehouse else (line.raw_warehouse_name or "未知仓库")
        warehouse_distribution[name] = warehouse_distribution.get(name, 0) + (line.quantity or 0)
    return templates.TemplateResponse(request, "qinsi_inventory_snapshot_detail.html", {
        "snapshot": snapshot,
        "status_labels": QINSI_SNAPSHOT_STATUS_CN,
        "match_status_labels": MATCH_STATUS_LABELS,
        "match_method_labels": MATCH_METHOD_LABELS,
        "warehouse_distribution": warehouse_distribution,
        "message": request.query_params.get("message"),
    })


@app.get("/qinsi-inventory-snapshots/{snapshot_id}/review", response_class=HTMLResponse)
def qinsi_inventory_snapshot_review(snapshot_id: int, request: Request, db: Session = Depends(get_db)):
    snapshot = _qinsi_inventory_snapshot_or_404(db, snapshot_id)
    products = list(db.scalars(select(Product).where(Product.status == "active").order_by(Product.internal_sku)))
    return templates.TemplateResponse(request, "qinsi_inventory_snapshot_review.html", {
        "snapshot": snapshot, "products": products,
        "warehouses": available_qinsi_warehouses(db),
        "match_status_labels": MATCH_STATUS_LABELS,
        "match_method_labels": MATCH_METHOD_LABELS,
        "message": request.query_params.get("message"),
    })


@app.get("/qinsi-inventory-snapshots/{snapshot_id}/download")
def qinsi_inventory_snapshot_download(snapshot_id: int, db: Session = Depends(get_db)):
    snapshot = _qinsi_inventory_snapshot_or_404(db, snapshot_id)
    return StreamingResponse(
        io.BytesIO(snapshot.file_content),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(snapshot.original_filename)}"},
    )


@app.post("/qinsi-inventory-snapshot-lines/{line_id}/match")
def qinsi_inventory_snapshot_line_match(
    line_id: int, product_id: int = Form(...), db: Session = Depends(get_db),
):
    try:
        line = manual_match_line(db, line_id, product_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    return RedirectResponse(f"/qinsi-inventory-snapshots/{line.snapshot_id}/review?message={quote('人工匹配已保存')}", status_code=303)


@app.post("/qinsi-inventory-snapshot-lines/{line_id}/warehouse")
def qinsi_inventory_snapshot_line_warehouse(
    line_id: int, warehouse_id: int = Form(...), db: Session = Depends(get_db),
):
    try:
        line = map_line_warehouse(db, line_id, warehouse_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return RedirectResponse(f"/qinsi-inventory-snapshots/{line.snapshot_id}/review?message={quote('仓库映射已保存')}", status_code=303)


@app.post("/qinsi-inventory-snapshots/{snapshot_id}/retry-matching")
def qinsi_inventory_snapshot_retry(snapshot_id: int, db: Session = Depends(get_db)):
    try:
        count = retry_snapshot_matching(db, snapshot_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    return RedirectResponse(f"/qinsi-inventory-snapshots/{snapshot_id}/review?message={quote(f'重新匹配成功{count}行')}", status_code=303)


@app.post("/qinsi-inventory-snapshots/{snapshot_id}/ignore-lines")
async def qinsi_inventory_snapshot_ignore(snapshot_id: int, request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    ids = {int(value) for value in form.getlist("line_ids") if str(value).isdigit()}
    try:
        count = ignore_snapshot_lines(db, snapshot_id, ids)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    return RedirectResponse(f"/qinsi-inventory-snapshots/{snapshot_id}/review?message={quote(f'已忽略{count}行')}", status_code=303)


@app.get("/api/purchase-batches", response_model=list[PurchaseBatchOutput])
def api_purchase_batches(db: Session = Depends(get_db)):
    return list_purchase_batches(db)


@app.get("/notifications", response_class=HTMLResponse)
def notifications_page(
    request: Request, include_archived: bool = Query(False), db: Session = Depends(get_db),
):
    return templates.TemplateResponse(request, "notifications.html", {
        "notifications": list_notifications(db, include_archived=include_archived),
        "include_archived": include_archived,
        "notification_type_labels": NOTIFICATION_TYPE_LABELS,
        "message": request.query_params.get("message"),
    })


@app.post("/notifications/{notification_id}/read")
def notification_read(notification_id: int, db: Session = Depends(get_db)):
    try:
        mark_notification_read(db, notification_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    return RedirectResponse("/notifications", status_code=303)


@app.post("/notifications/batch-read")
async def notifications_batch_read(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    ids = {int(value) for value in form.getlist("notification_ids") if str(value).isdigit()}
    count = bulk_mark_notifications_read(db, ids)
    return RedirectResponse(f"/notifications?message={quote(f'已标记{count}条通知为已读')}", status_code=303)


@app.post("/notifications/archive-read")
def notifications_archive_read(db: Session = Depends(get_db)):
    count = archive_read_notifications(db)
    return RedirectResponse(f"/notifications?message={quote(f'已归档{count}条已读通知')}", status_code=303)


@app.get("/monitor-status", response_class=HTMLResponse)
def monitor_status_page(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(request, "monitor_status.html", {
        "dashboard": monitor_dashboard(db),
        "scheduler_running": scheduler_running(),
        "message": request.query_params.get("message"),
    })


@app.post("/monitor/run-once")
def monitor_run_once(background_tasks: BackgroundTasks):
    background_tasks.add_task(run_due_monitor_cycle)
    return RedirectResponse(f"/monitor-status?message={quote('已提交一轮到期监控任务')}", status_code=303)


def _return_path(value: str, fallback: str = "/watched-products") -> str:
    return value if value.startswith("/") and not value.startswith("//") else fallback


@app.get("/watched-products", response_class=HTMLResponse)
def watched_products_page(request: Request, db: Session = Depends(get_db)):
    generate_watch_recommendations(db)
    watched_ids = set(db.scalars(select(ProductWatchConfig.product_id)))
    products = list(db.scalars(
        select(Product)
        .where(Product.status == "active", Product.id.not_in(watched_ids))
        .order_by(Product.updated_at.desc())
        .limit(500)
    ))
    return templates.TemplateResponse(request, "watched_products.html", {
        "groups": list_watch_groups(db), "products": products,
        "stores": list(db.scalars(select(Store).where(Store.is_active.is_(True)).order_by(Store.name_cn, Store.name_ja, Store.id))),
        "frequency_hours": FREQUENCY_HOURS, "reason_labels": REASON_LABELS,
        "message": request.query_params.get("message"), "error": request.query_params.get("error"),
    })


@app.post("/watched-products/add")
def watched_products_add(
    product_id: int = Form(...), user_target_price: str = Form(""),
    frequency_tier: str = Form("normal"), return_to: str = Form("/watched-products"),
    db: Session = Depends(get_db),
):
    try:
        add_watch(
            db, product_id, source="manual", user_target_price=user_target_price or None,
            frequency_tier=frequency_tier,
        )
    except (LookupError, ValueError) as exc:
        db.rollback()
        return RedirectResponse(f"{_return_path(return_to)}?error={quote(str(exc))}", status_code=303)
    return RedirectResponse(_return_path(return_to), status_code=303)


@app.post("/watched-products/{product_id}/update")
def watched_products_update(
    product_id: int, user_target_price: str = Form(""), frequency_tier: str = Form("normal"),
    monitor_restock: bool = Form(False), return_to: str = Form("/watched-products"),
    db: Session = Depends(get_db),
):
    try:
        update_watch(
            db, product_id, user_target_price=user_target_price or None,
            frequency_tier=frequency_tier, monitor_restock=monitor_restock,
        )
    except (LookupError, ValueError) as exc:
        db.rollback()
        return RedirectResponse(f"{_return_path(return_to)}?error={quote(str(exc))}", status_code=303)
    return RedirectResponse(_return_path(return_to), status_code=303)


@app.post("/watched-products/{product_id}/check-now")
def watched_product_check_now(
    product_id: int, background_tasks: BackgroundTasks, db: Session = Depends(get_db),
):
    config = get_watch(db, product_id)
    if config is None:
        return RedirectResponse(f"/watched-products?error={quote('关注配置不存在')}", status_code=303)
    if not config.enabled or config.effective_target_price is None or not config.product.jan:
        return RedirectResponse(f"/watched-products?error={quote('只有已启用且目标价和JAN有效的关注可立即检查')}", status_code=303)
    background_tasks.add_task(run_single_monitor_cycle, product_id)
    return RedirectResponse(f"/watched-products?message={quote('已提交立即检查任务')}", status_code=303)


@app.post("/watched-products/{product_id}/enable")
def watched_products_enable(
    product_id: int, return_to: str = Form("/watched-products"), db: Session = Depends(get_db),
):
    try:
        set_watch_enabled(db, product_id, True)
    except (LookupError, ValueError) as exc:
        db.rollback()
        return RedirectResponse(f"{_return_path(return_to)}?error={quote(str(exc))}", status_code=303)
    return RedirectResponse(_return_path(return_to), status_code=303)


@app.post("/watched-products/{product_id}/pause")
def watched_products_pause(
    product_id: int, return_to: str = Form("/watched-products"), db: Session = Depends(get_db),
):
    try:
        set_watch_enabled(db, product_id, False)
    except LookupError as exc:
        db.rollback()
        return RedirectResponse(f"{_return_path(return_to)}?error={quote(str(exc))}", status_code=303)
    return RedirectResponse(_return_path(return_to), status_code=303)


@app.post("/watched-products/recommendations/{recommendation_id}/accept")
def watched_recommendation_accept(recommendation_id: int, db: Session = Depends(get_db)):
    accept_recommendations(db, {recommendation_id})
    return RedirectResponse("/watched-products", status_code=303)


@app.post("/watched-products/recommendations/{recommendation_id}/ignore")
def watched_recommendation_ignore(recommendation_id: int, db: Session = Depends(get_db)):
    try:
        ignore_recommendation(db, recommendation_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    return RedirectResponse("/watched-products", status_code=303)


@app.post("/watched-products/batch-accept")
async def watched_products_batch_accept(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    ids = {int(value) for value in form.getlist("recommendation_ids") if str(value).isdigit()}
    accepted = accept_recommendations(db, ids)
    return RedirectResponse(f"/watched-products?message={quote(f'已接受{len(accepted)}条合法推荐')}", status_code=303)


@app.post("/watched-products/batch-enable")
async def watched_products_batch_enable(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    ids = {int(value) for value in form.getlist("product_ids") if str(value).isdigit()}
    enabled = bulk_enable_watches(db, ids)
    return RedirectResponse(f"/watched-products?message={quote(f'已启用{len(enabled)}个有有效目标价的商品')}", status_code=303)


@app.get("/products/{product_id}", response_class=HTMLResponse)
def product_detail(product_id: int, request: Request, db: Session = Depends(get_db)):
    product = db.get(Product, product_id)
    if not product:
        raise HTTPException(404, "商品不存在")
    rows = db.execute(
        select(ReceiptItem, Receipt, ReceiptBatch, ReceiptImage)
        .join(Receipt, Receipt.id == ReceiptItem.receipt_id)
        .join(ReceiptBatch, ReceiptBatch.id == Receipt.batch_id)
        .outerjoin(PurchaseBatchItem, PurchaseBatchItem.receipt_item_id == ReceiptItem.id)
        .outerjoin(PurchaseBatch, PurchaseBatch.id == PurchaseBatchItem.purchase_batch_id)
        .outerjoin(ReceiptImage, ReceiptImage.id == ReceiptItem.source_image_id)
        .where(
            ReceiptItem.product_id == product_id,
            Receipt.confirmation_status == "confirmed",
            ReceiptBatch.status.not_in({"deleted", "failed", "cancelled"}),
            or_(PurchaseBatch.id.is_(None), PurchaseBatch.status != "cancelled"),
        )
        .order_by(Receipt.purchased_at.desc(), ReceiptItem.id.desc())
    ).all()
    facts = purchase_facts(db, product_id=product_id)
    prices = [fact.reference_unit_price for fact in facts if fact.reference_unit_price is not None]
    latest_fact = max(facts, key=lambda fact: (fact.batch.purchased_at or fact.batch.confirmed_at, fact.item.id), default=None)
    legacy_prices = []
    if not facts:
        for item, _, _, _ in rows:
            amount = item.line_total if item.line_total is not None else (item.unit_price * item.quantity - item.discount_amount if item.unit_price is not None else None)
            if amount is not None:
                legacy_prices.append(Decimal(amount) / item.quantity)
    stats = {
        "count": len({fact.batch.id for fact in facts}) if facts else len(rows),
        "quantity": sum(fact.item.quantity for fact in facts) if facts else sum(item.quantity for item, _, _, _ in rows),
        "latest_price": latest_fact.reference_unit_price if latest_fact else (legacy_prices[0] if legacy_prices else None),
        "latest_date": latest_fact.batch.purchased_at if latest_fact else (rows[0][1].purchased_at if rows else None),
        "minimum_price": min(prices) if prices else (min(legacy_prices) if legacy_prices else None),
        "maximum_price": max(prices) if prices else (max(legacy_prices) if legacy_prices else None),
        "average_price": (
            Decimal(sum(fact.item.actual_line_amount for fact in facts if fact.item.actual_line_amount is not None))
            / sum(fact.item.quantity for fact in facts if fact.item.actual_line_amount is not None)
        ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) if any(fact.item.actual_line_amount is not None for fact in facts) else None,
    }
    purchase_details = list(db.scalars(
        select(PurchaseBatchItem)
        .join(PurchaseBatch, PurchaseBatch.id == PurchaseBatchItem.purchase_batch_id)
        .where(PurchaseBatchItem.product_id == product_id, PurchaseBatch.status != "cancelled")
        .options(
            selectinload(PurchaseBatchItem.purchase_batch),
            selectinload(PurchaseBatchItem.initial_location),
            selectinload(PurchaseBatchItem.qinsi_target_warehouse),
        )
        .order_by(PurchaseBatchItem.id.desc())
    ))
    inventory = latest_inventory_for_product(db, product_id)
    online_prices = list(db.scalars(
        select(ProductWatchSnapshot).where(
            ProductWatchSnapshot.product_id == product_id,
            ProductWatchSnapshot.status == "success",
            ProductWatchSnapshot.total_price.is_not(None),
        ).order_by(ProductWatchSnapshot.checked_at.desc(), ProductWatchSnapshot.id.desc()).limit(180)
    ))
    combined = [
        {"kind": "purchase", "date": fact.batch.purchased_at or fact.batch.confirmed_at,
         "price": fact.reference_unit_price, "fact": fact, "snapshot": None}
        for fact in facts if fact.reference_unit_price is not None
    ] + [
        {"kind": "online", "date": snapshot.checked_at, "price": Decimal(snapshot.total_price),
         "fact": None, "snapshot": snapshot}
        for snapshot in online_prices
    ]
    combined.sort(key=lambda point: (point["date"], point["kind"]))
    if combined:
        low = min(point["price"] for point in combined)
        span = max(Decimal(1), max(point["price"] for point in combined) - low)
        for index, point in enumerate(combined):
            point["x"] = 30 if len(combined) == 1 else 30 + index * 540 / (len(combined) - 1)
            point["y"] = 185 - float((point["price"] - low) * 145 / span)
    return templates.TemplateResponse(request, "product_detail.html", {
        "product": product, "rows": rows, "stats": stats, "purchase_details": purchase_details,
        "store_summaries": product_store_summaries(db, product_id),
        "trend_points": product_trend_points(facts), "purchase_facts": facts,
        "combined_price_points": combined,
        "purchase_price_points": [point for point in combined if point["kind"] == "purchase"],
        "online_price_points": [point for point in combined if point["kind"] == "online"],
        "current_online_price": online_prices[0] if online_prices else None,
        "watch": get_watch(db, product_id),
        "active_restock_lists": active_lists_for_product(db, product_id),
        "product_restock_lists": lists_for_product(db, product_id),
        "restock_status_labels": LIST_STATUS_LABELS,
        "restock_item_status_labels": ITEM_STATUS_LABELS,
        "inventory": inventory,
        "purchase_assistance": purchase_assistance(db, product, inventory=inventory),
        "inventory_status_labels": INVENTORY_STATUS_LABELS,
        "inventory_settings": inventory_settings(),
        "saved": request.query_params.get("saved"), "error": request.query_params.get("error"),
    })


@app.post("/products/{product_id}")
def update_product_page(
    product_id: int, name_cn: str = Form(...), name_ja: str = Form(""), jan: str | None = Form(None),
    qinsi_product_code: str | None = Form(None), db: Session = Depends(get_db),
):
    product = db.get(Product, product_id)
    if not product:
        raise HTTPException(404, "商品不存在")
    try:
        update_product_identifiers(db, product, jan=jan, qinsi_product_code=qinsi_product_code)
        product.name_cn = normalize_product_name(name_cn, "中文名") or product.name_cn
        product.name_ja = normalize_product_name(name_ja, "日文名") or product.name_ja
        product.display_name = format_product_display_name(product.name_cn, product.name_ja)
        product.product_data_confirmed = True
        product.name_locked = True
        db.commit()
    except ValueError as exc:
        db.rollback()
        return RedirectResponse(f"/products/{product_id}?error={quote(str(exc))}", status_code=303)
    except IntegrityError:
        db.rollback()
        return RedirectResponse(f"/products/{product_id}?error={quote('JAN或秦丝商品编码已存在，不能重复保存')}", status_code=303)
    return RedirectResponse(f"/products/{product_id}?saved=1", status_code=303)


@app.post("/products/{product_id}/inventory-settings")
def update_product_inventory_settings(
    product_id: int, low_stock_threshold: str = Form(""), db: Session = Depends(get_db),
):
    try:
        update_product_low_stock_threshold(db, product_id, low_stock_threshold)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        return RedirectResponse(f"/products/{product_id}?error={quote(str(exc))}", status_code=303)
    return RedirectResponse(f"/products/{product_id}?saved=1", status_code=303)


@app.post("/api/products", response_model=ProductOutput, status_code=201)
def api_create_product(data: ProductCreateInput, db: Session = Depends(get_db)):
    try:
        return create_product_record(db, name_cn=data.name_cn, jan=data.jan, qinsi_product_code=data.qinsi_product_code)
    except ValueError as exc:
        db.rollback()
        raise HTTPException(409, str(exc)) from exc
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(409, "JAN、秦丝商品编码或内部SKU发生唯一性冲突") from exc


@app.patch("/api/products/{product_id}", response_model=ProductOutput)
def api_update_product(product_id: int, data: ProductUpdateInput, db: Session = Depends(get_db)):
    product = db.get(Product, product_id)
    if not product:
        raise HTTPException(404, "商品不存在")
    fields = data.model_fields_set
    try:
        update_product_identifiers(
            db, product,
            jan=data.jan if "jan" in fields else product.jan,
            qinsi_product_code=data.qinsi_product_code if "qinsi_product_code" in fields else product.qinsi_product_code,
        )
        if "name_cn" in fields and data.name_cn:
            product.name_cn = data.name_cn
            product.display_name = format_product_display_name(product.name_cn, product.name_ja)
            product.product_data_confirmed = True
            product.name_locked = True
        db.commit()
        db.refresh(product)
        return product
    except ValueError as exc:
        db.rollback()
        raise HTTPException(409, str(exc)) from exc
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(409, "JAN、秦丝商品编码或内部SKU发生唯一性冲突") from exc


@app.post("/receipts/{batch_id}/delete")
def delete_page(batch_id: int, db: Session = Depends(get_db)):
    delete_unconfirmed_batch(db, get_batch_or_404(db, batch_id))
    return RedirectResponse("/receipts", status_code=303)


@app.get("/api/receipt-batches")
def api_batches(db: Session = Depends(get_db)):
    return [serialize_batch(batch) for batch in db.scalars(batch_query().order_by(ReceiptBatch.created_at.desc())).unique()]


@app.get("/api/receipt-batches/{batch_id}")
def api_batch(batch_id: int, db: Session = Depends(get_db)):
    return serialize_batch(load_batch(db, batch_id))


@app.get("/api/receipt-batches/{batch_id}/status")
def api_batch_status(batch_id: int, db: Session = Depends(get_db)):
    return serialize_batch_status(load_batch(db, batch_id))


@app.post("/api/receipt-batches/upload", status_code=201)
async def api_upload(background_tasks: BackgroundTasks, request: Request, files: list[UploadFile] = File(...), source_type: str = Form("unknown"), request_id: str | None = Form(None), db: Session = Depends(get_db)):
    logger.info("receipt_upload_request stage='files_received' host=%r scheme=%r content_length=%r user_agent=%r file_count=%s request_id=%r", (request.headers.get("host") or "")[:200], (request.headers.get("x-forwarded-proto") or request.url.scheme)[:16], (request.headers.get("content-length") or "unknown")[:30], (request.headers.get("user-agent") or "")[:300], len(files), request_id)
    result = await upload_receipt_images(db, files, source_type, request_id)
    payload = serialize_upload_result(result)
    if not result.batch:
        payload["detail"] = "全部图片上传失败"
        return JSONResponse(payload, status_code=415)
    if not result.replayed and not result.duplicate_only:
        background_tasks.add_task(process_receipt_batch, result.batch.id, db.get_bind())
    return payload


@app.post("/api/receipt-batches/{batch_id}/recognition-json")
async def api_recognition(batch_id: int, request: Request, db: Session = Depends(get_db)):
    raw = (await request.body()).decode("utf-8")
    batch = load_batch(db, batch_id)
    try:
        receipt = import_recognition_json(db, batch, raw)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"receipt_id": receipt.id, "batch_id": batch_id, "status": "imported", "recognition_run_count": len(batch.recognition_runs)}


@app.delete("/api/receipt-batches/{batch_id}", status_code=204)
def api_delete(batch_id: int, db: Session = Depends(get_db)):
    delete_unconfirmed_batch(db, get_batch_or_404(db, batch_id))

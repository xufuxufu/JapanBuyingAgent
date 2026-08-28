from __future__ import annotations

import asyncio
import io
import json
import os
import time
import unicodedata
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import quote

from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import func, or_, select, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, selectinload

from app.config import PREVIEW_DIR, PRODUCT_IMAGE_DIR, PROJECT_ROOT, QINSI_PRODUCT_IMAGE_DIR, TAG_EVIDENCE_DIR, default_role, ensure_data_directories, get_deepseek_config, is_testing
from app.db import SessionLocal, engine, get_db
from app.models import Customer, DurableBackgroundJob, DuplicateDetectionLog, EnrichmentAuditLog, FieldPurchaseItem, PlatformProviderState, Product, ProductAlias, ProductEnrichmentCandidate, ProductWatchConfig, ProductWatchSnapshot, PurchaseBatch, PurchaseBatchItem, QinsiExportJob, QinsiGoodsImportRow, QinsiImportBatch, QinsiPurchaseExportJob, Receipt, ReceiptBatch, ReceiptImage, ReceiptItem, RestockList, RestockListItem, SalesOrder, Salesperson, Store, StoreBrand, TagEvidence, ZipPackageItem, ZipPackageJob
from app.analytics_service import analytics_dashboard, procurement_data, resolve_date_range
from app.product_matching import (
    bind_product,
    candidate_products_for_jan,
    create_product_from_item,
    match_batch,
    match_date,
    match_item,
    match_receipt,
    normalize_alias,
    refresh_resolved_conflicts,
    validate_jan,
)
from app.product_merge import list_duplicate_jan_groups, merge_all_duplicate_jans, merge_duplicate_jan_group
from app.product_identity import create_product_record, format_product_display_name, normalize_product_name, update_product_identifiers
from app.product_admin import (
    EDITABLE_PRODUCT_STATUSES,
    archive_product,
    delete_product_if_allowed,
    execute_placeholder_cleanup,
    product_associations,
    preview_placeholder_cleanup,
    restore_product,
    save_product_photo_for_completion,
    update_product_master,
)
from app.price_service import build_lookup_view, query_prices, recent_price_lookup_histories
from app.product_enrichment import (
    accept_task, bind_task_to_existing, enrichment_summary_for_receipt, ensure_existing_product_enrichment_task, get_task,
    list_review_tasks, process_enrichment_task, process_price_lookup_enrichment, refresh_existing_product_main_image,
    product_needs_jan_completion, safe_trigger_receipt_items, translate_candidate,
)
from app.field_purchase import (
    assign_field_item_jan, bind_field_item_to_product, bulk_edit_field_items, complete_field_batch, confirm_field_item,
    create_field_batch, create_new_product_draft,
    get_field_batch, get_field_item, list_active_field_batches, list_field_review_items,
    process_durable_job, process_pending_jobs, product_lookup_payload,
    record_ambiguous_scan_for_review, record_existing_scan,
    retry_field_items, save_field_product_image, update_field_batch_store, update_field_item,
)
from app.location_service import get_default_physical_location, initialize_default_locations, list_locations
from app.local_product import normalize_jan, resolve_local_product_by_jan
from app.jan_governance import build_jan_governance_rows, export_jan_governance_report
from app.product_image_localization import (
    image_localization_dashboard, product_display_image, queue_missing_product_images,
    preferred_product_image_url, queue_product_image_localization, retry_failed_product_images,
)
from app.image_localization_worker import (
    start_image_localization_worker, stop_image_localization_worker,
    wake_image_localization_worker,
)
from app.monitor_scheduler import scheduler_running, start_monitor_scheduler, stop_monitor_scheduler
from app.monitor_service import (
    NOTIFICATION_TYPE_LABELS, archive_read_notifications, bulk_mark_notifications_read,
    list_notifications, mark_notification_read, monitor_dashboard, run_due_monitor_cycle,
    run_single_monitor_cycle, unread_notification_count,
)
from app.purchase_service import ensure_purchase_batch_for_receipt, get_purchase_batch, list_purchase_batches, purchase_batch_blockers_for_receipt
from app.receipt_pricing import actual_line_amount, purchase_unit_price
from app.qinsi_export import (
    QINSI_PRODUCT_EXPORTABLE_STATUSES,
    cancel_qinsi_export_confirmation, cancel_qinsi_product_export_confirmation,
    confirm_qinsi_export, confirm_qinsi_product_export, confirmed_qinsi_product_import_product_ids, create_qinsi_product_export,
    generate_merged_purchase_batch_export, generate_purchase_batch_exports, get_qinsi_export_job, get_qinsi_product_export_job,
    list_qinsi_export_jobs, list_qinsi_product_export_jobs, purchase_item_export_states,
    pending_qinsi_product_exports, qinsi_product_export_image_warning, qinsi_product_export_rows, qinsi_product_has_export_name,
    qinsi_product_is_exportable, qinsi_product_requires_import,
    regenerate_qinsi_product_export_file, retry_failed_qinsi_lines,
)
from app.qinsi_goods_import import (
    create_import_batch, process_queued_confirmation, process_queued_import,
    queue_import_confirmation,
)
from app.qinsi_product_master_import import (
    MASTER_SOURCE,
    QINSI_CONFLICT_RESOLUTION_TYPES,
    MasterInputFile,
    confirm_qinsi_master_import,
    create_qinsi_master_preview,
    export_qinsi_master_audit_workbook,
    qinsi_master_preview_statistics,
    resolve_qinsi_master_conflict,
)
from app.qinsi_inventory import (
    INVENTORY_STATUS_LABELS, MATCH_METHOD_LABELS, MATCH_STATUS_LABELS,
    available_qinsi_warehouses, create_inventory_snapshot, get_inventory_snapshot,
    ignore_snapshot_lines, inventory_settings, latest_inventory_for_product, latest_inventory_for_products,
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
from app.sales_order_service import (
    ALLOWED_TRANSITIONS, PRIMARY_NEXT_ACTION, SHIPPING_LABEL_DELETABLE_STATUSES,
    SHIPPING_LABEL_UPLOADABLE_STATUSES, STATUS_LABELS as SALES_ORDER_STATUS_CN,
    SalesOrderItemInput, add_shipping_label, create_customer, create_sales_order,
    ensure_default_salesperson, get_sales_order, get_shipping_label, list_sales_orders,
    list_salespersons, remove_shipping_label, search_customers,
    search_products as search_sales_order_products,
    status_counts as sales_order_status_counts, update_sales_order_status,
)
from app.sales_order_shipping import resolve_shipping_label_path
from app.procurement_service import (
    DEMAND_TYPE_LABELS, FRESHNESS_LABELS, SOURCE_TYPE_LABELS, STATUS_LABELS as PROCUREMENT_STATUS_LABELS,
    PlanSelectionInput, aggregate_all_demand_groups, aggregate_open_demand_groups,
    build_group_inventory_contexts, build_plan_inventory_contexts,
    close_investigation_demand, create_channel_shortage_demand, create_investigation_demand,
    create_plans, default_planned_quantity_for_group, get_group, list_investigation_demands, list_plans,
)
from app.provider_config import diagnostic_summary, provider_status_rows, test_provider_connection
from app.rakuten_ip_monitor import rakuten_public_ip_status
from app.product_translation_service import (
    count_missing_chinese_name_products,
    product_display_label,
    is_missing_chinese_name,
    translate_missing_chinese_names,
    translate_product_chinese_name,
)
from app.watch_service import (
    FREQUENCY_HOURS, REASON_LABELS, accept_recommendations, add_watch, bulk_enable_watches,
    generate_watch_recommendations, get_watch, ignore_recommendation, list_watch_groups,
    remove_watch, set_watch_enabled, update_watch,
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
IMPORT_STATUS_CN = {
    "queued": "等待解析", "parsing": "解析中", "previewed": "待确认导入", "importing": "导入中",
    "completed": "导入完成", "completed_with_issues": "导入完成（有冲突或错误）",
    "failed": "失败", "obsolete": "已废弃", "new": "新增", "update": "更新",
    "unchanged": "无变化", "skipped": "跳过", "conflict": "冲突", "error": "错误",
    "imported_new": "已新增", "imported_updated": "已更新",
}
BATCH_STATUS_CN = {"uploaded": "已上传", "processing": "处理中", "ready": "图片就绪", "review": "待审核", "confirmed": "已确认", "failed": "失败", "deleted": "已删除"}
PREPROCESS_STATUS_CN = {"previewed": "待处理", "processing": "处理中", "processed": "处理完成", "fallback": "已回退原图", "failed": "处理失败"}
AI_STATUS_CN = {"accepted": "已接受", "imported": "已导入", "failed": "失败", "error": "错误"}
LOCATION_TYPE_CN = {"qinsi_warehouse": "秦丝仓库", "local_physical": "本地物理位置", "transit": "在途位置", "system_status": "系统状态"}
PURCHASE_STATUS_CN = {"confirmed": "已确认", "pending_qinsi_submission": "待提交秦丝", "cancelled": "已取消"}
QINSI_EXPORT_STATUS_CN = {"generated": "已导出待确认", "imported": "全部导入成功", "partially_failed": "部分失败", "failed": "全部失败", "cancelled": "已取消"}
QINSI_EXPORT_TYPE_CN = {"new_product": "新商品导入", "restock": "采购单商品导入"}
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


class FieldExistingScanInput(BaseModel):
    batch_id: int = Field(gt=0)
    jan: str = Field(min_length=1, max_length=32)
    quantity: int = Field(default=1, ge=1, le=999)
    client_request_id: str = Field(min_length=1, max_length=100)
    selected_product_id: int | None = Field(default=None, gt=0)
    review_only: bool = False


class FieldAutoBatchInput(BaseModel):
    client_request_id: str = Field(min_length=1, max_length=100)
    operator_name: str = Field(default="现场采购", min_length=1, max_length=128)


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
templates.env.globals["product_image_url"] = preferred_product_image_url
templates.env.globals["product_display_image"] = product_display_image
templates.env.globals["qinsi_product_export_image_warning"] = qinsi_product_export_image_warning


NAV_PERMISSIONS = {
    "admin": {"field", "purchase", "products", "tasks", "qinsi", "analytics"},
    "buyer": {"field", "purchase", "products", "tasks"},
    "reviewer": {"products", "tasks"},
    "viewer": {"products", "analytics"},
}


def _nav_can(section: str) -> bool:
    return section in NAV_PERMISSIONS.get(default_role(), set())


templates.env.globals["nav_can"] = _nav_can


def _translation_user_message(status: str, error: str | None = None) -> str:
    text = error or ""
    folded = text.casefold()
    if status == "rate_limited" or "限流" in text or "rate" in folded:
        return "中文名生成服务暂时繁忙，请稍后重试"
    if "未配置" in text or "unconfigured" in folded:
        return "中文名生成服务未配置，请到平台配置页检查"
    if "超时" in text or "timeout" in folded:
        return "中文名生成服务超时，请稍后重试"
    if "网络" in text or "network" in folded:
        return "中文名生成服务连接失败，请稍后重试"
    if "缺少可翻译" in text:
        return "缺少可翻译的日文名，请先补全日文名"
    if status == "skipped":
        return text or "当前商品暂不需要生成中文名"
    return "中文名生成失败，请稍后重试或手动编辑"


@app.on_event("startup")
async def start_price_monitor() -> None:
    if is_testing():
        return
    asyncio.create_task(
        asyncio.to_thread(
            process_pending_jobs,
            engine,
            20,
            job_type="FIELD_PRODUCT_ENRICHMENT",
        )
    )
    start_image_localization_worker(engine)
    start_monitor_scheduler()


@app.on_event("shutdown")
async def stop_price_monitor() -> None:
    if not is_testing():
        await asyncio.to_thread(stop_image_localization_worker)
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


def _qinsi_confirmation_validation_message(exc: ValidationError) -> str:
    messages: list[str] = []
    for error in exc.errors():
        loc = ".".join(map(str, error["loc"]))
        if loc == "result":
            messages.append("确认结果只能是 all_success、partial_failure 或 all_failed")
        elif loc == "":
            messages.append("部分失败必须至少选择一条失败行")
        else:
            messages.append(f"{loc}: {error['msg']}")
    return "；".join(messages)


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


def jan_cleanup_suggestion(value: str | None) -> str | None:
    raw = unicodedata.normalize("NFKC", value or "").strip()
    if not raw or raw.isdigit():
        return None
    cleaned = "".join(char for char in raw if char.isdigit())
    return cleaned if cleaned != raw and validate_jan(cleaned) else None


def _receipt_item_snapshot(item: ReceiptItem) -> dict:
    return {
        "raw_name": item.raw_name,
        "recognized_name": item.recognized_name,
        "jan_candidate": item.jan_candidate,
        "product_id": item.product_id,
        "match_status": item.match_status,
        "quantity": item.quantity,
        "unit_price": item.unit_price,
        "discount_amount": item.discount_amount,
        "line_total": item.line_total,
    }


def _mark_qinsi_exports_stale(db: Session, purchase_detail: PurchaseBatchItem | None, reason: str) -> None:
    if purchase_detail is None:
        return
    rows = list(db.scalars(
        select(QinsiPurchaseExportJob)
        .join(PurchaseBatch, PurchaseBatch.id == QinsiPurchaseExportJob.purchase_batch_id)
        .where(
            PurchaseBatch.id == purchase_detail.purchase_batch_id,
            QinsiPurchaseExportJob.status.in_(["generated", "imported", "partially_failed"]),
        )
    ))
    for job in rows:
        note = job.confirmation_note or ""
        marker = f"数据已修改，需要重新导出/重新确认：{reason}"
        if marker not in note:
            job.confirmation_note = f"{note}\n{marker}".strip()
        if job.status == "generated":
            job.purchase_batch.qinsi_status = "not_exported"


def _sync_purchase_detail_from_receipt_item(db: Session, item: ReceiptItem) -> None:
    detail = item.purchase_detail
    if detail is None:
        ensure_purchase_batch_for_receipt(db, item.receipt)
        detail = item.purchase_detail
    if detail is None:
        return
    if item.product_id is not None:
        detail.product_id = item.product_id
    detail.quantity = item.quantity
    detail.unit_price = purchase_unit_price(item.quantity, item.unit_price, item.line_total)
    detail.discount_amount = item.discount_amount
    detail.actual_line_amount = actual_line_amount(item.quantity, item.unit_price, item.discount_amount, item.line_total)


def _normal_receipt_blockers(receipt: Receipt) -> list[str]:
    blockers: list[str] = []
    if receipt.confirmation_status == "confirmed":
        return blockers
    if receipt.duplicate_status in {"auto_duplicate", "review_required", "likely_duplicate"}:
        blockers.append(f"重复状态为 {receipt.duplicate_status}")
    active_items = [item for item in receipt.items if item.review_status != "ignored"]
    if not active_items:
        blockers.append("没有未忽略商品行")
    for item in active_items:
        if item.product_id is None:
            jan = item.jan_candidate or "无JAN"
            blockers.append(f"第{item.line_no}行 JAN {jan} 未解决")
        elif item.match_status in {"invalid_jan", "conflict"}:
            blockers.append(f"第{item.line_no}行 {item.match_status}")
    return blockers


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
    return templates.TemplateResponse(request, "home.html", {
        "recent": recent, "pending": pending, "batch_status_cn": BATCH_STATUS_CN,
        "rakuten_ip_status": rakuten_public_ip_status(),
    })


@app.get("/more", response_class=HTMLResponse)
def more_page(request: Request):
    return templates.TemplateResponse(request, "more.html", {})


@app.get("/platform-config", response_class=HTMLResponse)
def platform_config_page(
    request: Request,
    message: str = Query(""),
    test_status: str = Query(""),
    db: Session = Depends(get_db),
):
    return templates.TemplateResponse(request, "platform_config.html", {
        "rows": provider_status_rows(db),
        "message": message,
        "test_status": test_status,
        "rakuten_ip_status": rakuten_public_ip_status(),
    })


@app.post("/rakuten-ip/recheck")
def rakuten_ip_recheck(return_to: str = Form("/platform-config")):
    status = rakuten_public_ip_status(force=True)
    target = return_to if return_to.startswith("/") and not return_to.startswith("//") else "/platform-config"
    if status.status == "matched":
        message = f"Rakuten出口IP正常：{status.configured_ip}"
    elif status.status == "mismatched":
        message = "Rakuten许可IP与当前公网IP不一致，Rakuten查询可能失败。"
    elif status.status == "not_configured":
        message = "Rakuten许可IP未配置。"
    else:
        message = "Rakuten出口IP检测失败，请稍后重试。"
    separator = "&" if "?" in target else "?"
    return RedirectResponse(f"{target}{separator}message={quote(message)}", status_code=303)


@app.post("/platform-config/{provider_code}/test")
def platform_config_test(provider_code: str, db: Session = Depends(get_db)):
    try:
        response = test_provider_connection(db, provider_code)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    diagnostics = diagnostic_summary(response)
    if response.status == "success":
        message = f"测试成功：返回 {len(response.offers)} 条结果"
    elif response.status == "empty":
        message = "测试完成：认证有效，但该 JAN 暂无结果"
    else:
        message = response.message or "测试连接已完成"
    if diagnostics:
        message = f"{message}；{diagnostics}"
    return RedirectResponse(
        f"/platform-config?test_status={quote(response.status)}&message={quote(message)}",
        status_code=303,
    )


@app.get("/field-purchase", response_class=HTMLResponse)
def field_purchase_page(
    request: Request,
    batch_id: int | None = Query(None),
    db: Session = Depends(get_db),
):
    active_batches = list_active_field_batches(db)
    selected_batch = None
    error = None
    if batch_id is not None:
        try:
            selected_batch = get_field_batch(db, batch_id)
        except LookupError as exc:
            error = str(exc)
    elif active_batches:
        selected_batch = get_field_batch(db, active_batches[0].id)
    stores = list(db.scalars(select(Store).where(Store.is_active.is_(True)).order_by(Store.name)))
    return templates.TemplateResponse(request, "field_purchase.html", {
        "active_batches": active_batches,
        "selected_batch": selected_batch,
        "stores": stores,
        "error": error,
    })


@app.post("/field-purchase/batches")
def field_purchase_create_batch(
    store_id: str = Form(""),
    operator_name: str = Form(...),
    client_request_id: str = Form(...),
    db: Session = Depends(get_db),
):
    try:
        selected_store_id = int(store_id) if store_id.strip() else None
        batch = create_field_batch(
            db,
            store_id=selected_store_id,
            operator_name=operator_name,
            client_request_id=client_request_id,
        )
    except (TypeError, ValueError) as exc:
        db.rollback()
        raise HTTPException(422, str(exc)) from exc
    return RedirectResponse(f"/field-purchase?batch_id={batch.id}", status_code=303)


@app.post("/api/field-purchase/batches/auto", status_code=201)
def api_field_purchase_auto_batch(
    data: FieldAutoBatchInput,
    db: Session = Depends(get_db),
):
    try:
        batch = create_field_batch(
            db,
            store_id=None,
            operator_name=data.operator_name,
            client_request_id=data.client_request_id,
        )
    except ValueError as exc:
        db.rollback()
        raise HTTPException(422, str(exc)) from exc
    return {
        "id": batch.id,
        "batch_no": batch.batch_no,
        "operator_name": batch.operator_name,
        "store_id": None,
    }


@app.post("/field-purchase/batches/{batch_id}/store")
def field_purchase_update_batch_store(
    batch_id: int,
    store_id: str = Form(""),
    return_to: str = Form(""),
    db: Session = Depends(get_db),
):
    try:
        selected_store_id = int(store_id) if store_id.strip() else None
        update_field_batch_store(db, batch_id, store_id=selected_store_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except (TypeError, ValueError) as exc:
        db.rollback()
        raise HTTPException(422, str(exc)) from exc
    safe_return = (
        return_to
        if return_to.startswith("/") and not return_to.startswith("//")
        else f"/field-purchase?batch_id={batch_id}"
    )
    return RedirectResponse(safe_return, status_code=303)


@app.post("/field-purchase/batches/{batch_id}/complete")
def field_purchase_complete_batch(batch_id: int, db: Session = Depends(get_db)):
    try:
        complete_field_batch(db, batch_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return RedirectResponse("/field-purchase", status_code=303)


@app.get("/api/field-purchase/lookup")
def api_field_purchase_lookup(
    code: str = Query(""),
    batch_id: int | None = Query(None),
    request_id: str = Query(""),
    db: Session = Depends(get_db),
):
    jan_hint = (code or "").strip()[:13]
    request_hint = (request_id or "")[-12:]
    started = time.perf_counter()
    logger.info(
        "field_purchase_lookup stage='start' jan=%r batch_id=%r request_suffix=%r endpoint=%r",
        jan_hint, batch_id, request_hint, "/api/field-purchase/lookup",
    )
    try:
        payload = product_lookup_payload(db, code, batch_id)
    except Exception as exc:
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        logger.exception(
            "field_purchase_lookup stage='failed' jan=%r batch_id=%r request_suffix=%r elapsed_ms=%r http_status=500 result_state='error' error=%s",
            jan_hint, batch_id, request_hint, elapsed_ms, type(exc).__name__,
        )
        raise HTTPException(500, "本地商品查询失败，请重试") from exc
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    logger.info(
        "field_purchase_lookup stage='done' jan=%r batch_id=%r request_suffix=%r endpoint=%r elapsed_ms=%r http_status=200 result_state=%r match_source=%r",
        jan_hint, batch_id, request_hint, "/api/field-purchase/lookup", elapsed_ms, payload.get("status"), payload.get("match_source"),
    )
    return payload


@app.post("/api/field-purchase/scans")
def api_field_purchase_existing_scan(
    data: FieldExistingScanInput,
    db: Session = Depends(get_db),
):
    jan_hint = (data.jan or "").strip()[:13]
    request_hint = data.client_request_id[-12:]
    logger.info(
        "field_purchase_scan stage='start' batch_id=%r jan=%r request_suffix=%r review_only=%r",
        data.batch_id, jan_hint, request_hint, data.review_only,
    )
    try:
        if data.review_only:
            payload = record_ambiguous_scan_for_review(
                db,
                batch_id=data.batch_id,
                jan=data.jan,
                client_request_id=data.client_request_id,
                quantity=data.quantity,
            )
        else:
            payload = record_existing_scan(
                db,
                batch_id=data.batch_id,
                jan=data.jan,
                client_request_id=data.client_request_id,
                quantity=data.quantity,
                selected_product_id=data.selected_product_id,
            )
    except LookupError as exc:
        db.rollback()
        logger.warning("field_purchase_scan stage='not_found' batch_id=%r jan=%r request_suffix=%r exc=%s", data.batch_id, jan_hint, request_hint, type(exc).__name__)
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        db.rollback()
        logger.warning("field_purchase_scan stage='invalid' batch_id=%r jan=%r request_suffix=%r exc=%s", data.batch_id, jan_hint, request_hint, type(exc).__name__)
        raise HTTPException(422, str(exc)) from exc
    except Exception as exc:
        db.rollback()
        logger.exception("field_purchase_scan stage='failed' batch_id=%r jan=%r request_suffix=%r exc=%s", data.batch_id, jan_hint, request_hint, type(exc).__name__)
        raise HTTPException(500, "采购事实保存失败，请重试") from exc
    logger.info("field_purchase_scan stage='done' batch_id=%r jan=%r request_suffix=%r item_id=%r status=%r", data.batch_id, jan_hint, request_hint, payload.get("item_id"), payload.get("status"))
    return payload


@app.post("/api/field-purchase/drafts", status_code=201)
async def api_field_purchase_new_draft(
    background_tasks: BackgroundTasks,
    tag_photo: UploadFile = File(...),
    batch_id: int = Form(...),
    client_request_id: str = Form(...),
    jan: str = Form(""),
    temporary_id: str = Form(""),
    name: str = Form(""),
    unit_price: str = Form(""),
    quantity: int = Form(1),
    db: Session = Depends(get_db),
):
    try:
        price = int(unit_price) if unit_price.strip() else None
    except ValueError as exc:
        raise HTTPException(422, "价格必须为整数日元") from exc
    content = await tag_photo.read()
    jan_hint = (jan or "").strip()[:13]
    request_hint = client_request_id[-12:]
    logger.info(
        "field_purchase_draft stage='start' batch_id=%r jan=%r temporary_id=%r request_suffix=%r bytes=%r content_type=%r",
        batch_id, jan_hint, (temporary_id or "")[:24], request_hint, len(content), (tag_photo.content_type or "")[:60],
    )
    try:
        payload, job = create_new_product_draft(
            db,
            batch_id=batch_id,
            client_request_id=client_request_id,
            photo_content=content,
            photo_content_type=(tag_photo.content_type or "application/octet-stream").casefold(),
            photo_filename=tag_photo.filename or "tag-photo",
            jan=jan,
            temporary_id=temporary_id,
            name=name,
            unit_price=price,
            quantity=quantity,
        )
    except LookupError as exc:
        db.rollback()
        logger.warning("field_purchase_draft stage='not_found' batch_id=%r jan=%r request_suffix=%r exc=%s", batch_id, jan_hint, request_hint, type(exc).__name__)
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        db.rollback()
        logger.warning("field_purchase_draft stage='invalid' batch_id=%r jan=%r request_suffix=%r exc=%s", batch_id, jan_hint, request_hint, type(exc).__name__)
        raise HTTPException(422, str(exc)) from exc
    except Exception as exc:
        db.rollback()
        logger.exception("field_purchase_draft stage='failed' batch_id=%r jan=%r request_suffix=%r exc=%s", batch_id, jan_hint, request_hint, type(exc).__name__)
        raise HTTPException(500, "吊牌服务器同步失败，请稍后重试") from exc
    if not payload.get("replayed"):
        background_tasks.add_task(process_durable_job, job.id, db.get_bind())
    logger.info("field_purchase_draft stage='done' batch_id=%r jan=%r request_suffix=%r item_id=%r job_id=%r replayed=%r", batch_id, jan_hint, request_hint, payload.get("item_id"), job.id, payload.get("replayed"))
    return payload


@app.post("/api/background-jobs/run-pending", status_code=202)
def api_run_pending_jobs(background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    pending = db.scalar(
        select(func.count())
        .select_from(DurableBackgroundJob)
        .where(DurableBackgroundJob.status.in_({"PENDING", "FAILED_RETRYABLE"}))
    ) or 0
    background_tasks.add_task(process_pending_jobs, db.get_bind(), 50)
    return {"status": "accepted", "pending": pending}


@app.get("/field-purchase/tag-evidence/{evidence_id}")
def field_purchase_tag_evidence(evidence_id: int, db: Session = Depends(get_db)):
    evidence = db.get(TagEvidence, evidence_id)
    if evidence is None:
        raise HTTPException(404, "吊牌证据不存在")
    path = (PROJECT_ROOT / evidence.file_path).resolve()
    allowed = TAG_EVIDENCE_DIR.resolve()
    if not path.is_relative_to(allowed) or not path.is_file():
        raise HTTPException(404, "吊牌证据文件不存在")
    return FileResponse(path, media_type=evidence.content_type, filename=evidence.original_filename)


@app.get("/receipts/upload", response_class=HTMLResponse)
def upload_page(request: Request):
    return templates.TemplateResponse(request, "upload.html", {})


@app.get("/price-check", response_class=HTMLResponse)
def price_check_page(
    request: Request,
    background_tasks: BackgroundTasks,
    jan: str = Query(""),
    refresh: int = Query(1),
    auto: bool = Query(True),
    db: Session = Depends(get_db),
):
    if jan.strip() and auto:
        try:
            force_refresh = refresh != 0
            lookup = PriceLookupInput.model_validate({"jan": jan, "current_store_price": None, "force_refresh": force_refresh})
            local = resolve_local_product_by_jan(db, lookup.jan)
            providers = [] if local.product is not None and not lookup.force_refresh else None
            view = query_prices(db, lookup, providers=providers, trigger_enrichment=False)
            if local.product is None or lookup.force_refresh or product_needs_jan_completion(local.product):
                database_url = db.get_bind().url.render_as_string(hide_password=False)
                background_tasks.add_task(process_price_lookup_enrichment, database_url, lookup.jan, view.history.id)
            return RedirectResponse(f"/price-check/results/{view.history.id}", status_code=303)
        except (ValidationError, ValueError):
            pass
    return templates.TemplateResponse(request, "price_check.html", {
        "histories": recent_price_lookup_histories(db, jan=jan.strip() or None),
        "error": None, "jan": jan.strip(), "current_store_price": "",
    })


@app.post("/price-check", response_class=HTMLResponse)
def price_check_submit(
    request: Request, background_tasks: BackgroundTasks,
    jan: str = Form(...), current_store_price: str = Form(""),
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
    try:
        local = resolve_local_product_by_jan(db, lookup.jan)
        providers = [] if local.product is not None and not lookup.force_refresh else None
        view = query_prices(db, lookup, providers=providers, trigger_enrichment=False)
    except ValueError as exc:
        return templates.TemplateResponse(request, "price_check.html", {
            "histories": recent_price_lookup_histories(db), "error": str(exc),
            "jan": jan, "current_store_price": current_store_price,
        }, status_code=409)
    if local.product is None or product_needs_jan_completion(local.product):
        database_url = db.get_bind().url.render_as_string(hide_password=False)
        background_tasks.add_task(process_price_lookup_enrichment, database_url, lookup.jan, view.history.id)
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


def _receipt_item_total(item: ReceiptItem) -> int:
    if item.line_total is not None:
        return int(item.line_total)
    if item.unit_price is None:
        return 0
    return int(item.unit_price) * int(item.quantity or 0) - int(item.discount_amount or 0)


def _receipt_summary(receipt: Receipt) -> dict:
    rows: dict[str, dict] = {}
    for item in receipt.items:
        if item.review_status == "ignored":
            continue
        jan = item.jan_candidate or (item.product.jan if item.product else None)
        key = jan or f"NO-JAN-{item.id}"
        row = rows.setdefault(key, {
            "jan": jan or "无JAN",
            "name": item.raw_name,
            "quantity": 0,
            "amount": 0,
        })
        row["quantity"] += item.quantity or 0
        row["amount"] += _receipt_item_total(item)
    return {
        "receipt": receipt,
        "rows": list(rows.values()),
        "kind_count": len(rows),
        "quantity": sum(row["quantity"] for row in rows.values()),
        "amount": sum(row["amount"] for row in rows.values()),
    }


def _batch_receipt_summary(receipt_summaries: list[dict]) -> dict:
    rows: dict[str, dict] = {}
    for summary in receipt_summaries:
        for item in summary["rows"]:
            key = item["jan"] if item["jan"] != "无JAN" else f"NO-JAN-{summary['receipt'].id}-{item['name']}"
            row = rows.setdefault(key, {"jan": item["jan"], "name": item["name"], "quantity": 0, "amount": 0})
            row["quantity"] += item["quantity"]
            row["amount"] += item["amount"]
    return {
        "rows": list(rows.values()),
        "kind_count": len(rows),
        "quantity": sum(row["quantity"] for row in rows.values()),
        "amount": sum(row["amount"] for row in rows.values()),
    }


@app.get("/receipts/{batch_id}", response_class=HTMLResponse)
def receipt_detail(batch_id: int, request: Request, db: Session = Depends(get_db)):
    batch = load_batch(db, batch_id)
    receipt = batch.receipts[0] if batch.receipts else None
    master = db.get(Receipt, receipt.duplicate_of_receipt_id) if receipt and receipt.duplicate_of_receipt_id else None
    linked = list(db.scalars(select(Receipt).where(Receipt.duplicate_of_receipt_id == receipt.id))) if receipt else []
    gpt_jobs = list(db.scalars(
        select(ZipPackageJob).join(ZipPackageItem).where(ZipPackageItem.batch_id == batch_id).order_by(ZipPackageJob.created_at.desc())
    ).unique())
    receipt_summaries = [_receipt_summary(receipt) for receipt in batch.receipts]
    batch_summary = _batch_receipt_summary(receipt_summaries)
    return templates.TemplateResponse(request, "detail.html", {
        "batch": batch, "error": None, "payload": None, "rotated": request.query_params.get("rotated"),
        "duplicate_master": master, "duplicate_links": linked, "gpt_jobs": gpt_jobs,
        "receipt_summaries": receipt_summaries, "batch_summary": batch_summary,
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
    for candidate_receipt in batch.receipts:
        refresh_resolved_conflicts(db, candidate_receipt)
    receipt = current_receipt(batch, receipt_id)
    products = list(db.scalars(
        select(Product).where(Product.status == "active").order_by(Product.name_cn, Product.id).limit(500)
    ))
    candidate_products_by_item = {}
    selected_product_ids = {product.id for product in products}
    for item in receipt.items:
        candidates = candidate_products_for_jan(db, item.jan_candidate)
        if candidates:
            candidate_products_by_item[item.id] = candidates
        for product in candidates:
            if product.id not in selected_product_ids:
                products.append(product)
                selected_product_ids.add(product.id)
    product_by_id = {product.id: product for product in products}
    for item in receipt.items:
        if item.product_id and item.product_id not in product_by_id:
            product = db.get(Product, item.product_id)
            if product is not None:
                product_by_id[item.product_id] = product
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
    purchase_batch = db.scalar(
        select(PurchaseBatch)
        .where(PurchaseBatch.receipt_id == receipt.id, PurchaseBatch.status != "cancelled")
        .order_by(PurchaseBatch.id)
    )
    purchase_batches_by_receipt = {
        purchase.receipt_id: purchase
        for purchase in db.scalars(
            select(PurchaseBatch)
            .where(PurchaseBatch.gpt_batch_id == batch.id, PurchaseBatch.status != "cancelled")
            .order_by(PurchaseBatch.id)
        )
    }
    purchase_blockers_by_receipt = {
        candidate.id: purchase_batch_blockers_for_receipt(db, candidate)
        for candidate in batch.receipts
        if candidate.confirmation_status == "confirmed" and candidate.id not in purchase_batches_by_receipt
    }
    jan_suggestions_by_item = {
        item.id: jan_cleanup_suggestion(item.jan_candidate)
        for candidate in batch.receipts
        for item in candidate.items
    }
    stores = list(db.scalars(select(Store).where(Store.is_active.is_(True)).order_by(Store.name_cn, Store.name_ja, Store.id)))
    return templates.TemplateResponse(request, "review.html", {
        "batch": batch, "receipt": receipt, "warnings": amount_warnings(receipt), "error": None,
        "products": products, "product_by_id": product_by_id, "recommendations": recommendations,
        "fuzzy_candidates": fuzzy_candidates, "candidate_products_by_item": candidate_products_by_item,
        "physical_locations": physical_locations, "qinsi_warehouses": qinsi_warehouses,
        "default_physical": default_physical, "purchase_batch": purchase_batch,
        "purchase_batches_by_receipt": purchase_batches_by_receipt,
        "purchase_blockers_by_receipt": purchase_blockers_by_receipt,
        "stores": stores,
        "jan_suggestions_by_item": jan_suggestions_by_item,
        "receipt_status_cn": RECEIPT_STATUS_CN, "item_status_cn": ITEM_STATUS_CN, "match_status_cn": MATCH_STATUS_CN,
        "enrichment_summary": enrichment_summary_for_receipt(db, receipt.id),
        "product_display_label": product_display_label,
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
    item = next((row for row in receipt.items if row.id == item_id), None)
    if not item:
        raise HTTPException(404, "商品行不存在")
    try:
        data = await parse_item_form(request)
    except ValidationError as exc:
        raise HTTPException(422, _validation_message(exc)) from exc
    if receipt.confirmation_status == "confirmed":
        before = _receipt_item_snapshot(item)
        suggested = jan_cleanup_suggestion(data.jan_candidate)
        if suggested:
            data.jan_candidate = suggested
        data.review_status = "confirmed"
        apply_item_draft(item, data)
        item.jan_candidate = normalize_jan(item.jan_candidate)
        match_item(db, item, force=True)
        _sync_purchase_detail_from_receipt_item(db, item)
        _mark_qinsi_exports_stale(db, item.purchase_detail, f"小票行 {item.id} 纠错")
        db.add(DuplicateDetectionLog(
            entity_type="receipt_item",
            new_entity_id=item.id,
            matched_entity_id=item.product_id,
            algorithm_version="manual-correction-v1",
            sha_match=False,
            decision="correction",
            reason=json.dumps({"before": before, "after": _receipt_item_snapshot(item)}, ensure_ascii=False, default=str),
        ))
        db.commit()
        safe_trigger_receipt_items(db, [item], "confirmed_item_correction")
        return RedirectResponse(_review_url(batch_id, receipt) + f"#receipt-item-{item.id}", status_code=303)
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


@app.post("/receipts/{batch_id}/review/confirm-normal")
def confirm_normal_receipts(batch_id: int, db: Session = Depends(get_db)):
    batch = load_batch(db, batch_id)
    confirmed = 0
    skipped: list[str] = []
    settings = PurchaseConfirmationInput(initial_location_id=get_default_physical_location(db).id)
    for index, receipt in enumerate(batch.receipts, start=1):
        if receipt.confirmation_status == "confirmed":
            continue
        blockers = _normal_receipt_blockers(receipt)
        if blockers:
            skipped.append(f"第{index}张 {'；'.join(blockers)}")
            continue
        try:
            confirm_receipt(db, batch, receipt, settings)
            confirmed += 1
        except ValueError as exc:
            db.rollback()
            batch = load_batch(db, batch_id)
            skipped.append(f"第{index}张 {exc}")
    message = f"已确认 {confirmed} 张，跳过 {len(skipped)} 张"
    if skipped:
        message = message + "：" + "；".join(skipped[:8])
    return RedirectResponse(f"/receipts/{batch_id}/review?message={quote(message)}", status_code=303)


@app.post("/receipts/{batch_id}/review/receipts/{receipt_id}/not-duplicate")
def mark_receipt_not_duplicate(batch_id: int, receipt_id: int, db: Session = Depends(get_db)):
    batch = load_batch(db, batch_id)
    receipt = next((candidate for candidate in batch.receipts if candidate.id == receipt_id), None)
    if receipt is None:
        raise HTTPException(404, "小票不存在")
    before = {
        "duplicate_status": receipt.duplicate_status,
        "duplicate_of_receipt_id": receipt.duplicate_of_receipt_id,
        "duplicate_reason": receipt.duplicate_reason,
    }
    receipt.duplicate_status = "distinct"
    receipt.duplicate_of_receipt_id = None
    receipt.duplicate_score = None
    receipt.duplicate_reason = "人工确认不是重复"
    db.add(DuplicateDetectionLog(
        entity_type="receipt",
        new_entity_id=receipt.id,
        matched_entity_id=before["duplicate_of_receipt_id"],
        algorithm_version="manual-duplicate-clear-v1",
        sha_match=False,
        decision="manual_distinct",
        reason=json.dumps({"before": before, "after": {"duplicate_status": "distinct"}}, ensure_ascii=False, default=str),
    ))
    if receipt.confirmation_status == "confirmed":
        refresh_resolved_conflicts(db, receipt, commit=False)
        ensure_purchase_batch_for_receipt(db, receipt)
    else:
        match_receipt(db, receipt, force=True, commit=False)
    db.commit()
    safe_trigger_receipt_items(db, list(receipt.items), "manual_not_duplicate")
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
    if receipt.confirmation_status == "confirmed":
        refresh_resolved_conflicts(db, receipt)
        ensure_purchase_batch_for_receipt(db, receipt)
        db.commit()
    else:
        match_receipt(db, receipt, force=rematch)
    return RedirectResponse(_review_url(batch_id, receipt), status_code=303)


@app.post("/receipts/{batch_id}/match")
def match_whole_batch(batch_id: int, rematch: bool = Form(False), db: Session = Depends(get_db)):
    batch = load_batch(db, batch_id)
    if batch.status == "confirmed" or any(receipt.confirmation_status == "confirmed" for receipt in batch.receipts):
        for receipt in batch.receipts:
            refresh_resolved_conflicts(db, receipt)
            ensure_purchase_batch_for_receipt(db, receipt)
        db.commit()
    else:
        match_batch(db, batch, force=rematch)
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
    jobs = list(db.scalars(select(QinsiImportBatch).order_by(QinsiImportBatch.created_at.desc()).limit(20)))
    job = db.get(QinsiImportBatch, job_id) if job_id else (jobs[0] if jobs else None)
    rows = list(db.scalars(select(QinsiGoodsImportRow).where(
        QinsiGoodsImportRow.import_batch_id == job.id,
        QinsiGoodsImportRow.validation_status != "skipped",
    ).order_by(QinsiGoodsImportRow.excel_row_number))) if job else []
    for row in rows:
        raw = json.loads(row.raw_json)
        row.display_raw_json = json.dumps(raw, ensure_ascii=False, indent=2)
        row.display_conflicts = json.loads(row.conflict_json) if row.conflict_json else []
        row.display_warnings = json.loads(row.warnings) if row.warnings else []
        row.display_errors = json.loads(row.errors) if row.errors else []
    summary = json.loads(job.summary_json) if job and job.summary_json else {}
    return templates.TemplateResponse(request, "product_import.html", {
        "jobs": jobs, "job": job, "rows": rows, "summary": summary,
        "status_cn": IMPORT_STATUS_CN,
    })


@app.post("/products/import/preview")
async def product_import_preview(
    background_tasks: BackgroundTasks,
    file: UploadFile | None = File(None),
    use_reference: str | None = Form(None),
    business_batch_key: str | None = Form(None),
    db: Session = Depends(get_db),
):
    reference_name = "goodsImportTemplate已有商品模版-可到导入到本地数据库.xlsx"
    if file and file.filename:
        filename, content = Path(file.filename).name, await file.read()
    elif use_reference == reference_name:
        filename = reference_name
        content = (PROJECT_ROOT / "reference" / "qinsi" / reference_name).read_bytes()
    else:
        raise HTTPException(422, "请选择Excel文件或项目内已有商品模板")
    try:
        job, reused = create_import_batch(
            db, filename, content, business_batch_key=business_batch_key,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if not reused or job.status in {"failed", "queued"}:
        database_url = db.get_bind().url.render_as_string(hide_password=False)
        background_tasks.add_task(process_queued_import, database_url, job.id)
    return RedirectResponse(f"/products/import?job_id={job.id}", status_code=303)


@app.get("/products/import/{job_id}/status")
def product_import_status(job_id: int, db: Session = Depends(get_db)):
    job = db.get(QinsiImportBatch, job_id)
    if not job:
        raise HTTPException(404, "导入任务不存在")
    return {
        "id": job.id,
        "status": job.status,
        "status_label": IMPORT_STATUS_CN.get(job.status, job.status),
        "total_rows": job.total_rows,
        "new_count": job.new_count,
        "update_count": job.update_count,
        "unchanged_count": job.unchanged_count,
        "skipped_count": job.skipped_count,
        "conflict_count": job.conflict_count,
        "error_count": job.error_count,
        "warning_count": job.warning_count,
        "error_message": job.error_message,
    }


@app.post("/products/import/{job_id}/confirm")
def product_import_confirm(
    job_id: int,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    job = db.get(QinsiImportBatch, job_id)
    if not job:
        raise HTTPException(404, "导入任务不存在")
    try:
        queue_import_confirmation(db, job)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    database_url = db.get_bind().url.render_as_string(hide_password=False)
    background_tasks.add_task(process_queued_confirmation, database_url, job.id)
    return RedirectResponse(f"/products/import?job_id={job.id}", status_code=303)


@app.get("/products/qinsi-master-import", response_class=HTMLResponse)
def qinsi_product_master_import_page(
    request: Request,
    job_id: int | None = Query(None),
    db: Session = Depends(get_db),
):
    jobs = list(db.scalars(
        select(QinsiImportBatch)
        .where(QinsiImportBatch.source_system == MASTER_SOURCE)
        .order_by(QinsiImportBatch.created_at.desc())
        .limit(20)
    ))
    job = db.get(QinsiImportBatch, job_id) if job_id else (jobs[0] if jobs else None)
    if job is not None and job.source_system != MASTER_SOURCE:
        raise HTTPException(404, "秦丝商品主数据导入任务不存在")
    rows = list(db.scalars(select(QinsiGoodsImportRow).where(
        QinsiGoodsImportRow.import_batch_id == job.id,
    ).order_by(QinsiGoodsImportRow.excel_row_number).limit(200))) if job else []
    for row in rows:
        parsed = json.loads(row.parsed_data)
        raw = json.loads(row.raw_json)
        row.display_raw_json = json.dumps(raw, ensure_ascii=False, indent=2)
        row.display_name = parsed.get("qinsi_name")
        row.display_goods_no = parsed.get("qinsi_goods_no")
        row.display_jan = parsed.get("jan")
        row.display_jan_source = parsed.get("jan_source")
        row.display_has_jan = parsed.get("has_jan")
        row.display_conflicts = json.loads(row.conflict_json) if row.conflict_json else []
        row.display_candidate_products = []
        for conflict in row.display_conflicts:
            existing = conflict.get("existing_value")
            if isinstance(existing, list):
                row.display_candidate_products.extend(item for item in existing if isinstance(item, dict))
            elif isinstance(existing, dict):
                row.display_candidate_products.append(existing)
        row.display_warnings = json.loads(row.warnings) if row.warnings else []
        row.display_errors = json.loads(row.errors) if row.errors else []
    summary = qinsi_master_preview_statistics(db, job) if job else {}
    return templates.TemplateResponse(request, "qinsi_product_master_import.html", {
        "jobs": jobs, "job": job, "rows": rows, "summary": summary,
        "status_cn": IMPORT_STATUS_CN,
        "resolution_types": QINSI_CONFLICT_RESOLUTION_TYPES,
    })


@app.post("/products/qinsi-master-import/preview")
async def qinsi_product_master_import_preview(
    files: list[UploadFile] = File(...),
    business_batch_key: str | None = Form(None),
    db: Session = Depends(get_db),
):
    selected: list[MasterInputFile] = []
    for file in files:
        if not file.filename:
            continue
        selected.append(MasterInputFile(filename=Path(file.filename).name, content=await file.read()))
    if not selected:
        raise HTTPException(422, "请上传至少一个秦丝商品列表Excel")
    try:
        job = create_qinsi_master_preview(db, selected, business_batch_key=business_batch_key)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return RedirectResponse(f"/products/qinsi-master-import?job_id={job.id}", status_code=303)


@app.get("/products/qinsi-master-import/{job_id}/status")
def qinsi_product_master_import_status(job_id: int, db: Session = Depends(get_db)):
    job = db.get(QinsiImportBatch, job_id)
    if not job or job.source_system != MASTER_SOURCE:
        raise HTTPException(404, "秦丝商品主数据导入任务不存在")
    summary = qinsi_master_preview_statistics(db, job)
    return {
        "id": job.id,
        "status": job.status,
        "status_label": IMPORT_STATUS_CN.get(job.status, job.status),
        "total_rows": job.total_rows,
        "jan_from_unit_barcode": summary.get("jan_from_unit_barcode", 0),
        "jan_from_product_barcode": summary.get("jan_from_product_barcode", 0),
        "jan_from_goods_no": summary.get("jan_from_goods_no", 0),
        "no_jan_count": summary.get("no_jan_count", 0),
        "same_jan_multi_group_count": summary.get("same_jan_multi_group_count", 0),
        "same_jan_multi_product_count": summary.get("same_jan_multi_product_count", 0),
        "local_exists_count": summary.get("local_exists_count", 0),
        "jan_correction_count": summary.get("jan_correction_count", 0),
        "new_count": job.new_count,
        "update_count": job.update_count,
        "unchanged_count": job.unchanged_count,
        "conflict_count": job.conflict_count,
        "error_count": job.error_count,
        "mark_imported_count": summary.get("mark_imported_count", job.success_count),
        "error_message": job.error_message,
    }


@app.post("/products/qinsi-master-import/{job_id}/confirm")
def qinsi_product_master_import_confirm(job_id: int, db: Session = Depends(get_db)):
    job = db.get(QinsiImportBatch, job_id)
    if not job or job.source_system != MASTER_SOURCE:
        raise HTTPException(404, "秦丝商品主数据导入任务不存在")
    try:
        confirm_qinsi_master_import(db, job)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return RedirectResponse(f"/products/qinsi-master-import?job_id={job.id}", status_code=303)


@app.post("/products/qinsi-master-import/{job_id}/rows/{row_id}/resolve-conflict")
def qinsi_product_master_import_resolve_conflict(
    job_id: int,
    row_id: int,
    resolution_type: str = Form(...),
    action: str = Form(""),
    note: str = Form(""),
    auto_apply: bool = Form(False),
    primary_product_id: int | None = Form(None),
    db: Session = Depends(get_db),
):
    job = db.get(QinsiImportBatch, job_id)
    if not job or job.source_system != MASTER_SOURCE:
        raise HTTPException(404, "秦丝商品主数据导入任务不存在")
    try:
        resolve_qinsi_master_conflict(
            db,
            job,
            row_id,
            resolution_type=resolution_type,
            action=action or None,
            note=note,
            auto_apply=auto_apply,
            primary_product_id=primary_product_id,
        )
    except LookupError as exc:
        db.rollback()
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        db.rollback()
        return RedirectResponse(f"/products/qinsi-master-import?job_id={job.id}&error={quote(str(exc))}", status_code=303)
    return RedirectResponse(f"/products/qinsi-master-import?job_id={job.id}&message={quote('冲突处理已保存')}", status_code=303)


@app.get("/products/qinsi-master-import/{job_id}/audit.xlsx")
def qinsi_product_master_import_audit(job_id: int, db: Session = Depends(get_db)):
    job = db.get(QinsiImportBatch, job_id)
    if not job or job.source_system != MASTER_SOURCE:
        raise HTTPException(404, "秦丝商品主数据导入任务不存在")
    content = export_qinsi_master_audit_workbook(db, job)
    filename = quote(f"qinsi-product-master-audit-{job.id}.xlsx")
    return StreamingResponse(
        io.BytesIO(content),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{filename}"},
    )


@app.post("/products/match-by-date")
def product_match_by_date(purchased_date: date = Form(...), rematch: bool = Form(False), db: Session = Depends(get_db)):
    match_date(db, purchased_date, force=rematch)
    return RedirectResponse(f"/products?matched_date={purchased_date.isoformat()}", status_code=303)


@app.get("/products", response_class=HTMLResponse)
def products_page(
    request: Request, q: str = Query(""), page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=20, le=200), status: str = Query(""), db: Session = Depends(get_db),
):
    focused_qinsi_product_ids = {
        int(value)
        for raw in request.query_params.getlist("qinsi_product_ids")
        for value in raw.split(",")
        if value.isdigit()
    }
    query = select(
        Product,
        func.count(ReceiptItem.id),
        func.coalesce(func.sum(ReceiptItem.quantity), 0),
        func.max(Receipt.purchased_at),
    ).outerjoin(ReceiptItem, ReceiptItem.product_id == Product.id).outerjoin(Receipt, Receipt.id == ReceiptItem.receipt_id)
    filters = []
    if q.strip():
        value = f"%{q.strip()}%"
        filters.append(or_(
            Product.internal_sku.like(value), Product.jan.like(value),
            Product.qinsi_product_code.like(value), Product.name_cn.like(value),
            Product.name_ja.like(value), Product.display_name.like(value),
        ))
    if focused_qinsi_product_ids:
        filters.append(Product.id.in_(focused_qinsi_product_ids))
    product_status_labels = {
        "active": "普通商品",
        "new_pending_completion": "新商品待补全",
        "new_pending_review": "新商品待人工确认",
        "pending_qinsi_product_import": "待导入秦丝商品库",
        "qinsi_product_imported": "已导入秦丝商品库",
        "archived": "已停用/归档",
    }
    if status.strip():
        filters.append(Product.status == status.strip())
    else:
        filters.append(Product.status != "archived")
    if filters:
        query = query.where(*filters)
    total = db.scalar(select(func.count(Product.id)).where(*filters)) or 0
    raw_rows = db.execute(
        query.group_by(Product.id).order_by(Product.updated_at.desc(), Product.id.desc())
        .offset((page - 1) * page_size).limit(page_size)
    ).all()
    inventories = latest_inventory_for_products(db, [row[0].id for row in raw_rows])
    rows = [(product, count, quantity, latest, inventories[product.id]) for product, count, quantity, latest in raw_rows]
    association_by_id = {product.id: product_associations(db, product) for product, _, _, _, _ in rows}
    row_product_ids = {product.id for product, _, _, _, _ in rows}
    confirmed_imported_ids = confirmed_qinsi_product_import_product_ids(db, row_product_ids)
    qinsi_product_exportable_product_ids = {
        product.id for product, _, _, _, _ in rows
        if qinsi_product_is_exportable(product, confirmed_imported_ids)
    }
    page_count = max(1, (total + page_size - 1) // page_size)
    pending_export_ids = {
        int(value)
        for raw in request.query_params.getlist("pending_export_ids")
        for value in raw.split(",")
        if value.isdigit()
    }
    pending_export_links = [
        job for job in list_qinsi_product_export_jobs(db)
        if job.id in pending_export_ids and job.status == "exported"
    ]
    remaining_product_ids = [
        int(value)
        for raw in request.query_params.getlist("remaining_product_ids")
        for value in raw.split(",")
        if value.isdigit()
    ]
    return templates.TemplateResponse(request, "products.html", {
        "rows": rows, "q": q, "page": page, "page_size": page_size,
        "page_count": page_count, "total": total,
        "matched_date": request.query_params.get("matched_date"),
        "status": status, "product_status_labels": product_status_labels,
        "editable_product_statuses": EDITABLE_PRODUCT_STATUSES,
        "association_by_id": association_by_id,
        "qinsi_product_exportable_statuses": QINSI_PRODUCT_EXPORTABLE_STATUSES,
        "qinsi_product_exportable_product_ids": qinsi_product_exportable_product_ids,
        "qinsi_product_exports": list_qinsi_product_export_jobs(db)[:10],
        "pending_export_links": pending_export_links,
        "remaining_product_ids": remaining_product_ids,
        "message": request.query_params.get("message"),
        "error": request.query_params.get("error"),
        "missing_chinese_name_count": count_missing_chinese_name_products(db),
        "product_display_label": product_display_label,
        "is_missing_chinese_name": is_missing_chinese_name,
    })


@app.post("/products/{product_id}/generate-chinese-name")
def product_generate_chinese_name(
    product_id: int,
    return_to: str = Form(""),
    confirm_overwrite: bool = Form(False),
    db: Session = Depends(get_db),
):
    product = db.get(Product, product_id)
    if product is None:
        raise HTTPException(404, "商品不存在")
    if product.name_cn and not is_missing_chinese_name(product) and not confirm_overwrite:
        result_message = "已有中文名，请二次确认后覆盖"
        target = return_to if return_to.startswith("/") and not return_to.startswith("//") else f"/products/{product_id}"
        separator = "&" if "?" in target else "?"
        return RedirectResponse(f"{target}{separator}error={quote(result_message)}", status_code=303)
    result = translate_product_chinese_name(db, product, force=True, overwrite_existing=confirm_overwrite)
    if result.status == "success":
        db.commit()
        message = "已生成中文名"
    else:
        db.commit()
        message = _translation_user_message(result.status, result.error)
    target = return_to if return_to.startswith("/") and not return_to.startswith("//") else f"/products/{product_id}"
    separator = "&" if "?" in target else "?"
    key = "message" if result.status == "success" else "error"
    return RedirectResponse(f"{target}{separator}{key}={quote(message)}", status_code=303)


@app.post("/products/generate-chinese-names")
async def products_generate_chinese_names(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    selected = {int(value) for value in form.getlist("product_ids") if str(value).isdigit()}
    result = translate_missing_chinese_names(db, product_ids=selected or None, force=True)
    text = (
        f"候选{result.candidate_count}，成功{result.success_count}，"
        f"失败{result.failed_count}，跳过{result.skipped_count}"
    )
    if result.stopped_reason:
        text = f"{text}；已停止：{_translation_user_message('rate_limited', result.stopped_reason)}"
    return RedirectResponse(f"/products?message={quote(text)}", status_code=303)


@app.post("/products/{product_id}/enrich-by-jan")
def product_enrich_by_jan(
    product_id: int,
    return_to: str = Form(""),
    db: Session = Depends(get_db),
):
    product = db.get(Product, product_id)
    if product is None:
        raise HTTPException(404, "商品不存在")
    target = return_to if return_to.startswith("/") and not return_to.startswith("//") else f"/products/{product_id}"
    separator = "&" if "?" in target else "?"
    task = ensure_existing_product_enrichment_task(db, product)
    if task is None:
        return RedirectResponse(f"{target}{separator}error={quote('只有有合法JAN的缺资料商品可以按JAN补全')}", status_code=303)
    process_enrichment_task(db, task, force=True)
    db.refresh(product)
    if product.status == "new_pending_completion":
        return RedirectResponse(f"{target}{separator}error={quote('按JAN补全失败，已保留缺资料商品，可重试')}", status_code=303)
    return RedirectResponse(f"{target}{separator}message={quote('已按JAN补全商品资料')}", status_code=303)


@app.post("/products/qinsi-new-exports")
async def products_qinsi_new_export(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    product_ids = {int(value) for value in form.getlist("product_ids") if str(value).isdigit()}
    confirm_remaining = str(form.get("confirm_remaining") or "").casefold() in {"1", "true", "yes", "on"}
    pending = pending_qinsi_product_exports(db, product_ids)
    pending_product_ids = {item.product_id for item in pending}
    if pending and not confirm_remaining:
        job_ids = sorted({item.job_id for item in pending})
        if pending_product_ids == product_ids and len(job_ids) == 1:
            return RedirectResponse(f"/qinsi-product-exports/{job_ids[0]}", status_code=303)
        labels = "、".join(item.label for item in pending)
        params = f"pending_export_ids={quote(','.join(str(item) for item in job_ids))}"
        if pending_product_ids != product_ids:
            remaining_ids = sorted(product_ids - pending_product_ids)
            params = (
                f"{params}&remaining_product_ids={quote(','.join(str(item) for item in remaining_ids))}"
                f"&error={quote('部分商品已有待确认的新商品导出：' + labels + '。请确认是否仅生成剩余商品。')}"
            )
        else:
            params = f"{params}&error={quote('所选商品分别已有待确认的新商品导出：' + labels)}"
        return RedirectResponse(f"/products?{params}", status_code=303)
    if confirm_remaining:
        product_ids = product_ids - pending_product_ids
    try:
        job = create_qinsi_product_export(db, product_ids)
    except ValueError as exc:
        db.rollback()
        return RedirectResponse(f"/products?error={quote(str(exc))}", status_code=303)
    return RedirectResponse(f"/qinsi-product-exports/{job.id}", status_code=303)


@app.get("/products/placeholder-cleanup", response_class=HTMLResponse)
def product_placeholder_cleanup_page(request: Request, db: Session = Depends(get_db)):
    preview = preview_placeholder_cleanup(db)
    action_labels = {
        "delete_unlinked": "直接删除临时商品",
        "migrate_and_delete": "迁移采购关联后删除",
        "manual_multiple_formal": "人工选择：同JAN多个正式秦丝商品",
        "manual_blocking_links": "人工处理：存在非采购业务关联",
        "keep_no_formal": "保留：无正式秦丝商品",
    }
    return templates.TemplateResponse(request, "product_placeholder_cleanup.html", {
        "preview": preview,
        "stats": preview.stats,
        "action_labels": action_labels,
        "message": request.query_params.get("message"),
        "error": request.query_params.get("error"),
    })


@app.get("/products/duplicate-jans", response_class=HTMLResponse)
def product_duplicate_jans_page(request: Request, db: Session = Depends(get_db)):
    groups = list_duplicate_jan_groups(db)
    return templates.TemplateResponse(request, "duplicate_jans.html", {
        "groups": groups,
        "message": request.query_params.get("message"),
        "error": request.query_params.get("error"),
    })


@app.post("/products/duplicate-jans/{jan}/merge")
def product_duplicate_jan_merge(
    jan: str,
    primary_product_id: int = Form(...),
    actor: str = Form("system"),
    db: Session = Depends(get_db),
):
    try:
        result = merge_duplicate_jan_group(db, jan, primary_product_id=primary_product_id, actor=actor)
    except ValueError as exc:
        db.rollback()
        return RedirectResponse(f"/products/duplicate-jans?error={quote(str(exc))}", status_code=303)
    if result is None:
        message = f"JAN {jan} 当前没有重复商品"
    else:
        message = f"JAN {jan} 已合并 {len(result.merged_product_ids)} 个重复商品，迁移关联 {result.migrated_association_count} 条"
    return RedirectResponse(f"/products/duplicate-jans?message={quote(message)}", status_code=303)


@app.post("/products/duplicate-jans/merge-all")
def product_duplicate_jans_merge_all(actor: str = Form("system"), db: Session = Depends(get_db)):
    try:
        results = merge_all_duplicate_jans(db, actor=actor)
    except ValueError as exc:
        db.rollback()
        return RedirectResponse(f"/products/duplicate-jans?error={quote(str(exc))}", status_code=303)
    migrated = sum(result.migrated_association_count for result in results)
    merged = sum(len(result.merged_product_ids) for result in results)
    return RedirectResponse(
        f"/products/duplicate-jans?message={quote(f'已合并 {len(results)} 组重复 JAN、{merged} 个重复商品，迁移关联 {migrated} 条')}",
        status_code=303,
    )


@app.post("/products/placeholder-cleanup/confirm")
def product_placeholder_cleanup_confirm(actor: str = Form("system"), db: Session = Depends(get_db)):
    try:
        result = execute_placeholder_cleanup(db, actor=actor)
    except ValueError as exc:
        db.rollback()
        return RedirectResponse(f"/products/placeholder-cleanup?error={quote(str(exc))}", status_code=303)
    message = (
        f"已迁移商品 {result.migrated_product_count} 个、采购关联 {result.migrated_association_count} 条；"
        f"删除临时商品 {result.deleted_product_count} 个；人工处理 {result.manual_count} 个"
    )
    return RedirectResponse(f"/products/placeholder-cleanup?message={quote(message)}", status_code=303)


def _qinsi_product_export_or_404(db: Session, job_id: int) -> QinsiExportJob:
    job = get_qinsi_product_export_job(db, job_id)
    if job is None:
        raise HTTPException(404, "秦丝新商品导出记录不存在")
    return job


@app.get("/qinsi-product-exports/{job_id}", response_class=HTMLResponse)
def qinsi_product_export_detail(job_id: int, request: Request, db: Session = Depends(get_db)):
    job = _qinsi_product_export_or_404(db, job_id)
    return templates.TemplateResponse(request, "qinsi_product_export_detail.html", {
        "job": job,
        "rows": qinsi_product_export_rows(db, job.id),
    })


@app.get("/qinsi-product-exports/{job_id}/download")
def qinsi_product_export_download(job_id: int, db: Session = Depends(get_db)):
    job = _qinsi_product_export_or_404(db, job_id)
    regenerate_qinsi_product_export_file(db, job)
    db.commit()
    if not job.file_content:
        raise HTTPException(404, "秦丝新商品导出文件不存在")
    return StreamingResponse(
        io.BytesIO(job.file_content),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(job.export_filename or 'qinsi_new_products.xlsx')}"},
    )


@app.post("/qinsi-product-exports/{job_id}/confirm")
def qinsi_product_export_confirm(
    job_id: int,
    actor_name: str = Form("人工确认"),
    db: Session = Depends(get_db),
):
    job = _qinsi_product_export_or_404(db, job_id)
    try:
        confirm_qinsi_product_export(db, job, actor_name=actor_name)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return RedirectResponse(f"/qinsi-product-exports/{job_id}", status_code=303)


@app.post("/qinsi-product-exports/{job_id}/cancel-confirmation")
def qinsi_product_export_cancel_confirmation(
    job_id: int,
    actor_name: str = Form("管理员"),
    db: Session = Depends(get_db),
):
    job = _qinsi_product_export_or_404(db, job_id)
    try:
        cancel_qinsi_product_export_confirmation(db, job, actor_name=actor_name)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return RedirectResponse(f"/qinsi-product-exports/{job_id}", status_code=303)


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


@app.get("/product-local-images/{product_id}")
def product_localized_image(product_id: int, db: Session = Depends(get_db)):
    product = db.get(Product, product_id)
    if product is None or not product.local_image_path:
        raise HTTPException(404, "本地化商品图片不存在")
    path = Path(product.local_image_path)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    resolved = path.resolve()
    if not resolved.is_relative_to(QINSI_PRODUCT_IMAGE_DIR.resolve()) or not resolved.is_file():
        raise HTTPException(404, "本地化商品图片不存在")
    return FileResponse(resolved)


@app.get("/jan-governance", response_class=HTMLResponse)
def jan_governance_page(request: Request, db: Session = Depends(get_db)):
    rows, _ = build_jan_governance_rows(db)
    return templates.TemplateResponse(request, "jan_governance.html", {
        "rows": rows,
        "message": request.query_params.get("message"),
    })


@app.post("/jan-governance/export")
def jan_governance_export(db: Session = Depends(get_db)):
    report = export_jan_governance_report(db, apply_safe_fixes=True)
    message = (
        f"冲突/异常 {report.conflict_count} 行；自动修复 {report.auto_fixed_count} 条；"
        f"CSV：{report.csv_path.resolve()}"
    )
    return RedirectResponse(f"/jan-governance?message={quote(message)}", status_code=303)


@app.get("/product-image-localization", response_class=HTMLResponse)
def product_image_localization_page(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(request, "product_image_localization.html", {
        "dashboard": image_localization_dashboard(db),
        "message": request.query_params.get("message"),
    })


@app.get("/api/product-image-localization/status")
def product_image_localization_status(db: Session = Depends(get_db)):
    dashboard = image_localization_dashboard(db)
    dashboard.pop("recent_jobs", None)
    return dashboard


def _image_localization_json(session: Session) -> dict:
    dashboard = image_localization_dashboard(session)
    dashboard.pop("recent_jobs", None)
    return dashboard


@app.post("/product-image-localization/queue")
def product_image_localization_queue(
    request: Request,
    db: Session = Depends(get_db),
):
    count = queue_missing_product_images(db)
    if count:
        wake_image_localization_worker()
    if "application/json" in request.headers.get("accept", ""):
        return JSONResponse(
            {
                "status": "accepted",
                "queued": count,
                "dashboard": _image_localization_json(db),
            },
            status_code=202,
        )
    return RedirectResponse(
        f"/product-image-localization?message={quote(f'已新增 {count} 个图片任务')}",
        status_code=303,
    )


@app.post("/product-image-localization/retry-failed")
def product_image_localization_retry_failed(
    request: Request,
    db: Session = Depends(get_db),
):
    count = retry_failed_product_images(db)
    if count:
        wake_image_localization_worker()
    if "application/json" in request.headers.get("accept", ""):
        return JSONResponse(
            {
                "status": "accepted",
                "queued": count,
                "dashboard": _image_localization_json(db),
            },
            status_code=202,
        )
    return RedirectResponse(
        f"/product-image-localization?message={quote(f'已重试 {count} 个失败任务')}",
        status_code=303,
    )


@app.get("/tasks", response_class=HTMLResponse)
@app.get("/product-enrichment", response_class=HTMLResponse)
def product_enrichment_page(
    request: Request,
    missing: str = Query(""),
    low_confidence: bool = Query(False),
    failed: bool = Query(False),
    db: Session = Depends(get_db),
):
    filters = {
        "missing": missing if missing in {"name", "price", "image"} else "",
        "low_confidence": low_confidence,
        "failed": failed,
    }
    return templates.TemplateResponse(request, "product_enrichment.html", {
        "tasks": list_review_tasks(db),
        "field_items": list_field_review_items(
            db,
            missing=filters["missing"] or None,
            low_confidence=low_confidence,
            failed=failed,
        ),
        "filters": filters,
    })


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


@app.post("/product-enrichment/{task_id}/candidates/{candidate_id}/reject")
def product_enrichment_reject_candidate(
    task_id: int,
    candidate_id: int,
    return_to: str = Form("/product-enrichment"),
    db: Session = Depends(get_db),
):
    task = get_task(db, task_id)
    candidate = db.scalar(
        select(ProductEnrichmentCandidate).where(
            ProductEnrichmentCandidate.id == candidate_id,
            ProductEnrichmentCandidate.task_id == task.id,
        )
    )
    if candidate is None:
        raise HTTPException(404, "候选不存在")
    before = {"selected": candidate.selected, "source_url": candidate.source_url}
    candidate.selected = False
    db.add(EnrichmentAuditLog(
        enrichment_task_id=task.id,
        action="REJECT_CANDIDATE",
        actor="人工审核",
        before_json=json.dumps(before, ensure_ascii=False),
        after_json=json.dumps({"selected": False}, ensure_ascii=False),
    ))
    db.commit()
    safe_return = return_to if return_to.startswith("/") and not return_to.startswith("//") else "/product-enrichment"
    return RedirectResponse(safe_return, status_code=303)


@app.get("/field-purchase/items/{item_id}/review", response_class=HTMLResponse)
def field_purchase_item_review(item_id: int, request: Request, db: Session = Depends(get_db)):
    try:
        item = get_field_item(db, item_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    stores = list(db.scalars(select(Store).where(Store.is_active.is_(True)).order_by(Store.name)))
    resolution = resolve_local_product_by_jan(db, item.jan) if item.jan else None
    jan_candidates = list(resolution.candidate_products) if resolution and resolution.is_conflict else []
    return templates.TemplateResponse(
        request,
        "field_purchase_review.html",
        {"item": item, "stores": stores, "jan_candidates": jan_candidates, "error": None},
    )


@app.post("/field-purchase/items/{item_id}/bind")
def field_purchase_item_bind(
    item_id: int,
    product_id: int = Form(...),
    actor: str = Form("人工审核"),
    db: Session = Depends(get_db),
):
    try:
        bind_field_item_to_product(db, item_id, product_id, actor=actor)
    except LookupError as exc:
        db.rollback()
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        db.rollback()
        raise HTTPException(422, str(exc)) from exc
    return RedirectResponse("/product-enrichment", status_code=303)


@app.post("/field-purchase/items/{item_id}/jan")
def field_purchase_item_assign_jan(
    item_id: int,
    jan: str = Form(...),
    actor: str = Form("人工审核"),
    db: Session = Depends(get_db),
):
    try:
        item = assign_field_item_jan(db, item_id, jan, actor=actor)
    except LookupError as exc:
        db.rollback()
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        db.rollback()
        raise HTTPException(422, str(exc)) from exc
    return RedirectResponse(f"/field-purchase/items/{item.id}/review", status_code=303)


@app.post("/field-purchase/items/{item_id}/review")
async def field_purchase_item_update(
    item_id: int,
    product_image: UploadFile | None = File(None),
    name_cn: str = Form(""),
    name_ja: str = Form(""),
    unit_price: str = Form(""),
    brand: str = Form(""),
    category: str = Form(""),
    unit_name: str = Form(""),
    actor: str = Form("人工审核"),
    db: Session = Depends(get_db),
):
    try:
        price = int(unit_price) if unit_price.strip() else None
        if price is not None and price < 0:
            raise ValueError
    except ValueError as exc:
        raise HTTPException(422, "价格必须为非负整数日元") from exc
    try:
        update_field_item(
            db,
            item_id,
            actor=actor,
            name_cn=name_cn,
            name_ja=name_ja,
            unit_price=price,
            brand=brand,
            category=category,
            unit_name=unit_name,
        )
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    if product_image is not None and product_image.filename:
        try:
            save_field_product_image(
                db,
                item_id,
                actor=actor,
                content=await product_image.read(),
                content_type=(product_image.content_type or "application/octet-stream").casefold(),
                original_filename=product_image.filename,
            )
        except ValueError as exc:
            db.rollback()
            raise HTTPException(422, str(exc)) from exc
    return RedirectResponse(f"/field-purchase/items/{item_id}/review", status_code=303)


@app.post("/field-purchase/items/{item_id}/candidates/{candidate_id}/select")
def field_purchase_item_select_candidate(
    item_id: int,
    candidate_id: int,
    db: Session = Depends(get_db),
):
    item = get_field_item(db, item_id)
    candidate = db.get(ProductEnrichmentCandidate, candidate_id)
    if candidate is None or item.enrichment_task_id != candidate.task_id:
        raise HTTPException(404, "线上资料候选不存在")
    task = get_task(db, candidate.task_id)
    before = {
        "name_ja": item.name_ja,
        "unit_price": item.unit_price,
        "selected_candidate_id": next((row.id for row in task.candidates if row.selected), None),
    }
    for row in task.candidates:
        row.selected = row.id == candidate.id
    if candidate.name_ja and not item.name_ja:
        item.name_ja = candidate.name_ja[:128]
    if candidate.item_price is not None and item.unit_price is None:
        item.unit_price = candidate.item_price
    selected = {
        "jan": candidate.jan,
        "name_ja": candidate.name_ja,
        "image_url": candidate.image_url,
        "source_url": candidate.source_url,
        "platform": candidate.platform,
        "item_price": candidate.item_price,
        "shipping_price": candidate.shipping_price,
        "total_price": candidate.total_price,
        "seller": candidate.provider_summary_json,
    }
    task.selected_data_json = json.dumps(selected, ensure_ascii=False, default=str)
    db.add(EnrichmentAuditLog(
        field_purchase_item_id=item.id,
        enrichment_task_id=task.id,
        action="SELECT_CANDIDATE",
        actor="人工审核",
        before_json=json.dumps(before, ensure_ascii=False),
        after_json=json.dumps({
            "candidate_id": candidate.id,
            "name_ja": item.name_ja,
            "unit_price": item.unit_price,
            "image_url": candidate.image_url,
            "source_url": candidate.source_url,
        }, ensure_ascii=False),
    ))
    db.commit()
    return RedirectResponse(f"/field-purchase/items/{item_id}/review", status_code=303)


@app.post("/field-purchase/items/{item_id}/ai-name")
def field_purchase_item_ai_name(item_id: int, db: Session = Depends(get_db)):
    item = get_field_item(db, item_id)
    if item.enrichment_task is None:
        return RedirectResponse(
            f"/field-purchase/items/{item_id}/review?error={quote('请先取得线上资料候选')}",
            status_code=303,
        )
    task = get_task(db, item.enrichment_task.id)
    candidate = next((row for row in task.candidates if row.selected), None) or next(iter(task.candidates), None)
    if candidate is None or not (candidate.name_ja or item.name_ja):
        return RedirectResponse(
            f"/field-purchase/items/{item_id}/review?error={quote('没有可翻译的日文商品名')}",
            status_code=303,
        )
    if not candidate.name_ja and item.name_ja:
        candidate.name_ja = item.name_ja
    result = translate_candidate(db, task, candidate)
    if result is None:
        db.commit()
        status = task.deepseek_status or "未配置"
        return RedirectResponse(
            f"/field-purchase/items/{item_id}/review?error={quote('AI生成中文名未完成：' + status)}",
            status_code=303,
        )
    before = {"name_cn": item.name_cn, "name_ja": item.name_ja}
    item.name_cn = result.name_cn[:128]
    item.name_ja = result.name_ja[:128]
    if item.status not in {"CONFIRMED", "FAILED_MANUAL"}:
        item.status = "READY" if item.name_cn and item.name_ja else "NEEDS_REVIEW"
    db.add(EnrichmentAuditLog(
        field_purchase_item_id=item.id,
        enrichment_task_id=task.id,
        action="MANUAL_EDIT",
        actor="DeepSeek",
        before_json=json.dumps(before, ensure_ascii=False),
        after_json=json.dumps({"name_cn": item.name_cn, "name_ja": item.name_ja}, ensure_ascii=False),
        source="ai_translation",
    ))
    db.commit()
    return RedirectResponse(f"/field-purchase/items/{item_id}/review", status_code=303)


@app.post("/field-purchase/items/{item_id}/confirm")
def field_purchase_item_confirm(
    item_id: int,
    request: Request,
    actor: str = Form("人工审核"),
    db: Session = Depends(get_db),
):
    try:
        product = confirm_field_item(db, item_id, actor=actor)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        db.rollback()
        item = get_field_item(db, item_id)
        stores = list(db.scalars(select(Store).where(Store.is_active.is_(True)).order_by(Store.name)))
        resolution = resolve_local_product_by_jan(db, item.jan) if item.jan else None
        jan_candidates = list(resolution.candidate_products) if resolution and resolution.is_conflict else []
        return templates.TemplateResponse(
            request,
            "field_purchase_review.html",
            {"item": item, "stores": stores, "jan_candidates": jan_candidates, "error": str(exc)},
            status_code=422,
        )
    return RedirectResponse(f"/products/{product.id}", status_code=303)


@app.get("/field-purchase/items/{item_id}/image")
def field_purchase_item_image(item_id: int, db: Session = Depends(get_db)):
    item = db.get(FieldPurchaseItem, item_id)
    if item is None or not item.product_image_path:
        raise HTTPException(404, "商品照片不存在")
    path = (PROJECT_ROOT / item.product_image_path).resolve()
    if not path.is_relative_to(TAG_EVIDENCE_DIR.resolve()) or not path.is_file():
        raise HTTPException(404, "商品照片不存在")
    return FileResponse(path)


@app.post("/field-purchase/items/batch")
async def field_purchase_items_batch(
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    form = await request.form()
    item_ids = {int(value) for value in form.getlist("item_ids") if str(value).isdigit()}
    action = str(form.get("action") or "")
    actor = str(form.get("actor") or "批量审核")
    if action == "edit":
        bulk_edit_field_items(
            db,
            item_ids,
            actor=actor,
            brand=str(form.get("brand") or ""),
            category=str(form.get("category") or ""),
            unit_name=str(form.get("unit_name") or ""),
        )
    elif action == "retry":
        job_ids = retry_field_items(db, item_ids, actor=actor)
        for job_id in job_ids:
            background_tasks.add_task(process_durable_job, job_id, db.get_bind())
    else:
        raise HTTPException(422, "不支持的批量操作")
    return RedirectResponse("/product-enrichment", status_code=303)


@app.get("/locations", response_class=HTMLResponse)
def locations_page(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(request, "locations.html", {
        "locations": list_locations(db), "location_type_cn": LOCATION_TYPE_CN,
    })


@app.get("/purchase-analytics", response_class=HTMLResponse)
def purchase_analytics_page(
    request: Request, range_key: str = Query("30d", alias="range"),
    start_date: date | None = Query(None), end_date: date | None = Query(None),
    bucket: str | None = Query(None), receipt_no: str = Query(""),
    query_text: str = Query("", alias="query"), store_id: str = Query(""),
    operator_name: str = Query("", alias="operator"), batch: str = Query(""),
    status_filter: str = Query("", alias="status"), notes: str = Query(""),
    db: Session = Depends(get_db),
):
    period = resolve_date_range(range_key, start_date, end_date)
    context = analytics_dashboard(db, period, selected_bucket=bucket)
    filters = {"receipt_no": receipt_no, "query": query_text, "store_id": store_id,
               "operator": operator_name, "batch": batch, "status": status_filter, "notes": notes}
    context.update(procurement_data(db, period, filters))
    context["purchase_filters"] = filters
    context["filter_stores"] = list(db.scalars(select(Store).where(Store.is_active.is_(True)).order_by(Store.name_cn, Store.name_ja, Store.id)))
    context["filter_operators"] = list(db.scalars(
        select(PurchaseBatch.operator_name).where(PurchaseBatch.operator_name.is_not(None)).distinct().order_by(PurchaseBatch.operator_name)
    ))
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


def _product_search_payload(product: Product) -> dict:
    return {
        "id": product.id,
        "display_name": product.display_name or product.name_cn or product.name_ja or product.internal_sku,
        "jan": product.jan,
        "internal_sku": product.internal_sku,
        "sale_price": str(product.sale_price) if product.sale_price is not None else None,
    }


def _customer_search_payload(customer: Customer) -> dict:
    return {
        "id": customer.id, "name": customer.name, "phone": customer.phone,
        "wechat_name": customer.wechat_name, "address": customer.address, "note": customer.note,
    }


class CustomerCreateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=255)
    phone: str | None = None
    wechat_name: str | None = None
    address: str | None = None
    note: str | None = None


@app.get("/api/products/search")
def api_sales_order_product_search(q: str = Query(""), db: Session = Depends(get_db)):
    return [_product_search_payload(product) for product in search_sales_order_products(db, q, limit=20)]


@app.get("/api/customers/search")
def api_customer_search(q: str = Query(""), db: Session = Depends(get_db)):
    return [_customer_search_payload(customer) for customer in search_customers(db, q, limit=20)]


@app.post("/api/customers", status_code=201)
def api_create_customer(data: CustomerCreateInput, db: Session = Depends(get_db)):
    try:
        customer = create_customer(
            db, name=data.name, phone=data.phone, wechat_name=data.wechat_name,
            address=data.address, note=data.note,
        )
    except ValueError as exc:
        db.rollback()
        raise HTTPException(422, str(exc)) from exc
    return _customer_search_payload(customer)


SALES_ORDER_TAB_STATUSES = ("submitted", "ready_to_ship", "shipped", "completed", "cancelled")


@app.get("/sales-orders", response_class=HTMLResponse)
def sales_orders_page(
    request: Request, status: str = Query(""), q: str = Query(""),
    date_from: str = Query(""), date_to: str = Query(""), db: Session = Depends(get_db),
):
    parsed_from = date.fromisoformat(date_from) if date_from else None
    parsed_to = date.fromisoformat(date_to) if date_to else None
    rows = list_sales_orders(db, status=status or None, q=q or None, date_from=parsed_from, date_to=parsed_to)
    counts = sales_order_status_counts(db)
    tabs = [(value, SALES_ORDER_STATUS_CN[value], counts.get(value, 0)) for value in SALES_ORDER_TAB_STATUSES]
    return templates.TemplateResponse(request, "sales_orders.html", {
        "rows": rows, "status": status, "q": q, "date_from": date_from, "date_to": date_to,
        "status_labels": SALES_ORDER_STATUS_CN, "tabs": tabs, "total_count": sum(counts.values()),
        "primary_next_action": PRIMARY_NEXT_ACTION,
    })


@app.get("/sales-orders/new", response_class=HTMLResponse)
def sales_order_new_page(request: Request, db: Session = Depends(get_db)):
    default_salesperson = ensure_default_salesperson(db)
    salespersons = list_salespersons(db)
    return templates.TemplateResponse(request, "sales_order_new.html", {
        "default_salesperson": default_salesperson, "salespersons": salespersons,
        "error": request.query_params.get("error"),
    })


@app.post("/sales-orders")
async def sales_order_create(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    customer_id = str(form.get("customer_id") or "")
    salesperson_id = str(form.get("salesperson_id") or "")
    note = str(form.get("note") or "")
    try:
        if not customer_id.isdigit() or not salesperson_id.isdigit():
            raise ValueError("请先选择客户")
        raw_items = json.loads(form.get("items_json") or "[]")
        if not isinstance(raw_items, list):
            raise ValueError("商品数据格式错误")
        items: list[SalesOrderItemInput] = []
        for raw in raw_items:
            try:
                quantity = int(raw.get("quantity"))
                unit_sale_price = Decimal(str(raw.get("unit_sale_price")))
            except (TypeError, ValueError, ArithmeticError) as exc:
                raise ValueError("数量或单价格式错误") from exc
            product_id_raw = raw.get("product_id")
            items.append(SalesOrderItemInput(
                product_id=int(product_id_raw) if product_id_raw not in (None, "", 0) else None,
                manual_name=raw.get("manual_name"), jan=raw.get("jan"),
                quantity=quantity, unit_sale_price=unit_sale_price, note=raw.get("note"),
            ))
        order = create_sales_order(
            db, customer_id=int(customer_id), salesperson_id=int(salesperson_id), items=items, note=note,
            recipient_name=str(form.get("recipient_name") or ""),
            recipient_phone=str(form.get("recipient_phone") or ""),
            shipping_address=str(form.get("shipping_address") or ""),
        )
    except (LookupError, ValueError) as exc:
        db.rollback()
        return RedirectResponse(f"/sales-orders/new?error={quote(str(exc))}", status_code=303)
    return RedirectResponse(f"/sales-orders/{order.id}", status_code=303)


@app.get("/sales-orders/{order_id}", response_class=HTMLResponse)
def sales_order_detail_page(order_id: int, request: Request, db: Session = Depends(get_db)):
    order = get_sales_order(db, order_id)
    if order is None:
        raise HTTPException(404, "订单不存在")
    return templates.TemplateResponse(request, "sales_order_detail.html", {
        "order": order, "status_labels": SALES_ORDER_STATUS_CN,
        "allowed_transitions": ALLOWED_TRANSITIONS.get(order.status, set()),
        "primary_next_action": PRIMARY_NEXT_ACTION.get(order.status),
        "can_upload_shipping_label": order.status in SHIPPING_LABEL_UPLOADABLE_STATUSES,
        "can_delete_shipping_label": order.status in SHIPPING_LABEL_DELETABLE_STATUSES,
        "error": request.query_params.get("error"),
    })


@app.post("/sales-orders/{order_id}/status")
def sales_order_status_update(order_id: int, status: str = Form(...), db: Session = Depends(get_db)):
    try:
        update_sales_order_status(db, order_id, status)
    except LookupError as exc:
        db.rollback()
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        db.rollback()
        return RedirectResponse(f"/sales-orders/{order_id}?error={quote(str(exc))}", status_code=303)
    return RedirectResponse(f"/sales-orders/{order_id}", status_code=303)


@app.post("/sales-orders/{order_id}/shipping-labels")
async def sales_order_shipping_label_upload(order_id: int, file: UploadFile = File(...), db: Session = Depends(get_db)):
    content = await file.read()
    try:
        add_shipping_label(db, order_id, content=content, original_filename=file.filename)
    except LookupError as exc:
        db.rollback()
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        db.rollback()
        return RedirectResponse(f"/sales-orders/{order_id}?error={quote(str(exc))}", status_code=303)
    return RedirectResponse(f"/sales-orders/{order_id}", status_code=303)


@app.get("/sales-orders/shipping-labels/{label_id}")
def sales_order_shipping_label_view(label_id: int, db: Session = Depends(get_db)):
    label = get_shipping_label(db, label_id)
    if label is None:
        raise HTTPException(404, "面单图片不存在")
    path = resolve_shipping_label_path(label.relative_path)
    if path is None:
        raise HTTPException(404, "面单图片文件不存在")
    return FileResponse(path, media_type=label.content_type or "application/octet-stream")


@app.get("/sales-orders/shipping-labels/{label_id}/download")
def sales_order_shipping_label_download(label_id: int, db: Session = Depends(get_db)):
    label = get_shipping_label(db, label_id)
    if label is None:
        raise HTTPException(404, "面单图片不存在")
    path = resolve_shipping_label_path(label.relative_path)
    if path is None:
        raise HTTPException(404, "面单图片文件不存在")
    filename = label.original_filename or label.stored_filename
    return FileResponse(path, media_type=label.content_type or "application/octet-stream", filename=filename)


@app.post("/sales-orders/shipping-labels/{label_id}/delete")
def sales_order_shipping_label_delete(label_id: int, db: Session = Depends(get_db)):
    label = get_shipping_label(db, label_id)
    order_id = label.sales_order_id if label else None
    try:
        remove_shipping_label(db, label_id)
    except LookupError as exc:
        db.rollback()
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        db.rollback()
        return RedirectResponse(f"/sales-orders/{order_id}?error={quote(str(exc))}", status_code=303)
    return RedirectResponse(f"/sales-orders/{order_id}", status_code=303)


PROCUREMENT_DEMAND_VIEWS = ("open", "investigation", "planned", "all")


@app.get("/procurement-demands", response_class=HTMLResponse)
def procurement_demands_page(request: Request, view: str = Query("open"), db: Session = Depends(get_db)):
    if view not in PROCUREMENT_DEMAND_VIEWS:
        view = "open"
    groups: list = []
    investigations: list = []
    plans: list = []
    inventory_contexts: dict = {}
    plan_inventories: dict = {}
    if view == "open":
        groups = [g for g in aggregate_open_demand_groups(db) if g.confirmed_demands]
        inventory_contexts = build_group_inventory_contexts(db, groups)
    elif view == "investigation":
        investigations = list_investigation_demands(db, status="open")
    elif view == "planned":
        plans = list_plans(db, status="planned")
        plan_inventories = build_plan_inventory_contexts(db, plans)
    else:
        groups = aggregate_all_demand_groups(db)
    return templates.TemplateResponse(request, "procurement_demands.html", {
        "view": view, "groups": groups, "investigations": investigations, "plans": plans,
        "demand_type_labels": DEMAND_TYPE_LABELS, "source_type_labels": SOURCE_TYPE_LABELS,
        "status_labels": PROCUREMENT_STATUS_LABELS, "default_qty": default_planned_quantity_for_group,
        "inventory_contexts": inventory_contexts, "plan_inventories": plan_inventories,
        "freshness_labels": FRESHNESS_LABELS, "error": request.query_params.get("error"),
    })


@app.get("/procurement-demands/groups/{kind}/{key}", response_class=HTMLResponse)
def procurement_demand_group_detail(kind: str, key: str, request: Request, db: Session = Depends(get_db)):
    group = get_group(db, kind, key)
    if group is None:
        raise HTTPException(404, "需求分组不存在")
    inventory_context = build_group_inventory_contexts(db, [group])[group.group_ref]
    return templates.TemplateResponse(request, "procurement_demand_detail.html", {
        "group": group, "demand_type_labels": DEMAND_TYPE_LABELS, "source_type_labels": SOURCE_TYPE_LABELS,
        "status_labels": PROCUREMENT_STATUS_LABELS, "default_qty": default_planned_quantity_for_group(group),
        "inventory_context": inventory_context, "freshness_labels": FRESHNESS_LABELS,
    })


@app.post("/procurement-demands/plan")
async def procurement_demand_create_plans(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    try:
        raw_selections = json.loads(form.get("selections_json") or "[]")
        if not isinstance(raw_selections, list):
            raise ValueError("提交数据格式错误")
        selections = []
        for raw in raw_selections:
            selections.append(PlanSelectionInput(
                kind=str(raw.get("kind")), key=str(raw.get("key")),
                planned_quantity=int(raw.get("planned_quantity")),
            ))
        create_plans(db, selections)
    except (ValueError, LookupError, TypeError) as exc:
        db.rollback()
        return RedirectResponse(f"/procurement-demands?error={quote(str(exc))}", status_code=303)
    return RedirectResponse("/procurement-demands?view=planned", status_code=303)


@app.get("/procurement-demands/report-shortage", response_class=HTMLResponse)
def procurement_demand_report_shortage_page(request: Request):
    return templates.TemplateResponse(request, "procurement_demand_shortage_new.html", {
        "error": request.query_params.get("error"),
    })


@app.post("/procurement-demands/report-shortage")
async def procurement_demand_report_shortage_submit(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    product_id = str(form.get("product_id") or "")
    manual_name = str(form.get("manual_name") or "")
    quantity_raw = str(form.get("quantity") or "")
    note = str(form.get("note") or "")
    try:
        create_channel_shortage_demand(
            db, product_id=int(product_id) if product_id.isdigit() else None,
            manual_name=manual_name or None, quantity=int(quantity_raw) if quantity_raw.isdigit() else None,
            note=note or None,
        )
    except (ValueError, LookupError) as exc:
        db.rollback()
        return RedirectResponse(f"/procurement-demands/report-shortage?error={quote(str(exc))}", status_code=303)
    return RedirectResponse("/procurement-demands/report-shortage?ok=1", status_code=303)


@app.post("/procurement-demands/investigations")
async def procurement_demand_investigation_submit(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    product_id = str(form.get("product_id") or "")
    manual_name = str(form.get("manual_name") or "")
    quantity_raw = str(form.get("quantity") or "")
    note = str(form.get("note") or "")
    try:
        create_investigation_demand(
            db, product_id=int(product_id) if product_id.isdigit() else None,
            manual_name=manual_name or None, quantity=int(quantity_raw) if quantity_raw.isdigit() else None,
            note=note or None,
        )
    except (ValueError, LookupError) as exc:
        db.rollback()
        return RedirectResponse(f"/sales-orders/new?error={quote(str(exc))}", status_code=303)
    return RedirectResponse("/sales-orders/new?investigation_ok=1", status_code=303)


@app.post("/procurement-demands/{demand_id}/close")
def procurement_demand_close(demand_id: int, note: str = Form(""), db: Session = Depends(get_db)):
    try:
        close_investigation_demand(db, demand_id, note=note or None)
    except LookupError as exc:
        db.rollback()
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        db.rollback()
        return RedirectResponse(f"/procurement-demands?view=investigation&error={quote(str(exc))}", status_code=303)
    return RedirectResponse("/procurement-demands?view=investigation", status_code=303)


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
def store_detail(
    store_id: int, request: Request, month: str | None = Query(None), db: Session = Depends(get_db),
):
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
    month_facts = [
        fact for fact in facts
        if month and (fact.batch.purchased_at or fact.batch.confirmed_at).strftime("%Y-%m") == month
    ]
    return templates.TemplateResponse(request, "store_detail.html", {
        "store": store, "products": products, "facts": facts, "receipt_rows": receipt_rows, "stats": stats,
        "monthly_trend": store_monthly_trend_points(facts),
        "selected_month": month, "month_facts": month_facts,
        "recent_restock_lists": recent_lists_for_store(db, store_id),
        "restock_status_labels": LIST_STATUS_LABELS,
    })


@app.get("/api/locations", response_model=list[LocationOutput])
def api_locations(db: Session = Depends(get_db)):
    return list_locations(db)


def _purchase_batches_template_context(
    db: Session,
    *,
    selected_purchase_batch_ids: set[int] | None = None,
    merge_error: str | None = None,
) -> dict:
    purchase_batches = list_purchase_batches(db)
    selected_purchase_batch_ids = selected_purchase_batch_ids or set()
    merge_rows = []
    for purchase_batch in purchase_batches:
        states = purchase_item_export_states(db, purchase_batch.id)
        pending_count = sum(states.get(item.id, "pending") == "pending" for item in purchase_batch.items)
        blocking_count = len({
            item.product_id for item in purchase_batch.items
            if item.product.status != "qinsi_product_imported"
        })
        merge_rows.append({
            "batch": purchase_batch,
            "pending_count": pending_count,
            "blocking_count": blocking_count,
            "selectable": purchase_batch.status != "cancelled" and pending_count > 0,
        })
    blocking_products = list({
            item.product.id: item.product
            for purchase_batch in purchase_batches if purchase_batch.id in selected_purchase_batch_ids
            for item in purchase_batch.items
            if item.product and qinsi_product_requires_import(item.product)
    }.values())
    return {
        "purchase_batches": purchase_batches, "merge_rows": merge_rows,
        "purchase_status_cn": PURCHASE_STATUS_CN,
        "selected_purchase_batch_ids": selected_purchase_batch_ids,
        "merge_error": merge_error,
        "merge_blocking_products": blocking_products,
        "product_display_label": product_display_label,
    }


@app.get("/purchase-batches", response_class=HTMLResponse)
def purchase_batches_page(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(request, "purchase_batches.html", _purchase_batches_template_context(db))


@app.post("/purchase-batches/qinsi-exports/merge")
async def create_merged_purchase_batch_qinsi_export(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    purchase_batch_ids: set[int] = set()
    try:
        purchase_batch_ids = {int(value) for value in form.getlist("purchase_batch_ids")}
        job = generate_merged_purchase_batch_export(db, purchase_batch_ids)
    except LookupError as exc:
        db.rollback()
        return templates.TemplateResponse(
            request, "purchase_batches.html",
            _purchase_batches_template_context(db, selected_purchase_batch_ids=purchase_batch_ids, merge_error=str(exc)),
            status_code=200,
        )
    except (TypeError, ValueError) as exc:
        db.rollback()
        return templates.TemplateResponse(
            request, "purchase_batches.html",
            _purchase_batches_template_context(db, selected_purchase_batch_ids=purchase_batch_ids, merge_error=str(exc)),
            status_code=200,
        )
    return RedirectResponse(f"/qinsi-exports/{job.id}", status_code=303)


@app.get("/purchase-batches/{purchase_batch_id}", response_class=HTMLResponse)
def purchase_batch_detail(purchase_batch_id: int, request: Request, db: Session = Depends(get_db)):
    purchase_batch = get_purchase_batch(db, purchase_batch_id)
    if purchase_batch is None:
        raise HTTPException(404, "采购批次不存在")
    export_states = purchase_item_export_states(db, purchase_batch.id)
    item_states = {item.id: export_states.get(item.id, "pending") for item in purchase_batch.items}
    blocking_products = list({
        item.product.id: item.product for item in purchase_batch.items
        if qinsi_product_requires_import(item.product)
    }.values())
    summary = {
        "kind_count": len({item.product.jan or item.product_id for item in purchase_batch.items}),
        "quantity": sum(item.quantity for item in purchase_batch.items),
        "amount": sum(item.actual_line_amount or 0 for item in purchase_batch.items),
    }
    return templates.TemplateResponse(request, "purchase_batch_detail.html", {
        "purchase_batch": purchase_batch, "purchase_status_cn": PURCHASE_STATUS_CN,
        "item_states": item_states, "purchase_export_state_cn": PURCHASE_EXPORT_STATE_CN,
        "pending_export_count": 0 if blocking_products else sum(state == "pending" for state in item_states.values()),
        "blocking_products": blocking_products, "summary": summary,
        "product_display_label": product_display_label,
    })


@app.post("/purchase-batches/{purchase_batch_id}/metadata")
def update_purchase_batch_metadata(
    purchase_batch_id: int, operator_name: str = Form(""), note: str = Form(""),
    db: Session = Depends(get_db),
):
    purchase_batch = db.get(PurchaseBatch, purchase_batch_id)
    if purchase_batch is None:
        raise HTTPException(404, "采购批次不存在")
    purchase_batch.operator_name = operator_name.strip()[:128] or None
    purchase_batch.note = note.strip()[:2000] or None
    db.commit()
    return RedirectResponse(f"/purchase-batches/{purchase_batch_id}?message={quote('人员与备注已保存')}", status_code=303)


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
        jobs = [job for job in jobs if purchase_batch_id in job.selected_batch_ids]
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
        "error": request.query_params.get("error"),
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
            "actor_name": str(form.get("actor_name") or job.purchase_batch.operator_name or "人工确认"),
            "note": str(form.get("note") or "") or None,
        })
        confirm_qinsi_export(db, job, confirmation)
    except ValidationError as exc:
        db.rollback()
        return templates.TemplateResponse(request, "qinsi_export_detail.html", {
            "job": job, "qinsi_export_status_cn": QINSI_EXPORT_STATUS_CN,
            "qinsi_export_type_cn": QINSI_EXPORT_TYPE_CN, "qinsi_line_status_cn": QINSI_LINE_STATUS_CN,
            "error": f"确认结果无效：{_qinsi_confirmation_validation_message(exc)}",
        }, status_code=422)
    except ValueError as exc:
        db.rollback()
        return templates.TemplateResponse(request, "qinsi_export_detail.html", {
            "job": job, "qinsi_export_status_cn": QINSI_EXPORT_STATUS_CN,
            "qinsi_export_type_cn": QINSI_EXPORT_TYPE_CN, "qinsi_line_status_cn": QINSI_LINE_STATUS_CN,
            "error": str(exc),
        }, status_code=409)
    return RedirectResponse(f"/qinsi-exports/{export_job_id}", status_code=303)


@app.post("/qinsi-exports/{export_job_id}/cancel-confirmation")
async def qinsi_export_cancel_confirmation(export_job_id: int, request: Request, db: Session = Depends(get_db)):
    job = _qinsi_export_or_404(db, export_job_id)
    form = await request.form()
    actor = str(form.get("actor_name") or "管理员")
    try:
        cancel_qinsi_export_confirmation(db, job, actor_name=actor)
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
    request: Request, product_id: int = Form(...), user_target_price: str = Form(""),
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
        if request.headers.get("x-requested-with") == "fetch":
            return JSONResponse({"ok": False, "message": str(exc)}, status_code=422)
        return RedirectResponse(f"{_return_path(return_to)}?error={quote(str(exc))}", status_code=303)
    except SQLAlchemyError:
        db.rollback()
        message = "关注保存失败，请稍后重试"
        if request.headers.get("x-requested-with") == "fetch":
            return JSONResponse({"ok": False, "message": message}, status_code=503)
        return RedirectResponse(f"{_return_path(return_to)}?error={quote(message)}", status_code=303)
    if request.headers.get("x-requested-with") == "fetch":
        return JSONResponse({"ok": True, "message": "已关注"})
    return RedirectResponse(_return_path(return_to), status_code=303)


@app.post("/watched-products/{product_id}/remove")
def watched_products_remove(
    request: Request, product_id: int, return_to: str = Form("/watched-products"),
    db: Session = Depends(get_db),
):
    try:
        removed = remove_watch(db, product_id)
    except SQLAlchemyError:
        db.rollback()
        message = "取消关注失败，请稍后重试"
        if request.headers.get("x-requested-with") == "fetch":
            return JSONResponse({"ok": False, "message": message}, status_code=503)
        return RedirectResponse(f"{_return_path(return_to)}?error={quote(message)}", status_code=303)
    message = "已取消关注" if removed else "当前未关注"
    if request.headers.get("x-requested-with") == "fetch":
        return JSONResponse({"ok": True, "message": message})
    return RedirectResponse(f"{_return_path(return_to)}?message={quote(message)}", status_code=303)


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
        select(ReceiptItem, Receipt, ReceiptBatch, ReceiptImage, PurchaseBatchItem, PurchaseBatch)
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
        for item, _, _, _, _, _ in rows:
            amount = item.line_total if item.line_total is not None else (item.unit_price * item.quantity - item.discount_amount if item.unit_price is not None else None)
            if amount is not None:
                legacy_prices.append(Decimal(amount) / item.quantity)
    stats = {
        "count": len({fact.batch.id for fact in facts}) if facts else len(rows),
        "quantity": sum(fact.item.quantity for fact in facts) if facts else sum(item.quantity for item, _, _, _, _, _ in rows),
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
        "product_status_labels": {
            "active": "普通商品",
            "new_pending_completion": "新商品待补全",
            "new_pending_review": "新商品待人工确认",
            "pending_qinsi_product_import": "待导入秦丝商品库",
            "qinsi_product_imported": "已导入秦丝商品库",
            "archived": "已停用/归档",
        },
        "editable_product_statuses": EDITABLE_PRODUCT_STATUSES,
        "associations": product_associations(db, product),
        "operation_logs": product.operation_logs[:10],
        "saved": request.query_params.get("saved"), "error": request.query_params.get("error"),
        "message": request.query_params.get("message"),
        "product_display_label": product_display_label,
        "is_missing_chinese_name": is_missing_chinese_name,
    })


@app.post("/products/{product_id}")
def update_product_page(
    product_id: int,
    name_cn: str = Form(""),
    name_ja: str = Form(""),
    jan: str | None = Form(None),
    main_image_source_url: str = Form(""),
    purchase_price: str = Form(""),
    specification: str = Form(""),
    net_weight_g: str = Form(""),
    volume_ml: str = Form(""),
    length_mm: str = Form(""),
    width_mm: str = Form(""),
    height_mm: str = Form(""),
    depth_mm: str = Form(""),
    pack_quantity: str = Form(""),
    spec_text: str = Form(""),
    status: str = Form("active"),
    actor: str = Form("人工操作"),
    reason: str = Form(""),
    db: Session = Depends(get_db),
):
    product = db.get(Product, product_id)
    if not product:
        raise HTTPException(404, "商品不存在")
    try:
        update_product_master(
            db,
            product,
            name_cn=name_cn,
            name_ja=name_ja,
            jan=jan,
            image_url=main_image_source_url,
            purchase_price=purchase_price,
            specification=specification,
            net_weight_g=net_weight_g,
            volume_ml=volume_ml,
            length_mm=length_mm,
            width_mm=width_mm,
            height_mm=height_mm,
            depth_mm=depth_mm,
            pack_quantity=pack_quantity,
            spec_text=spec_text,
            status=status,
            actor=actor,
            reason=reason,
        )
    except ValueError as exc:
        db.rollback()
        return RedirectResponse(f"/products/{product_id}?error={quote(str(exc))}", status_code=303)
    except IntegrityError:
        db.rollback()
        return RedirectResponse(f"/products/{product_id}?error={quote('JAN或秦丝商品编码已存在，不能重复保存')}", status_code=303)
    if main_image_source_url.strip():
        wake_image_localization_worker()
    return RedirectResponse(f"/products/{product_id}?saved=1", status_code=303)


@app.post("/products/{product_id}/photo")
async def product_photo_completion(
    product_id: int,
    product_image: UploadFile = File(...),
    name_cn: str = Form(""),
    name_ja: str = Form(""),
    spec_text: str = Form(""),
    actor: str = Form("人工操作"),
    db: Session = Depends(get_db),
):
    product = db.get(Product, product_id)
    if not product:
        raise HTTPException(404, "商品不存在")
    try:
        save_product_photo_for_completion(
            db,
            product,
            content=await product_image.read(),
            content_type=(product_image.content_type or "application/octet-stream").casefold(),
            original_filename=product_image.filename or "product-photo",
            name_cn=name_cn,
            name_ja=name_ja,
            spec_text=spec_text,
            actor=actor,
        )
    except ValueError as exc:
        db.rollback()
        return RedirectResponse(f"/products/{product_id}?error={quote(str(exc))}", status_code=303)
    return RedirectResponse(f"/products/{product_id}?message={quote('已上传商品照片，资料候选待确认')}", status_code=303)


@app.post("/products/{product_id}/redownload-image")
def product_redownload_image(product_id: int, db: Session = Depends(get_db)):
    product = db.get(Product, product_id)
    if not product:
        raise HTTPException(404, "商品不存在")
    result = refresh_existing_product_main_image(db, product)
    if result.replaced:
        message = f"已替换为高清商品图：{result.new_width}x{result.new_height}"
    elif result.reason == "thumbnail_only":
        message = "只找到缩略图，已标记thumbnail并保留旧图"
    elif result.reason == "not_larger":
        message = "新图不比现有主图更清晰，已保留旧图"
    else:
        return RedirectResponse(
            f"/products/{product_id}?error={quote('没有找到可刷新的商品图候选')}",
            status_code=303,
        )
    return RedirectResponse(
        f"/products/{product_id}?message={quote(message)}",
        status_code=303,
    )


@app.post("/products/{product_id}/restore-auto-image")
def product_restore_auto_image(product_id: int, db: Session = Depends(get_db)):
    product = db.get(Product, product_id)
    if not product:
        raise HTTPException(404, "商品不存在")
    product.main_image_locked = False
    product.main_image_path = None
    product.main_image_hash = None
    product.image_width = None
    product.image_height = None
    product.image_quality = None
    db.commit()
    return RedirectResponse(
        f"/products/{product_id}?message={quote('已恢复自动图片优先级')}",
        status_code=303,
    )


@app.post("/products/{product_id}/restore-auto-image")
def product_restore_auto_image(product_id: int, db: Session = Depends(get_db)):
    product = db.get(Product, product_id)
    if not product:
        raise HTTPException(404, "商品不存在")
    product.main_image_locked = False
    product.main_image_path = None
    product.main_image_hash = None
    product.image_width = None
    product.image_height = None
    product.image_quality = None
    db.commit()
    return RedirectResponse(
        f"/products/{product_id}?message={quote('已恢复自动图片优先级')}",
        status_code=303,
    )


@app.post("/products/{product_id}/archive")
def product_archive_page(
    product_id: int,
    actor: str = Form("人工操作"),
    reason: str = Form(""),
    return_to: str = Form(""),
    db: Session = Depends(get_db),
):
    product = db.get(Product, product_id)
    if not product:
        raise HTTPException(404, "商品不存在")
    archive_product(db, product, actor=actor, reason=reason)
    target = return_to if return_to.startswith("/") and not return_to.startswith("//") else f"/products/{product_id}"
    return RedirectResponse(f"{target}?message={quote('商品已停用/归档')}", status_code=303)


@app.post("/products/{product_id}/restore")
def product_restore_page(
    product_id: int,
    actor: str = Form("人工操作"),
    reason: str = Form(""),
    return_to: str = Form(""),
    db: Session = Depends(get_db),
):
    product = db.get(Product, product_id)
    if not product:
        raise HTTPException(404, "商品不存在")
    restore_product(db, product, actor=actor, reason=reason)
    target = return_to if return_to.startswith("/") and not return_to.startswith("//") else f"/products/{product_id}"
    return RedirectResponse(f"{target}?message={quote('商品已恢复启用')}", status_code=303)


@app.post("/products/{product_id}/delete")
def product_delete_page(
    product_id: int,
    actor: str = Form("人工操作"),
    reason: str = Form(""),
    confirm_delete: str = Form(""),
    db: Session = Depends(get_db),
):
    product = db.get(Product, product_id)
    if not product:
        raise HTTPException(404, "商品不存在")
    if confirm_delete != "1":
        return RedirectResponse(f"/products/{product_id}?error={quote('请二次确认后再删除商品')}", status_code=303)
    try:
        delete_product_if_allowed(db, product, actor=actor, reason=reason)
    except ValueError as exc:
        db.rollback()
        return RedirectResponse(f"/products/{product_id}?error={quote(str(exc))}", status_code=303)
    return RedirectResponse(f"/products?message={quote('商品已物理删除')}", status_code=303)


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
        if "qinsi_product_code" in fields:
            update_product_identifiers(
                db, product,
                jan=data.jan if "jan" in fields else product.jan,
                qinsi_product_code=data.qinsi_product_code,
            )
        update_product_master(
            db,
            product,
            name_cn=data.name_cn if "name_cn" in fields else product.name_cn,
            name_ja=data.name_ja if "name_ja" in fields else product.name_ja,
            jan=data.jan if "jan" in fields else product.jan,
            image_url=data.main_image_source_url if "main_image_source_url" in fields else product.main_image_source_url,
            purchase_price=data.purchase_price if "purchase_price" in fields else product.purchase_price,
            status=data.status if "status" in fields and data.status else product.status,
            actor="API",
            reason="api_update",
        )
        if product.main_image_source_url:
            wake_image_localization_worker()
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

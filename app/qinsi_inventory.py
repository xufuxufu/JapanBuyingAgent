from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.models import (
    Location,
    Product,
    ProductWatchConfig,
    PurchaseBatch,
    PurchaseBatchItem,
    QinsiInventorySnapshot,
    QinsiInventorySnapshotLine,
    QinsiProductMapping,
    QinsiPurchaseExportLine,
    QinsiPurchaseExportLineSource,
)
from app.qinsi_import import read_product_sheet


BASE_HEADERS = {
    "名称（必填）", "名称(必填)", "商品规格", "货号（必填且唯一）", "货号(必填且唯一)",
    "条码", "型号规格", "品牌", "分类", "单位", "采购价", "销售价", "最低销售价", "排序",
    "状态", "启用积分", "库存预警下限", "库存预警上限", "保质期（天）", "启用批次",
    "过期预警（天）", "商品图片链接", "商品备注", "产地", "适用年龄", "商品重量（KG）",
    "启用序列号", "库位", "盘点库存数量", "当前库存（导入时不需要录入）", "盘点仓库:",
    "内部SKU", "internal_sku",
}
INVENTORY_STATUS_LABELS = {
    "no_snapshot": "无快照",
    "snapshot_stale": "快照已过期",
    "stock_available": "库存充足",
    "stock_low": "库存偏低",
    "out_of_stock": "快照数量为零",
    "incoming_or_pending": "有待提交或待确认采购",
    "review_needed": "需要人工复核",
}
MATCH_STATUS_LABELS = {"matched": "已匹配", "unmatched": "未匹配", "conflict": "冲突", "ignored": "已忽略"}
MATCH_METHOD_LABELS = {
    "qinsi_product_code": "秦丝商品编码",
    "jan": "JAN",
    "confirmed_mapping": "已确认映射",
    "internal_sku": "内部SKU",
    "manual": "人工选择",
}


@dataclass(frozen=True, slots=True)
class InventorySettings:
    stale_hours: int
    default_low_stock_threshold: int
    max_upload_bytes: int
    allowed_extensions: tuple[str, ...]
    reuse_duplicate_file: bool
    purchase_assistance_enabled: bool


@dataclass(frozen=True, slots=True)
class WarehouseStock:
    warehouse: Location
    quantity: int


@dataclass(frozen=True, slots=True)
class ProductInventoryView:
    snapshot: QinsiInventorySnapshot | None
    warehouses: tuple[WarehouseStock, ...]
    total_quantity: int | None
    data_time: datetime | None
    is_stale: bool
    stale_hours: int
    review_needed: bool = False


@dataclass(frozen=True, slots=True)
class PurchaseAssistance:
    status: str
    base_status: str
    message: str
    low_stock_threshold: int
    pending_quantity: int
    awaiting_confirmation_quantity: int
    has_incoming_or_pending: bool
    target_reached: bool


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        return default
    return min(maximum, max(minimum, value))


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().casefold() in {"1", "true", "yes", "on"}


def inventory_settings() -> InventorySettings:
    extensions = tuple(
        item if item.startswith(".") else f".{item}"
        for item in (part.strip().lower() for part in os.getenv("JBA_QINSI_SNAPSHOT_EXTENSIONS", ".xlsx").split(","))
        if item
    ) or (".xlsx",)
    return InventorySettings(
        stale_hours=_env_int("JBA_QINSI_SNAPSHOT_STALE_HOURS", 72, 1, 8760),
        default_low_stock_threshold=_env_int("JBA_QINSI_DEFAULT_LOW_STOCK_THRESHOLD", 3, 1, 1000000),
        max_upload_bytes=_env_int("JBA_QINSI_SNAPSHOT_MAX_UPLOAD_MB", 20, 1, 20) * 1024 * 1024,
        allowed_extensions=extensions,
        reuse_duplicate_file=_env_bool("JBA_QINSI_REUSE_DUPLICATE_FILE", True),
        purchase_assistance_enabled=_env_bool("JBA_PURCHASE_ASSISTANCE_ENABLED", True),
    )


def _text(value: str | None) -> str | None:
    value = (value or "").strip()
    if not value:
        return None
    if re.search(r"[Ee][+-]?\d+", value):
        return value
    return value[:-2] if value.endswith(".0") and value[:-2].isdigit() else value


def _quantity(value: str | None) -> int | None:
    text = (value or "").strip().replace(",", "")
    if not text:
        return None
    try:
        number = float(text)
    except ValueError as exc:
        raise ValueError("账面数量不是整数") from exc
    if not number.is_integer():
        raise ValueError("账面数量不是整数")
    return int(number)


def _product_fields(raw: dict[str, str]) -> tuple[str | None, str | None, str | None, str | None]:
    name = _text(raw.get("名称（必填）") or raw.get("名称(必填)"))
    code = _text(raw.get("货号（必填且唯一）") or raw.get("货号(必填且唯一)"))
    jan = _text(raw.get("条码"))
    internal_sku = _text(raw.get("内部SKU") or raw.get("internal_sku"))
    return name, code, jan, internal_sku


def _inventory_entries(raw: dict[str, str]) -> list[tuple[str | None, str | None]]:
    explicit_warehouse = _text(raw.get("盘点仓库:"))
    counted = raw.get("盘点库存数量", "")
    current = raw.get("当前库存（导入时不需要录入）", "")
    if explicit_warehouse:
        return [(explicit_warehouse, counted or current)]
    dynamic = [(header.strip(), value) for header, value in raw.items() if header not in BASE_HEADERS and header.strip()]
    populated = [(header, value) for header, value in dynamic if (value or "").strip()]
    if populated:
        return populated
    if len(dynamic) == 1 and (counted or current).strip():
        return [(dynamic[0][0], counted or current)]
    return [(dynamic[0][0] if len(dynamic) == 1 else None, counted or current)]


def _match_product(
    session: Session, *, code: str | None, jan: str | None, internal_sku: str | None,
) -> tuple[Product | None, str | None, str]:
    if code:
        product = session.scalar(select(Product).where(Product.qinsi_product_code == code))
        if product is not None:
            return product, "qinsi_product_code", "matched"
    if jan:
        product = session.scalar(select(Product).where(Product.jan == jan))
        if product is not None:
            return product, "jan", "matched"
    if code:
        mapping = session.scalar(
            select(QinsiProductMapping)
            .where(QinsiProductMapping.qinsi_product_code == code)
            .options(selectinload(QinsiProductMapping.product))
        )
        if mapping is not None:
            return mapping.product, "confirmed_mapping", "matched"
    if internal_sku:
        product = session.scalar(select(Product).where(Product.internal_sku == internal_sku))
        if product is not None:
            return product, "internal_sku", "matched"
    return None, None, "unmatched"


def _warehouse(session: Session, name: str | None) -> Location | None:
    if not name:
        return None
    return session.scalar(select(Location).where(
        Location.display_name == name,
        Location.is_qinsi_warehouse.is_(True),
        Location.is_active.is_(True),
    ))


def _refresh_snapshot_summary(snapshot: QinsiInventorySnapshot) -> None:
    lines = list(snapshot.lines)
    snapshot.total_rows = len(lines)
    snapshot.success_rows = sum(
        line.matching_status == "matched" and line.warehouse_id is not None and line.quantity is not None
        for line in lines
    )
    snapshot.unmatched_rows = sum(line.matching_status == "unmatched" for line in lines)
    snapshot.exception_rows = sum(
        line.matching_status == "conflict" or line.warehouse_status != "matched" or line.quantity is None
        for line in lines if line.matching_status != "ignored"
    )
    errors = list(dict.fromkeys(line.error_message for line in lines if line.error_message))
    snapshot.error_summary = "；".join(errors)[:1000] or None
    snapshot.status = "completed_with_issues" if snapshot.unmatched_rows or snapshot.exception_rows else "completed"


def create_inventory_snapshot(
    session: Session,
    filename: str,
    content: bytes,
    *,
    data_at: datetime | None = None,
    now: datetime | None = None,
) -> tuple[QinsiInventorySnapshot, bool]:
    settings = inventory_settings()
    extension = Path(filename or "").suffix.lower()
    if extension not in settings.allowed_extensions:
        raise ValueError(f"只允许上传：{', '.join(settings.allowed_extensions)}")
    if not content or len(content) > settings.max_upload_bytes:
        raise ValueError("Excel文件为空或超过允许大小")
    file_hash = hashlib.sha256(content).hexdigest()
    existing = session.scalar(select(QinsiInventorySnapshot).where(QinsiInventorySnapshot.file_hash == file_hash))
    if existing is not None:
        if settings.reuse_duplicate_file:
            return existing, True
        raise ValueError("该文件已导入，当前配置禁止重复文件复用")
    rows = read_product_sheet(content)
    imported_at = now or datetime.now(timezone.utc)
    snapshot = QinsiInventorySnapshot(
        batch_no=f"QS-{imported_at:%Y%m%d}-{uuid.uuid4().hex[:10].upper()}",
        original_filename=Path(filename).name[:255],
        file_hash=file_hash,
        file_content=content,
        imported_at=imported_at,
        data_at=data_at,
        status="completed",
    )
    session.add(snapshot)
    session.flush()
    for row_no, raw in rows:
        name, code, jan, internal_sku = _product_fields(raw)
        if not any((name, code, jan, internal_sku)):
            continue
        product, method, matching_status = _match_product(
            session, code=code, jan=jan, internal_sku=internal_sku,
        )
        for warehouse_name, raw_quantity in _inventory_entries(raw):
            errors: list[str] = []
            try:
                quantity = _quantity(raw_quantity)
            except ValueError as exc:
                quantity = None
                errors.append(str(exc))
            if quantity is None:
                errors.append("账面数量为空")
            warehouse = _warehouse(session, warehouse_name)
            if warehouse is None:
                errors.append(f"未知秦丝仓库：{warehouse_name or '未提供'}")
            if matching_status == "unmatched":
                errors.append("未匹配到本地商品")
            summary = {key: value for key, value in raw.items() if (value or "").strip()}
            line = QinsiInventorySnapshotLine(
                snapshot_id=snapshot.id,
                original_row_no=row_no,
                raw_product_name=name,
                jan=jan,
                qinsi_product_code=code,
                internal_sku=internal_sku,
                raw_warehouse_name=warehouse_name,
                quantity=quantity,
                raw_summary_json=json.dumps(summary, ensure_ascii=False),
                product_id=product.id if product else None,
                warehouse_id=warehouse.id if warehouse else None,
                matching_method=method,
                matching_status=matching_status,
                warehouse_status="matched" if warehouse else "unknown",
                error_message="；".join(dict.fromkeys(errors)) or None,
            )
            snapshot.lines.append(line)
    _refresh_snapshot_summary(snapshot)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        existing = session.scalar(select(QinsiInventorySnapshot).where(QinsiInventorySnapshot.file_hash == file_hash))
        if existing is not None:
            return existing, True
        raise
    session.refresh(snapshot)
    return snapshot, False


def list_inventory_snapshots(session: Session) -> list[QinsiInventorySnapshot]:
    return list(session.scalars(
        select(QinsiInventorySnapshot).order_by(QinsiInventorySnapshot.imported_at.desc(), QinsiInventorySnapshot.id.desc())
    ))


def get_inventory_snapshot(session: Session, snapshot_id: int) -> QinsiInventorySnapshot | None:
    return session.scalar(
        select(QinsiInventorySnapshot)
        .where(QinsiInventorySnapshot.id == snapshot_id)
        .options(
            selectinload(QinsiInventorySnapshot.lines).selectinload(QinsiInventorySnapshotLine.product),
            selectinload(QinsiInventorySnapshot.lines).selectinload(QinsiInventorySnapshotLine.warehouse),
        )
    )


def manual_match_line(session: Session, line_id: int, product_id: int) -> QinsiInventorySnapshotLine:
    line = session.get(QinsiInventorySnapshotLine, line_id)
    product = session.get(Product, product_id)
    if line is None:
        raise LookupError("快照明细不存在")
    if product is None:
        raise LookupError("商品不存在")
    line.product_id = product.id
    line.matching_method = "manual"
    line.matching_status = "matched"
    if line.qinsi_product_code:
        mapping = session.scalar(select(QinsiProductMapping).where(
            QinsiProductMapping.qinsi_product_code == line.qinsi_product_code
        ))
        if mapping is None:
            mapping = QinsiProductMapping(qinsi_product_code=line.qinsi_product_code, product_id=product.id)
            session.add(mapping)
        else:
            mapping.product_id = product.id
            mapping.confirmed_at = datetime.now(timezone.utc)
        mapping.source_snapshot_line_id = line.id
    line.error_message = _line_errors_without(line, "未匹配到本地商品")
    snapshot = line.snapshot
    _refresh_snapshot_summary(snapshot)
    session.commit()
    session.refresh(line)
    return line


def _line_errors_without(line: QinsiInventorySnapshotLine, text: str) -> str | None:
    values = [value for value in (line.error_message or "").split("；") if value and value != text]
    return "；".join(values) or None


def map_line_warehouse(session: Session, line_id: int, warehouse_id: int) -> QinsiInventorySnapshotLine:
    line = session.get(QinsiInventorySnapshotLine, line_id)
    warehouse = session.get(Location, warehouse_id)
    if line is None:
        raise LookupError("快照明细不存在")
    if warehouse is None or not warehouse.is_active or not warehouse.is_qinsi_warehouse:
        raise ValueError("只能映射到已有且启用的秦丝仓库")
    line.warehouse_id = warehouse.id
    line.warehouse_status = "matched"
    errors = [value for value in (line.error_message or "").split("；") if not value.startswith("未知秦丝仓库：")]
    line.error_message = "；".join(errors) or None
    snapshot = line.snapshot
    _refresh_snapshot_summary(snapshot)
    session.commit()
    session.refresh(line)
    return line


def ignore_snapshot_lines(session: Session, snapshot_id: int, line_ids: set[int]) -> int:
    lines = list(session.scalars(select(QinsiInventorySnapshotLine).where(
        QinsiInventorySnapshotLine.snapshot_id == snapshot_id,
        QinsiInventorySnapshotLine.id.in_(line_ids),
        QinsiInventorySnapshotLine.matching_status.in_(("unmatched", "conflict")),
    ))) if line_ids else []
    for line in lines:
        line.matching_status = "ignored"
        line.product_id = None
        line.matching_method = None
    snapshot = session.get(QinsiInventorySnapshot, snapshot_id)
    if snapshot is None:
        raise LookupError("库存快照不存在")
    _refresh_snapshot_summary(snapshot)
    session.commit()
    return len(lines)


def retry_snapshot_matching(session: Session, snapshot_id: int) -> int:
    snapshot = get_inventory_snapshot(session, snapshot_id)
    if snapshot is None:
        raise LookupError("库存快照不存在")
    matched = 0
    for line in snapshot.lines:
        if line.matching_status not in {"unmatched", "conflict"}:
            continue
        product, method, status = _match_product(
            session, code=line.qinsi_product_code, jan=line.jan, internal_sku=line.internal_sku,
        )
        if product is not None:
            line.product_id = product.id
            line.matching_method = method
            line.matching_status = status
            line.error_message = _line_errors_without(line, "未匹配到本地商品")
            matched += 1
    _refresh_snapshot_summary(snapshot)
    session.commit()
    return matched


def available_qinsi_warehouses(session: Session) -> list[Location]:
    return list(session.scalars(select(Location).where(
        Location.is_qinsi_warehouse.is_(True), Location.is_active.is_(True),
    ).order_by(Location.sort_order, Location.id)))


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def latest_inventory_for_product(
    session: Session, product_id: int, *, now: datetime | None = None,
) -> ProductInventoryView:
    settings = inventory_settings()
    snapshot = session.scalar(
        select(QinsiInventorySnapshot)
        .join(QinsiInventorySnapshotLine, QinsiInventorySnapshotLine.snapshot_id == QinsiInventorySnapshot.id)
        .where(
            QinsiInventorySnapshotLine.product_id == product_id,
            QinsiInventorySnapshotLine.matching_status == "matched",
        )
        .order_by(func.coalesce(QinsiInventorySnapshot.data_at, QinsiInventorySnapshot.imported_at).desc(), QinsiInventorySnapshot.id.desc())
        .limit(1)
    )
    if snapshot is None:
        return ProductInventoryView(None, (), None, None, False, settings.stale_hours)
    lines = list(session.scalars(
        select(QinsiInventorySnapshotLine)
        .where(
            QinsiInventorySnapshotLine.snapshot_id == snapshot.id,
            QinsiInventorySnapshotLine.product_id == product_id,
            QinsiInventorySnapshotLine.matching_status == "matched",
        )
        .options(selectinload(QinsiInventorySnapshotLine.warehouse))
    ))
    quantities: dict[int, tuple[Location, int]] = {}
    review_needed = False
    for line in lines:
        if line.warehouse is None or line.quantity is None:
            review_needed = True
            continue
        previous = quantities.get(line.warehouse.id, (line.warehouse, 0))
        quantities[line.warehouse.id] = (line.warehouse, previous[1] + line.quantity)
    warehouses = tuple(
        WarehouseStock(warehouse, quantity)
        for warehouse, quantity in sorted(quantities.values(), key=lambda value: (value[0].sort_order, value[0].id))
    )
    total = sum(item.quantity for item in warehouses)
    data_time = snapshot.data_at or snapshot.imported_at
    current = now or datetime.now(timezone.utc)
    stale = _aware(current) - _aware(data_time) > timedelta(hours=settings.stale_hours)
    return ProductInventoryView(snapshot, warehouses, total, data_time, stale, settings.stale_hours, review_needed)


def _pending_quantities(session: Session, product_id: int) -> tuple[int, int]:
    items = list(session.scalars(
        select(PurchaseBatchItem)
        .join(PurchaseBatch, PurchaseBatch.id == PurchaseBatchItem.purchase_batch_id)
        .where(PurchaseBatchItem.product_id == product_id, PurchaseBatch.status != "cancelled")
    ))
    if not items:
        return 0, 0
    active_lines = list(session.scalars(
        select(QinsiPurchaseExportLine)
        .join(QinsiPurchaseExportLineSource, QinsiPurchaseExportLineSource.export_line_id == QinsiPurchaseExportLine.id)
        .where(
            QinsiPurchaseExportLineSource.purchase_batch_item_id.in_([item.id for item in items]),
            QinsiPurchaseExportLineSource.is_active.is_(True),
        )
    ))
    by_item = {line.purchase_batch_item_id: line for line in active_lines}
    pending = sum(item.quantity for item in items if by_item.get(item.id) is None or by_item[item.id].status != "imported")
    awaiting = sum(item.quantity for item in items if by_item.get(item.id) is not None and by_item[item.id].status == "generated")
    return pending, awaiting


def purchase_assistance(
    session: Session,
    product: Product,
    *,
    inventory: ProductInventoryView | None = None,
    now: datetime | None = None,
) -> PurchaseAssistance:
    settings = inventory_settings()
    inventory = inventory or latest_inventory_for_product(session, product.id, now=now)
    threshold = product.low_stock_threshold or settings.default_low_stock_threshold
    pending, awaiting = _pending_quantities(session, product.id)
    watch = session.scalar(select(ProductWatchConfig).where(ProductWatchConfig.product_id == product.id))
    target_reached = bool(
        watch and watch.enabled and watch.current_lowest_price is not None
        and watch.effective_target_price is not None and watch.current_lowest_price <= watch.effective_target_price
    )
    if not settings.purchase_assistance_enabled:
        base_status, message = "review_needed", "采购辅助提示已关闭"
    elif inventory.snapshot is None:
        base_status, message = "no_snapshot", "暂无秦丝库存快照，请先导入后再判断"
    elif inventory.review_needed:
        base_status, message = "review_needed", "最近快照存在仓库或数量异常，需要人工复核"
    elif inventory.is_stale:
        base_status, message = "snapshot_stale", "秦丝库存快照已过期，请更新快照"
    elif inventory.total_quantity is not None and inventory.total_quantity <= 0:
        base_status, message = "out_of_stock", "秦丝最近快照数量为0"
    elif inventory.total_quantity is not None and inventory.total_quantity < threshold:
        base_status = "stock_low"
        message = "库存偏低且已达到目标价，可考虑补货" if target_reached else "秦丝最近快照库存偏低，建议结合价格人工判断"
    else:
        base_status, message = "stock_available", "秦丝最近快照库存充足，暂缓补货"
    has_incoming = pending > 0
    status = "incoming_or_pending" if has_incoming else base_status
    if has_incoming:
        message = f"{message}；另有{pending}件采购待提交或待确认，未计入秦丝快照库存"
    return PurchaseAssistance(
        status, base_status, message, threshold, pending, awaiting, has_incoming, target_reached,
    )


def update_product_low_stock_threshold(session: Session, product_id: int, value: int | str | None) -> Product:
    product = session.get(Product, product_id)
    if product is None:
        raise LookupError("商品不存在")
    if value in (None, ""):
        product.low_stock_threshold = None
    else:
        try:
            threshold = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("低库存阈值必须是正整数") from exc
        if threshold <= 0:
            raise ValueError("低库存阈值必须是正整数")
        product.low_stock_threshold = threshold
    session.commit()
    session.refresh(product)
    return product


def latest_snapshot_statistics(session: Session) -> tuple[QinsiInventorySnapshot | None, list[tuple[str, int, int]]]:
    snapshot = session.scalar(select(QinsiInventorySnapshot).order_by(
        func.coalesce(QinsiInventorySnapshot.data_at, QinsiInventorySnapshot.imported_at).desc(),
        QinsiInventorySnapshot.id.desc(),
    ).limit(1))
    if snapshot is None:
        return None, []
    rows = session.execute(
        select(Location.display_name, func.count(func.distinct(QinsiInventorySnapshotLine.product_id)), func.coalesce(func.sum(QinsiInventorySnapshotLine.quantity), 0))
        .join(QinsiInventorySnapshotLine, QinsiInventorySnapshotLine.warehouse_id == Location.id)
        .where(
            QinsiInventorySnapshotLine.snapshot_id == snapshot.id,
            QinsiInventorySnapshotLine.matching_status == "matched",
        )
        .group_by(Location.id, Location.display_name)
        .order_by(Location.sort_order, Location.id)
    ).all()
    return snapshot, [(name, product_count, quantity) for name, product_count, quantity in rows]


def watched_inventory_status_distribution(session: Session, *, now: datetime | None = None) -> dict[str, int]:
    result: dict[str, int] = {}
    products = list(session.scalars(
        select(Product).join(ProductWatchConfig, ProductWatchConfig.product_id == Product.id)
        .where(ProductWatchConfig.enabled.is_(True))
    ))
    for product in products:
        status = purchase_assistance(session, product, now=now).base_status
        result[status] = result.get(status, 0) + 1
    return result

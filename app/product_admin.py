from __future__ import annotations

import hashlib
import json
import mimetypes
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import func, inspect, select, update
from sqlalchemy.orm import Session

from app.models import (
    FieldPurchaseItem,
    InventoryTransaction,
    PriceLookupHistory,
    PriceSearchRun,
    ProductEnrichmentTask,
    ProductMatchLog,
    ProductOffer,
    Product,
    ProductOperationLog,
    ProductPlaceholderCleanupLog,
    ProductSerial,
    PurchaseBatchItem,
    QinsiExportLine,
    QinsiInventorySnapshotLine,
    QinsiProductMapping,
    QinsiPurchaseExportLine,
    ReceiptItem,
    RestockListItem,
)
from app.config import PRODUCT_IMAGE_DIR, PROJECT_ROOT
from app.local_product import is_valid_jan
from app.product_identity import format_product_display_name, normalize_product_name, update_product_identifiers
from app.product_specs import parse_product_specs
from app.product_image_localization import queue_product_image_localization


EDITABLE_PRODUCT_STATUSES = {
    "active",
    "new_pending_completion",
    "new_pending_review",
    "pending_qinsi_product_import",
    "qinsi_product_imported",
    "archived",
}


@dataclass(frozen=True, slots=True)
class ProductAssociationSummary:
    counts: dict[str, int]
    qinsi_imported: bool

    @property
    def has_business_links(self) -> bool:
        return self.qinsi_imported or any(self.counts.values())

    @property
    def can_delete(self) -> bool:
        return not self.has_business_links

    @property
    def labels(self) -> list[str]:
        names = {
            "receipt_items": "小票商品行",
            "purchase_batch_items": "采购批次明细",
            "qinsi_export_lines": "秦丝商品导出记录",
            "qinsi_purchase_export_lines": "秦丝采购导出记录",
            "qinsi_inventory_lines": "秦丝库存快照",
            "qinsi_product_mappings": "秦丝商品映射",
            "restock_list_items": "补货清单",
            "field_purchase_items": "现场采购草稿",
            "product_serials": "序列号",
            "inventory_transactions": "库存流水",
        }
        labels = [f"{names[key]} {value}" for key, value in self.counts.items() if value]
        if self.qinsi_imported:
            labels.append("已导入秦丝")
        return labels


def _count(session: Session, model, product_id: int) -> int:
    return session.scalar(select(func.count()).select_from(model).where(model.product_id == product_id)) or 0


def product_associations(session: Session, product: Product) -> ProductAssociationSummary:
    counts = {
        "receipt_items": _count(session, ReceiptItem, product.id),
        "purchase_batch_items": _count(session, PurchaseBatchItem, product.id),
        "qinsi_export_lines": _count(session, QinsiExportLine, product.id),
        "qinsi_purchase_export_lines": _count(session, QinsiPurchaseExportLine, product.id),
        "qinsi_inventory_lines": _count(session, QinsiInventorySnapshotLine, product.id),
        "qinsi_product_mappings": _count(session, QinsiProductMapping, product.id),
        "restock_list_items": _count(session, RestockListItem, product.id),
        "field_purchase_items": _count(session, FieldPurchaseItem, product.id),
        "product_serials": _count(session, ProductSerial, product.id),
        "inventory_transactions": _count(session, InventoryTransaction, product.id),
    }
    return ProductAssociationSummary(
        counts=counts,
        qinsi_imported=product.status == "qinsi_product_imported" or product.product_origin == "qinsi",
    )


def product_snapshot(product: Product) -> dict[str, Any]:
    return {
        "id": product.id,
        "internal_sku": product.internal_sku,
        "jan": product.jan,
        "name_cn": product.name_cn,
        "name_ja": product.name_ja,
        "display_name": product.display_name,
        "main_image_source_url": product.main_image_source_url,
        "image_url": product.image_url,
        "purchase_price": str(product.purchase_price) if product.purchase_price is not None else None,
        "status": product.status,
        "net_weight_g": str(product.net_weight_g) if product.net_weight_g is not None else None,
        "volume_ml": str(product.volume_ml) if product.volume_ml is not None else None,
        "length_mm": str(product.length_mm) if product.length_mm is not None else None,
        "width_mm": str(product.width_mm) if product.width_mm is not None else None,
        "height_mm": str(product.height_mm) if product.height_mm is not None else None,
        "depth_mm": str(product.depth_mm) if product.depth_mm is not None else None,
        "pack_quantity": product.pack_quantity,
        "spec_text": product.spec_text,
    }


def _log(
    session: Session,
    product: Product,
    *,
    action: str,
    actor: str,
    reason: str | None,
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
) -> None:
    session.add(ProductOperationLog(
        product_id=product.id,
        internal_sku=product.internal_sku,
        action=action,
        actor=(actor or "人工操作")[:128],
        reason=(reason or "").strip() or None,
        before_json=json.dumps(before, ensure_ascii=False, default=str) if before is not None else None,
        after_json=json.dumps(after, ensure_ascii=False, default=str) if after is not None else None,
    ))


def parse_reference_price(value: str | int | Decimal | None) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        price = value
    else:
        text = str(value).strip()
        if not text:
            return None
        try:
            price = Decimal(text)
        except InvalidOperation as exc:
            raise ValueError("参考价格必须是数字") from exc
    if price < 0:
        raise ValueError("参考价格不能为负数")
    return price.quantize(Decimal("0.01"))


def parse_optional_decimal(value: str | int | Decimal | None, label: str) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        amount = value
    else:
        text = str(value).strip()
        if not text:
            return None
        try:
            amount = Decimal(text)
        except InvalidOperation as exc:
            raise ValueError(f"{label}必须是数字") from exc
    if amount < 0:
        raise ValueError(f"{label}不能为负数")
    return amount.quantize(Decimal("0.001"))


def parse_optional_int(value: str | int | None, label: str) -> int | None:
    text = "" if value is None else str(value).strip()
    if not text:
        return None
    try:
        amount = int(text)
    except ValueError as exc:
        raise ValueError(f"{label}必须是整数") from exc
    if amount < 1:
        raise ValueError(f"{label}必须大于 0")
    return amount


def _set_structured_specs(
    product: Product,
    *,
    net_weight_g: str | Decimal | None,
    volume_ml: str | Decimal | None,
    length_mm: str | Decimal | None,
    width_mm: str | Decimal | None,
    height_mm: str | Decimal | None,
    depth_mm: str | Decimal | None,
    pack_quantity: str | int | None,
    spec_text: str | None,
) -> None:
    product.net_weight_g = parse_optional_decimal(net_weight_g, "净重g")
    product.volume_ml = parse_optional_decimal(volume_ml, "容量ml")
    product.length_mm = parse_optional_decimal(length_mm, "长mm")
    product.width_mm = parse_optional_decimal(width_mm, "宽mm")
    product.height_mm = parse_optional_decimal(height_mm, "高mm")
    product.depth_mm = parse_optional_decimal(depth_mm, "深mm")
    product.pack_quantity = parse_optional_int(pack_quantity, "套装/个数")
    product.spec_text = (spec_text or "").strip()[:255] or None


def update_product_master(
    session: Session,
    product: Product,
    *,
    name_cn: str | None,
    name_ja: str | None,
    jan: str | None,
    image_url: str | None,
    purchase_price: str | int | Decimal | None,
    status: str,
    actor: str,
    specification: str | None = None,
    net_weight_g: str | Decimal | None = None,
    volume_ml: str | Decimal | None = None,
    length_mm: str | Decimal | None = None,
    width_mm: str | Decimal | None = None,
    height_mm: str | Decimal | None = None,
    depth_mm: str | Decimal | None = None,
    pack_quantity: str | int | None = None,
    spec_text: str | None = None,
    reason: str | None = None,
) -> Product:
    if status not in EDITABLE_PRODUCT_STATUSES:
        raise ValueError("商品状态无效")
    before = product_snapshot(product)
    previous_image_url = product.main_image_source_url
    update_product_identifiers(session, product, jan=jan, qinsi_product_code=product.qinsi_product_code)
    product.name_cn = normalize_product_name(name_cn, "中文名")
    product.name_ja = normalize_product_name(name_ja, "日文名")
    product.display_name = format_product_display_name(product.name_cn, product.name_ja)
    product.main_image_source_url = (image_url or "").strip() or None
    product.purchase_price = parse_reference_price(purchase_price)
    product.specification = (specification or "").strip()[:255] or None
    _set_structured_specs(
        product,
        net_weight_g=net_weight_g,
        volume_ml=volume_ml,
        length_mm=length_mm,
        width_mm=width_mm,
        height_mm=height_mm,
        depth_mm=depth_mm,
        pack_quantity=pack_quantity,
        spec_text=spec_text,
    )
    product.status = status
    product.product_data_confirmed = True
    product.name_locked = True
    if product.main_image_source_url and product.main_image_source_url != previous_image_url:
        queue_product_image_localization(session, product)
    _log(session, product, action="edit", actor=actor, reason=reason, before=before, after=product_snapshot(product))
    session.commit()
    session.refresh(product)
    return product


def save_product_photo_for_completion(
    session: Session,
    product: Product,
    *,
    content: bytes,
    content_type: str,
    original_filename: str,
    name_cn: str | None = None,
    name_ja: str | None = None,
    spec_text: str | None = None,
    actor: str,
) -> Product:
    if not is_valid_jan(product.jan):
        raise ValueError("只有合法 JAN 商品可以上传商品照片补资料")
    if not content:
        raise ValueError("商品照片不能为空")
    extension = mimetypes.guess_extension((content_type or "").split(";", 1)[0]) or ""
    if extension.lower() not in {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif"}:
        suffix = "." + (original_filename or "").rsplit(".", 1)[-1].casefold() if "." in (original_filename or "") else ""
        extension = suffix if suffix in {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif"} else ".jpg"
    digest = hashlib.sha256(content).hexdigest()
    PRODUCT_IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    destination = PRODUCT_IMAGE_DIR / f"{digest}{extension}"
    if not destination.exists():
        destination.write_bytes(content)

    before = product_snapshot(product)
    candidate_cn = normalize_product_name(name_cn, "中文名")
    candidate_ja = normalize_product_name(name_ja, "日文名")
    if candidate_cn and not product.name_cn:
        product.name_cn = candidate_cn
    existing_ja = normalize_product_name(product.name_ja, "日文名")
    if candidate_ja and (not existing_ja or existing_ja in PLACEHOLDER_EXACT_NAMES):
        product.name_ja = candidate_ja
    if candidate_cn or candidate_ja:
        product.display_name = format_product_display_name(product.name_cn, product.name_ja)
        product.name_source = "photo"
        product.needs_review = True
    parsed = parse_product_specs(spec_text)
    if spec_text:
        product.specification = product.specification or spec_text.strip()[:255]
        for field, value in parsed.as_dict().items():
            if value is not None and getattr(product, field, None) is None:
                setattr(product, field, value)
    product.main_image_path = destination.resolve().relative_to(PROJECT_ROOT.resolve()).as_posix()
    product.main_image_hash = digest
    product.main_image_locked = True
    product.display_image_url = f"/product-images/{product.id}"
    product.source = "photo"
    if product.status == "new_pending_completion" and (product.name_cn or product.name_ja):
        product.status = "new_pending_review"
    _log(
        session,
        product,
        action="edit",
        actor=actor,
        reason="上传商品照片补资料",
        before=before,
        after=product_snapshot(product),
    )
    session.commit()
    session.refresh(product)
    return product


def archive_product(session: Session, product: Product, *, actor: str, reason: str | None) -> Product:
    if product.status == "archived":
        return product
    before = product_snapshot(product)
    product.status = "archived"
    _log(session, product, action="archive", actor=actor, reason=reason, before=before, after=product_snapshot(product))
    session.commit()
    session.refresh(product)
    return product


def restore_product(session: Session, product: Product, *, actor: str, reason: str | None, status: str = "active") -> Product:
    if product.status != "archived":
        return product
    before = product_snapshot(product)
    product.status = status if status in EDITABLE_PRODUCT_STATUSES and status != "archived" else "active"
    _log(session, product, action="restore", actor=actor, reason=reason, before=before, after=product_snapshot(product))
    session.commit()
    session.refresh(product)
    return product


def delete_product_if_allowed(session: Session, product: Product, *, actor: str, reason: str | None) -> None:
    associations = product_associations(session, product)
    if not associations.can_delete:
        raise ValueError("商品已有业务关联或已导入秦丝，不能物理删除，请停用/归档")
    before = product_snapshot(product)
    _log(session, product, action="delete", actor=actor, reason=reason, before=before, after=None)
    session.delete(product)
    session.commit()


PLACEHOLDER_CLEANUP_REASON = "replace_placeholder_with_qinsi_product"
PLACEHOLDER_EXACT_NAMES = {
    "缺商品",
    "新商品待补全",
    "待补全",
    "待补",
    "未命名",
    "中文名待补",
    "日文名待补",
}
TEMPORARY_SOURCES = {"receipt", "scan", "field_purchase", "field_scan", "price_scan"}
SUPPORTED_MIGRATION_MODELS = (
    ReceiptItem,
    PurchaseBatchItem,
    QinsiExportLine,
    QinsiPurchaseExportLine,
    FieldPurchaseItem,
    InventoryTransaction,
)
SUPPORTED_ASSOCIATION_KEYS = {
    "receipt_items",
    "purchase_batch_items",
    "qinsi_export_lines",
    "qinsi_purchase_export_lines",
    "field_purchase_items",
    "inventory_transactions",
}
DERIVED_PRODUCT_LINK_MODELS = (
    PriceSearchRun,
    PriceLookupHistory,
    ProductEnrichmentTask,
    ProductOffer,
)


@dataclass(frozen=True, slots=True)
class PlaceholderCleanupRow:
    old_product_id: int
    jan: str
    placeholder_name: str
    purchase_association_count: int
    business_association_count: int
    blocking_association_count: int
    new_product_id: int | None
    qinsi_product_name: str | None
    qinsi_goods_no: str | None
    formal_candidate_count: int
    action: str


@dataclass(frozen=True, slots=True)
class PlaceholderCleanupPreview:
    rows: list[PlaceholderCleanupRow]

    @property
    def temporary_product_count(self) -> int:
        return len(self.rows)

    @property
    def direct_delete_count(self) -> int:
        return sum(row.action == "delete_unlinked" for row in self.rows)

    @property
    def purchase_needs_migration_count(self) -> int:
        return sum(
            row.purchase_association_count > 0 and row.formal_candidate_count == 1
            for row in self.rows
        )

    @property
    def auto_migrate_count(self) -> int:
        return sum(row.action == "migrate_and_delete" for row in self.rows)

    @property
    def multiple_formal_count(self) -> int:
        return sum(row.action == "manual_multiple_formal" for row in self.rows)

    @property
    def no_formal_count(self) -> int:
        return sum(row.action == "keep_no_formal" for row in self.rows)

    @property
    def manual_count(self) -> int:
        return sum(row.action.startswith("manual_") for row in self.rows)

    @property
    def estimated_delete_count(self) -> int:
        return sum(row.action in {"delete_unlinked", "migrate_and_delete"} for row in self.rows)

    @property
    def estimated_migration_association_count(self) -> int:
        return sum(
            row.purchase_association_count
            for row in self.rows
            if row.action == "migrate_and_delete"
        )

    @property
    def stats(self) -> dict[str, int]:
        return {
            "temporary_product_count": self.temporary_product_count,
            "direct_delete_count": self.direct_delete_count,
            "purchase_needs_migration_count": self.purchase_needs_migration_count,
            "auto_migrate_count": self.auto_migrate_count,
            "multiple_formal_count": self.multiple_formal_count,
            "no_formal_count": self.no_formal_count,
            "estimated_delete_count": self.estimated_delete_count,
            "estimated_migration_association_count": self.estimated_migration_association_count,
            "manual_count": self.manual_count,
        }


@dataclass(frozen=True, slots=True)
class PlaceholderCleanupResult:
    preview: PlaceholderCleanupPreview
    migrated_product_count: int
    deleted_product_count: int
    migrated_association_count: int
    manual_count: int


def _display_name(product: Product) -> str:
    return product.display_name or product.name_cn or product.name_ja or ""


def _normalized_names(product: Product) -> list[str]:
    values = [product.display_name, product.name_cn, product.name_ja]
    return [value.strip() for value in values if value and value.strip()]


def _is_placeholder_name(product: Product) -> bool:
    names = _normalized_names(product)
    if not names:
        return False
    jan = (product.jan or "").strip()
    for name in names:
        if name == jan:
            return True
        if name in PLACEHOLDER_EXACT_NAMES:
            return True
        parts = [part.strip() for part in name.replace("｜", "|").split("|") if part.strip()]
        if parts and all(part == jan or part in PLACEHOLDER_EXACT_NAMES for part in parts):
            return True
        if "缺商品" in name or "待补全占位" in name:
            return True
    return False


def _has_rich_manual_identity(product: Product) -> bool:
    has_specific_name = bool(_normalized_names(product)) and not _is_placeholder_name(product)
    has_image = any((
        product.main_image_path,
        product.main_image_source_url,
        product.image_url,
        product.display_image_url,
        product.local_image_path,
    ))
    has_specs = any((
        product.specification,
        product.spec_text,
        product.model_spec,
        product.brand,
        product.manufacturer,
        product.category,
        product.capacity,
        product.color,
        product.model_number,
        product.package_count,
    ))
    return has_specific_name and (has_image or has_specs or product.product_data_confirmed or product.name_locked)


def _is_temporary_placeholder(product: Product) -> bool:
    if product.status == "qinsi_product_imported":
        return False
    if product.qinsi_product_code:
        return False
    if not is_valid_jan(product.jan):
        return False
    if _has_rich_manual_identity(product):
        return False
    if _is_placeholder_name(product):
        return True
    source = (product.source or "").strip()
    origin = (product.product_origin or "").strip()
    return product.needs_review and (source in TEMPORARY_SOURCES or origin in TEMPORARY_SOURCES)


def _has_qinsi_mapping(session: Session, product_id: int, qinsi_goods_no: str | None) -> bool:
    if not qinsi_goods_no:
        return False
    return bool(session.scalar(
        select(func.count())
        .select_from(QinsiProductMapping)
        .where(QinsiProductMapping.product_id == product_id, QinsiProductMapping.qinsi_product_code == qinsi_goods_no)
    ))


def _is_formal_qinsi_product(session: Session, product: Product) -> bool:
    if product.status != "qinsi_product_imported" or not product.qinsi_product_code:
        return False
    if product.product_origin == "qinsi" or product.source in {"qinsi_import", "qinsi_sync", "qinsi"}:
        return True
    return _has_qinsi_mapping(session, product.id, product.qinsi_product_code)


def _formal_qinsi_products_for_jan(session: Session, jan: str, old_product_id: int) -> list[Product]:
    candidates = list(session.scalars(
        select(Product)
        .where(Product.jan == jan, Product.id != old_product_id, Product.status == "qinsi_product_imported")
        .order_by(Product.id)
    ))
    return [product for product in candidates if _is_formal_qinsi_product(session, product)]


def _supported_association_count(session: Session, product_id: int) -> int:
    return sum(_count(session, model, product_id) for model in SUPPORTED_MIGRATION_MODELS)


def _business_counts(session: Session, product: Product) -> dict[str, int]:
    return product_associations(session, product).counts


def _blocking_association_count(session: Session, product: Product) -> int:
    counts = _business_counts(session, product)
    return sum(value for key, value in counts.items() if key not in SUPPORTED_ASSOCIATION_KEYS)


def _cleanup_action(formal_count: int, supported_count: int, blocking_count: int) -> str:
    if formal_count == 0:
        return "keep_no_formal"
    if formal_count > 1:
        return "manual_multiple_formal"
    if blocking_count:
        return "manual_blocking_links"
    if supported_count:
        return "migrate_and_delete"
    return "delete_unlinked"


def preview_placeholder_cleanup(session: Session) -> PlaceholderCleanupPreview:
    products = list(session.scalars(select(Product).where(Product.jan.is_not(None)).order_by(Product.id)))
    rows: list[PlaceholderCleanupRow] = []
    for product in products:
        if not _is_temporary_placeholder(product):
            continue
        jan = product.jan or ""
        formals = _formal_qinsi_products_for_jan(session, jan, product.id)
        supported_count = _supported_association_count(session, product.id)
        blocking_count = _blocking_association_count(session, product)
        formal = formals[0] if len(formals) == 1 else None
        rows.append(PlaceholderCleanupRow(
            old_product_id=product.id,
            jan=jan,
            placeholder_name=_display_name(product) or "缺商品",
            purchase_association_count=supported_count,
            business_association_count=supported_count + blocking_count,
            blocking_association_count=blocking_count,
            new_product_id=formal.id if formal is not None else None,
            qinsi_product_name=(formal.qinsi_name or formal.name_cn or formal.name_ja or formal.display_name) if formal is not None else None,
            qinsi_goods_no=formal.qinsi_product_code if formal is not None else None,
            formal_candidate_count=len(formals),
            action=_cleanup_action(len(formals), supported_count, blocking_count),
        ))
    return PlaceholderCleanupPreview(rows)


def _migrate_supported_product_links(session: Session, old_product_id: int, new_product_id: int) -> int:
    migrated = 0
    for model in SUPPORTED_MIGRATION_MODELS:
        result = session.execute(
            update(model)
            .where(model.product_id == old_product_id)
            .values(product_id=new_product_id)
        )
        migrated += result.rowcount or 0
    return migrated


def _migrate_derived_product_links(session: Session, old_product_id: int, new_product_id: int) -> None:
    for model in DERIVED_PRODUCT_LINK_MODELS:
        session.execute(
            update(model)
            .where(model.product_id == old_product_id)
            .values(product_id=new_product_id)
        )
    for column in (ProductMatchLog.old_product_id, ProductMatchLog.new_product_id):
        session.execute(
            update(ProductMatchLog)
            .where(column == old_product_id)
            .values({column.key: new_product_id})
        )


def _verify_no_remaining_business_links(session: Session, product: Product) -> None:
    associations = product_associations(session, product)
    if any(associations.counts.values()):
        raise ValueError(f"临时商品 {product.id} 仍有业务关联，已回滚")


def _placeholder_cleanup_log_table_exists(session: Session) -> bool:
    return inspect(session.get_bind()).has_table(ProductPlaceholderCleanupLog.__tablename__)


def execute_placeholder_cleanup(session: Session, *, actor: str = "system") -> PlaceholderCleanupResult:
    preview = preview_placeholder_cleanup(session)
    migrated_products = 0
    deleted_products = 0
    migrated_associations = 0
    actor = (actor or "system").strip()[:128] or "system"
    try:
        for row in preview.rows:
            if row.action not in {"delete_unlinked", "migrate_and_delete"}:
                continue
            old_product = session.get(Product, row.old_product_id)
            new_product = session.get(Product, row.new_product_id) if row.new_product_id is not None else None
            if old_product is None or new_product is None:
                raise ValueError("预览结果已过期，请重新预览")
            if not _is_temporary_placeholder(old_product) or not _is_formal_qinsi_product(session, new_product):
                raise ValueError("商品身份已变化，请重新预览")
            formals = _formal_qinsi_products_for_jan(session, row.jan, old_product.id)
            if [product.id for product in formals] != [new_product.id]:
                raise ValueError("同 JAN 正式秦丝商品数量已变化，请重新预览")
            migrated = 0
            if row.action == "migrate_and_delete":
                migrated = _migrate_supported_product_links(session, old_product.id, new_product.id)
                _migrate_derived_product_links(session, old_product.id, new_product.id)
                migrated_products += 1
                migrated_associations += migrated
            session.flush()
            _verify_no_remaining_business_links(session, old_product)
            if _placeholder_cleanup_log_table_exists(session):
                session.add(ProductPlaceholderCleanupLog(
                    old_product_id=old_product.id,
                    new_product_id=new_product.id,
                    jan=row.jan,
                    migrated_association_count=migrated,
                    operation_type=row.action,
                    actor=actor,
                    reason=PLACEHOLDER_CLEANUP_REASON,
                ))
            session.delete(old_product)
            deleted_products += 1
        session.commit()
    except Exception:
        session.rollback()
        raise
    return PlaceholderCleanupResult(
        preview=preview,
        migrated_product_count=migrated_products,
        deleted_product_count=deleted_products,
        migrated_association_count=migrated_associations,
        manual_count=preview.manual_count,
    )

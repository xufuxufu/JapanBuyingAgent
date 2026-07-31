from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    FieldPurchaseItem,
    Product,
    ProductOperationLog,
    ProductSerial,
    PurchaseBatchItem,
    QinsiExportLine,
    QinsiInventorySnapshotLine,
    QinsiProductMapping,
    QinsiPurchaseExportLine,
    ReceiptItem,
    RestockListItem,
)
from app.product_identity import format_product_display_name, normalize_product_name, update_product_identifiers
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
    product.status = status
    product.product_data_confirmed = True
    product.name_locked = True
    if product.main_image_source_url and product.main_image_source_url != previous_image_url:
        queue_product_image_localization(session, product)
    _log(session, product, action="edit", actor=actor, reason=reason, before=before, after=product_snapshot(product))
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

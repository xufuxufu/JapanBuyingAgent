from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.location_service import (
    DEFAULT_PHYSICAL_LOCATION_CODE, QINSI_NO_BARCODE_LOCATION_CODE,
    get_location_by_code, initialize_default_locations,
)
from app.models import Location, Product, PurchaseBatch, PurchaseBatchItem, Receipt
from app.schemas import PurchaseConfirmationInput


ABNORMAL_DUPLICATE_STATUSES = {"auto_duplicate", "review_required", "likely_duplicate"}


def _active_location(session: Session, location_id: int, *, qinsi: bool = False, physical: bool = False) -> Location:
    location = session.get(Location, location_id)
    if location is None or not location.is_active:
        raise ValueError("所选位置不存在或已禁用")
    if qinsi and not location.is_qinsi_warehouse:
        raise ValueError("秦丝目标仓库必须选择已启用的秦丝仓库")
    if physical and location.location_type not in {"local_physical", "qinsi_warehouse"}:
        raise ValueError("初始物理位置必须选择物理位置或仓库")
    return location


def _actual_line_amount(item) -> int | None:
    if item.line_total is not None:
        return item.line_total
    if item.unit_price is None:
        return None
    return item.unit_price * item.quantity - item.discount_amount


def ensure_purchase_batch_for_receipt(
    session: Session, receipt: Receipt, settings: PurchaseConfirmationInput | None = None,
) -> PurchaseBatch | None:
    existing = session.scalar(select(PurchaseBatch).where(PurchaseBatch.receipt_id == receipt.id))
    if existing is not None:
        return existing
    if (
        receipt.confirmation_status != "confirmed"
        or receipt.review_status != "reviewed"
        or receipt.confirmed_at is None
        or receipt.batch.status in {"deleted", "failed", "cancelled"}
        or receipt.duplicate_status in ABNORMAL_DUPLICATE_STATUSES
    ):
        return None
    active_items = [item for item in receipt.items if item.review_status != "ignored"]
    if not active_items or any(item.product_id is None for item in active_items):
        return None

    settings = settings or PurchaseConfirmationInput()
    active_ids = {item.id for item in active_items}
    unknown_overrides = set(settings.line_qinsi_target_overrides) - active_ids
    if unknown_overrides:
        raise ValueError("行级目标仓库覆盖包含不属于当前小票的商品行")

    try:
        default_initial = get_location_by_code(session, DEFAULT_PHYSICAL_LOCATION_CODE)
    except LookupError:
        initialize_default_locations(session, commit=False)
        default_initial = get_location_by_code(session, DEFAULT_PHYSICAL_LOCATION_CODE)
    initial = _active_location(
        session, settings.initial_location_id or default_initial.id, physical=True,
    )
    batch_target = (
        _active_location(session, settings.qinsi_target_warehouse_id, qinsi=True)
        if settings.qinsi_target_warehouse_id else None
    )
    jan_target = _active_location(session, default_initial.id, qinsi=True)
    no_jan_target = _active_location(
        session, get_location_by_code(session, QINSI_NO_BARCODE_LOCATION_CODE).id, qinsi=True,
    )

    purchase_batch = PurchaseBatch(
        batch_no=f"PB-R{receipt.id:08d}",
        receipt_id=receipt.id,
        gpt_batch_id=receipt.batch_id,
        purchased_at=receipt.purchased_at,
        store_name=receipt.raw_store_name,
        store_id=receipt.store_id,
        confirmed_at=receipt.confirmed_at,
        status="confirmed",
        default_initial_location_id=initial.id,
        default_qinsi_warehouse_id=batch_target.id if batch_target else None,
    )
    session.add(purchase_batch)
    session.flush()
    for item in active_items:
        product = session.get(Product, item.product_id)
        if product is None:
            raise ValueError(f"小票商品行 {item.line_no} 关联商品不存在")
        override_id = settings.line_qinsi_target_overrides.get(item.id)
        if override_id:
            target = _active_location(session, override_id, qinsi=True)
        elif batch_target:
            target = batch_target
        else:
            target = jan_target if product.jan else no_jan_target
        session.add(PurchaseBatchItem(
            purchase_batch_id=purchase_batch.id,
            product_id=product.id,
            receipt_item_id=item.id,
            quantity=item.quantity,
            unit_price=item.unit_price,
            discount_amount=item.discount_amount,
            actual_line_amount=_actual_line_amount(item),
            initial_location_id=initial.id,
            qinsi_target_warehouse_id=target.id,
            target_warehouse_overridden=bool(override_id),
        ))
    session.flush()
    return purchase_batch


def list_purchase_batches(session: Session) -> list[PurchaseBatch]:
    return list(session.scalars(
        select(PurchaseBatch)
        .options(selectinload(PurchaseBatch.items))
        .order_by(PurchaseBatch.confirmed_at.desc(), PurchaseBatch.id.desc())
    ))


def get_purchase_batch(session: Session, purchase_batch_id: int) -> PurchaseBatch | None:
    return session.scalar(
        select(PurchaseBatch)
        .where(PurchaseBatch.id == purchase_batch_id)
        .options(
            selectinload(PurchaseBatch.receipt),
            selectinload(PurchaseBatch.gpt_batch),
            selectinload(PurchaseBatch.default_initial_location),
            selectinload(PurchaseBatch.default_qinsi_warehouse),
            selectinload(PurchaseBatch.store),
            selectinload(PurchaseBatch.qinsi_export_jobs),
            selectinload(PurchaseBatch.items).selectinload(PurchaseBatchItem.product),
            selectinload(PurchaseBatch.items).selectinload(PurchaseBatchItem.receipt_item),
            selectinload(PurchaseBatch.items).selectinload(PurchaseBatchItem.initial_location),
            selectinload(PurchaseBatch.items).selectinload(PurchaseBatchItem.qinsi_target_warehouse),
        )
    )

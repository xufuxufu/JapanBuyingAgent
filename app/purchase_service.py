from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.location_service import (
    DEFAULT_PHYSICAL_LOCATION_CODE, QINSI_NEW_JAPAN_WAREHOUSE_CODE,
    get_location_by_code, initialize_default_locations,
)
from app.models import Location, Product, PurchaseBatch, PurchaseBatchItem, Receipt
from app.receipt_pricing import actual_line_amount, purchase_unit_price
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


def _purchase_batch_no(session: Session, receipt: Receipt) -> str:
    receipt_ids = sorted(row.id for row in receipt.batch.receipts)
    position = receipt_ids.index(receipt.id) + 1 if receipt.id in receipt_ids else 1
    base = f"PB-B{receipt.batch_id:08d}"
    candidate = base if position == 1 else f"{base}-R{position:02d}"
    if not session.scalar(select(PurchaseBatch).where(PurchaseBatch.batch_no == candidate)):
        return candidate
    suffix = 2
    while session.scalar(select(PurchaseBatch).where(PurchaseBatch.batch_no == f"{candidate}-{suffix}")):
        suffix += 1
    return f"{candidate}-{suffix}"


def purchase_batch_blockers_for_receipt(session: Session, receipt: Receipt) -> list[str]:
    if session.scalar(select(PurchaseBatch.id).where(PurchaseBatch.receipt_id == receipt.id)):
        return []
    blockers: list[str] = []
    if receipt.confirmation_status != "confirmed":
        blockers.append("小票尚未最终确认")
    if receipt.review_status != "reviewed":
        blockers.append("小票审核状态尚未标记为 reviewed")
    if receipt.confirmed_at is None:
        blockers.append("小票缺少确认时间")
    if receipt.batch.status in {"deleted", "failed", "cancelled"}:
        blockers.append(f"小票批次状态为 {receipt.batch.status}")
    if receipt.duplicate_status in ABNORMAL_DUPLICATE_STATUSES:
        blockers.append(f"小票重复状态为 {receipt.duplicate_status}")
    active_items = [item for item in receipt.items if item.review_status != "ignored"]
    if not active_items:
        blockers.append("没有未忽略商品行")
    for item in active_items:
        if item.product_id is None:
            jan = item.jan_candidate or "无JAN"
            blockers.append(f"第 {item.line_no} 行 JAN {jan} 尚未解决{('冲突' if item.match_status == 'conflict' else '匹配')}")
        elif session.get(Product, item.product_id) is None:
            blockers.append(f"第 {item.line_no} 行关联商品 {item.product_id} 已不存在")
    return blockers


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
    default_target = _active_location(
        session, get_location_by_code(session, QINSI_NEW_JAPAN_WAREHOUSE_CODE).id, qinsi=True,
    )
    batch_target = (
        _active_location(session, settings.qinsi_target_warehouse_id, qinsi=True)
        if settings.qinsi_target_warehouse_id else default_target
    )

    purchase_batch = PurchaseBatch(
        batch_no=_purchase_batch_no(session, receipt),
        receipt_id=receipt.id,
        gpt_batch_id=receipt.batch_id,
        purchased_at=receipt.purchased_at,
        store_name=receipt.raw_store_name,
        store_id=receipt.store_id,
        confirmed_at=receipt.confirmed_at,
        status="confirmed",
        default_initial_location_id=initial.id,
        default_qinsi_warehouse_id=batch_target.id,
    )
    session.add(purchase_batch)
    session.flush()
    existing_detail_item_ids = set(session.scalars(
        select(PurchaseBatchItem.receipt_item_id).where(PurchaseBatchItem.receipt_item_id.in_(active_ids))
    ))
    for item in active_items:
        if item.id in existing_detail_item_ids:
            continue
        product = session.get(Product, item.product_id)
        if product is None:
            raise ValueError(f"小票商品行 {item.line_no} 关联商品不存在")
        override_id = settings.line_qinsi_target_overrides.get(item.id)
        if override_id:
            target = _active_location(session, override_id, qinsi=True)
        else:
            target = batch_target
        session.add(PurchaseBatchItem(
            purchase_batch_id=purchase_batch.id,
            product_id=product.id,
            receipt_item_id=item.id,
            quantity=item.quantity,
            unit_price=purchase_unit_price(item.quantity, item.unit_price, item.line_total),
            discount_amount=item.discount_amount,
            actual_line_amount=actual_line_amount(item.quantity, item.unit_price, item.discount_amount, item.line_total),
            initial_location_id=initial.id,
            qinsi_target_warehouse_id=target.id,
            target_warehouse_overridden=bool(override_id),
        ))
    if any((session.get(Product, item.product_id).status or "").startswith("new_") for item in active_items):
        receipt.batch.product_status = "blocked_by_new_products"
    else:
        receipt.batch.product_status = "matched"
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

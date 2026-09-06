from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

from sqlalchemy import case, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.models import (
    Customer, CustomerAddress, Product, SalesOrder, SalesOrderItem, SalesOrderShippingLabel,
    SalesShipment, SalesShipmentItem, Salesperson,
)
from app.procurement_service import sync_demands_for_sales_order
from app.sales_order_shipping import (
    delete_shipping_label_file, delete_sales_order_item_image_file,
    save_shipping_label_file, save_sales_order_item_image_file,
)


DEFAULT_SALESPERSON_NAME = "秀"

TOKYO = timezone(timedelta(hours=9), "Asia/Tokyo")

ORDER_STATUSES = {"submitted", "paid", "partially_shipped", "shipped", "completed", "cancelled"}

# User-facing text only -- the underlying `status` column values (see
# ORDER_STATUSES/ALLOWED_TRANSITIONS) are unchanged and still what routes,
# templates and tests key off of. "paid"/"completed" are deliberately no
# longer surfaced as "已付款"/"已完成" to users -- the wording now reflects
# what needs to happen next ("待发货") or what already happened to the
# customer's parcel ("已收货"), matching how staff actually think about the
# workflow. Never re-introduce "已付款"/"已完成" in user-facing copy.
STATUS_LABELS: dict[str, str] = {
    "submitted": "新订单",
    "paid": "待发货",
    "partially_shipped": "部分发货",
    "shipped": "已发货",
    "completed": "已收货",
    "cancelled": "已取消",
}

# Centralized state machine for HUMAN-TRIGGERED transitions only (routes/
# templates must call update_sales_order_status() rather than re-implementing
# this logic). "partially_shipped" and "shipped" are never reached through
# here -- they are derived automatically from shipment facts by
# _recompute_order_status_from_shipments(), which is the only code allowed to
# set them.
ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "submitted": {"paid", "cancelled"},
    "paid": {"cancelled"},
    "partially_shipped": set(),
    "shipped": {"completed"},
    "completed": set(),
    "cancelled": set(),
}

# UI hint only (not a business rule): the one-tap "move it forward" action to surface
# per status. Cancellation and other allowed transitions stay reachable but secondary.
PRIMARY_NEXT_ACTION: dict[str, tuple[str, str]] = {
    "submitted": ("paid", "标记已付款"),
    "shipped": ("completed", "标记已收货"),
}

# Items (product/quantity/price) and the customer can only change before payment;
# paid+ orders lock them (see ITEM_LOCKED_MESSAGE / ensure_items_editable below).
ITEM_EDITABLE_STATUSES = {"submitted"}
ITEM_LOCKED_MESSAGE = "订单待发货，商品、数量和售价已锁定；发货前仍可修改收货地址。"

# The order-level "current/default" address represents whatever hasn't shipped
# yet; it stays editable until every item is fully shipped (or the order is
# done/cancelled). Once a specific SalesShipment is marked shipped, that
# shipment's OWN address snapshot is separately frozen forever.
ADDRESS_EDITABLE_STATUSES = {"submitted", "paid", "partially_shipped"}

# Shipping labels are dispatch photos attached to a specific shipment. Normal
# upload/delete happens while still pending; a shipped shipment ALSO accepts
# corrections (mis-scanned/wrong label uploaded) but only through the explicit
# "modify logistics info" UI action, never the default read-only gallery view --
# shipment items/quantity/shipped_at stay immutable regardless.
SHIPPING_LABEL_UPLOADABLE_STATUSES = {"pending", "shipped"}
SHIPPING_LABEL_DELETABLE_STATUSES = {"pending", "shipped"}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def ensure_default_salesperson(session: Session, *, commit: bool = True) -> Salesperson:
    salesperson = session.scalar(select(Salesperson).where(Salesperson.name == DEFAULT_SALESPERSON_NAME))
    if salesperson is None:
        salesperson = Salesperson(name=DEFAULT_SALESPERSON_NAME, active=True)
        session.add(salesperson)
        if commit:
            session.commit()
        else:
            session.flush()
    return salesperson


def list_salespersons(session: Session) -> list[Salesperson]:
    return list(session.scalars(select(Salesperson).where(Salesperson.active.is_(True)).order_by(Salesperson.id)))


def search_customers(session: Session, q: str, *, limit: int = 20) -> list[Customer]:
    query = select(Customer)
    value = (q or "").strip()
    if value:
        like = f"%{value}%"
        query = query.where(or_(Customer.name.like(like), Customer.phone.like(like), Customer.wechat_name.like(like)))
    return list(session.scalars(query.order_by(Customer.updated_at.desc(), Customer.id.desc()).limit(limit)))


def create_customer(
    session: Session, *, name: str, phone: str | None = None, wechat_name: str | None = None,
    note: str | None = None, recipient_name: str | None = None, recipient_phone: str | None = None,
    address: str | None = None, address_label: str | None = None, commit: bool = True,
) -> Customer:
    """Create a customer. If `address` is given, it becomes the customer's
    first saved CustomerAddress (auto-default) rather than writing the
    deprecated Customer.address column.

    `phone` (the customer's own contact number) and `recipient_phone` (the
    delivery contact for this address) are deliberately separate fields --
    they're often the same person but must never be silently conflated.
    `recipient_phone` falls back to `phone` only when left blank.
    """
    clean_name = (name or "").strip()
    if not clean_name:
        raise ValueError("客户姓名不能为空")
    clean_phone = (phone or "").strip()[:50] or None
    customer = Customer(
        name=clean_name[:255], phone=clean_phone,
        wechat_name=(wechat_name or "").strip()[:128] or None,
        note=(note or "").strip() or None,
    )
    session.add(customer)
    session.flush()
    clean_address = (address or "").strip()
    if clean_address:
        add_customer_address(
            session, customer.id,
            recipient_name=(recipient_name or "").strip() or clean_name,
            phone=(recipient_phone or "").strip()[:50] or clean_phone,
            address=clean_address, label=address_label,
            is_default=True, commit=False,
        )
    if commit:
        session.commit()
    else:
        session.flush()
    return customer


def list_customer_addresses(session: Session, customer_id: int) -> list[CustomerAddress]:
    return list(session.scalars(
        select(CustomerAddress).where(CustomerAddress.customer_id == customer_id)
        .order_by(CustomerAddress.is_default.desc(), CustomerAddress.id)
    ))


def _normalize_address_key(recipient_name: str, phone: str | None, address: str) -> tuple[str, str, str]:
    """Whitespace-insensitive identity for duplicate detection -- exact match on
    the three human-facing fields only, never fuzzy/partial matching."""
    return (
        "".join((recipient_name or "").split()),
        "".join((phone or "").split()),
        "".join((address or "").split()),
    )


def find_duplicate_customer_address(
    session: Session, customer_id: int, *, recipient_name: str, phone: str | None, address: str,
) -> CustomerAddress | None:
    key = _normalize_address_key(recipient_name, phone, address)
    for existing in list_customer_addresses(session, customer_id):
        if _normalize_address_key(existing.recipient_name, existing.phone, existing.address) == key:
            return existing
    return None


def add_customer_address(
    session: Session, customer_id: int, *, recipient_name: str, phone: str | None = None,
    address: str, label: str | None = None, is_default: bool = False, commit: bool = True,
    allow_duplicate: bool = False,
) -> CustomerAddress:
    customer = session.get(Customer, customer_id)
    if customer is None:
        raise LookupError("客户不存在")
    clean_recipient = (recipient_name or "").strip()[:255] or customer.name
    clean_address = (address or "").strip()
    if not clean_address:
        raise ValueError("收货地址不能为空")
    if not allow_duplicate:
        duplicate = find_duplicate_customer_address(
            session, customer_id, recipient_name=clean_recipient, phone=phone, address=clean_address,
        )
        if duplicate is not None:
            raise DuplicateAddressError(duplicate)
    is_first = session.scalar(select(CustomerAddress.id).where(CustomerAddress.customer_id == customer_id)) is None
    make_default = is_default or is_first
    if make_default:
        session.execute(
            CustomerAddress.__table__.update()
            .where(CustomerAddress.customer_id == customer_id)
            .values(is_default=False)
        )
    entry = CustomerAddress(
        customer_id=customer_id, recipient_name=clean_recipient,
        phone=(phone or "").strip()[:50] or None, address=clean_address,
        label=(label or "").strip()[:50] or None, is_default=make_default,
    )
    session.add(entry)
    if commit:
        session.commit()
    else:
        session.flush()
    return entry


class DuplicateAddressError(ValueError):
    """Raised by add_customer_address when an identical address already exists
    for this customer (same recipient/phone/address after whitespace
    normalization). Carries the existing row so callers can offer to reuse it
    instead of silently creating a duplicate."""

    def __init__(self, existing: CustomerAddress) -> None:
        super().__init__("该客户已存在相同收货地址")
        self.existing = existing


def update_customer_address(
    session: Session, address_id: int, *, recipient_name: str, phone: str | None, address: str,
    label: str | None = None, is_default: bool = False,
) -> CustomerAddress:
    entry = session.get(CustomerAddress, address_id)
    if entry is None:
        raise LookupError("地址不存在")
    clean_recipient = (recipient_name or "").strip()[:255] or entry.recipient_name
    clean_address = (address or "").strip()
    if not clean_address:
        raise ValueError("收货地址不能为空")
    if is_default and not entry.is_default:
        session.execute(
            CustomerAddress.__table__.update()
            .where(CustomerAddress.customer_id == entry.customer_id)
            .values(is_default=False)
        )
    entry.recipient_name = clean_recipient
    entry.phone = (phone or "").strip()[:50] or None
    entry.address = clean_address
    entry.label = (label or "").strip()[:50] or None
    entry.is_default = is_default or entry.is_default
    session.commit()
    return entry


def delete_customer_address(session: Session, address_id: int) -> None:
    entry = session.get(CustomerAddress, address_id)
    if entry is None:
        raise LookupError("地址不存在")
    was_default = entry.is_default
    customer_id = entry.customer_id
    session.delete(entry)
    session.flush()
    if was_default:
        # Promote the next-oldest remaining address so the customer always has
        # exactly one default when at least one address is left.
        successor = session.scalar(
            select(CustomerAddress).where(CustomerAddress.customer_id == customer_id).order_by(CustomerAddress.id)
        )
        if successor is not None:
            successor.is_default = True
    session.commit()


def search_products(session: Session, q: str, *, limit: int = 20) -> list[Product]:
    """Substring search across code/name fields, ranked by relevance BEFORE
    the limit is applied -- a wide keyword (e.g. "精华") can legitimately
    match far more than `limit` products, and sorting only by recency (as
    this used to do) could push an obviously-matching product entirely off
    the first page while unrelated, more-recently-touched products filled
    it. Rank, highest priority first:
      0. exact JAN / qinsi_product_code match
      1. exact name_cn / name_ja match
      2. name_cn / name_ja prefix match
      3. name_cn / name_ja / display_name contains the keyword
      4. only internal_sku / qinsi_product_code contains it (substring, not exact)
    Ties within a rank fall back to the previous recency ordering.
    """
    value = (q or "").strip()
    if not value:
        return []
    like = f"%{value}%"
    prefix = f"{value}%"
    name_contains = or_(Product.name_cn.like(like), Product.name_ja.like(like), Product.display_name.like(like))
    code_contains = or_(Product.internal_sku.like(like), Product.jan.like(like), Product.qinsi_product_code.like(like))
    query = select(Product).where(Product.status != "archived", or_(name_contains, code_contains))
    rank = case(
        (or_(Product.jan == value, Product.qinsi_product_code == value), 0),
        (or_(Product.name_cn == value, Product.name_ja == value), 1),
        (or_(Product.name_cn.like(prefix), Product.name_ja.like(prefix)), 2),
        (name_contains, 3),
        else_=4,
    )
    return list(session.scalars(query.order_by(rank, Product.updated_at.desc()).limit(limit)))


ORDER_NO_DAILY_SEQUENCE_MAX = 99


def _generate_order_no(session: Session, *, now: datetime | None = None) -> str:
    """YYMMDD + 2-digit daily sequence (e.g. "26090503"), bucketed by TOKYO
    calendar day to match this app's universal date convention. Only rows
    already in this exact 8-char all-digit shape count toward the day's
    sequence -- the legacy "SO-YYYYMMDD-NNNN" format is a different length
    and can never match the LIKE pattern below, so old and new rows never
    interfere with each other's numbering.
    """
    tokyo_now = (now or utcnow()).astimezone(TOKYO)
    prefix = f"{tokyo_now:%y%m%d}"
    existing_today = session.scalars(
        select(SalesOrder.order_no).where(
            SalesOrder.order_no.like(f"{prefix}__"),
            func.length(SalesOrder.order_no) == 8,
        )
    ).all()
    used_sequences = {int(order_no[6:8]) for order_no in existing_today if order_no[6:8].isdigit()}
    for seq in range(1, ORDER_NO_DAILY_SEQUENCE_MAX + 1):
        if seq not in used_sequences:
            return f"{prefix}{seq:02d}"
    raise ValueError(f"{prefix} 当天订单号已用满{ORDER_NO_DAILY_SEQUENCE_MAX}个，请手工指定订单号")


def suggest_order_no(session: Session, *, now: datetime | None = None) -> str:
    """Read-only preview of what _generate_order_no would produce right now
    -- used to pre-fill the new-order form. Not reserved; a real race
    between two concurrent submissions is still caught at commit time in
    create_sales_order (see the IntegrityError handling there)."""
    return _generate_order_no(session, now=now)


@dataclass(frozen=True, slots=True)
class SalesOrderItemInput:
    product_id: int | None
    manual_name: str | None
    jan: str | None
    quantity: int
    unit_sale_price: Decimal
    note: str | None = None
    manual_image_content: bytes | None = None
    manual_image_filename: str | None = None


def last_sale_price_for_product(session: Session, product_id: int) -> Decimal | None:
    """Most recent 微信售价 (CNY) this exact Product sold at, across any
    non-cancelled order -- UI hint only, never used as a default/lock. Never
    matched by product name; product_id is the only key."""
    return session.scalar(
        select(SalesOrderItem.unit_sale_price)
        .join(SalesOrder, SalesOrder.id == SalesOrderItem.sales_order_id)
        .where(SalesOrderItem.product_id == product_id, SalesOrder.status != "cancelled")
        .order_by(SalesOrder.order_date.desc(), SalesOrderItem.id.desc())
        .limit(1)
    )


def _build_order_item(session: Session, order: SalesOrder, entry: SalesOrderItemInput) -> SalesOrderItem:
    if entry.quantity <= 0:
        raise ValueError("商品数量必须大于0")
    if entry.unit_sale_price < 0:
        raise ValueError("微信售价不能为负数")
    product = None
    if entry.product_id is not None:
        product = session.get(Product, entry.product_id)
        if product is None:
            raise LookupError("所选商品不存在")
    if product is not None:
        name_snapshot = product.display_name or product.name_cn or product.name_ja or product.internal_sku
        jan_snapshot = product.jan
    else:
        name_snapshot = (entry.manual_name or "").strip() or None
        jan_snapshot = (entry.jan or "").strip() or None
    manual_image_fields: dict = {}
    if product is None and entry.manual_image_content:
        stored_filename, relative_path, content_type, safe_original_name, file_size = save_sales_order_item_image_file(
            order.id, content=entry.manual_image_content, original_filename=entry.manual_image_filename,
        )
        manual_image_fields = {
            "manual_image_relative_path": relative_path,
            "manual_image_original_filename": safe_original_name,
            "manual_image_content_type": content_type,
            "manual_image_file_size": file_size,
        }
    # A manual item must be identified by at least one of: a real product,
    # a name, or a photo -- an item with none of those is meaningless.
    if product is None and not name_snapshot and not manual_image_fields:
        raise ValueError("手工商品必须填写商品名或上传图片")
    return SalesOrderItem(
        product_id=product.id if product else None,
        product_name_snapshot=name_snapshot[:255] if name_snapshot else None,
        jan_snapshot=jan_snapshot[:32] if jan_snapshot else None,
        quantity=entry.quantity, unit_sale_price=entry.unit_sale_price,
        note=(entry.note or "").strip() or None,
        **manual_image_fields,
    )


def create_sales_order(
    session: Session, *, customer_id: int, salesperson_id: int,
    items: list[SalesOrderItemInput], note: str | None = None,
    recipient_name: str | None = None, recipient_phone: str | None = None,
    shipping_address: str | None = None, customer_address_id: int | None = None,
    order_no: str | None = None, order_date: datetime | None = None,
    historical_backfill: bool = False,
) -> SalesOrder:
    """`order_no`/`order_date` are optional manual overrides for backfilling
    historical orders -- left blank, both default to "generate for right
    now" exactly as before. `historical_backfill=True` skips
    sync_demands_for_sales_order() so re-entering an old, already-fulfilled
    order never spawns a live procurement demand; it must be an explicit
    caller choice, never inferred from order_date being in the past."""
    customer = session.get(Customer, customer_id)
    if customer is None:
        raise LookupError("客户不存在")
    salesperson = session.get(Salesperson, salesperson_id)
    if salesperson is None:
        raise LookupError("销售员不存在")
    if not items:
        raise ValueError("请至少添加一个商品")
    resolved_order_no = (order_no or "").strip()[:40]
    if resolved_order_no:
        if session.scalar(select(SalesOrder.id).where(SalesOrder.order_no == resolved_order_no)):
            raise ValueError(f"订单号「{resolved_order_no}」已存在")
    else:
        resolved_order_no = _generate_order_no(session)
    resolved_order_date = order_date if order_date is not None else utcnow()
    # Recipient/address are snapshotted at order time so later edits to the
    # customer record never rewrite historical orders. Priority: an explicit
    # one-off override > a chosen saved CustomerAddress > the customer's own
    # default address (if any) -- same person receives unless overridden.
    chosen_address = None
    if customer_address_id is not None:
        chosen_address = session.get(CustomerAddress, customer_address_id)
        if chosen_address is None or chosen_address.customer_id != customer.id:
            raise LookupError("所选收货地址不存在")
    if chosen_address is None:
        chosen_address = session.scalar(
            select(CustomerAddress).where(CustomerAddress.customer_id == customer.id, CustomerAddress.is_default.is_(True))
        )
    recipient_name_snapshot = (recipient_name or "").strip()[:255] or (chosen_address.recipient_name if chosen_address else None) or customer.name
    recipient_phone_snapshot = (recipient_phone or "").strip()[:50] or (chosen_address.phone if chosen_address else None) or customer.phone
    shipping_address_snapshot = (shipping_address or "").strip() or (chosen_address.address if chosen_address else None) or customer.address
    order = SalesOrder(
        order_no=resolved_order_no, customer_id=customer.id, salesperson_id=salesperson.id,
        status="submitted", order_date=resolved_order_date, note=(note or "").strip() or None,
        recipient_name_snapshot=recipient_name_snapshot,
        recipient_phone_snapshot=recipient_phone_snapshot,
        shipping_address_snapshot=shipping_address_snapshot,
    )
    session.add(order)
    session.flush()
    for entry in items:
        order.items.append(_build_order_item(session, order, entry))
    session.flush()
    if not historical_backfill:
        # Every confirmed order line is a procurement demand from the moment
        # it's placed; the demand center is what surfaces it, not this
        # function. Historical backfill explicitly opts out (see docstring).
        sync_demands_for_sales_order(session, order, commit=False)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise ValueError(f"订单号「{resolved_order_no}」已存在") from exc
    return get_sales_order(session, order.id)


def ensure_items_editable(order: SalesOrder) -> None:
    if order.status not in ITEM_EDITABLE_STATUSES:
        raise ValueError(ITEM_LOCKED_MESSAGE)


def update_sales_order(
    session: Session, order_id: int, *, customer_id: int, items: list[SalesOrderItemInput],
    note: str | None = None, recipient_name: str | None = None, recipient_phone: str | None = None,
    shipping_address: str | None = None, customer_address_id: int | None = None,
) -> SalesOrder:
    """Replace a submitted (unpaid) order's customer/items/address wholesale.

    Only allowed while the order is still 'submitted' -- once paid, product,
    quantity, price and customer are locked (ensure_items_editable enforces
    this; the address stays separately editable via update_sales_order_address).
    """
    order = session.get(SalesOrder, order_id)
    if order is None:
        raise LookupError("订单不存在")
    ensure_items_editable(order)
    customer = session.get(Customer, customer_id)
    if customer is None:
        raise LookupError("客户不存在")
    if not items:
        raise ValueError("请至少添加一个商品")
    chosen_address = None
    if customer_address_id is not None:
        chosen_address = session.get(CustomerAddress, customer_address_id)
        if chosen_address is None or chosen_address.customer_id != customer.id:
            raise LookupError("所选收货地址不存在")
    order.customer_id = customer.id
    order.note = (note or "").strip() or None
    order.recipient_name_snapshot = (recipient_name or "").strip()[:255] or (chosen_address.recipient_name if chosen_address else None) or customer.name
    order.recipient_phone_snapshot = (recipient_phone or "").strip()[:50] or (chosen_address.phone if chosen_address else None) or customer.phone
    order.shipping_address_snapshot = (shipping_address or "").strip() or (chosen_address.address if chosen_address else None) or customer.address
    # Wholesale replace: any previously-uploaded manual images on removed
    # rows are orphaned on disk, not deleted -- they're historical snapshots
    # of what was once on the order, and a pre-payment edit is rare/small
    # enough that this is an acceptable, safe-by-default trade-off.
    order.items.clear()
    session.flush()
    new_items = [_build_order_item(session, order, entry) for entry in items]
    order.items.extend(new_items)
    session.flush()
    sync_demands_for_sales_order(session, order, commit=False)
    session.commit()
    return get_sales_order(session, order.id)


def update_sales_order_address(
    session: Session, order_id: int, *, recipient_name: str, recipient_phone: str | None,
    shipping_address: str,
) -> SalesOrder:
    """Update the order's current/default address (whatever hasn't shipped
    yet). Refuses once nothing is left to ship (shipped/completed/cancelled).
    Already-shipped SalesShipment rows keep their own frozen snapshot regardless."""
    order = session.get(SalesOrder, order_id)
    if order is None:
        raise LookupError("订单不存在")
    if order.status not in ADDRESS_EDITABLE_STATUSES:
        raise ValueError(f"「{STATUS_LABELS.get(order.status, order.status)}」状态不支持修改收货地址")
    clean_name = (recipient_name or "").strip()
    clean_address = (shipping_address or "").strip()
    if not clean_name or not clean_address:
        raise ValueError("收件人姓名和收货地址不能为空")
    order.recipient_name_snapshot = clean_name[:255]
    order.recipient_phone_snapshot = (recipient_phone or "").strip()[:50] or None
    order.shipping_address_snapshot = clean_address
    session.commit()
    return order


def get_sales_order(session: Session, order_id: int) -> SalesOrder | None:
    return session.scalar(
        select(SalesOrder).where(SalesOrder.id == order_id).options(
            selectinload(SalesOrder.customer), selectinload(SalesOrder.salesperson),
            selectinload(SalesOrder.items).selectinload(SalesOrderItem.product),
            selectinload(SalesOrder.shipping_labels),
            selectinload(SalesOrder.shipments).selectinload(SalesShipment.items).selectinload(SalesShipmentItem.sales_order_item),
            selectinload(SalesOrder.shipments).selectinload(SalesShipment.shipping_labels),
        )
    )


def list_sales_orders(
    session: Session, *, status: str | None = None, q: str | None = None,
    date_from: date | None = None, date_to: date | None = None,
    shipped_date_from: date | None = None, shipped_date_to: date | None = None,
) -> list[SalesOrder]:
    query = select(SalesOrder).options(
        selectinload(SalesOrder.customer), selectinload(SalesOrder.salesperson),
        selectinload(SalesOrder.items).selectinload(SalesOrderItem.product),
        selectinload(SalesOrder.shipping_labels),
    )
    if status in ORDER_STATUSES:
        query = query.where(SalesOrder.status == status)
    value = (q or "").strip()
    if value:
        like = f"%{value}%"
        query = query.join(Customer, Customer.id == SalesOrder.customer_id).where(
            or_(SalesOrder.order_no.like(like), Customer.name.like(like)),
        )
    if date_from is not None:
        query = query.where(SalesOrder.order_date >= datetime.combine(date_from, time.min, timezone.utc))
    if date_to is not None:
        query = query.where(SalesOrder.order_date <= datetime.combine(date_to, time.max, timezone.utc))
    if shipped_date_from is not None or shipped_date_to is not None:
        # Matches if ANY of the order's shipments has shipped_at in range --
        # explicitly NOT a stand-in using order created_at/order_date. The
        # picker's date is a Tokyo-local calendar day (matching how the rest
        # of the UI displays dates via tokyo_datetime); shipped_at is stored
        # as naive UTC (SQLite has no real tz-aware column), so the bound is
        # converted Tokyo -> UTC and stripped of tzinfo before comparing.
        shipment_filter = SalesShipment.sales_order_id == SalesOrder.id
        if shipped_date_from is not None:
            start = datetime.combine(shipped_date_from, time.min, TOKYO).astimezone(timezone.utc).replace(tzinfo=None)
            shipment_filter = shipment_filter & (SalesShipment.shipped_at >= start)
        if shipped_date_to is not None:
            end = datetime.combine(shipped_date_to, time.max, TOKYO).astimezone(timezone.utc).replace(tzinfo=None)
            shipment_filter = shipment_filter & (SalesShipment.shipped_at <= end)
        query = query.where(select(SalesShipment.id).where(shipment_filter).exists())
    return list(session.scalars(query.order_by(SalesOrder.created_at.desc(), SalesOrder.id.desc())))


def status_counts(session: Session) -> dict[str, int]:
    rows = session.execute(
        select(SalesOrder.status, func.count(SalesOrder.id)).group_by(SalesOrder.status)
    ).all()
    counts = {status: 0 for status in ORDER_STATUSES}
    counts.update({status: count for status, count in rows})
    return counts


def update_sales_order_status(session: Session, order_id: int, target_status: str) -> SalesOrder:
    """Human-triggered transitions only. 'partially_shipped'/'shipped' are
    never reachable here -- they are set exclusively by
    _recompute_order_status_from_shipments() when a shipment ships."""
    if target_status not in ORDER_STATUSES:
        raise ValueError(f"未知订单状态：{target_status}")
    order = session.get(SalesOrder, order_id)
    if order is None:
        raise LookupError("订单不存在")
    if order.status == target_status:
        return order
    allowed = ALLOWED_TRANSITIONS.get(order.status, set())
    if target_status not in allowed:
        current_label = STATUS_LABELS.get(order.status, order.status)
        target_label = STATUS_LABELS.get(target_status, target_status)
        raise ValueError(f"订单当前状态为「{current_label}」，不能直接变更为「{target_label}」")
    order.status = target_status
    session.commit()
    return order


def cancel_sales_order(session: Session, order_id: int) -> SalesOrder:
    return update_sales_order_status(session, order_id, "cancelled")


def _generate_shipment_no(session: Session, order: SalesOrder) -> str:
    seq = len(order.shipments) + 1
    while True:
        candidate = f"{order.order_no}-S{seq}"
        if not session.scalar(select(SalesShipment.id).where(SalesShipment.shipment_no == candidate)):
            return candidate
        seq += 1


def create_shipment(
    session: Session, order_id: int, *, item_quantities: list[tuple[int, int]],
    recipient_name: str | None = None, recipient_phone: str | None = None,
    shipping_address: str | None = None, carrier: str | None = "中通", tracking_no: str | None = None,
) -> SalesShipment:
    """Create a new (pending) shipment covering a partial or full subset of
    an order's not-yet-fully-shipped items.

    Cumulative shipped+reserved quantity per item can never exceed the
    ordered quantity -- checked here against ALL existing shipment_items for
    that order item regardless of shipment status, since a pending shipment's
    quantity is already committed/reserved even before it actually ships.
    The address is snapshotted from the order's CURRENT address at creation
    time (or an explicit override), then frozen forever once this shipment ships.
    """
    order = session.get(SalesOrder, order_id)
    if order is None:
        raise LookupError("订单不存在")
    if order.status not in {"paid", "partially_shipped"}:
        raise ValueError(f"「{STATUS_LABELS.get(order.status, order.status)}」状态不支持创建发货单")
    if not item_quantities:
        raise ValueError("请至少选择一个商品")
    items_by_id = {item.id: item for item in order.items}
    resolved_recipient_name = (recipient_name or "").strip()[:255] or order.recipient_name_snapshot
    resolved_shipping_address = (shipping_address or "").strip() or order.shipping_address_snapshot
    if not resolved_recipient_name or not resolved_shipping_address:
        raise ValueError("请先填写收货人和收货地址，再创建发货单")

    # Validate everything first, before mutating the session at all, so a
    # rejected request (bad item id, oversell) never leaves a half-built
    # shipment attached to the order.
    validated: list[tuple[SalesOrderItem, int]] = []
    reserved_so_far: dict[int, int] = {}
    for order_item_id, quantity in item_quantities:
        if quantity <= 0:
            raise ValueError("发货数量必须大于0")
        order_item = items_by_id.get(order_item_id)
        if order_item is None:
            raise LookupError("订单中不存在该商品行")
        already_reserved = sum(shipment_item.quantity for shipment_item in order_item.shipment_items)
        already_reserved += reserved_so_far.get(order_item_id, 0)
        if already_reserved + quantity > order_item.quantity:
            raise ValueError(
                f"「{order_item.product_name_snapshot or '商品'}」发货数量超出剩余可发数量"
                f"（剩余 {order_item.quantity - already_reserved}）"
            )
        reserved_so_far[order_item_id] = already_reserved + quantity
        validated.append((order_item, quantity))

    shipment = SalesShipment(
        sales_order_id=order.id, shipment_no=_generate_shipment_no(session, order), status="pending",
        recipient_name_snapshot=resolved_recipient_name,
        recipient_phone_snapshot=(recipient_phone or "").strip()[:50] or order.recipient_phone_snapshot,
        shipping_address_snapshot=resolved_shipping_address,
        carrier=(carrier or "").strip()[:50] or None, tracking_no=(tracking_no or "").strip()[:100] or None,
        items=[SalesShipmentItem(sales_order_item=order_item, quantity=quantity) for order_item, quantity in validated],
    )
    order.shipments.append(shipment)
    session.commit()
    return shipment


def _recompute_order_status_from_shipments(order: SalesOrder) -> None:
    if order.status not in {"paid", "partially_shipped", "shipped"}:
        return
    if all(item.remaining_quantity == 0 for item in order.items):
        order.status = "shipped"
    elif any(item.shipped_quantity > 0 for item in order.items):
        order.status = "partially_shipped"
    else:
        order.status = "paid"


def maybe_complete_order_from_tracking(order: SalesOrder) -> bool:
    """Auto-transition shipped -> completed (已收货) once every shipment on a
    FULLY shipped order has reached a terminal carrier-tracking state.

    Deliberately conservative: a single shipment's delivery must never
    complete a split/partially_shipped order early (order.status must
    already be "shipped", i.e. every item's remaining_quantity is 0), and
    ANY shipment lacking a tracking number is treated as "can't tell" and
    blocks auto-completion outright -- it never guesses. In practice this
    also means non-中通 shipments (never queried by
    shipment_tracking_service, so tracking_terminal stays False forever)
    never auto-complete an order; a human still marks those done manually
    via PRIMARY_NEXT_ACTION.

    Returns True if the order was just auto-completed, else False. Caller is
    responsible for committing.
    """
    if order.status != "shipped":
        return False
    shipped_shipments = [s for s in order.shipments if s.status == "shipped"]
    if not shipped_shipments:
        return False
    if all(s.tracking_terminal and (s.tracking_no or "").strip() for s in shipped_shipments):
        order.status = "completed"
        return True
    return False


def mark_shipment_shipped(
    session: Session, shipment_id: int, *, carrier: str | None = None, tracking_no: str | None = None,
) -> SalesShipment:
    shipment = session.get(SalesShipment, shipment_id)
    if shipment is None:
        raise LookupError("发货单不存在")
    if shipment.status != "pending":
        raise ValueError("发货单已发货，不能重复操作")
    if not shipment.shipping_labels:
        raise ValueError("请先上传发货面单图片")
    if carrier is not None:
        shipment.carrier = carrier.strip()[:50] or None
    if tracking_no is not None:
        shipment.tracking_no = tracking_no.strip()[:100] or None
    shipment.status = "shipped"
    shipment.shipped_at = utcnow()
    _recompute_order_status_from_shipments(shipment.sales_order)
    session.commit()
    return shipment


def update_shipment_tracking(
    session: Session, shipment_id: int, *, carrier: str | None, tracking_no: str | None,
) -> SalesShipment:
    shipment = session.get(SalesShipment, shipment_id)
    if shipment is None:
        raise LookupError("发货单不存在")
    shipment.carrier = (carrier or "").strip()[:50] or None
    shipment.tracking_no = (tracking_no or "").strip()[:100] or None
    session.commit()
    return shipment


def add_shipping_label(
    session: Session, shipment_id: int, *, content: bytes, original_filename: str | None,
) -> SalesOrderShippingLabel:
    shipment = session.get(SalesShipment, shipment_id)
    if shipment is None:
        raise LookupError("发货单不存在")
    if shipment.status not in SHIPPING_LABEL_UPLOADABLE_STATUSES:
        raise ValueError("该发货单已发货，不支持上传发货面单")
    stored_filename, relative_path, content_type, safe_original_name, file_size = save_shipping_label_file(
        shipment.sales_order_id, content=content, original_filename=original_filename,
    )
    try:
        label = SalesOrderShippingLabel(
            sales_order_id=shipment.sales_order_id,
            stored_filename=stored_filename, original_filename=safe_original_name,
            relative_path=relative_path, content_type=content_type, file_size=file_size,
        )
        # Append through the relationship (not just setting shipment_id) so
        # shipment.shipping_labels stays correct in memory for the rest of this
        # session -- e.g. later cascade deletes and re-reads of the already-loaded order.
        shipment.shipping_labels.append(label)
        session.commit()
    except Exception:
        session.rollback()
        delete_shipping_label_file(relative_path)
        raise
    return label


def get_shipping_label(session: Session, label_id: int) -> SalesOrderShippingLabel | None:
    return session.get(SalesOrderShippingLabel, label_id)


def remove_shipping_label(session: Session, label_id: int) -> None:
    label = session.get(SalesOrderShippingLabel, label_id)
    if label is None:
        raise LookupError("面单图片不存在")
    shipment = session.get(SalesShipment, label.shipment_id) if label.shipment_id else None
    if shipment is not None and shipment.status not in SHIPPING_LABEL_DELETABLE_STATUSES:
        raise ValueError("该发货单已发货，不支持删除发货面单")
    relative_path = label.relative_path
    session.delete(label)
    session.commit()
    delete_shipping_label_file(relative_path)

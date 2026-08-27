from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from decimal import Decimal

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.models import Customer, Product, SalesOrder, SalesOrderItem, Salesperson


DEFAULT_SALESPERSON_NAME = "秀"

ORDER_STATUSES = {"submitted", "ready_to_ship", "shipped", "completed", "cancelled"}

STATUS_LABELS: dict[str, str] = {
    "submitted": "新订单",
    "ready_to_ship": "待发货",
    "shipped": "已发货",
    "completed": "已完成",
    "cancelled": "已取消",
}

# Centralized state machine: the only place that decides which sales-order status
# transitions are legal. Routes/templates must call update_sales_order_status()
# rather than re-implementing this logic.
ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "submitted": {"ready_to_ship", "cancelled"},
    "ready_to_ship": {"shipped", "cancelled"},
    "shipped": {"completed"},
    "completed": set(),
    "cancelled": set(),
}

# UI hint only (not a business rule): the one-tap "move it forward" action to surface
# per status. Cancellation and other allowed transitions stay reachable but secondary.
PRIMARY_NEXT_ACTION: dict[str, tuple[str, str]] = {
    "submitted": ("ready_to_ship", "设为待发货"),
    "ready_to_ship": ("shipped", "标记已发货"),
    "shipped": ("completed", "标记完成"),
}


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
    address: str | None = None, note: str | None = None, commit: bool = True,
) -> Customer:
    clean_name = (name or "").strip()
    if not clean_name:
        raise ValueError("客户姓名不能为空")
    customer = Customer(
        name=clean_name[:255],
        phone=(phone or "").strip()[:50] or None,
        wechat_name=(wechat_name or "").strip()[:128] or None,
        address=(address or "").strip() or None,
        note=(note or "").strip() or None,
    )
    session.add(customer)
    if commit:
        session.commit()
    else:
        session.flush()
    return customer


def search_products(session: Session, q: str, *, limit: int = 20) -> list[Product]:
    value = (q or "").strip()
    if not value:
        return []
    like = f"%{value}%"
    query = select(Product).where(
        Product.status != "archived",
        or_(
            Product.internal_sku.like(like), Product.jan.like(like), Product.qinsi_product_code.like(like),
            Product.name_cn.like(like), Product.name_ja.like(like), Product.display_name.like(like),
        ),
    )
    return list(session.scalars(query.order_by(Product.updated_at.desc()).limit(limit)))


def _generate_order_no(session: Session, *, now: datetime | None = None) -> str:
    now = now or utcnow()
    prefix = f"SO-{now:%Y%m%d}-"
    seq = 1
    while True:
        candidate = f"{prefix}{seq:04d}"
        if not session.scalar(select(SalesOrder.id).where(SalesOrder.order_no == candidate)):
            return candidate
        seq += 1


@dataclass(frozen=True, slots=True)
class SalesOrderItemInput:
    product_id: int | None
    manual_name: str | None
    jan: str | None
    quantity: int
    unit_sale_price: Decimal
    note: str | None = None


def create_sales_order(
    session: Session, *, customer_id: int, salesperson_id: int,
    items: list[SalesOrderItemInput], note: str | None = None,
    recipient_name: str | None = None, recipient_phone: str | None = None,
    shipping_address: str | None = None,
) -> SalesOrder:
    customer = session.get(Customer, customer_id)
    if customer is None:
        raise LookupError("客户不存在")
    salesperson = session.get(Salesperson, salesperson_id)
    if salesperson is None:
        raise LookupError("销售员不存在")
    if not items:
        raise ValueError("请至少添加一个商品")
    # Recipient/address are snapshotted at order time so later edits to the customer
    # record never rewrite historical orders; default to the customer's own info
    # (same person receives) unless the caller overrides it (a different recipient).
    recipient_name_snapshot = (recipient_name or "").strip()[:255] or customer.name
    recipient_phone_snapshot = (recipient_phone or "").strip()[:50] or customer.phone
    shipping_address_snapshot = (shipping_address or "").strip() or customer.address
    order = SalesOrder(
        order_no=_generate_order_no(session), customer_id=customer.id, salesperson_id=salesperson.id,
        status="submitted", order_date=utcnow(), note=(note or "").strip() or None,
        recipient_name_snapshot=recipient_name_snapshot,
        recipient_phone_snapshot=recipient_phone_snapshot,
        shipping_address_snapshot=shipping_address_snapshot,
    )
    session.add(order)
    session.flush()
    for entry in items:
        if entry.quantity <= 0:
            raise ValueError("商品数量必须大于0")
        if entry.unit_sale_price < 0:
            raise ValueError("销售单价不能为负数")
        product = None
        if entry.product_id is not None:
            product = session.get(Product, entry.product_id)
            if product is None:
                raise LookupError("所选商品不存在")
        if product is not None:
            name_snapshot = product.display_name or product.name_cn or product.name_ja or product.internal_sku
            jan_snapshot = product.jan
        else:
            name_snapshot = (entry.manual_name or "").strip()
            jan_snapshot = (entry.jan or "").strip() or None
        if not name_snapshot:
            raise ValueError("商品名称不能为空")
        order.items.append(SalesOrderItem(
            product_id=product.id if product else None,
            product_name_snapshot=name_snapshot[:255],
            jan_snapshot=jan_snapshot[:32] if jan_snapshot else None,
            quantity=entry.quantity, unit_sale_price=entry.unit_sale_price,
            note=(entry.note or "").strip() or None,
        ))
    session.commit()
    return get_sales_order(session, order.id)


def get_sales_order(session: Session, order_id: int) -> SalesOrder | None:
    return session.scalar(
        select(SalesOrder).where(SalesOrder.id == order_id).options(
            selectinload(SalesOrder.customer), selectinload(SalesOrder.salesperson),
            selectinload(SalesOrder.items).selectinload(SalesOrderItem.product),
        )
    )


def list_sales_orders(
    session: Session, *, status: str | None = None, q: str | None = None,
    date_from: date | None = None, date_to: date | None = None,
) -> list[SalesOrder]:
    query = select(SalesOrder).options(
        selectinload(SalesOrder.customer), selectinload(SalesOrder.salesperson),
        selectinload(SalesOrder.items).selectinload(SalesOrderItem.product),
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
    return list(session.scalars(query.order_by(SalesOrder.created_at.desc(), SalesOrder.id.desc())))


def status_counts(session: Session) -> dict[str, int]:
    rows = session.execute(
        select(SalesOrder.status, func.count(SalesOrder.id)).group_by(SalesOrder.status)
    ).all()
    counts = {status: 0 for status in ORDER_STATUSES}
    counts.update({status: count for status, count in rows})
    return counts


def update_sales_order_status(session: Session, order_id: int, target_status: str) -> SalesOrder:
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

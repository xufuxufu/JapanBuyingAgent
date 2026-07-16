from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.models import Product, PurchaseBatch, PurchaseBatchItem, Receipt, Store, StoreAlias, StoreBrand
from app.schemas import StoreBrandCreateInput, StoreCreateInput


def normalize_store_text(value: str | None) -> str:
    return re.sub(r"[\s\-‐‑‒–—―・,，.。/／()（）]+", "", (value or "").strip().casefold())


def normalize_phone(value: str | None) -> str | None:
    digits = re.sub(r"\D", "", value or "")
    return digits or None


def normalize_postal_code(value: str | None) -> str | None:
    digits = re.sub(r"\D", "", value or "")
    return digits or None


def create_store_brand(session: Session, data: StoreBrandCreateInput) -> StoreBrand:
    name_cn = (data.name_cn or "").strip() or None
    name_ja = (data.name_ja or "").strip() or None
    key_cn, key_ja = normalize_store_text(name_cn), normalize_store_text(name_ja)
    for brand in session.scalars(select(StoreBrand)):
        if (key_cn and normalize_store_text(brand.name_cn) == key_cn) or (key_ja and normalize_store_text(brand.name_ja) == key_ja):
            return brand
    brand = StoreBrand(name_cn=name_cn, name_ja=name_ja)
    session.add(brand)
    session.commit()
    session.refresh(brand)
    return brand


def create_store(session: Session, data: StoreCreateInput) -> Store:
    code = (data.receipt_store_code or "").strip() or None
    phone = (data.phone or "").strip() or None
    normalized = normalize_phone(phone)
    existing = session.scalar(select(Store).where(Store.receipt_store_code == code)) if code else None
    if existing is None and normalized:
        existing = session.scalar(select(Store).where(Store.normalized_phone == normalized))
    if existing is not None:
        return existing
    if data.brand_id is not None and session.get(StoreBrand, data.brand_id) is None:
        raise ValueError("店铺品牌不存在")
    name_cn = (data.name_cn or "").strip() or None
    name_ja = (data.name_ja or "").strip() or None
    raw_name = (data.raw_name or "").strip() or None
    legacy_name = name_cn or name_ja or raw_name or "未命名门店"
    store = Store(
        name=legacy_name, brand_id=data.brand_id, name_cn=name_cn, name_ja=name_ja, raw_name=raw_name,
        phone=phone, normalized_phone=normalized,
        postal_code=(data.postal_code or "").strip() or None,
        normalized_postal_code=normalize_postal_code(data.postal_code),
        address=(data.address or "").strip() or None,
        normalized_address=normalize_store_text(data.address) or None,
        receipt_store_code=code, is_active=data.is_active, is_online=data.is_online,
    )
    session.add(store)
    session.commit()
    session.refresh(store)
    return store


def _bind_receipt(session: Session, receipt: Receipt, store: Store, method: str, confidence: float, confirmed: bool = False) -> Store:
    receipt.store = store
    receipt.store_match_status = "confirmed" if confirmed else "matched"
    receipt.store_match_method = method
    receipt.store_match_confidence = confidence
    if receipt.purchase_batch is not None:
        receipt.purchase_batch.store = store
    session.flush()
    return store


def _unique(candidates) -> Store | None:
    items = {item.id: item for item in candidates if item.is_active}
    return next(iter(items.values())) if len(items) == 1 else None


def match_receipt_store(session: Session, receipt: Receipt) -> Store | None:
    active = list(session.scalars(select(Store).where(Store.is_active.is_(True)).options(selectinload(Store.brand))))
    code = (receipt.raw_store_code or "").strip()
    if code:
        store = _unique(item for item in active if item.receipt_store_code == code)
        if store:
            return _bind_receipt(session, receipt, store, "store_code", 1.0)
    phone = normalize_phone(receipt.raw_store_phone)
    if phone:
        store = _unique(item for item in active if item.normalized_phone == phone)
        if store:
            return _bind_receipt(session, receipt, store, "phone", 1.0)
    postal, address = normalize_postal_code(receipt.raw_store_postal_code), normalize_store_text(receipt.raw_store_address)
    if postal or address:
        store = _unique(item for item in active if (postal and item.normalized_postal_code == postal) or (address and item.normalized_address == address))
        if store:
            return _bind_receipt(session, receipt, store, "postal_or_address", .95)
    raw_name = normalize_store_text(receipt.raw_store_name)
    branch = normalize_store_text(receipt.raw_store_branch_name)
    if raw_name and branch:
        candidates = []
        for item in active:
            brand_names = (normalize_store_text(item.brand.name_cn), normalize_store_text(item.brand.name_ja)) if item.brand else ("", "")
            store_names = (normalize_store_text(item.name_cn), normalize_store_text(item.name_ja), normalize_store_text(item.raw_name))
            if any(name and name in raw_name for name in brand_names) and any(name and (name in raw_name or name == branch) for name in store_names):
                candidates.append(item)
        store = _unique(candidates)
        if store:
            return _bind_receipt(session, receipt, store, "brand_branch", .9)
    if raw_name:
        alias = session.scalar(select(StoreAlias).where(StoreAlias.normalized_alias == raw_name, StoreAlias.confirmed.is_(True)))
        if alias and alias.store.is_active:
            return _bind_receipt(session, receipt, alias.store, "confirmed_alias", 1.0)
    receipt.store_id = None
    receipt.store_match_status = "pending"
    receipt.store_match_method = None
    receipt.store_match_confidence = None
    session.flush()
    return None


def confirm_receipt_store(session: Session, receipt: Receipt, store: Store) -> Store:
    if not store.is_active:
        raise ValueError("门店已禁用")
    alias_text = (receipt.raw_store_name or "").strip()
    normalized = normalize_store_text(alias_text)
    if normalized:
        alias = session.scalar(select(StoreAlias).where(StoreAlias.normalized_alias == normalized))
        if alias is not None and alias.store_id != store.id:
            raise ValueError("该原始名称已确认给其他门店")
    _bind_receipt(session, receipt, store, "manual", 1.0, confirmed=True)
    if normalized:
        if alias is None:
            session.add(StoreAlias(store_id=store.id, alias=alias_text, normalized_alias=normalized, source_receipt_id=receipt.id, confirmed=True))
    session.commit()
    session.refresh(receipt)
    return store


@dataclass(slots=True)
class PurchaseFact:
    item: PurchaseBatchItem
    batch: PurchaseBatch
    receipt: Receipt
    store: Store
    reference_unit_price: float | None


def purchase_facts(session: Session, *, product_id: int | None = None, store_id: int | None = None) -> list[PurchaseFact]:
    query = (
        select(PurchaseBatchItem)
        .join(PurchaseBatch, PurchaseBatch.id == PurchaseBatchItem.purchase_batch_id)
        .join(Receipt, Receipt.id == PurchaseBatch.receipt_id)
        .where(PurchaseBatch.status != "cancelled")
        .options(
            selectinload(PurchaseBatchItem.product),
            selectinload(PurchaseBatchItem.purchase_batch).selectinload(PurchaseBatch.receipt).selectinload(Receipt.batch),
            selectinload(PurchaseBatchItem.purchase_batch).selectinload(PurchaseBatch.store),
            selectinload(PurchaseBatchItem.purchase_batch).selectinload(PurchaseBatch.receipt).selectinload(Receipt.store),
        )
    )
    if product_id is not None:
        query = query.where(PurchaseBatchItem.product_id == product_id)
    items = list(session.scalars(query.order_by(PurchaseBatch.purchased_at, PurchaseBatchItem.id)))
    facts = []
    for item in items:
        batch, receipt = item.purchase_batch, item.purchase_batch.receipt
        store = batch.store or receipt.store
        if store is None or (store_id is not None and store.id != store_id):
            continue
        unit_price = item.actual_line_amount / item.quantity if item.actual_line_amount is not None and item.quantity else None
        facts.append(PurchaseFact(item, batch, receipt, store, unit_price))
    return facts


def _date_key(fact: PurchaseFact) -> tuple[datetime, int]:
    return (fact.batch.purchased_at or fact.batch.confirmed_at, fact.item.id)


def product_store_summaries(session: Session, product_id: int) -> list[dict]:
    groups: dict[int, list[PurchaseFact]] = {}
    for fact in purchase_facts(session, product_id=product_id):
        groups.setdefault(fact.store.id, []).append(fact)
    rows = []
    for facts in groups.values():
        latest = max(facts, key=_date_key)
        prices = [fact.reference_unit_price for fact in facts if fact.reference_unit_price is not None]
        rows.append({
            "store": latest.store, "purchase_count": len({fact.batch.id for fact in facts}),
            "quantity": sum(fact.item.quantity for fact in facts), "minimum_price": min(prices) if prices else None,
            "latest_price": latest.reference_unit_price, "latest_date": latest.batch.purchased_at,
            "latest_batch": latest.batch, "latest_receipt": latest.receipt,
        })
    return sorted(rows, key=lambda row: (row["latest_date"] or datetime.min), reverse=True)


def store_product_summaries(session: Session, store_id: int) -> tuple[list[dict], list[PurchaseFact]]:
    facts = purchase_facts(session, store_id=store_id)
    groups: dict[int, list[PurchaseFact]] = {}
    for fact in facts:
        groups.setdefault(fact.item.product_id, []).append(fact)
    rows = []
    for product_facts in groups.values():
        latest = max(product_facts, key=_date_key)
        rows.append({
            "product": latest.item.product, "purchase_count": len({fact.batch.id for fact in product_facts}),
            "quantity": sum(fact.item.quantity for fact in product_facts),
            "total_amount": sum(fact.item.actual_line_amount or 0 for fact in product_facts),
            "latest_date": latest.batch.purchased_at, "latest_batch": latest.batch, "latest_receipt": latest.receipt,
        })
    return sorted(rows, key=lambda row: (row["latest_date"] or datetime.min), reverse=True), sorted(facts, key=_date_key, reverse=True)


def product_trend_points(facts: list[PurchaseFact]) -> list[dict]:
    priced = [fact for fact in facts if fact.reference_unit_price is not None]
    if not priced:
        return []
    low, high = min(fact.reference_unit_price for fact in priced), max(fact.reference_unit_price for fact in priced)
    span = max(1, high - low)
    count = len(priced)
    return [{
        "fact": fact, "x": 30 if count == 1 else 30 + index * 540 / (count - 1),
        "y": 185 - (fact.reference_unit_price - low) * 145 / span,
    } for index, fact in enumerate(priced)]

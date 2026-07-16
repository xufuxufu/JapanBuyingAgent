from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta, timezone
from difflib import SequenceMatcher

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Product, ProductAlias, ProductMatchLog, Receipt, ReceiptBatch, ReceiptItem
from app.product_identity import assert_jan_available


GTIN_LENGTHS = {8, 12, 13, 14}


def normalize_alias(value: str | None) -> str:
    return re.sub(r"\s+", "", (value or "").strip().casefold())


def validate_jan(value: str | None) -> bool:
    if not value or not value.isdigit() or len(value) not in GTIN_LENGTHS:
        return False
    digits = [int(char) for char in value]
    weighted = sum(digit * (3 if (len(digits) - index) % 2 == 0 else 1) for index, digit in enumerate(digits[:-1]))
    return (10 - weighted % 10) % 10 == digits[-1]


def _record(session: Session, item: ReceiptItem, old_product_id: int | None, method: str, decision: str) -> None:
    session.add(ProductMatchLog(
        receipt_item_id=item.id, old_product_id=old_product_id, new_product_id=item.product_id,
        method=method, decision=decision,
    ))


def match_item(session: Session, item: ReceiptItem, force: bool = False) -> ReceiptItem:
    if item.review_status != "confirmed":
        return item
    if item.product_id and item.match_method in {"manual", "manual_new"} and not force:
        return item
    old_product_id = item.product_id
    jan = (item.jan_candidate or "").strip()
    item.product_id = None
    item.match_confidence = None
    if not jan:
        alias = normalize_alias(item.recognized_name or item.raw_name)
        recommendation = session.scalar(select(ProductAlias).where(ProductAlias.normalized_alias == alias, ProductAlias.confirmed.is_(True))) if alias else None
        fuzzy = False
        if not recommendation and alias:
            fuzzy = any(SequenceMatcher(None, alias, normalize_alias(product.name_cn)).ratio() >= 0.55 for product in session.scalars(select(Product).where(Product.name_cn.is_not(None))))
        item.match_status = "needs_review"
        item.match_method = "alias_recommendation" if recommendation else ("name_candidate" if fuzzy else "no_jan")
        decision = "recommended" if recommendation else ("candidate" if fuzzy else "needs_review")
    elif not validate_jan(jan):
        item.match_status, item.match_method = "invalid_jan", "jan_validation"
        decision = "invalid_jan"
    else:
        products = list(session.scalars(select(Product).where(Product.jan == jan)))
        if len(products) == 1:
            item.product_id = products[0].id
            item.match_status, item.match_method, item.match_confidence = "matched_existing", "jan_exact", 1.0
            decision = "matched_existing"
        elif not products:
            item.match_status, item.match_method = "new_product", "jan_not_found"
            decision = "new_product"
        else:
            item.match_status, item.match_method = "conflict", "jan_multiple"
            decision = "conflict"
    item.matched_at = datetime.now(timezone.utc)
    session.flush()
    _record(session, item, old_product_id, item.match_method or "unknown", decision)
    return item


def _sync_batch_product_status(batch: ReceiptBatch) -> None:
    statuses = {item.match_status for receipt in batch.receipts for item in receipt.items if item.review_status != "ignored"}
    if statuses and statuses <= {"matched_existing", "new_product"}:
        batch.product_status = "matched"
    elif statuses:
        batch.product_status = "needs_review"
    else:
        batch.product_status = "not_matched"


def match_receipt(session: Session, receipt: Receipt, force: bool = False, commit: bool = True) -> int:
    count = 0
    for item in receipt.items:
        if item.review_status == "confirmed":
            match_item(session, item, force=force)
            count += 1
    _sync_batch_product_status(receipt.batch)
    if commit:
        session.commit()
        from app.product_enrichment import safe_trigger_receipt_items
        safe_trigger_receipt_items(session, list(receipt.items), "receipt_matching")
    return count


def match_batch(session: Session, batch: ReceiptBatch, force: bool = False, commit: bool = True) -> int:
    count = sum(match_receipt(session, receipt, force=force, commit=False) for receipt in batch.receipts)
    _sync_batch_product_status(batch)
    if commit:
        session.commit()
        from app.product_enrichment import safe_trigger_receipt_items
        safe_trigger_receipt_items(
            session, [item for receipt in batch.receipts for item in receipt.items], "receipt_matching",
        )
    return count


def match_date(session: Session, purchased_date: date, force: bool = False) -> int:
    tokyo = timezone(timedelta(hours=9))
    start = datetime.combine(purchased_date, time.min, tokyo).astimezone(timezone.utc)
    end = datetime.combine(purchased_date, time.max, tokyo).astimezone(timezone.utc)
    receipts = list(session.scalars(select(Receipt).where(Receipt.purchased_at >= start, Receipt.purchased_at <= end)))
    count = sum(match_receipt(session, receipt, force=force, commit=False) for receipt in receipts)
    session.commit()
    from app.product_enrichment import safe_trigger_receipt_items
    safe_trigger_receipt_items(session, [item for receipt in receipts for item in receipt.items], "receipt_matching")
    return count


def bind_product(session: Session, item: ReceiptItem, product: Product) -> None:
    old_product_id = item.product_id
    item.product_id = product.id
    item.match_status, item.match_method, item.match_confidence = "matched_existing", "manual", 1.0
    item.matched_at = datetime.now(timezone.utc)
    alias_text = (item.recognized_name or item.raw_name).strip()
    normalized = normalize_alias(alias_text)
    if normalized and not session.scalar(select(ProductAlias).where(ProductAlias.product_id == product.id, ProductAlias.normalized_alias == normalized)):
        session.add(ProductAlias(product_id=product.id, alias=alias_text, normalized_alias=normalized, confirmed=True, created_from_item_id=item.id))
    session.flush()
    _record(session, item, old_product_id, "manual", "matched_existing")
    _sync_batch_product_status(item.receipt.batch)
    if item.receipt.confirmation_status == "confirmed":
        from app.purchase_service import ensure_purchase_batch_for_receipt
        ensure_purchase_batch_for_receipt(session, item.receipt)
    session.commit()


def create_product_from_item(session: Session, item: ReceiptItem) -> Product:
    jan = (item.jan_candidate or "").strip()
    product_jan = assert_jan_available(session, jan) if validate_jan(jan) else None
    product = Product(
        jan=product_jan,
        name_cn=(item.recognized_name or item.raw_name).strip(), purchase_price=item.unit_price,
        product_data_confirmed=True, name_locked=True,
        product_origin="receipt", source="receipt_manual",
    )
    session.add(product)
    session.flush()
    old_product_id = item.product_id
    item.product_id = product.id
    item.match_status, item.match_method, item.match_confidence = "new_product", "manual_new", 1.0
    item.matched_at = datetime.now(timezone.utc)
    _record(session, item, old_product_id, "manual_new", "new_product")
    _sync_batch_product_status(item.receipt.batch)
    if item.receipt.confirmation_status == "confirmed":
        from app.purchase_service import ensure_purchase_batch_for_receipt
        ensure_purchase_batch_for_receipt(session, item.receipt)
    session.commit()
    session.refresh(product)
    return product

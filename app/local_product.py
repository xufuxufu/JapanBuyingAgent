from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Product, ProductAlias, ProductBarcode
from app.product_matching import validate_jan


QINSI_DERIVED_BARCODE_SOURCE = "qinsi_sku_derived"
_QINSI_DERIVED_JAN_PATTERN = re.compile(r"^/([0-9]{8}|[0-9]{13})$")
ACTIVE_PRODUCT_STATUSES = {
    "active",
    "new_pending_completion",
    "new_pending_review",
    "pending_qinsi_product_import",
    "qinsi_product_imported",
}


@dataclass(frozen=True)
class LocalProductResolution:
    status: str
    jan: str | None
    product: Product | None = None
    match_method: str | None = None
    candidate_product_ids: tuple[int, ...] = ()
    candidate_products: tuple[Product, ...] = ()

    @property
    def is_conflict(self) -> bool:
        return self.status == "AMBIGUOUS"

    @property
    def is_unique(self) -> bool:
        return self.status == "UNIQUE"

    @property
    def is_not_found(self) -> bool:
        return self.status == "NOT_FOUND"


def normalize_jan(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = unicodedata.normalize("NFKC", value).strip()
    return normalized or None


def is_valid_jan(value: str | None) -> bool:
    return bool(value and validate_jan(value))


def derive_jan_from_qinsi_sku(value: str | None) -> str | None:
    candidate = (value or "").strip()
    match = _QINSI_DERIVED_JAN_PATTERN.fullmatch(candidate)
    if match is None:
        return None
    jan = match.group(1)
    return jan if is_valid_jan(jan) else None


def _active_product_query():
    return select(Product).where(Product.status.in_(ACTIVE_PRODUCT_STATUSES))


def _products_for_ids(session: Session, product_ids: set[int]) -> list[Product]:
    if not product_ids:
        return []
    return list(session.scalars(_active_product_query().where(Product.id.in_(product_ids)).order_by(Product.id)))


def resolve_local_product_by_jan(
    session: Session,
    value: str | None,
) -> LocalProductResolution:
    jan = normalize_jan(value)
    if not is_valid_jan(jan):
        return LocalProductResolution(status="INVALID", jan=jan)

    direct_products = list(
        session.scalars(_active_product_query().where(Product.jan == jan).order_by(Product.id))
    )
    if len(direct_products) == 1:
        product = direct_products[0]
        return LocalProductResolution(
            status="UNIQUE",
            jan=jan,
            product=product,
            match_method="product_jan",
            candidate_product_ids=(product.id,),
            candidate_products=(product,),
        )
    if len(direct_products) > 1:
        product_ids = tuple(product.id for product in direct_products)
        return LocalProductResolution(
            status="AMBIGUOUS",
            jan=jan,
            candidate_product_ids=product_ids,
            candidate_products=tuple(direct_products),
        )

    matches: list[tuple[str, Product]] = []
    barcode_rows = list(session.scalars(
        select(ProductBarcode)
        .where(
            ProductBarcode.barcode == jan,
            ProductBarcode.source_system != QINSI_DERIVED_BARCODE_SOURCE,
        )
        .order_by(ProductBarcode.id)
    ))
    barcode_product_ids = {row.product_id for row in barcode_rows}
    matches.extend(("product_barcode", product) for product in _products_for_ids(session, barcode_product_ids))

    alias_rows = session.scalars(
        select(ProductAlias)
        .where(
            ProductAlias.confirmed.is_(True),
            ProductAlias.alias == jan,
        )
        .order_by(ProductAlias.id)
    )
    alias_product_ids = {row.product_id for row in alias_rows if is_valid_jan(row.alias)}
    matches.extend(("product_alias_jan", product) for product in _products_for_ids(session, alias_product_ids))

    product_ids = tuple(sorted({product.id for _, product in matches}))
    candidate_products = tuple(_products_for_ids(session, set(product_ids)))
    if len(product_ids) > 1:
        return LocalProductResolution(
            status="AMBIGUOUS",
            jan=jan,
            candidate_product_ids=product_ids,
            candidate_products=candidate_products,
        )
    if not product_ids:
        return LocalProductResolution(status="NOT_FOUND", jan=jan)

    product_id = product_ids[0]
    method, product = next((method, product) for method, product in matches if product.id == product_id)
    return LocalProductResolution(
        status="UNIQUE",
        jan=jan,
        product=product,
        match_method=method,
        candidate_product_ids=product_ids,
        candidate_products=candidate_products,
    )


def ensure_qinsi_derived_barcode(
    session: Session,
    product: Product,
) -> ProductBarcode | None:
    jan = derive_jan_from_qinsi_sku(product.qinsi_product_code)
    if jan is None:
        return None
    resolution = resolve_local_product_by_jan(session, jan)
    if resolution.is_conflict or (
        resolution.product is not None and resolution.product.id != product.id
    ):
        raise ValueError(f"派生条码 {jan} 对应多个商品，需人工处理")
    existing_rows = list(
        session.scalars(
            select(ProductBarcode)
            .where(ProductBarcode.barcode == jan)
            .order_by(ProductBarcode.id)
        )
    )
    conflicting = [row for row in existing_rows if row.product_id != product.id]
    if conflicting:
        raise ValueError(f"派生条码 {jan} 已关联其他商品，需人工处理")
    if existing_rows:
        return existing_rows[0]
    row = ProductBarcode(
        product_id=product.id,
        barcode=jan,
        source_system=QINSI_DERIVED_BARCODE_SOURCE,
        is_primary=False,
    )
    session.add(row)
    return row

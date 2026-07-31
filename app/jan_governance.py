from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.config import REPORT_DIR
from app.local_product import (
    derive_jan_from_qinsi_sku,
    ensure_qinsi_derived_barcode,
    is_valid_jan,
    normalize_jan,
    resolve_local_product_by_jan,
)
from app.models import Product, ProductBarcode


@dataclass(frozen=True, slots=True)
class JanGovernanceRow:
    jan: str
    local_product: str
    qinsi_product_code: str
    qinsi_barcode: str
    conflict_type: str
    suggested_jan: str
    suggested_action: str


@dataclass(frozen=True, slots=True)
class JanGovernanceReport:
    rows: tuple[JanGovernanceRow, ...]
    conflict_count: int
    auto_fixed_count: int
    csv_path: Path


def _label(product: Product) -> str:
    name = product.display_name or product.name_cn or product.name_ja or "未命名"
    spec = product.specification or product.model_spec or "规格未填"
    return f"{product.internal_sku} · {name} · {spec}"


def _qinsi_barcodes(product: Product) -> str:
    return "、".join(
        row.barcode for row in product.barcodes
        if row.source_system in {"qinsi", "qinsi_sku_derived"}
    )


def _safe_backfill(session: Session, products: list[Product]) -> int:
    fixed = 0
    for product in products:
        jan = derive_jan_from_qinsi_sku(product.qinsi_product_code)
        if jan is None:
            continue
        before = any(
            row.barcode == jan and row.source_system == "qinsi_sku_derived"
            for row in product.barcodes
        )
        resolution = resolve_local_product_by_jan(session, jan)
        if resolution.is_conflict or (
            resolution.product is not None and resolution.product.id != product.id
        ):
            continue
        ensure_qinsi_derived_barcode(session, product)
        session.flush()
        after = session.scalar(
            select(ProductBarcode.id).where(
                ProductBarcode.product_id == product.id,
                ProductBarcode.barcode == jan,
                ProductBarcode.source_system == "qinsi_sku_derived",
            )
        )
        if not before and after is not None:
            fixed += 1
    if fixed:
        session.commit()
    return fixed


def build_jan_governance_rows(
    session: Session,
    *,
    apply_safe_fixes: bool = False,
) -> tuple[list[JanGovernanceRow], int]:
    products = list(
        session.scalars(
            select(Product)
            .options(selectinload(Product.barcodes))
            .order_by(Product.id)
        )
    )
    auto_fixed = _safe_backfill(session, products) if apply_safe_fixes else 0
    if auto_fixed:
        products = list(
            session.scalars(
                select(Product).options(selectinload(Product.barcodes)).order_by(Product.id)
            )
        )

    rows: list[JanGovernanceRow] = []
    sources: dict[str, dict[int, Product]] = {}
    product_jans: dict[int, set[str]] = {product.id: set() for product in products}
    for product in products:
        raw_product_jan = normalize_jan(product.jan)
        if raw_product_jan:
            if is_valid_jan(raw_product_jan):
                sources.setdefault(raw_product_jan, {})[product.id] = product
                product_jans[product.id].add(raw_product_jan)
            else:
                rows.append(JanGovernanceRow(
                    raw_product_jan, _label(product), product.qinsi_product_code or "",
                    _qinsi_barcodes(product), "INVALID_PRODUCT_JAN", "",
                    "人工确认后修正本地 JAN；不得伪造",
                ))
        for barcode in product.barcodes:
            value = normalize_jan(barcode.barcode)
            if not value:
                continue
            if is_valid_jan(value):
                sources.setdefault(value, {})[product.id] = product
                product_jans[product.id].add(value)
            else:
                rows.append(JanGovernanceRow(
                    value, _label(product), product.qinsi_product_code or "",
                    _qinsi_barcodes(product), "INVALID_QINSI_BARCODE", "",
                    "在线修改秦丝条码；本地不自动去符号",
                ))
        exact_code = normalize_jan(product.qinsi_product_code)
        if is_valid_jan(exact_code):
            sources.setdefault(exact_code, {})[product.id] = product
            product_jans[product.id].add(exact_code)
        derived = derive_jan_from_qinsi_sku(product.qinsi_product_code)
        if derived:
            sources.setdefault(derived, {})[product.id] = product
            product_jans[product.id].add(derived)

    for jan, matched in sorted(sources.items()):
        if len(matched) > 1:
            for product in matched.values():
                rows.append(JanGovernanceRow(
                    jan, _label(product), product.qinsi_product_code or "",
                    _qinsi_barcodes(product), "AMBIGUOUS", "",
                    "禁止自动选品/新增/累加；人工确认并在线修正秦丝",
                ))

    for product in products:
        valid_values = sorted(product_jans[product.id])
        if len(valid_values) > 1:
            suggested = normalize_jan(product.jan)
            if not is_valid_jan(suggested):
                suggested = ""
            rows.append(JanGovernanceRow(
                "、".join(valid_values), _label(product), product.qinsi_product_code or "",
                _qinsi_barcodes(product), "PRODUCT_JAN_DISAGREEMENT", suggested or "",
                "人工核对包装后统一本地与秦丝；不自动覆盖",
            ))

    rows.sort(key=lambda row: (row.jan, row.conflict_type, row.local_product))
    return rows, auto_fixed


def export_jan_governance_report(
    session: Session,
    *,
    apply_safe_fixes: bool = False,
    output_dir: Path = REPORT_DIR,
    now: datetime | None = None,
) -> JanGovernanceReport:
    rows, auto_fixed = build_jan_governance_rows(
        session, apply_safe_fixes=apply_safe_fixes,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = (now or datetime.now()).strftime("%Y%m%d_%H%M%S")
    path = output_dir / f"jan_governance_{stamp}.csv"
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=[
            "JAN", "本地商品", "秦丝货号", "秦丝条码",
            "冲突类型", "建议JAN", "建议动作",
        ])
        writer.writeheader()
        writer.writerows({
            "JAN": row.jan,
            "本地商品": row.local_product,
            "秦丝货号": row.qinsi_product_code,
            "秦丝条码": row.qinsi_barcode,
            "冲突类型": row.conflict_type,
            "建议JAN": row.suggested_jan,
            "建议动作": row.suggested_action,
        } for row in rows)
    return JanGovernanceReport(
        rows=tuple(rows),
        conflict_count=len(rows),
        auto_fixed_count=auto_fixed,
        csv_path=path,
    )

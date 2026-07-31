from __future__ import annotations

from sqlalchemy import select

from app.jan_governance import export_jan_governance_report
from app.models import Product, ProductBarcode


def test_jan_governance_exports_conflicts_and_only_auto_fixes_safe_aliases(
    db_session,
    tmp_path,
):
    ambiguous_jan = "4550726010198"
    safe_jan = "4901234567894"
    explicit = Product(name_cn="明确 JAN", name_ja="明確", jan=ambiguous_jan)
    ambiguous = Product(name_cn="派生冲突", name_ja="競合", qinsi_product_code=f"/{ambiguous_jan}")
    safe = Product(name_cn="安全派生", name_ja="安全", qinsi_product_code=f"/{safe_jan}")
    invalid = Product(name_cn="非法条码", name_ja="不正")
    db_session.add_all([explicit, ambiguous, safe, invalid])
    db_session.flush()
    db_session.add(ProductBarcode(
        product_id=invalid.id,
        barcode="1234567890",
        source_system="qinsi",
        is_primary=True,
    ))
    db_session.commit()

    report = export_jan_governance_report(
        db_session,
        apply_safe_fixes=True,
        output_dir=tmp_path,
    )
    assert report.auto_fixed_count == 1
    assert report.conflict_count == 3
    assert {row.conflict_type for row in report.rows} == {
        "AMBIGUOUS", "INVALID_QINSI_BARCODE",
    }
    assert sum(row.conflict_type == "AMBIGUOUS" for row in report.rows) == 2
    safe_alias = db_session.scalar(select(ProductBarcode).where(
        ProductBarcode.product_id == safe.id,
        ProductBarcode.barcode == safe_jan,
        ProductBarcode.source_system == "qinsi_sku_derived",
    ))
    assert safe_alias is not None
    assert db_session.scalar(select(ProductBarcode).where(
        ProductBarcode.product_id == ambiguous.id,
        ProductBarcode.barcode == ambiguous_jan,
        ProductBarcode.source_system == "qinsi_sku_derived",
    )) is None
    content = report.csv_path.read_text(encoding="utf-8-sig")
    assert content.splitlines()[0] == "JAN,本地商品,秦丝货号,秦丝条码,冲突类型,建议JAN,建议动作"
    assert ambiguous_jan in content and "AMBIGUOUS" in content

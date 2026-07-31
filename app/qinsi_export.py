from __future__ import annotations

import hashlib
import io
import json
import uuid
import zipfile
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path, PurePosixPath
from xml.etree import ElementTree as ET

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.config import PROJECT_ROOT
from app.models import (
    Product,
    PurchaseBatch,
    PurchaseBatchItem,
    QinsiExportJob,
    QinsiExportLine,
    QinsiPurchaseExportJob,
    QinsiPurchaseExportLine,
    QinsiPurchaseExportLineSource,
    ReceiptBatch,
)
from app.product_identity import format_product_display_name
from app.schemas import QinsiExportConfirmationInput


GOODS_TEMPLATE_PATH = PROJECT_ROOT / "ExcelTemplate" / "goodsImportTemplate-秦丝新增商品模版.xlsx"
PURCHASE_TEMPLATE_PATH = PROJECT_ROOT / "ExcelTemplate" / "秦丝采购单商品导入模板.xlsx"
QINSI_GOODS_TEMPLATE_HEADERS = (
    "名称（必填）", "货号（必填且唯一）", "条码", "型号规格", "品牌", "分类", "单位",
    "采购价", "销售价", "最低销售价", "排序", "状态", "启用积分", "库存预警下限",
    "库存预警上限", "保质期（天）", "启用批次", "过期预警（天）", "商品图片链接",
    "商品备注", "产地", "适用年龄", "商品重量（KG）", "启用序列号", "库位",
    "盘点库存数量", "当前库存（导入时不需要录入）", "盘点仓库:", "新日本仓库",
)
QINSI_PURCHASE_TEMPLATE_HEADERS = (
    "条码", "货号", "单位", "数量(必填)", "单价", "折扣(%)", "备注(20字以内)",
)
QINSI_TEMPLATE_HEADERS = QINSI_GOODS_TEMPLATE_HEADERS
QINSI_PRODUCT_EXPORTABLE_STATUSES = {
    "new_pending_completion", "new_pending_review", "pending_qinsi_product_import",
}
SHEET_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
OFFICE_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PACKAGE_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
NS = {"m": SHEET_NS, "r": OFFICE_REL_NS}
REL_NS = {"p": PACKAGE_REL_NS}


@dataclass(frozen=True, slots=True)
class ExportRow:
    purchase_batch_item: PurchaseBatchItem
    product: Product
    code: str


@dataclass(frozen=True, slots=True)
class PurchaseAggregate:
    product: Product
    details: tuple[PurchaseBatchItem, ...]
    quantity: int
    total_paid: int
    unit_price: Decimal


def _column_name(number: int) -> str:
    result = ""
    while number:
        number, remainder = divmod(number - 1, 26)
        result = chr(65 + remainder) + result
    return result


def _sheet_path(archive: zipfile.ZipFile, sheet_name: str) -> str:
    workbook = ET.fromstring(archive.read("xl/workbook.xml"))
    relationships = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    targets = {node.attrib["Id"]: node.attrib["Target"] for node in relationships.findall("p:Relationship", REL_NS)}
    sheet = next((node for node in workbook.findall("m:sheets/m:sheet", NS) if node.attrib.get("name") == sheet_name), None)
    if sheet is None:
        raise ValueError(f"秦丝模板缺少“{sheet_name}”工作表")
    target = targets.get(sheet.attrib.get(f"{{{OFFICE_REL_NS}}}id", ""))
    if not target:
        raise ValueError("秦丝模板工作表关系损坏")
    return str(PurePosixPath("xl") / target.lstrip("/")) if not target.startswith("xl/") else target


def _cell(row: ET.Element, row_no: int, column_no: int) -> ET.Element:
    reference = f"{_column_name(column_no)}{row_no}"
    existing = next((node for node in row.findall(f"{{{SHEET_NS}}}c") if node.attrib.get("r") == reference), None)
    if existing is not None:
        return existing
    node = ET.Element(f"{{{SHEET_NS}}}c", {"r": reference})
    cells = list(row.findall(f"{{{SHEET_NS}}}c"))
    before = next((item for item in cells if _cell_column(item.attrib.get("r", "")) > column_no), None)
    row.insert(list(row).index(before), node) if before is not None else row.append(node)
    return node


def _cell_column(reference: str) -> int:
    value = 0
    for char in reference:
        if not char.isalpha():
            break
        value = value * 26 + ord(char.upper()) - 64
    return value


def _set_cell(row: ET.Element, row_no: int, column_no: int, value: str | int | Decimal | None) -> None:
    cell = _cell(row, row_no, column_no)
    for child in list(cell):
        cell.remove(child)
    cell.attrib.pop("t", None)
    if value is None or value == "":
        return
    if isinstance(value, (int, Decimal)):
        ET.SubElement(cell, f"{{{SHEET_NS}}}v").text = format(value, "f")
        return
    cell.attrib["t"] = "inlineStr"
    inline = ET.SubElement(cell, f"{{{SHEET_NS}}}is")
    ET.SubElement(inline, f"{{{SHEET_NS}}}t").text = str(value)


def _shared_strings(archive: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in archive.namelist():
        return []
    root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
    return ["".join(node.text or "" for node in item.findall(".//m:t", NS)) for item in root.findall("m:si", NS)]


def _cell_text(cell: ET.Element, shared: list[str]) -> str:
    if cell.attrib.get("t") == "inlineStr":
        return "".join(node.text or "" for node in cell.findall(".//m:t", NS)).strip()
    value = cell.find("m:v", NS)
    raw = value.text if value is not None and value.text is not None else ""
    if cell.attrib.get("t") == "s":
        try:
            return shared[int(raw)].strip()
        except (ValueError, IndexError) as exc:
            raise ValueError("秦丝模板 sharedStrings 索引损坏") from exc
    return raw.strip()


def _template_headers(source: bytes, sheet_name: str) -> tuple[str, ...]:
    with zipfile.ZipFile(io.BytesIO(source), "r") as archive:
        target_path = _sheet_path(archive, sheet_name)
        sheet = ET.fromstring(archive.read(target_path))
        header = next((row for row in sheet.findall("m:sheetData/m:row", NS) if row.attrib.get("r") == "1"), None)
        if header is None:
            raise ValueError("秦丝模板缺少表头")
        shared = _shared_strings(archive)
        cells = {_cell_column(cell.attrib.get("r", "")): _cell_text(cell, shared) for cell in header.findall("m:c", NS)}
        return tuple(cells.get(index, "") for index in range(1, max(cells, default=0) + 1))


def _rewrite_template(
    template_path: Path,
    sheet_name: str,
    expected_headers: tuple[str, ...],
    output_rows: list[dict[int, str | int | Decimal | None]],
    *,
    require_preformatted_rows: bool = False,
) -> bytes:
    if not output_rows:
        raise ValueError("没有可导出的明细")
    source = template_path.read_bytes()
    if _template_headers(source, sheet_name) != expected_headers:
        raise ValueError(f"秦丝模板“{sheet_name}”列名已变化，已停止生成")
    output_buffer = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(source), "r") as source_zip, zipfile.ZipFile(output_buffer, "w") as output_zip:
        target_path = _sheet_path(source_zip, sheet_name)
        sheet = ET.fromstring(source_zip.read(target_path))
        sheet_data = sheet.find(f"{{{SHEET_NS}}}sheetData")
        if sheet_data is None:
            raise ValueError(f"秦丝模板“{sheet_name}”结构损坏")
        row_nodes = {int(row.attrib["r"]): row for row in sheet_data.findall(f"{{{SHEET_NS}}}row")}
        if require_preformatted_rows and len(output_rows) > max(0, len(row_nodes) - 1):
            raise ValueError(f"秦丝新商品模板最多支持 {max(0, len(row_nodes) - 1)} 条明细")
        for row_no, values in enumerate(output_rows, 2):
            row = row_nodes.get(row_no)
            if row is None:
                row = ET.SubElement(sheet_data, f"{{{SHEET_NS}}}row", {"r": str(row_no)})
            for column_no in range(1, len(expected_headers) + 1):
                _set_cell(row, row_no, column_no, None)
            for column_no, value in values.items():
                _set_cell(row, row_no, column_no, value)
        dimension = sheet.find(f"{{{SHEET_NS}}}dimension")
        if dimension is not None:
            dimension.attrib["ref"] = f"A1:{_column_name(len(expected_headers))}{len(output_rows) + 1}"
        ET.register_namespace("", SHEET_NS)
        ET.register_namespace("r", OFFICE_REL_NS)
        rewritten = ET.tostring(sheet, encoding="utf-8", xml_declaration=True)
        for info in source_zip.infolist():
            output_zip.writestr(info, rewritten if info.filename == target_path else source_zip.read(info.filename))
    return output_buffer.getvalue()


def _template_bytes(rows: list[ExportRow], warehouse_name: str) -> bytes:
    """Compatibility helper used by the existing inventory-snapshot fixtures."""
    output_rows: list[dict[int, str | int | Decimal | None]] = []
    for export_row in rows:
        detail, product = export_row.purchase_batch_item, export_row.product
        output_rows.append({
            1: product.name_cn or product.name_ja or product.internal_sku,
            2: export_row.code,
            3: product.jan,
            4: product.model_spec or product.specification,
            8: detail.unit_price if detail.unit_price is not None else product.purchase_price,
            9: product.sale_price if product.sale_price is not None else 0,
            10: product.minimum_sale_price,
            11: 100,
            12: "启用" if product.status == "active" else "停用",
            13: "启用",
            19: product.image_url,
            25: product.location_code,
            26: detail.quantity,
            29: detail.quantity,
        })
    content = _rewrite_template(
        GOODS_TEMPLATE_PATH, "商品导入", QINSI_GOODS_TEMPLATE_HEADERS, output_rows,
        require_preformatted_rows=True,
    )
    if warehouse_name == QINSI_GOODS_TEMPLATE_HEADERS[28]:
        return content
    output_buffer = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(content), "r") as source_zip, zipfile.ZipFile(output_buffer, "w") as output_zip:
        target_path = _sheet_path(source_zip, "商品导入")
        sheet = ET.fromstring(source_zip.read(target_path))
        header = next(
            (row for row in sheet.findall("m:sheetData/m:row", NS) if row.attrib.get("r") == "1"),
            None,
        )
        if header is None:
            raise ValueError("秦丝模板缺少表头")
        _set_cell(header, 1, 29, warehouse_name)
        ET.register_namespace("", SHEET_NS)
        ET.register_namespace("r", OFFICE_REL_NS)
        rewritten = ET.tostring(sheet, encoding="utf-8", xml_declaration=True)
        for info in source_zip.infolist():
            output_zip.writestr(info, rewritten if info.filename == target_path else source_zip.read(info.filename))
    return output_buffer.getvalue()


def _product_export_name(product: Product) -> str:
    if not product.jan:
        raise ValueError(f"商品 {product.internal_sku} 缺少 JAN，不能生成秦丝新商品文件")
    if product.status == "new_pending_completion" or not (product.name_cn or product.name_ja):
        return f"{product.jan}|缺商品"
    return format_product_display_name(product.name_cn, product.name_ja)


def _goods_template_bytes(products: list[Product]) -> bytes:
    unique: dict[str, Product] = {}
    for product in products:
        if not product.jan:
            raise ValueError(f"商品 {product.internal_sku} 缺少 JAN，不能生成秦丝新商品文件")
        unique.setdefault(product.jan, product)
    rows = [{
        1: _product_export_name(product),
        2: product.jan,
        3: product.jan,
        7: "个",
        8: product.purchase_price,
        9: product.purchase_price,
        11: 100,
        12: "启用",
        13: "启用",
        17: "停用",
        19: product.main_image_source_url or product.image_url,
        24: "停用",
    } for product in unique.values()]
    return _rewrite_template(
        GOODS_TEMPLATE_PATH, "商品导入", QINSI_GOODS_TEMPLATE_HEADERS, rows,
        require_preformatted_rows=True,
    )


def _aggregate_purchase_rows(details: list[PurchaseBatchItem]) -> list[PurchaseAggregate]:
    grouped: dict[str, list[PurchaseBatchItem]] = defaultdict(list)
    for detail in details:
        if not detail.product.jan:
            raise ValueError(f"商品 {detail.product.internal_sku} 缺少 JAN，不能生成采购导入文件")
        grouped[detail.product.jan].append(detail)
    output: list[PurchaseAggregate] = []
    for jan, group in grouped.items():
        quantity = sum(detail.quantity for detail in group)
        amounts: list[int] = []
        for detail in group:
            amount = detail.actual_line_amount
            if amount is None and detail.unit_price is not None:
                amount = detail.unit_price * detail.quantity - (detail.discount_amount or 0)
            if amount is None:
                raise ValueError(f"JAN {jan} 存在无法计算实付金额的采购明细")
            amounts.append(amount)
        total_paid = sum(amounts)
        unit_price = (Decimal(total_paid) / Decimal(quantity)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        output.append(PurchaseAggregate(group[0].product, tuple(group), quantity, total_paid, unit_price))
    return output


def _purchase_template_bytes(
    details: list[PurchaseBatchItem], purchase_batch: PurchaseBatch, *, note: str | None = None,
) -> bytes:
    aggregates = _aggregate_purchase_rows(details)
    note = (note or purchase_batch.batch_no or purchase_batch.store_name or "")[:20] or None
    rows = [{
        1: aggregate.product.jan,
        2: aggregate.product.jan,
        3: "个",
        4: aggregate.quantity,
        5: aggregate.unit_price,
        6: None,
        7: note,
    } for aggregate in aggregates]
    return _rewrite_template(PURCHASE_TEMPLATE_PATH, "采购单商品导入", QINSI_PURCHASE_TEMPLATE_HEADERS, rows)


def _job_options():
    return (
        selectinload(QinsiPurchaseExportJob.purchase_batch),
        selectinload(QinsiPurchaseExportJob.qinsi_target_warehouse),
        selectinload(QinsiPurchaseExportJob.lines).selectinload(QinsiPurchaseExportLine.product),
        selectinload(QinsiPurchaseExportJob.lines).selectinload(QinsiPurchaseExportLine.purchase_batch_item),
        selectinload(QinsiPurchaseExportJob.lines).selectinload(QinsiPurchaseExportLine.receipt),
        selectinload(QinsiPurchaseExportJob.lines).selectinload(QinsiPurchaseExportLine.receipt_item),
        selectinload(QinsiPurchaseExportJob.lines).selectinload(QinsiPurchaseExportLine.qinsi_target_warehouse),
        selectinload(QinsiPurchaseExportJob.lines).selectinload(QinsiPurchaseExportLine.source),
    )


def get_qinsi_export_job(session: Session, job_id: int) -> QinsiPurchaseExportJob | None:
    return session.scalar(select(QinsiPurchaseExportJob).where(QinsiPurchaseExportJob.id == job_id).options(*_job_options()))


def list_qinsi_export_jobs(session: Session) -> list[QinsiPurchaseExportJob]:
    return list(session.scalars(
        select(QinsiPurchaseExportJob).options(*_job_options()).order_by(QinsiPurchaseExportJob.created_at.desc(), QinsiPurchaseExportJob.id.desc())
    ))


def get_qinsi_product_export_job(session: Session, job_id: int) -> QinsiExportJob | None:
    return session.get(QinsiExportJob, job_id)


def list_qinsi_product_export_jobs(session: Session) -> list[QinsiExportJob]:
    return list(session.scalars(select(QinsiExportJob).order_by(QinsiExportJob.created_at.desc(), QinsiExportJob.id.desc())))


def qinsi_product_export_rows(session: Session, job_id: int) -> list[tuple[QinsiExportLine, Product]]:
    return list(session.execute(
        select(QinsiExportLine, Product)
        .join(Product, Product.id == QinsiExportLine.product_id)
        .where(QinsiExportLine.job_id == job_id)
        .order_by(QinsiExportLine.id)
    ).all())


def create_qinsi_product_export(session: Session, product_ids: set[int]) -> QinsiExportJob:
    if not product_ids:
        raise ValueError("请至少选择一个未导入秦丝的新商品")
    products = list(session.scalars(
        select(Product).where(Product.id.in_(product_ids)).order_by(Product.id)
    ))
    if len(products) != len(product_ids):
        raise ValueError("所选商品包含不存在的记录")
    invalid = [product for product in products if product.status not in QINSI_PRODUCT_EXPORTABLE_STATUSES]
    if invalid:
        labels = "、".join(product.jan or product.internal_sku for product in invalid)
        raise ValueError(f"以下商品不是待导入秦丝状态：{labels}")
    if any(not product.jan for product in products):
        raise ValueError("所选新商品存在缺少 JAN 的记录")
    if len({product.jan for product in products}) != len(products):
        raise ValueError("所选商品中 JAN 重复，已停止生成")
    already_pending = set(session.scalars(
        select(QinsiExportLine.product_id)
        .join(QinsiExportJob, QinsiExportJob.id == QinsiExportLine.job_id)
        .where(QinsiExportLine.product_id.in_(product_ids), QinsiExportJob.status == "exported")
    ))
    if already_pending:
        labels = "、".join(product.jan or product.internal_sku for product in products if product.id in already_pending)
        raise ValueError(f"以下商品已有待确认的新商品导出：{labels}")
    now = datetime.now(timezone.utc)
    filename = f"qinsi_NEW_PRODUCTS_{now:%Y%m%d}_{uuid.uuid4().hex[:10].upper()}.xlsx"
    job = QinsiExportJob(
        status="exported",
        export_filename=filename,
        file_content=_goods_template_bytes(products),
        exported_at=now,
    )
    session.add(job)
    session.flush()
    for product in products:
        session.add(QinsiExportLine(
            job_id=job.id,
            product_id=product.id,
            qinsi_product_code=product.jan,
            product_name=_product_export_name(product),
            quantity=1,
            purchase_price=int(product.purchase_price) if product.purchase_price is not None else None,
            status="exported",
        ))
        if product.status != "new_pending_completion":
            product.status = "pending_qinsi_product_import"
    session.commit()
    session.refresh(job)
    return job


def confirm_qinsi_product_export(session: Session, job: QinsiExportJob, *, actor_name: str) -> QinsiExportJob:
    if job.status == "confirmed":
        return job
    if job.status != "exported":
        raise ValueError("当前新商品导出记录不能确认")
    product_ids: set[int] = set()
    for line, product in qinsi_product_export_rows(session, job.id):
        line.status = "confirmed"
        product.status = "qinsi_product_imported"
        product.product_origin = "qinsi"
        product_ids.add(product.id)
    job.status = "confirmed"
    job.confirmed_at = datetime.now(timezone.utc)
    job.confirmed_by = (actor_name or "人工确认")[:128]
    job.cancelled_at = None
    job.cancelled_by = None
    batch_ids = set(session.scalars(
        select(PurchaseBatchItem.purchase_batch_id).where(PurchaseBatchItem.product_id.in_(product_ids))
    ))
    for batch_id in batch_ids:
        _refresh_receipt_batch_product_status(session, batch_id)
    session.commit()
    session.refresh(job)
    return job


def cancel_qinsi_product_export_confirmation(session: Session, job: QinsiExportJob, *, actor_name: str) -> QinsiExportJob:
    if job.status != "confirmed":
        raise ValueError("只有已确认的新商品导出记录可以撤销")
    product_ids: set[int] = set()
    for line, product in qinsi_product_export_rows(session, job.id):
        line.status = "exported"
        product.status = (
            "new_pending_completion"
            if product.jan and line.product_name == f"{product.jan}|缺商品"
            else "pending_qinsi_product_import"
        )
        product_ids.add(product.id)
    job.status = "exported"
    job.cancelled_at = datetime.now(timezone.utc)
    job.cancelled_by = (actor_name or "管理员")[:128]
    job.confirmed_at = None
    job.confirmed_by = None
    batch_ids = set(session.scalars(
        select(PurchaseBatchItem.purchase_batch_id).where(PurchaseBatchItem.product_id.in_(product_ids))
    ))
    for batch_id in batch_ids:
        _refresh_receipt_batch_product_status(session, batch_id)
    session.commit()
    session.refresh(job)
    return job


def _loaded_purchase_batch(session: Session, purchase_batch_id: int) -> PurchaseBatch | None:
    return session.scalar(
        select(PurchaseBatch).where(PurchaseBatch.id == purchase_batch_id).execution_options(populate_existing=True).options(
            selectinload(PurchaseBatch.items).selectinload(PurchaseBatchItem.product),
            selectinload(PurchaseBatch.items).selectinload(PurchaseBatchItem.receipt_item),
            selectinload(PurchaseBatch.items).selectinload(PurchaseBatchItem.qinsi_target_warehouse),
        )
    )


def _selection_key(prefix: str, details: list[PurchaseBatchItem]) -> str:
    raw = f"{prefix}:" + ",".join(str(detail.id) for detail in sorted(details, key=lambda item: item.id))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _create_job(
    session: Session,
    purchase_batch: PurchaseBatch,
    details: list[PurchaseBatchItem],
    export_type: str,
    warehouse_id: int,
    selection_key: str,
    parent_job_id: int | None = None,
    selected_batch_ids: list[int] | None = None,
) -> QinsiPurchaseExportJob:
    existing = session.scalar(select(QinsiPurchaseExportJob).where(QinsiPurchaseExportJob.selection_key == selection_key))
    if existing is not None:
        return existing
    warehouse = details[0].qinsi_target_warehouse
    if any(detail.qinsi_target_warehouse_id != warehouse_id for detail in details):
        raise ValueError("同一秦丝文件只能包含一个目标仓库")
    export_no = f"QE-{datetime.now(timezone.utc):%Y%m%d}-{uuid.uuid4().hex[:10].upper()}"
    type_code = "NEW" if export_type == "new_product" else "RESTOCK"
    selected_batch_ids = sorted(set(selected_batch_ids or [purchase_batch.id]))
    batch_label = purchase_batch.batch_no if len(selected_batch_ids) == 1 else f"MERGED-{len(selected_batch_ids)}-BATCHES"
    filename = f"qinsi_{type_code}_{batch_label}_{warehouse.internal_code}_{export_no}.xlsx"
    job = QinsiPurchaseExportJob(
        export_no=export_no,
        selection_key=selection_key,
        export_type=export_type,
        purchase_batch_id=purchase_batch.id,
        selected_batch_ids_json=json.dumps(selected_batch_ids),
        qinsi_target_warehouse_id=warehouse_id,
        parent_export_job_id=parent_job_id,
        filename=filename,
        file_content=b"",
        status="generated",
        line_count=len({detail.product.jan for detail in details}),
    )
    session.add(job)
    session.flush()
    row_by_jan = {
        jan: row_no for row_no, jan in enumerate(
            dict.fromkeys(detail.product.jan for detail in sorted(details, key=lambda item: item.id)), 2,
        )
    }
    for row_no, detail in enumerate(sorted(details, key=lambda item: item.id), 2):
        product = detail.product
        if export_type == "new_product":
            product.status = "pending_qinsi_product_import"
        code = product.jan or product.qinsi_product_code or product.internal_sku
        line = QinsiPurchaseExportLine(
            export_job_id=job.id,
            purchase_batch_id=detail.purchase_batch_id,
            purchase_batch_item_id=detail.id,
            receipt_id=detail.receipt_item.receipt_id,
            receipt_item_id=detail.receipt_item_id,
            product_id=product.id,
            qinsi_target_warehouse_id=detail.qinsi_target_warehouse_id,
            row_no=row_by_jan.get(product.jan, row_no),
            internal_sku=product.internal_sku,
            jan=product.jan,
            qinsi_product_code=code,
            product_name=_product_export_name(product) if export_type == "new_product" else format_product_display_name(product.name_cn, product.name_ja),
            quantity=detail.quantity,
            purchase_price=detail.unit_price if detail.unit_price is not None else product.purchase_price,
            status="generated",
        )
        if export_type == "restock":
            line.source = QinsiPurchaseExportLineSource(purchase_batch_item_id=detail.id, is_active=True)
        session.add(line)
    job.file_content = (
        _goods_template_bytes(list({detail.product.id: detail.product for detail in details}.values()))
        if export_type == "new_product"
        else _purchase_template_bytes(
            details, purchase_batch,
            note=(purchase_batch.batch_no if len(selected_batch_ids) == 1 else f"MERGED {len(selected_batch_ids)} BATCHES"),
        )
    )
    if export_type == "restock":
        purchase_batch.status = "pending_qinsi_submission"
    session.flush()
    return job


def generate_merged_purchase_batch_export(
    session: Session, purchase_batch_ids: set[int],
) -> QinsiPurchaseExportJob:
    batch_ids = sorted(purchase_batch_ids)
    if len(batch_ids) < 2:
        raise ValueError("请至少选择两个待入库采购批次")
    batches = [_loaded_purchase_batch(session, batch_id) for batch_id in batch_ids]
    if any(batch is None for batch in batches):
        raise LookupError("所选采购批次包含不存在的记录")
    selected = [batch for batch in batches if batch is not None]
    cancelled = [batch.batch_no for batch in selected if batch.status == "cancelled"]
    if cancelled:
        raise ValueError("已取消采购批次不能合并导出：" + "、".join(cancelled))
    blocking_products = list({
        detail.product.id: detail.product
        for batch in selected for detail in batch.items
        if detail.product.status != "qinsi_product_imported"
    }.values())
    if blocking_products:
        labels = "、".join(
            f"{product.jan or product.internal_sku}（{_product_export_name(product) if product.jan else product.internal_sku}）"
            for product in blocking_products
        )
        raise ValueError(f"存在未导入秦丝的新商品，采购Excel已阻塞：{labels}")
    historical_item_ids = set(session.scalars(select(QinsiPurchaseExportLineSource.purchase_batch_item_id)))
    details = [
        detail for batch in selected for detail in batch.items
        if detail.id not in historical_item_ids
    ]
    if not details:
        raise ValueError("所选采购批次没有待入库明细")
    missing_batches = [
        batch.batch_no for batch in selected
        if not any(detail.purchase_batch_id == batch.id for detail in details)
    ]
    if missing_batches:
        raise ValueError("以下批次没有待入库明细：" + "、".join(missing_batches))
    warehouse_ids = {detail.qinsi_target_warehouse_id for detail in details}
    if len(warehouse_ids) != 1:
        raise ValueError("所选批次包含不同秦丝目标仓库，不能安全合并为一份采购Excel")
    warehouse_id = next(iter(warehouse_ids))
    key = _selection_key("merged:" + ",".join(map(str, batch_ids)), details)
    job = _create_job(
        session, selected[0], details, "restock", warehouse_id, key,
        selected_batch_ids=batch_ids,
    )
    for batch in selected:
        batch.status = "pending_qinsi_submission"
    session.commit()
    loaded = get_qinsi_export_job(session, job.id)
    if loaded is None:
        raise LookupError("合并采购导出记录创建失败")
    return loaded


def generate_purchase_batch_exports(session: Session, purchase_batch_id: int) -> list[QinsiPurchaseExportJob]:
    purchase_batch = _loaded_purchase_batch(session, purchase_batch_id)
    if purchase_batch is None:
        raise LookupError("采购批次不存在")
    if purchase_batch.status == "cancelled":
        raise ValueError("已取消的采购批次不能生成秦丝文件")
    blocking_products = list({
        detail.product.id: detail.product for detail in purchase_batch.items
        if detail.product.status != "qinsi_product_imported"
    }.values())
    if blocking_products:
        labels = "、".join(
            f"{product.jan or product.internal_sku}（{_product_export_name(product) if product.jan else product.internal_sku}）"
            for product in blocking_products
        )
        raise ValueError(f"存在未导入秦丝的新商品，采购Excel已阻塞：{labels}")
    existing_initial = list(session.scalars(
        select(QinsiPurchaseExportJob).where(
            QinsiPurchaseExportJob.purchase_batch_id == purchase_batch.id,
            QinsiPurchaseExportJob.parent_export_job_id.is_(None),
            QinsiPurchaseExportJob.export_type == "restock",
        ).order_by(QinsiPurchaseExportJob.id)
    ))
    historical_item_ids = set(session.scalars(select(QinsiPurchaseExportLineSource.purchase_batch_item_id)))
    candidates = [detail for detail in purchase_batch.items if detail.id not in historical_item_ids]
    grouped: dict[int, list[PurchaseBatchItem]] = defaultdict(list)
    for detail in candidates:
        grouped[detail.qinsi_target_warehouse_id].append(detail)
    created: list[QinsiPurchaseExportJob] = []
    for warehouse_id, details in grouped.items():
        key = _selection_key(f"initial:{purchase_batch.id}:restock:{warehouse_id}", details)
        created.append(_create_job(session, purchase_batch, details, "restock", warehouse_id, key))
    session.commit()
    result = created or existing_initial
    return [get_qinsi_export_job(session, job.id) for job in result if job is not None]


def _refresh_purchase_batch_status(session: Session, purchase_batch_id: int) -> None:
    purchase_batch = session.get(PurchaseBatch, purchase_batch_id)
    if purchase_batch is None or purchase_batch.status == "cancelled":
        return
    total = session.scalar(select(func.count()).select_from(PurchaseBatchItem).where(PurchaseBatchItem.purchase_batch_id == purchase_batch_id)) or 0
    imported = session.scalar(
        select(func.count()).select_from(QinsiPurchaseExportLineSource)
        .join(QinsiPurchaseExportLine, QinsiPurchaseExportLine.id == QinsiPurchaseExportLineSource.export_line_id)
        .where(
            QinsiPurchaseExportLine.purchase_batch_id == purchase_batch_id,
            QinsiPurchaseExportLineSource.is_active.is_(True),
            QinsiPurchaseExportLine.status == "imported",
        )
    ) or 0
    purchase_batch.status = "confirmed" if total and imported == total else "pending_qinsi_submission"


def _refresh_receipt_batch_product_status(session: Session, purchase_batch_id: int) -> None:
    purchase_batch = _loaded_purchase_batch(session, purchase_batch_id)
    if purchase_batch is None:
        return
    gpt_batch = session.get(ReceiptBatch, purchase_batch.gpt_batch_id)
    if gpt_batch is None:
        return
    blocking = [
        item.product for item in purchase_batch.items
        if item.product and item.product.status in {
            "new_pending_completion", "new_pending_review", "pending_qinsi_product_import",
        }
    ]
    gpt_batch.product_status = "blocked_by_new_products" if blocking else "matched"


def confirm_qinsi_export(
    session: Session,
    job: QinsiPurchaseExportJob,
    confirmation: QinsiExportConfirmationInput,
) -> QinsiPurchaseExportJob:
    if job.status != "generated":
        if job.status in {"imported", "partially_failed", "failed", "cancelled"}:
            return job
        raise ValueError("当前导出记录不能确认")
    line_ids = {line.id for line in job.lines}
    if confirmation.result == "all_success":
        failed_ids: set[int] = set()
    elif confirmation.result == "all_failed":
        failed_ids = line_ids
    else:
        failed_ids = confirmation.failed_line_ids
        if not failed_ids < line_ids:
            raise ValueError("部分失败必须选择本导出中的部分明细，不能为空或全选")
    if not failed_ids.issubset(line_ids):
        raise ValueError("失败行包含不属于当前导出的明细")
    for line in job.lines:
        if line.id in failed_ids:
            line.status = "failed"
            line.failure_message = "秦丝导入失败，等待重试"
            if line.source:
                line.source.is_active = False
        else:
            line.status = "imported"
            line.failure_message = None
            if job.export_type == "new_product":
                line.product.product_origin = "qinsi"
                line.product.status = "qinsi_product_imported"
    job.status = "failed" if len(failed_ids) == len(line_ids) else ("partially_failed" if failed_ids else "imported")
    job.confirmed_at = datetime.now(timezone.utc)
    job.confirmed_by = confirmation.actor_name
    job.confirmation_note = confirmation.note
    for purchase_batch_id in {line.purchase_batch_id for line in job.lines}:
        _refresh_purchase_batch_status(session, purchase_batch_id)
        _refresh_receipt_batch_product_status(session, purchase_batch_id)
    session.commit()
    return get_qinsi_export_job(session, job.id)


def cancel_qinsi_export_confirmation(
    session: Session,
    job: QinsiPurchaseExportJob,
    *,
    actor_name: str,
) -> QinsiPurchaseExportJob:
    if job.status not in {"imported", "partially_failed", "failed"}:
        raise ValueError("只有已确认导入结果的导出记录可以撤销")
    for line in job.lines:
        if line.status == "imported" and job.export_type == "new_product":
            line.product.status = "pending_qinsi_product_import"
        line.status = "generated"
        line.failure_message = None
        if line.source:
            line.source.is_active = True
    job.status = "generated"
    job.cancelled_at = datetime.now(timezone.utc)
    job.cancelled_by = (actor_name or "管理员")[:128]
    job.confirmed_at = None
    job.confirmed_by = None
    for purchase_batch_id in {line.purchase_batch_id for line in job.lines}:
        _refresh_purchase_batch_status(session, purchase_batch_id)
        _refresh_receipt_batch_product_status(session, purchase_batch_id)
    session.commit()
    return get_qinsi_export_job(session, job.id)


def retry_failed_qinsi_lines(
    session: Session,
    job: QinsiPurchaseExportJob,
    failed_line_ids: set[int],
) -> QinsiPurchaseExportJob:
    if job.status not in {"partially_failed", "failed"}:
        raise ValueError("只有失败或部分失败的导出记录可以重试")
    failed_lines = {line.id: line for line in job.lines if line.status == "failed"}
    if not failed_line_ids or not failed_line_ids.issubset(failed_lines):
        raise ValueError("只能选择当前导出中的失败行重试")
    selected = [failed_lines[line_id] for line_id in sorted(failed_line_ids)]
    purchase_batch = _loaded_purchase_batch(session, job.purchase_batch_id)
    if purchase_batch is None:
        raise LookupError("采购批次不存在")
    selected_item_ids = {line.purchase_batch_item_id for line in selected}
    details = list(session.scalars(
        select(PurchaseBatchItem)
        .where(PurchaseBatchItem.id.in_(selected_item_ids))
        .options(
            selectinload(PurchaseBatchItem.product),
            selectinload(PurchaseBatchItem.receipt_item),
            selectinload(PurchaseBatchItem.qinsi_target_warehouse),
        )
        .order_by(PurchaseBatchItem.id)
    ))
    if len(details) != len(selected_item_ids):
        raise LookupError("失败行关联的采购明细不存在")
    key = _selection_key(f"retry:{job.id}", details)
    existing_retry = session.scalar(select(QinsiPurchaseExportJob).where(QinsiPurchaseExportJob.selection_key == key))
    if existing_retry is not None:
        return get_qinsi_export_job(session, existing_retry.id)
    active_item_ids = set(session.scalars(
        select(QinsiPurchaseExportLineSource.purchase_batch_item_id).where(QinsiPurchaseExportLineSource.is_active.is_(True))
    ))
    if any(line.purchase_batch_item_id in active_item_ids for line in selected):
        raise ValueError("所选失败行已有活动导出或成功提交，不能重复导出")
    retry_job = _create_job(
        session, purchase_batch, details, job.export_type, job.qinsi_target_warehouse_id, key,
        parent_job_id=job.id, selected_batch_ids=sorted({detail.purchase_batch_id for detail in details}),
    )
    session.commit()
    return get_qinsi_export_job(session, retry_job.id)


def purchase_item_export_states(session: Session, purchase_batch_id: int) -> dict[int, str]:
    lines = list(session.scalars(
        select(QinsiPurchaseExportLine)
        .where(QinsiPurchaseExportLine.purchase_batch_id == purchase_batch_id)
        .options(selectinload(QinsiPurchaseExportLine.source))
        .order_by(QinsiPurchaseExportLine.id)
    ))
    states: dict[int, str] = {}
    for line in lines:
        if line.source and line.source.is_active:
            states[line.purchase_batch_item_id] = "submitted" if line.status == "imported" else "awaiting_confirmation"
        elif line.status == "failed" and line.purchase_batch_item_id not in states:
            states[line.purchase_batch_item_id] = "failed_retry"
    return states

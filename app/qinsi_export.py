from __future__ import annotations

import hashlib
import io
import uuid
import zipfile
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from xml.etree import ElementTree as ET

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.config import PROJECT_ROOT
from app.models import (
    Product,
    PurchaseBatch,
    PurchaseBatchItem,
    QinsiPurchaseExportJob,
    QinsiPurchaseExportLine,
    QinsiPurchaseExportLineSource,
)
from app.qinsi_import import read_product_sheet
from app.schemas import QinsiExportConfirmationInput


TEMPLATE_PATH = PROJECT_ROOT / "reference" / "qinsi" / "goodsImportTemplate秦丝导入模版.xlsx"
QINSI_TEMPLATE_HEADERS = (
    "名称（必填）", "货号（必填且唯一）", "条码", "型号规格", "品牌", "分类", "单位",
    "采购价", "销售价", "最低销售价", "排序", "状态", "启用积分", "库存预警下限",
    "库存预警上限", "保质期（天）", "启用批次", "过期预警（天）", "商品图片链接",
    "商品备注", "产地", "适用年龄", "商品重量（KG）", "启用序列号", "库位",
    "盘点库存数量", "当前库存（导入时不需要录入）", "盘点仓库:", "新日本仓库",
)
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


def _set_cell(row: ET.Element, row_no: int, column_no: int, value: str | int | None) -> None:
    cell = _cell(row, row_no, column_no)
    for child in list(cell):
        cell.remove(child)
    cell.attrib.pop("t", None)
    if value is None or value == "":
        return
    if isinstance(value, int):
        ET.SubElement(cell, f"{{{SHEET_NS}}}v").text = str(value)
        return
    cell.attrib["t"] = "inlineStr"
    inline = ET.SubElement(cell, f"{{{SHEET_NS}}}is")
    ET.SubElement(inline, f"{{{SHEET_NS}}}t").text = str(value)


def _template_bytes(rows: list[ExportRow], warehouse_name: str) -> bytes:
    if not rows:
        raise ValueError("没有可导出的采购明细")
    source = TEMPLATE_PATH.read_bytes()
    parsed = read_product_sheet(source)
    headers = tuple(parsed[0][1]) if parsed else ()
    if headers != QINSI_TEMPLATE_HEADERS:
        raise ValueError("秦丝模板列名已变化，已停止生成以避免不兼容文件")
    if len(rows) > len(parsed):
        raise ValueError(f"单个秦丝文件最多支持 {len(parsed)} 条明细")

    input_buffer = io.BytesIO(source)
    output_buffer = io.BytesIO()
    with zipfile.ZipFile(input_buffer, "r") as source_zip, zipfile.ZipFile(output_buffer, "w") as output_zip:
        target_path = _sheet_path(source_zip, "商品导入")
        sheet = ET.fromstring(source_zip.read(target_path))
        sheet_data = sheet.find(f"{{{SHEET_NS}}}sheetData")
        if sheet_data is None:
            raise ValueError("秦丝模板商品导入表结构损坏")
        row_nodes = {int(row.attrib["r"]): row for row in sheet_data.findall(f"{{{SHEET_NS}}}row")}
        header = row_nodes.get(1)
        if header is None:
            raise ValueError("秦丝模板缺少表头")
        _set_cell(header, 1, 29, warehouse_name)
        for row_no, export_row in enumerate(rows, 2):
            row = row_nodes.get(row_no)
            if row is None:
                row = ET.SubElement(sheet_data, f"{{{SHEET_NS}}}row", {"r": str(row_no)})
            detail, product = export_row.purchase_batch_item, export_row.product
            values: dict[int, str | int | None] = {
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
            }
            for column_no, value in values.items():
                _set_cell(row, row_no, column_no, value)
        ET.register_namespace("", SHEET_NS)
        ET.register_namespace("r", OFFICE_REL_NS)
        rewritten = ET.tostring(sheet, encoding="utf-8", xml_declaration=True)
        for info in source_zip.infolist():
            output_zip.writestr(info, rewritten if info.filename == target_path else source_zip.read(info.filename))
    return output_buffer.getvalue()


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


def _loaded_purchase_batch(session: Session, purchase_batch_id: int) -> PurchaseBatch | None:
    return session.scalar(
        select(PurchaseBatch).where(PurchaseBatch.id == purchase_batch_id).options(
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
) -> QinsiPurchaseExportJob:
    existing = session.scalar(select(QinsiPurchaseExportJob).where(QinsiPurchaseExportJob.selection_key == selection_key))
    if existing is not None:
        return existing
    warehouse = details[0].qinsi_target_warehouse
    if any(detail.qinsi_target_warehouse_id != warehouse_id for detail in details):
        raise ValueError("同一秦丝文件只能包含一个目标仓库")
    export_no = f"QE-{datetime.now(timezone.utc):%Y%m%d}-{uuid.uuid4().hex[:10].upper()}"
    type_code = "NEW" if export_type == "new_product" else "RESTOCK"
    filename = f"qinsi_{type_code}_{purchase_batch.batch_no}_{warehouse.internal_code}_{export_no}.xlsx"
    job = QinsiPurchaseExportJob(
        export_no=export_no,
        selection_key=selection_key,
        export_type=export_type,
        purchase_batch_id=purchase_batch.id,
        qinsi_target_warehouse_id=warehouse_id,
        parent_export_job_id=parent_job_id,
        filename=filename,
        file_content=b"",
        status="generated",
        line_count=len(details),
    )
    session.add(job)
    session.flush()
    export_rows: list[ExportRow] = []
    for row_no, detail in enumerate(sorted(details, key=lambda item: item.id), 2):
        product = detail.product
        code = product.qinsi_product_code or product.internal_sku
        line = QinsiPurchaseExportLine(
            export_job_id=job.id,
            purchase_batch_id=purchase_batch.id,
            purchase_batch_item_id=detail.id,
            receipt_id=purchase_batch.receipt_id,
            receipt_item_id=detail.receipt_item_id,
            product_id=product.id,
            qinsi_target_warehouse_id=detail.qinsi_target_warehouse_id,
            row_no=row_no,
            internal_sku=product.internal_sku,
            jan=product.jan,
            qinsi_product_code=code,
            product_name=product.name_cn or product.name_ja or product.internal_sku,
            quantity=detail.quantity,
            purchase_price=detail.unit_price if detail.unit_price is not None else product.purchase_price,
            status="generated",
        )
        line.source = QinsiPurchaseExportLineSource(purchase_batch_item_id=detail.id, is_active=True)
        session.add(line)
        export_rows.append(ExportRow(detail, product, code))
    job.file_content = _template_bytes(export_rows, warehouse.display_name)
    purchase_batch.status = "pending_qinsi_submission"
    session.flush()
    return job


def generate_purchase_batch_exports(session: Session, purchase_batch_id: int) -> list[QinsiPurchaseExportJob]:
    purchase_batch = _loaded_purchase_batch(session, purchase_batch_id)
    if purchase_batch is None:
        raise LookupError("采购批次不存在")
    if purchase_batch.status == "cancelled":
        raise ValueError("已取消的采购批次不能生成秦丝文件")
    existing_initial = list(session.scalars(
        select(QinsiPurchaseExportJob).where(
            QinsiPurchaseExportJob.purchase_batch_id == purchase_batch.id,
            QinsiPurchaseExportJob.parent_export_job_id.is_(None),
        ).order_by(QinsiPurchaseExportJob.id)
    ))
    historical_item_ids = set(session.scalars(select(QinsiPurchaseExportLineSource.purchase_batch_item_id)))
    candidates = [detail for detail in purchase_batch.items if detail.id not in historical_item_ids]
    grouped: dict[tuple[str, int], list[PurchaseBatchItem]] = defaultdict(list)
    for detail in candidates:
        export_type = "restock" if detail.product.product_origin == "qinsi" else "new_product"
        grouped[(export_type, detail.qinsi_target_warehouse_id)].append(detail)
    created: list[QinsiPurchaseExportJob] = []
    for (export_type, warehouse_id), details in grouped.items():
        key = _selection_key(f"initial:{purchase_batch.id}:{export_type}:{warehouse_id}", details)
        created.append(_create_job(session, purchase_batch, details, export_type, warehouse_id, key))
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
            line.source.is_active = False
        else:
            line.status = "imported"
            line.failure_message = None
            if job.export_type == "new_product":
                if line.product.qinsi_product_code is None:
                    line.product.qinsi_product_code = line.qinsi_product_code
                line.product.product_origin = "qinsi"
    job.status = "failed" if len(failed_ids) == len(line_ids) else ("partially_failed" if failed_ids else "imported")
    job.confirmed_at = datetime.now(timezone.utc)
    _refresh_purchase_batch_status(session, job.purchase_batch_id)
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
    details_by_id = {detail.id: detail for detail in purchase_batch.items}
    details = [details_by_id[line.purchase_batch_item_id] for line in selected]
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
        session, purchase_batch, details, job.export_type, job.qinsi_target_warehouse_id, key, parent_job_id=job.id,
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
        if line.source.is_active:
            states[line.purchase_batch_item_id] = "submitted" if line.status == "imported" else "awaiting_confirmation"
        elif line.status == "failed" and line.purchase_batch_item_id not in states:
            states[line.purchase_batch_item_id] = "failed_retry"
    return states

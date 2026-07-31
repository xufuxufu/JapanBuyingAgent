from __future__ import annotations

import io
import json
import re
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import PurePosixPath
from xml.etree import ElementTree as ET

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ImportJob, ImportRow, Product


NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main", "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships"}
REL_NS = {"p": "http://schemas.openxmlformats.org/package/2006/relationships"}
MAX_XLSX_BYTES = 20 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 80 * 1024 * 1024

HEADER_MAP = {
    "条码": "jan",
    "货号（必填且唯一）": "qinsi_product_code",
    "货号(必填且唯一)": "qinsi_product_code",
    "名称（必填）": "name_cn",
    "名称(必填)": "name_cn",
    "商品规格": "specification",
    "型号规格": "model_spec",
    "采购价": "purchase_price",
    "销售价": "sale_price",
    "最低销售价": "minimum_sale_price",
    "商品图片链接": "image_url",
    "库位": "location_code",
    "状态": "status",
}
PRODUCT_FIELDS = tuple(dict.fromkeys(HEADER_MAP.values()))


@dataclass(frozen=True)
class ParsedCell:
    text: str
    raw_type: str | None


def _xml(archive: zipfile.ZipFile, name: str) -> ET.Element:
    try:
        return ET.fromstring(archive.read(name))
    except KeyError as exc:
        raise ValueError(f"Excel结构缺少 {name}") from exc
    except ET.ParseError as exc:
        raise ValueError(f"Excel结构损坏：{name}") from exc


def _shared_strings(archive: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in archive.namelist():
        return []
    root = _xml(archive, "xl/sharedStrings.xml")
    return ["".join(node.text or "" for node in item.findall(".//m:t", NS)) for item in root.findall("m:si", NS)]


def _style_formats(archive: zipfile.ZipFile) -> dict[int, str]:
    if "xl/styles.xml" not in archive.namelist():
        return {}
    root = _xml(archive, "xl/styles.xml")
    custom = {int(node.attrib["numFmtId"]): node.attrib.get("formatCode", "") for node in root.findall("m:numFmts/m:numFmt", NS)}
    result: dict[int, str] = {}
    for index, xf in enumerate(root.findall("m:cellXfs/m:xf", NS)):
        result[index] = custom.get(int(xf.attrib.get("numFmtId", "0")), "")
    return result


def _cell_reference_column(reference: str) -> int:
    letters = re.match(r"[A-Z]+", reference.upper())
    if not letters:
        return 0
    value = 0
    for char in letters.group(0):
        value = value * 26 + ord(char) - 64
    return value - 1


def _numeric_text(raw: str, format_code: str) -> str:
    raw = raw.strip()
    if re.search(r"[Ee]", raw):
        return raw
    try:
        number = Decimal(raw)
    except InvalidOperation:
        return raw
    text = str(int(number)) if number == number.to_integral_value() else format(number, "f")
    zero_pattern = re.sub(r'"[^"]*"|\\.', "", format_code).split(";", 1)[0]
    if re.fullmatch(r"0+", zero_pattern) and number == number.to_integral_value():
        text = text.zfill(len(zero_pattern))
    return text


def _cell_value(cell: ET.Element, shared: list[str], formats: dict[int, str]) -> ParsedCell:
    cell_type = cell.attrib.get("t")
    if cell_type == "inlineStr":
        return ParsedCell("".join(node.text or "" for node in cell.findall(".//m:t", NS)), cell_type)
    value_node = cell.find("m:v", NS)
    raw = value_node.text if value_node is not None and value_node.text is not None else ""
    if cell_type == "s":
        try:
            resolved = shared[int(raw)]
            return ParsedCell(resolved, cell_type)
        except (ValueError, IndexError) as exc:
            raise ValueError("Excel共享字符串索引损坏") from exc
    if cell_type in {"str", "e"}:
        return ParsedCell(raw, cell_type)
    style = formats.get(int(cell.attrib.get("s", "0")), "")
    return ParsedCell(_numeric_text(raw, style), cell_type)


def read_product_sheet(content: bytes) -> list[tuple[int, dict[str, str]]]:
    if not content or len(content) > MAX_XLSX_BYTES:
        raise ValueError("Excel文件为空或超过20MB")
    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile as exc:
        raise ValueError("文件不是有效的 .xlsx 工作簿") from exc
    with archive:
        if len(archive.infolist()) > 1000 or sum(item.file_size for item in archive.infolist()) > MAX_UNCOMPRESSED_BYTES:
            raise ValueError("Excel解压内容过大")
        workbook = _xml(archive, "xl/workbook.xml")
        relationships = _xml(archive, "xl/_rels/workbook.xml.rels")
        targets = {node.attrib["Id"]: node.attrib["Target"] for node in relationships.findall("p:Relationship", REL_NS)}
        sheet_node = next((node for node in workbook.findall("m:sheets/m:sheet", NS) if node.attrib.get("name") == "商品导入"), None)
        if sheet_node is None:
            raise ValueError("Excel缺少“商品导入”工作表")
        rel_id = sheet_node.attrib.get(f"{{{NS['r']}}}id")
        target = targets.get(rel_id or "")
        if not target:
            raise ValueError("商品导入工作表关系损坏")
        sheet_path = str(PurePosixPath("xl") / target.lstrip("/")) if not target.startswith("xl/") else target
        root = _xml(archive, sheet_path)
        shared, formats = _shared_strings(archive), _style_formats(archive)
        rows: list[tuple[int, list[ParsedCell]]] = []
        for row in root.findall("m:sheetData/m:row", NS):
            values: dict[int, ParsedCell] = {}
            for cell in row.findall("m:c", NS):
                values[_cell_reference_column(cell.attrib.get("r", "A1"))] = _cell_value(cell, shared, formats)
            width = max(values, default=-1) + 1
            rows.append((int(row.attrib.get("r", len(rows) + 1)), [values.get(index, ParsedCell("", None)) for index in range(width)]))
        if not rows:
            raise ValueError("商品导入工作表为空")
        headers = [cell.text.strip() for cell in rows[0][1]]
        if "条码" not in headers or not any(header in headers for header in ("货号（必填且唯一）", "货号(必填且唯一)")):
            raise ValueError("Excel缺少条码或货号列")
        result = []
        for row_no, cells in rows[1:]:
            raw = {header: (cells[index].text.strip() if index < len(cells) else "") for index, header in enumerate(headers) if header}
            result.append((row_no, raw))
        return result


def _identifier(value: str, label: str) -> str | None:
    value = value.strip()
    if not value:
        return None
    if re.search(r"[Ee][+-]?\d+", value):
        raise ValueError(f"{label}使用了科学计数法，无法保证前导零")
    if value.endswith(".0") and value[:-2].isdigit():
        value = value[:-2]
    return value


def _money(value: str, label: str) -> Decimal | None:
    value = value.strip()
    if not value:
        return None
    try:
        number = Decimal(value.replace(",", ""))
    except InvalidOperation as exc:
        raise ValueError(f"{label}不是有效数值") from exc
    rounded = number.quantize(Decimal("0.01"))
    if rounded != number:
        raise ValueError(f"{label}最多支持2位小数")
    return rounded


def _mapped_row(raw: dict[str, str]) -> tuple[dict, list[str], list[str]]:
    mapped: dict = {}
    warnings: list[str] = []
    errors: list[str] = []
    for header, field in HEADER_MAP.items():
        if header in raw:
            value = raw[header].strip()
            if field in {"jan", "qinsi_product_code"}:
                try:
                    mapped[field] = _identifier(value, "条码" if field == "jan" else "货号")
                except ValueError as exc:
                    errors.append(str(exc))
            elif field in {"purchase_price", "sale_price", "minimum_sale_price"}:
                try:
                    mapped[field] = _money(value, header)
                except ValueError as exc:
                    errors.append(str(exc))
            elif field == "status":
                if value in {"", "启用"}:
                    mapped[field] = "active"
                elif value == "停用":
                    mapped[field] = "inactive"
                else:
                    errors.append(f"状态值无效：{value}")
            else:
                mapped[field] = value or None
    return {field: mapped.get(field) for field in PRODUCT_FIELDS}, warnings, errors


def _existing_target(session: Session, mapped: dict) -> tuple[Product | None, str | None]:
    jan, code = mapped.get("jan"), mapped.get("qinsi_product_code")
    by_jan = session.scalar(select(Product).where(Product.jan == jan)) if jan else None
    by_code = session.scalar(select(Product).where(Product.qinsi_product_code == code)) if code else None
    if by_jan and by_code and by_jan.id != by_code.id:
        return None, "条码与货号分别指向不同商品"
    target = by_jan or by_code
    if target and jan and target.jan and target.jan != jan:
        return None, "货号命中商品，但条码与现有条码冲突"
    if target and code and target.qinsi_product_code and target.qinsi_product_code != code:
        return None, "条码命中商品，但货号与现有货号冲突"
    return target, None


def create_import_preview(session: Session, filename: str, content: bytes) -> ImportJob:
    rows = read_product_sheet(content)
    job = ImportJob(job_type="qinsi_products", status="previewed", original_filename=filename, total_rows=len(rows))
    session.add(job)
    session.flush()
    prepared: list[tuple[ImportRow, dict]] = []
    jan_rows: dict[str, list[ImportRow]] = {}
    code_rows: dict[str, list[ImportRow]] = {}
    for row_no, raw in rows:
        mapped, warnings, errors = _mapped_row(raw)
        empty_product = not any(mapped.get(key) for key in ("jan", "qinsi_product_code", "name_cn"))
        if empty_product:
            status, error = "skipped", "空白模板行"
        elif not mapped.get("name_cn"):
            status, error = "error", "名称（必填）为空"
        elif errors:
            status, error = "error", "；".join(errors)
        else:
            _, conflict = _existing_target(session, mapped)
            status, error = ("conflict", conflict) if conflict else (("warning", None) if warnings else ("ready", None))
        row = ImportRow(
            import_job_id=job.id, row_no=row_no, raw_json=json.dumps(raw, ensure_ascii=False),
            parsed_json=json.dumps(mapped, ensure_ascii=False), warnings_json=json.dumps(warnings, ensure_ascii=False) if warnings else None,
            status=status, error_message=error,
        )
        session.add(row)
        prepared.append((row, mapped))
        if mapped.get("jan"):
            jan_rows.setdefault(mapped["jan"], []).append(row)
        if mapped.get("qinsi_product_code"):
            code_rows.setdefault(mapped["qinsi_product_code"], []).append(row)
    for label, groups in (("条码", jan_rows), ("货号", code_rows)):
        for value, duplicates in groups.items():
            if len(duplicates) > 1:
                for row in duplicates:
                    row.status = "conflict"
                    row.error_message = f"Excel内重复非空{label}：{value}"
    job.skipped_count = sum(row.status == "skipped" for row, _ in prepared)
    job.conflict_count = sum(row.status == "conflict" for row, _ in prepared)
    job.error_count = sum(row.status == "error" for row, _ in prepared)
    job.warning_count = sum(bool(row.warnings_json) for row, _ in prepared)
    job.summary_json = json.dumps({"sheet": "商品导入", "mapping": "header"}, ensure_ascii=False)
    session.commit()
    session.refresh(job)
    return job


def confirm_import(session: Session, job: ImportJob) -> ImportJob:
    if job.job_type != "qinsi_products" or job.status != "previewed":
        raise ValueError("导入任务状态不允许确认")
    rows = list(session.scalars(select(ImportRow).where(ImportRow.import_job_id == job.id).order_by(ImportRow.row_no)))
    success = skipped = conflicts = errors = 0
    try:
        for row in rows:
            if row.status == "skipped":
                skipped += 1
                continue
            if row.status == "conflict":
                conflicts += 1
                continue
            if row.status == "error":
                errors += 1
                continue
            mapped = json.loads(row.parsed_json or "{}")
            target, conflict = _existing_target(session, mapped)
            if conflict:
                row.status, row.error_message = "conflict", conflict
                conflicts += 1
                continue
            if target is None:
                target = Product(product_origin="qinsi", source="qinsi_import")
                session.add(target)
                changed = True
            else:
                changed = False
            for field in PRODUCT_FIELDS:
                value = mapped.get(field)
                if value is not None and value != "" and getattr(target, field) != value:
                    setattr(target, field, value)
                    changed = True
            session.flush()
            row.product_id = target.id
            if changed:
                row.status = "imported"
                success += 1
            else:
                row.status = "skipped"
                row.error_message = "与现有商品一致，无需更新"
                skipped += 1
        job.status = "completed" if not errors and not conflicts else "completed_with_issues"
        job.success_count, job.skipped_count = success, skipped
        job.conflict_count, job.error_count = conflicts, errors
        job.confirmed_at = datetime.now(timezone.utc)
        session.commit()
    except Exception:
        session.rollback()
        raise
    session.refresh(job)
    return job


# Public QinSi goods-import APIs use the maintained openpyxl implementation.
# The legacy XML helpers above remain temporarily for migration compatibility only.
from app.qinsi_goods_import import (  # noqa: E402,F401
    confirm_import,
    create_import_preview,
    parse_qinsi_workbook,
    read_product_sheet,
)

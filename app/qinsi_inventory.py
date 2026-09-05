from __future__ import annotations

import hashlib
import io
import json
import os
import re
import uuid
import zipfile
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.models import (
    Location,
    Product,
    ProductWatchConfig,
    PurchaseBatch,
    PurchaseBatchItem,
    QinsiInventorySnapshot,
    QinsiInventorySnapshotLine,
    QinsiProductMapping,
    QinsiPurchaseExportLine,
    QinsiPurchaseExportLineSource,
)
from app.qinsi_goods_import import parse_qinsi_workbook, read_product_sheet


BASE_HEADERS = {
    "名称（必填）", "名称(必填)", "商品规格", "货号（必填且唯一）", "货号(必填且唯一)",
    "条码", "型号规格", "品牌", "分类", "单位", "采购价", "销售价", "最低销售价", "排序",
    "状态", "启用积分", "库存预警下限", "库存预警上限", "保质期（天）", "启用批次",
    "过期预警（天）", "商品图片链接", "商品备注", "产地", "适用年龄", "商品重量（KG）",
    "启用序列号", "库位", "盘点库存数量", "当前库存（导入时不需要录入）", "盘点仓库:",
    "内部SKU", "internal_sku",
}
INVENTORY_STATUS_LABELS = {
    "no_snapshot": "无快照",
    "snapshot_stale": "快照已过期",
    "stock_available": "库存充足",
    "stock_low": "库存偏低",
    "out_of_stock": "快照数量为零",
    "incoming_or_pending": "有待提交或待确认采购",
    "review_needed": "需要人工复核",
}
MATCH_STATUS_LABELS = {"matched": "已匹配", "unmatched": "未匹配", "conflict": "冲突", "ignored": "已忽略"}
MATCH_METHOD_LABELS = {
    "code_jan_verified": "货号+JAN双重确认",
    "qinsi_product_code": "秦丝货号",
    "jan": "JAN",
    "confirmed_mapping": "已确认映射",
    "internal_sku": "内部SKU",
    "manual": "人工选择",
    "duplicate": "系统去重（完全重复行）",
    "quantity_conflict": "同货号同仓库数量冲突",
}

INVENTORY_REGION_CHINA = "china"
INVENTORY_REGION_JAPAN = "japan"

# Which physical region a QinSi/JBA warehouse sits in, for splitting reference
# stock into China vs Japan (procurement demand center, Phase 2B). Location has
# no country/region column -- adding one was considered and deliberately
# deferred (see AI_HANDOFF Phase 2B notes) -- so this stays a small, explicitly
# reviewed table keyed by the location's stable internal_code. It is never
# inferred from display_name: QinSi imports can spell the same real warehouse
# differently across imports (the seeded "2025招财猫" and the later
# auto-created "招财猫店" -- see qinsi_goods_import._warehouse_code -- are the
# same China warehouse under two different raw names/codes). A location not
# listed here contributes to neither region (unclassified, not guessed); when
# a genuinely new warehouse name shows up, add one line here.
INVENTORY_REGION_BY_LOCATION_CODE: dict[str, str] = {
    "QW-2025-QIANYU": INVENTORY_REGION_CHINA,
    "QW-2025-ZHAOCAIMAO": INVENTORY_REGION_CHINA,
    "QW-QINSI-1DE1039D20FB": INVENTORY_REGION_CHINA,  # "招财猫店", same warehouse as QW-2025-ZHAOCAIMAO under a later raw name
    "QW-2026-QIANYU": INVENTORY_REGION_CHINA,  # "2026千羽", QinSi renamed "千羽" for the new calendar year
    "QW-2026-ZHAOCAIMAO": INVENTORY_REGION_CHINA,  # "2026招财猫", same for "招财猫"
    "QW-NO-BARCODE": INVENTORY_REGION_CHINA,  # "无条码商品" -- a real China warehouse despite the name; must count
    "QW-NEW-JAPAN": INVENTORY_REGION_JAPAN,
    "LOC-JP-HOME": INVENTORY_REGION_JAPAN,
}


def inventory_region_for_location(location: Location | None) -> str | None:
    if location is None:
        return None
    return INVENTORY_REGION_BY_LOCATION_CODE.get(location.internal_code)


@dataclass(frozen=True, slots=True)
class InventorySettings:
    stale_hours: int
    default_low_stock_threshold: int
    max_upload_bytes: int
    allowed_extensions: tuple[str, ...]
    reuse_duplicate_file: bool
    purchase_assistance_enabled: bool


@dataclass(frozen=True, slots=True)
class WarehouseStock:
    warehouse: Location
    quantity: int


@dataclass(frozen=True, slots=True)
class ProductInventoryView:
    snapshot: QinsiInventorySnapshot | None
    warehouses: tuple[WarehouseStock, ...]
    total_quantity: int | None
    data_time: datetime | None
    is_stale: bool
    stale_hours: int
    review_needed: bool = False


@dataclass(frozen=True, slots=True)
class PurchaseAssistance:
    status: str
    base_status: str
    message: str
    low_stock_threshold: int
    pending_quantity: int
    awaiting_confirmation_quantity: int
    has_incoming_or_pending: bool
    target_reached: bool


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        return default
    return min(maximum, max(minimum, value))


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().casefold() in {"1", "true", "yes", "on"}


def inventory_settings() -> InventorySettings:
    extensions = tuple(
        item if item.startswith(".") else f".{item}"
        for item in (part.strip().lower() for part in os.getenv("JBA_QINSI_SNAPSHOT_EXTENSIONS", ".xlsx").split(","))
        if item
    ) or (".xlsx",)
    return InventorySettings(
        stale_hours=_env_int("JBA_QINSI_SNAPSHOT_STALE_HOURS", 72, 1, 8760),
        default_low_stock_threshold=_env_int("JBA_QINSI_DEFAULT_LOW_STOCK_THRESHOLD", 3, 1, 1000000),
        max_upload_bytes=_env_int("JBA_QINSI_SNAPSHOT_MAX_UPLOAD_MB", 20, 1, 20) * 1024 * 1024,
        allowed_extensions=extensions,
        reuse_duplicate_file=_env_bool("JBA_QINSI_REUSE_DUPLICATE_FILE", True),
        purchase_assistance_enabled=_env_bool("JBA_PURCHASE_ASSISTANCE_ENABLED", True),
    )


def _text(value: str | None) -> str | None:
    value = (value or "").strip()
    if not value:
        return None
    if re.search(r"[Ee][+-]?\d+", value):
        return value
    return value[:-2] if value.endswith(".0") and value[:-2].isdigit() else value


def _quantity(value: str | None) -> int | None:
    text = (value or "").strip().replace(",", "")
    if not text:
        return None
    try:
        number = Decimal(text)
    except InvalidOperation as exc:
        raise ValueError("账面数量不是整数") from exc
    if not number.is_finite() or number != number.to_integral_value():
        raise ValueError("账面数量不是整数")
    return int(number)


def _product_fields(raw: dict[str, str | None]) -> tuple[str | None, str | None, str | None, str | None]:
    name = _text(raw.get("名称（必填）") or raw.get("名称(必填)"))
    code = _text(raw.get("货号（必填且唯一）") or raw.get("货号(必填且唯一)"))
    jan = _text(raw.get("条码"))
    internal_sku = _text(raw.get("内部SKU") or raw.get("internal_sku"))
    return name, code, jan, internal_sku


def _inventory_entries(raw: dict[str, str | None]) -> list[tuple[str | None, str | None]]:
    explicit_warehouse = _text(raw.get("盘点仓库:"))
    counted = raw.get("盘点库存数量") or ""
    current = raw.get("当前库存（导入时不需要录入）") or ""
    if explicit_warehouse:
        return [(explicit_warehouse, counted or current)]
    dynamic = [(header.strip(), value) for header, value in raw.items() if header not in BASE_HEADERS and header.strip()]
    populated = [(header, value) for header, value in dynamic if (value or "").strip()]
    if populated:
        return populated
    if len(dynamic) == 1 and (counted or current or "").strip():
        return [(dynamic[0][0], counted or current)]
    return [(dynamic[0][0] if len(dynamic) == 1 else None, counted or current)]


def _match_product(
    session: Session, *, code: str | None, jan: str | None, internal_sku: str | None,
) -> tuple[Product | None, str | None, str, tuple[Product, Product] | None]:
    """Cross-validated code/JAN matching.

    code (qinsi_product_code) and jan are independent identity keys -- a row
    naming both is only a genuine product-identity conflict when they each
    resolve to a *different* existing Product. Either one hitting alone is a
    normal match; neither hitting falls back to confirmed_mapping/internal_sku.
    A conflict never auto-binds product_id; the caller must keep raw values
    and route it to human review.
    """
    by_code = session.scalar(select(Product).where(Product.qinsi_product_code == code)) if code else None
    by_jan = session.scalar(select(Product).where(Product.jan == jan)) if jan else None
    if by_code is not None and by_jan is not None:
        if by_code.id == by_jan.id:
            return by_code, "code_jan_verified", "matched", None
        return None, None, "conflict", (by_code, by_jan)
    if by_code is not None:
        return by_code, "qinsi_product_code", "matched", None
    if by_jan is not None:
        return by_jan, "jan", "matched", None
    if code:
        mapping = session.scalar(
            select(QinsiProductMapping)
            .where(QinsiProductMapping.qinsi_product_code == code)
            .options(selectinload(QinsiProductMapping.product))
        )
        if mapping is not None:
            return mapping.product, "confirmed_mapping", "matched", None
    if internal_sku:
        product = session.scalar(select(Product).where(Product.internal_sku == internal_sku))
        if product is not None:
            return product, "internal_sku", "matched", None
    return None, None, "unmatched", None


def _warehouse(session: Session, name: str | None) -> Location | None:
    if not name:
        return None
    return session.scalar(select(Location).where(
        Location.display_name == name,
        Location.is_qinsi_warehouse.is_(True),
        Location.is_active.is_(True),
    ))


def _refresh_snapshot_summary(snapshot: QinsiInventorySnapshot) -> None:
    lines = list(snapshot.lines)
    snapshot.total_rows = len(lines)
    snapshot.success_rows = sum(
        line.matching_status == "matched" and line.warehouse_id is not None and line.quantity is not None
        for line in lines
    )
    snapshot.unmatched_rows = sum(line.matching_status == "unmatched" for line in lines)
    snapshot.exception_rows = sum(
        line.matching_status == "conflict" or line.warehouse_status != "matched" or line.quantity is None
        for line in lines if line.matching_status != "ignored"
    )
    errors = list(dict.fromkeys(line.error_message for line in lines if line.error_message))
    snapshot.error_summary = "；".join(errors)[:1000] or None
    snapshot.status = "completed_with_issues" if snapshot.unmatched_rows or snapshot.exception_rows else "completed"


@dataclass(frozen=True, slots=True)
class _RowResult:
    global_row_no: int
    source_file: str
    source_row_no: int
    name: str | None
    code: str | None
    jan: str | None
    internal_sku: str | None
    warehouse_name: str | None
    quantity: int | None
    raw: dict
    product: Product | None
    matching_method: str | None
    matching_status: str
    conflict_pair: tuple[Product, Product] | None
    warehouse: Location | None
    error_notes: tuple[str, ...]


def _compute_row_results(
    session: Session, source_rows: list[tuple[str, int, dict]],
) -> list[_RowResult]:
    """Match + duplicate/quantity-conflict detection shared by preview and confirm.

    Runs read-only queries only (Product/QinsiProductMapping/Location lookups)
    -- safe to call before any snapshot row exists, which is what the
    multi-file preview step relies on.
    """
    expanded: list[dict] = []
    global_no = 0
    for source_file, source_row_no, raw in source_rows:
        name, code, jan, internal_sku = _product_fields(raw)
        if not any((name, code, jan, internal_sku)):
            continue
        product, method, status, conflict_pair = _match_product(
            session, code=code, jan=jan, internal_sku=internal_sku,
        )
        for warehouse_name, raw_quantity in _inventory_entries(raw):
            global_no += 1
            try:
                quantity = _quantity(raw_quantity)
                qty_error: str | None = None
            except ValueError as exc:
                quantity = None
                qty_error = str(exc)
            expanded.append({
                "global_row_no": global_no, "source_file": source_file, "source_row_no": source_row_no,
                "name": name, "code": code, "jan": jan, "internal_sku": internal_sku,
                "warehouse_name": warehouse_name, "quantity": quantity, "qty_error": qty_error, "raw": raw,
                "product": product, "method": method, "status": status, "conflict_pair": conflict_pair,
            })

    # Type A: fully identical rows (same identity + warehouse + quantity) --
    # only the first occurrence counts; later copies are excluded from
    # aggregation so inventory is never double counted.
    first_seen: dict[tuple, int] = {}
    for idx, item in enumerate(expanded):
        key = (item["name"], item["code"], item["jan"], item["warehouse_name"], item["quantity"])
        item["duplicate_of"] = first_seen.get(key)
        if item["duplicate_of"] is None:
            first_seen[key] = idx

    # Type B: same product code + warehouse but disagreeing quantity -- never
    # silently summed/maxed/last-wins; all rows in the group are excluded
    # from aggregation until a human resolves which value is correct.
    qty_groups: dict[tuple, set] = defaultdict(set)
    qty_group_rows: dict[tuple, list[int]] = defaultdict(list)
    for idx, item in enumerate(expanded):
        if item["duplicate_of"] is not None:
            continue
        if item["code"] and item["warehouse_name"]:
            key = (item["code"], item["warehouse_name"])
            qty_groups[key].add(item["quantity"])
            qty_group_rows[key].append(idx)
    conflicted_qty_keys = {key for key, values in qty_groups.items() if len(values) > 1}

    results: list[_RowResult] = []
    for idx, item in enumerate(expanded):
        errors: list[str] = []
        if item["qty_error"]:
            errors.append(item["qty_error"])
        if item["quantity"] is None:
            errors.append("账面数量为空")

        status = item["status"]
        method = item["method"]
        product = item["product"]
        conflict_pair = item["conflict_pair"]

        if item["duplicate_of"] is not None:
            status, method, product = "ignored", "duplicate", None
            original_row = expanded[item["duplicate_of"]]["global_row_no"]
            errors.append(f"完全重复行（与第{original_row}行完全一致），已去重只计一次，本行不计入库存合计")
        else:
            key = (item["code"], item["warehouse_name"]) if item["code"] and item["warehouse_name"] else None
            if key is not None and key in conflicted_qty_keys:
                status, method, product = "ignored", "quantity_conflict", None
                seen_qty = sorted(q for q in qty_groups[key] if q is not None)
                other_rows = [expanded[i]["global_row_no"] for i in qty_group_rows[key] if i != idx]
                errors.append(
                    f"同货号同仓库出现不同数量{seen_qty}（另见第{other_rows}行），未自动合并/取最大/取最新，"
                    "本行不计入库存合计，需人工核实"
                )
            elif status == "conflict" and conflict_pair is not None:
                by_code_product, by_jan_product = conflict_pair
                errors.append(
                    f"货号命中商品{by_code_product.internal_sku}，JAN候选命中商品{by_jan_product.internal_sku}，"
                    "二者不一致，需人工确认后再匹配库存"
                )
            elif status == "unmatched":
                errors.append("未匹配到本地商品")

        warehouse = _warehouse(session, item["warehouse_name"])
        if warehouse is None:
            errors.append(f"未知秦丝仓库：{item['warehouse_name'] or '未提供'}")

        results.append(_RowResult(
            global_row_no=item["global_row_no"], source_file=item["source_file"], source_row_no=item["source_row_no"],
            name=item["name"], code=item["code"], jan=item["jan"], internal_sku=item["internal_sku"],
            warehouse_name=item["warehouse_name"], quantity=item["quantity"], raw=item["raw"],
            product=product, matching_method=method, matching_status=status, conflict_pair=conflict_pair,
            warehouse=warehouse, error_notes=tuple(dict.fromkeys(errors)),
        ))
    return results


def _line_from_result(snapshot: QinsiInventorySnapshot, result: _RowResult) -> QinsiInventorySnapshotLine:
    summary = {key: value for key, value in result.raw.items() if (value or "").strip()}
    summary["__源文件__"] = result.source_file
    summary["__源行号__"] = result.source_row_no
    return QinsiInventorySnapshotLine(
        snapshot_id=snapshot.id,
        original_row_no=result.global_row_no,
        raw_product_name=result.name,
        jan=result.jan,
        qinsi_product_code=result.code,
        internal_sku=result.internal_sku,
        raw_warehouse_name=result.warehouse_name,
        quantity=result.quantity,
        raw_summary_json=json.dumps(summary, ensure_ascii=False, default=str),
        product_id=result.product.id if result.product else None,
        warehouse_id=result.warehouse.id if result.warehouse else None,
        matching_method=result.matching_method,
        matching_status=result.matching_status,
        warehouse_status="matched" if result.warehouse else "unknown",
        error_message="；".join(result.error_notes) or None,
    )


_FILENAME_RANGE_PATTERN = re.compile(r"\((\d+)\s*-\s*(\d+)\)")


def _parse_filename_range(filename: str) -> tuple[int, int] | None:
    match = _FILENAME_RANGE_PATTERN.search(filename)
    if not match:
        return None
    start, end = int(match.group(1)), int(match.group(2))
    return (start, end) if start <= end else None


@dataclass(frozen=True, slots=True)
class FileRangeInfo:
    filename: str
    range: tuple[int, int] | None
    row_count: int


@dataclass(frozen=True, slots=True)
class MultiFileCompletenessReport:
    files: tuple[FileRangeInfo, ...]
    all_ranges_parsed: bool
    expected_min: int | None
    expected_max: int | None
    expected_total_from_ranges: int | None
    actual_total_rows: int
    gaps: tuple[tuple[int, int], ...]
    overlaps: tuple[tuple[int, int, int, int], ...]
    header_mismatch: bool

    @property
    def has_blocking_issue(self) -> bool:
        return bool(self.gaps) or bool(self.overlaps) or self.header_mismatch


def range_completeness_from_file_infos(
    infos: list[FileRangeInfo], *, header_mismatch: bool,
) -> MultiFileCompletenessReport:
    """Pure range/gap/overlap math over already-computed per-file info.

    Shared by every multi-file upload flow (inventory snapshot, sales
    summary, ...) -- callers parse their own format and build FileRangeInfo
    via _parse_filename_range()/their own row count, then hand the list
    here. Filenames are a hint, never the source of truth for row counts;
    an unparseable filename just can't be range-checked, never treated as a
    gap/overlap/error on its own.
    """
    ranges = [info.range for info in infos if info.range]
    all_ranges_parsed = bool(infos) and len(ranges) == len(infos)
    ranges_sorted = sorted(ranges)
    overlaps: list[tuple[int, int, int, int]] = []
    for i in range(len(ranges_sorted)):
        for j in range(i + 1, len(ranges_sorted)):
            a_start, a_end = ranges_sorted[i]
            b_start, b_end = ranges_sorted[j]
            if b_start <= a_end:
                overlaps.append((a_start, a_end, b_start, b_end))
    gaps: list[tuple[int, int]] = []
    running_end: int | None = None
    for start, end in ranges_sorted:
        if running_end is not None and start > running_end + 1:
            gaps.append((running_end + 1, start - 1))
        running_end = max(running_end, end) if running_end is not None else end

    return MultiFileCompletenessReport(
        files=tuple(infos),
        all_ranges_parsed=all_ranges_parsed,
        expected_min=ranges_sorted[0][0] if ranges_sorted else None,
        expected_max=ranges_sorted[-1][1] if ranges_sorted else None,
        expected_total_from_ranges=sum(end - start + 1 for start, end in ranges) if ranges else None,
        actual_total_rows=sum(info.row_count for info in infos),
        gaps=tuple(gaps),
        overlaps=tuple(overlaps),
        header_mismatch=header_mismatch,
    )


def analyze_multi_file_completeness(files: list[tuple[str, bytes]]) -> MultiFileCompletenessReport:
    """Filename-range sanity check for a multi-file INVENTORY snapshot upload
    (qinsi_goods_import's format). See range_completeness_from_file_infos()
    for the format-agnostic core this delegates to.
    """
    infos: list[FileRangeInfo] = []
    headers_seen: set[tuple[str, ...]] = set()
    for filename, content in files:
        parsed = parse_qinsi_workbook(content)
        # The legacy qinsi_goods_template format's last header cell holds the
        # sheet's single warehouse NAME (a data value), not a real column
        # header -- comparing it across files would flag every legitimate
        # multi-warehouse merge of that format as a header mismatch.
        comparable_headers = parsed.headers[:-1] if parsed.source_format == "qinsi_goods_template" else parsed.headers
        headers_seen.add(comparable_headers)
        row_range = _parse_filename_range(filename)
        infos.append(FileRangeInfo(filename=Path(filename).name, range=row_range, row_count=len(parsed.rows)))

    return range_completeness_from_file_infos(infos, header_mismatch=len(headers_seen) > 1)


def _validate_upload_files(files: list[tuple[str, bytes]], settings: InventorySettings) -> None:
    if not files:
        raise ValueError("至少需要一个Excel文件")
    for filename, content in files:
        extension = Path(filename or "").suffix.lower()
        if extension not in settings.allowed_extensions:
            raise ValueError(f"只允许上传：{', '.join(settings.allowed_extensions)}（{filename or '未命名文件'}）")
        if not content or len(content) > settings.max_upload_bytes:
            raise ValueError(f"Excel文件为空或超过允许大小（{filename or '未命名文件'}）")


def _combined_file_hash(files: list[tuple[str, bytes]]) -> str:
    if len(files) == 1:
        return hashlib.sha256(files[0][1]).hexdigest()
    digest = hashlib.sha256()
    for name, content in sorted(files, key=lambda item: item[0]):
        digest.update(f"{Path(name).name}:{len(content)}:".encode("utf-8"))
        digest.update(hashlib.sha256(content).digest())
    return digest.hexdigest()


@dataclass(frozen=True, slots=True)
class InventorySnapshotPreview:
    completeness: MultiFileCompletenessReport
    total_rows: int
    matched_count: int
    unmatched_count: int
    conflict_count: int
    duplicate_count: int
    quantity_conflict_count: int
    method_counts: dict[str, int]
    warehouse_counts: dict[str, tuple[int, int]]
    china_quantity: int
    japan_quantity: int
    unclassified_quantity: int
    conflicts: tuple[_RowResult, ...]
    duplicates: tuple[_RowResult, ...]
    quantity_conflicts: tuple[_RowResult, ...]


def preview_inventory_snapshot_files(
    session: Session, files: list[tuple[str, bytes]],
) -> InventorySnapshotPreview:
    """Read-only dry run: parses + matches every row but writes nothing."""
    settings = inventory_settings()
    _validate_upload_files(files, settings)
    completeness = analyze_multi_file_completeness(files)

    source_rows: list[tuple[str, int, dict]] = []
    for filename, content in files:
        for row_no, raw in read_product_sheet(content):
            source_rows.append((Path(filename).name, row_no, raw))
    results = _compute_row_results(session, source_rows)

    method_counts: dict[str, int] = {}
    warehouse_counts: dict[str, list[int]] = {}
    china_quantity = japan_quantity = unclassified_quantity = 0
    matched = unmatched = conflict = duplicate = quantity_conflict = 0
    conflicts: list[_RowResult] = []
    duplicates: list[_RowResult] = []
    quantity_conflicts: list[_RowResult] = []

    for result in results:
        method_key = result.matching_method or result.matching_status
        method_counts[method_key] = method_counts.get(method_key, 0) + 1
        if result.matching_status == "matched":
            matched += 1
        elif result.matching_status == "unmatched":
            unmatched += 1
        elif result.matching_status == "conflict":
            conflict += 1
            conflicts.append(result)
        elif result.matching_status == "ignored":
            if result.matching_method == "duplicate":
                duplicate += 1
                duplicates.append(result)
            elif result.matching_method == "quantity_conflict":
                quantity_conflict += 1
                quantity_conflicts.append(result)
        # Warehouse/region totals reflect QinSi's own reported stock per
        # warehouse -- every row with a resolved warehouse + a real quantity
        # counts, whether or not it matched a local Product yet. Rows
        # collapsed as duplicate/quantity-conflict ("ignored") are excluded
        # so nothing is double counted or guessed.
        if result.warehouse is not None and result.quantity is not None and result.matching_status != "ignored":
            bucket = warehouse_counts.setdefault(result.warehouse.display_name, [0, 0])
            bucket[0] += 1
            bucket[1] += result.quantity
            region = inventory_region_for_location(result.warehouse)
            if region == INVENTORY_REGION_CHINA:
                china_quantity += result.quantity
            elif region == INVENTORY_REGION_JAPAN:
                japan_quantity += result.quantity
            else:
                unclassified_quantity += result.quantity

    return InventorySnapshotPreview(
        completeness=completeness, total_rows=len(results),
        matched_count=matched, unmatched_count=unmatched, conflict_count=conflict,
        duplicate_count=duplicate, quantity_conflict_count=quantity_conflict,
        method_counts=method_counts,
        warehouse_counts={name: (value[0], value[1]) for name, value in warehouse_counts.items()},
        china_quantity=china_quantity, japan_quantity=japan_quantity, unclassified_quantity=unclassified_quantity,
        conflicts=tuple(conflicts), duplicates=tuple(duplicates), quantity_conflicts=tuple(quantity_conflicts),
    )


def create_inventory_snapshot(
    session: Session,
    filename: str,
    content: bytes,
    *,
    data_at: datetime | None = None,
    now: datetime | None = None,
) -> tuple[QinsiInventorySnapshot, bool]:
    return create_inventory_snapshot_from_files(session, [(filename, content)], data_at=data_at, now=now)


def create_inventory_snapshot_from_files(
    session: Session,
    files: list[tuple[str, bytes]],
    *,
    data_at: datetime | None = None,
    now: datetime | None = None,
) -> tuple[QinsiInventorySnapshot, bool]:
    """Create exactly one snapshot from one or more source files.

    Multiple files are treated as segments of a single QinSi export taken at
    one point in time: one snapshot_id, one imported_at/data_at, matching and
    duplicate detection run across the merged row set, never per file.
    """
    settings = inventory_settings()
    _validate_upload_files(files, settings)
    file_hash = _combined_file_hash(files)
    existing = session.scalar(select(QinsiInventorySnapshot).where(QinsiInventorySnapshot.file_hash == file_hash))
    if existing is not None:
        if settings.reuse_duplicate_file:
            return existing, True
        raise ValueError("该文件（组合）已导入，当前配置禁止重复文件复用")

    imported_at = now or datetime.now(timezone.utc)
    if len(files) == 1:
        original_filename = Path(files[0][0]).name[:255]
        file_content = files[0][1]
    else:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, content in files:
                archive.writestr(Path(name).name, content)
        file_content = buffer.getvalue()
        original_filename = f"秦丝库存快照_合并{len(files)}个文件_{imported_at:%Y%m%d}.zip"[:255]

    snapshot = QinsiInventorySnapshot(
        batch_no=f"QS-{imported_at:%Y%m%d}-{uuid.uuid4().hex[:10].upper()}",
        original_filename=original_filename,
        file_hash=file_hash,
        file_content=file_content,
        imported_at=imported_at,
        data_at=data_at,
        status="completed",
    )
    session.add(snapshot)
    session.flush()

    source_rows: list[tuple[str, int, dict]] = []
    for filename, content in files:
        for row_no, raw in read_product_sheet(content):
            source_rows.append((Path(filename).name, row_no, raw))

    for result in _compute_row_results(session, source_rows):
        snapshot.lines.append(_line_from_result(snapshot, result))

    _refresh_snapshot_summary(snapshot)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        existing = session.scalar(select(QinsiInventorySnapshot).where(QinsiInventorySnapshot.file_hash == file_hash))
        if existing is not None:
            return existing, True
        raise
    session.refresh(snapshot)
    return snapshot, False


def list_inventory_snapshots(session: Session) -> list[QinsiInventorySnapshot]:
    return list(session.scalars(
        select(QinsiInventorySnapshot).order_by(QinsiInventorySnapshot.imported_at.desc(), QinsiInventorySnapshot.id.desc())
    ))


def get_inventory_snapshot(session: Session, snapshot_id: int) -> QinsiInventorySnapshot | None:
    return session.scalar(
        select(QinsiInventorySnapshot)
        .where(QinsiInventorySnapshot.id == snapshot_id)
        .options(
            selectinload(QinsiInventorySnapshot.lines).selectinload(QinsiInventorySnapshotLine.product),
            selectinload(QinsiInventorySnapshot.lines).selectinload(QinsiInventorySnapshotLine.warehouse),
        )
    )


def get_inventory_snapshot_summary(session: Session, snapshot_id: int) -> QinsiInventorySnapshot | None:
    """Same snapshot row as get_inventory_snapshot, but WITHOUT eager-loading
    every line -- for the detail/review pages, which only need the snapshot's
    own summary columns (total_rows/success_rows/... are precomputed at
    import/retry time, not derived from .lines) plus a paginated line query."""
    return session.get(QinsiInventorySnapshot, snapshot_id)


SNAPSHOT_LINE_PAGE_SIZES = (10, 20, 50, 100)
DEFAULT_SNAPSHOT_LINE_PAGE_SIZE = 20


@dataclass(frozen=True, slots=True)
class SnapshotLinesPage:
    lines: list[QinsiInventorySnapshotLine]
    total_count: int
    page: int
    page_size: int

    @property
    def total_pages(self) -> int:
        return max(1, -(-self.total_count // self.page_size))


def get_inventory_snapshot_lines_page(
    session: Session, snapshot_id: int, *, matching_status: str | None = None, warehouse_id: int | None = None,
    name_query: str | None = None, jan_query: str | None = None, qinsi_code_query: str | None = None,
    actionable_only: bool = False, page: int = 1, page_size: int = DEFAULT_SNAPSHOT_LINE_PAGE_SIZE,
) -> SnapshotLinesPage:
    """Real SQL-level pagination -- never loads the whole (potentially
    thousands-of-rows) line set to slice it in Python or hide rows with CSS.
    Filtering happens in the WHERE clause too, so the count/pages reflect the
    filtered set, not the whole snapshot.

    actionable_only reproduces the review page's original in-memory filter
    (unmatched/conflict OR a warehouse that still needs mapping) -- it takes
    priority over matching_status, since the review page is inherently about
    "still needs a human decision" rather than one single status value.
    """
    page_size = page_size if page_size in SNAPSHOT_LINE_PAGE_SIZES else DEFAULT_SNAPSHOT_LINE_PAGE_SIZE
    page = max(1, page)

    conditions = [QinsiInventorySnapshotLine.snapshot_id == snapshot_id]
    if actionable_only:
        conditions.append(or_(
            QinsiInventorySnapshotLine.matching_status.in_(("unmatched", "conflict")),
            QinsiInventorySnapshotLine.warehouse_status != "matched",
        ))
    elif matching_status and matching_status != "all":
        conditions.append(QinsiInventorySnapshotLine.matching_status == matching_status)
    if warehouse_id:
        conditions.append(QinsiInventorySnapshotLine.warehouse_id == warehouse_id)
    if name_query:
        conditions.append(QinsiInventorySnapshotLine.raw_product_name.like(f"%{name_query}%"))
    if jan_query:
        conditions.append(QinsiInventorySnapshotLine.jan.like(f"%{jan_query}%"))
    if qinsi_code_query:
        conditions.append(QinsiInventorySnapshotLine.qinsi_product_code.like(f"%{qinsi_code_query}%"))

    total_count = session.scalar(
        select(func.count()).select_from(QinsiInventorySnapshotLine).where(*conditions)
    ) or 0
    lines = list(session.scalars(
        select(QinsiInventorySnapshotLine).where(*conditions)
        .options(selectinload(QinsiInventorySnapshotLine.product), selectinload(QinsiInventorySnapshotLine.warehouse))
        .order_by(QinsiInventorySnapshotLine.original_row_no)
        .limit(page_size).offset((page - 1) * page_size)
    ))
    return SnapshotLinesPage(lines=lines, total_count=total_count, page=page, page_size=page_size)


def get_snapshot_warehouse_distribution(session: Session, snapshot_id: int) -> dict[str, int]:
    """Quantity per warehouse across the WHOLE snapshot (never affected by line
    pagination/filters) -- a SQL GROUP BY, not a Python loop over every row."""
    warehouse_name = func.coalesce(Location.display_name, QinsiInventorySnapshotLine.raw_warehouse_name, "未知仓库")
    rows = session.execute(
        select(warehouse_name, func.coalesce(func.sum(QinsiInventorySnapshotLine.quantity), 0))
        .select_from(QinsiInventorySnapshotLine)
        .outerjoin(Location, Location.id == QinsiInventorySnapshotLine.warehouse_id)
        .where(
            QinsiInventorySnapshotLine.snapshot_id == snapshot_id,
            QinsiInventorySnapshotLine.matching_status != "ignored",
        )
        .group_by(warehouse_name)
    ).all()
    return {name: int(quantity) for name, quantity in rows}


def manual_match_line(session: Session, line_id: int, product_id: int) -> QinsiInventorySnapshotLine:
    line = session.get(QinsiInventorySnapshotLine, line_id)
    product = session.get(Product, product_id)
    if line is None:
        raise LookupError("快照明细不存在")
    if product is None:
        raise LookupError("商品不存在")
    line.product_id = product.id
    line.matching_method = "manual"
    line.matching_status = "matched"
    if line.qinsi_product_code:
        mapping = session.scalar(select(QinsiProductMapping).where(
            QinsiProductMapping.qinsi_product_code == line.qinsi_product_code
        ))
        if mapping is None:
            mapping = QinsiProductMapping(qinsi_product_code=line.qinsi_product_code, product_id=product.id)
            session.add(mapping)
        else:
            mapping.product_id = product.id
            mapping.confirmed_at = datetime.now(timezone.utc)
        mapping.source_snapshot_line_id = line.id
    line.error_message = _line_errors_without(line, "未匹配到本地商品")
    snapshot = line.snapshot
    _refresh_snapshot_summary(snapshot)
    session.commit()
    session.refresh(line)
    return line


def _line_errors_without(line: QinsiInventorySnapshotLine, text: str) -> str | None:
    values = [value for value in (line.error_message or "").split("；") if value and value != text]
    return "；".join(values) or None


def _clear_matching_errors(line: QinsiInventorySnapshotLine) -> str | None:
    values = [
        value for value in (line.error_message or "").split("；")
        if value
        and value != "未匹配到本地商品"
        and not value.startswith("货号命中商品")
    ]
    return "；".join(values) or None


def map_line_warehouse(session: Session, line_id: int, warehouse_id: int) -> QinsiInventorySnapshotLine:
    line = session.get(QinsiInventorySnapshotLine, line_id)
    warehouse = session.get(Location, warehouse_id)
    if line is None:
        raise LookupError("快照明细不存在")
    if warehouse is None or not warehouse.is_active or not warehouse.is_qinsi_warehouse:
        raise ValueError("只能映射到已有且启用的秦丝仓库")
    line.warehouse_id = warehouse.id
    line.warehouse_status = "matched"
    errors = [value for value in (line.error_message or "").split("；") if not value.startswith("未知秦丝仓库：")]
    line.error_message = "；".join(errors) or None
    snapshot = line.snapshot
    _refresh_snapshot_summary(snapshot)
    session.commit()
    session.refresh(line)
    return line


def ignore_snapshot_lines(session: Session, snapshot_id: int, line_ids: set[int]) -> int:
    lines = list(session.scalars(select(QinsiInventorySnapshotLine).where(
        QinsiInventorySnapshotLine.snapshot_id == snapshot_id,
        QinsiInventorySnapshotLine.id.in_(line_ids),
        QinsiInventorySnapshotLine.matching_status.in_(("unmatched", "conflict")),
    ))) if line_ids else []
    for line in lines:
        line.matching_status = "ignored"
        line.product_id = None
        line.matching_method = None
    snapshot = session.get(QinsiInventorySnapshot, snapshot_id)
    if snapshot is None:
        raise LookupError("库存快照不存在")
    _refresh_snapshot_summary(snapshot)
    session.commit()
    return len(lines)


def retry_snapshot_matching(session: Session, snapshot_id: int) -> int:
    """Recompute unmatched/conflict lines. Never bypasses a conflict: a row
    that still resolves to two different products stays matching_status
    "conflict" (with refreshed candidate info), it is not silently promoted
    to matched."""
    snapshot = get_inventory_snapshot(session, snapshot_id)
    if snapshot is None:
        raise LookupError("库存快照不存在")
    matched = 0
    for line in snapshot.lines:
        if line.matching_status not in {"unmatched", "conflict"}:
            continue
        product, method, status, conflict_pair = _match_product(
            session, code=line.qinsi_product_code, jan=line.jan, internal_sku=line.internal_sku,
        )
        if status == "matched" and product is not None:
            line.product_id = product.id
            line.matching_method = method
            line.matching_status = status
            line.error_message = _clear_matching_errors(line)
            matched += 1
        elif status == "conflict" and conflict_pair is not None:
            by_code_product, by_jan_product = conflict_pair
            line.product_id = None
            line.matching_method = None
            line.matching_status = "conflict"
            note = (
                f"货号命中商品{by_code_product.internal_sku}，JAN候选命中商品{by_jan_product.internal_sku}，"
                "二者不一致，需人工确认后再匹配库存"
            )
            kept = _clear_matching_errors(line)
            line.error_message = "；".join(dict.fromkeys(value for value in (kept, note) if value))
    _refresh_snapshot_summary(snapshot)
    session.commit()
    return matched


def available_qinsi_warehouses(session: Session) -> list[Location]:
    return list(session.scalars(select(Location).where(
        Location.is_qinsi_warehouse.is_(True), Location.is_active.is_(True),
    ).order_by(Location.sort_order, Location.id)))


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def latest_inventory_for_product(
    session: Session, product_id: int, *, now: datetime | None = None,
) -> ProductInventoryView:
    return latest_inventory_for_products(session, [product_id], now=now)[product_id]


def latest_inventory_for_products(
    session: Session, product_ids: list[int], *, now: datetime | None = None,
) -> dict[int, ProductInventoryView]:
    settings = inventory_settings()
    if not product_ids:
        return {}
    records = session.execute(
        select(QinsiInventorySnapshotLine, QinsiInventorySnapshot)
        .join(QinsiInventorySnapshot, QinsiInventorySnapshot.id == QinsiInventorySnapshotLine.snapshot_id)
        .where(
            QinsiInventorySnapshotLine.product_id.in_(product_ids),
            QinsiInventorySnapshotLine.matching_status == "matched",
        )
        .options(selectinload(QinsiInventorySnapshotLine.warehouse))
        .order_by(
            func.coalesce(QinsiInventorySnapshot.data_at, QinsiInventorySnapshot.imported_at).desc(),
            QinsiInventorySnapshot.id.desc(),
            QinsiInventorySnapshotLine.id,
        )
    ).all()
    grouped: dict[int, list[tuple[QinsiInventorySnapshotLine, QinsiInventorySnapshot]]] = {
        product_id: [] for product_id in product_ids
    }
    for line, snapshot in records:
        if line.product_id in grouped:
            grouped[line.product_id].append((line, snapshot))
    return {
        product_id: _inventory_view_from_records(grouped[product_id], settings, now=now)
        for product_id in product_ids
    }


def _inventory_view_from_records(records, settings, *, now: datetime | None = None) -> ProductInventoryView:
    if not records:
        return ProductInventoryView(None, (), None, None, False, settings.stale_hours)
    snapshot = records[0][1]
    quantities: dict[int, tuple[Location, int]] = {}
    selected_snapshot_by_warehouse: dict[int, int] = {}
    selected_times: list[datetime] = []
    review_needed = False
    for line, line_snapshot in records:
        if line.warehouse is None:
            review_needed = True
            continue
        selected_snapshot_id = selected_snapshot_by_warehouse.get(line.warehouse.id)
        if selected_snapshot_id is not None and selected_snapshot_id != line_snapshot.id:
            continue
        if selected_snapshot_id is None:
            selected_snapshot_by_warehouse[line.warehouse.id] = line_snapshot.id
            selected_times.append(line_snapshot.data_at or line_snapshot.imported_at)
        if line.quantity is None:
            review_needed = True
            continue
        previous = quantities.get(line.warehouse.id, (line.warehouse, 0))
        quantities[line.warehouse.id] = (line.warehouse, previous[1] + line.quantity)
    warehouses = tuple(
        WarehouseStock(warehouse, quantity)
        for warehouse, quantity in sorted(quantities.values(), key=lambda value: (value[0].sort_order, value[0].id))
    )
    total = sum(item.quantity for item in warehouses)
    data_time = min(selected_times) if selected_times else (snapshot.data_at or snapshot.imported_at)
    current = now or datetime.now(timezone.utc)
    stale = _aware(current) - _aware(data_time) > timedelta(hours=settings.stale_hours)
    return ProductInventoryView(snapshot, warehouses, total, data_time, stale, settings.stale_hours, review_needed)


def _pending_quantities(session: Session, product_id: int) -> tuple[int, int]:
    items = list(session.scalars(
        select(PurchaseBatchItem)
        .join(PurchaseBatch, PurchaseBatch.id == PurchaseBatchItem.purchase_batch_id)
        .where(PurchaseBatchItem.product_id == product_id, PurchaseBatch.status != "cancelled")
    ))
    if not items:
        return 0, 0
    active_lines = list(session.scalars(
        select(QinsiPurchaseExportLine)
        .join(QinsiPurchaseExportLineSource, QinsiPurchaseExportLineSource.export_line_id == QinsiPurchaseExportLine.id)
        .where(
            QinsiPurchaseExportLineSource.purchase_batch_item_id.in_([item.id for item in items]),
            QinsiPurchaseExportLineSource.is_active.is_(True),
        )
    ))
    by_item = {line.purchase_batch_item_id: line for line in active_lines}
    pending = sum(item.quantity for item in items if by_item.get(item.id) is None or by_item[item.id].status != "imported")
    awaiting = sum(item.quantity for item in items if by_item.get(item.id) is not None and by_item[item.id].status == "generated")
    return pending, awaiting


def purchase_assistance(
    session: Session,
    product: Product,
    *,
    inventory: ProductInventoryView | None = None,
    now: datetime | None = None,
) -> PurchaseAssistance:
    settings = inventory_settings()
    inventory = inventory or latest_inventory_for_product(session, product.id, now=now)
    threshold = product.low_stock_threshold or settings.default_low_stock_threshold
    pending, awaiting = _pending_quantities(session, product.id)
    watch = session.scalar(select(ProductWatchConfig).where(ProductWatchConfig.product_id == product.id))
    target_reached = bool(
        watch and watch.enabled and watch.current_lowest_price is not None
        and watch.effective_target_price is not None and watch.current_lowest_price <= watch.effective_target_price
    )
    if not settings.purchase_assistance_enabled:
        base_status, message = "review_needed", "采购辅助提示已关闭"
    elif inventory.snapshot is None:
        base_status, message = "no_snapshot", "暂无秦丝库存快照，请先导入后再判断"
    elif inventory.review_needed:
        base_status, message = "review_needed", "最近快照存在仓库或数量异常，需要人工复核"
    elif inventory.is_stale:
        base_status, message = "snapshot_stale", "秦丝库存快照已过期，请更新快照"
    elif inventory.total_quantity is not None and inventory.total_quantity <= 0:
        base_status, message = "out_of_stock", "秦丝最近快照数量为0"
    elif inventory.total_quantity is not None and inventory.total_quantity < threshold:
        base_status = "stock_low"
        message = "库存偏低且已达到目标价，可考虑补货" if target_reached else "秦丝最近快照库存偏低，建议结合价格人工判断"
    else:
        base_status, message = "stock_available", "秦丝最近快照库存充足，暂缓补货"
    has_incoming = pending > 0
    status = "incoming_or_pending" if has_incoming else base_status
    if has_incoming:
        message = f"{message}；另有{pending}件采购待提交或待确认，未计入秦丝快照库存"
    return PurchaseAssistance(
        status, base_status, message, threshold, pending, awaiting, has_incoming, target_reached,
    )


def update_product_low_stock_threshold(session: Session, product_id: int, value: int | str | None) -> Product:
    product = session.get(Product, product_id)
    if product is None:
        raise LookupError("商品不存在")
    if value in (None, ""):
        product.low_stock_threshold = None
    else:
        try:
            threshold = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("低库存阈值必须是正整数") from exc
        if threshold <= 0:
            raise ValueError("低库存阈值必须是正整数")
        product.low_stock_threshold = threshold
    session.commit()
    session.refresh(product)
    return product


def latest_snapshot_statistics(session: Session) -> tuple[QinsiInventorySnapshot | None, list[tuple[str, int, int]]]:
    snapshot = session.scalar(select(QinsiInventorySnapshot).order_by(
        func.coalesce(QinsiInventorySnapshot.data_at, QinsiInventorySnapshot.imported_at).desc(),
        QinsiInventorySnapshot.id.desc(),
    ).limit(1))
    if snapshot is None:
        return None, []
    rows = session.execute(
        select(Location.display_name, func.count(func.distinct(QinsiInventorySnapshotLine.product_id)), func.coalesce(func.sum(QinsiInventorySnapshotLine.quantity), 0))
        .join(QinsiInventorySnapshotLine, QinsiInventorySnapshotLine.warehouse_id == Location.id)
        .where(
            QinsiInventorySnapshotLine.snapshot_id == snapshot.id,
            QinsiInventorySnapshotLine.matching_status == "matched",
        )
        .group_by(Location.id, Location.display_name)
        .order_by(Location.sort_order, Location.id)
    ).all()
    return snapshot, [(name, product_count, quantity) for name, product_count, quantity in rows]


def watched_inventory_status_distribution(session: Session, *, now: datetime | None = None) -> dict[str, int]:
    result: dict[str, int] = {}
    products = list(session.scalars(
        select(Product).join(ProductWatchConfig, ProductWatchConfig.product_id == Product.id)
        .where(ProductWatchConfig.enabled.is_(True))
    ))
    for product in products:
        status = purchase_assistance(session, product, now=now).base_status
        result[status] = result.get(status, 0) + 1
    return result

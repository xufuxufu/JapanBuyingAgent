from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any


DIMENSION_TOKEN_PATTERN = re.compile(
    r"(?i)(W|幅|横|L|長さ|H|高さ|D|奥行|奥行き|深さ)\s*約?\s*"
    r"(\d+(?:\.\d+)?)\s*(?:mm|㎜)?"
)
DIMENSION_TEXT_PATTERN = re.compile(
    r"(?i)(?:W|幅|横|L|長さ|H|高さ|D|奥行|奥行き|深さ)\s*約?\s*\d+(?:\.\d+)?\s*(?:mm|㎜)?"
    r"(?:\s*[x×＊*]\s*(?:W|幅|横|L|長さ|H|高さ|D|奥行|奥行き|深さ)\s*約?\s*\d+(?:\.\d+)?\s*(?:mm|㎜)?)+"
)
WEIGHT_PATTERN = re.compile(r"(?i)(?<![A-Z0-9])(\d+(?:\.\d+)?)\s*(kg|g|グラム)(?![A-Z0-9])")
VOLUME_PATTERN = re.compile(r"(?i)(?<![A-Z0-9])(\d+(?:\.\d+)?)\s*(ml|mL|ML|l|L|ミリリットル)(?![A-Z0-9])")
PACK_PATTERN = re.compile(r"(?<!\d)([2-9]\d{0,2})\s*(?:個|个|本|枚|袋|包|箱|錠|粒)\s*(?:装|裝|入り|入|セット|パック)?")
SPEC_SNIPPET_PATTERN = re.compile(
    r"(?i)(?:W|幅|横|L|長さ|H|高さ|D|奥行|奥行き|深さ)\s*約?\s*\d|"
    r"\d+(?:\.\d+)?\s*(?:kg|g|ml|mL|ML|l|L|個|个|本|枚|袋|包|箱|錠|粒)\s*(?:装|裝|入り|入|セット|パック)?"
)


@dataclass(frozen=True, slots=True)
class ParsedProductSpecs:
    net_weight_g: Decimal | None = None
    volume_ml: Decimal | None = None
    length_mm: Decimal | None = None
    width_mm: Decimal | None = None
    height_mm: Decimal | None = None
    depth_mm: Decimal | None = None
    pack_quantity: int | None = None
    spec_text: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "net_weight_g": self.net_weight_g,
            "volume_ml": self.volume_ml,
            "length_mm": self.length_mm,
            "width_mm": self.width_mm,
            "height_mm": self.height_mm,
            "depth_mm": self.depth_mm,
            "pack_quantity": self.pack_quantity,
            "spec_text": self.spec_text,
        }


def _decimal(value: str) -> Decimal | None:
    try:
        return Decimal(value)
    except (InvalidOperation, TypeError):
        return None


def _clean_text(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def extract_spec_text(*values: object, limit: int = 255) -> str | None:
    snippets: list[str] = []
    for value in values:
        text = _clean_text(value)
        if not text:
            continue
        dimension = DIMENSION_TEXT_PATTERN.search(text)
        if dimension:
            snippets.append(dimension.group(0).strip())
        for match in SPEC_SNIPPET_PATTERN.finditer(text):
            snippet = match.group(0).strip()
            if snippet not in snippets:
                snippets.append(snippet)
        if snippets:
            break
    joined = " / ".join(snippets)
    return joined[:limit] or None


def parse_product_specs(*values: object) -> ParsedProductSpecs:
    text = " ".join(_clean_text(value) for value in values if _clean_text(value))
    if not text:
        return ParsedProductSpecs()
    dimensions: dict[str, Decimal] = {}
    for label, value in DIMENSION_TOKEN_PATTERN.findall(text):
        amount = _decimal(value)
        if amount is None:
            continue
        folded = label.casefold()
        if folded in {"w", "幅", "横"}:
            dimensions.setdefault("width_mm", amount)
        elif folded in {"l", "長さ"}:
            dimensions.setdefault("length_mm", amount)
        elif folded in {"h", "高さ"}:
            dimensions.setdefault("height_mm", amount)
        elif folded in {"d", "奥行", "奥行き", "深さ"}:
            dimensions.setdefault("depth_mm", amount)
    weight = None
    for value, unit in WEIGHT_PATTERN.findall(text):
        amount = _decimal(value)
        if amount is None:
            continue
        weight = amount * Decimal("1000") if unit.casefold() == "kg" else amount
        break
    volume = None
    for value, unit in VOLUME_PATTERN.findall(text):
        amount = _decimal(value)
        if amount is None:
            continue
        volume = amount * Decimal("1000") if unit.casefold() == "l" else amount
        break
    pack_quantity = None
    match = PACK_PATTERN.search(text)
    if match:
        pack_quantity = int(match.group(1))
    return ParsedProductSpecs(
        net_weight_g=weight,
        volume_ml=volume,
        length_mm=dimensions.get("length_mm"),
        width_mm=dimensions.get("width_mm"),
        height_mm=dimensions.get("height_mm"),
        depth_mm=dimensions.get("depth_mm"),
        pack_quantity=pack_quantity,
        spec_text=extract_spec_text(text),
    )


def compact_spec_label(product: Any) -> str:
    parts: list[str] = []
    if getattr(product, "net_weight_g", None) is not None:
        parts.append(f"{_format_decimal(product.net_weight_g)}g")
    if getattr(product, "volume_ml", None) is not None:
        parts.append(f"{_format_decimal(product.volume_ml)}ml")
    dimensions = []
    for prefix, field in (("L", "length_mm"), ("W", "width_mm"), ("H", "height_mm"), ("D", "depth_mm")):
        value = getattr(product, field, None)
        if value is not None:
            dimensions.append(f"{prefix}{_format_decimal(value)}")
    if dimensions:
        parts.append("×".join(dimensions) + "mm")
    if getattr(product, "pack_quantity", None):
        parts.append(f"{product.pack_quantity}个装")
    return " / ".join(parts)


def _format_decimal(value: Any) -> str:
    decimal = value if isinstance(value, Decimal) else Decimal(str(value))
    if decimal == decimal.to_integral_value():
        return str(int(decimal))
    return format(decimal.normalize(), "f")

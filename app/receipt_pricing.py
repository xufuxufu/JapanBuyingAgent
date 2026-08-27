from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP


def purchase_unit_price(quantity: int | None, unit_price: int | None, line_amount: int | None) -> int | None:
    if unit_price is not None:
        return unit_price
    if quantity and quantity > 0 and line_amount is not None:
        return int((Decimal(line_amount) / Decimal(quantity)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    return None


def actual_line_amount(quantity: int | None, unit_price: int | None, discount_amount: int | None, line_amount: int | None) -> int | None:
    if line_amount is not None:
        return line_amount
    if unit_price is None or not quantity:
        return None
    return int(unit_price) * int(quantity) - int(discount_amount or 0)

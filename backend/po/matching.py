from pydantic import BaseModel

from backend.core.enums import InvoiceMatchStatus

PRICE_TOLERANCE_INR = 0.50
LINE_TOTAL_TOLERANCE_INR = 1.00


class ThreeWayMatchInput(BaseModel):
    po_qty: int
    po_unit_price: float
    grn_received_qty: int
    invoice_qty: int
    invoice_unit_price: float
    invoice_line_subtotal: float | None = None


class ThreeWayMatchResult(BaseModel):
    matched: bool
    match_status: InvoiceMatchStatus
    qty_ok: bool
    price_ok: bool
    math_ok: bool
    price_diff_per_unit: float
    total_price_impact: float
    qty_diff: int
    discrepancy_summary: str
    damaged_hold: bool = False
    stock_action: str = "NONE"
    stock_qty: int = 0
    stock_note: str = ""


def evaluate_three_way_match(data: ThreeWayMatchInput) -> ThreeWayMatchResult:
    """Pure deterministic 3-way match across PO, GRN, and extracted Invoice values."""
    qty_ok = data.invoice_qty == data.grn_received_qty
    price_diff = round(data.invoice_unit_price - data.po_unit_price, 4)
    price_ok = abs(price_diff) <= PRICE_TOLERANCE_INR

    expected_line_subtotal = round(data.invoice_qty * data.invoice_unit_price, 2)
    if data.invoice_line_subtotal is None:
        math_ok = True
        math_diff = 0.0
    else:
        math_diff = round(data.invoice_line_subtotal - expected_line_subtotal, 2)
        math_ok = abs(math_diff) <= LINE_TOTAL_TOLERANCE_INR

    qty_diff = data.invoice_qty - data.grn_received_qty
    expected_grn_total = data.grn_received_qty * data.po_unit_price
    billed_total = (
        data.invoice_line_subtotal
        if data.invoice_line_subtotal is not None
        else expected_line_subtotal
    )
    total_price_impact = round(billed_total - expected_grn_total, 2)

    reasons: list[str] = []
    if not qty_ok:
        reasons.append(
            f"Quantity mismatch: PO={data.po_qty}, GRN={data.grn_received_qty}, "
            f"Invoice={data.invoice_qty}"
        )
    if not price_ok:
        sign = "+" if price_diff > 0 else ""
        reasons.append(
            f"Price mismatch: Invoice ₹{data.invoice_unit_price:,.2f}/unit vs "
            f"PO ₹{data.po_unit_price:,.2f}/unit ({sign}₹{price_diff:,.2f}/unit)"
        )
    if not math_ok:
        reasons.append(
            f"Line total arithmetic mismatch: {data.invoice_qty} x "
            f"₹{data.invoice_unit_price:,.2f} = ₹{expected_line_subtotal:,.2f}, "
            f"but invoice subtotal states ₹{data.invoice_line_subtotal:,.2f}"
        )

    if qty_ok and price_ok and math_ok:
        status = InvoiceMatchStatus.MATCHED
        summary = "3-way match passed (PO, GRN, and Invoice agree within tolerance)."
    elif not qty_ok and (not price_ok or not math_ok):
        status = InvoiceMatchStatus.MISMATCH_BOTH
        summary = "; ".join(reasons)
    elif not qty_ok:
        status = InvoiceMatchStatus.MISMATCH_QTY
        summary = "; ".join(reasons)
    else:
        status = InvoiceMatchStatus.MISMATCH_PRICE
        summary = "; ".join(reasons)

    return ThreeWayMatchResult(
        matched=(status == InvoiceMatchStatus.MATCHED),
        match_status=status,
        qty_ok=qty_ok,
        price_ok=price_ok,
        math_ok=math_ok,
        price_diff_per_unit=round(price_diff, 2),
        total_price_impact=total_price_impact,
        qty_diff=qty_diff,
        discrepancy_summary=summary,
    )

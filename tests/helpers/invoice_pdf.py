from dataclasses import dataclass

from fpdf import FPDF


def _latin1_safe(text: str) -> str:
    """Normalize common Unicode punctuation to latin-1 safe equivalents for Helvetica FPDF."""
    replacements = {
        "\u2014": "-",
        "\u2013": "-",
        "\u2018": "'",
        "\u2019": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u20b9": "INR ",
    }
    out = text
    for src, dst in replacements.items():
        out = out.replace(src, dst)
    return out.encode("latin-1", errors="replace").decode("latin-1")


@dataclass(frozen=True)
class InvoiceLineSpec:
    sku: str
    description: str
    qty: int
    unit_price: float
    line_total: float | None = None


def build_invoice_pdf_bytes(
    *,
    supplier_name: str,
    invoice_number: str,
    po_number: str | None,
    invoice_date: str,
    lines: list[InvoiceLineSpec],
    subtotal_override: float | None = None,
    gst_rate: float = 0.18,
    currency: str = "INR",
    notes: str | None = None,
) -> bytes:
    """Generate a realistic commercial supplier invoice PDF using fpdf2."""
    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()

    # Header
    pdf.set_font("Helvetica", "B", 16)
    pdf.cell(0, 10, _latin1_safe(supplier_name), new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(0, 6, "TAX INVOICE (ORIGINAL FOR RECIPIENT)", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)

    # Invoice metadata block
    pdf.set_font("Helvetica", "B", 10)
    pdf.cell(45, 7, "Invoice Number:")
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(60, 7, _latin1_safe(invoice_number))
    pdf.set_font("Helvetica", "B", 10)
    pdf.cell(35, 7, "Invoice Date:")
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(0, 7, _latin1_safe(invoice_date), new_x="LMARGIN", new_y="NEXT")

    pdf.set_font("Helvetica", "B", 10)
    pdf.cell(45, 7, "Purchase Order Ref:")
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(60, 7, _latin1_safe(po_number or "N/A"))
    pdf.set_font("Helvetica", "B", 10)
    pdf.cell(35, 7, "Currency:")
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(0, 7, _latin1_safe(currency), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(6)

    # Table Header
    pdf.set_font("Helvetica", "B", 10)
    pdf.set_fill_color(235, 238, 242)
    pdf.cell(28, 8, "SKU", border=1, fill=True)
    pdf.cell(72, 8, "Description", border=1, fill=True)
    pdf.cell(20, 8, "Qty", border=1, align="R", fill=True)
    pdf.cell(34, 8, f"Unit Price ({currency})", border=1, align="R", fill=True)
    pdf.cell(
        36,
        8,
        f"Line Total ({currency})",
        border=1,
        align="R",
        fill=True,
        new_x="LMARGIN",
        new_y="NEXT",
    )

    # Table Rows
    pdf.set_font("Helvetica", "", 10)
    computed_subtotal = 0.0
    for line in lines:
        lt = (
            line.line_total
            if line.line_total is not None
            else round(line.qty * line.unit_price, 2)
        )
        computed_subtotal += lt
        pdf.cell(28, 8, _latin1_safe(line.sku), border=1)
        pdf.cell(72, 8, _latin1_safe(line.description)[:36], border=1)
        pdf.cell(20, 8, str(line.qty), border=1, align="R")
        pdf.cell(34, 8, f"{line.unit_price:,.2f}", border=1, align="R")
        pdf.cell(36, 8, f"{lt:,.2f}", border=1, align="R", new_x="LMARGIN", new_y="NEXT")

    subtotal = round(subtotal_override if subtotal_override is not None else computed_subtotal, 2)
    tax_amount = round(subtotal * gst_rate, 2)
    grand_total = round(subtotal + tax_amount, 2)

    pdf.ln(4)
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(120, 7, "")
    pdf.cell(34, 7, "Subtotal:", align="R")
    pdf.cell(36, 7, f"{currency} {subtotal:,.2f}", align="R", new_x="LMARGIN", new_y="NEXT")

    pdf.cell(120, 7, "")
    pdf.cell(34, 7, f"GST ({int(round(gst_rate * 100))}%):", align="R")
    pdf.cell(36, 7, f"{currency} {tax_amount:,.2f}", align="R", new_x="LMARGIN", new_y="NEXT")

    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(120, 8, "")
    pdf.cell(34, 8, "Grand Total:", align="R")
    pdf.cell(36, 8, f"{currency} {grand_total:,.2f}", align="R", new_x="LMARGIN", new_y="NEXT")

    if notes:
        pdf.ln(6)
        pdf.set_font("Helvetica", "I", 9)
        pdf.multi_cell(0, 5, f"Notes: {_latin1_safe(notes)}")

    return bytes(pdf.output())

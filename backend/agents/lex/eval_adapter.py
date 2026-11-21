from typing import Any, TypeVar

from fpdf import FPDF
from pydantic import BaseModel
from sqlalchemy.orm import Session

from backend.agents.lex.agent import LexAgent
from backend.agents.lex.schemas import (
    EmailDraftInput,
    EmailTriageInput,
    StockRequestTriageInput,
)
from backend.agents.llm_client import GeminiLLMClient, LLMClientProtocol, LLMResult
from backend.core.config import settings

T = TypeVar("T", bound=BaseModel)


class OfflineCaseLLMClient:
    """Deterministic LLM stub validating YAML mock_response against the target schema."""

    def __init__(self, mock_response: dict[str, Any]) -> None:
        self._mock_response = mock_response

    def generate_structured(
        self,
        *,
        prompt: str,
        response_schema: type[T],
        system_instruction: str | None = None,
        temperature: float = 0.0,
        pdf_bytes: bytes | None = None,
    ) -> LLMResult[T]:
        parsed = response_schema.model_validate(self._mock_response)
        return LLMResult(
            parsed=parsed,
            raw_text=parsed.model_dump_json(),
            input_tokens=320,
            output_tokens=96,
            latency_ms=8,
            cost_inr=0.0044,
            model_name=f"{settings.gemini_model} (offline)",
        )


def _render_invoice_pdf_from_spec(pdf_spec: dict[str, Any]) -> bytes:
    """Build a realistic commercial invoice PDF from declarative YAML spec using fpdf2."""
    supplier_name = str(pdf_spec.get("supplier_name", "Supplier"))
    invoice_number = str(pdf_spec.get("invoice_number", ""))
    po_number = pdf_spec.get("po_number")
    invoice_date = str(pdf_spec.get("invoice_date", "2026-11-20"))
    currency = str(pdf_spec.get("currency", "INR"))
    gst_rate = float(pdf_spec.get("gst_rate", 0.18))
    lines = list(pdf_spec.get("lines", []))
    subtotal_override = pdf_spec.get("subtotal_override")
    notes = pdf_spec.get("notes")

    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()

    pdf.set_font("Helvetica", "B", 16)
    pdf.cell(0, 10, supplier_name, new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(0, 6, "TAX INVOICE (ORIGINAL FOR RECIPIENT)", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)

    pdf.set_font("Helvetica", "B", 10)
    pdf.cell(45, 7, "Invoice Number:")
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(60, 7, invoice_number or "N/A")
    pdf.set_font("Helvetica", "B", 10)
    pdf.cell(35, 7, "Invoice Date:")
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(0, 7, invoice_date, new_x="LMARGIN", new_y="NEXT")

    pdf.set_font("Helvetica", "B", 10)
    pdf.cell(45, 7, "Purchase Order Ref:")
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(60, 7, str(po_number) if po_number else "N/A")
    pdf.set_font("Helvetica", "B", 10)
    pdf.cell(35, 7, "Currency:")
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(0, 7, currency, new_x="LMARGIN", new_y="NEXT")
    pdf.ln(6)

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

    pdf.set_font("Helvetica", "", 10)
    computed_subtotal = 0.0
    for line in lines:
        qty = int(line["qty"])
        unit_price = float(line["unit_price"])
        lt = float(line["line_total"]) if line.get("line_total") is not None else round(
            qty * unit_price, 2
        )
        computed_subtotal += lt
        pdf.cell(28, 8, str(line.get("sku", "")), border=1)
        pdf.cell(72, 8, str(line.get("description", ""))[:36], border=1)
        pdf.cell(20, 8, str(qty), border=1, align="R")
        pdf.cell(34, 8, f"{unit_price:,.2f}", border=1, align="R")
        pdf.cell(36, 8, f"{lt:,.2f}", border=1, align="R", new_x="LMARGIN", new_y="NEXT")

    subtotal = round(
        float(subtotal_override) if subtotal_override is not None else computed_subtotal, 2
    )
    tax_amount = round(subtotal * gst_rate, 2)
    grand_total = round(subtotal + tax_amount, 2)

    pdf.ln(4)
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
        pdf.multi_cell(0, 5, f"Notes: {notes}")

    return bytes(pdf.output())


def _render_plain_text_pdf(text: str) -> bytes:
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", "", 11)
    pdf.multi_cell(0, 7, text)
    return bytes(pdf.output())


class LexEvalExecutor:
    """Executes Lex YAML eval cases through real LexAgent methods & guardrails."""

    def execute_eval_case(
        self,
        session: Session,
        *,
        task: str,
        case_input: dict[str, Any],
        mode: str,
        mock_response: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        llm_client: LLMClientProtocol
        if mode == "offline":
            if mock_response is None:
                raise ValueError(f"Offline eval case for task '{task}' is missing mock_response.")
            llm_client = OfflineCaseLLMClient(mock_response)
        elif mode == "live":
            llm_client = GeminiLLMClient(model_name=settings.gemini_model)
        else:
            raise ValueError(f"Unsupported eval mode: {mode}")

        lex = LexAgent(llm_client=llm_client)

        if task == "email_triage":
            triage_input = EmailTriageInput.model_validate(case_input)
            return lex.triage_email(session, triage_input).model_dump(mode="json")

        if task == "invoice_extraction":
            if "pdf_spec" in case_input:
                pdf_bytes = _render_invoice_pdf_from_spec(case_input["pdf_spec"])
            elif "raw_text_pdf" in case_input:
                pdf_bytes = _render_plain_text_pdf(str(case_input["raw_text_pdf"]))
            else:
                raise ValueError(
                    "invoice_extraction case input requires 'pdf_spec' or 'raw_text_pdf'."
                )
            filename = str(case_input.get("filename", "eval_invoice.pdf"))
            supplier_name = case_input.get("supplier_name")
            return lex.extract_invoice(
                session,
                pdf_bytes,
                filename=filename,
                supplier_name=str(supplier_name) if supplier_name is not None else None,
            ).model_dump(mode="json")

        if task == "email_draft":
            draft_input = EmailDraftInput.model_validate(case_input)
            return lex.draft_email(session, draft_input).model_dump(mode="json")

        if task == "stock_request_triage":
            sr_input = StockRequestTriageInput.model_validate(case_input)
            return lex.triage_stock_request(session, sr_input).model_dump(mode="json")

        raise ValueError(f"Unsupported Lex eval task '{task}'.")

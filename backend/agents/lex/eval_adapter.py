from collections.abc import Callable
from typing import Any, TypeVar

from fpdf import FPDF
from pydantic import BaseModel
from sqlalchemy.orm import Session

from backend.agents.lex.agent import LexAgent
from backend.agents.lex.schemas import (
    EmailDraftInput,
    EmailTriageInput,
    ReminderWakeupInput,
    StockRequestTriageInput,
)
from backend.agents.llm_client import GeminiLLMClient, LLMClientProtocol, LLMResult
from backend.agents.sandbox import run_sandbox_python
from backend.agents.tools import ToolRegistry
from backend.core.config import settings
from backend.observability.tracer import list_traces

T = TypeVar("T", bound=BaseModel)


class OfflineCaseLLMClient:
    """Deterministic LLM stub validating YAML mock_response against the target schema."""

    def __init__(self, mock_response: dict[str, Any]) -> None:
        self._mock_response = dict(mock_response)

    def _clean_payload(self) -> dict[str, Any]:
        cleaned = dict(self._mock_response)
        cleaned.pop("mock_tool_calls", None)
        return cleaned

    def generate_structured(
        self,
        *,
        prompt: str,
        response_schema: type[T],
        system_instruction: str | None = None,
        temperature: float = 0.0,
        pdf_bytes: bytes | None = None,
        image_attachments: list[tuple[str, bytes]] | None = None,
    ) -> LLMResult[T]:
        del prompt, system_instruction, temperature, pdf_bytes, image_attachments
        parsed = response_schema.model_validate(self._clean_payload())
        return LLMResult(
            parsed=parsed,
            raw_text=parsed.model_dump_json(),
            input_tokens=320,
            output_tokens=96,
            latency_ms=8,
            cost_inr=0.0044,
            model_name=f"{settings.gemini_model} (offline)",
        )

    def generate_with_tools(
        self,
        *,
        prompt: str,
        response_schema: type[T],
        tool_registry: ToolRegistry,
        system_instruction: str | None = None,
        temperature: float = 0.0,
        pdf_bytes: bytes | None = None,
        image_attachments: list[tuple[str, bytes]] | None = None,
        max_steps: int = 8,
    ) -> LLMResult[T]:
        del prompt, system_instruction, temperature, pdf_bytes, image_attachments, max_steps
        tool_call_logs: list[dict[str, Any]] = []
        for entry in list(self._mock_response.get("mock_tool_calls") or []):
            if not isinstance(entry, dict):
                continue
            tool_name = str(entry.get("tool", ""))
            tool_args = dict(entry.get("args") or {})
            tool_res = tool_registry.execute(tool_name, tool_args)
            tool_call_logs.append(ToolRegistry.format_trace_record(tool_res, tool_args))

        parsed = response_schema.model_validate(self._clean_payload())
        return LLMResult(
            parsed=parsed,
            raw_text=parsed.model_dump_json(),
            input_tokens=320 + 60 * len(tool_call_logs),
            output_tokens=96 + 30 * len(tool_call_logs),
            latency_ms=8 + sum(int(t.get("duration_ms", 0)) for t in tool_call_logs),
            cost_inr=0.0044,
            model_name=f"{settings.gemini_model} (offline)",
            tool_calls=tool_call_logs,
        )


def _build_standalone_eval_registry(case_input: dict[str, Any]) -> ToolRegistry:
    """Domain-isolated eval ToolRegistry supporting sandbox calc + case_input lookups."""
    registry = ToolRegistry()

    def _run_sandbox_calc(
        python_script: str,
        variables: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        res = run_sandbox_python(python_script, variables or {})
        return res.model_dump(mode="json")

    def _get_po_details(po_number_or_id: str | int) -> dict[str, Any]:
        needle = str(po_number_or_id).strip().lower()
        for po in case_input.get("open_pos") or []:
            if str(po.get("po_number", "")).strip().lower() == needle:
                return {"found": True, **po}
        if str(case_input.get("po_number", "")).strip().lower() == needle:
            return {
                "found": True,
                "po_number": case_input.get("po_number"),
                "status": case_input.get("po_status"),
                "grn_count": case_input.get("grn_count", 0),
                "missing_dispatch_fields": case_input.get("missing_dispatch_fields", []),
            }
        return {"found": False, "query": str(po_number_or_id)}

    def _get_part_snapshot(sku_or_part_id: str | int) -> dict[str, Any]:
        snap = case_input.get("part_snapshot")
        if isinstance(snap, dict):
            return {"found": True, **snap}
        return {
            "found": True,
            "sku": str(sku_or_part_id),
            "days_to_stockout": case_input.get("days_to_stockout"),
        }

    def _get_supplier_profile(supplier_key_or_id: str | int) -> dict[str, Any]:
        return {
            "found": True,
            "supplier_key": str(supplier_key_or_id),
            "name": case_input.get("sender_name") or case_input.get("supplier_name"),
            "memory_notes": list(case_input.get("supplier_memory_notes") or []),
        }

    def _get_mailroom_thread(thread_id: str) -> dict[str, Any]:
        return {
            "thread_id": thread_id,
            "messages": list(case_input.get("thread_history") or []),
        }

    def _list_open_pos(
        supplier_id: int | str | None = None,
        part_id: int | str | None = None,
    ) -> dict[str, Any]:
        del supplier_id, part_id
        open_pos = list(case_input.get("open_pos") or [])
        return {"count": len(open_pos), "open_pos": open_pos}

    registry.register_function(
        name="list_open_pos",
        description="List currently open PurchaseOrders.",
        parameters_schema={"type": "object", "properties": {}},
        handler=_list_open_pos,
    )
    registry.register_function(
        name="run_sandbox_calc",
        description="Execute AST-sandboxed Python mathematical or date calculations.",
        parameters_schema={
            "type": "object",
            "properties": {
                "python_script": {"type": "string"},
                "variables": {"type": "object"},
            },
            "required": ["python_script"],
        },
        handler=_run_sandbox_calc,
    )
    registry.register_function(
        name="get_po_details",
        description="Retrieve open PO details by PO number or ID.",
        parameters_schema={
            "type": "object",
            "properties": {"po_number_or_id": {"type": "string"}},
            "required": ["po_number_or_id"],
        },
        handler=_get_po_details,
    )
    registry.register_function(
        name="get_part_snapshot",
        description="Retrieve current stock balance and stockout runway for a SKU or part ID.",
        parameters_schema={
            "type": "object",
            "properties": {"sku_or_part_id": {"type": "string"}},
            "required": ["sku_or_part_id"],
        },
        handler=_get_part_snapshot,
    )
    registry.register_function(
        name="get_supplier_profile",
        description="Retrieve supplier metrics and existing SupplierMemoryNotes.",
        parameters_schema={
            "type": "object",
            "properties": {"supplier_key_or_id": {"type": "string"}},
            "required": ["supplier_key_or_id"],
        },
        handler=_get_supplier_profile,
    )
    registry.register_function(
        name="get_mailroom_thread",
        description="Retrieve messages in a mailroom thread.",
        parameters_schema={
            "type": "object",
            "properties": {"thread_id": {"type": "string"}},
            "required": ["thread_id"],
        },
        handler=_get_mailroom_thread,
    )
    return registry


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

    def __init__(
        self,
        *,
        tool_registry_factory: Callable[[Session], ToolRegistry] | None = None,
    ) -> None:
        self.tool_registry_factory = tool_registry_factory

    def _resolve_registry(self, session: Session, case_input: dict[str, Any]) -> ToolRegistry:
        # Synthetic YAML cases supply isolated open_pos / part_snapshot / po_status inputs;
        # bind tools to those synthetic fixtures so live runs don't collide with seeded POs.
        if any(k in case_input for k in ("open_pos", "part_snapshot", "po_status")):
            return _build_standalone_eval_registry(case_input)
        if self.tool_registry_factory is not None:
            return self.tool_registry_factory(session)
        return _build_standalone_eval_registry(case_input)

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
        tool_registry = self._resolve_registry(session, case_input)

        if task == "email_triage":
            triage_input = EmailTriageInput.model_validate(case_input)
            out = lex.triage_email(
                session,
                triage_input,
                tool_registry=tool_registry,
            ).model_dump(mode="json")
            latest = list_traces(session, agent_name=lex.agent_name, task_type=task, limit=1)
            if latest:
                tcalls = list(latest[0].tool_calls_json or [])
                out["tool_calls"] = tcalls
                out["tool_names"] = [str(tc.get("tool", "")) for tc in tcalls]
            return out

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
            out = lex.draft_email(
                session,
                draft_input,
                tool_registry=tool_registry,
            ).model_dump(mode="json")
            latest = list_traces(session, agent_name=lex.agent_name, task_type=task, limit=1)
            if latest:
                tcalls = list(latest[0].tool_calls_json or [])
                out["tool_calls"] = tcalls
                out["tool_names"] = [str(tc.get("tool", "")) for tc in tcalls]
            return out

        if task == "stock_request_triage":
            sr_input = StockRequestTriageInput.model_validate(case_input)
            return lex.triage_stock_request(session, sr_input).model_dump(mode="json")

        if task == "reminder_wakeup":
            wakeup_input = ReminderWakeupInput.model_validate(case_input)
            out = lex.inspect_reminder_wakeup(
                session,
                wakeup_input,
                tool_registry=tool_registry,
            ).model_dump(mode="json")
            latest = list_traces(session, agent_name=lex.agent_name, task_type=task, limit=1)
            if latest:
                tcalls = list(latest[0].tool_calls_json or [])
                out["tool_calls"] = tcalls
                out["tool_names"] = [str(tc.get("tool", "")) for tc in tcalls]
            return out

        raise ValueError(f"Unsupported Lex eval task '{task}'.")

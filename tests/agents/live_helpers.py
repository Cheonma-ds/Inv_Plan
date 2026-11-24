import io
import os
from datetime import timedelta
from typing import Any

from PIL import Image, ImageDraw, ImageFont
from pydantic import BaseModel, Field
from sqlalchemy import select

from backend.agents.lex import LexAgent
from backend.agents.llm_client import GeminiLLMClient
from backend.core import clock
from backend.core.config import settings
from backend.core.enums import PersonaKey, POStatus
from backend.core.models import Part, PurchaseOrder, Supplier
from backend.po.service import create_po, transition_po


def should_run_live_gemini() -> bool:
    has_key = bool(os.environ.get("GEMINI_API_KEY") or settings.gemini_api_key)
    enabled = os.environ.get("RUN_LIVE_GEMINI", "").strip().lower() in {"1", "true", "yes"}
    return has_key and enabled


LIVE_SKIP_REASON = "Set RUN_LIVE_GEMINI=1 (with GEMINI_API_KEY) to run live Gemini Lex suite"


def make_live_lex() -> LexAgent:
    return LexAgent(llm_client=GeminiLLMClient(model_name=settings.gemini_model))


def create_open_po(
    session,
    *,
    sku: str = "BRG-402",
    persona: PersonaKey = PersonaKey.SUP1,
    status: POStatus = POStatus.SENT_AWAITING_ACK,
    qty: int = 20,
    unit_price: float = 450.0,
    eta_days: int = 12,
) -> PurchaseOrder:
    sim_now = clock.now(session)
    part = session.scalar(select(Part).where(Part.sku == sku))
    sup = session.scalar(select(Supplier).where(Supplier.persona_key == persona.value))
    assert part is not None and sup is not None
    po = create_po(
        session,
        part_id=part.id,
        supplier_id=sup.id,
        qty=qty,
        unit_price=unit_price,
        initial_status=POStatus.PENDING_DISPATCH_APPROVAL,
    )
    transition_po(
        session,
        po,
        POStatus.SENT_AWAITING_ACK,
        promised_eta=sim_now + timedelta(days=eta_days),
    )
    if status == POStatus.ACKNOWLEDGED:
        transition_po(
            session,
            po,
            POStatus.ACKNOWLEDGED,
            promised_eta=sim_now + timedelta(days=eta_days),
        )
    session.commit()
    return po


def build_dispatch_slip_png(
    *,
    po_number: str,
    sku: str = "BRG-402",
    sku_name: str = "Deep Groove Ball Bearing 6208",
    qty: int = 15,
    carrier: str = "BlueDart Express",
    tracking: str = "BD-492817",
    dispatch_date: str = "2026-10-03",
    eta_date: str = "2026-10-12",
) -> bytes:
    """Render a crisp PNG dispatch advice slip containing logistics details."""
    img = Image.new("RGB", (920, 420), color=(255, 255, 255))
    draw = ImageDraw.Draw(img)
    font = ImageFont.load_default(size=26)
    small_font = ImageFont.load_default(size=24)

    draw.rectangle([(20, 20), (900, 400)], outline=(30, 41, 59), width=3)
    draw.text((44, 42), "SUPPLIER DISPATCH ADVICE", fill=(15, 23, 42), font=font)
    draw.line([(44, 88), (876, 88)], fill=(148, 163, 184), width=2)

    lines = [
        f"Purchase Order Ref: {po_number}",
        f"Part SKU: {sku} ({sku_name})",
        f"Confirmed Quantity: {qty} units",
        f"Carrier / Courier: {carrier}",
        f"Tracking Number (AWB): {tracking}",
        f"Promised Dispatch Date: {dispatch_date}",
        f"Estimated Delivery Date (ETA): {eta_date}",
    ]
    y = 112
    for line in lines:
        draw.text((44, y), line, fill=(15, 23, 42), font=small_font)
        y += 38

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def build_damage_inspection_png(
    *,
    po_number: str,
    sku: str,
    good_qty: int,
    damaged_qty: int,
) -> bytes:
    """Render a warehouse/transit inspection slip photo showing damaged carton details."""
    img = Image.new("RGB", (920, 420), color=(254, 249, 240))
    draw = ImageDraw.Draw(img)
    font = ImageFont.load_default(size=26)
    small_font = ImageFont.load_default(size=23)

    draw.rectangle([(20, 20), (900, 400)], outline=(180, 40, 40), width=4)
    draw.text((44, 42), "DOCK RECEIVING DAMAGE REPORT", fill=(160, 25, 25), font=font)
    draw.line([(44, 88), (876, 88)], fill=(200, 120, 120), width=2)

    lines = [
        f"PO Reference: {po_number} | SKU: {sku}",
        "Condition: OUTER CARTON CRUSHED & WATER-STAINED",
        f"Usable / Intact Units: {good_qty} units",
        f"Damaged / Unusable Units: {damaged_qty} units",
        "Carrier: SafeExpress | Docket: SE-88310",
        "Action: Hold damaged units in quarantine pending claim",
    ]
    y = 115
    for line in lines:
        draw.text((44, y), line, fill=(30, 30, 35), font=small_font)
        y += 40

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


class CopilotEvalResponse(BaseModel):
    """Structured response contract used to evaluate Lex on Manager Copilot queries."""

    answer: str
    cited_skus: list[str] = Field(default_factory=list)
    cited_po_numbers: list[str] = Field(default_factory=list)
    cited_metrics: dict[str, Any] = Field(default_factory=dict)
    recommended_actions: list[str] = Field(default_factory=list)
    cannot_verify_or_missing_tools: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)


COPILOT_EVAL_SYSTEM_PROMPT = """\
You are Lex, the InvPlan AI Supply Chain Copilot assisting the Inventory Manager.
Answer complex operational, analytical, and terminology questions accurately using ONLY
verified data retrieved from your tools and calculations run via `run_sandbox_calc`.

Rules:
1. Always inspect live database state using your tools before quoting numbers, SKUs,
   PO statuses, stockout runways, supplier scores, or alerts.
2. Use `run_sandbox_calc` for multi-step math (post-withdrawal runways, comparisons, diffs).
3. Never mutate business records, approve drafts, or fabricate numbers not backed by tools.
   If a required lookup is unavailable via your current tools, state that clearly in
   `cannot_verify_or_missing_tools` and `answer` instead of guessing.
4. Know InvPlan domain definitions accurately:
   - Daily Burn: 90-day trailing completed-IST-day EWMA (alpha = 0.30); same-day withdrawals
     reduce On Hand and Stockout In immediately while Daily Burn stays frozen until rollover.
   - On Hand: usable warehouse stock (excludes On Hold / quarantined stock).
   - On Hold: quarantined units held due to 3-way invoice mismatch or damage.
   - On Order: remaining qty across dispatched+ active POs (PENDING_DISPATCH_APPROVAL = 0).
   - Inventory Position: On Hand + On Hold + On Order.
   - ROP/T: Reorder Point (ROP) and Order-Up-To Target level (T), using service-level Z-factors
     (CRITICAL=2.05, STANDARD=1.65, LOW=1.28), safety stock, and effective lead time.
   - Stockout In urgency: compares days_to_stockout against primary effective LT +
     comm_buffer_days + safety_days and earliest dispatched PO ETA.
"""

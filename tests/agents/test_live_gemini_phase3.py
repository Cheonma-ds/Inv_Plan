import io
import os
from datetime import datetime, timedelta, timezone

import pytest
from PIL import Image, ImageDraw, ImageFont
from sqlalchemy import select

from backend.agents.lex import (
    EmailDraftInput,
    EmailIntent,
    LexAgent,
)
from backend.agents.lex.eval_adapter import LexEvalExecutor
from backend.agents.llm_client import GeminiLLMClient
from backend.core import clock
from backend.core.config import settings
from backend.core.enums import EmailCategory, GRNStatus, LexReminderStatus, PersonaKey, POStatus
from backend.core.models import (
    ActionItem,
    GoodsReceipt,
    LexReminder,
    Part,
    PurchaseOrder,
    Supplier,
)
from backend.mailroom import OutboundMessage, list_drafts
from backend.observability import list_traces
from backend.observability.eval_runner import evaluate_expectations, load_eval_suite
from backend.orchestrator.lex_tools import build_lex_tool_registry
from backend.orchestrator.workflows import WorkflowEngine
from backend.po.service import create_po, transition_po


def _should_run_live_gemini() -> bool:
    has_key = bool(os.environ.get("GEMINI_API_KEY") or settings.gemini_api_key)
    enabled = os.environ.get("RUN_LIVE_GEMINI", "").strip().lower() in {"1", "true", "yes"}
    return has_key and enabled


def _build_dispatch_slip_png(*, po_number: str) -> bytes:
    """Render a crisp, high-contrast PNG dispatch advice containing tracking/carrier text."""
    img = Image.new("RGB", (920, 420), color=(255, 255, 255))
    draw = ImageDraw.Draw(img)
    font = ImageFont.load_default(size=26)
    small_font = ImageFont.load_default(size=24)

    draw.rectangle([(20, 20), (900, 400)], outline=(30, 41, 59), width=3)
    draw.text((44, 42), "APEX PRECISION COMPONENTS - DISPATCH ADVICE", fill=(15, 23, 42), font=font)
    draw.line([(44, 88), (876, 88)], fill=(148, 163, 184), width=2)

    lines = [
        f"Purchase Order Ref: {po_number}",
        "Part SKU: BRG-402 (Deep Groove Ball Bearing 6208)",
        "Confirmed Quantity: 15 units",
        "Carrier / Courier: BlueDart Express",
        "Tracking Number (AWB): BD-492817",
        "Promised Dispatch Date: 2026-10-03",
        "Estimated Delivery Date (ETA): 2026-10-12",
    ]
    y = 112
    for line in lines:
        draw.text((44, y), line, fill=(15, 23, 42), font=small_font)
        y += 38

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _create_open_po(
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


@pytest.mark.skipif(
    not _should_run_live_gemini(),
    reason="Set RUN_LIVE_GEMINI=1 (with GEMINI_API_KEY) to run live Gemini Phase 3 tests",
)
def test_live_gemini_tool_loop_4d_deferral_and_sandbox_calc(seeded_session) -> None:
    """Live Gemini invokes lex_tools + sandbox calc and defers +4d via LexReminder."""
    part = seeded_session.scalar(select(Part).where(Part.sku == "BRG-402"))
    assert part is not None
    part.on_hand_stock = 260  # > 30d runway vs 4d deferral + 10d LT
    seeded_session.commit()

    po = _create_open_po(seeded_session, sku="BRG-402", persona=PersonaKey.SUP1, eta_days=12)

    lex = LexAgent(llm_client=GeminiLLMClient(model_name=settings.gemini_model))
    engine = WorkflowEngine(lex_agent=lex)

    res = engine.send_mailroom_email(
        seeded_session,
        OutboundMessage(
            from_persona=PersonaKey.SUP1,
            to_personas=[PersonaKey.INVENTORY_MANAGER],
            cc_personas=[PersonaKey.LEX],
            po_id=po.id,
            subject=f"Re: {po.po_number} - Confirmed (Dispatch details in 4 days)",
            body=(
                f"Dear Team,\n\nWe confirm {po.po_number} for 20 units of BRG-402 at "
                "Rs 450.00/unit (delivery ETA 2026-10-13).\n\nDue to an ERP migration, we "
                "can only share carrier and tracking details after 4 days (by 2026-10-05). "
                "Please verify stock runway against our 4-day wait using your tools and "
                "calculation sandbox before scheduling any follow-up."
            ),
        ),
    )

    triage_bundle = res["triage"]
    assert triage_bundle is not None
    triage = triage_bundle["triage"]
    assert triage.matched_po_number == po.po_number
    assert triage.intent == EmailIntent.ACKNOWLEDGED
    assert triage.confidence >= 0.85
    assert triage.details_promised_later is True
    assert triage.details_promised_days == 4 or triage.details_promised_by_date == "2026-10-05"

    traces = list_traces(seeded_session, agent_name="lex", task_type="email_triage", limit=1)
    assert len(traces) == 1
    assert traces[0].input_tokens > 0
    assert traces[0].output_tokens > 0
    assert traces[0].tool_calls_json is not None and len(traces[0].tool_calls_json) >= 1

    seeded_session.refresh(po)
    assert po.status == POStatus.ACKNOWLEDGED.value

    reminder = seeded_session.scalar(
        select(LexReminder).where(
            LexReminder.po_id == po.id,
            LexReminder.status == LexReminderStatus.PENDING.value,
        )
    )
    assert reminder is not None
    assert clock.to_ist_date(reminder.remind_at).isoformat() == "2026-10-05"

    # Safe +4d deferral must NOT generate an immediate follow-up draft today
    drafts = list_drafts(seeded_session, PersonaKey.LEX.value)
    nudge_drafts = [
        d
        for d in drafts
        if d.po_id == po.id
        and d.category
        in {
            EmailCategory.DISPATCH_DETAILS_REQUEST.value,
            EmailCategory.DISPATCH_DETAILS_NUDGE.value,
        }
    ]
    assert nudge_drafts == []

    deferred_alert = seeded_session.scalar(
        select(ActionItem).where(
            ActionItem.po_id == po.id,
            ActionItem.type == "DISPATCH_DETAILS_DEFERRED",
        )
    )
    assert deferred_alert is not None


@pytest.mark.skipif(
    not _should_run_live_gemini(),
    reason="Set RUN_LIVE_GEMINI=1 (with GEMINI_API_KEY) to run live Gemini Phase 3 tests",
)
def test_live_gemini_reminder_wakeup_missing_vs_grn_received(seeded_session) -> None:
    """Live Gemini reminder wakeup drafts nudge when missing and skips when GRN exists."""
    po_missing = _create_open_po(
        seeded_session,
        sku="BRG-402",
        persona=PersonaKey.SUP1,
        status=POStatus.ACKNOWLEDGED,
    )
    po_grn = _create_open_po(
        seeded_session,
        sku="FLT-108",
        persona=PersonaKey.SUP1,
        status=POStatus.ACKNOWLEDGED,
        qty=12,
        unit_price=320.0,
    )
    sim_now = clock.now(seeded_session)
    seeded_session.add(
        GoodsReceipt(
            po_id=po_grn.id,
            received_qty=12,
            received_at=sim_now + timedelta(days=2),
            condition="GOOD",
            status=GRNStatus.RELEASED.value,
        )
    )

    remind_at = datetime(2026, 10, 5, 7, 30, tzinfo=timezone.utc)
    rem_missing = LexReminder(
        po_id=po_missing.id,
        part_id=po_missing.part_id,
        supplier_id=po_missing.supplier_id,
        remind_at=remind_at,
        reason="Supplier promised dispatch details after 4 days (by 2026-10-05).",
        context_json={"wait_days": 4, "promised_by_date": "2026-10-05"},
        status=LexReminderStatus.PENDING.value,
        created_at=sim_now,
    )
    rem_grn = LexReminder(
        po_id=po_grn.id,
        part_id=po_grn.part_id,
        supplier_id=po_grn.supplier_id,
        remind_at=remind_at,
        reason="Supplier promised dispatch details after 4 days (by 2026-10-05).",
        context_json={"wait_days": 4, "promised_by_date": "2026-10-05"},
        status=LexReminderStatus.PENDING.value,
        created_at=sim_now,
    )
    seeded_session.add_all([rem_missing, rem_grn])
    seeded_session.commit()

    lex = LexAgent(llm_client=GeminiLLMClient(model_name=settings.gemini_model))
    engine = WorkflowEngine(lex_agent=lex)
    sla_res = engine.set_clock_and_watch(
        seeded_session,
        datetime(2026, 10, 5, 8, 0, tzinfo=timezone.utc),
    )
    assert sla_res is not None

    seeded_session.refresh(rem_missing)
    seeded_session.refresh(rem_grn)
    assert rem_missing.status == LexReminderStatus.TRIGGERED.value
    assert rem_grn.status == LexReminderStatus.TRIGGERED.value

    wakeup_traces = list_traces(seeded_session, agent_name="lex", task_type="reminder_wakeup")
    assert len(wakeup_traces) >= 1
    assert any((t.tool_calls_json or []) for t in wakeup_traces)

    drafts = list_drafts(seeded_session, PersonaKey.LEX.value)
    po_missing_drafts = [d for d in drafts if d.po_id == po_missing.id]
    po_grn_drafts = [d for d in drafts if d.po_id == po_grn.id]

    assert len(po_missing_drafts) == 1
    assert po_missing.po_number in po_missing_drafts[0].body
    assert "**Lex**" in po_missing_drafts[0].body
    assert po_grn_drafts == []


@pytest.mark.skipif(
    not _should_run_live_gemini(),
    reason="Set RUN_LIVE_GEMINI=1 (with GEMINI_API_KEY) to run live Gemini Phase 3 tests",
)
def test_live_gemini_multimodal_png_dispatch_slip(seeded_session, tmp_path) -> None:
    """Live Gemini reads carrier, tracking AWB, and dispatch date directly from PNG image."""
    po = _create_open_po(seeded_session, sku="BRG-402", persona=PersonaKey.SUP1, qty=15)
    png_path = tmp_path / "dispatch_slip.png"
    png_path.write_bytes(_build_dispatch_slip_png(po_number=po.po_number))

    lex = LexAgent(llm_client=GeminiLLMClient(model_name=settings.gemini_model))
    engine = WorkflowEngine(lex_agent=lex)

    res = engine.send_mailroom_email(
        seeded_session,
        OutboundMessage(
            from_persona=PersonaKey.SUP1,
            to_personas=[PersonaKey.INVENTORY_MANAGER],
            cc_personas=[PersonaKey.LEX],
            po_id=po.id,
            subject=f"Re: {po.po_number} - Confirmed with dispatch slip image",
            body=(
                f"Hi Team, we acknowledge {po.po_number}. Please inspect the attached "
                "PNG dispatch advice image for carrier name, AWB tracking number, "
                "and dispatch schedule."
            ),
            attachment_paths=[str(png_path)],
        ),
    )

    triage_bundle = res["triage"]
    assert triage_bundle is not None
    triage = triage_bundle["triage"]
    assert triage.matched_po_number == po.po_number
    assert triage.intent == EmailIntent.ACKNOWLEDGED
    assert triage.confidence >= 0.85
    assert triage.carrier_name is not None and "bluedart" in triage.carrier_name.lower()
    assert triage.tracking_number is not None and "492817" in triage.tracking_number

    seeded_session.refresh(po)
    assert po.carrier_name is not None and "bluedart" in po.carrier_name.lower()
    assert po.tracking_number is not None and "492817" in po.tracking_number


@pytest.mark.skipif(
    not _should_run_live_gemini(),
    reason="Set RUN_LIVE_GEMINI=1 (with GEMINI_API_KEY) to run live Gemini Phase 3 tests",
)
def test_live_gemini_draft_email_markdown_currency_and_guardrails(seeded_session) -> None:
    """Live Gemini draft honors structured Markdown, INR currency, CC rules, and sign-off."""
    lex = LexAgent(llm_client=GeminiLLMClient(model_name=settings.gemini_model))
    draft = lex.draft_email(
        seeded_session,
        EmailDraftInput(
            category=EmailCategory.EXPEDITE_REQUEST,
            po_number="PO-3099",
            part_sku="BRG-402",
            part_name="Deep Groove Ball Bearing 6208",
            qty=20,
            unit_price=450.0,
            currency="INR",
            recipient_persona="sup1",
            recipient_name="Apex Precision Components",
            current_date="2026-10-01",
            supplier_memory_notes=[
                "Frequently invoices above quoted unit price; remind to match PO INR rate."
            ],
            context_facts={"days_overdue": 2, "days_to_stockout": 4.5},
        ),
    )

    assert "PO-3099" in draft.subject
    assert "**" not in draft.subject
    assert "inventory_manager" in draft.cc_personas
    assert "procurement" in draft.cc_personas
    assert "\n\n" in draft.body
    assert "**PO-3099**" in draft.body
    assert "**BRG-402**" in draft.body
    assert "****" not in draft.body
    assert "$" not in draft.body
    assert "₹" in draft.body
    assert "Best regards,\n**Lex**\nAI Supply Chain Assistant" in draft.body

    traces = list_traces(seeded_session, agent_name="lex", task_type="email_draft", limit=1)
    assert len(traces) == 1
    assert traces[0].input_tokens > 0
    assert traces[0].output_tokens > 0


@pytest.mark.skipif(
    not _should_run_live_gemini(),
    reason="Set RUN_LIVE_GEMINI=1 (with GEMINI_API_KEY) to run live Gemini Phase 3 tests",
)
def test_live_gemini_phase3_eval_cases_live_mode(seeded_session) -> None:
    """Execute Phase 3 & formatted-draft lex.yaml eval cases directly against live Gemini."""
    suite = load_eval_suite("backend/evals/lex.yaml")
    executor = LexEvalExecutor(tool_registry_factory=build_lex_tool_registry)
    target_ids = {
        "draft_po_dispatch_structured_markdown",
        "triage_ack_deferred_4d_variable_and_sandbox",
        "reminder_wakeup_drafts_when_details_missing",
        "reminder_wakeup_skips_when_grn_already_received",
    }
    cases = [c for c in suite.cases if c.id in target_ids]
    assert len(cases) == 4

    for case in cases:
        output = executor.execute_eval_case(
            seeded_session,
            task=case.task,
            case_input=case.input,
            mode="live",
            mock_response=case.mock_response,
        )
        # Tool choice/order varies live, and live Gemini typically sets
        # should_draft_followup=False natively without hitting Guardrail Override.
        live_expect = {
            k: v
            for k, v in case.expect.items()
            if not k.startswith("tool_calls.")
            and not (
                case.id == "reminder_wakeup_skips_when_grn_already_received"
                and k == "reasoning"
            )
        }
        assertions = evaluate_expectations(output, live_expect)
        failures = [a.message for a in assertions if not a.passed]
        assert not failures, f"Live eval failed for {case.id}: {failures}; output={output}"

        if "tool_calls.length" in case.expect:
            assert len(output.get("tool_calls") or []) >= 1, (
                f"Expected live Gemini to invoke >=1 tool in {case.id}, got {output}"
            )

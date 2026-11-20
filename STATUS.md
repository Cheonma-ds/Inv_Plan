# STATUS.md — Live Session Baton

**Current Stage:** Slice 2 Complete → Ready for **Slice 3**  
**Last Updated:** 2026-11-20

## Completed
- **PRD (`PRD.md` v1.0) & Design (`DESIGN.md` v1.0):** Approved baselines.
- **Pre-Slice 0 (`e794193`):** Repo bootstrap (`git`, `uv`, FastAPI + React/Vite/Tailwind skeleton, `AGENTS.md`, `STATUS.md`).
- **Slice 1 (`24d8352`) — Deterministic Core + Seed Data:**
  - `backend/core/`: `config.py`, `enums.py`, `db.py` (SQLite FK pragma), `models.py` (all 14 tables), `clock.py` (`SimulatedClock` + `ensure_utc()`).
  - `backend/inventory/`: `schemas.py`, `service.py` (Inventory Position, `PENDING_DISPATCH_APPROVAL` duplicate-PO safety, MOQ rounding, `AcceleratedBurnAlert`, quarantine helpers).
  - `backend/po/`: `state_machine.py`, `matching.py`, `service.py`, `actions.py`.
  - `backend/seed/`: `data.py` & `reset.py` (3 suppliers, 15 parts with primary + backup terms, 12m consumption ledger, 36 closed POs + 2 open POs, 8 Lex `AutonomyPolicy` rows).
- **Slice 2 — 5-Persona Mailroom + SLA Watcher + Lex Email Agent:**
  - `backend/mailroom/`: `schemas.py`, `attachments.py` (PDF validation & storage under `data/attachments/`), `transport.py` (`EmailTransport` protocol + `DatabaseEmailTransport`, blocks manual send-as-`lex` with `ManualLexSendForbiddenError`), `service.py` (`approve_draft`, `reject_draft`, `annotate_triage`).
  - `backend/observability/`: `tracer.py` (agent-agnostic `AgentTracer` writing SQLite `agent_traces` + OTel span hook + optional Phoenix init + INR cost estimation), `autonomy.py` (`record_approval_outcome`, `list_autonomy_policies`).
  - `backend/agents/`: `base.py` (`BaseAgent`), `llm_client.py` (`LLMClientProtocol` + `GeminiLLMClient` wrapping `google-genai` SDK with `gemini-3.5-flash-lite`, native PDF byte support, Pydantic `response_schema`, token/INR metering), `lex/` (`schemas.py`, `prompts.py`, `agent.py` with `< 0.85` confidence / unmatched-PO `AMBIGUOUS` guardrail, CC enforcement, and PO-number subject guard).
  - `backend/orchestrator/`: `sla_watcher.py` (Ack SLA 24h/48h × 3 escalation, Pre-ETA check-in, Overdue detection, Quarantine 48h/96h SLA), `workflows.py` (`WorkflowEngine`: stock withdrawal → PO + draft, inbound supplier triage & routing across Scenarios 1/1A/2/3/AMBIGUOUS + Supplier Memory learning, Draft approval/rejection + autonomy tracking, inline Alert CTA execution), `deps.py`, `routes/` (`clock`, `parts`, `pos`, `alerts`, `mailroom`, `suppliers`, `observability`, `admin`), wired in `backend/main.py` with lifespan auto-seed + CORS for `:5173`.
  - **Tests & Verification:** `31 passed` (`tests/mailroom/test_mailroom.py`, `tests/agents/test_lex_agent.py`, `tests/orchestrator/test_workflows_and_sla.py`, plus all Slice 1 + AST architecture tests in `tests/test_architecture.py`), `uv run ruff check backend tests` clean, and live `gemini-3.5-flash-lite` smoke test verified structured `EmailTriageResult` (`PRICE_CHANGE`, `confidence=0.99`, `proposed_unit_price=1350.0`, `new_memory_note` extracted).

## Next Active Slice: **Slice 3 — PDF Invoice Pipeline + Planning Intelligence Engine**
- **Read ONLY:** `AGENTS.md`, `STATUS.md`, `DESIGN.md` sections **`§4.3`**, **`§6.2`**, **`§7`**, **`§9`**, and **`§10`**.
- **Modules to Build:**
  - `backend/agents/lex/agent.py` + `prompts.py` — add `extract_invoice(session, pdf_bytes, ...)` using `GeminiLLMClient(..., pdf_bytes=...)` returning `InvoiceExtractionResult` (already defined in `backend/agents/lex/schemas.py`) with `< 0.85` confidence escalation and `AgentTracer` logging (`task_type="invoice_extraction"`).
  - `backend/orchestrator/workflows.py` — wire PDF invoice extraction when an inbound supplier email has a `.pdf` attachment (or `INVOICE_SUBMISSION` intent): extract PDF → invoke `po.submit_invoice_and_match()` → on mismatch (`QUARANTINED_INVOICE_MISMATCH`), have Lex draft `INVOICE_DISCREPANCY` email + raise `HIGH`/`CRITICAL` quarantine alert; on match (`CLOSED`), release quarantined stock to `on_hand_stock` and auto-resolve open quarantine alerts!
  - `backend/planning/` — 100% deterministic Python (zero LLM calls):
    - `forecasting.py` — 12-month consumption analysis (WMA for regular parts, Croston's method for intermittent spare parts where ≥50% months are zero, trend/seasonality flag, manual override support).
    - `scoring.py` — Supplier Reliability Score (`0.50*OTD + 0.30*InvoiceAccuracy + 0.20*ResponseRate`), Effective Lead Time (`0.70*actual + 0.30*quoted` with recency weighting), Lead Time std dev, and recalculation persistence onto `Supplier` + `PartSupplierTerm`.
    - `rop.py` — Dynamic Safety Stock (`Z_alpha * sqrt(L_eff * sigma_d^2 + d_bar^2 * sigma_L^2)`), Dynamic ROP (`ceil(d_bar * L_eff + SS)`), Dynamic Replenishment Point `S`, and `ActionItem` recommendations when drift ≥ 20% or ≥ 2 units.
  - `backend/orchestrator/routes/planning.py` — FastAPI endpoints for `GET /api/planning/overview`, `POST /api/planning/recalculate`, and `POST /api/planning/parts/{part_id}/apply-recommendation` (and optional sample PDF invoice generator utility in `tests/` using `fpdf2` for realistic testing!).
- **Done Criteria:**
  - `uv run pytest` passes deterministic unit tests for WMA, Croston's, Supplier Reliability Score, Effective Lead Time, Dynamic Safety Stock/ROP/S, and end-to-end PDF invoice upload → quarantine → corrected PDF → release flow.
  - At least 1 live `gemini-3.5-flash-lite` PDF extraction smoke test using an `fpdf2`-generated invoice PDF.
  - `tests/test_architecture.py` and `uv run ruff check backend tests` pass cleanly.

## Upcoming Slices
- **Slice 4:** React Operations Dashboard & 5-Persona Mailroom UI (`DESIGN.md §9`)
- **Slice 5:** AI Activity & Health UI, YAML Eval Runner (`evals/lex.yaml`) & Optional Phoenix (`DESIGN.md §8, §9`)

## Caveats / Design Decisions Made
- `tests/conftest.py` uses SQLAlchemy `StaticPool` for `:memory:` SQLite fixtures so FastAPI `TestClient` worker threads share the exact seeded in-memory database cleanly.
- `EXCEPTION_REVIEW` is entered only from `SENT_AWAITING_ACK` or `UNRESPONSIVE_ESCALATED` (respecting `po/state_machine.py` guardrails); post-ack delays route to `DELAYED_CRITICAL` (critical/low-stock) or stay `ACKNOWLEDGED` with updated `revised_eta` (minor delay).
- `InvoiceExtractionResult` schema is already pre-defined in `backend/agents/lex/schemas.py` ready for Slice 3.
- Demo presets remain intentionally deferred until after Slice 5.

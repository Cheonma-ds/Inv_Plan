# STATUS.md — Live Session Baton

**Current Stage:** Slice 3 Complete → Ready for **Slice 4**  
**Last Updated:** 2026-11-20

## Completed
- **PRD (`PRD.md` v1.0) & Design (`DESIGN.md` v1.0):** Approved baselines.
- **Pre-Slice 0 (`e794193`):** Repo bootstrap (`git`, `uv`, FastAPI + React/Vite/Tailwind skeleton, `AGENTS.md`, `STATUS.md`).
- **Slice 1 (`24d8352`) — Deterministic Core + Seed Data:**
  - `backend/core/`: `config.py`, `enums.py`, `db.py` (SQLite FK pragma), `models.py` (all 14 tables), `clock.py` (`SimulatedClock` + `ensure_utc()`).
  - `backend/inventory/`: `schemas.py`, `service.py` (Inventory Position, `PENDING_DISPATCH_APPROVAL` duplicate-PO safety, MOQ rounding, `AcceleratedBurnAlert`, quarantine helpers).
  - `backend/po/`: `state_machine.py`, `matching.py`, `service.py`, `actions.py`.
  - `backend/seed/`: `data.py` & `reset.py` (3 suppliers, 15 parts with primary + backup terms, 12m consumption ledger, 36 closed POs + 2 open POs, 8 Lex `AutonomyPolicy` rows).
- **Slice 2 (`779e512`) — 5-Persona Mailroom + SLA Watcher + Lex Email Agent:**
  - `backend/mailroom/`: `schemas.py`, `attachments.py`, `transport.py`, `service.py`.
  - `backend/observability/`: `tracer.py`, `autonomy.py`.
  - `backend/agents/`: `base.py`, `llm_client.py`, `lex/` (`schemas.py`, `prompts.py`, `agent.py`).
  - `backend/orchestrator/`: `sla_watcher.py`, `workflows.py`, `deps.py`, `routes/` (`clock`, `parts`, `pos`, `alerts`, `mailroom`, `suppliers`, `observability`, `admin`), wired in `backend/main.py`.
- **Slice 3 — PDF Invoice Pipeline + Planning Intelligence Engine:**
  - `backend/agents/lex/prompts.py` & `agent.py`: Added `INVOICE_EXTRACTION_SYSTEM_PROMPT` and `LexAgent.extract_invoice(session, pdf_bytes, ...)` (temp `0.0`, multimodal PDF via `GeminiLLMClient`, extracts numbers verbatim with zero match math, logs `AgentTrace` with `task_type="invoice_extraction"`, enforces empty-lines / missing-invoice-number confidence cap).
  - `backend/orchestrator/workflows.py`, `routes/mailroom.py`, `routes/pos.py`:
    - Wired `_process_pdf_invoice_email()` for inbound supplier emails with `.pdf` attachments or `INVOICE_SUBMISSION` intent (even if email body omits PO#, PDF `po_number` resolves open POs safely).
    - Hard guardrail: confidence `< 0.85`, PO conflict, unresolved PO, or empty line items escalate to `INVOICE_EXTRACTION_REVIEW` with zero PO/stock mutation.
    - Runs deterministic `po.submit_invoice_and_match()`: `PENDING_GRN` creates `INVOICE_PENDING_GRN` alert; mismatch transitions PO to `QUARANTINED_INVOICE_MISMATCH`, holds stock in quarantine, drafts Lex `INVOICE_DISCREPANCY` email (`DRAFT_PENDING_APPROVAL`), and raises `HIGH`/`CRITICAL` `INVOICE_MISMATCH_QUARANTINE` alert; clean match (v1 or corrected v2) releases quarantined stock to `on_hand_stock`, closes PO, and auto-resolves open quarantine/GRN alerts.
    - Added `WorkflowEngine.record_grn()` (wired into `POST /api/pos/{po_id}/grn`) so GRN logged after an early invoice also triggers mismatch draft + alert or clean release.
    - Added `recalculate_planning()` (deduped/upserted `ROP_RECOMMENDATION` alerts) and `apply_rop_recommendation()`, plus fixed `execute_alert_cta` (`approve_rop`) to read `cta_payload_json` and `PartSnapshot.part_id`.
  - `backend/planning/` (100% deterministic Python, zero LLM calls, zero sideways imports):
    - `schemas.py`: `DemandForecastResult`, `SupplierScoreResult`, `EffectiveLeadTimeResult`, `ROPRecommendation`, `PlanningRecalculationSummary`.
    - `forecast.py`: 12-month consumption bucket builder anchored at `clock.now(session)`, EWMA (`alpha=0.25`) for `avg_daily_demand` (`D_bar`) & `daily_demand_std` (`sigma_D`), intermittent detector (`>50%` zero months with conservative rolling-peak floor), additive `manual_demand_override`.
    - `scoring.py`: Rolling 12m Supplier Reliability Score (OTD 40% with `-2 pts/day` avg late penalty, Lead Time Consistency 25%, Invoice Accuracy v1 20%, Responsiveness 0-nudge 15%) and Effective Lead Time (`<3` closed deliveries → quoted; `>=3` → `max(actual_mean, 0.25*quoted + 0.75*actual_mean)`).
    - `rop.py`: Criticality Z-factors (`CRITICAL=2.05`, `STANDARD=1.65`, `LOW=1.28`), `SS = Z * sqrt(L_eff * sigma_D^2 + D_bar^2 * sigma_L^2)`, `ROP_rec = ceil(D_bar * L_eff + SS)`, `S_rec = ROP_rec + ceil(D_bar * target_coverage_days)`.
    - `service.py` & `__init__.py`: `evaluate_part_recommendation`, `get_planning_overview`, `recalculate_all`.
  - `backend/orchestrator/routes/planning.py` (registered in `routes/__init__.py`): `GET /api/planning/overview`, `POST /api/planning/recalculate`, `POST /api/planning/parts/{part_id}/apply-recommendation`.
  - **Tests & Verification:**
    - Added `tests/helpers/invoice_pdf.py` (`fpdf2` commercial invoice PDF generator), `tests/planning/test_planning_engine.py`, `tests/orchestrator/test_invoice_pipeline.py`, and `tests/agents/test_live_gemini_pdf.py`.
    - Offline suite: `uv run pytest` → **`41 passed, 1 skipped`** (including `tests/test_architecture.py` AST boundary & clock checks).
    - Live multimodal smoke test: `RUN_LIVE_GEMINI=1 uv run pytest tests/agents/test_live_gemini_pdf.py -v` → **`1 passed`** (`gemini-3.5-flash-lite` extracted `INV-LIVE-2026-088`, `PO-1042`, qty `6`, unit price `₹5,250.00`, subtotal `₹31,500.00`, `confidence >= 0.85`, logged trace tokens, and triggered deterministic `MISMATCH_PRICE` against PO `₹4,800.00`).
    - Lint: `uv run ruff check backend tests` → **`All checks passed!`**.

## Next Active Slice: **Slice 4 — React Operations Dashboard & 5-Persona Mailroom UI**
- **Read ONLY:** `AGENTS.md`, `STATUS.md`, `DESIGN.md` sections **`§9`** and **`§10`** (plus `PRD.md` only if UX flow details are ambiguous).
- **Modules to Build (`frontend/`):**
  - Shared API client (`frontend/src/api/`), types, and top-level layout with global **Simulated Clock** bar (`+6h`, `+24h`, `+3d`, `+7d`, Reset & Seed Demo), persona/view navigation, and toast/status feedback.
  - **Action Center** (prioritized `ActionItem` feed with severity badges, inline Lex draft preview/edit/approve/reject, and 1-click CTAs for PO dispatch, price-change/delay/backup-supplier, quarantine, and ROP approval).
  - **Inventory & Planning View** (15-SKU table with on-hand / on-order / quarantined / ROP / S, quick Stock Withdrawal modal, `Recalculate Planning` trigger, and per-part recommended vs current ROP/S apply action + supplier reliability panel).
  - **Purchase Orders & GRN View** (PO lifecycle list/detail with status badges, promised vs revised ETA, invoice history, and Log GRN form).
  - **5-Persona Mailroom UI** (`inventory_manager`, `sup1`, `sup2`, `sup3`, `lex` persona switcher, Inbox/Sent tabs, manual compose with `.pdf` invoice upload, read-only enforcement for `lex`, inline draft approval controls).
  - Remember guardrail: **No hardcoded demo preset scripts** in the UI; keep Mailroom cleanly usable with manual compose/PDF upload.
- **Done Criteria:**
  - `cd frontend && npm run build` passes TypeScript + Vite production build cleanly.
  - Backend + frontend verified end-to-end against live local FastAPI server (or API contract checks), plus `uv run pytest` and `uv run ruff check backend tests` remain green.

## Upcoming Slices
- **Slice 5:** AI Activity & Health UI, YAML Eval Runner (`evals/lex.yaml`) & Optional Phoenix (`DESIGN.md §8, §9`)

## Caveats / Design Decisions Made
- `backend/planning/` follows `DESIGN.md §7` strictly: EWMA (`alpha=0.25`) + intermittent rolling-peak floor (`>50%` zero months), 40/25/20/15 Supplier Reliability Score, `<3` deliveries quoted fallback / `>=3` `max(actual, 0.25*quoted + 0.75*actual)` Effective Lead Time, and Z-factor dynamic ROP/S.
- `tests/agents/test_live_gemini_pdf.py` is gated behind `RUN_LIVE_GEMINI=1` (in addition to `$GEMINI_API_KEY`) so default offline `uv run pytest` stays fast and deterministic, while opt-in live verification exercises real `gemini-3.5-flash-lite` multimodal PDF extraction.
- `tests/conftest.py` uses SQLAlchemy `StaticPool` for `:memory:` SQLite fixtures so FastAPI `TestClient` worker threads share the exact seeded in-memory database cleanly.
- `EXCEPTION_REVIEW` is entered only from `SENT_AWAITING_ACK` or `UNRESPONSIVE_ESCALATED` (respecting `po/state_machine.py` guardrails); post-ack delays route to `DELAYED_CRITICAL` (critical/low-stock) or stay `ACKNOWLEDGED` with updated `revised_eta` (minor delay).
- Demo presets remain intentionally deferred until after Slice 5.

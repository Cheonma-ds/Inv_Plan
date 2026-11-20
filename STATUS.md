# STATUS.md — Live Session Baton

**Current Stage:** Slices 1–4 Complete → Ready for **Slice 5 (AI Activity & Health UI, YAML Eval Runner & Optional Phoenix)**  
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
- **Slice 3 (`013bf32`) — PDF Invoice Pipeline + Planning Intelligence Engine:**
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
- **Pre-Slice 4 Audit Hardening Pass (`P1`–`P18` in `problems_to_resolve.md`):**
  - **Runtime & Data Integrity (`P1`–`P4`):**
    - `backend/seed/data.py`: `seed_database(session, *, reset: bool = False)` now supports in-session `reset=True` (deleting rows in reverse topological order and clearing session identity map) for `POST /api/admin/reset-seed` and `reset=False` for FastAPI lifespan auto-seeding (`P1`).
    - `backend/orchestrator/routes/parts.py`: Fixed `PATCH /api/parts/{part_id}/rop` to access `PartSnapshot.part_id` (`P2`).
    - `backend/po/state_machine.py` & `DESIGN.md §4.2`: Added `POStatus.DELAYED_CRITICAL -> POStatus.OVERDUE` transition so `run_sla_watcher()` transitions delayed critical POs that miss their `revised_eta + 1d` (`P3`).
    - `backend/orchestrator/workflows.py`: Fixed `recalculate_planning()` to assign native Python `list`/`dict` objects to `suggested_actions_json` and `cta_payload_json` without `json.dumps()` double-encoding (`P4`).
  - **Domain Math & PRD Alignment (`P5`–`P13`):**
    - `backend/planning/scoring.py` & `service.py`: Included `OVERDUE` POs without GRN in OTD evaluation (`P5`), computed supplier-level lead-time consistency from `actual_lt - quoted_lt` deviations so heterogeneous SKU lead times do not penalize on-time suppliers (`P6`), updated `evaluate_part_recommendation()` to compute live effective lead times matching `get_planning_overview()` (`P7`), and added `refresh_supplier_metrics()` triggered on PO overdue/close/mismatch/reminder events (`P5`).
    - `backend/inventory/service.py` & `backend/orchestrator/workflows.py`: Added `receive_stock_direct()`, `clear_quarantined_stock()`, and `set_manual_demand_override()`. Deducted `part.quarantined_stock` from `get_on_order_stock()` while keeping `inventory_position = on_hand_stock + on_order_stock + quarantined_stock` (eliminating double-counting while preventing duplicate POs during quarantine disputes, `P10`), and wired quarantine stock cleanup on PO cancellation / backup switch (`P8`).
    - `backend/orchestrator/sla_watcher.py`: Anchored Quarantine 48h/96h SLA timer at `max(latest_inv.received_at, latest_grn.received_at)` so pre-GRN invoices only start the quarantine clock once physical goods arrive (`P9`).
    - `backend/orchestrator/workflows.py`: Computed multi-line invoice weighted average unit price from `sum(li.qty * li.unit_price) / extracted_qty` so subtotal arithmetic errors are never masked, and consumed `match_result.price_diff_per_unit`, `total_price_impact`, and `qty_diff` directly (`P11`).
    - `backend/po/actions.py` & `backend/orchestrator/routes/alerts.py`: Implemented deterministic `compute_urgency_score()` with strict non-overlapping severity bands (`CRITICAL`: 80–100, `HIGH`: 60–79, `MEDIUM`: 35–54, `INFO`: 10–29) and exposed `"urgency_score"` in `GET /api/alerts` (`P12`).
    - `backend/orchestrator/workflows.py`: Added deterministic stockout projection (`avg_daily_demand`, `projected_days_of_supply`, `projected_stockout_date`, `days_to_new_eta` with 3-day safety buffer branching) for Scenario 1A (`POST_ACK_DELAY`) and deterministic price variance (`price_variance_inr`, `price_variance_pct`, `total_cost_impact_inr`) + stockout projection for Scenario 2 (`PO_EXCEPTION`) (`P13`).
  - **Module Independence & API Completeness (`P14`–`P18`):**
    - `backend/po/service.py` & `matching.py`: Decoupled `po/` from `Part` and `InventoryTransaction` mutations; `_apply_match_outcome()` emits `stock_action` directives on `ThreeWayMatchResult` that `WorkflowEngine._handle_invoice_match_outcome()` executes via `backend/inventory/service.py` (`P14`).
    - `backend/mailroom/transport.py` & `service.py`: Extended `EmailTransport` & `DatabaseEmailTransport` with `get_communication`, `approve_draft`, `reject_draft`, and `annotate_triage` so `mailroom/service.py` delegates 100% through `EmailTransport` (`P15`).
    - `backend/orchestrator/workflows.py` & `routes/pos.py`: Unified backup supplier switching into `WorkflowEngine.switch_to_backup_supplier()` (`P16`).
    - `backend/orchestrator/routes/parts.py`, `routes/clock.py`, `workflows.py`: Added `PATCH /api/parts/{part_id}/demand-override`, `POST /api/clock`, `cancel_po` CTA (`WorkflowEngine.cancel_po_workflow`), and `cta_payload_json` fallback defaults on `update_po` CTA (`P17`).
    - `backend/agents/lex/schemas.py`, `DESIGN.md §6.2`, `backend/po/service.py`: Added `po_reference`, `grand_total`, `part_code` aliases/properties on `InvoiceExtractionResult` / `InvoiceLineItem` without introducing `anyOf` into `model_json_schema()`, and guarded `update_po_terms()` against mutating `CLOSED` or `CANCELLED` POs (`P18`).
  - **Tests & Verification (Pre-Slice 4):**
    - Offline suite: `uv run pytest -v` → **`58 passed, 1 skipped`** (including 17 new unit, integration, and AST architecture boundary tests across `tests/test_architecture.py`, `tests/seed/`, `tests/po/`, `tests/inventory/`, `tests/mailroom/`, `tests/planning/`, `tests/agents/`, and `tests/orchestrator/`).
    - Lint: `uv run ruff check backend tests` → **`All checks passed!`**.
- **Slice 4 — React Operations Dashboard & 5-Persona Mailroom UI:**
  - **Shared API Client & Layout (`frontend/src/`):**
    - `frontend/src/api/types.ts` & `frontend/src/api/client.ts`: Strict TypeScript contracts and typed `fetch` wrapper covering Clock/Admin, Alerts, Parts/Inventory, POs/GRN, 5-Persona Mailroom (including `FormData` PDF upload), Suppliers/Memory, and Planning endpoints.
    - `frontend/src/utils/format.ts`: UTC simulated timestamp formatting, INR currency formatting, status/severity/criticality badge styles, and persona display names.
    - `frontend/src/components/ClockBar.tsx` & `ToastContainer.tsx`: Persistent simulated clock header (`+6h`, `+1d`, `+3d`, `+7d` via `POST /api/clock/advance`, `Reset Seed` via `POST /api/admin/reset-seed`, open alerts badge) and non-blocking toast notifications.
    - `frontend/src/App.tsx`: Responsive sidebar navigation across all 5 Slice 4 views plus a clean Slice 5 placeholder tab (`AIHealthPlaceholderPage.tsx`), cross-tab PO/Part drill-down navigation, and synchronized global refresh nonce.
  - **5 Operational Views (`frontend/src/pages/`):**
    - `AlertsPage.tsx`: Priority/urgency-sorted alert cards (`OPEN` / `RESOLVED` / `DISMISSED` filter), stockout & price-variance impact pills from `cta_payload`, inline editable Lex email draft preview, inline CTA execution (`POST /api/alerts/{id}/execute-cta`), and `Dismiss`.
    - `InventoryPage.tsx`: 15-SKU live inventory table (`on_hand`, `quarantined`, `on_order`, `inventory_position`, `ROP`/`S`, primary & backup suppliers) with search/criticality filters and modals for **Withdraw Stock** (`POST /api/parts/{id}/consume`), **Edit ROP/S** (`PATCH /api/parts/{id}/rop`), and **Manual Demand Override** (`PATCH /api/parts/{id}/demand-override`).
    - `POTrackerPage.tsx`: Filterable PO table + slide-over detail drawer showing PO timeline, GRN logging (`POST /api/pos/{id}/grn`, gated to `ACKNOWLEDGED`, `DELAYED_CRITICAL`, `OVERDUE` per `po/state_machine.py`), 3-way invoice match breakdown, manager notes (`PATCH /api/pos/{id}`), switch-to-backup supplier (`POST /api/pos/{id}/switch-supplier`), and linked email thread.
    - `MailroomPage.tsx`: Gmail-style Inbox/Sent/Drafts with 1-click 5-persona switcher (`inventory_manager`, `sup1`, `sup2`, `sup3`, read-only `lex` with `403` compose lock), Compose/Reply with `To`/`CC` and `.pdf` upload (`POST /api/mailroom/send`), inline Lex draft approve/edit/reject (`POST /api/mailroom/drafts/{comm_id}/approve` & `/reject`), and Lex triage classification/confidence badges. Zero hardcoded demo preset scripts.
    - `SuppliersPlanningPage.tsx`: Side-by-side `Sup1`/`Sup2`/`Sup3` reliability scorecards (40/25/20/15 breakdown, quoted vs. effective lead time bars, OTD %, invoice accuracy %, response %), editable **Supplier Memory** notes (`POST/PATCH/DELETE /api/suppliers/{id}/memory`), **Recalculate Planning** (`POST /api/planning/recalculate`), and Dynamic ROP/$S$ recommendation cards with 12-month consumption buckets and 1-click Apply (`POST /api/planning/parts/{part_id}/apply-recommendation`).
    - `AIHealthPlaceholderPage.tsx`: Clean Slice 5 placeholder route/tab.
  - **Repo Hygiene:** Added `dbbackup/` to `.gitignore` and backed up `data/inv_plan.db` to `dbbackup/inv_plan.db`.
  - **Tests & E2E Verification:**
    - `cd frontend && npm run build` → `✓ built in 1.57s` (zero TS/Vite errors).
    - `uv run pytest` → `58 passed, 1 skipped in 6.67s`.
    - `uv run ruff check backend tests` → `All checks passed!`.
    - Live FastAPI (`:8000`) + Vite (`:5173`) E2E verification (with live Gemini extraction & drafting) passed all 9 checks: Vite index/proxy, clock advance & reset-seed, stock withdrawal auto-triggering PO `PO-1039`, inline ROP/S & demand override, approving Lex draft (`SENT_AWAITING_ACK`), logging GRN (`HELD_QUARANTINE`), Mailroom PDF invoice upload + Lex extraction on `PO-1037`, supplier memory CRUD + planning recalculate & apply, and headless Chromium DOM mount check.

## Next Active Slice: **Slice 5 — AI Activity & Health UI, YAML Eval Runner & Optional Phoenix**
- **Read ONLY:** `AGENTS.md`, `STATUS.md`, `DESIGN.md` sections **`§8`**, **`§9`** (View 6), and **`§10`**, plus `backend/orchestrator/routes/observability.py` and `backend/observability/`.
- **Modules to Build:**
  - `backend/evals/lex.yaml` + `backend/observability/eval_runner.py` (offline + live-capable structured YAML eval suite covering Lex email triage, PDF invoice extraction, confidence escalation `< 0.85`, and draft quality).
  - Observability API route(s) for listing `agent_traces`, `autonomy_policies`, and triggering/inspecting eval runs (without breaking `observability/` agent-agnostic design).
  - `frontend/src/pages/AIHealthPage.tsx` (replacing `AIHealthPlaceholderPage.tsx` in `frontend/src/App.tsx`): Agent trace feed, Lex Draft Trust Metrics (`APPROVED_AS_IS` vs `APPROVED_EDITED` vs `REJECTED`), and 1-click YAML Eval Runner scorecard UI.
  - Optional Phoenix tracing integration check (`ENABLE_PHOENIX=true`) if `--extra phoenix` is enabled.
- **Done Criteria:**
  - `uv run pytest`, `uv run ruff check backend tests`, and `cd frontend && npm run build` all pass cleanly.
  - Eval runner executes and persists/returns scored results, visible in the AI Activity & Health UI.

## Upcoming Slices
- **Post-Slice 5:** Optional demo presets / polish (deferred until after Slice 5).

## Caveats / Design Decisions Made
- `backend/planning/` follows `DESIGN.md §7` strictly: EWMA (`alpha=0.25`) + intermittent rolling-peak floor (`>50%` zero months), 40/25/20/15 Supplier Reliability Score, `<3` deliveries quoted fallback / `>=3` `max(actual, 0.25*quoted + 0.75*actual)` Effective Lead Time, and Z-factor dynamic ROP/S.
- `tests/agents/test_live_gemini_pdf.py` is gated behind `RUN_LIVE_GEMINI=1` (in addition to `$GEMINI_API_KEY`) so default offline `uv run pytest` stays fast and deterministic, while opt-in live verification exercises real `gemini-3.5-flash-lite` multimodal PDF extraction.
- `tests/conftest.py` uses SQLAlchemy `StaticPool` for `:memory:` SQLite fixtures so FastAPI `TestClient` worker threads share the exact seeded in-memory database cleanly.
- `EXCEPTION_REVIEW` is entered only from `SENT_AWAITING_ACK` or `UNRESPONSIVE_ESCALATED` (respecting `po/state_machine.py` guardrails); post-ack delays route to `DELAYED_CRITICAL` (critical/low-stock) or stay `ACKNOWLEDGED` with updated `revised_eta` (minor delay).
- Demo presets remain intentionally deferred until after Slice 5.

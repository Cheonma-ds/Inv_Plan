# Phase 3 Phased Build Slices (`SLICES_P3.md`)

**Execution Philosophy:** Each slice is small, self-contained (< 30% session context), independently testable, preserves `tests/test_architecture.py`, and ends with `uv run pytest`, `uv run ruff check backend tests`, and (when frontend changes) `cd frontend && npm run build`.

> **Discrepancy Protocol (`AGENTS.md §5`):** Coding agents must implement [`PRD_P3.md`](PRD_P3.md) and [`DESIGN_P3.md`](DESIGN_P3.md) faithfully. Any edge-case discrepancy uncovered during implementation must be documented with proof in [`STATUS_P3.md`](STATUS_P3.md) and [`DESIGN_P3.md`](DESIGN_P3.md).

---

## Dependency Graph

```text
  [P3-1] Core Clock (Oct 1 1 PM IST), Schema Migrations,
         On-Order Draft Exclusion, Partial Match & Overdue Fixes
       │
       ├───────────────────────────────┐
       ▼                               ▼
  [P3-2] 90d Causal Seed, Daily EWMA   [P3-4] Lex Resilience, Multimodal Images
         (α=0.3), Clock Burn Rollover,        & Structured Draft Formatting
         Cycles & Supplier Metrics            │
       │                                      ▼
       ▼                               [P3-5] Lex v2 Tool Loop, Sandbox Calc,
  [P3-3] Alerting Overhaul: Edit PO,          Dynamic Reminders & Memory Alerts
         Dismiss Warning & ROP/T Sweep        │
       │                                      │
       └───────────────┬──────────────────────┘
                       ▼
  [P3-6] Frontend Core: IST Clock, Nav Cleanup, FormattedEmailBody,
         Inventory Revamp (View PO / Daily Burn / Stockout In) & Cycle Drawer
                       │
                       ▼
  [P3-7] Frontend Create/Edit PO Modal (Suggested Qty), Alerts Revamp,
         Suppliers 90d Metrics & AI Health Tool-Call Traces
                       │
                       ▼
  [P3-8] Full Phase 3 E2E Walkthrough, Live Gemini Suite & Docs Parity Gate
```

---

## Slice Overview Matrix

| Slice | Goal | New Files | Existing Files Touched | Independent Verification Gate |
|---|---|---|---|---|
| **P3-1** | Oct 1 1 PM IST Clock, Schema Migrations, `on_order` Draft Exclusion, Partial GRN Match & Overdue Transitions | `tests/po/test_phase3_core_fixes.py` | `backend/core/{config,enums,models,migrations,clock}.py`, `backend/inventory/{schemas,service}.py`, `backend/po/{matching,service,state_machine}.py`, `backend/orchestrator/{logistics_workflow,sla_watcher,workflows}.py`, impacted core tests | `uv run pytest`, `uv run ruff check backend tests`; proves `on_order=0` on draft PO without duplicate POs, partial GRN matching, and `UNRESPONSIVE_ESCALATED -> OVERDUE`. |
| **P3-2** | 90-Day Causal Simulator (`Jul–Sep`, `0` Active POs), Daily Burn EWMA ($\alpha=0.3$), Clock Consumption Rollover & Cycle Analytics API | `backend/planning/ewma_daily.py`, `backend/planning/cycles.py`, `tests/planning/test_daily_ewma_and_cycles.py` | `backend/planning/{rop,scoring,schemas,service,__init__}.py`, `backend/seed/data.py`, `backend/orchestrator/{workflows,routes/parts,routes/suppliers}.py`, `tests/seed/test_seed_and_clock.py` | `uv run pytest`, `ruff`; proves 0 open POs on seed, `Daily Burn` fixed on same-day withdraw while `Stockout In` updates, pure predicted burn tick leaves `ROP/T` unchanged while extra draw updates EWMA & `ROP/T`, and `AcceleratedBurn` pre-zero firing. |
| **P3-3** | Alerting Overhaul (`Edit PO`, `Reject` Non-Resolve, `DISMISSED` Draft Hiding + Inventory Warning) & `ROP/T` Naming Sweep | `tests/orchestrator/test_alerts_phase3.py` | `backend/po/{actions,__init__}.py`, `backend/planning/rop.py`, `backend/orchestrator/{workflows,stock_request_workflow,routes/alerts,routes/pos,routes/parts}.py` | `uv run pytest`, `ruff`; proves `Edit PO` mutates/sends existing draft PO with 0 duplicates, `reject_lex_draft` preserves alert status, `DISMISSED` hides `draft_preview` & flags part until resolved. |
| **P3-4** | Lex Resilience (Retries/Fallbacks), Multimodal Image Triage & Enforced Outbound Email Formatting | `tests/agents/test_lex_formatting_and_resilience.py` | `backend/agents/llm_client.py`, `backend/agents/lex/{prompts,agent,eval_adapter}.py`, `backend/orchestrator/workflows.py`, `backend/evals/lex.yaml` | `uv run pytest`, `ruff`; proves retry/backoff, non-crashing `AMBIGUOUS` fallback, image bytes passed to triage, and bold/paragraph/sign-off formatting invariants. |
| **P3-5** | Lex v2 Tool-Use Loop, AST-Sandboxed Calc Executor, Dynamic Reminders (`+4d` Deferral) & Memory-Informed Alerts | `backend/agents/tools.py`, `backend/agents/sandbox.py`, `backend/orchestrator/lex_tools.py`, `tests/agents/test_sandbox_and_tools.py`, `tests/orchestrator/test_lex_v2_workflows.py` | `backend/agents/llm_client.py`, `backend/agents/lex/{schemas,prompts,agent,eval_adapter}.py`, `backend/orchestrator/{logistics_workflow,sla_watcher,workflows}.py`, `backend/evals/lex.yaml`, `tests/test_architecture.py` | `uv run pytest`, `ruff`; AST tests pass cleanly; sandbox blocks unsafe code; *"details after 4 days"* schedules 4-day `LexReminder` without premature nudge and wakes cleanly on `+4d`. |
| **P3-6** | Frontend Core: IST Formatting, Sidebar Cleanup, `FormattedEmailBody`, `Inventory & Parts` Revamp & `Stockout In` Cycle Drawer | `frontend/src/components/FormattedEmailBody.tsx`, `frontend/src/components/StockoutCycleDrawer.tsx` | `frontend/src/App.tsx`, `frontend/src/components/ClockBar.tsx`, `frontend/src/utils/format.ts`, `frontend/src/api/{types,client}.ts`, `frontend/src/pages/{InventoryPage,MailroomPage,POTrackerPage}.tsx` | `cd frontend && npm run build`; browser check confirms IST clock, no sidebar Alerts link, `View PO` jumps & opens PO drawer, `Daily Burn` neutral grey trend %, clickable `Stockout In` 90d cycle chart drawer, and dismissed warning badge. |
| **P3-7** | Frontend `CreateEditPOModal` (Suggested Qty + Reasoning), `AlertsPage` Revamp, `SuppliersPlanningPage` 90d Metrics & `AIHealthPage` Tool Traces | `frontend/src/components/CreateEditPOModal.tsx` | `frontend/src/pages/{InventoryPage,AlertsPage,POTrackerPage,SuppliersPlanningPage,AIHealthPage}.tsx`, `frontend/src/api/{types,client}.ts` | `cd frontend && npm run build`, `uv run pytest`, `ruff`; browser verifies `Edit PO` modal from Alerts, `Suggested Qty` reasoning, hidden draft on `DISMISSED`, `ROP/T` everywhere, 90d cycles & avg response days, and tool traces. |
| **P3-8** | Final Phase 3 Verification: Full Browser E2E, Live `gemini-3.5-flash-lite` Eval Sweep & Documentation Parity | *(Verification & doc polish only)* | `docs/phase3/STATUS_P3.md`, `STATUS.md`, `README.md` | `uv run pytest`, `RUN_LIVE_GEMINI=1 uv run pytest tests/agents/`, live eval run, `ruff`, `npm run build`, and headless Chromium E2E across all Phase 3 acceptance criteria. |

---

## Detailed Slice Specifications (Fresh-Session Execution Ready)

### Slice P3-1 — Clock Baseline, Migrations, `on_order` Draft Exclusion, Partial Match & Overdue Fixes
- **Read Only:** `AGENTS.md`, `docs/phase3/{STATUS_P3,PRD_P3,DESIGN_P3}.md §2–§3`, `backend/core/{config,enums,models,migrations,clock}.py`, `backend/inventory/service.py`, `backend/po/{matching,service,state_machine}.py`, `backend/orchestrator/sla_watcher.py`.
- **Leave Untouched:** `frontend/`, `backend/agents/`, `backend/planning/`, `backend/seed/data.py` (only adjust `SEED_START_TIME` constant here so `P3-2` can rebuild the 90d simulator cleanly).
- **Tasks:**
  1. Set `SEED_START_TIME` & `settings.default_sim_start` to `2026-10-01T07:30:00+00:00` (`Oct 1, 2026 13:00 IST`), add `to_ist_date()` in `core/clock.py`, and align `parse_iso_date_utc()` date-only normalization to `07:30:00Z`.
  2. Add Phase 3 additive columns (`parts`, `suppliers`, `inventory_transactions`, `action_items`) and `LexReminder` ORM model + `ensure_schema_migrations()`.
  3. Update `get_on_order_stock()` (exclude `PENDING_DISPATCH_APPROVAL`, subtract per-PO GRNs) and expose `active_po_id`, `active_po_number`, `active_po_status`, `pending_dispatch_qty` on `PartSnapshot`; preserve duplicate-PO guard when `has_active_po`.
  4. Update `matching.py` (`qty_ok = (invoice_qty == grn_received_qty)`), `po/service.py` (cumulative partial GRN completion), `state_machine.py` (`UNRESPONSIVE_ESCALATED -> OVERDUE`, `SENT_AWAITING_ACK -> OVERDUE`, `RECEIVED_PENDING_INVOICE -> ACKNOWLEDGED`), and `sla_watcher.py`.
  5. Update impacted existing tests and add `tests/po/test_phase3_core_fixes.py`.

---

### Slice P3-2 — 90-Day Causal Seed Simulator, Daily Burn EWMA ($\alpha=0.3$), Clock Rollover & Cycle Analytics
- **Read Only:** `AGENTS.md`, `docs/phase3/{STATUS_P3,PRD_P3,DESIGN_P3}.md §3.2–§4`, `backend/planning/*`, `backend/seed/data.py`, `backend/orchestrator/workflows.py` (clock & withdraw methods), `backend/orchestrator/routes/{parts,suppliers}.py`.
- **Leave Untouched:** `frontend/`, `backend/agents/`, `backend/mailroom/`.
- **Tasks:**
  1. Create `backend/planning/ewma_daily.py` implementing 90-day daily consumption buckets (IST calendar days), daily EWMA ($\alpha = 0.3$), 7-day neutral trend `%`, $\sigma_D$, intermittent floor, supplier `comm_buffer_days`, `Stockout In` urgency tier, and supplier-specific PO quantity suggestion + reasoning.
  2. Create `backend/planning/cycles.py` reconstructing 90-day daily `on_hand` trajectory + `Stockout → PO → Replenishment → Stockout` markers from DB transactions/POs/GRNs + future stockout projection.
  3. Update `planning/scoring.py` & `service.py` to compute `avg_response_days` and `cycles_90d` from persisted DB rows, and update `rop.py` to consume 90d daily EWMA + $\sigma_D$.
  4. Wire `settle_daily_burn_on_clock_advance()` into `WorkflowEngine` clock advance/set/jump methods (preserving the zero-extra-event `ROP/T` freeze invariant and updating EWMA/$\sigma_D$/`ROP/T` on days with `Withdraw`/Making consumption), plus `ACCELERATED_BURN` (`on_hand > 0` & `predicted_stockout_date < earliest_po_eta`) and `STOCKOUT_ACTIVE` (`on_hand == 0`).
  5. Rewrite `backend/seed/data.py` with 15 varied 90-day Jul–Sep daily patterns + causal PO/GRN/Invoice/Comm generation ending with **0 active POs** on `Oct 1 13:00 IST`, and compute initial `ROP/T` & supplier scores via `planning/`.
  6. Add `GET /api/parts/{id}/cycle-analytics` & `GET /api/parts/{id}/po-suggestion` and enrich `GET /api/parts` & `GET /api/suppliers`.
  7. Add `tests/planning/test_daily_ewma_and_cycles.py` and update `tests/seed/test_seed_and_clock.py`.

---

### Slice P3-3 — Alerting Overhaul (`Edit PO`, `Dismiss` Warning, Non-Resolving `Reject`) & `ROP/T` Sweep
- **Read Only:** `AGENTS.md`, `docs/phase3/{STATUS_P3,PRD_P3,DESIGN_P3}.md §3.4`, `backend/po/actions.py`, `backend/orchestrator/{workflows,stock_request_workflow,routes/alerts,routes/pos}.py`, `backend/planning/rop.py`.
- **Leave Untouched:** `frontend/`, `backend/agents/`, `backend/seed/data.py`.
- **Tasks:**
  1. Replace `reject_draft` suggested action with `edit_po` on `PO_DISPATCH_APPROVAL` alerts; implement `WorkflowEngine.dispatch_edited_po()` + `POST /api/pos/{po_id}/dispatch-edited` + `edit_po` CTA handler.
  2. Fix `reject_lex_draft()` so it rejects only the email draft without resolving the parent `ActionItem`.
  3. Update `dismiss_action_item` / `GET /api/alerts`: reject pending unapproved draft on dismiss, set `draft_preview = None` on `DISMISSED` alerts, expose `unresolved_dismissed_alerts` on `GET /api/parts`, auto-set `dismiss_resolved_at` when underlying PO/stock condition resolves, and add `POST /api/alerts/{id}/reopen` & `POST /api/alerts/{id}/acknowledge-dismissed`.
  4. Sweep all backend `ROP/S` / `ROP / S` strings to `ROP/T`.
  5. Add `tests/orchestrator/test_alerts_phase3.py`.

---

### Slice P3-4 — Lex Resilience, Multimodal Image Support & Structured Formatting Upgrade
- **Read Only:** `AGENTS.md`, `docs/phase3/{STATUS_P3,PRD_P3,DESIGN_P3}.md §5.3`, `backend/agents/{llm_client,lex/prompts,lex/agent,lex/eval_adapter}.py`, `backend/orchestrator/workflows.py`, `backend/evals/lex.yaml`.
- **Leave Untouched:** `frontend/`, `backend/inventory/`, `backend/planning/`, `backend/po/`.
- **Tasks:**
  1. Harden `GeminiLLMClient` with configurable timeout, exponential-backoff retries on transient errors, and `image_attachments` multimodal input support in `LLMClientProtocol`.
  2. Upgrade `DRAFT_SYSTEM_PROMPT` & `LexAgent.draft_email()` with structured paragraph/bullet/`**bold**`/sign-off formatting enforcement + deterministic cleanup & currency/PO verification.
  3. Pass image attachments and multi-PDF context cleanly from `workflows.py`, add graceful fallback to `AMBIGUOUS_EMAIL` on LLM failure, and close the `additional_po_updates` `PRICE_CHANGE` guardrail gap in `agent.py`.
  4. Update `backend/evals/lex.yaml` and add `tests/agents/test_lex_formatting_and_resilience.py`.

---

### Slice P3-5 — Lex v2 Tool-Use Loop, Sandbox Calc Executor, Dynamic Reminders & Smart Memory Alerts
- **Read Only:** `AGENTS.md`, `docs/phase3/{STATUS_P3,PRD_P3,DESIGN_P3}.md §1.2 & §5`, `backend/agents/lex/*`, `backend/orchestrator/{workflows,logistics_workflow,sla_watcher}.py`, `tests/test_architecture.py`.
- **Leave Untouched:** `frontend/`, `backend/inventory/service.py`, `backend/planning/*`.
- **Tasks:**
  1. Create `backend/agents/tools.py` (`ToolDefinition`, `ToolRegistry`) and `backend/agents/sandbox.py` (AST-validated safe calculation sandbox with timeout).
  2. Implement `generate_with_tools()` in `llm_client.py` logging tool calls into `AgentTracer.span.tool_calls`.
  3. Create `backend/orchestrator/lex_tools.py` binding DB read tools, `run_sandbox_calc`, `schedule_lex_reminder`, `propose_memory_note`, and `create_advisory_alert` while passing all AST rules in `tests/test_architecture.py`.
  4. Upgrade `LexAgent` + `logistics_workflow.py` + `sla_watcher.py`: support variable deferral days (`details_promised_days` / `details_promised_by_date`), schedule `LexReminder` instead of nagging immediately when supplier promises details later and stockout runway is safe, wake Lex on clock advance when `remind_at <= sim_now`, and deduplicate + contextualize memory-triggered alerts.
  5. Update `eval_adapter.py` + `lex.yaml` and add `tests/agents/test_sandbox_and_tools.py` & `tests/orchestrator/test_lex_v2_workflows.py`.

---

### Slice P3-6 — Frontend Core: IST Clock, Sidebar Cleanup, Formatted Emails, `Inventory & Parts` & Cycle Drawer
- **Read Only:** `AGENTS.md`, `docs/phase3/{STATUS_P3,PRD_P3,DESIGN_P3}.md`, `frontend/src/{App.tsx,api/types.ts,api/client.ts,utils/format.ts,components/ClockBar.tsx}`, `frontend/src/pages/{InventoryPage,MailroomPage,POTrackerPage}.tsx`.
- **Leave Untouched:** `backend/`, `SuppliersPlanningPage.tsx`, `AIHealthPage.tsx`, `AlertsPage.tsx` (Alerts/Suppliers/AIHealth modal wiring in `P3-7`).
- **Tasks:**
  1. Update `formatSimDateTime` / `formatSimDate` to `Asia/Kolkata` (`IST`), remove `Actionable Alerts` from sidebar (`App.tsx`), highlight `Open Alerts` in `ClockBar` when active, and pass `onNavigatePo` into `InventoryPage`.
  2. Create `FormattedEmailBody.tsx` and wire into `MailroomPage` and `POTrackerPage` linked email threads.
  3. Revamp `InventoryPage.tsx`: columns (`On Hand`, `On Hold`, `On Order`, `Daily Burn`, `Stockout In`, `ROP/T` without `30d cov`), remove `Demand Override` / `Edit ROP/S` / `Demand`, swap `[Create PO]` ↔ `[View PO]` (navigating to `POTracker` & opening PO drawer), and render unresolved dismissed-alert warning badge + interactive popover.
  4. Create `StockoutCycleDrawer.tsx` with 90-day SVG trend + cycle markers, predicted stockout date, effective LT + comm buffer, `α = 0.3` note, and recommended PO quantity.

---

### Slice P3-7 — Frontend `CreateEditPOModal`, `AlertsPage`, `SuppliersPlanningPage` & `AIHealthPage` Tool Traces
- **Read Only:** `AGENTS.md`, `docs/phase3/{STATUS_P3,PRD_P3,DESIGN_P3}.md`, `frontend/src/pages/{InventoryPage,AlertsPage,POTrackerPage,SuppliersPlanningPage,AIHealthPage}.tsx`.
- **Leave Untouched:** `backend/`.
- **Tasks:**
  1. Extract `CreateEditPOModal.tsx` with live per-supplier **Suggested Qty + Reasoning**; wire into `InventoryPage` (`Create PO`), `AlertsPage` (`Edit PO` CTA on `PO_DISPATCH_APPROVAL`), and `POTrackerPage` (`PENDING_DISPATCH_APPROVAL` drawer).
  2. Update `AlertsPage.tsx`: remove `[Reject Draft]` on PO dispatch alerts, hide `draft_preview` on `DISMISSED` alerts, render read-only email bodies via `FormattedEmailBody`, and replace `ROP/S` → `ROP/T`.
  3. Update `SuppliersPlanningPage.tsx`: show `Cycles (90d)` & `Avg Response (days)`, `α = 0.3` note, and replace `ROP/S` → `ROP/T`.
  4. Update `AIHealthPage.tsx` to display `tool_calls` (tool name, args, sandbox output, reminder scheduling) cleanly in expandable trace cards.

---

### Slice P3-8 — Final Phase 3 Verification & Documentation Parity Gate
- **Read Only:** All modified Phase 3 files + `docs/phase3/*`, `STATUS.md`, `README.md`.
- **Tasks:**
  1. Run full offline `uv run pytest -v`, `uv run ruff check backend tests`, `cd frontend && npm run build`.
  2. Run `RUN_LIVE_GEMINI=1 uv run pytest -v tests/agents/` and `POST /api/evals/lex/run` (`mode: "live"`).
  3. Execute full headless Playwright/Chromium browser walkthrough across every acceptance criterion in `PRD_P3.md §3`.
  4. Sync `docs/phase3/STATUS_P3.md`, `STATUS.md`, and `README.md` with exact verified outputs.

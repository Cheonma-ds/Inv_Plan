# STATUS_P2.md — Phase 2 Live Session Baton

**Current Stage:** Slices P2-1 through P2-5 Complete (Ready for **Slice P2-6**)  
**Last Updated:** 2026-11-21  
**Baseline Reference:** Phase 1 MVP Complete (Slices 1–5 in [`STATUS.md`](../../STATUS.md))  
**Planning Docs:** [`PRD_P2.md`](PRD_P2.md) · [`DESIGN_P2.md`](DESIGN_P2.md) · [`SLICES_P2.md`](SLICES_P2.md)

---

## Slice Progress Tracker

- [x] **Slice P2-1:** Apple HIG Design System, Calm Palette, Smooth Animations & Wider Horizontal Layout
- [x] **Slice P2-2:** Universal `DataTable` (Dual-Axis Scroll, Frozen Title Case Headers, Zero-Icon Hover Tooltips, Resizable Columns)
- [x] **Slice P2-3:** Backend Core Schema, Idempotent SQLite Migrations, 7 Personas (`making`, `procurement`) & PO Logistics Fields + Seed Fix
- [x] **Slice P2-4:** Backend Making-Team Stock Request Pipeline & Lex Analysis
- [x] **Slice P2-5:** Frontend Mailroom (7 Personas), Remove `Withdraw` from Inventory & Stock-Request Alerts
- [ ] **Slice P2-6:** Backend PO Logistics Triage, 2-Day Deferred SLA, Mandatory Lex Action Alerts & Procurement Escalation
- [ ] **Slice P2-7:** Frontend Horizontal Order Tracking Stepper, Logistics Card & `Escalate to Procurement` UI

---

## Completed in Phase 2

### Slice P2-1 — Apple HIG Design System, Calm Palette, Smooth Animations & Wider Layout
- **Design Tokens & Motion (`frontend/src/styles/theme.css`, `frontend/src/index.css`, `frontend/index.html`):**
  - Created `frontend/src/styles/theme.css` with Apple-inspired CSS variables (`--bg-canvas: #F5F5F7`, `--surface-card: #FFFFFF`, `--surface-glass: rgba(255, 255, 255, 0.86)` with `backdrop-filter: blur(12px)`, hairline borders `rgba(0, 0, 0, 0.08)`, 3-tier ink `#1D1D1F` / `#6E6E73` / `#86868B`, calm accent `#0066CC` / `#0071E3`, soft semantic tokens, and exclusive Lex AI violet-indigo `#5E5CE6`).
  - Added smooth spring keyframes (`fadeSlideUp`, `modalPop`, `drawerSlide`, `backdropFade`, `toastSlideIn`) using `180ms–260ms cubic-bezier(0.22, 1, 0.36, 1)` and `@media (prefers-reduced-motion: reduce)` collapse rules.
  - Wired `theme.css`, Apple-first system font stack, antialiasing, and `.tabular-nums` in `frontend/src/index.css` and `frontend/index.html`.
- **Workspace Expansion & Chrome (`frontend/src/App.tsx`, `frontend/src/components/ClockBar.tsx`, `frontend/src/components/ToastContainer.tsx`, `frontend/src/utils/format.ts`):**
  - Expanded `<main>` in `App.tsx` from `max-w-7xl` to `max-w-[1640px] w-full mx-auto` with `px-5 md:px-7 py-5`, frosted sidebar (`bg-white/80 backdrop-blur-xl`), calm `#0066CC` active nav pills, and `anim-page-enter` view transitions.
  - Styled `ClockBar.tsx` with translucent frosted header (`bg-white/82 backdrop-blur-xl`) and pill controls; updated `ToastContainer.tsx` with frosted surfaces and `anim-toast-in`; refined `format.ts` semantic badge classes.
- **Page Polish (`frontend/src/pages/{AlertsPage,InventoryPage,POTrackerPage,MailroomPage,SuppliersPlanningPage,AIHealthPage}.tsx`):**
  - Applied calm surface/button/badge hierarchy, `tabular-nums`, and smooth modal/drawer/accordion transitions while preserving all existing table layouts (`P2-2`), `Withdraw` flow (`P2-5`), and `"5-Persona Mailroom"` / 5-persona labels (`P2-5`).
- **Verification:**
  - `cd frontend && npm run build` (`tsc -b && vite build`) passed with 0 errors; confirmed `--bg-canvas`, `fadeSlideUp`, `modalPop`, `drawerSlide`, `prefers-reduced-motion`, and `max-w-[1640px]` compiled into `frontend/dist/assets/index-*.css`.
  - `uv run pytest` (`69 passed, 1 skipped`) and `uv run ruff check backend tests` (`All checks passed!`) confirmed zero backend regressions.

### Slice P2-2 — Universal `DataTable` (Dual-Axis Scroll, Frozen Title Case Headers, Zero-Icon Hover Tooltips, Resizable Columns)
- **Reusable `DataTable` Package (`frontend/src/components/DataTable/`):**
  - `types.ts`: Strict `ColumnDef<T>`, `ColumnAlignment`, and `DataTableProps<T>` interfaces (`key`, `header`, `tooltip`, `defaultWidth`, `minWidth`, `align`, `renderCell`, `headerClassName`, `cellClassName`).
  - `useColumnResize.ts`: Pointer-event drag resizing (`startResize`), per-column `minWidth` clamping, `localStorage` persistence (`storageKey`), double-click single-column reset (`resetColumn`), and full reset (`resetAllColumns`).
  - `HeaderTooltip.tsx`: Pure-hover / focus portal tooltip rendered via `createPortal(..., document.body)` with `position: fixed` anchored to `<th>` `getBoundingClientRect()` (viewport-clamped, `pointer-events-none`, zero clipping by `overflow-auto`) and **zero icons** inside `<th>`.
  - `DataTable.tsx` & `index.ts`: Bounded vertical + horizontal scroll container (`overflow-auto overscroll-contain`, configurable `maxHeight`), `table-fixed` + `<colgroup>` width binding, `border-separate border-spacing-0` sticky `<thead>` (`sticky top-0 z-10 bg-[#FBFBFD]/95 backdrop-blur-md`), Title Case headers with no `uppercase` transform, subtle hover-revealed right-edge `col-resize` dividers with double-click reset, and a conditional `Reset column widths` control when widths are customized.
- **Page Migrations:**
  - `frontend/src/pages/InventoryPage.tsx`: Migrated parts table to `<DataTable>` (`invplan_col_widths_inventory`, `maxHeight="calc(100vh - 280px)"`) with Title Case headers (`Part`, `On-Hand`, `Quarantined`, `On-Order`, `Inventory Position`, `Reorder Point / Target`, `Demand Override`, `Primary & Backup Suppliers`, `Actions`) and hover tooltips (including `CRITICAL` / `STANDARD` / `LOW` tiers and `Inventory Position` pipeline guard). Preserved `Withdraw` button/modal for Slice P2-5.
  - `frontend/src/pages/POTrackerPage.tsx`: Migrated PO table to `<DataTable>` (`invplan_col_widths_po_tracker`, `maxHeight="calc(100vh - 280px)"`) with Title Case headers (`PO Number`, `Part`, `Supplier`, `Quantity`, `Unit Price`, `Total Value`, `Status`, `Promised / Revised ETA`) and hover tooltips. Preserved slide-over drawer for Slice P2-7.
  - `frontend/src/pages/AIHealthPage.tsx`: Migrated Autonomy Policies table to embedded `<DataTable>` (`invplan_col_widths_ai_health`, `maxHeight="380px"`) with Title Case headers (`Category`, `Policy Mode`, `Approved As-Is`, `Approved Edited`, `Rejected`, `Edit-Free %`, `Trust Distribution`) and hover tooltips.
- **Verification:**
  - `cd frontend && npm run build` (`tsc -b && vite build`) passed with 0 errors.
  - Headless Chromium CDP browser verification across all 3 pages confirmed bounded dual-axis scrolling, sticky frozen Title Case headers (`textTransform !== 'uppercase'`, 0 icons inside `<th>`), `document.body` fixed-position hover tooltips, pointer-drag column resizing with `localStorage` persistence, and double-click width reset.

### Slice P2-3 — Backend Core Schema, Migrations, 7 Personas & PO Logistics Fields + Seed Fix
- **Core Enums & Models (`backend/core/enums.py`, `backend/core/models.py`):**
  - Added `PersonaKey.MAKING` (`"making"`) and `PersonaKey.PROCUREMENT` (`"procurement"`).
  - Added Phase 2 `EmailCategory` values (`STOCK_REQUEST_CLARIFICATION`, `STOCK_REQUEST_RESPONSE`, `DISPATCH_DETAILS_REQUEST`, `DISPATCH_DETAILS_NUDGE`, `PROCUREMENT_ESCALATION`).
  - Added `DispatchDetailsStatus` and `StockRequestStatus` enums.
  - Added 11 additive columns on `PurchaseOrder` (`acknowledged_at`, `promised_dispatch_date`, `actual_dispatch_date`, `carrier_name`, `tracking_number`, `dispatch_details_status`, `dispatch_details_due_at`, `missing_dispatch_fields_json`, `escalated_to_procurement_at`, `procurement_escalation_note`, `closed_at`).
  - Added the `StockRequest` ORM table (`stock_requests`).
- **Safe Idempotent Migrations (`backend/core/migrations.py`, `backend/core/db.py`):**
  - Added `ensure_schema_migrations()` called from `init_db()` to inspect SQLite `PRAGMA table_info(purchase_orders)` and idempotently `ALTER TABLE ... ADD COLUMN` for pre-Phase-2 databases.
- **PO State Machine & Logistics Helpers (`backend/po/state_machine.py`, `backend/po/logistics.py`, `backend/po/__init__.py`):**
  - `transition_po()` now records `acknowledged_at` on first transition to `ACKNOWLEDGED` and `closed_at` on `CLOSED`.
  - Added `backend/po/logistics.py` (`LogisticsUpdateResult`, `compute_missing_dispatch_fields`, `apply_po_logistics_updates`) strictly within `backend/po/` (zero sideways imports).
- **7-Persona Mailroom Transport (`backend/mailroom/transport.py`):**
  - Updated `DatabaseEmailTransport` with `INBOUND_PERSONAS = SUPPLIER_PERSONAS | {PersonaKey.MAKING.value}` so `making` (`INBOUND`/`RECEIVED`) and `procurement` (`OUTBOUND`/`SENT`) route cleanly through `EmailTransport`.
- **Seed Data Lifecycle Separation (`backend/seed/data.py`):**
  - Seeded distinct `created_at` < `sent_at` across all 36 historical closed POs and both in-flight POs (`PO-1037`, `PO-1038`), plus `acknowledged_at`, `promised_dispatch_date`, `actual_dispatch_date`, `carrier_name`, `tracking_number`, and `closed_at`.
- **Tests & Verification:**
  - Added `tests/core/test_migrations.py`, `tests/po/test_logistics.py`, `test_making_and_procurement_personas_mailroom_routing` in `tests/mailroom/test_mailroom.py`, and lifecycle/logistics assertions in `tests/seed/test_seed_and_clock.py`.
  - Updated `tests/orchestrator/test_workflows_and_sla.py` (`len(autonomy_resp.json()) == len(EmailCategory)` instead of hardcoded `8`, matching `tests/seed/test_seed_and_clock.py`).
  - Verified with `uv run pytest -v` (`69 passed, 1 skipped`) and `uv run ruff check backend tests` (`All checks passed!`).

### Slice P2-4 — Backend Making-Team Stock Request Pipeline & Lex Analysis
- **Lex Schemas, Prompts & Helper (`backend/agents/lex/schemas.py`, `backend/agents/lex/prompts.py`, `backend/agents/lex/stock_request.py`, `backend/agents/lex/agent.py`, `backend/agents/lex/__init__.py`, `backend/agents/lex/eval_adapter.py`):**
  - Added flat JSON-schema-safe `StockRequestIntent`, `StockRequestItem`, `StockPartContext`, `PendingStockRequestContext`, `StockRequestTriageInput`, and `StockRequestTriageResult`, plus `STOCK_REQUEST_TRIAGE_PROMPT` and drafting guidance for `STOCK_REQUEST_CLARIFICATION` / `STOCK_REQUEST_RESPONSE`.
  - Created `backend/agents/lex/stock_request.py` implementing `run_stock_request_triage()` with `AgentTracer.span(..., task_type="stock_request_triage")` and `< 0.85` confidence / missing-qty / unknown-or-ambiguous-SKU escalation guardrails.
  - Exposed `LexAgent.triage_stock_request()`, updated `LexAgent.draft_email()` subject handling when `po_number` is empty, and added `stock_request_triage` execution in `LexEvalExecutor`.
- **Orchestrator Stock-Request Workflow & Mailroom Validation (`backend/orchestrator/stock_request_workflow.py`, `backend/orchestrator/workflows.py`, `backend/orchestrator/routes/mailroom.py`):**
  - Created `backend/orchestrator/stock_request_workflow.py` handling Making-team inbound email & `MINIMUM_QTY_CONFIRMATION` triage, deterministic `PartSnapshot` + active open PO + primary/backup supplier + ROP impact analysis, `StockRequest` lifecycle, Scenarios S1–S5 alerts and `DRAFT_PENDING_APPROVAL` clarification drafts with zero premature stock deduction, and Manager CTAs (`approve_stock_withdrawal`, `issue_partial_stock`, `ask_making_minimum`, `reject_stock_request`).
  - Added `400 Bad Request` enforcement in `POST /api/mailroom/send` (`backend/orchestrator/routes/mailroom.py`) when `from_persona == "making"` sends to `"inventory_manager"` without `"lex"` in To/CC.
  - Wired thin delegations in `backend/orchestrator/workflows.py` (`send_mailroom_email`, `approve_lex_draft`, `execute_alert_cta`) preserving all Phase 1 signatures.
- **Evals & Tests (`backend/evals/lex.yaml`, `tests/orchestrator/test_stock_request_workflow.py`, `tests/observability/test_eval_runner_and_phoenix.py`, `tests/orchestrator/test_observability_routes.py`):**
  - Added 4 Making stock-request eval cases (`stock_request_clear_single_sku`, `stock_request_minimum_qty_confirmation`, `stock_request_low_confidence_guardrail`, `stock_request_unknown_sku_guardrail`) to `backend/evals/lex.yaml` (20 total cases, 100% offline pass rate).
  - Created `tests/orchestrator/test_stock_request_workflow.py` verifying HTTP `400` CC `lex` rule, Scenarios S1–S5, `< 0.85` / unknown SKU zero-mutation guardrails, and delayed stock deduction strictly upon Manager CTA approval.
- **Verification:**
  - `uv run pytest -v` (`75 passed, 1 skipped` offline; `RUN_LIVE_GEMINI=1 uv run pytest -v` **78 passed, 0 skipped** including `tests/agents/test_live_gemini_pdf.py` and `tests/agents/test_live_gemini_stock_request.py`) and `uv run ruff check backend tests` (`All checks passed!`).
  - Live `gemini-3.5-flash-lite` YAML eval suite: 4/4 `stock_request_triage` cases passed (`19/20` overall; the single Phase 1 `triage_unmatched_po_guardrail` miss occurred because Gemini itself returned `AMBIGUOUS` directly rather than needing post-LLM guardrail override).

### Slice P2-5 — Frontend Mailroom (7 Personas), Remove `Withdraw` from Inventory & Stock-Request Alerts
- **7-Persona Mailroom & Naming (`frontend/src/api/types.ts`, `frontend/src/utils/format.ts`, `frontend/src/App.tsx`, `frontend/src/pages/MailroomPage.tsx`):**
  - Expanded `PersonaKey` and `personaLabel()` with `'making'` (`Making Team`) and `'procurement'` (`Procurement`).
  - Renamed `"5-Persona Mailroom"` → `"Mailroom"` in `App.tsx` sidebar navigation and `MailroomPage.tsx` header. *(UI Naming Rule: keep the in-app label strictly `"Mailroom"`, never `"5-Persona Mailroom"` or `"7-Persona Mailroom"`.)*
  - Added `making` and `procurement` to `PERSONAS` switcher and To/CC recipient pills; auto-selects and locks `lex` in `CC` (with lock icon and explanatory note) whenever `making` composes or replies to `inventory_manager`, and surfaces stock-request alert counts in the send toast.
- **Remove Direct `Withdraw` (`frontend/src/pages/InventoryPage.tsx`):**
  - Removed the `Withdraw` action button, `consume` modal state, `withdrawQty`/`withdrawNote` fields, and `handleSubmitConsume`; updated the `Actions` tooltip and page subtitle to note that stock withdrawals arrive via Making Team Mailroom requests & Manager alert approvals.
- **Stock-Request Metrics & CTAs (`frontend/src/pages/AlertsPage.tsx`):**
  - Rendered Making-team stock request metrics from `cta_payload` (`Requested`, `On-Hand`, `Quarantined`, `Shortfall`, `Post-Issue` projection / `Inv. Position / ROP / S`, `Projected PO` & Primary Supplier terms, and clickable `Active POs` links) without mislabeling current `rop`/`replenishment_point` as `Recommended ROP / S`.
  - Styled and wired all 4 stock-request CTAs (`approve_stock_withdrawal`, `issue_partial_stock`, `ask_making_minimum`, `reject_stock_request`), forwarding inline edited `edited_subject` / `edited_body` on `ask_making_minimum` and reporting issued qty / new on-hand / created PO# in the execution toast.
- **Verification:**
  - `cd frontend && npm run build` (`tsc -b && vite build`) passed with 0 errors.
  - `uv run pytest` (`75 passed, 3 skipped`) and `uv run ruff check backend tests` (`All checks passed!`) confirmed zero backend regressions.
  - Headless Chromium Playwright E2E suite (against isolated temp SQLite + Vite dev server + deterministic offline fake LLM) verified: `"Mailroom"` sidebar & heading naming, 0 `Withdraw` buttons on `InventoryPage`, all 7 personas + `Procurement` compose/sent, `Making Team` Compose & Reply locking `Lex` in `CC`, zero premature stock deduction, Scenario S2 (`Approve Issue & Trigger PO`), Scenario S3 (`STOCK_SHORTAGE` metrics + inline-edited `ask_making_minimum` inquiry), Scenario S4 (`MINIMUM_QTY_CONFIRMATION` approval), Scenario S1 (`Reject Request`), `issue_partial_stock`, and 0 console/page errors.

---

## Next Slice Options
- **Backend PO Logistics Track (`Slice P2-6`):** PO logistics triage, 2-day deferred SLA, mandatory Lex action alerts, and Procurement escalation.
  - **Read First:** `AGENTS.md`, `docs/phase2/STATUS_P2.md`, `docs/phase2/PRD_P2.md §5`, `docs/phase2/DESIGN_P2.md §4.1`, `docs/phase2/SLICES_P2.md` (`Slice P2-6`).
  - **Leave Untouched:** `frontend/`, `backend/inventory/`, `backend/planning/`.
- **Frontend PO Tracker Stepper & Escalation Track (`Slice P2-7`, depends on `P2-6`):** Horizontal Order Tracking Stepper, Shipment & Logistics Details card, and `Escalate to Procurement` modal in `POTrackerPage.tsx`.

---

## Plan vs. Implementation Discrepancies
*(Per `AGENTS.md §5`, record any flaw or edge-case discrepancy uncovered in `PRD_P2.md` / `DESIGN_P2.md` during a slice here, along with proof and resolution.)*

| Slice | Original Plan Reference | Discrepancy / Proof | Resolution Implemented |
|---|---|---|---|
| P2-3 | `SLICES_P2.md` (`Slice P2-3` existing files) | `tests/orchestrator/test_workflows_and_sla.py:464` asserted `len(autonomy_resp.json()) == 8` (hardcoded Phase 1 `EmailCategory` count), failing once 5 new `EmailCategory` values were added (`13 == 8`). | Replaced hardcoded `8` with `len(EmailCategory)` (matching `tests/seed/test_seed_and_clock.py:117`). |
| P2-4 | `SLICES_P2.md` (`Slice P2-4` existing files) | `tests/observability/test_eval_runner_and_phoenix.py` and `tests/orchestrator/test_observability_routes.py` hardcoded Phase 1 `lex.yaml` count (`16`) and task-type set, failing once 4 `stock_request_triage` eval cases were added (`20 == 16`). | Updated both test files to assert dynamically against `len(suite.cases)` (`>= 20`) and include `stock_request_triage` in expected task types. |

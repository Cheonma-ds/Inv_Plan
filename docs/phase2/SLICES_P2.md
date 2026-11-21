# Phase 2 Phased Build Slices (`SLICES_P2.md`)

**Execution Philosophy:** Each slice is intentionally small (< 30% session context), touches the minimum set of existing files, adds focused single-purpose modules, preserves every Phase 1 MVP test, and ends with `uv run pytest`, `uv run ruff check backend tests`, and (when frontend changes) `cd frontend && npm run build`.

> **Discrepancy Protocol:** Coding agents must follow [`PRD_P2.md`](PRD_P2.md) and [`DESIGN_P2.md`](DESIGN_P2.md) faithfully. If any planned logic proves flawed or incompatible during implementation, **do not silently improvise**—document the discrepancy and proof in [`STATUS_P2.md`](STATUS_P2.md) and [`DESIGN_P2.md`](DESIGN_P2.md).

---

## Dependency Graph & Independence Map

```text
  [Slice P2-1] Apple Design System & Wider Layout     (Frontend-only, Independent)
       │
       ▼
  [Slice P2-2] Universal DataTable Component          (Frontend-only, Independent of Backend)
       │                                              (Wires into Inventory, PO Tracker, AI Health)
       │
  [Slice P2-3] Core Schema, Migrations, Personas &    (Backend-only foundation; zero UI break)
               PO Logistics Fields + Seed Fix
       ├──────────────────────────┐
       ▼                          ▼
  [Slice P2-4]               [Slice P2-6]
  Backend Stock-Request      Backend PO Logistics Triage,
  Workflow (Making + Lex)    2-Day Wait SLA & Procurement
       │                     Escalation
       ▼                          │
  [Slice P2-5]                    ▼
  Frontend Mailroom (7p),    [Slice P2-7]
  Inventory Withdraw Removal Frontend Horizontal Stepper,
  & Stock-Request Alerts     Logistics Card & Escalate UI
```

*Notice that `P2-1` → `P2-2` (Frontend UI/Table foundation) and `P2-3` (Backend schema/seed foundation) are completely independent and can be built cleanly without cross-layer churn; similarly `P2-4`/`P2-5` (Making Stock Requests) and `P2-6`/`P2-7` (PO Logistics & Escalation) are decoupled vertical tracks!*

---

## Slice Overview Table

| Slice | Goal | New Small Files Created | Existing Files Touched (Minimal) | Verification Gate |
|---|---|---|---|---|
| **P2-1** | Apple HIG Design System, Calm Palette, Smooth Animations & Wider Horizontal Margins | `frontend/src/styles/theme.css` | `frontend/src/index.css`, `frontend/src/App.tsx`, `frontend/src/components/ClockBar.tsx`, `frontend/src/components/ToastContainer.tsx`, `frontend/src/utils/format.ts`, `frontend/src/pages/SuppliersPlanningPage.tsx` | `npm run build`; visual layout check across all 6 tabs verifying wider workspace and smooth transitions. |
| **P2-2** | Universal Scrollable, Frozen Title Case Header, Zero-Icon Tooltip, Resizable `DataTable` | `frontend/src/components/DataTable/{types.ts, useColumnResize.ts, HeaderTooltip.tsx, DataTable.tsx}` | `frontend/src/pages/InventoryPage.tsx`, `frontend/src/pages/POTrackerPage.tsx`, `frontend/src/pages/AIHealthPage.tsx` | `npm run build`; browser check verifying table-only vertical/horizontal scrolling, frozen Title Case headers, portal hover tooltips without icons, and width drag/persistence. |
| **P2-3** | Safe SQLite Migrations, `making`/`procurement` Personas, `PurchaseOrder` Logistics Fields, `StockRequest` Model & Seed Timestamps | `backend/core/migrations.py`, `backend/po/logistics.py`, `tests/core/test_migrations.py`, `tests/po/test_logistics.py` | `backend/core/enums.py`, `backend/core/models.py`, `backend/core/db.py`, `backend/po/state_machine.py`, `backend/po/__init__.py`, `backend/mailroom/transport.py`, `backend/seed/data.py` | `uv run pytest` (existing 65+ tests + new migration/logistics/mailroom persona tests) & `ruff` clean. |
| **P2-4** | Backend Making-Team Stock Request Pipeline & Lex Analysis | `backend/agents/lex/stock_request.py`, `backend/orchestrator/stock_request_workflow.py`, `tests/orchestrator/test_stock_request_workflow.py` | `backend/agents/lex/schemas.py`, `backend/agents/lex/prompts.py`, `backend/agents/lex/agent.py`, `backend/agents/lex/__init__.py`, `backend/orchestrator/workflows.py`, `backend/orchestrator/routes/mailroom.py`, `backend/evals/lex.yaml` | `uv run pytest`, `ruff`; tests prove `making -> inventory_manager` requires CC `lex` (`400` otherwise), scenarios S1–S5, `<0.85` guardrail, and stock updates only after Manager approval. |
| **P2-5** | Frontend 7-Persona Mailroom, Remove `Withdraw` from Inventory & Stock-Request Alerts | *(None — small targeted UI updates)* | `frontend/src/api/types.ts`, `frontend/src/utils/format.ts`, `frontend/src/App.tsx`, `frontend/src/pages/MailroomPage.tsx`, `frontend/src/pages/InventoryPage.tsx`, `frontend/src/pages/AlertsPage.tsx` | `npm run build`; E2E check: `Withdraw` removed, `Mailroom` shows 7 personas, `Making Team` auto-CCs `Lex`, Manager approves request from `AlertsPage` and sees `InventoryPage` deduct stock. |
| **P2-6** | Backend PO Logistics Triage, 2-Day Deferred SLA, Mandatory Lex Action Alerts & Procurement Escalation | `backend/orchestrator/logistics_workflow.py`, `backend/orchestrator/escalation_workflow.py`, `tests/orchestrator/test_logistics_and_escalation.py` | `backend/agents/lex/schemas.py`, `backend/agents/lex/prompts.py`, `backend/po/service.py`, `backend/po/__init__.py`, `backend/orchestrator/workflows.py`, `backend/orchestrator/sla_watcher.py`, `backend/orchestrator/routes/pos.py`, `backend/evals/lex.yaml` | `uv run pytest`, `ruff`; tests prove complete/missing/deferred ack flows, `+2d` nudge SLA, follow-up carrier/tracking updates with Manager alerts, and `POST /api/pos/{id}/escalate-procurement` delivering to `procurement` inbox. |
| **P2-7** | Frontend Horizontal Order Tracking Stepper, Logistics Details & `Escalate to Procurement` UI | `frontend/src/components/OrderTrackingStepper.tsx`, `frontend/src/components/ProcurementEscalationModal.tsx` | `frontend/src/api/types.ts`, `frontend/src/api/client.ts`, `frontend/src/pages/POTrackerPage.tsx` | `npm run build` + full Phase 2 E2E walkthrough covering all 6 acceptance criteria in `PRD_P2.md §6`. |

---

## Detailed Slice Specifications (Ready for Fresh Coding Sessions)

### Slice P2-1 — Apple Design System, Calm Palette, Smooth Animations & Wider Layout
- **Read Only:** `AGENTS.md`, `docs/phase2/STATUS_P2.md`, `docs/phase2/PRD_P2.md §2`, `docs/phase2/DESIGN_P2.md §3.1`.
- **Leave Untouched:** All `backend/` files, all `tests/` files, table scroll/resize logic (handled in `P2-2`), persona lists & `Withdraw` logic (handled in `P2-5`).
- **Build:**
  1. Create `frontend/src/styles/theme.css` with Apple-inspired color tokens (`--bg-canvas: #F5F5F7`, `--surface-card`, `--surface-glass`, `--ink-primary: #1D1D1F`, `--ink-secondary: #6E6E73`, `--accent: #0066CC`), calm status colors, keyframes (`fadeSlideUp`, `modalPop`, `drawerSlide`), and `prefers-reduced-motion` support.
  2. Wire `theme.css` in `frontend/src/index.css`.
  3. Expand `<main>` container in `App.tsx` (remove tight `max-w-7xl` margins → `max-w-[1640px] w-full mx-auto`), refine sidebar glass/hairline borders, rename `"5-Persona Mailroom"` or keep label transition clean (note: renaming tab label can happen here or in `P2-5`—let's do Apple polish here and functional persona changes in `P2-5`, or cleanly rename `"Mailroom"` in `P2-5`).
  4. Apply calm visual tokens and smooth transitions to `ClockBar.tsx`, `ToastContainer.tsx`, `format.ts`, and page cards (`AlertsPage`, `SuppliersPlanningPage`, `AIHealthPage`, `POTrackerPage`, `InventoryPage`, `MailroomPage`).
- **Acceptance:** `cd frontend && npm run build` passes with zero TS/Vite errors; visual width expands smoothly on wide screens with zero horizontal overflow on page wrapper.

---

### Slice P2-2 — Universal `DataTable` (Dual-Axis Scroll, Frozen Title Case Headers, Hover Tooltips, Resizable Columns)
- **Read Only:** `AGENTS.md`, `docs/phase2/STATUS_P2.md`, `docs/phase2/PRD_P2.md §3`, `frontend/src/pages/{InventoryPage,POTrackerPage,AIHealthPage}.tsx`.
- **Leave Untouched:** All `backend/` files, `MailroomPage.tsx`, `Withdraw` modal behavior (kept until `P2-5`!).
- **Build:**
  1. Create `frontend/src/components/DataTable/`:
     - `types.ts`: Column config interface (`key`, `header`, `tooltip`, `defaultWidth`, `minWidth`, `align`, `renderCell`, `headerClassName`, `cellClassName`).
     - `useColumnResize.ts`: Pointer-event drag resizing + `minWidth` clamp + `localStorage` persistence + double-click reset.
     - `HeaderTooltip.tsx`: Fixed/portal tooltip rendered into `document.body` (or root overlay) on hover/focus of `<th>` label, with **zero icons** inside `<th>`.
     - `DataTable.tsx`: Bounded scroll box (`overflow-auto`, configurable `maxHeight`), sticky `<thead>` (`top-0 z-10`), `table-fixed` + `<colgroup>` widths, Title Case column header rendering (never `uppercase`), subtle hover drag divider on right border of `<th>`.
  2. Migrate all 3 existing tables to `<DataTable>`:
     - `InventoryPage.tsx` (every column has descriptive hover tooltip—e.g. `Part` explains `LOW` / `STANDARD` / `CRITICAL`).
     - `POTrackerPage.tsx` (`PO Number`, `Part`, `Supplier`, `Quantity`, `Unit Price`, `Total Value`, `Status`, `Promised / Revised ETA`).
     - `AIHealthPage.tsx` (Autonomy Policies table: `Category`, `Policy Mode`, `Approved As-Is`, `Approved Edited`, `Rejected`, `Edit-Free %`, `Trust Distribution`).
- **Acceptance:** `cd frontend && npm run build` passes; scrolling inside any table scrolls only table rows while `<thead>` stays frozen; horizontal scroll works cleanly; hovering any column header shows description with zero tooltip icons; column edges drag-resize smoothly.

---

### Slice P2-3 — Backend Core Schema, Idempotent SQLite Migrations, 7 Personas & PO Logistics Fields
- **Read Only:** `AGENTS.md`, `docs/phase2/STATUS_P2.md`, `docs/phase2/DESIGN_P2.md §2–§3.2`, `backend/core/{enums,models,db}.py`, `backend/po/{state_machine,service}.py`, `backend/mailroom/transport.py`, `backend/seed/data.py`.
- **Leave Untouched:** `frontend/`, `backend/agents/`, `backend/orchestrator/workflows.py`, `backend/orchestrator/sla_watcher.py`.
- **Build:**
  1. Update `backend/core/enums.py`: add `PersonaKey.MAKING`, `PersonaKey.PROCUREMENT`, new `EmailCategory` members, `DispatchDetailsStatus`, `StockRequestStatus`.
  2. Update `backend/core/models.py`: add `PurchaseOrder` logistics/tracking/escalation/closed columns (`acknowledged_at`, `promised_dispatch_date`, `actual_dispatch_date`, `carrier_name`, `tracking_number`, `dispatch_details_status`, `dispatch_details_due_at`, `missing_dispatch_fields_json`, `escalated_to_procurement_at`, `procurement_escalation_note`, `closed_at`) and new `StockRequest` ORM table.
  3. Create `backend/core/migrations.py` (`ensure_schema_migrations(engine)`) and hook into `backend/core/db.py::init_db()`.
  4. Update `backend/po/state_machine.py` (`acknowledged_at`, `closed_at`) and create `backend/po/logistics.py` (update logistics fields, compute missing dispatch fields, manage `dispatch_details_status` / `dispatch_details_due_at`). Export from `backend/po/__init__.py`.
  5. Update `backend/mailroom/transport.py` so `making` (`INBOUND`/`RECEIVED`) and `procurement` (`OUTBOUND`/`SENT`) route cleanly through `DatabaseEmailTransport`.
  6. Update `backend/seed/data.py` so historical and live in-flight POs have distinct `created_at`, `sent_at`, `acknowledged_at`, `promised_dispatch_date`, `actual_dispatch_date`, `carrier_name`, `tracking_number`, and `closed_at`.
  7. Add `tests/core/test_migrations.py`, `tests/po/test_logistics.py`, and extend `tests/mailroom/test_mailroom.py` for 7 personas.
- **Acceptance:** `uv run pytest` passes 100% of Phase 1 tests + new Phase 2 core/PO/mailroom tests; `uv run ruff check backend tests` passes.

---

### Slice P2-4 — Backend Making-Team Stock Request Pipeline & Lex Analysis
- **Read Only:** `AGENTS.md`, `docs/phase2/STATUS_P2.md`, `docs/phase2/PRD_P2.md §4.3`, `docs/phase2/DESIGN_P2.md §4.2`, `backend/agents/lex/{schemas,prompts,agent}.py`, `backend/orchestrator/workflows.py` (lines 220–345 & 1696–1872), `backend/orchestrator/routes/mailroom.py`.
- **Leave Untouched:** `frontend/`, `backend/inventory/`, `backend/planning/`, `backend/po/matching.py`, `backend/orchestrator/sla_watcher.py`.
- **Build:**
  1. Add `StockRequestItem`, `StockRequestIntent`, `StockRequestTriageInput`, `StockRequestTriageResult` to `backend/agents/lex/schemas.py`, plus `STOCK_REQUEST_TRIAGE_PROMPT` in `prompts.py`.
  2. Create `backend/agents/lex/stock_request.py` implementing `triage_stock_request()` with `AgentTracer.span` and `< 0.85` confidence escalation guardrail; expose on `LexAgent`.
  3. Create `backend/orchestrator/stock_request_workflow.py` handling:
     - Parsing `making` email (and follow-up `MINIMUM_QTY_CONFIRMATION` replies),
     - Deterministic `PartSnapshot` + active PO + primary/backup supplier analysis,
     - Multi-scenario alert + draft generation (`S1`–`S5` in `PRD_P2.md §4.3`) with **zero** premature stock deduction,
     - Manager CTA handlers (`approve_stock_withdrawal`, `issue_partial_stock`, `ask_making_minimum`, `reject_stock_request`) calling `engine.withdraw_stock()` upon approval.
  4. Wire `making` CC `lex` validation (`400` if missing `lex` when sending to `inventory_manager`) in `routes/mailroom.py`, route `making` inbound emails in `workflows.py::send_mailroom_email()`, and route new CTA actions in `workflows.py::execute_alert_cta()`.
  5. Add Making stock-request eval cases to `backend/evals/lex.yaml` and comprehensive workflow tests in `tests/orchestrator/test_stock_request_workflow.py`.
- **Acceptance:** `uv run pytest` (all existing + new stock request tests + YAML offline evals pass) & `uv run ruff check backend tests` clean.

---

### Slice P2-5 — Frontend Mailroom (7 Personas), Inventory `Withdraw` Removal & Stock-Request Alerts
- **Read Only:** `AGENTS.md`, `docs/phase2/STATUS_P2.md`, `docs/phase2/PRD_P2.md §4`, `frontend/src/{App.tsx,api/types.ts,utils/format.ts}`, `frontend/src/pages/{MailroomPage,InventoryPage,AlertsPage}.tsx`.
- **Leave Untouched:** `backend/`, `POTrackerPage.tsx`, `SuppliersPlanningPage.tsx`, `AIHealthPage.tsx`.
- **Build:**
  1. Update `PersonaKey` in `frontend/src/api/types.ts` and `personaLabel()` in `utils/format.ts` with `making` (`Making Team`) and `procurement` (`Procurement`).
  2. Rename `"5-Persona Mailroom"` → `"Mailroom"` in `App.tsx` and `MailroomPage.tsx`; add `making` and `procurement` to `PERSONAS` switcher + recipient buttons; auto-select and lock `lex` in `CC` when `making` composes to `inventory_manager`.
  3. Remove the **Withdraw** button, withdraw modal, and `handleSubmitConsume` from `InventoryPage.tsx` (adding a helpful subtle subtitle or tooltip note noting stock withdrawals arrive via Making Team email requests).
  4. Update `AlertsPage.tsx` to display Making-team stock-request analysis metrics (Requested Qty, On-Hand, Shortfall, Active Open POs, Reorder Trigger & Primary Supplier info) and style the new CTA actions (`approve_stock_withdrawal`, `issue_partial_stock`, `ask_making_minimum`, `reject_stock_request`).
- **Acceptance:** `cd frontend && npm run build` passes; browser check confirms `Withdraw` is gone from `Inventory & Parts`, Mailroom has all 7 personas, `Making Team` enforces `CC: Lex`, and Manager approval deducts stock on `InventoryPage`.

---

### Slice P2-6 — Backend PO Logistics Triage, 2-Day Deferred SLA, Mandatory Lex Action Alerts & Procurement Escalation
- **Read Only:** `AGENTS.md`, `docs/phase2/STATUS_P2.md`, `docs/phase2/PRD_P2.md §5`, `docs/phase2/DESIGN_P2.md §4.1`, `backend/agents/lex/{schemas,prompts}.py`, `backend/po/service.py`, `backend/orchestrator/{workflows,sla_watcher,routes/pos}.py`.
- **Leave Untouched:** `frontend/`, `backend/inventory/`, `backend/planning/`.
- **Build:**
  1. Extend `EmailTriageResult` (`schemas.py`) and `TRIAGE_SYSTEM_PROMPT` / `DRAFT_SYSTEM_PROMPT` (`prompts.py`) with `promised_dispatch_date`, `actual_dispatch_date`, `carrier_name`, `tracking_number`, `details_promised_later`, `details_promised_by_date` (plus `DISPATCH_DETAILS_REQUEST` / `DISPATCH_DETAILS_NUDGE` drafting rules).
  2. Add `escalate_po_to_procurement()` to `backend/po/service.py` (exported in `po/__init__.py`).
  3. Create `backend/orchestrator/logistics_workflow.py` and `backend/orchestrator/escalation_workflow.py`, wiring them into `workflows.py` and `sla_watcher.py`:
     - Supplier ack branches (`COMPLETE` / `AWAITING_IMMEDIATE` / `DEFERRED_2D`),
     - 2-day timer expiry in `sla_watcher.py` generating `DISPATCH_DETAILS_NUDGE` + `MISSING_DISPATCH_DETAILS_DUE` alert,
     - Follow-up supplier emails updating logistics fields and auto-resolving open missing-detail alerts,
     - **Mandatory Manager Alert (`ActionItem`) created for every autonomous Lex PO action** (ack, field update, 2-day wait, learned memory note),
     - Protect informational Lex notification alerts from unintended wipe-out inside `auto_resolve_po_alerts()`.
  4. Update `routes/pos.py` (`_serialize_po` exposing new logistics/escalation/closed timestamps + `POST /api/pos/{po_id}/escalate-procurement`).
  5. Add logistics triage cases to `backend/evals/lex.yaml` and comprehensive tests in `tests/orchestrator/test_logistics_and_escalation.py`.
- **Acceptance:** `uv run pytest` (all Phase 1 + Phase 2 tests + YAML offline evals pass) & `uv run ruff check backend tests` clean.

---

### Slice P2-7 — Frontend Horizontal Order Tracking Stepper, Logistics Card & `Escalate to Procurement` UI
- **Read Only:** `AGENTS.md`, `docs/phase2/STATUS_P2.md`, `docs/phase2/PRD_P2.md §5.1–§5.4`, `frontend/src/api/{types,client}.ts`, `frontend/src/pages/POTrackerPage.tsx`.
- **Leave Untouched:** `backend/`, `InventoryPage.tsx`, `MailroomPage.tsx`, `AIHealthPage.tsx`.
- **Build:**
  1. Update `frontend/src/api/types.ts` (`POListItem` / `PODetail` logistics, stepper timestamps, and escalation fields) and `frontend/src/api/client.ts` (`escalatePoToProcurement`).
  2. Create `frontend/src/components/OrderTrackingStepper.tsx` (left-to-right animated stepper: `Created` → `PO Sent` → `Acknowledged` → `Dispatched` → `Delivered` → `Closed`, showing dates, carrier/tracking badges, and exception/overdue/quarantine indicators).
  3. Create `frontend/src/components/ProcurementEscalationModal.tsx`.
  4. Update `frontend/src/pages/POTrackerPage.tsx`:
     - Replace flat Lifecycle Timeline box with `<OrderTrackingStepper />` + **Shipment & Logistics Details** card (`Promised Dispatch`, `Actual Dispatch`, `Carrier`, `Tracking Number`, `Waiting Status` if `DEFERRED_2D`/`AWAITING_IMMEDIATE`, `Escalated` badge if escalated).
     - Add **Escalate to Procurement** button in the PO detail drawer actions bar alongside **Switch to Backup Supplier**.
     - Optionally surface `Carrier` / `Tracking` / `Dispatch` summary columns in the PO Tracker `<DataTable>` (showing off horizontal scroll + resizable columns!).
- **Acceptance:** `cd frontend && npm run build`, `uv run pytest`, and `uv run ruff check backend tests` all pass; full Phase 2 end-to-end walkthrough matches `PRD_P2.md §6`.

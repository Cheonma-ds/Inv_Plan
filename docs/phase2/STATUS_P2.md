# STATUS_P2.md — Phase 2 Live Session Baton

**Current Stage:** Slices P2-1, P2-2 & P2-3 Complete (Ready for **Slice P2-4** or **Slice P2-6**)  
**Last Updated:** 2026-11-21  
**Baseline Reference:** Phase 1 MVP Complete (Slices 1–5 in [`STATUS.md`](../../STATUS.md))  
**Planning Docs:** [`PRD_P2.md`](PRD_P2.md) · [`DESIGN_P2.md`](DESIGN_P2.md) · [`SLICES_P2.md`](SLICES_P2.md)

---

## Slice Progress Tracker

- [x] **Slice P2-1:** Apple HIG Design System, Calm Palette, Smooth Animations & Wider Horizontal Layout
- [x] **Slice P2-2:** Universal `DataTable` (Dual-Axis Scroll, Frozen Title Case Headers, Zero-Icon Hover Tooltips, Resizable Columns)
- [x] **Slice P2-3:** Backend Core Schema, Idempotent SQLite Migrations, 7 Personas (`making`, `procurement`) & PO Logistics Fields + Seed Fix
- [ ] **Slice P2-4:** Backend Making-Team Stock Request Pipeline & Lex Analysis
- [ ] **Slice P2-5:** Frontend 7-Persona Mailroom, Remove `Withdraw` from Inventory & Stock-Request Alerts
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

---

## Next Slice Options
- **Backend Stock-Request Track (`Slice P2-4`):** Making-team stock request triage, deterministic stock/PO analysis, and Manager approval CTAs.
  - **Read First:** `AGENTS.md`, `docs/phase2/STATUS_P2.md`, `docs/phase2/PRD_P2.md §4.3`, `docs/phase2/DESIGN_P2.md §4.2`, `docs/phase2/SLICES_P2.md` (`Slice P2-4`).
  - **Leave Untouched:** `frontend/`, `backend/inventory/`, `backend/planning/`, `backend/po/matching.py`, `backend/orchestrator/sla_watcher.py`.
- **Backend PO Logistics Track (`Slice P2-6`):** PO logistics triage, 2-day deferred SLA, mandatory Lex action alerts, and Procurement escalation.
  - **Read First:** `AGENTS.md`, `docs/phase2/STATUS_P2.md`, `docs/phase2/PRD_P2.md §5`, `docs/phase2/DESIGN_P2.md §4.1`, `docs/phase2/SLICES_P2.md` (`Slice P2-6`).
  - **Leave Untouched:** `frontend/`, `backend/inventory/`, `backend/planning/`.

---

## Plan vs. Implementation Discrepancies
*(Per `AGENTS.md §5`, record any flaw or edge-case discrepancy uncovered in `PRD_P2.md` / `DESIGN_P2.md` during a slice here, along with proof and resolution.)*

| Slice | Original Plan Reference | Discrepancy / Proof | Resolution Implemented |
|---|---|---|---|
| P2-3 | `SLICES_P2.md` (`Slice P2-3` existing files) | `tests/orchestrator/test_workflows_and_sla.py:464` asserted `len(autonomy_resp.json()) == 8` (hardcoded Phase 1 `EmailCategory` count), failing once 5 new `EmailCategory` values were added (`13 == 8`). | Replaced hardcoded `8` with `len(EmailCategory)` (matching `tests/seed/test_seed_and_clock.py:117`). |

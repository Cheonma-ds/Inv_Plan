# Phase 2 Technical Design & Modular Build Strategy (`DESIGN_P2.md`)

**Document Status:** Phase 2 Engineering Design v1.0 (Aligned to [`PRD_P2.md`](PRD_P2.md) & [`AGENTS.md`](../../AGENTS.md))  
**Core Mandate:** Implement all Phase 2 capabilities through **small, independent, single-purpose modules** with **zero regression to Phase 1 MVP logic and tests**.

---

## 1. Preserving MVP Integrity & Enforcing Architectural Rules

### 1.1 Hard Rules That Remain Unchanged (`AGENTS.md §2` & `tests/test_architecture.py`)
1. **No sideways imports between domain modules:** `backend/inventory/`, `backend/po/`, `backend/mailroom/`, `backend/planning/`, and `backend/agents/` must **never** import from each other. `tests/test_architecture.py` enforces this via AST checks.
2. **Zero direct DB mutation by agents or `po/` crossing into `inventory/`:**
   - `backend/agents/lex/` only parses inputs and returns strict Pydantic models.
   - `backend/po/` only mutates PO/GRN/Invoice/ActionItem models.
   - `backend/orchestrator/` coordinates `mailroom/` → `lex/` → `po/` → `inventory/`.
3. **Simulated Clock everywhere:** Zero `datetime.now()` / `datetime.utcnow()` calls in backend business logic; always call `core.clock.now()`.
4. **Mailroom behind `EmailTransport`:** All mailroom reads, sends, draft approvals/rejections, and triage annotations stay behind `backend/mailroom/transport.py`.
5. **Agent-agnostic observability:** `backend/observability/` continues tracking all new Phase 2 Lex tasks (`stock_request_triage`, `dispatch_details_nudge`, etc.) via `AgentTracer.span(agent_name="lex", ...)` without touching Lex-specific code inside `observability/`.

### 1.2 Preventing `orchestrator/workflows.py` From Becoming a Monolith
Today, [`backend/orchestrator/workflows.py`](../../backend/orchestrator/workflows.py) is **1,872 lines** long. Dumping Phase 2's Making-Team stock requests, PO logistics updates, and Procurement escalation into that single file would bloat agent context windows and violate modular engineering practices.

**Phase 2 Decomposition Strategy:**
- Extract **focused, independent orchestrator workflow helpers** into small files under `backend/orchestrator/`:
  - `backend/orchestrator/stock_request_workflow.py` (~220–280 lines) — Making-team email triage, deterministic shortage/ROP analysis, `StockRequest` lifecycle, and Manager CTA execution (`approve_stock_withdrawal`, `issue_partial_stock`, `ask_making_minimum`, `reject_stock_request`).
  - `backend/orchestrator/logistics_workflow.py` (~180–240 lines) — PO dispatch/carrier/tracking extraction handling, missing-detail evaluation, 2-day deferred wait scheduling, mandatory Lex-action Manager alert creation, and ongoing thread field updates.
  - `backend/orchestrator/escalation_workflow.py` (~90–120 lines) — `Escalate to Procurement` validation, PO audit note/timestamp update, internal `inventory_manager -> procurement` mailroom dispatch, and Procurement alert creation.
- `WorkflowEngine` in `backend/orchestrator/workflows.py` delegates cleanly to these focused modules instead of growing inline, preserving every existing public `WorkflowEngine` method signature so all Phase 1 tests keep passing!

### 1.3 Preserving `backend/inventory/service.py` & Phase 1 Tests
- **Do NOT remove `consume_stock()`** in [`backend/inventory/service.py`](../../backend/inventory/service.py) or `WorkflowEngine.withdraw_stock()`. Both implement the exact deterministic inventory position check (`on_hand + on_order + quarantined < rop`), MOQ rounding, `InventoryTransaction(CONSUMPTION)`, and `AcceleratedBurnAlert`.
- Instead, in Phase 2:
  - The **UI** (`InventoryPage.tsx`) removes the direct **Withdraw** button and modal.
  - The **backend** `stock_request_workflow.py` invokes `WorkflowEngine.withdraw_stock()` **only after** `InventoryManager` approves a Making-Team stock request alert CTA.
  - Keep `POST /api/parts/{part_id}/consume` intact as an internal/legacy-compatible API route (documented as deprecated for direct UI consumption) so existing Phase 1 HTTP tests (`tests/orchestrator/test_workflows_and_sla.py::test_fastapi_routes_http_end_to_end`) do not unexpectedly break unless deliberately migrated in a future cleanup.

---

## 2. Safe SQLite Schema Evolution & Seed Strategy

### 2.1 Why `Base.metadata.create_all()` Alone Is Not Enough
Currently [`backend/core/db.py`](../../backend/core/db.py#L40-L44) runs `Base.metadata.create_all(...)`. That creates **new tables** (such as `stock_requests`), **but SQLite will NOT add new columns to an existing `purchase_orders` table** if `data/inv_plan.db` already exists on disk!

**Solution (`backend/core/migrations.py` — small dedicated file, ~70 lines):**
- Add `ensure_schema_migrations(engine)` called inside `init_db()` right after `Base.metadata.create_all(...)`.
- Uses `PRAGMA table_info(purchase_orders)` (and any other altered table) and executes idempotent `ALTER TABLE ... ADD COLUMN ...` statements for missing columns only.
- Works safely on both brand-new `:memory:` test DBs (where `create_all` already created the columns so `ALTER TABLE` is a no-op) and existing persistent `data/inv_plan.db` files.

### 2.2 New & Extended Database Models (`backend/core/models.py` & `enums.py`)

#### A. Enum Extensions (`backend/core/enums.py`)
- **`PersonaKey`:** Add:
  - `MAKING = "making"`
  - `PROCUREMENT = "procurement"`
- **`EmailCategory`:** Add:
  - `STOCK_REQUEST_CLARIFICATION = "STOCK_REQUEST_CLARIFICATION"` (Lex draft asking Making for minimum needed qty)
  - `STOCK_REQUEST_RESPONSE = "STOCK_REQUEST_RESPONSE"` (Optional status/rejection reply to Making)
  - `DISPATCH_DETAILS_REQUEST = "DISPATCH_DETAILS_REQUEST"` (Immediate draft asking supplier for missing dispatch date / carrier / tracking #)
  - `DISPATCH_DETAILS_NUDGE = "DISPATCH_DETAILS_NUDGE"` (2-day SLA follow-up draft when supplier promised details later)
  - `PROCUREMENT_ESCALATION = "PROCUREMENT_ESCALATION"` (Internal manager-to-procurement escalation email)
- **`DispatchDetailsStatus` (new `StrEnum`):**
  - `NONE = "NONE"`
  - `AWAITING_IMMEDIATE = "AWAITING_IMMEDIATE"`
  - `DEFERRED_2D = "DEFERRED_2D"`
  - `COMPLETE = "COMPLETE"`
  - `TIMED_OUT = "TIMED_OUT"`
- **`StockRequestStatus` (new `StrEnum`):**
  - `PENDING_REVIEW = "PENDING_REVIEW"`
  - `AWAITING_MIN_QTY = "AWAITING_MIN_QTY"`
  - `APPROVED = "APPROVED"`
  - `PARTIALLY_APPROVED = "PARTIALLY_APPROVED"`
  - `REJECTED = "REJECTED"`

#### B. `PurchaseOrder` Additive Columns (`backend/core/models.py`)
All nullable or safely defaulted so existing rows stay 100% valid:
- `acknowledged_at: Mapped[datetime | None]` — Timestamp supplier acknowledged the PO.
- `promised_dispatch_date: Mapped[datetime | None]` — Supplier's promised shipment dispatch date.
- `actual_dispatch_date: Mapped[datetime | None]` — Actual shipment dispatch date from supplier.
- `carrier_name: Mapped[str | None]` (`String(120)`) — Delivery service (`BlueDart`, `Delhivery`, `DTDC`, etc.).
- `tracking_number: Mapped[str | None]` (`String(120)`) — Courier tracking ID.
- `dispatch_details_status: Mapped[str]` (`String(32)`, default `"NONE"`).
- `dispatch_details_due_at: Mapped[datetime | None]` — 2-day wait deadline when supplier promises details later.
- `missing_dispatch_fields_json: Mapped[list[str]]` (`JSON`, default `list`).
- `escalated_to_procurement_at: Mapped[datetime | None]` — Timestamp of Procurement escalation.
- `procurement_escalation_note: Mapped[str]` (`Text`, default `""`).
- `closed_at: Mapped[datetime | None]` — Timestamp PO transitioned to `CLOSED`.

#### C. New Table: `StockRequest` (`backend/core/models.py`)
Tracks Making-team stock withdrawal requests across shortage / minimum-quantity turns:
- `id` (PK), `comm_id` (FK → `po_communications.id`), `thread_id` (`String(64)`), `part_id` (FK → `parts.id`), `requested_qty` (`Integer`), `approved_qty` (`Integer | None`), `status` (`String(32)` — `StockRequestStatus`), `reason` (`Text`), `analysis_json` (`JSON`), `created_at`, `resolved_at`.

*(Note: Seed `AutonomyPolicy` rows in `backend/seed/data.py` by iterating `for category in EmailCategory:` — because Phase 1 already loops over `EmailCategory`, adding new `EmailCategory` members automatically seeds their `APPROVAL_REQUIRED` autonomy policy rows on reset/seed! On pre-existing DBs, `observability/autonomy.py::record_approval_outcome()` already lazily creates missing policy rows!)*

### 2.3 Fixing Seed Timestamps (`backend/seed/data.py`)
- Update **36 historical closed POs** so `created_at` → `sent_at` (`+2h`) → `acknowledged_at` (`+6h..+24h`) → `promised_dispatch_date` (`promised_eta - 3d`) → `actual_dispatch_date` (`delivered_at - 3d`) → `closed_at` (`delivered_at + 2h`) and realistic `carrier_name` / `tracking_number` are populated.
- Update the **2 live in-flight POs** (`PO-1037`, `PO-1038`) so `created_at` and `sent_at` are distinct (`sent_at = created_at + 2h`), and `PO-1037` (`ACKNOWLEDGED`) has `acknowledged_at`, `promised_dispatch_date`, `carrier_name`, `tracking_number` (or clean realistic values) so the Horizontal Stepper looks authentic immediately after seed.

---

## 3. Modular File Map (Small, Single-Purpose Files)

### 3.1 Frontend Modular Architecture (`frontend/src/`)

| File Path | Status | Single Responsibility |
|---|---|---|
| `frontend/src/styles/theme.css` | **New** | Apple HIG-inspired CSS variables (calm alabaster canvas, translucent surfaces, 3-tier ink, calm steel blue & soft semantic badge tokens), smooth keyframes (`fadeSlideUp`, `modalPop`, `drawerSlide`), and `prefers-reduced-motion` guard. |
| `frontend/src/index.css` | Modify | Imports Tailwind + `./styles/theme.css` and sets base typography (`tabular-nums`, antialiasing). |
| `frontend/src/App.tsx` | Modify | Expands main content width (removes `max-w-7xl` constraint → `max-w-[1640px]`), renames nav `"5-Persona Mailroom"` → `"Mailroom"`, applies Apple-style sidebar/surface styling & smooth view transitions. |
| `frontend/src/components/ClockBar.tsx` | Modify | Translucent Apple-style top bar (`bg-white/80 backdrop-blur-xl`), calm pill controls, clean spacing. |
| `frontend/src/components/ToastContainer.tsx` | Modify | Soft frosted toast surfaces with smooth entrance/exit transitions. |
| `frontend/src/utils/format.ts` | Modify | Adds `making` (`Making Team`) and `procurement` (`Procurement`) to `personaLabel()`, refines calm semantic badge classes. |
| `frontend/src/components/DataTable/types.ts` | **New** | Strict TypeScript interfaces for `ColumnDef<T>` (`key`, `header`, `tooltip`, `defaultWidth`, `minWidth`, `align`, `render`). |
| `frontend/src/components/DataTable/useColumnResize.ts` | **New** | Pointer-drag column width hook + `localStorage` persistence + double-click reset. |
| `frontend/src/components/DataTable/HeaderTooltip.tsx` | **New** | Zero-icon pure-hover portal/fixed tooltip anchored to `<th>` `getBoundingClientRect()` so `overflow: auto` never clips it. |
| `frontend/src/components/DataTable/DataTable.tsx` | **New** | Bounded vertical + horizontal scroll container, sticky frozen Title Case headers, `<colgroup>` width binding, drag handles, empty-state handling. |
| `frontend/src/components/OrderTrackingStepper.tsx` | **New** | Reusable left-to-right Horizontal Order Tracking Stepper rendering milestone nodes (`Created` → `PO Sent` → `Acknowledged` → `Dispatched` → `Delivered` → `Closed`) with status-aware colors, dates, and smooth connector fill. |
| `frontend/src/components/ProcurementEscalationModal.tsx` | **New** | Compact modal for **Escalate to Procurement** on an open PO with reason input and confirmation. |
| `frontend/src/pages/InventoryPage.tsx` | Modify | Removes **Withdraw** button/modal; swaps raw `<table>` for `<DataTable>` with Title Case headers, hover tooltips (explaining `CRITICAL`/`STANDARD`/`LOW`, `Inventory Position`, etc.), and resizable columns. |
| `frontend/src/pages/POTrackerPage.tsx` | Modify | Swaps raw `<table>` for `<DataTable>`; integrates `<OrderTrackingStepper />`, Logistics & Carrier card (`Promised Dispatch`, `Actual Dispatch`, `Carrier`, `Tracking #`), and **Escalate to Procurement** CTA + modal. |
| `frontend/src/pages/MailroomPage.tsx` | Modify | Renames `"5-Persona Mailroom"` → `"Mailroom"`; supports all 7 personas; enforces `CC: lex` when `making` composes to `inventory_manager`; adds simple `Procurement` inbox experience. |
| `frontend/src/pages/AlertsPage.tsx` | Modify | Renders Making-team stock request analysis metrics (requested qty, on-hand, active POs, shortfall, recommended PO/supplier) and new stock-request CTAs (`Approve & Issue Stock`, `Approve Issue & Trigger PO`, `Issue Available On-Hand`, etc.) + Apple styling. |
| `frontend/src/pages/SuppliersPlanningPage.tsx` | Modify | Applies Apple card/surface polish and wider grid breathing room. |
| `frontend/src/pages/AIHealthPage.tsx` | Modify | Swaps Autonomy Policies `<table>` for `<DataTable>` with Title Case headers, tooltips, and resizable columns + Apple styling. |

### 3.2 Backend Modular Architecture (`backend/`)

| File Path | Status | Single Responsibility |
|---|---|---|
| `backend/core/enums.py` | Modify | Adds `MAKING`, `PROCUREMENT`, new `EmailCategory` members, `DispatchDetailsStatus`, `StockRequestStatus`. |
| `backend/core/models.py` | Modify | Adds additive logistics/escalation columns on `PurchaseOrder` and new `StockRequest` table. |
| `backend/core/migrations.py` | **New** | Idempotent SQLite `PRAGMA table_info` + `ALTER TABLE ADD COLUMN` runner invoked from `db.init_db()`. |
| `backend/core/db.py` | Modify | Calls `ensure_schema_migrations()` inside `init_db()`. |
| `backend/po/state_machine.py` | Modify | Sets `po.acknowledged_at` on first transition to `ACKNOWLEDGED`, `po.closed_at` on `CLOSED`. |
| `backend/po/logistics.py` | **New** | Pure PO-domain helpers for updating logistics fields (`promised_dispatch_date`, `actual_dispatch_date`, `carrier_name`, `tracking_number`), computing missing dispatch fields, and setting `dispatch_details_status` / `dispatch_details_due_at`. |
| `backend/po/service.py` | Modify | Exposes `escalate_po_to_procurement()` (PO-domain timestamp/note mutation only — zero mailroom imports!). |
| `backend/mailroom/transport.py` | Modify | Updates direction/status mapping (`INBOUND`/`RECEIVED` for suppliers + `making`; `OUTBOUND`/`SENT` for `inventory_manager`, `procurement`, `lex`) and allows all 7 valid `PersonaKey` values. |
| `backend/agents/lex/schemas.py` | Modify | Adds `promised_dispatch_date`, `actual_dispatch_date`, `carrier_name`, `tracking_number`, `details_promised_later`, `details_promised_by_date` to `EmailTriageResult`; adds `StockRequestItem`, `StockRequestIntent`, `StockRequestTriageInput`, `StockRequestTriageResult` (all flat JSON-schema-safe types with no `anyOf` bugs). |
| `backend/agents/lex/prompts.py` | Modify | Extends `TRIAGE_SYSTEM_PROMPT` & `DRAFT_SYSTEM_PROMPT` for logistics extraction/deferred-detail cues and new categories; adds `STOCK_REQUEST_TRIAGE_PROMPT`. |
| `backend/agents/lex/stock_request.py` | **New** | Small focused helper/method for `LexAgent.triage_stock_request()` with `AgentTracer.span` and `< 0.85` confidence guardrail. |
| `backend/agents/lex/agent.py` | Modify | Exposes `triage_stock_request()` delegating to `stock_request.py` and preserves `triage_email`, `draft_email`, `extract_invoice`. |
| `backend/orchestrator/stock_request_workflow.py` | **New** | Handles `making` inbound email triage, deterministic stock/PO/supplier impact computation, `StockRequest` creation, multi-scenario alerts/drafts, and Manager CTAs (`approve_stock_withdrawal`, `issue_partial_stock`, `ask_making_minimum`, `reject_stock_request`). |
| `backend/orchestrator/logistics_workflow.py` | **New** | Handles supplier ack missing-detail branching, 2-day deferred wait scheduling, ongoing thread logistics updates, and mandatory Manager `ActionItem` notifications on every Lex action. |
| `backend/orchestrator/escalation_workflow.py` | **New** | Handles `Escalate to Procurement` (`po.escalate_po_to_procurement` + `mailroom.send_email` to `procurement` + `PROCUREMENT_ESCALATION` alert). |
| `backend/orchestrator/workflows.py` | Modify | Thin routing additions: routes `making` emails (CC `lex`) to `stock_request_workflow`, delegates supplier ack/follow-up logistics to `logistics_workflow`, exposes `escalate_to_procurement`, and dispatches new CTA actions. |
| `backend/orchestrator/sla_watcher.py` | Modify | Evaluates `dispatch_details_due_at` when `dispatch_details_status == DEFERRED_2D` to draft `DISPATCH_DETAILS_NUDGE` + raise `MISSING_DISPATCH_DETAILS_DUE` alert; adjusts `auto_resolve_po_alerts` so informational Lex notification alerts are preserved cleanly. |
| `backend/orchestrator/routes/mailroom.py` | Modify | Enforces `400` if `making -> inventory_manager` omits `lex` in To/CC. |
| `backend/orchestrator/routes/pos.py` | Modify | Serializes new logistics/escalation/stepper timestamps on `GET /api/pos` & `GET /api/pos/{id}`; adds `POST /api/pos/{po_id}/escalate-procurement`. |
| `backend/seed/data.py` | Modify | Distinct staggered lifecycle timestamps + carrier/tracking on seeded POs. |

---

## 4. Detailed Logic & Contract Specifications

### 4.1 Missing Dispatch Details & 2-Day Wait State Machine
When a supplier email is triaged on an open PO:
1. **Extracted Logistics Fields (`EmailTriageResult`):**
   - `promised_dispatch_date: str | None` (`YYYY-MM-DD`)
   - `actual_dispatch_date: str | None` (`YYYY-MM-DD`)
   - `carrier_name: str | None`
   - `tracking_number: str | None`
   - `details_promised_later: bool = False`
   - `details_promised_by_date: str | None` (`YYYY-MM-DD`)
2. **Computing Missing Fields (`backend/po/logistics.py`):**
   - Dispatch date satisfied if `po.promised_dispatch_date is not None or po.actual_dispatch_date is not None`.
   - Carrier satisfied if `bool(po.carrier_name and po.carrier_name.strip())`.
   - Tracking satisfied if `bool(po.tracking_number and po.tracking_number.strip())`.
   - Note: If supplier acknowledges cleanly (`ACKNOWLEDGED`) without shipping yet, missing `promised_dispatch_date`, `carrier_name`, or `tracking_number` are flagged in `missing_dispatch_fields_json`.
3. **Branching:**
   - **Zero missing fields:** `dispatch_details_status = COMPLETE`, `dispatch_details_due_at = None`. Auto-resolve any open `MISSING_DISPATCH_DETAILS*` / `DISPATCH_DETAILS_DEFERRED` alerts; create `INFO` `PO_ACKNOWLEDGED_COMPLETE` (or `PO_LOGISTICS_UPDATED`) alert detailing exact updated fields.
   - **Missing fields + `details_promised_later == True`:** `dispatch_details_status = DEFERRED_2D`, `dispatch_details_due_at = sim_now + timedelta(days=2)`. Create `INFO` `DISPATCH_DETAILS_DEFERRED` alert telling Manager Lex recorded the acknowledgment, noted the missing fields, and will wait 2 days before nudging.
   - **Missing fields + `details_promised_later == False`:** `dispatch_details_status = AWAITING_IMMEDIATE`, `dispatch_details_due_at = None`. Draft `DISPATCH_DETAILS_REQUEST` email (`DRAFT_PENDING_APPROVAL`) asking specifically for `missing_fields`, and create `MEDIUM` `MISSING_DISPATCH_DETAILS` alert with `Approve & Send` CTA.
4. **2-Day SLA Expiration (`sla_watcher.py`):**
   - Query POs where `dispatch_details_status == DEFERRED_2D` and `dispatch_details_due_at <= sim_now` and `status IN (ACKNOWLEDGED, DELAYED_CRITICAL, OVERDUE)` and no GRN logged.
   - Transition `dispatch_details_status = TIMED_OUT`, draft `DISPATCH_DETAILS_NUDGE` email, create `HIGH` `MISSING_DISPATCH_DETAILS_DUE` alert with `Approve & Send Nudge` CTA.
5. **Follow-Up Updates (`ACKNOWLEDGED` / `DELAYED_CRITICAL` / `OVERDUE`):**
   - Even if a later supplier reply is classified as `GENERAL_REPLY` (or `POST_ACK_DELAY` / `ACKNOWLEDGED`) with new logistics fields, `logistics_workflow.py` applies any changed field (`promised_dispatch_date`, `actual_dispatch_date`, `carrier_name`, `tracking_number`), updates `missing_dispatch_fields_json`, and creates an `INFO` `PO_LOGISTICS_UPDATED` alert listing what changed.
   - If the supplier replies with another `"we'll send tracking in 2 days"` message while already `TIMED_OUT` or `AWAITING_IMMEDIATE`, Lex updates the note and alerts the Manager instead of silently looping endlessly.

### 4.2 Making Team Stock Request Analysis & Alert Payloads
- **Strict Pydantic Schema (`StockRequestTriageResult`):**
  - `intent`: `STOCK_REQUEST` | `MINIMUM_QTY_CONFIRMATION` | `GENERAL_MESSAGE` | `AMBIGUOUS`
  - `confidence`: `float` (`0.0–1.0`)
  - `requested_items`: `list[StockRequestItem]` (`part_sku: str | None`, `part_name: str | None`, `requested_qty: int | None`)
  - `urgency_note`: `str | None`
  - `summary_for_manager`: `str`
  - `suggested_action`: `str`
  - `reasoning`: `str`
- **Guardrails:**
  - If `confidence < 0.85` OR `requested_items` is empty OR any item has `requested_qty is None or requested_qty <= 0` OR SKU/name doesn't uniquely resolve to an active `Part` → force `AMBIGUOUS`, create `HIGH` `AMBIGUOUS_STOCK_REQUEST` alert, touch zero stock!
  - Note on multi-SKU emails: Evaluate each requested part independently (creating one `StockRequest` + one focused `ActionItem` per SKU so the Manager can approve/reject each SKU cleanly—even if one SKU is in stock and another is short!).
- **Deterministic Analysis Payload (`cta_payload_json`):**
  - `stock_request_id`, `part_id`, `sku`, `part_name`, `requested_qty`, `on_hand_stock`, `quarantined_stock`, `on_order_stock`, `inventory_position`, `rop`, `replenishment_point`, `shortfall_qty`, `will_trigger_po` (`bool`), `projected_order_qty` (`int | None`), `primary_supplier_name`, `primary_unit_price`, `primary_effective_lt`, `open_pos` (list of `{po_number, status, qty, eta, supplier_name}`).
- **Manager CTAs (`execute_alert_cta`):**
  - `approve_stock_withdrawal` — Calls `engine.withdraw_stock()`, marks `StockRequest.APPROVED`, resolves alert.
  - `issue_partial_stock` — Calls `engine.withdraw_stock(qty=min(requested_qty, on_hand_stock))`, marks `StockRequest.PARTIALLY_APPROVED`, resolves alert.
  - `ask_making_minimum` — Approves/sends Lex's `STOCK_REQUEST_CLARIFICATION` draft (or allows inline edit via `edited_subject`/`edited_body`), marks `StockRequest.AWAITING_MIN_QTY`, resolves alert.
  - `reject_stock_request` — Marks `StockRequest.REJECTED`, rejects any linked clarification draft if present, optionally records note, resolves alert.

---

## 5. Testing & Evaluation Strategy

1. **Zero Regression Gate (Every Slice):**
   - Run `uv run pytest`, `uv run ruff check backend tests`, and `cd frontend && npm run build` after each slice.
   - Verify all 5 AST architectural boundary checks in [`tests/test_architecture.py`](../../tests/test_architecture.py) pass—and add a new AST rule if appropriate ensuring `backend/po/logistics.py` never imports from `inventory/` or `mailroom/`.
2. **Backend Unit & Integration Tests:**
   - `tests/core/test_migrations.py`: Prove `ensure_schema_migrations()` safely upgrades a pre-Phase-2 SQLite schema (simulating an old `purchase_orders` table missing new columns) without data loss.
   - `tests/po/test_logistics.py`: Missing-field calculation, `DEFERRED_2D` timer setup, `COMPLETE` transitions.
   - `tests/orchestrator/test_stock_request_workflow.py`: Making → Manager (without Lex `400`), Making → Manager (CC Lex) across Scenarios S1 (healthy), S2 (triggers PO draft), S3 (shortage → ask min qty → Making reply → issue min qty), S4 (low-confidence `< 0.85` AMBIGUOUS zero mutation), and verify stock does NOT decrement until Manager approval.
   - `tests/orchestrator/test_logistics_and_escalation.py`: Ack complete (creates `INFO` alert), Ack missing immediate (creates `DISPATCH_DETAILS_REQUEST` draft + alert), Ack deferred later (sets 2-day timer + `INFO` alert → clock `+2d` triggers `DISPATCH_DETAILS_NUDGE` draft + alert), follow-up email populates carrier/tracking & resolves missing alert, and `POST /api/pos/{id}/escalate-procurement` delivers to `procurement` inbox + creates alert.
3. **Golden YAML Eval Expansion (`backend/evals/lex.yaml`):**
   - Add deterministic offline + live-ready eval cases for:
     - Supplier ack with full carrier/tracking/dispatch date
     - Supplier ack missing carrier/tracking (no promise)
     - Supplier ack deferring tracking for 2 days (`details_promised_later: true`)
     - Making-team stock request extraction + `< 0.85` ambiguous SKU guardrail
     - `DISPATCH_DETAILS_REQUEST` / `STOCK_REQUEST_CLARIFICATION` draft compliance
4. **Frontend Build & Live Browser/DOM Smoke Checks:**
   - Typecheck + Vite build (`npm run build`).
   - Verify no `Withdraw` button remains on `InventoryPage`, Mailroom shows all 7 personas with `Making Team` CC'ing `Lex`, `POTrackerPage` renders the Horizontal Stepper with distinct `Created` vs `PO Sent` vs `Dispatched` dates and `Escalate to Procurement` modal, and `DataTable` supports vertical/horizontal scroll containment, frozen Title Case headers, hover tooltips without icons, and column dragging.

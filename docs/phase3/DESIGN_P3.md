# Phase 3 Technical Design & Architecture Specification (`DESIGN_P3.md`)

**Document Status:** Phase 3 Engineering Design v1.0 (Aligned to [`PRD_P3.md`](PRD_P3.md) & [`AGENTS.md`](../../AGENTS.md))  
**Core Mandate:** Deliver all 10 Phase 3 upgrades through **decoupled, testable vertical slices**, resolving all confirmed documentation/code drifts while preserving `tests/test_architecture.py` AST boundary guarantees.

---

## 1. Architectural Preservation & Boundary Design

### 1.1 Preserving `AGENTS.md §2` & `tests/test_architecture.py`
All 5 AST checks in [`tests/test_architecture.py`](../../tests/test_architecture.py) remain strictly enforced:
1. `inventory/`, `po/`, `mailroom/`, `planning/`, and `agents/` **never** import from each other.
2. Only `orchestrator/` composes cross-module workflows and binds DB tools.
3. No `datetime.now()` or `datetime.utcnow()` anywhere in domain/orchestrator/observability modules—always `core.clock.now()`.
4. `po/` never instantiates `InventoryTransaction` or mutates `Part.on_hand_stock` / `Part.quarantined_stock`.
5. `observability/` remains 100% agent-agnostic and domain-agnostic.

### 1.2 How Lex Gets Full DB Tools & Sandbox Execution Without Sideways Imports
Because `backend/agents/` cannot import `inventory/`, `po/`, `mailroom/`, or `planning/`:
- **`backend/agents/tools.py` (Domain-Agnostic Tool Framework):** Defines `ToolSpec`, `ToolResult`, `ToolHandler` callable protocol, and `ToolRegistry`. Contains zero domain imports.
- **`backend/agents/sandbox.py` (Restricted Python Calculation Engine):** Pure Python AST-validated sandbox executing calculation scripts with `math`, `statistics`, `datetime`, `timedelta`, `decimal`, `json`—blocking `import os/sys/subprocess/socket`, filesystem I/O, network calls, and `eval`/`exec`, bounded by execution timeout and memory/loop limits.
- **`backend/orchestrator/lex_tools.py` (Concrete Tool Bindings):** Constructs a session-scoped `ToolRegistry` exposing read-only DB/planning/mailroom queries + `run_sandbox_calc` + `schedule_lex_reminder` + `propose_memory_note` + `create_advisory_alert` to `LexAgent`.
- **Execution Traceability:** Every tool invocation during `LexAgent` reasoning appends a structured record (`{tool, args, result_summary, duration_ms, error}`) to `AgentTracer.span.tool_calls` (already persisted in `AgentTrace.tool_calls_json`!).

---

## 2. Database Schema & Idempotent Migrations

### 2.1 Enum Updates (`backend/core/enums.py`)
- **`EmailCategory`:** Add `ADVISORY_NOTICE = "ADVISORY_NOTICE"` if needed, or reuse `GENERAL` / `DISPATCH_DETAILS_NUDGE` cleanly so existing `AutonomyPolicy` seeding continues iterating `EmailCategory`.
- **`LexReminderStatus` (new `StrEnum`):** `PENDING`, `TRIGGERED`, `CANCELLED`.
- **`DispatchDetailsStatus`:** Preserve existing `DEFERRED_2D = "DEFERRED_2D"` enum value on `PurchaseOrder.dispatch_details_status` for Phase 1/2 test and API compatibility while persisting the exact variable N-day deadline (`+4d`, etc.) in `PurchaseOrder.dispatch_details_due_at` and `LexReminder.remind_at`.
- **`InventoryTxType`:** Keep `CONSUMPTION`, `RECEIPT_RELEASED`, `ADJUSTMENT` — distinguish simulated daily burn vs discrete demand events via a boolean column or explicit prefix/flag (see §2.2).

### 2.2 Additive Columns & New Tables (`backend/core/models.py` + `backend/core/migrations.py`)

| Table | Additive Column / New Table | Type & Default | Purpose |
|---|---|---|---|
| `parts` | `daily_burn_ewma` | `Float, default=0.0` | Persisted predicted daily burn ($\alpha = 0.3$) up through most recent completed IST day. |
| `parts` | `daily_burn_prev_7d` | `Float, default=0.0` | EWMA baseline from 7 completed days prior for neutral trend `%` calculation. |
| `parts` | `daily_demand_std` | `Float, default=0.0` | Unrounded daily demand standard deviation ($\sigma_D$) across observed daily series. |
| `parts` | `burn_carry` | `Float, default=0.0` | Fractional consumption carry accumulator so integer `on_hand_stock` honors fractional burn rates over multi-day ticks. |
| `parts` | `last_burn_settled_date` | `String(16), nullable=True` | Last completed IST calendar date (`YYYY-MM-DD`) settled for daily burn. |
| `suppliers` | `avg_response_days` | `Float, default=0.5` | Mean email acknowledgment/response time in days computed from real PO communication latencies. |
| `suppliers` | `cycles_90d` | `Integer, default=0` | Total replenishment cycles in the trailing 90-day window. |
| `inventory_transactions` | `is_predicted_burn` | `Boolean, default=False` | Marks clock-tick simulated daily burn deductions (`True`) vs `[Withdraw]` / Making-Team demand events (`False`). |
| `action_items` | `dismiss_resolved_at` | `DateTime(tz), nullable=True` | Tracks when a `DISMISSED` alert's underlying inventory/PO condition was resolved or manually acknowledged (keeps `status == DISMISSED` in history while clearing the Inventory warning icon). |
| `lex_reminders` *(New)* | `id`, `po_id` (FK nullable), `part_id` (FK nullable), `supplier_id` (FK nullable), `comm_id` (FK nullable), `remind_at` (DateTime tz), `reason` (Text), `context_json` (JSON), `status` (String), `created_at`, `triggered_at` | New Table | Persists Lex's dynamically scheduled follow-ups (e.g., supplier promised details in 4 days). |

*Migration Strategy:* Update [`backend/core/migrations.py`](../../backend/core/migrations.py) `_add_missing_columns()` for `parts`, `suppliers`, `inventory_transactions`, and `action_items`; `Base.metadata.create_all()` creates `lex_reminders` automatically.

---

## 3. Exact Mathematical & State-Machine Contracts

### 3.1 Timezone & Clock Alignment (`backend/core/config.py`, `core/clock.py`, `seed/data.py`)
- `IST = timezone(timedelta(hours=5, minutes=30))` (or `ZoneInfo("Asia/Kolkata")`).
- `SEED_START_TIME = datetime(2026, 10, 1, 7, 30, 0, tzinfo=timezone.utc)` (`2026-10-01 13:00:00 IST`).
- `settings.default_sim_start = "2026-10-01T07:30:00+00:00"`.
- Helper `core.clock.to_ist_date(dt) -> date` converts any UTC timestamp to its IST calendar date (`dt.astimezone(IST).date()`).
- In `logistics_workflow.py::parse_iso_date_utc()`, date-only `"YYYY-MM-DD"` strings normalize to **`07:30:00Z`** (`13:00:00 IST`) to stay aligned with `SEED_START_TIME` and predictable `+1d` SLA checks.

### 3.2 Daily Burn EWMA ($\alpha = 0.3$), Same-Day Freeze & Clock Rollover (`backend/planning/ewma_daily.py`)
1. **Window & Inputs:**
   - Given current simulated IST date $d_{\text{today}} = \text{to\_ist\_date}(\text{sim\_now})$:
   - Historical completed days span $d_0 = d_{\text{today}} - 90\text{d}$ through $d_{89} = d_{\text{today}} - 1\text{d}$ (on `Oct 1`, that is `2026-07-03` through `2026-09-30`).
   - Same-day events (`timestamp` on $d_{\text{today}}$) are **excluded** from $C_{d_0 \dots d_{89}}$.
2. **Daily EWMA Recursion ($\alpha = 0.3$):**
   - $E_0 = \text{float}(C_{d_0})$
   - For $i = 1 \dots 89$:
     $$E_i = 0.3 \cdot C_{d_i} + 0.7 \cdot E_{i-1}$$
   - `daily_burn_ewma` $= E_{89}$ (full float precision; never rounded mid-calculation).
   - `daily_burn_prev_7d` $= E_{82}$ (7 completed days prior).
   - `trend_pct` $= ((E_{89} - E_{82}) / E_{82}) \times 100$ if $E_{82} > 0$ else $0.0$.
3. **Daily Demand Std Dev ($\sigma_D$) & Intermittent Safety Floor:**
   - $\sigma_D = \text{statistics.stdev}(C_{d_0 \dots d_{89}})$ (unrounded float).
   - If $> 50\%$ of the 90 days have $0$ consumption (or $> 50\%$ of 30-day rolling windows are sparse), flag `is_intermittent = True` and apply the conservative rolling-peak floor (`max(ewma, max_7d_peak_avg * 0.5)` or `max_30d_rolling_avg / 30`) so slow-moving critical parts retain safe `ROP/T`.
4. **Clock Rollover Engine (`settle_daily_burn_on_clock_advance()` in `workflows.py`):**
   - When clock advances to `new_now`:
     - Let `target_last_completed = to_ist_date(new_now) - timedelta(days=1)`.
     - For each unsettled IST date $d$ from `last_burn_settled_date + 1d` through `target_last_completed`:
       1. Look up discrete `extra_events` (`InventoryTransaction` on IST date $d$ with `type == CONSUMPTION` and `is_predicted_burn == False`).
       2. Compute physical burn qty to deduct using `part.burn_carry + part.daily_burn_ewma`:
          - `raw_consume = part.burn_carry + part.daily_burn_ewma`
          - `int_consume = int(math.floor(raw_consume + 1e-9))`
          - `new_carry = raw_consume - int_consume`
          - Deduct `actual_deduct = min(part.on_hand_stock, int_consume)` from `part.on_hand_stock` and insert `InventoryTransaction(type=CONSUMPTION, qty_delta=-actual_deduct, timestamp=end_of_day_d, is_predicted_burn=True, note="Simulated daily burn")` (when `actual_deduct > 0`).
          - Update `part.burn_carry = new_carry`.
       3. **Update EWMA & `ROP/T` only if unpredicted demand occurred:**
          - Total day demand $C_d = \text{part.daily\_burn\_ewma} + \text{extra\_events}$ *(uncensored predicted pace + actual unpredicted draws)*.
          - If `extra_events == 0`:
            - `part.daily_burn_ewma` stays identical ($0.3\hat{D} + 0.7\hat{D} = \hat{D}$).
            - `part.daily_demand_std`, `part.rop`, and `part.replenishment_point` **remain untouched** (preserving exact user invariant!).
          - If `extra_events > 0`:
            - Update `part.daily_burn_ewma = 0.3 * C_d + 0.7 * part.daily_burn_ewma`.
            - Recompute `daily_demand_std` over the observed daily series from `2026-07-03` through $d$.
            - Recalculate and persist dynamic `part.rop` and `part.replenishment_point` using `planning/rop.py`!
       4. Update `part.last_burn_settled_date = d.isoformat()`.
       5. Check replenishment trigger (`inventory_position < rop` and `not has_active_po`), `ACCELERATED_BURN`, and `STOCKOUT_ACTIVE` on day $d$.

### 3.3 `Stockout In` Urgency, Communication Buffer & Cycle Reconstruction
1. **Metrics (`PartPlanningMetrics`):**
   - `days_to_stockout = 0.0` if `on_hand == 0`, else (`on_hand / daily_burn_ewma` if `daily_burn_ewma > 0` else `None`).
   - `predicted_stockout_date = sim_now + timedelta(days=days_to_stockout)`.
   - `comm_buffer_days`:
     $$\text{comm\_buffer\_days} = \text{primary\_supplier.avg\_response\_days} + (1.0 \text{ if } \text{response\_rate} < 0.80 \text{ else } 0.0)$$
   - `safety_days = safety_stock / daily_burn_ewma` (when `daily_burn_ewma > 0`).
   - `urgent_threshold_days = primary_effective_lt + comm_buffer_days`
   - `warning_threshold_days = urgent_threshold_days + safety_days`
   - **Earliest Active PO Coverage:** Find earliest non-terminal dispatched PO (`status NOT IN (PENDING_DISPATCH_APPROVAL, CLOSED, CANCELLED)`): `earliest_eta = po.revised_eta or po.promised_eta or (sent_at + primary_effective_lt)`.
   - **Badge Urgency Tier:**
     - `CRITICAL` (Red): `on_hand == 0` OR (`earliest_eta is not None` and `predicted_stockout_date < earliest_eta`) OR (`earliest_eta is None` and `days_to_stockout <= urgent_threshold_days`)
     - `WARNING` (Amber): `earliest_eta is None` and `days_to_stockout <= warning_threshold_days`
     - `HEALTHY` (Green): otherwise.
2. **Recommended PO Quantity (for `Create PO` / `Edit PO` / Drawer):**
   - Given any candidate `supplier_id` (Primary or Backup) with `(L_eff_sup, sigma_L_sup, moq_sup)`:
   - Compute supplier-specific `SS_sup`, `ROP_sup`, `T_sup = ROP_sup + ceil(D_ewma * target_coverage_days)`.
   - If `inventory_position < T_sup`: `raw_qty = T_sup - inventory_position`; else `raw_qty = ceil(D_ewma * target_coverage_days)`.
   - `suggested_qty = max(moq_sup, raw_qty)`.
   - Emit plain-English `reasoning` citing `inventory_position`, `daily_burn_ewma`, `L_eff_sup`, `comm_buffer_days`, `SS_sup`, `T_sup`, and `moq_sup`.
3. **90-Day Cycle Reconstruction (`backend/planning/cycles.py`):**
   - Reconstruct daily closing `on_hand` backward from current `part.on_hand_stock` across the 90 trailing days using every `InventoryTransaction` (`CONSUMPTION` and `RECEIPT_RELEASED`) so the end point equals live `on_hand_stock` identically!
   - Attach cycle events from `PurchaseOrder` & `GoodsReceipt` rows:
     - `STOCKOUT` / `LOW_STOCK` (days where reconstructed `on_hand == 0` or crossed below ROP)
     - `PO_PLACED` (`po.created_at` / `po.sent_at`, `po_number`, `supplier_name`, `qty`)
     - `REPLENISHMENT` (`grn.received_at`, `po_number`, `received_qty`, `actual_lt_days`)
   - Append projected linear depletion line (`Oct 1` → `predicted_stockout_date`) plus projected open-PO arrival step if an active PO exists.

### 3.4 `on_order_stock`, Duplicate PO Guard, Partial GRNs, Overdue & Alert Rules
1. **`get_on_order_stock(session, part_id)` (`backend/inventory/service.py`):**
   - Query active POs where `status NOT IN (PENDING_DISPATCH_APPROVAL, CLOSED, CANCELLED)`.
   - For each PO, compute `remaining = max(0, po.qty - sum(grns on po where status in (RELEASED, HELD_QUARANTINE)))`.
   - `on_order_stock = sum(remaining)` (0 when only `PENDING_DISPATCH_APPROVAL` POs exist!).
   - Expose `active_po_id`, `active_po_number`, `active_po_status`, and `pending_dispatch_qty` on `PartSnapshot` so duplicate-PO guard blocks creating a second PO while any PO (`PENDING_DISPATCH_APPROVAL` included) is open.
2. **Partial Delivery 3-Way Match (`backend/po/matching.py` & `po/service.py`):**
   - `qty_ok = (data.invoice_qty == data.grn_received_qty)`.
   - Track cumulative released GRN qty across the PO. If a GRN passes 3-way match and `cumulative_released < po.qty`, transition `RECEIVED_PENDING_INVOICE → ACKNOWLEDGED` (allowing future deliveries for remaining qty); transition to `CLOSED` once `cumulative_released >= po.qty`.
3. **Overdue Allow-List (`backend/po/state_machine.py` & `sla_watcher.py`):**
   - Add `POStatus.UNRESPONSIVE_ESCALATED → POStatus.OVERDUE`, `POStatus.SENT_AWAITING_ACK → POStatus.OVERDUE`, and `POStatus.RECEIVED_PENDING_INVOICE → POStatus.ACKNOWLEDGED`.
   - `sla_watcher.py` checks `OVERDUE` (`sim_now >= effective_eta + 1d` with no GRN) across `ACKNOWLEDGED`, `DELAYED_CRITICAL`, `UNRESPONSIVE_ESCALATED`, and `SENT_AWAITING_ACK` (when `promised_eta` exists).
4. **Accelerated Burn & Active Stockout (`inventory/service.py` & `workflows.py`):**
   - `ACCELERATED_BURN`: `on_hand_stock > 0` AND `earliest_po_eta is not None` AND `predicted_stockout_date < earliest_po_eta` (deduplicated per part while `OPEN`).
   - `STOCKOUT_ACTIVE` (`CRITICAL`): `on_hand_stock == 0` (deduplicated per part while `OPEN`).
5. **Alert `Edit PO`, `Reject Draft` & `Dismiss` Semantics:**
   - `PO_DISPATCH_APPROVAL` alerts emit actions `approve_draft` (`Approve & Send PO`) and `edit_po` (`Edit PO`).
   - `edit_po` opens `CreateEditPOModal`; saving calls `POST /api/pos/{po_id}/dispatch-edited` which updates `supplier_id`/`qty`/`unit_price`, cancels/supersedes the old `DRAFT_PENDING_APPROVAL` comm, transitions `PENDING_DISPATCH_APPROVAL → SENT_AWAITING_ACK`, dispatches the edited email, sets `promised_eta` & `next_sla_check_at`, and resolves the alert.
   - `reject_lex_draft` rejects the `POCommunication` only—it **never** marks the parent `ActionItem` `RESOLVED`.
   - `dismiss_action_item`: marks `ActionItem.status = DISMISSED`, rejects any unapproved `DRAFT_PENDING_APPROVAL` linked comm, and clears `draft_preview` in `GET /api/alerts`. Any `DISMISSED` alert with `dismiss_resolved_at == None` whose underlying PO is still open or part condition persists displays on `PartSnapshot.unresolved_dismissed_alerts`.

---

## 4. 90-Day Causal Seed Plan (`backend/seed/data.py`)

| SKU | Criticality | Primary Sup | 90-Day Demand Profile (Jul 3 – Sep 30) | Est. Cycles (90d) | Target Oct 1 State |
|---|---|---|---|---|---|
| `BRG-402` | `CRITICAL` | `sup1` | Steady + late-Sep upward ramp (~5–8/d) | 4 cycles | Healthy buffer (~14d runway) |
| `VLV-204` | `CRITICAL` | `sup2` | Mid-Aug spike + steady (~3–5/d); Sup2 late cycles | 3 cycles | Amber/Red tension (~16d vs 23d Sup2 LT!) |
| `SNS-802` | `CRITICAL` | `sup3` | Weekday-heavy industrial pull (~4–6/d); Sup3 slow acks | 3 cycles | ~12d runway |
| `PMP-701` | `CRITICAL` | `sup3` | Bimodal project bursts (~2–6/d) | 3 cycles | ~15d runway |
| `BRG-405` | `CRITICAL` | `sup3` | Intermittent/lumpy spare (bursts every ~20d) | 2 cycles | Healthy buffer |
| `FLT-108` | `STANDARD` | `sup1` | Alternating high/low weeks (~4/d avg) | 4 cycles | Healthy (~18d) |
| `BLT-301` | `STANDARD` | `sup2` | Gradual seasonal climb (~6–9/d); Sup2 late + v1 mismatch | 3 cycles | ~19d runway |
| `GSK-512` | `STANDARD` | `sup2` | Smooth rising trend (~5–7/d); Sup2 late + v1 mismatch | 3 cycles | ~22d runway |
| `SHF-605` | `STANDARD` | `sup2` | Cooling downward trend (~5 → 2.5/d) | 3 cycles | ~24d runway |
| `RLY-901` | `STANDARD` | `sup3` | Late-September surge (~3 → 6/d) | 3 cycles | ~14d runway |
| `BLT-305` | `STANDARD` | `sup1` | Steady with mid-August dip (~4/d) | 4 cycles | ~20d runway |
| `WSH-104` | `LOW` | `sup1` | High-velocity smooth (~10/d) | 5 cycles | ~22d runway |
| `ORG-112` | `LOW` | `sup2` | Low-volume steady (~2/d, <3 closed deliveries for LT test or 2 cycles) | 2 cycles | ~25d runway |
| `CBL-114` | `LOW` | `sup3` | High-velocity ramp (~8–11/d) | 4 cycles | ~18d runway |
| `FUS-115` | `LOW` | `sup1` | Intermittent spare (sporadic 4–8u draws) | 2 cycles | ~28d runway |

*All historical POs close on or before `2026-09-29`, guaranteeing **0 active POs** on `2026-10-01 13:00 IST`!*

---

## 5. Lex Agentic Upgrade & Sandbox Architecture

### 5.1 Tool Catalog (`backend/orchestrator/lex_tools.py`)
1. `get_part_snapshot(sku_or_part_id)` → live balances, daily burn, stockout date, urgency tier, `ROP/T`, suppliers.
2. `get_consumption_history(part_id, days=90)` → 90-day daily series summary, recent withdrawals, cycles.
3. `get_po_details(po_number_or_id)` → full PO fields, GRNs, invoices, communications, SLA state.
4. `list_open_pos(supplier_id=None, part_id=None)` → active POs.
5. `get_supplier_profile(supplier_key_or_id)` → reliability score, OTD, invoice accuracy, `avg_response_days`, `cycles_90d`, effective LTs, and existing `SupplierMemoryNote`s.
6. `get_mailroom_thread(thread_id)` → full email thread history.
7. `run_sandbox_calc(python_script, variables)` → executes AST-restricted math/date calculation via `backend/agents/sandbox.py`.
8. `schedule_lex_reminder(po_id, remind_at_iso, reason, suggested_action)` → inserts `LexReminder(PENDING)` + updates `PO.dispatch_details_due_at` dynamically when applicable.
9. `propose_memory_note(supplier_id, note, reasoning)` → deduplicates against existing memory notes before saving and optionally creates a contextual memory alert.
10. `create_advisory_alert(title, summary, severity, po_id, part_id)` → creates grounded Manager alerts when memory/context identifies a real risk.

### 5.2 Dynamic Deferral Flow (Solving the "After 4 Days" Failure)
When a supplier writes *"We acknowledged the PO, but due to system issues we can only share shipment details after 4 days"*:
1. `LexAgent` uses `get_po_details`, `get_part_snapshot`, and `get_supplier_profile` + `run_sandbox_calc` to check `days_to_stockout` vs `4 days` + `effective_lead_time_days`.
2. `EmailTriageResult` extracts `details_promised_later=True` AND `details_promised_days=4` / `details_promised_by_date="2026-10-05"`.
3. `logistics_workflow.py` honors `details_promised_by_date` / `details_promised_days` (removing the hardcoded `details_due_days = 2` cap, via `apply_po_logistics_updates(..., details_due_at=...)`), schedules a `LexReminder` for `+4 days` (`2026-10-05 13:00 IST`), does **NOT** create a nagging draft email today, and alerts the Manager (`DISPATCH_DETAILS_DEFERRED` if runway is safe, or `HIGH` stockout-risk escalation if `days_to_stockout` is dangerously tight!).
4. When `SimulatedClock` passes `2026-10-05 13:00 IST`, `sla_watcher.py` fires the pending `LexReminder` first (suppressing duplicate legacy `DEFERRED_2D` fallback firing via `handled_deferred_po_ids`), wakes Lex with live PO/GRN/thread context, and Lex drafts the follow-up only if dispatch details are still missing.

### 5.3 Formatting Enforcement & Frontend Rendering
- `DRAFT_SYSTEM_PROMPT` mandates structured Markdown formatting (salutation, blank lines, bulleted terms with `**bold**` PO#/SKUs/quantities/`₹` totals/dates, explicit CTA, and standard Lex signature).
- `LexAgent.draft_email()` normalizes whitespace, verifies sign-off + PO# + currency, and retries/repairs if formatting rules fail.
- `frontend/src/components/FormattedEmailBody.tsx` safely renders `**bold**`, `- ` bullets, and blank-line paragraphs in React (`MailroomPage`, `POTrackerPage`, read-only `AlertsPage`).

---

## 6. Impacted Existing Tests (Intentional Contract Updates)
When implementing Phase 3 slices, update tests tied to superseded Phase 1/2 rules:
- `tests/seed/test_seed_and_clock.py` & `tests/conftest.py`: `SEED_START_TIME` moves from `2026-11-20T09:00:00Z` to `2026-10-01T07:30:00Z` (`Oct 1 13:00 IST`); seeded open POs move from `4` (`PO-1037..PO-1040`) to `0`.
- `tests/inventory/test_inventory.py` & `tests/orchestrator/test_workflows_and_sla.py`: `PENDING_DISPATCH_APPROVAL` now yields `on_order_stock == 0` while still preventing duplicate POs.
- `tests/orchestrator/test_prd_fixes.py`: `F-4.3` date-only ETA assertion aligns to `07:30:00Z` (`13:00 IST`).
- `tests/orchestrator/test_logistics_and_escalation.py`: variable deferral days (`details_promised_days`) respected alongside default 2-day fallback when unspecified.
- `tests/observability/` & `backend/evals/lex.yaml`: expanded for tool-calling traces and draft formatting assertions.

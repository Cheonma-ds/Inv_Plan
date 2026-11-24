# Phase 3 Product Requirements Document (`PRD_P3.md`)

**Document Status:** Phase 3 Specification v1.0 (Final Development Phase — Builds on [`PRD.md`](../../PRD.md), [`PRD_P2.md`](../phase2/PRD_P2.md), and [`PRD_FIXES.md`](../PRD_FIXES.md))  
**Target Users:** Inventory Management Team (`InventoryManager`), Making / Production Team (`Making`), Procurement Team (`Procurement`)  
**AI Agent:** **Lex** (`gemini-3.5-flash-lite` via `google-genai` and `$GEMINI_API_KEY`)

---

## 1. Executive Summary & Verified Baseline Reconciliation

Phases 1 and 2 plus the Fix Pack (`F-1.1`–`F-4.3`) established the core Demand-to-Delivery engine, 7-persona Mailroom, multimodal PDF pipeline, Apple HIG `DataTable` UI, Horizontal Order Tracking Stepper, and observability/eval framework.

However, live end-to-end review uncovered **documentation drift** against the current `main` branch (`HEAD: 099474f`) and **ten concrete product, mathematical, UX, and agentic gaps** that prevent InvPlan from behaving like a true intelligent inventory control plane.

### 1.1 Confirmed Documentation Drifts (Baseline vs. `HEAD`)
Before adding Phase 3 features, these verified baseline realities are locked in:

| Area | Earlier Documentation Claim | Actual Codebase Reality (`HEAD`) | Phase 3 Resolution |
|---|---|---|---|
| **Inventory `Withdraw` & Manual Actions** | `PRD_P2.md §4.2` & `STATUS_P2.md` stated `Withdraw` was removed from `Inventory & Parts`. | Commit `25a8bdc` re-added `Withdraw`, `Create PO`, `ROP/S`, and `Demand` buttons/modals in `InventoryPage.tsx`. | **Keep `Withdraw`** (and Making Team email requests) as live demand events; **keep `Create PO`** (upgraded to `View PO` when an active PO exists); **remove `Edit ROP/S` and `Demand` buttons/modals** and `Demand Override` column. |
| **Inventory Column Headers & `ROP/S`** | `InventoryPage.tsx` uses `On-Hand`, `Quarantined`, `On-Order`, `Reorder Point/Target`, `Demand Override`, and `ROP/S` (`[30d cov]` subtext). | Same in `AlertsPage.tsx`, `SuppliersPlanningPage.tsx`, `workflows.py`, `planning/rop.py`. | Rename to **`On Hand`**, **`On Hold`**, **`On Order`**, **`ROP/T`** (remove `[30d cov]` subtext), remove **`Demand Override`**, add **`Daily Burn`** and **`Stockout In`**, and replace all `ROP/S` / `ROP / S` references globally with **`ROP/T`**. |
| **Simulated Clock Baseline** | `config.py` / `seed/data.py` start at `2026-11-20T09:00:00Z`; `format.ts` renders `UTC`. | `PO-1037`..`PO-1040` are seeded as 4 live in-flight POs; 12m consumption is uncoupled from closed PO cycles. | Reset clock start to **Oct 1, 2026, 1:00 PM IST** (`2026-10-01T07:30:00Z`), format UI timestamps in **IST**, seed **0 active POs**, and replace random 12m data with a **causally consistent 90-day simulation (Jul–Sep)**. |
| **`on_order_stock` Draft Safety** | `inventory/service.py` counts `PENDING_DISPATCH_APPROVAL` inside `on_order_stock`. | Creating a Lex draft immediately inflates `On Order` before any PO is sent. | `on_order_stock` counts **only sent+ active POs** (`SENT_AWAITING_ACK` and downstream); duplicate-PO guard checks `has_active_po` / `pending_dispatch_qty` separately. |
| **Quarantine Double-Counting** | `PRD.md §4.1` vs `inventory/service.py`. | `P10` partially fixed `get_on_order_stock()` by subtracting `part.quarantined_stock` globally across a part's open POs. | Refine to **per-PO GRN subtraction** (`RELEASED + HELD_QUARANTINE`) so unrelated open POs never cross-contaminate. |
| **Partial Deliveries (`3-Way Match`)** | `PRD.md §4.1(1)` supports partial receipts while `matching.py` enforced `invoice == po == grn`. | `matching.py:36` still checks `invoice_qty == po_qty == grn_received_qty`. | Change to **`invoice_qty == grn_received_qty`**; only close PO when cumulative released GRNs reach `po.qty`. |
| **Overdue Transition** | `state_machine.py` permits `ACKNOWLEDGED`/`DELAYED_CRITICAL` → `OVERDUE`, not `UNRESPONSIVE_ESCALATED`. | `UNRESPONSIVE_ESCALATED` and `SENT_AWAITING_ACK` cannot transition to `OVERDUE`. | Permit `UNRESPONSIVE_ESCALATED → OVERDUE` and `SENT_AWAITING_ACK → OVERDUE` when `effective_eta + 1d` passes without GRN. |
| **`AcceleratedBurnAlert`** | `inventory/service.py:172` only fires at `on_hand_stock == 0`. | Zero-on-hand detection misses early incoming-PO gaps while stock > 0. | Fire **`ACCELERATED_BURN`** when `on_hand_stock > 0` and `predicted_stockout_date < earliest_po.effective_eta`; emit separate **`STOCKOUT_ACTIVE`** (`CRITICAL`) at `on_hand_stock == 0`. |

---

## 2. Phase 3 Product Pillars (Requirements `1`–`10`)

### Pillar 1 — Simulated Clock (Oct 1, 1 PM IST), Clean Seed Reset & 90-Day Causal History
*(Covers Requirements 3, 4, 5)*

1. **Simulated Clock Start & IST Display:**
   - Default `SimulatedClock` and `Reset Seed` timestamp is **October 1, 2026 at 1:00 PM IST** (`2026-10-01T07:30:00+00:00`).
   - All UI timestamp displays (`ClockBar`, `AlertsPage`, `POTrackerPage`, `MailroomPage`, `SuppliersPlanningPage`, `AIHealthPage`) render in **IST (`Asia/Kolkata`, `IST`)**.
   - Date-only ETA parsing normalizes to `13:00:00 IST` (`07:30:00Z`) so `+1d` / `+3d` / `+7d` advances cross SLA boundaries cleanly.
2. **Zero Active POs on Reset Seed:**
   - Clicking **Reset Seed** leaves **zero active/open POs** in `PO Tracker` (`Open POs` tab starts clean at `0`).
   - Historical POs completed during the Jul–Sep 90-day period appear under `All POs` / `CLOSED` for auditability, supplier scoring, and cycle markers.
3. **15 Distinct 90-Day Consumption Profiles (Jul–Sep) + Causal Supplier Cycles:**
   - Each of the 15 SKUs has a unique, realistic, hardcoded **90-day daily consumption sequence** covering **July 3 – September 30, 2026** (90 completed days prior to Oct 1), exhibiting distinct trends (upward growth, cooling decline, mid-quarter surge, weekday/weekend shifts, bimodal bursts, lumpy/intermittent spares, smooth high-velocity).
   - The seeder simulates each SKU forward day-by-day across Jul–Sep using the real behavioral profiles of `Sup1`, `Sup2`, and `Sup3`:
     - **`Sup1` (Apex Precision Components):** fast email acknowledgment (`~0.2d–0.4d`), high OTD (`>90%`), 100% clean v1 invoices.
     - **`Sup2` (Bharat Industrial Fasteners):** moderate email response (`~0.8d–1.5d`), chronically late delivery (`+6d` to `+11d` beyond quoted 15d LT), frequent v1 invoice price mismatches corrected on v2.
     - **`Sup3` (Deccan Electro-Spares):** slow email acknowledgment (`~1.5d–3.2d`, often requiring 1–2 follow-up nudges), occasional `+3d–6d` delays and price volatility.
   - Whenever a simulated historical cycle triggers a PO and receives delivery on or before Sep 29, the seeder writes real `PurchaseOrder`, `POCommunication`, `GoodsReceipt`, `Invoice`, and `InventoryTransaction` records.
   - **Zero Mismatch Guarantee:** Every cycle marker in the 90-day chart, every supplier metric (`cycles_90d`, `avg_response_days`, `effective_lead_time_days`, `lead_time_std_dev`, `otd_rate`, `invoice_accuracy_rate`, `response_rate`, `reliability_score`), and every part's initial `ROP/T` is computed in Python directly from those persisted rows—zero hardcoded score overrides.
   - Initial Oct 1 `on_hand_stock` balances are calibrated so parts have healthy runway (advancing `+1d` or `+3d` does not trigger alert floods across healthy items), while showcasing varied urgency levels across the 15 SKUs.

---

### Pillar 2 — Daily EWMA Burn ($\alpha = 0.3$), `Stockout In` Urgency Badges, Cycle Analytics & Dynamic `ROP/T`
*(Covers Requirements 3, 4, 7, 9)*

1. **Daily Burn Calculation (`EWMA`, $\alpha = 0.3$):**
   - Computed over trailing completed IST days ($t \in [d_{\text{today}} - 90\text{d}, \dots, d_{\text{today}} - 1\text{d}]$) using:
     $$E_t = \alpha \cdot C_t + (1 - \alpha) \cdot E_{t-1}, \qquad \alpha = 0.3$$
   - **Completed-Day Lock:** `Daily Burn` ($E_{\text{yesterday}}$) is a predicted rate calculated strictly up through the most recent completed IST day.
   - **Same-Day Demand Event Invariant:** Performing `[Withdraw]` or approving a Making Team stock request **today** reduces `on_hand_stock` and updates `Stockout In` **immediately**, while leaving today's `Daily Burn` unchanged until the SIM CLOCK advances to the next IST day.
   - **Trend Indicator:** Compares $E_{\text{yesterday}}$ against $E_{\text{today}-8\text{d}}$ (7-day EWMA shift):
     $$\text{trend\_pct} = \frac{E_{\text{yesterday}} - E_{\text{today}-8\text{d}}}{E_{\text{today}-8\text{d}}} \times 100$$
   - **UI Rendering (`Daily Burn` column):** Displays e.g. `3/day` alongside a smaller, neutral-grey (`#86868B`) trend indicator (`↑ 13%` or `↓ 8%` or `0%`) — **strictly no red/green color coding on the trend %**.
   - **Rounding Rule:** Full floating-point precision is preserved across all intermediate math (`E_t`, `sigma_D`, `L_eff`, `SS`, `days_to_stockout`); rounding occurs **only** on discrete thresholds (`math.ceil` for `ROP` and `T`) and final display formatting (`Math.round` for `>= 1/day`, 1 decimal for `< 1/day`).
   - An explicit note documenting `EWMA α = 0.30` is visible in the UI (`Stockout In` cycle drawer and `Suppliers & Planning` header/cards).

2. **Clock-Advance Daily Consumption & Dynamic `ROP/T` Invariant:**
   - Whenever the SIM CLOCK advances across $k \ge 1$ IST calendar dates (via `+6h` crossing midnight, `+1d`, `+3d`, `+7d`, or `Jump to ETA`), the engine settles each completed day chronologically:
     - Deducts predicted daily burn (using fractional `burn_carry` so integer `on_hand_stock` tracks fractional rates accurately without truncation loss) and logs simulated burn consumption.
     - **Pure Predicted Burn Day (`extra_events == 0`):** If no `[Withdraw]` or Making Team approval occurred on that day, `on_hand_stock` drops by the predicted burn (e.g., `10` units), `Daily Burn` remains unchanged ($0.3 \hat{D} + 0.7 \hat{D} = \hat{D}$), and **`ROP/T` does not change**.
     - **Unpredicted Demand Day (`extra_events > 0`):** If `Withdraw` or Making Team consumption of $x$ units (e.g., `4`) occurred on that day, `on_hand_stock` dropped by $x$ immediately and by predicted burn at rollover (total drop `10 + 4 = 14`), total day demand $C_t = \hat{D}_t + x$ updates `Daily Burn` via EWMA ($\alpha = 0.3$), updates $\sigma_D$, and **dynamically recalculates `ROP` and `T`**!
   - Replenishment trigger (`inventory_position < rop` with no active PO) and `AcceleratedBurn` / `StockoutActive` checks run on each settled day.

3. **Dynamic `ROP/T` Formulas (100% Deterministic Python):**
   - Service-level $Z$: `CRITICAL = 2.05`, `STANDARD = 1.65`, `LOW = 1.28`.
   - $\text{Safety Stock (SS)} = Z \times \sqrt{(L_{\text{eff}} \times \sigma_D^2) + (\hat{D}^2 \times \sigma_L^2)}$
   - $\text{ROP} = \lceil \hat{D} \times L_{\text{eff}} + \text{SS} \rceil$
   - $T = \text{ROP} + \lceil \hat{D} \times \text{target\_coverage\_days} \rceil$

4. **`Stockout In` Urgency-Aware Badge & Cycle Analytics Drawer:**
   - `days_to_stockout = on_hand_stock / daily_burn_ewma` (unrounded float; `0` if `on_hand_stock == 0`).
   - `predicted_stockout_date = sim_now + timedelta(days=days_to_stockout)`.
   - **Communication Delay Buffer:** Derived from the Primary supplier's historical `avg_response_days` (plus unacknowledged risk buffer when `response_rate < 0.80`).
   - **Color-Coded Urgency Logic** (takes into account Primary `effective_lead_time_days` + `comm_buffer_days` + `safety_days = SS / daily_burn_ewma` + `on_order_stock` / earliest active PO ETA):
     - **Red (`CRITICAL`):** `on_hand_stock == 0`, OR (`days_to_stockout <= L_eff + comm_buffer_days` with no active PO arriving in time), OR (`open_po_exists` and `predicted_stockout_date < earliest_po.effective_eta`).
     - **Amber (`WARNING`):** `days_to_stockout <= L_eff + comm_buffer_days + safety_days` (and not covered earlier by an active PO).
     - **Green (`HEALTHY`):** Safe runway beyond lead time + communication buffer + safety stock (or adequately covered by an arriving active PO).
     - Thus **`15d`** can legitimately render **Red** for a slow/long-LT supplier (`Sup2` 23.5d eff LT) and **Green** for a fast/short-LT supplier (`Sup1` 8d eff LT).
   - **Clickable Drawer / Info Panel:** Clicking any `Stockout In` badge opens a focused drawer displaying:
     - **90-Day Stock & Consumption Chart:** Daily consumption bars + reconstructed daily `on_hand` curve + projected runout curve, with explicit lifecycle markers (**Stockout / Low → PO Placed → Replenishment Received → Stockout**) generated from actual DB records (showing natural variation in cycle lengths and incomplete current cycle).
     - **Predicted Stockout Date** & unrounded days remaining.
     - **Primary Supplier Effective Lead Time** (`L_eff`, quoted LT, communication delay buffer, and avg response days).
     - **EWMA & Safety Stock Breakdown** ($\alpha = 0.30$ note, `Daily Burn`, trend %, `SS`, `ROP/T`).
     - **Recommended PO Quantity** covering target cycle ($T$) and safety stock (rounded to supplier MOQ), with clear formula explanation and quick jump to `[Create PO]` / `[View PO]`.

5. **Supplier Scorecard Enrichment (`Suppliers & Planning`):**
   - Display **Cycles Participated (90d)** and **Avg Email Response (days)** (e.g., `0.3d`, `1.1d`, `2.3d`) alongside OTD %, Invoice Accuracy %, Quoted vs Effective Lead Time, and Reliability Score—100% matching the 90-day cycle data.

---

### Pillar 3 — Navigation, `Inventory & Parts` UX & Suggested-Quantity `Create PO`
*(Covers Requirements 2, 6, 7, 8)*

1. **Sidebar Navigation (`App.tsx`):**
   - Remove **`Actionable Alerts`** from the left sidebar navigation.
   - The prominent **`Open Alerts`** button (with live count badge) in the persistent top `ClockBar` is the sole entry point to `AlertsPage`, highlighting cleanly when active.
2. **`Inventory & Parts` Table & Actions Cleanup:**
   - **Columns:** `Part` · `On Hand` · `On Hold` · `On Order` · `Inventory Position` · `Daily Burn` · `Stockout In` · `ROP/T` · `Primary & Backup Suppliers` · `Actions`.
   - Remove **`Demand Override`** column, **`[30d cov]`** sub-label under `ROP/T`, **`[Edit ROP/S]`**, and **`[Demand]`** actions/modals.
   - Replace every `ROP/S` / `ROP / S` string across UI and backend messages with **`ROP/T`**.
   - **Active PO Button Swap (`[Create PO]` → `[View PO]`):**
     - When a part has any active PO (`status NOT IN (CLOSED, CANCELLED)`), render **`[View PO]`**.
     - Clicking `[View PO]` navigates to `PO Tracker` and **simultaneously opens that PO's slide-over detail drawer**.
3. **Suggested Order Quantity + Reasoning in `[Create PO]` / `[Edit PO]` Modal:**
   - Opening `[Create PO]` (or `[Edit PO]` from an alert) queries deterministic PO suggestion logic for the currently selected supplier (`1°` Primary or `2°` Backup) and pre-fills **Order Quantity** with a visible **Suggestion & Reasoning** card explaining:
     - `Inventory Position`, `Daily Burn` (`EWMA`), selected supplier's `Effective Lead Time` + `MOQ`, `Safety Stock`, `Target Stock (T)`, and the resulting recommended order quantity (`max(moq, T_supplier - inventory_position)`).
   - Switching suppliers inside the modal dynamically updates the suggested quantity and reasoning.

---

### Pillar 4 — Alerting & PO State-Machine Overhaul
*(Covers Requirements 9)*

1. **`on_order_stock` Strictly for Dispatched+ POs:**
   - `PENDING_DISPATCH_APPROVAL` contributes **`0`** to `On Order` (`on_order_stock`).
   - `on_order_stock` counts only `SENT_AWAITING_ACK` and active post-dispatch POs, subtracting per-PO GRNs (`RELEASED + HELD_QUARANTINE`).
   - Duplicate-PO prevention still checks active PO existence (`PENDING_DISPATCH_APPROVAL` included) so multiple withdrawals never spawn duplicate draft POs.
2. **`[Edit PO]` Replaces `[Reject Draft]` & True Draft/Dismiss Semantics:**
   - PO dispatch/replenishment alerts (`PO_DISPATCH_APPROVAL`) replace `[Reject Draft]` with **`[Edit PO]`**, opening the shared `CreateEditPOModal` pre-populated with the existing draft PO's part, supplier, qty, suggested qty, and draft flow—updating and sending the existing PO without creating duplicates.
   - Rejecting a draft communication directly (`reject_lex_draft` from Mailroom) sets the communication to `REJECTED` and **never** falsely transitions the parent `ActionItem` to `RESOLVED`.
   - **Dismiss Behavior:**
     - Dismissing an alert sets `ActionItem.status = DISMISSED`, cancels/clears any unapproved `DRAFT_PENDING_APPROVAL` communication from active draft queues, and **hides `draft_preview`** on `DISMISSED` alerts (`GET /api/alerts` + `AlertsPage`).
     - **Unresolved Inventory Warning Indicator:** Any `DISMISSED` alert whose underlying condition remains unresolved (e.g., open/unsent PO, part `inventory_position < rop`, `on_hand == 0`, or quarantined stock) surfaces a clear **warning triangle badge** on that SKU's row in `Inventory & Parts`. Clicking the warning reveals the dismissed alert(s) with **Reopen Alert**, **Mark Resolved**, or **View PO / Create PO**. Auto-clears when the underlying condition resolves (e.g., PO dispatched/closed or stock replenished).
3. **Refined `AcceleratedBurnAlert` & `STOCKOUT_ACTIVE`:**
   - **`ACCELERATED_BURN` (`HIGH`/`CRITICAL`):** Fires when `on_hand_stock > 0` AND `open_po_exists` AND `predicted_stockout_date < earliest_po.effective_eta`.
   - **`STOCKOUT_ACTIVE` (`CRITICAL`):** Separate highest-urgency alert when `on_hand_stock == 0`.
4. **Partial Deliveries, Overdue Transitions & Quarantine Accounting:**
   - `matching.py`: `qty_ok = (invoice_qty == grn_received_qty)`. Partial matched receipts (`cumulative_released < po.qty`) transition `RECEIVED_PENDING_INVOICE → ACKNOWLEDGED` (remaining qty stays `on_order`), closing PO only once cumulative released qty `>= po.qty`.
   - `state_machine.py` & `sla_watcher.py`: permit `UNRESPONSIVE_ESCALATED → OVERDUE` and `SENT_AWAITING_ACK → OVERDUE` when `effective_eta + 1d` passes without GRN.

---

### Pillar 5 — Lex Outbound Email Formatting & Rendering
*(Covers Requirement 1)*

1. **Structured Outbound Formatting:**
   - All Lex outbound drafts (`PO_DISPATCH`, `REVISED_PO`, `ACK_REMINDER`, `DISPATCH_DETAILS_REQUEST`, `DISPATCH_DETAILS_NUDGE`, `PRE_ETA_CHECKIN`, `OVERDUE_CHASE`, `EXPEDITE_REQUEST`, `INVOICE_DISCREPANCY`, `STOCK_REQUEST_*`, `GENERAL`) follow clean business-email formatting:
     - Salutation (`Dear <Recipient/Team>,`)
     - Blank-line separated paragraphs (`\n\n`)
     - Bullet points / key-value highlights with **`**bold**`** emphasis on PO numbers, part SKUs, quantities, `**₹...**` currency amounts, and dates
     - Explicit, polite call-to-action
     - Standard signature block (`Best regards,\n**Lex**\nAI Supply Chain Assistant`)
2. **Code-Enforced Formatting & Safe Rich Preview:**
   - `LexAgent.draft_email()` normalizes spacing, validates mandatory sign-off/PO/currency grounding, and rejects malformed single-wall-of-text outputs.
   - Frontend renders email bodies in `MailroomPage`, `POTrackerPage`, and read-only `AlertsPage` views through a safe React Markdown-lite component (`FormattedEmailBody`) supporting bold spans, bullet lists, and clean paragraph spacing, while keeping editable `<textarea>` drafts plain Markdown.

---

### Pillar 6 — Upgrading Agent Lex into a Tool-Using AI Partner
*(Covers Requirement 10)*

1. **True Multi-Step Tool-Use Loop (`Gemini` Function Calling + `AgentTracer`):**
   - Upgrade Lex from single-shot JSON extraction/drafting into a **tool-calling agent** (`generate_with_tools` in `llm_client.py`) logging every tool invocation in `AgentTrace.tool_calls_json` (visible in `AIHealthPage`).
   - Preserves `AGENTS.md` strict module isolation via a `ToolRegistry` defined in `backend/agents/tools.py` and bound to domain read/advisory services inside `backend/orchestrator/lex_tools.py`.
2. **Database Read Tools (Full Operational Awareness):**
   - `get_part_snapshot(sku_or_part_id)` (stock, daily burn, stockout runway, dynamic `ROP/T`, primary/backup suppliers)
   - `get_consumption_history(part_id, days=90)` (daily burn trend, recent withdrawals, cycles)
   - `get_po_details(po_number_or_id)` & `list_open_pos(supplier_id, part_id)`
   - `get_supplier_profile(supplier_key_or_id)` (metrics, `avg_response_days`, all `SupplierMemoryNote`s)
   - `get_mailroom_thread(thread_id)`
3. **Sandboxed Python Calculation Execution (`backend/agents/sandbox.py`):**
   - `run_sandbox_calc(python_script, variables)` runs AST-validated, timeout-bounded mathematical/date Python scripts (no filesystem, network, or unsafe imports) so Lex can compute custom runway checks, partial-delivery gaps, multi-PO impacts, and deferral risk deterministically.
4. **Dynamic Variable-Day Deferral & Scheduled Reminder Engine (`LexReminder`):**
   - Fixes the broken behavior when a supplier says *"we acknowledged the PO but due to system issues we can only send shipment details after 4 days"* (or any variable timeframe/date):
     - Lex calls `get_part_snapshot` / `run_sandbox_calc` to check whether waiting $N$ days (`4 days`) is safe relative to `Stockout In` and `supplier.reliability_score`.
     - If safe, Lex calls **`schedule_lex_reminder(po_id, remind_at, reason)`** instead of drafting a premature follow-up email, updates PO deferral state dynamically (removing the hardcoded 2-day cap), and alerts the Manager (`INFO`) with the exact math & scheduled follow-up date.
     - If unsafe (e.g., stockout projected before the supplier's promised info date), Lex explains the gap using real numbers and drafts a polite expedite/clarification or escalation alert immediately.
   - When `SimulatedClock` advances past `remind_at`, `sla_watcher.py` wakes Lex up to inspect current state and draft the appropriate follow-up only if still needed.
5. **Intelligent Memory-Triggered Alerting (`propose_memory_note` & `create_advisory_alert`):**
   - Instead of pushing every `new_memory_note` directly into DB blindly, Lex checks existing memory notes first (deduplicating/consolidating) and raises **timely, memory-informed advisory alerts** when supplier history creates real risk (e.g., reminding the Manager about `Sup2`'s invoice mismatch habit on pre-delivery or `Sup3`'s price-hike history when ordering critical parts).
6. **Runtime Resilience & Multimodal Coverage:**
   - Retries with backoff + timeouts in `GeminiLLMClient`, graceful `AMBIGUOUS_EMAIL` degradation on API failure (zero `500` crashes), multi-PDF inspection, and multimodal image attachment inspection (`.jpg`/`.jpeg`/`.png`) in `triage_email`.

---

## 3. Phase 3 End-to-End Acceptance Criteria

Phase 3 is complete when **all** of the following hold end-to-end (tested via `uv run pytest`, `uv run ruff check backend tests`, `cd frontend && npm run build`, live Gemini evals, and browser walkthrough):

1. **Seed & Clock Baseline:** `Reset Seed` sets Sim Clock to **Oct 1, 2026 1:00 PM IST**, `PO Tracker` shows **0 open POs**, all 15 parts have distinct 90-day Jul–Sep histories, `ROP/T` is computed by Python planning code, and supplier metrics (`cycles_90d`, `avg_response_days`, `effective_lead_time_days`, `reliability_score`) match the 90-day POs with zero discrepancy.
2. **Daily Burn, Same-Day Withdraw & Clock Advance Invariants:**
   - Withdrawing stock or approving a Making request today immediately reduces `On Hand` and `Stockout In`, while **today's `Daily Burn` stays fixed**; advancing `+1d` updates `Daily Burn` via EWMA ($\alpha = 0.3$) and recalculates `ROP/T`.
   - Advancing `+1d` with **no** extra consumption deducts predicted daily burn from `On Hand` while leaving `Daily Burn` and **`ROP/T` unchanged**.
3. **`Stockout In` Badge & Cycle Drawer:** Badges reflect supplier-aware urgency colors (15d urgent for long-LT supplier vs safe for short-LT supplier); clicking opens the 90-day cycle chart (`Stockout → PO → Replenishment → Stockout`), predicted stockout date, Primary effective LT + comm buffer, $\alpha = 0.3$ note, and recommended PO qty.
4. **Inventory UX & Navigation:** Sidebar omits `Actionable Alerts`; `Open Alerts` in header opens Alerts; columns read `On Hand`, `On Hold`, `On Order`, `ROP/T` (no `[30d cov]`, no `Demand Override`, no `Edit ROP/S` / `Demand` actions); `[Create PO]` shows supplier-specific suggested qty + reasoning; active-PO parts show `[View PO]` and jump straight into PO Tracker's open PO drawer.
5. **Alerting & State Machine:** `PENDING_DISPATCH_APPROVAL` keeps `On Order = 0` while preventing duplicate POs; `[Edit PO]` replaces `[Reject Draft]` and opens the Create/Edit PO modal; `DISMISSED` hides Lex's draft preview and puts a warning symbol on the Inventory item until resolved; `ACCELERATED_BURN` fires before `on_hand == 0` when `predicted_stockout_date < earliest_po.effective_eta`; partial GRNs match `invoice == grn`; `UNRESPONSIVE_ESCALATED → OVERDUE` succeeds.
6. **Lex Formatting & Tool-Using Upgrade:** Drafts render with clean paragraphs, `**bold**` highlights, bullet lists, and sign-off; Lex uses DB read tools + Python calculation sandbox + `schedule_lex_reminder` so a supplier email saying *"can only send shipment details after 4 days"* schedules a 4-day reminder without nagging today (unless stockout risk overrides), logs full `tool_calls` in `AIHealthPage`, and surfaces memory-aware alerts cleanly.

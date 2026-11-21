# Phase 2 Product Requirements Document (`PRD_P2.md`)

**Document Status:** Phase 2 Specification v1.0 (Builds on Approved Baseline [`PRD.md`](../../PRD.md) v1.0)  
**Target Users:** Inventory Management Team (`InventoryManager`), Making / Production Team (`Making`), Procurement Team (`Procurement`)  
**MVP Agent:** **Lex** (`gemini-3.5-flash-lite` via `$GEMINI_API_KEY`, continuing strict Draft-First outbound governance and deterministic math separation)

---

## 1. Executive Summary & Phase 2 Scope

Phase 1 (Slices 1–5) validated the deterministic Demand-to-Delivery core, 5-persona Mailroom, multimodal PDF 3-way match, statistical planning engine, React operations UI, and agent-agnostic observability/evals.

Phase 2 addresses **four product & UX gaps** uncovered while reviewing the live prototype:

| # | Area | Phase 1 Limitation | Phase 2 Outcome |
|---|---|---|---|
| **1** | **Design System & Layout Width** | Default slate/indigo styling, abrupt transitions, and `max-w-7xl` center-constraining leaves wide wasted horizontal margins. | Apple HIG-inspired calm visual hierarchy, restrained tonal palette, smooth spring-like micro-transitions, and wider horizontal workspace utilization. |
| **2** | **Universal Scrollable Resizable Tables** | Tables scroll the entire browser page vertically, lack frozen headers, use uppercase headers without tooltips, and have fixed column widths. | Bounded vertical + horizontal table scroll viewports with frozen **Title Case** headers, zero-icon pure-hover header descriptions, and user-resizable columns across all app tables. |
| **3** | **Mailroom Expansion & Making-Team Stock Requests** | Inventory Planner clicks "Withdraw" directly in the Inventory table (unrealistic); only 5 personas exist; no Making or Procurement persona. | Direct `Withdraw` button removed from Inventory UI. **Mailroom** expands to 7 personas (**Making** + **Procurement** added). Making requests stock via email (CC'ing Lex); Lex analyzes stock, active POs, and reorder triggers and opens an approval alert; stock deducts **only after Manager approval**. |
| **4** | **PO Tracker Logistics, Tracking & Escalation** | `Created` and `Dispatched` show the identical PO email timestamp; no carrier/tracking fields; supplier ack doesn't check missing dispatch details or alert Manager on Lex actions; no direct Procurement escalation from PO Tracker. | Distinct `PO Created`, `PO Sent`, `Acknowledged`, `Promised/Actual Dispatch`, `Delivered`, and `Closed` milestones rendered in a **Horizontal Order Tracking Stepper**; carrier & tracking fields; Lex missing-details check + 2-day deferred wait + nudge + mandatory Manager alert for every autonomous Lex action; 1-click **Escalate to Procurement**. |

### Non-Negotiable MVP Guardrails Preserved in Phase 2
1. **Draft-First Outbound Rule:** Every outbound email drafted by Lex to an external supplier (and Making follow-ups drafted by Lex) still enters `DRAFT_PENDING_APPROVAL` and requires `InventoryManager` approval before dispatch.
2. **Deterministic Math & Inventory Position Guard:** `inventory_position = on_hand_stock + on_order_stock + quarantined_stock` (`on_order_stock` deducts `quarantined_stock` to avoid double-counting while still preventing duplicate POs). All consumption, ROP triggering, stockout days, and 3-way matching arithmetic stay 100% deterministic Python.
3. **Confidence Escalation Gate:** Lex extractions/triages below `0.85` confidence (or ambiguous SKU/PO references) force safe manual review with **zero state mutation**.
4. **Simulated Clock Fidelity:** All timestamps, 2-day missing-detail waits, and UI dates continue to use `SimulatedClock`.

---

## 2. Pillar 1 — Apple-Inspired UI Redesign & Workspace Expansion

### 2.1 Visual Hierarchy & Calm Color Palette
Inspired by Apple macOS / iPadOS productivity interfaces:
- **Canvas & Surfaces:**
  - App background: soft warm alabaster (`#F5F5F7`) instead of harsh cool gray.
  - Primary cards, tables, and drawers: crisp white (`#FFFFFF`) or subtle frosted translucency (`rgba(255, 255, 255, 0.86)` + `backdrop-filter: blur(12px)`) for sticky bars and overlays, paired with hairline borders (`rgba(0, 0, 0, 0.08)`) and ultra-subtle diffused elevation shadows rather than heavy dark outlines.
- **Typography & Hierarchy:**
  - System Apple-first font stack (`-apple-system, BlinkMacSystemFont, "SF Pro Display", "SF Pro Text", "Inter", sans-serif`).
  - Clear 3-tier ink hierarchy: Primary charcoal (`#1D1D1F`), Secondary muted slate (`#6E6E73`), Tertiary captions (`#86868B`).
  - `tabular-nums` enforced across all stock balances, currency figures (`₹`), lead times, urgency scores, and timestamps.
- **Calm Accent & Semantic Palette:**
  - Primary interactive accent: calm deep steel/system blue (`#0066CC` / `#0071E3` hover) instead of stark `slate-900` solid black buttons everywhere.
  - Semantic status badges use soft tinted fills with restrained borders and high-contrast readable text (calm sage green for healthy/closed, warm amber for pending/deferred/quarantine, muted coral-crimson for critical/overdue, soft violet-indigo exclusively for **Lex AI** indicators).

### 2.2 Workspace Width & Margin Balance
- **Problem:** Currently `<main className="flex-1 p-5 md:p-6 max-w-7xl w-full mx-auto">` caps content at `1280px` and centers it, creating large unused margins on standard desktop screens (`1440px`–`1920px+`).
- **Requirement:**
  - Widen the main workspace container (e.g., `max-w-[1640px]` or full available width with balanced `px-5 md:px-7 py-5` outer breathing room) and allow tables, split mailroom panes, and multi-column scorecards to use the horizontal space naturally.
  - Keep the persistent top `ClockBar` and left navigation rail clean, lightweight, and uncrowded (with active nav pill styling and smooth hover states).

### 2.3 Smooth Animations & Micro-Interactions
- **Transitions:** `180ms–260ms` `cubic-bezier(0.22, 1, 0.36, 1)` ease curves across:
  - Tab/page entrance fade + subtle vertical settle (`4px`–`6px`).
  - Slide-over drawer (`POTrackerPage`) slide + backdrop fade.
  - Modals scale/fade entrance (`0.98 → 1.0`).
  - Horizontal stepper progress bar fill & node state transitions.
  - Expandable trace/eval accordions and button active press feedback (`scale(0.99)`).
- **Accessibility:** Respect `@media (prefers-reduced-motion: reduce)` by collapsing motion durations to instant transitions.

---

## 3. Pillar 2 — Universal Scrollable, Frozen-Header, Resizable Tables

Applies uniformly to **every data table** in the application (`Inventory & Parts`, `PO Tracker`, `AI Health — Autonomy Policies`, and any table introduced in Phase 2).

### 3.1 Dual-Axis Scroll Containment & Frozen Headers
- **Independent Table Scroll Viewport:**
  - Every table renders inside a bounded scroll container (`overflow: auto`, responsive max height such as `max-height: calc(100vh - 260px)` on main table pages or appropriate section cap on multi-section pages).
  - Moving the pointer over the table and scrolling vertically scrolls **only the table body rows**, keeping the page header, filters, and navigation stationary.
  - Scrolling horizontally (trackpad, shift+wheel, or horizontal scrollbar) scrolls columns smoothly when total column width exceeds the viewport—supporting future column additions without layout breakage.
- **Frozen `<thead>`:**
  - Table column headers remain `position: sticky; top: 0` with an opaque/frosted background (`#FBFBFD` / `rgba(251,251,253,0.95)`) and crisp bottom divider so scrolling down never hides column labels.

### 3.2 Title Case Headers & Icon-Free Hover Tooltips
- **Title Case Rule:** All column headers must be formatted in clean **Title Case** (e.g., `Part`, `On-Hand`, `Quarantined`, `On-Order`, `Inventory Position`, `Reorder Point / Target`, `Demand Override`, `Primary & Backup Suppliers`, `Actions`, `PO Number`, `Unit Price`, `Total Value`, `Status`, `Promised / Revised ETA`). Remove forced CSS `uppercase` transforms on `<th>`.
- **Hover Tooltip Requirement:**
  - Hovering the cursor directly over any column header displays a concise, well-formatted description popover explaining what the column measures and what its values/statuses mean.
  - **Example (`Inventory & Parts` → `Part`):** *"Unique SKU identifier, part name, and operational criticality tier. **CRITICAL**: essential parts with 98% target service level (Z = 2.05) and 24h supplier ack SLA. **STANDARD**: core production items with 95% service level (Z = 1.65) and 48h SLA. **LOW**: routine/non-urgent items with 90% service level (Z = 1.28)."*
  - **Example (`Inventory Position`):** *"Total pipeline stock (`On-Hand + On-Order + Quarantined`) evaluated against ROP before triggering a new PO so in-flight or disputed shipments never spawn duplicate POs."*
  - **Strict Constraint:** **Do NOT add any tooltip icon** (`?`, `(i)`, info badge, etc.) anywhere inside column headers. Hovering the header cell itself triggers the floating tooltip (rendered via portal/fixed layer so `overflow: auto` never clips it).

### 3.3 Adjustable Column Widths
- Each column header includes a subtle, invisible-until-hovered vertical resize handle on its right edge (`cursor: col-resize`).
- Dragging adjusts that column's width smoothly in real time (clamped to a sane minimum width, e.g. `72px`–`96px`, so content never collapses to zero).
- Double-clicking a column divider resets that column to its default width.
- Adjusted column widths persist across refreshes (per-table `localStorage` key, e.g., `invplan_col_widths_inventory`, with a clean reset option if needed).

---

## 4. Pillar 3 — Mailroom Expansion (7 Personas) & Making-Team Stock Requests

### 4.1 Renaming & Persona Directory
- Rename **"5-Persona Mailroom"** to **"Mailroom"** in the left navigation, page header, and documentation (never `"7-Persona Mailroom"` or `"5-Persona Mailroom"` in the UI).
- Expand `PersonaKey` from 5 to **7 personas**:

| Persona Key | Display Label | Role & Capabilities |
|---|---|---|
| `inventory_manager` | Inventory Manager | Human decision-maker; approves/edits/rejects Lex drafts, approves/resolves Making stock requests, escalates POs to Procurement, corresponds with suppliers/Making/Procurement. |
| `making` *(New)* | Making Team | Manufacturing / shop-floor team requesting parts from inventory. Sends stock request emails to `Inventory Manager` (**must CC `Lex`**). |
| `procurement` *(New)* | Procurement | Simple inbox/sent/compose persona representing the Procurement team. Receives escalated POs (`Escalate to Procurement`), critical expedite escalations, and supplier commercial escalations. |
| `sup1` | Sup1 (Apex Precision) | Reliable benchmark supplier. |
| `sup2` | Sup2 (Bharat Fasteners) | Chronically late & invoice-prone supplier. |
| `sup3` | Sup3 (Deccan Electro-Spares) | Slow-to-ack & volatile-pricing supplier. |
| `lex` | Lex (AI Agent) | Read-only inbox view (`403` on manual send). Autonomously triages inbound supplier emails, PDF invoices, and Making-team stock requests; creates alerts and drafts for Manager approval. |

### 4.2 Removing Direct "Withdraw" from `Inventory & Parts`
- Remove the **Withdraw** button and Withdraw modal from [`InventoryPage.tsx`](../../frontend/src/pages/InventoryPage.tsx).
- The Inventory Planning team no longer performs arbitrary direct withdrawals from the inventory table.
- All operational stock consumption flows through the **Making Team Stock Request → Lex Analysis → Inventory Manager Approval** loop below. *(Note: Deterministic `consume_stock()` inside `backend/inventory/` remains the underlying ledger & ROP engine called upon Manager approval.)*

### 4.3 Making Team Stock Request Workflow (End-to-End)

#### Step 1: Making Team Composes Stock Request
- In **Mailroom**, operator switches to **`Making Team`** (`making`) and composes an email to **`Inventory Manager`** (`inventory_manager`).
- **Mandatory CC Rule:** `making` **must** include `lex` in `CC` (or `To`) when sending a stock request to `inventory_manager`.
  - **UI enforcement:** When `making` selects `inventory_manager` as recipient, `lex` is automatically pre-selected and locked in `CC` (with a calm note explaining Lex automatically analyzes stock requests).
  - **API enforcement:** `POST /api/mailroom/send` returns `400 Bad Request` if `from_persona == "making"` sends to `inventory_manager` without `lex` in `to_personas` or `cc_personas`.

#### Step 2: Lex Parsing & Deterministic Inventory/PO Analysis
When an email from `making` arrives with `lex` in `To` or `CC`:
1. **Lex (`extract_stock_request` / `triage_stock_request`, temp `0.0`):**
   - Extracts structured `StockRequestTriageResult`:
     - `intent`: `STOCK_REQUEST` | `MINIMUM_QTY_CONFIRMATION` | `GENERAL_MESSAGE` | `AMBIGUOUS`
     - `confidence` (`0.0`–`1.0`)
     - `requested_items`: list of `{ part_sku, part_name, requested_qty }`
     - `reason` / `urgency_context`
     - `summary_for_manager`, `reasoning`
   - **Guardrail:** If `confidence < 0.85`, empty `requested_items`, or any requested SKU/name cannot be uniquely matched to a `Part` in the catalog → force `AMBIGUOUS` and create a `HIGH` `AMBIGUOUS_STOCK_REQUEST` alert for Manager review with **zero stock/PO mutation**.
2. **Deterministic Orchestrator Analysis (Zero LLM Math):**
   For each matched part, `orchestrator/` computes from live DB state:
   - `on_hand_stock`, `quarantined_stock`, `on_order_stock`, `inventory_position`, `rop`, `replenishment_point`
   - **Active Open POs** for that part (`status NOT IN (CLOSED, CANCELLED)`): PO#, status, qty, supplier name, `promised_eta` / `revised_eta`
   - **Primary & Backup Supplier Terms** (agreed unit price, quoted/effective lead time, MOQ, reliability score)
   - **Post-Withdrawal Projection:** `projected_on_hand = on_hand_stock - requested_qty`, `projected_ip = inventory_position - requested_qty`, whether `projected_ip < rop`, and if so the exact deterministic PO `order_qty = max(S - projected_ip, primary_moq)`

#### Step 3: Scenario Branching & Manager Alert Creation
**Crucial Invariant:** Stock in `parts` is **NOT** decremented when the email arrives. Only an `ActionItem` alert + persisted `StockRequest` record are created.

| Scenario | Condition | Lex Analysis & Alert Content | Inline Manager CTAs |
|---|---|---|---|
| **S1: Healthy Stock, No PO Triggered** | `requested_qty <= on_hand_stock` AND `projected_ip >= rop` | Shows requested qty, current `on_hand`, active POs (if any), and confirms post-issue IP remains `>= ROP`. (`INFO` or `MEDIUM` if `CRITICAL` part). | **Approve & Issue Stock** · **Reject Request** |
| **S2: Sufficient Stock, Triggers Replenishment PO** | `requested_qty <= on_hand_stock` AND `projected_ip < rop` | Informs Manager that approving the withdrawal drops IP below `ROP`, lists active POs (if any), and provides **exact PO calculation** (`order_qty`, **Primary Supplier name, unit price, effective LT, MOQ, reliability score** + Backup option). (`MEDIUM` / `HIGH` for `CRITICAL` parts). | **Approve Issue & Trigger PO** *(deducts stock, creates `PENDING_DISPATCH_APPROVAL` PO + Lex `PO_DISPATCH` email draft for approval)* · **Reject Request** |
| **S3: Insufficient On-Hand Stock (Shortage)** | `requested_qty > on_hand_stock` (including `on_hand == 0`) | Highlights exact shortfall (`requested - on_hand`), surfaces **active open POs & ETAs**, flags any **quarantined units** blocked on invoice mismatch, and **asks the Manager how to proceed**. Includes a pre-drafted email (`DRAFT_PENDING_APPROVAL`) to `Making Team` asking for the **minimum quantity needed** right now. (`HIGH` / `CRITICAL`). | **Approve & Send Min-Qty Inquiry** *(sends Lex draft to Making, sets `StockRequest` to `AWAITING_MIN_QTY`)* · **Issue Available On-Hand (`X` units)** *(if `on_hand > 0`)* · **Trigger Replenishment PO** *(if IP `< ROP` or no sufficient open PO)* · **Reject / Defer Until ETA** |
| **S4: Making Replies with Minimum Needed Qty** | `making` replies on thread with e.g. *"We can manage with minimum 2 units"* | Lex classifies `MINIMUM_QTY_CONFIRMATION`, extracts `min_qty`, checks `min_qty <= on_hand_stock` and whether issuing triggers a PO, and opens a fresh `STOCK_WITHDRAWAL_REQUEST` alert for that `min_qty`. (If still `> on_hand`, stays in shortage review). | **Approve & Issue Min Qty (`X` units)** · **Reject Request** |
| **S5: Accelerated Burn (`on_hand -> 0` with Open PO far from ETA)** | Approving S1/S2/S4 leaves `on_hand == 0` while earliest open PO ETA is `> sim_now + 3d` | After Manager approves issue, deterministic `consume_stock()` also raises `CRITICAL` `ACCELERATED_BURN` alert recommending expedite review. | Standard `ACCELERATED_BURN` CTAs |

#### Step 4: Manager Approval Updates Inventory
- Only when `InventoryManager` clicks **Approve & Issue Stock** (or **Approve Issue & Trigger PO** / **Issue Available On-Hand**) does `orchestrator/` invoke `inventory.consume_stock()`:
  - `on_hand_stock` decreases in the `Inventory & Parts` table.
  - Immutable `InventoryTransaction(CONSUMPTION)` is logged with the Making request reference.
  - If `projected_ip < rop`, the replenishment PO (`PENDING_DISPATCH_APPROVAL`) and Lex `PO_DISPATCH` draft are created and surfaced for Manager dispatch approval (or if `on_hand` hit 0 with far ETA, `ACCELERATED_BURN` alert is created).
  - The `StockRequest` transitions to `APPROVED` (or `PARTIALLY_APPROVED`), and an optional confirmation / status note can be logged.

### 4.4 `Procurement` Persona Inbox
- Simple, functional mailbox inside **Mailroom**:
  - Supports **Inbox**, **Sent**, **Drafts**, **Compose**, and **Reply** with `To`/`CC` and `.pdf` attachments.
  - Receives internal escalation emails when the Inventory Manager clicks **Escalate to Procurement** on a PO (Section 5.4) or when critical escalations are routed to Procurement.

---

## 5. Pillar 4 — PO Tracker Logistics, Stepper, Automated Tracking Follow-Up & Procurement Escalation

### 5.1 Fixing `Created` vs. `Dispatched` & Separating Lifecycle Dates
- **Phase 1 Bug:** `POTrackerPage.tsx` labeled `po.sent_at` as `"Dispatched"`, and `seed/data.py` seeded `created_at == sent_at` on in-flight POs, making `Created` and `Dispatched` display identical timestamps.
- **Phase 2 Data Model & Semantics:**
  - **PO Created (`created_at`):** Timestamp the PO record was generated.
  - **PO Sent (`sent_at`):** Timestamp the Inventory Manager approved and emailed the PO to the supplier.
  - **Acknowledged (`acknowledged_at`):** Timestamp the supplier confirmed the PO.
  - **Promised Dispatch Date (`promised_dispatch_date`):** Date the supplier promised to ship the order from their facility.
  - **Actual Dispatch Date (`actual_dispatch_date`):** Date the shipment actually left the supplier's facility.
  - **Delivery Service (`carrier_name`):** Courier / logistics provider (e.g., `BlueDart`, `Delhivery`, `DTDC`, `DHL`, `SafeExpress`).
  - **Tracking Number (`tracking_number`):** Shipment tracking ID provided by the supplier.
  - **Delivered / GRN (`goods_receipts[0].received_at`)** & **Closed (`closed_at`)**.

### 5.2 Horizontal Order Tracking Stepper (UI)
Inside the PO Detail slide-over drawer, replace the flat text-only timeline box with a **Horizontal Order Tracking Stepper** (left-to-right progress):

```text
  [1. Created] ───▶ [2. PO Sent] ───▶ [3. Acknowledged] ───▶ [4. Dispatched] ───▶ [5. Received (GRN)] ───▶ [6. Closed]
   Nov 14            Nov 14            Nov 15                 Promised: Nov 21     ETA: Nov 24              —
                     (+2h)                                    (or Shipped: Date)
```

- **Each Step Node Displays:**
  - Visual indicator: Completed (calm green check), Current/Active (calm blue animated pulse ring), Blocked/Exception/Overdue/Quarantined (amber/coral warning badge), Pending (subtle outline).
  - Concise **Status Label**.
  - **Relevant Date** beneath the label (`Created` date, `PO Sent` date, `Ack` date, `Promised Dispatch` or `Actual Dispatch` date, `GRN` date or `ETA`, `Closed` date).
- **Logistics & Carrier Summary Strip** (directly integrated under the stepper):
  - Displays **Promised Dispatch Date**, **Actual Dispatch Date**, **Delivery Service (Carrier)**, **Tracking Number**, **Promised ETA**, **Revised ETA**, **Follow-ups Sent**, and **Pre-ETA Check** status.

### 5.3 Lex Communication Tracking, Missing-Detail Detection, 2-Day Wait & Mandatory Manager Alerts

#### Core Rule: Zero Silent Lex Actions
> **Mandatory Visibility Rule:** Every time Lex takes an autonomous action on a PO (acknowledging a PO, updating carrier/tracking/dispatch date, recording a 2-day wait window, logging a supplier memory note, or updating terms from a thread), Lex **MUST** create an `ActionItem` alert notifying the `InventoryManager` of the exact action taken and fields modified.

#### Flow A — Supplier Acknowledges a PO (`SENT_AWAITING_ACK` / `UNRESPONSIVE_ESCALATED` → `ACKNOWLEDGED`)
When Lex triages an acknowledgment email from a supplier:
1. **Extract Logistics Details:** Lex extracts `promised_eta_date`, `promised_dispatch_date`, `actual_dispatch_date`, `carrier_name`, `tracking_number`, plus whether the supplier explicitly said they will send missing logistics details later (`details_promised_later: bool`, `details_promised_by_date: str | None`).
2. **Update PO:** Transition PO to `ACKNOWLEDGED` (setting `acknowledged_at = sim_now` and populating any provided logistics fields).
3. **Evaluate Missing Shipment Details** (required dispatch info: `promised_dispatch_date` [or `actual_dispatch_date`], `carrier_name`, `tracking_number` — note: if not yet dispatched, `promised_dispatch_date` + `carrier_name` + `tracking_number` or carrier/dispatch commitment check):
   - *Wait — let's make missing-detail logic crystal clear and realistic:*
     - A complete acknowledgment/dispatch commitment requires: **`promised_dispatch_date` (or `actual_dispatch_date`)**, **`carrier_name`**, and **`tracking_number`** (if already dispatched, or if supplier hasn't shared tracking/carrier/dispatch date yet).
   - **Branch A1 — All 3 Details Present (`dispatch_date` + `carrier_name` + `tracking_number`):**
     - Mark `dispatch_details_status = COMPLETE`.
     - Create **`INFO` Alert (`PO_ACKNOWLEDGED_COMPLETE`)** notifying the Manager that Lex acknowledged the PO and recorded the dispatch date, carrier, and tracking number.
   - **Branch A2 — Details Missing & Supplier Promised Them Later (`details_promised_later == True`):**
     - Record `dispatch_details_status = DEFERRED_2D`, `missing_dispatch_fields_json = [...]`, and set **`dispatch_details_due_at = sim_now + 2 days`**.
     - Create **`INFO` Alert (`DISPATCH_DETAILS_DEFERRED`)** notifying the Manager: what Lex updated on the PO, which fields are still missing, that the supplier promised them later, and that Lex is waiting **2 days** (until `<due date>`) before nudging.
   - **Branch A3 — Details Missing & Supplier Did NOT Promise Them Later (`details_promised_later == False`):**
     - Record `dispatch_details_status = AWAITING_IMMEDIATE`, `missing_dispatch_fields_json = [...]`, and set `dispatch_details_due_at = None`.
     - Lex immediately drafts an email (`EmailCategory.DISPATCH_DETAILS_REQUEST`, `DRAFT_PENDING_APPROVAL`) asking the supplier for the specific missing fields (`promised_dispatch_date`, `carrier_name`, and/or `tracking_number`).
     - Create **`MEDIUM` Alert (`MISSING_DISPATCH_DETAILS`)** notifying the Manager of the acknowledgment + PO updates and presenting the pre-drafted request email for 1-click **Approve & Send**.

#### Flow B — 2-Day Wait Expiry (`SLAWatcher`)
- When the `SimulatedClock` advances past `po.dispatch_details_due_at` while `dispatch_details_status == DEFERRED_2D` and the PO is still active (`ACKNOWLEDGED` / `DELAYED_CRITICAL` / `OVERDUE`) with no GRN:
  - Lex generates a polite follow-up email draft (`EmailCategory.DISPATCH_DETAILS_NUDGE`, `DRAFT_PENDING_APPROVAL`) requesting the still-missing fields (`missing_dispatch_fields_json`).
  - Updates `dispatch_details_status = TIMED_OUT`.
  - Creates **`HIGH` Alert (`MISSING_DISPATCH_DETAILS_DUE`)** nudging the Inventory Manager with the draft ready for approval.

#### Flow C — Ongoing Manager ↔ Supplier Communication Tracking
- Whenever a follow-up email arrives on a PO-linked thread (from supplier with `lex` in `To`/`CC`, or manager-to-supplier CC'ing `lex` for context):
  - Even if the email is a shipment update / `GENERAL_REPLY` / `DISPATCH_UPDATE` on an `ACKNOWLEDGED` (or `DELAYED_CRITICAL` / `OVERDUE`) PO:
    - Lex extracts any newly supplied `promised_dispatch_date`, `actual_dispatch_date`, `carrier_name`, `tracking_number`, or `revised_eta`.
    - Orchestrator updates the PO fields, recomputes remaining missing fields (if all resolved, transitions `dispatch_details_status` to `COMPLETE` and auto-resolves open `MISSING_DISPATCH_DETAILS*` / `DISPATCH_DETAILS_DEFERRED` alerts for that PO), and **creates an `INFO` Alert (`PO_LOGISTICS_UPDATED`)** informing the Manager of every field Lex updated.
- Note: Manager-sent emails (from `inventory_manager` CC'ing `lex` to `sup*` or `making`) do **not** trigger supplier exception/ack transitions unless from the supplier, **but** Lex can passively track outbound context or keep thread context enriched; supplier emails trigger field extraction & alerts as above.

### 5.4 "Escalate to Procurement" on PO Tracker
- In `POTrackerPage.tsx`, inside the PO Detail Drawer (for any non-terminal PO, i.e. not `CLOSED` or `CANCELLED`):
  - Add an **"Escalate to Procurement"** button alongside **Switch to Backup Supplier**.
  - Clicking opens a compact escalation modal/form showing PO#, Part SKU, Supplier, current Status, and Follow-ups Sent, with an optional escalation reason input (e.g., *"Supplier unresponsive after follow-ups; missing tracking details"*).
  - Submitting triggers `POST /api/pos/{po_id}/escalate-procurement`:
    1. Records `escalated_to_procurement_at = sim_now` and `procurement_escalation_note` on the PO, and appends an audit entry to `po.notes`.
    2. Dispatches a manager-initiated internal email from `inventory_manager` → `procurement` (CC `lex`, linked to `po_id` & thread) summarizing the PO, part, supplier reliability score, open discrepancies/delays, and the Manager's escalation note. *(Because this is directly triggered by the human Inventory Manager to an internal team inbox, it dispatches cleanly to Procurement's inbox immediately without violating Lex's supplier draft-first rule.)*
    3. Creates a `HIGH` **`PROCUREMENT_ESCALATION`** alert (`assigned_team="Procurement"`) so it is also visible in the Action Queue and cross-referenced on the PO.

---

## 6. Phase 2 End-to-End Acceptance Criteria

Phase 2 is complete when all Phase 1 acceptance scenarios continue to pass **and** the following Phase 2 scenarios work end-to-end:

1. **Apple UI & Table Ergonomics:** Every page fills the desktop workspace cleanly without excess outer margins; every table scrolls vertically and horizontally within its own viewport with frozen **Title Case** headers, zero tooltip icons, pure-hover column descriptions, and persistent draggable column widths.
2. **Making Team Stock Request (Healthy & Reorder Trigger):** `Withdraw` button is gone from `Inventory & Parts`. `making` emails `inventory_manager` (CC `lex`) requesting 2 units of `SKU-001` (`on_hand=4, ROP=3, S=6`) → Stock stays `4` until Manager clicks **Approve Issue & Trigger PO** on Lex's alert → `on_hand` drops to `2`, `PO-1039` (`PENDING_DISPATCH_APPROVAL`) + Lex `PO_DISPATCH` draft are created.
3. **Making Team Stock Shortage & Min-Qty Loop:** `making` requests 10 units when `on_hand=2` and `PO-1039` is open → Lex creates Shortage Alert with active PO ETA and a draft asking Making for minimum needed qty → Manager approves draft → `making` replies *"Minimum 2 units needed"* → Lex opens approval alert for 2 units → Manager approves → `on_hand` updates to `0`.
4. **PO Tracker Stepper & Separated Dates:** Seeded and newly created POs display distinct `Created` vs `PO Sent` vs `Acknowledged` vs `Dispatched` dates on a **Horizontal Order Tracking Stepper**, alongside Carrier and Tracking Number.
5. **Missing Dispatch Details + 2-Day Wait & Alerting:**
   - Supplier acks PO without carrier/tracking/dispatch date → Lex creates `ACKNOWLEDGED` update alert + drafts `DISPATCH_DETAILS_REQUEST` email for Manager.
   - Supplier acks PO saying *"Will share BlueDart tracking in 2 days"* → Lex sets 2-day timer & notifies Manager (`INFO` alert). Advancing Simulated Clock `+2d` triggers `DISPATCH_DETAILS_NUDGE` draft + Manager alert. Supplier replying with `"BlueDart tracking BD-8821"` updates the PO fields, clears the wait timer, and creates an `INFO` alert showing the updated tracking info.
6. **Escalate to Procurement:** Clicking **Escalate to Procurement** on an open PO in PO Tracker logs the escalation on the PO, delivers an escalation email to `procurement`'s Mailroom inbox, and creates a Procurement-assigned alert.

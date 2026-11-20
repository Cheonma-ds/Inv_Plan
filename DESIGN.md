# Technical Design & Build Plan (`DESIGN.md`)

**Document Status:** Approved Baseline v1.0 (Aligned to Approved `PRD.md` v1.0)  
**Design Principle:** Strictly decoupled vertical modules so any future coding session only loads `AGENTS.md` + `STATUS.md` + 1–2 sections of this document (keeping context under 30–35%).

> **Section Index (Read On-Demand Only):**  
> `§1` Tech Stack, Ports & Environment · `§2` Repo Structure & Decoupling Rules · `§3` Database Schema · `§4` Deterministic Core Contracts (Inventory, PO State Machine, Clock, 3-Way Match) · `§5` Built-In 5-Persona Mailroom (`EmailTransport`) · `§6` Multi-Agent Layer & Lex Contracts (`gemini-3.5-flash-lite`) · `§7` Planning Intelligence Formulas · `§8` Observability, Evals & Autonomy Tracking · `§9` API Surface & UI Views · `§10` Phased Build Slices (Session Plan)

---

## §1. Tech Stack, Ports & Environment

Verified against this machine's WSL Ubuntu environment (`Python 3.12.3`, `uv`, `Node v26.3.0`, `npm`, `git`, `$GEMINI_API_KEY` set).

| Layer | Choice | Why |
|---|---|---|
| **Backend** | Python 3.12 + **FastAPI** + Pydantic v2 | Clean, fast REST API; native ecosystem for Gemini SDK, math/planning, and Arize Phoenix. |
| **Package Manager** | **`uv`** (`pyproject.toml` + `.venv`) | Fast, isolated, reproducible Python environment with zero global pip pollution. |
| **Database** | **SQLite** via SQLAlchemy 2.0 (`data/inv_plan.db`) | Zero setup, single local file, instant reset for testing; trivially migrates to Postgres in Phase 2. |
| **LLM SDK** | **`google-genai`** (`gemini-3.5-flash-lite`) | Official Google SDK with native multimodal PDF input and strict Pydantic structured outputs (`response.parsed`). |
| **Frontend** | **React + Vite + Tailwind CSS** (TypeScript) | Clean, responsive Gmail-style 5-persona Mailroom, inline Alert CTAs, and live Simulated Clock bar. |
| **Observability / Evals** | SQLite summary tables + **OpenTelemetry** + optional local **Arize Phoenix** (`phoenix serve`) | Non-technical plain-English UI inside the app, plus zero-rework multi-step trace trees when future agents arrive. |
| **Testing** | **`pytest`** (backend unit + integration) + declarative YAML golden evals | Every deterministic rule is 100% tested without burning LLM tokens; Lex accuracy is verified via YAML evals. |

### Fixed Local Ports & Paths
- **Backend API:** `http://localhost:8000` (interactive docs at `http://localhost:8000/docs`)
- **Frontend UI:** `http://localhost:5173` (Vite proxies `/api/*` → `http://localhost:8000`)
- **Optional Arize Phoenix UI:** `http://localhost:6006` (started only when desired via `uv run phoenix serve`)
- **Local Data Directory (gitignored):** `data/inv_plan.db` and `data/attachments/` (stored PDF invoices)
- **Required Env Var:** `GEMINI_API_KEY` (already set in shell); optional `ENABLE_PHOENIX=true`

---

## §2. Repo Structure & Hard Decoupling Rules

The codebase is split into **independent domain modules** so a session working on one slice never needs to read unrelated modules.

```text
inv_plan/
├── AGENTS.md                   # Always-read compact operating manual (~100 lines)
├── STATUS.md                   # Always-read live session baton (~50 lines)
├── PRD.md                      # Product requirements (read on demand)
├── DESIGN.md                   # This file (read specific § sections on demand)
├── pyproject.toml              # Python dependencies (managed by uv)
├── backend/
│   ├── main.py                 # FastAPI app entrypoint & router mounting
│   ├── core/                   # Shared primitives ONLY: config, db, SimulatedClock, enums
│   ├── inventory/              # Parts, ledger, consumption, Inventory Position trigger
│   ├── po/                     # PO state machine, GRN, 3-way match math, quarantine, ActionItems
│   ├── mailroom/               # Swappable EmailTransport + built-in 5-persona DB inbox + PDF storage
│   ├── planning/               # 12m EWMA forecast, Supplier Reliability Score, Effective LT, Dynamic ROP/S
│   ├── observability/          # Agent-agnostic AgentTracer (SQLite + OTel), YAML EvalRunner, AutonomyPolicy
│   ├── agents/
│   │   ├── base.py             # BaseAgent contract (Lex today; future Manager Copilot tomorrow)
│   │   ├── llm_client.py       # Thin google-genai wrapper (structured output + token/cost metering)
│   │   └── lex/                # Lex ONLY: email triage, PDF invoice parser, draft composer, memory notes
│   ├── orchestrator/           # Thin workflow glue & REST routes (ONLY layer composing domain modules)
│   ├── seed/                   # Realistic 15-part, Sup1–Sup3, 12-month history seeder + PDF fixture generator
│   └── evals/                  # Declarative golden YAML suites (lex.yaml, future copilot.yaml) + PDF fixtures
├── tests/                      # Mirrors backend/ module folders (unit + slice integration tests)
├── frontend/
│   ├── package.json
│   └── src/
│       ├── api/                # Typed REST client (one file per domain area)
│       ├── components/         # Layout, SimulatedClockBar, AlertCard (inline CTAs), StatusBadge
│       └── pages/              # AlertsQueue, Inventory, POTracker, Mailroom, SuppliersPlanning, AIHealth
└── data/                       # Runtime SQLite DB & PDF attachments (gitignored)
```

### Hard Decoupling Rules (Enforced Across All Sessions)
1. **No sideways imports between domain modules:** `inventory/`, `po/`, `mailroom/`, `planning/`, and `agents/` must **never** import from each other's internal modules. They may only import from `core/` and `observability/`.
2. **Only `orchestrator/` composes workflows:** When an action spans multiple domains (e.g., *Mailroom receives email → Lex triages → PO state updates → Alert created*), that sequence lives strictly in `orchestrator/`.
3. **Agents never mutate business tables directly:** `agents/lex/` takes structured inputs and returns strict Pydantic models (`EmailTriageResult`, `InvoiceExtractionResult`, `EmailDraftResult`). `orchestrator/` validates guardrails and calls `po/` or `inventory/` services to persist changes.
4. **Never call system wall-clock time in business logic:** Every module uses `core.clock.now()` (the `SimulatedClock`) so time-travel testing and SLA timers stay 100% consistent.
5. **Each module owns its own tests:** `tests/inventory/`, `tests/po/`, `tests/planning/`, `tests/mailroom/`, `tests/lex/`, `tests/orchestrator/` run independently via `uv run pytest tests/<module>`.

---

## §3. Database Schema (SQLite / SQLAlchemy 2.0)

Every table maps directly to the PRD entities with clean foreign keys and audit fields.

| Table | Key Columns | Notes |
|---|---|---|
| `system_state` | `key` (PK), `value` | Stores `sim_now` (ISO timestamp) so Simulated Clock survives restarts. |
| `parts` | `id`, `sku` (unique), `name`, `criticality` (`CRITICAL`/`STANDARD`/`LOW`), `on_hand_stock`, `quarantined_stock`, `rop`, `replenishment_point`, `target_coverage_days`, `manual_demand_override` | `on_order_stock` is derived cleanly from open POs (never drifts out of sync). |
| `suppliers` | `id`, `persona_key` (`sup1`/`sup2`/`sup3`), `name`, `email`, `phone`, `reliability_score`, `otd_rate`, `invoice_accuracy_rate`, `response_rate` | Updated deterministically by `planning/` on PO/invoice/SLA events. |
| `supplier_memory_notes` | `id`, `supplier_id`, `note`, `source` (`LEX_LEARNED`/`MANAGER_ADDED`), `created_at`, `updated_at` | Editable qualitative learnings read by Lex before drafting emails/alerts. |
| `part_supplier_terms` | `id`, `part_id`, `supplier_id`, `supplier_rank` (`1`=Primary, `2+`=Backup), `agreed_unit_price`, `currency`, `quoted_lead_time_days`, `effective_lead_time_days`, `lead_time_std_dev`, `moq`, `last_validated_at` | Powers primary ordering + backup supplier failover. |
| `inventory_transactions` | `id`, `part_id`, `type` (`CONSUMPTION`/`RECEIPT_RELEASED`/`ADJUSTMENT`), `qty_delta`, `timestamp`, `note` | Immutable 12+ month ledger for demand forecasting. |
| `purchase_orders` | `id`, `po_number` (unique), `part_id`, `supplier_id`, `qty`, `unit_price`, `status`, `notes`, `created_at`, `sent_at`, `promised_eta`, `revised_eta`, `follow_up_count`, `pre_eta_checked`, `next_sla_check_at` | Preserves original `promised_eta` separately from `revised_eta` for truthful OTD scoring. |
| `po_communications` | `id`, `po_id` (nullable), `thread_id`, `direction` (`OUTBOUND`/`INBOUND`), `status` (`DRAFT_PENDING_APPROVAL`/`SENT`/`RECEIVED`/`REJECTED`), `category`, `from_persona`, `to_personas` (JSON), `cc_personas` (JSON), `subject`, `body`, `attachment_path`, `ai_classification`, `ai_extracted_json`, `ai_confidence`, `approval_outcome` (`APPROVED_AS_IS`/`APPROVED_EDITED`/`REJECTED`), `timestamp` | Single source of truth for the 5-Persona Mailroom & PO audit trail. |
| `goods_receipts` | `id`, `po_id`, `received_qty`, `received_at`, `condition`, `status` (`HELD_QUARANTINE`/`RELEASED`) | Logging a GRN triggers matching if an invoice already arrived, or awaits PDF invoice. |
| `invoices` | `id`, `po_id`, `comm_id`, `invoice_number`, `pdf_path`, `extracted_qty`, `extracted_unit_price`, `extracted_total`, `match_status` (`MATCHED`/`MISMATCH_PRICE`/`MISMATCH_QTY`/`MISMATCH_BOTH`), `discrepancy_summary`, `version`, `received_at` | Supports multiple invoice versions per PO (v1 mismatched → v2 corrected). |
| `action_items` | `id`, `type`, `severity` (`CRITICAL`/`HIGH`/`MEDIUM`/`INFO`), `urgency_score`, `po_id`, `part_id`, `draft_comm_id`, `title`, `summary`, `suggested_actions_json`, `cta_payload_json`, `status` (`OPEN`/`RESOLVED`/`DISMISSED`), `assigned_team`, `created_at`, `resolved_at` | Drives the Home Screen Actionable Alerts Queue + inline CTAs. |
| `agent_traces` | `id`, `agent_name`, `task_type`, `trigger_summary`, `linked_entity_type`, `linked_entity_id`, `input_summary`, `output_summary`, `confidence`, `tool_calls_json`, `raw_prompt`, `raw_response`, `latency_ms`, `input_tokens`, `output_tokens`, `cost_inr`, `human_feedback`, `timestamp` | Agent-agnostic observability backing the in-app **AI Activity & Health** tab. |
| `eval_runs` | `id`, `agent_name`, `suite_name`, `total_cases`, `passed_cases`, `pass_rate`, `results_json`, `model_name`, `timestamp` | Stores historical scorecard runs per agent. |
| `autonomy_policies` | `id`, `agent_name`, `action_category`, `mode` (`APPROVAL_REQUIRED`/`AUTO_SEND`), `approved_as_is_count`, `approved_edited_count`, `rejected_count`, `updated_at` | Locked to `APPROVAL_REQUIRED` for all Lex emails in MVP; collects performance metrics for future autonomy graduation. |

---

## §4. Deterministic Core Contracts

### 4.1 Inventory Position Trigger (`backend/inventory/`)
- **Formula:**
  - `on_order_stock = SUM(po.qty)` across active POs for `part_id` where `status NOT IN (CLOSED, CANCELLED)` minus any already-released receipt qty.
  - `inventory_position = on_hand_stock + on_order_stock`.
- **On stock withdrawal (`consume_stock(part_id, qty, note)`):**
  1. Validate `qty > 0` and `qty <= on_hand_stock`.
  2. Decrement `on_hand_stock` and insert `InventoryTransaction(type=CONSUMPTION, qty_delta=-qty)`.
  3. If `inventory_position < part.rop`:
     - Compute `raw_qty = part.replenishment_point - inventory_position`.
     - Round up to primary supplier's `moq` if `raw_qty < moq`.
     - Return a `ReorderTriggered(part_id, order_qty, primary_supplier_id)` event to `orchestrator/`.
  4. **Important MVP Nuance (Draft-First Safety):** `orchestrator/` immediately creates the PO in status **`PENDING_DISPATCH_APPROVAL`** (which already counts toward `on_order_stock`!) and asks Lex to draft the PO email for `InventoryManager` approval. That guarantees a second withdrawal *before* the manager approves the PO email still sees `inventory_position >= ROP` and **never creates a duplicate PO**.
  5. If `on_hand_stock == 0` while an open PO exists with `eta > sim_now + 3d`, emit `AcceleratedBurnAlert`.

### 4.2 PO State Machine (`backend/po/state_machine.py`)
Valid states & transitions are enforced via an explicit allow-list dictionary (illegal transitions raise `InvalidPOTransitionError`):

- `PENDING_DISPATCH_APPROVAL` → `SENT_AWAITING_ACK` (manager approves & sends PO draft) | `CANCELLED`
- `SENT_AWAITING_ACK` → `ACKNOWLEDGED` | `EXCEPTION_REVIEW` (Scen 2) | `UNRESPONSIVE_ESCALATED` (Scen 3) | `CANCELLED`
- `EXCEPTION_REVIEW` → `PENDING_DISPATCH_APPROVAL` (manager revises PO → Lex drafts revised PO) | `CANCELLED` (cancel or switch to backup supplier, which creates a new PO for backup)
- `UNRESPONSIVE_ESCALATED` → `ACKNOWLEDGED` (late reply) | `EXCEPTION_REVIEW` | `CANCELLED` (switch supplier)
- `ACKNOWLEDGED` → `DELAYED_CRITICAL` (Scen 1A critical/high risk) | `ACKNOWLEDGED` (Scen 1A non-critical safe delay updates `revised_eta`) | `OVERDUE` (Scen 1B) | `RECEIVED_PENDING_INVOICE` (GRN logged)
- `DELAYED_CRITICAL` → `RECEIVED_PENDING_INVOICE` | `CANCELLED`
- `OVERDUE` → `RECEIVED_PENDING_INVOICE` | `CANCELLED`
- `RECEIVED_PENDING_INVOICE` → `QUARANTINED_INVOICE_MISMATCH` (Scen 1C mismatch) | `CLOSED` (3-way match passed → stock released)
- `QUARANTINED_INVOICE_MISMATCH` → `CLOSED` (corrected PDF invoice passes 3-way match → quarantined stock released) | `CANCELLED`

### 4.3 Deterministic 3-Way Match (`backend/po/matching.py`)
Pure Python arithmetic—zero LLM involvement:
- Compares `Invoice` (extracted by Lex) vs. `PurchaseOrder` vs. `GoodsReceipt`:
  - `qty_ok = (invoice.extracted_qty == po.qty == grn.received_qty)`
  - `price_ok = abs(invoice.extracted_unit_price - po.unit_price) <= 0.50` (₹0.50 rounding tolerance)
  - `math_ok = abs((invoice.extracted_qty * invoice.extracted_unit_price) - line_subtotal) <= 1.00`
- **Match Passed:** `GRN.status = RELEASED`, `part.quarantined_stock` (if held) decremented, `part.on_hand_stock += grn.received_qty`, `InventoryTransaction(RECEIPT_RELEASED)` logged, `PO.status = CLOSED`.
- **Match Failed:** `GRN.status = HELD_QUARANTINE`, `part.quarantined_stock += grn.received_qty` (first time only), `PO.status = QUARANTINED_INVOICE_MISMATCH`, exact rupee/unit discrepancy string returned to `orchestrator/` so Lex drafts the supplier correction email + manager alert.

### 4.4 Simulated Clock & Synchronous SLA Watcher (`core/clock.py`, `orchestrator/sla_watcher.py`)
Advancing the clock (`+1d`, `+3d`, `+7d`, or `Jump to PO ETA`) immediately evaluates all open POs synchronously and returns a plain-English summary of triggered events:
- **Ack SLA (`SENT_AWAITING_ACK`):** 24h for `CRITICAL`, 48h for `STANDARD`/`LOW`.
  - Breach 1 → Lex drafts Follow-up #1; `follow_up_count = 1`
  - Breach 2 → Lex drafts Follow-up #2; `follow_up_count = 2`
  - Breach 3 → PO moves to `UNRESPONSIVE_ESCALATED`, high-priority Alert with **Switch to Backup Supplier** CTA.
- **Pre-ETA Check-in (`ACKNOWLEDGED`):** At `effective_eta - 3 days`, if `part.criticality == CRITICAL` OR `supplier.reliability_score < 75` and `not po.pre_eta_checked` → Lex drafts dispatch check-in email (`pre_eta_checked = True`).
- **Overdue Detection (`ACKNOWLEDGED` / `DELAYED_CRITICAL`):** At `effective_eta + 1 day` with no GRN → PO moves to `OVERDUE`, Lex drafts urgent status email, raises overdue alert, records late event on supplier.
- **Quarantine SLA (`QUARANTINED_INVOICE_MISMATCH`):** After 48h waiting for corrected PDF → Lex drafts reminder; after 4 days → escalates alert severity to `CRITICAL`.

---

## §5. Built-In 5-Persona Mailroom (`backend/mailroom/`)

Designed behind a minimal abstract interface so Phase 2 swaps in real IMAP/Gmail without touching any other module:

```python
class EmailTransport(Protocol):
    def send(self, msg: OutboundMessage) -> StoredEmail: ...
    def list_inbox(self, persona_key: str) -> list[StoredEmail]: ...
    def list_sent(self, persona_key: str) -> list[StoredEmail]: ...
    def get_thread(self, thread_id: str) -> list[StoredEmail]: ...
```

- **5 Personas:** `inventory_manager`, `sup1`, `sup2`, `sup3`, `lex`.
- **Rules Enforced in Mailroom Service:**
  - Any of `inventory_manager`, `sup1`, `sup2`, `sup3` can manually compose or reply (with `To`, `CC`, and `.pdf` attachment) to any persona.
  - Direct manual sending **from `lex`** is blocked at the API layer (`403 Forbidden`)—only the `orchestrator/` can dispatch a Lex email, and in MVP *only* after `InventoryManager` approves a `DRAFT_PENDING_APPROVAL` record.
  - Whenever an email arrives where `lex` is in `To` or `CC` (from `sup1`/`sup2`/`sup3`), `orchestrator/` synchronously triggers Lex's inbound triage (and PDF invoice parser if a `.pdf` attachment is present).
  - All timestamps use `core.clock.now()`.

---

## §6. Multi-Agent Platform & Lex Contracts (`backend/agents/`)

### 6.1 Multi-Agent Abstraction (`agents/base.py` & `agents/llm_client.py`)
- **`LLMClient`** wraps `google.genai.Client()` (reads `$GEMINI_API_KEY` automatically), defaults to **`gemini-3.5-flash-lite`**, enforces Pydantic `response_schema`, supports inline PDF bytes (`types.Part.from_bytes(..., mime_type="application/pdf")`), measures latency/tokens/INR cost, and passes every call through `AgentTracer`.
- **`BaseAgent`** defines a tiny common contract (`agent_name`, `eval_suite_path`) so adding a future `ManagerCopilotAgent` with SQL/charting tools plugs straight into `LLMClient`, `AgentTracer`, and the in-app **AI Activity & Health** tab with zero core refactoring.

### 6.2 Lex's Three Structured Contracts (`agents/lex/schemas.py`)
1. **`EmailTriageResult`** *(temperature `0.0`)*:
   - Inputs: email headers/body, thread history, open POs for that supplier, supplier's current `SupplierMemoryNote` list.
   - Output fields: `matched_po_number`, `intent` (`ACKNOWLEDGED` | `PRICE_CHANGE` | `LEAD_TIME_CHANGE` | `PARTIAL_AVAILABILITY` | `OUT_OF_STOCK` | `POST_ACK_DELAY` | `INVOICE_SUBMISSION` | `GENERAL_REPLY` | `AMBIGUOUS`), `confidence` (`0.0–1.0`), `proposed_unit_price`, `proposed_qty`, `proposed_eta_date`, `delay_reason`, `summary_for_manager`, `suggested_action`, `new_memory_note` (optional, only if genuinely noteworthy pattern), `reasoning`.
   - **Safety Guardrail:** If `confidence < 0.85` OR `matched_po_number` cannot be uniquely resolved → force `AMBIGUOUS`, do not mutate PO state, route straight to `InventoryManager` Alert.
2. **`InvoiceExtractionResult`** *(temperature `0.0`, multimodal PDF input)*:
   - Output fields: `invoice_number`, `po_reference`, `supplier_name`, `invoice_date`, `line_items` (`part_code`, `description`, `qty`, `unit_price`, `line_total`), `subtotal`, `tax_amount`, `grand_total`, `confidence`.
   - **Safety Guardrail:** Lex *only* extracts numbers; `po/matching.py` performs all arithmetic and match decisions.
3. **`EmailDraftResult`** *(temperature `0.2`)*:
   - Inputs: drafting intent/category, PO details, discrepancy or delay facts, recipient persona, `SupplierMemoryNote` list, persona directory (so Lex knows who to `CC`—e.g., always CC `inventory_manager` on supplier escalations/discrepancies).
   - Output fields: `to_personas`, `cc_personas`, `subject`, `body`.
   - **Safety Guardrail:** System prompt explicitly forbids making unauthorized commercial commitments (accepting price hikes, promising extra payments, waiving penalties). Every draft is saved as `DRAFT_PENDING_APPROVAL` and attached to an Alert card.

---

## §7. Planning Intelligence Formulas (`backend/planning/`)

100% deterministic, zero LLM calls, unit-tested independently:

1. **12-Month EWMA Demand Forecast (`forecast.py`):**
   - Aggregates last 12 monthly consumption buckets from `inventory_transactions` (using `sim_now` as anchor).
   - Computes Exponentially Weighted Moving Average ($\alpha = 0.25$) → `avg_daily_demand` ($\bar{D}$) and `daily_demand_std` ($\sigma_D$).
   - If `> 50%` of months have zero usage, sets `is_intermittent = True` and uses a conservative rolling-peak floor so slow-moving critical spare parts aren't under-stocked.
   - Adds `part.manual_demand_override` if set by the manager.
2. **Supplier Reliability Score & Effective Lead Time (`scoring.py`):**
   - Weighted 0–100 score across rolling 12-month history:
     - **OTD Rate (40%):** `% delivered <= original promised_eta`, penalized by average days late (`-2 pts/day` capped).
     - **Lead Time Consistency (25%):** Based on $\sigma_L$ relative to quoted lead time.
     - **Invoice Accuracy (20%):** `% of POs matched on invoice v1 without quarantine`.
     - **Responsiveness (15%):** `% of POs acknowledged without needing follow-up nudges`.
   - **Effective Lead Time:** If `< 3` closed deliveries → `quoted_lead_time_days`; if `≥ 3` → `max(actual_mean, 0.25 * quoted + 0.75 * actual_mean)` so chronically late suppliers (`Sup2`) can't hide behind optimistic quotes.
3. **Dynamic ROP & Replenishment Point (`rop.py`):**
   - $Z$ factor: `CRITICAL = 2.05` (98%), `STANDARD = 1.65` (95%), `LOW = 1.28` (90%).
   - $\text{SS} = Z \times \sqrt{(\bar{L}_{\text{eff}} \times \sigma_D^2) + (\bar{D}^2 \times \sigma_L^2)}$
   - $\text{ROP}_{\text{rec}} = \lceil \bar{D} \times \bar{L}_{\text{eff}} + \text{SS} \rceil$
   - $S_{\text{rec}} = \text{ROP}_{\text{rec}} + \lceil \bar{D} \times \text{target\_coverage\_days} \rceil$
   - Surfaces a plain-English `ActionItem` recommendation whenever `ROP_rec != part.rop` or `S_rec != part.replenishment_point`, with 1-click **Approve New ROP/S** CTA.

---

## §8. Multi-Agent Observability, Evals & Autonomy (`backend/observability/`)

Implements the locked **Lightweight Hybrid** architecture:

1. **`AgentTracer` (`tracer.py`):**
   - Context manager / decorator used by `LLMClient` and every agent (`with tracer.span(agent_name="lex", task_type="email_triage", ...) as span:`).
   - Always writes a clean, plain-English row to SQLite `agent_traces` (including INR cost estimate based on `gemini-3.5-flash-lite` token counts).
   - Also emits a standard OpenTelemetry span—no-op unless `ENABLE_PHOENIX=true`, in which case it streams straight to local **Arize Phoenix** (`http://localhost:6006`) via `phoenix.otel.register(project_name="inv-plan")`.
2. **Declarative Per-Agent YAML Evals (`eval_runner.py` + `backend/evals/lex.yaml`):**
   - Each agent registers a human-readable YAML test suite. Example test case structure:
     ```yaml
     - id: scen2_price_hike
       name: "Supplier replies with messy price hike + longer lead time"
       task: email_triage
       input: { from: sup3, po_number: "PO-1005", body: "Hi, steel prices jumped, now Rs 135/pc and need 35 days" }
       expect: { intent: PRICE_CHANGE, proposed_unit_price: 135, min_confidence: 0.85 }
     ```
   - Clicking **"Run Evals"** in the UI (or running `uv run pytest` / CLI) executes the suite against `gemini-3.5-flash-lite`, saves an `EvalRun` row, and displays a plain-English green/red scorecard explaining every failure.
3. **Autonomy Progression Metrics (`autonomy.py`):**
   - Every time `InventoryManager` acts on a Lex email draft (`Approve as-is`, `Edit & Approve`, `Reject`), increments the corresponding `AutonomyPolicy` counter so the UI displays Lex's live accuracy per email category (ready to unlock graduated auto-send post-MVP).

---

## §9. API Surface & Frontend Views

### Key REST Endpoints (`backend/orchestrator/routes/`)
- `GET/POST /api/clock`, `POST /api/clock/advance`, `POST /api/clock/jump-eta/{po_id}`
- `GET /api/parts`, `POST /api/parts/{id}/consume`, `PATCH /api/parts/{id}/rop`
- `GET /api/pos`, `GET /api/pos/{id}`, `POST /api/pos/{id}/grn`, `PATCH /api/pos/{id}` (inline CTA updates / notes), `POST /api/pos/{id}/switch-supplier`
- `GET /api/alerts`, `POST /api/alerts/{id}/execute-cta`, `POST /api/alerts/{id}/dismiss`
- `GET /api/mailroom/{persona}/inbox`, `GET /api/mailroom/{persona}/sent`, `POST /api/mailroom/send` (multipart for PDF attachment), `POST /api/mailroom/drafts/{comm_id}/approve` (supports edited body), `POST /api/mailroom/drafts/{comm_id}/reject`
- `GET /api/suppliers`, `POST/PATCH/DELETE /api/suppliers/{id}/memory`, `POST /api/planning/recalculate`
- `GET /api/observability/traces` (filterable by `agent_name`), `GET /api/observability/autonomy`, `GET/POST /api/evals/{agent_name}/run`
- `POST /api/admin/reset-seed` (resets demo database to fresh seeded state in 1 second)

### 6 Clean Frontend Views (`frontend/src/pages/`)
Persistent top bar across all views: **Simulated Clock Bar** (current sim date, `+1d`, `+3d`, `+7d`, `Reset Seed` button) + open Alert badge count.

1. **Actionable Alerts (Home):** Priority-sorted alert cards with concise summary, stockout/cost impact, editable Lex email draft preview (when present), and **embedded inline CTAs** (*Approve & Send Draft*, *Update PO Price/ETA*, *Add Note*, *Switch to Backup Supplier `Sup1`*, *Approve New ROP/S*).
2. **Inventory & Parts:** Live `on_hand`, `quarantined`, `on_order`, `inventory_position`, ROP/$S$, primary & backup suppliers, and inline **"Withdraw Stock"** modal.
3. **PO Tracker:** Filterable PO table + expandable detail drawer showing full state timeline, GRN log button, invoice match breakdown, notes, and linked email thread.
4. **5-Persona Mailroom:** Gmail-style inbox with 1-click persona switcher (`InventoryManager`, `Sup1`, `Sup2`, `Sup3`, read-only `Lex`), full Compose/Reply with `To`/`CC` and `.pdf` upload, and clear badges showing Lex's triage tag on each received email. *(No hardcoded demo presets yet—saved post-build once we craft scenarios together.)*
5. **Suppliers & Planning:** Side-by-side `Sup1`/`Sup2`/`Sup3` scorecards (reliability score, quoted vs. actual lead time chart, OTD %, invoice accuracy %), editable **Supplier Memory** notes, and Dynamic ROP/S recommendation cards with math breakdown.
6. **AI Activity & Health:** Agent dropdown (`All | Lex | Future Agents`), plain-English activity feed with cost (₹) and expandable raw prompt/output, **Lex Draft Trust Metrics** (approval rate per category), and 1-click **"Run Evals"** green/red scorecard.

---

## §10. Phased Build Slices (Fresh-Session Execution Plan)

Each slice is scoped to fit comfortably inside a single fresh session (< 30% context), with explicit test criteria and a clean git commit + `STATUS.md` update at the end.

| Slice | Goal & Modules Touched | Verification ("Done" Criteria) |
|---|---|---|
| **Pre-Slice 0** | Bootstrap repo (`git init`, `pyproject.toml` via `uv`, frontend scaffold, folder layout) + write **`AGENTS.md`** & **`STATUS.md`**. | `uv run pytest` and `npm run build` succeed on empty skeleton; git initial commit clean. |
| **Slice 1** | **Deterministic Core + Seed Data:** `core/`, `inventory/`, `po/`, `seed/` (15 realistic parts, `Sup1–Sup3` primary+backup, 12-month history). | Unit tests prove Inventory Position trigger (no duplicate PO on second withdrawal), all PO state transitions, 3-way match math, quarantine lock & release. |
| **Slice 2** | **5-Persona Mailroom + SLA Watcher + Lex Email Agent:** `mailroom/`, `observability/tracer.py`, `agents/llm_client.py`, `agents/lex/` (triage, drafts, memory), `orchestrator/` (email flow, SLA clock ticks, draft approval & inline CTA endpoints). | Integration tests pass with mocked LLM + live smoke test against `gemini-3.5-flash-lite` proving Scenarios 1A, 1B, 2, 3 & Draft-First approval. |
| **Slice 3** | **PDF Invoice Pipeline + Planning Intelligence:** Lex PDF invoice extraction → `po/` 3-way match quarantine loop + `planning/` (12m EWMA forecast, Supplier Reliability Score, Effective Lead Time, Dynamic ROP/S). | Tests pass with real generated sample PDF invoices (price mismatch, qty mismatch, clean match) + planning unit tests proving `Sup2` late deliveries raise Effective LT & ROP. |
| **Slice 4** | **React Operations Dashboard & Mailroom UI:** All 6 frontend views (Alerts Queue with inline CTAs, Inventory, PO Tracker, 5-Persona Mailroom with PDF upload, Suppliers & Planning, Simulated Clock Bar). | Live end-to-end manual walkthrough in browser across Scenarios 1–3, GRN, PDF invoice quarantine/release, and backup supplier switch. |
| **Slice 5** | **AI Activity & Health UI, YAML Eval Suite & Phoenix Hook:** `observability/eval_runner.py`, `evals/lex.yaml` (15–20 golden cases + PDF fixtures), `AIHealth` page, optional local `phoenix serve` verification. | Clicking **Run Evals** in UI scores Lex end-to-end with plain-English pass/fail breakdown; optional `ENABLE_PHOENIX=true` streams live spans to `localhost:6006`; full 7-scenario acceptance pass. |

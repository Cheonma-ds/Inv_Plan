# InvPlan — Demand-to-Delivery & Inventory Planning Prototype

> **Project Status:** Phase 1 MVP (Slices 1–5) + Phase 2 + PRD Fixes (`F-1.1`–`F-4.3`) + Phase 3 (`P3-1`–`P3-8`) + Phase 4 Manager Copilot (`P4-1`–`P4-4`) Complete & Verified  
> **Context:** A portfolio project built to practice **Product Management** and **Agentic Engineering**—translating recurring operational pain points gathered from Supply Chain Management (SCM) communities into a working, guardrailed prototype built with **Google Antigravity CLI**.
>
> *Note: This repository is an architectural and product prototype meant to demonstrate problem framing, system design, and agentic engineering practices rather than a packaged, end-user runnable distribution.*

---

## 1. Problem Discovery & Product Framing

Across SCM discussions, inventory and procurement teams consistently describe the same operational bottleneck: triggering a purchase order is easy, but everything *after* the PO is sent collapses into manual email tracking, scattered spreadsheets, and reactive firefighting. Critical supplier replies get buried, silent non-deliveries go unnoticed until a technician hits an empty bin, and mismatched invoices freeze usable stock.

**InvPlan** reframes the workflow from **chasing every PO manually** to **managing by exception** across six core pain points:

| Pain Point | Operational Failure Today | InvPlan Product & System Response |
|---|---|---|
| **1. Unacknowledged POs (Ghosting)** | Suppliers never reply to a PO; no automated follow-up cadence exists. | Deterministic SLA watcher tracks acknowledgment windows, triggers follow-up drafts, and escalates unresponsive suppliers. |
| **2. Missed Supplier Exceptions** | Price hikes, quantity cuts, or stockouts arrive by email and sit unread. | **Lex** triages inbound emails into structured intents, computes exact INR/stockout impact, and surfaces inline-CTA alerts. |
| **3. Unassessed Post-Ack Delays** | Delays after acknowledgment are either ignored or cause unnecessary panic. | Deterministic days-of-supply math distinguishes safe-buffer delays from `DELAYED_CRITICAL` stockout threats. |
| **4. Silent Non-Deliveries** | Suppliers miss promised ETAs without warning; discovered only at stockout. | Simulated clock + SLA watcher flags `OVERDUE` POs and penalizes supplier reliability scores immediately. |
| **5. PO–Invoice & Quality Mismatches** | Shipments arrive damaged or with wrong prices/quantities/credit notes; goods sit unusable in limbo. | Multimodal PDF extraction feeds deterministic 3-way matching (`PO vs. GRN vs. Invoice`, `invoice_qty == grn_received_qty` supporting partial receipts), Credit Note net settlement, Packing List cross-checks, and a physical `DAMAGED` GRN quarantine gate. |
| **6. Static, Blind Planning** | Reorder points ignore changing demand and chronic supplier lateness. | 90-day daily EWMA burn (`alpha = 0.30`), supplier-aware `Stockout In` urgency & cycle analytics, actual-vs-quoted lead time + communication delay tracking, and criticality-weighted safety stock generate explainable `ROP/T` recommendations. |

---

## 2. How It Works & 3-Minute Guided Tour

Every workflow in InvPlan follows a **human-in-the-loop (HITL)** loop:
1. **Events Enter:** Stock drops below ROP (via **Inventory & Parts** `Withdraw` or a **Making Team** email), time advances (**Simulated Clock** starting **Oct 1, 2026 1:00 PM IST** with **0 open POs** on reset), or a supplier replies/attaches a PDF or image in **Mailroom**.
2. **Lex Triages & Drafts:** Tool-using Agent Lex inspects live inventory, POs, alerts, supplier scorecards/memory, and AST-sandboxed Python calculations, reads emails/PDFs/images/`.csv` BOMs, schedules dynamic `LexReminder`s when safe, and returns structured outputs plus formatted `DRAFT_PENDING_APPROVAL` emails—**never mutating POs or stock silently**.
3. **Manager Decides in 1 Click:** **Open Alerts** (top `ClockBar`) surfaces exact INR impact, stockout dates, and inline CTAs (*Approve & Send PO*, *Edit PO*, *Switch to Backup Supplier*, *Release Usable Qty*, *Approve Net Settlement*).

### What to Click (3-Minute Browser Tour)
1. **Explore 90-Day Burn & Runway:** Open **Inventory & Parts** → click any `Stockout In` badge to open the **90-Day Cycle Analytics** drawer (`Stockout → PO → Replenishment → Stockout`), primary effective LT + communication buffer, and recommended PO quantity.
2. **Trigger a Replenishment PO:** Click **Withdraw** on `BRG-402` to drop Inventory Position below ROP. InvPlan auto-creates a `PENDING_DISPATCH_APPROVAL` PO (`On Order` stays `0`, button swaps to **View PO**), and Lex drafts a structured Markdown/`₹` dispatch email.
3. **Approve or Edit & Send:** Click **Open Alerts** in the header → use **Approve & Send PO** or **Edit PO** (opens `CreateEditPOModal` with per-supplier suggested qty & reasoning) → PO moves to `SENT_AWAITING_ACK`.
4. **Watch the SLA Clock & Dynamic Deferral:** Open **Mailroom** → switch persona to **`Apex Precision (Sup1)`** → reply citing the PO with *"We confirm, but can only share tracking details after 4 days."* Lex uses its Python sandbox to verify runway safety and schedules a 4-day `LexReminder` without nagging today; inspect full `tool_calls` in **AI Activity & Health**.
5. **Test a Supplier Exception:** Reply as a supplier with *"Unit price is now ₹540/unit and ETA slips to 2026-10-20."* → return to **Open Alerts** to see price variance, projected stockout gap, and 1-click **Switch to Backup Supplier**.

---

## 2. Agentic Engineering Philosophy: Right Tool for Each Job

A central design principle in InvPlan is knowing **where not to use an LLM**. Using generative models for inventory arithmetic, state transitions, or timer math introduces hallucination risk where deterministic code is exact, fast, and auditable. Conversely, rigid parsers break on messy supplier emails and inconsistent PDF invoice layouts.

| Layer | Approach | Responsibility |
|---|---|---|
| **Deterministic Core** | Pure Python + SQLAlchemy (`inventory/`, `po/`, `core/`) | Inventory position (`on_hand + on_order + quarantined`, excluding `PENDING_DISPATCH_APPROVAL` from `on_order`), duplicate-PO prevention, MOQ rounding, explicit allow-listed PO state machine, partial-delivery 3-way match arithmetic (`invoice == grn`), and persisted `SimulatedClock` (IST). |
| **Planning Intelligence** | Deterministic Statistics (`planning/`) | 90-day daily EWMA demand forecasting (`alpha = 0.30`) with same-day demand freeze & rollover settlement, 90-day cycle reconstruction, 4-factor Supplier Reliability Score (`40/25/20/15`, `avg_response_days`, `cycles_90d`), effective lead time blending, and Z-factor dynamic `ROP/T`. |
| **Targeted AI Agent (`Lex`)** | `google-genai` (`gemini-3.5-flash-lite`) + Strict Pydantic Schemas + Tool Loop (`agents/lex/`, `agents/sandbox.py`) | 13-tool read/calc/reminder/memory reasoning loop (`orchestrator/lex_tools.py`), AST-sandboxed Python calculation execution, multimodal email/PDF/image triage, multi-PDF invoice/credit-note/packing-list extraction, Making `.csv` BOM triage, and Markdown/`₹`-grounded outbound drafting. **Zero direct database writes.** |
| **Workflow Orchestration** | `orchestrator/` | Sole cross-module coordinator enforcing confidence guardrails, invoking domain services, and managing SLA timers & `LexReminder` wakeups. |

### Safety Guardrails & Human-in-the-Loop (HITL) Trust Boundaries
- **Draft-First Outbound Rule:** Every email drafted by Lex enters `DRAFT_PENDING_APPROVAL` and requires `InventoryManager` approval before dispatch. Outcomes (`APPROVED_AS_IS`, `APPROVED_EDITED`, `REJECTED`) are tracked per action category in `autonomy_policies` to measure trust before graduating categories in later phases.
- **Confidence & Currency Escalation Gates:** Inbound triage or invoice extractions below `0.85` confidence (or missing key identifiers, encrypted/unreadable PDFs, multi-PO consolidated PDFs, or foreign-currency `USD` quotes vs. `INR` POs) force an `AMBIGUOUS`, `CURRENCY_MISMATCH`, or `INVOICE_EXTRACTION_REVIEW` escalation with **zero blind PO or inventory state mutation**.
- **Billing & Quality Quarantine Locks:** Goods with mismatched invoices or `condition="DAMAGED"` GRNs are held in `quarantined_stock` (excluded from usable `on_hand_stock`) until resolved, while still accounted for in inventory position checks so no duplicate replenishment PO is spawned during a dispute.

---

## 3. System Architecture & Decoupling

InvPlan is structured into strictly isolated vertical modules so agents and developers can reason about one domain without loading unrelated context:

```mermaid
flowchart TB
    subgraph Core["1. Deterministic Core & Planning"]
        INV["inventory/<br/>Ledger, ROP Trigger, Quarantine"]
        PO["po/<br/>State Machine, GRN, 3-Way Match"]
        PLAN["planning/<br/>EWMA Forecast, Supplier Score, Dynamic ROP"]
        MAIL["mailroom/<br/>7-Persona Inbox behind EmailTransport"]
    end

    subgraph Agents["2. Agentic Layer (Read-Only/Structured Output)"]
        BASE["agents/base.py & llm_client.py"]
        LEX["agents/lex/<br/>Triage · PDF Extractor · Draft Composer"]
    end

    subgraph Orch["3. Orchestration & Observability"]
        WF["orchestrator/<br/>Workflows, SLA Watcher, FastAPI Routes"]
        OBS["observability/<br/>AgentTracer (SQLite/OTel), Autonomy, YAML Evals"]
    end

    UI["frontend/<br/>React + TypeScript + Tailwind Operations Dashboard"]

    UI <--> WF
    WF --> INV
    WF --> PO
    WF --> PLAN
    WF --> MAIL
    WF --> LEX
    LEX --> BASE
    WF -.-> OBS
    LEX -.-> OBS
```

### Enforced Architectural Boundaries
Hard decoupling rules in [`AGENTS.md`](AGENTS.md) are verified programmatically via AST checks in [`tests/test_architecture.py`](tests/test_architecture.py):
- **No sideways imports** between `backend/inventory/`, `backend/po/`, `backend/mailroom/`, `backend/planning/`, and `backend/agents/`. Domain modules may only import `backend/core/` and `backend/observability/`.
- **Single orchestration layer:** Only `backend/orchestrator/` composes multi-domain workflows and exposes FastAPI routes.
- **Swappable mail transport:** All mailroom reads, sends, multi-attachment uploads (`.pdf`, `.csv` BOMs, `.jpg`/`.png` damage photos), and draft approvals/rejections route strictly through the `EmailTransport` protocol in `backend/mailroom/transport.py` (currently backed by a simulated 7-persona DB mailroom: `inventory_manager`, `making`, `procurement`, `Apex Precision (Sup1)`, `Bharat Fasteners (Sup2)`, `Deccan Electro-Spares (Sup3)`, and read-only `lex`).
- **Deterministic simulated time:** Business logic uses `core.clock.now()` (`SimulatedClock`) instead of system wall-clock time so multi-day SLA timelines, overdue penalties, and quarantine escalations are reproducible.
- **Agent-agnostic observability:** `backend/observability/` keys all traces, feedback, eval runs, and autonomy policies by `agent_name` so future agents can plug into the same backbone without schema rework.

---

## 4. How the Prototype Was Built (Agentic Engineering Workflow)

This prototype was developed iteratively using **Google Antigravity CLI** (`agy`) with deliberate context-management and verification discipline:

1. **Product & Technical Specs First:** Locked scope and math in [`PRD.md`](PRD.md) and modular boundaries in [`DESIGN.md`](DESIGN.md) before writing application code.
2. **Context Discipline via Operating Manual:** Maintained [`AGENTS.md`](AGENTS.md) (hard rules and non-negotiable guardrails) and [`STATUS.md`](STATUS.md) (a compact live baton) so each vertical slice ran in a clean, focused session.
3. **Vertical Slice Delivery (Slices 1–5):**
   - **Slice 1:** Deterministic inventory ledger, PO state machine, 3-way match math, and 15-SKU/12-month historical seed data.
   - **Slice 2:** 5-persona `EmailTransport` mailroom, SLA watcher, Lex email triage/drafting, and agent tracing.
   - **Slice 3:** Multimodal PDF invoice extraction pipeline and deterministic planning intelligence engine.
   - **Pre-Slice 4 Audit & Hardening:** Identified and resolved 18 runtime, domain-math, decoupling, and API gaps documented in [`docs/resolved_problems.md`](docs/resolved_problems.md).
   - **Slice 4:** React + TypeScript + Tailwind operations dashboard (`AlertsPage`, `InventoryPage` with direct `Withdraw` & Lex-assisted `Create PO` workflows, `POTrackerPage`, `MailroomPage`, `SuppliersPlanningPage`, persistent `ClockBar`).
   - **Slice 5:** Agent-agnostic YAML eval runner (`backend/evals/lex.yaml` — 30 golden cases across email triage, PDF extraction, stock requests, and draft compliance), `AIHealthPage` trust/eval dashboard, and optional OpenTelemetry/Arize Phoenix integration.
4. **Phase 2 & 50-Scenario Live Stress Testing (`docs/phase2/`, `docs/PRD_FIXES.md`):**
   - **Phase 2 (`docs/phase2/PRD_P2.md`):** Added dispatch logistics tracking (`promised_dispatch_date`, `carrier_name`, `tracking_number`, 2-day deferral/nudge SLA), Making-team stock request & minimum-quantity negotiation workflow, and Procurement escalation.
   - **50-Scenario Live Evaluation & Fixes (`F-1.1`–`F-4.3`):** Executed 50 end-to-end live scenarios against `gemini-3.5-flash-lite` (`docs/test-scenarios/`), closed all 12 discovered bugs/gaps in [`docs/PRD_FIXES.md`](docs/PRD_FIXES.md), and achieved **`16/16 PASS`** on re-verification ([`results-fixes-retest.md`](docs/test-scenarios/results-fixes-retest.md)).
5. **Phase 3 (`docs/phase3/`, Slices `P3-1`–`P3-8`):**
   - Reset simulated clock & seed baseline to **Oct 1, 2026 1:00 PM IST** with a 90-day causal Jul–Sep simulation (**0 open POs** on seed), daily EWMA (`alpha = 0.30`) + cycle analytics, `ROP/T` standardization, `Edit PO` + unresolved dismissed-alert inventory warnings, and **Lex v2** (13 tools, AST-sandboxed calculation executor, dynamic N-day `LexReminder` scheduling, multimodal image/PDF triage, and `COPILOT_SYSTEM_PROMPT`).
   - Verified with **`124 passed, 25 skipped`** offline tests, **`43/43`** in `tests/agents/` (`RUN_LIVE_GEMINI=1`, **25/25 live Gemini tests**), **`34/34` (`100.0%`)** live YAML evals, and full headless Chromium E2E across all 6 `PRD_P3.md §3` acceptance criteria.
6. **Phase 4 — Manager Copilot (`docs/phase4/`, Slices `P4-1`–`P4-4`):**
   - Built a persistent, multi-turn **Manager Copilot** drawer (`Ctrl+L` shortcut & `ClockBar` **Ask Lex** launcher) backed by strictly read-only LLM reasoning (`read_only_only=True`), deterministic supply-chain statistics (`run_statistical_analysis`), interactive SVG charts & diagrams (`render_chart` / `render_diagram`), zero-asterisk Markdown rendering (`CopilotMarkdown`), side-by-side non-closing SKU/PO/Alert deep links (`--copilot-offset`), and an in-chat **[Confirm & Execute]** / **[Revise / Clarify Intent]** / **[Cancel]** human-in-the-loop action workflow (`copilot_actions.py`), plus `copilot_query` traces in `AIHealthPage`.
   - Verified with **`132 passed, 27 skipped`** offline tests (incl. 5 AST checks), **`9/9`** live Copilot Gemini tests, **`36/36` (`100.0%`)** live YAML evals, `ruff` & `npm run build` clean, and headless Chromium Playwright E2E across the built Vite bundle + live FastAPI backend.

---

## 5. Tech Stack

- **Backend:** Python 3.12, FastAPI, SQLAlchemy 2.0, Pydantic v2, SQLite (`uv` for dependency management)
- **AI/Multimodal Extraction:** `google-genai` (`gemini-3.5-flash-lite`) with structured Pydantic outputs
- **Observability & Evals:** SQLite trace/autonomy/eval tables, OpenTelemetry SDK, optional Arize Phoenix export, declarative YAML eval suites (`PyYAML`, `fpdf2` for synthetic PDF fixtures)
- **Frontend:** React 19, TypeScript, Vite, Tailwind CSS v4, `lucide-react`

---

## 6. Quick Start (Local Development)

```bash
# 1. Install backend dependencies & run offline tests (132 passed, 27 live Gemini skipped)
uv sync
uv run pytest -q

# Optional: run full live Gemini suite + tool/sandbox/copilot unit tests
export GEMINI_API_KEY="your-api-key"
RUN_LIVE_GEMINI=1 uv run pytest -v tests/agents/

# 2. Run FastAPI backend
uv run uvicorn backend.main:app --reload --port 8000

# 3. Run React/Vite frontend (http://localhost:5173, proxies /api -> :8000)
cd frontend && npm install && npm run dev
```

---

## 7. Repository Structure & Documentation Map

```text
inv_plan/
├── README.md                     # Project overview, product framing, and architecture
├── PRD.md                        # Master Product Requirements Document (v1.0)
├── DESIGN.md                     # Modular Technical Design & Slice Plan (v1.0)
├── AGENTS.md                     # Agent operating manual & hard decoupling rules
├── STATUS.md                     # Live build baton across Slices 1–5, Phase 2 & PRD Fixes
├── docs/
│   ├── PRD_FIXES.md              # Fixes & gap closure spec (F-1.1–F-4.3)
│   ├── resolved_problems.md      # Pre-Slice 4 audit & resolution ledger (P1–P18)
│   ├── phase2/                   # Phase 2 PRD, design & status (logistics, Making, procurement)
│   ├── phase3/                   # Phase 3 PRD, design, slices & status (EWMA, Lex v2, UX)
│   ├── phase4/                   # Phase 4 PRD, design, slices & status (Manager Copilot)
│   └── test-scenarios/           # 50 live scenarios, top-10 demo playbook, fixtures & reports
├── backend/
│   ├── core/                     # Config, DB models, idempotent migrations, SimulatedClock (IST)
│   ├── inventory/                # Stock ledger, inventory position, ROP trigger, quarantine
│   ├── po/                       # PO state machine, GRN, 3-way match math, action alerts
│   ├── mailroom/                 # EmailTransport protocol, 7-persona inbox, multi-attachment policy
│   ├── planning/                 # 90d EWMA daily burn (α=0.30), cycle analytics, supplier scoring, ROP/T
│   ├── agents/                   # BaseAgent, ToolRegistry, AST sandbox, GeminiLLMClient, Lex
│   ├── observability/            # AgentTracer (tool_calls), AutonomyPolicy tracking, YAML EvalRunner
│   ├── orchestrator/             # Cross-domain WorkflowEngine, lex_tools, copilot_actions, SLA watcher
│   ├── seed/                     # 15-part, 3-supplier, 90-day causal Jul–Sep seed generator (0 open POs)
│   └── evals/                    # Golden YAML evaluation suite (lex.yaml — 36 cases)
├── frontend/                     # React + TypeScript + Tailwind operations, Copilot & AI health UI
└── tests/                        # Unit, integration, 27 live Gemini, and AST architecture tests
```

### Next-Stage Direction
Phases 1–4 (`P4-1`–`P4-4`) are complete and verified. Optional next-stage work includes progressive autonomy graduation per email category (`AutonomyPolicy`) and external IMAP/Gmail `EmailTransport` integration.

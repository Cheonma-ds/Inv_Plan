# Phase 4 Product Requirements Document (`PRD_P4.md`)

**Document Status:** Phase 4 Specification v2.0 (Updated with Manager Alignment Feedback — Builds on [`PRD_P3.md`](../phase3/PRD_P3.md) & verified `HEAD: d8d10a8`)  
**Target Users:** Inventory Management Team (`InventoryManager`)  
**AI Agent:** **Lex** (`gemini-3.5-flash-lite` via `google-genai` and `$GEMINI_API_KEY`)

---

## 1. Executive Summary & Goal

Phase 3 proved that **Lex** can accurately answer complex, multi-domain supply chain and InvPlan terminology questions via `COPILOT_SYSTEM_PROMPT` and 10 read/calculation tools (verified 7/7 live in `tests/agents/test_live_lex_copilot_readiness.py`).

**Phase 4** turns that verified reasoning capability into a full-stack, multi-turn **Manager Copilot** feature across backend and frontend, enabling the Inventory Manager to query data, run statistical analyses, view charts and diagrams, and propose **in-chat human-approved actions**—all from a persistent drawer with `Ctrl+L` access and 1-click navigation that keeps the chat open.

---

## 2. Core Product Requirements

### 2.1 Copilot Surface, `Ctrl+L` Shortcut & Persistent Open State
- **Global Shortcut (`Ctrl+L`) & `ClockBar` Launcher:**
  - Pressing **`Ctrl+L`** anywhere in InvPlan toggles the Copilot drawer open/closed (preventing browser focus hijack cleanly).
  - Accessible via an **"Ask Lex"** pill button (showing a `Ctrl+L` hint badge) in `ClockBar.tsx` adjacent to `Open Alerts` and `Reset Seed`.
  - Preserves the Phase 3 left sidebar (`Inventory & Parts`, `PO Tracker`, `Mailroom`, `Suppliers & Planning`, `AI Activity & Health` — keeping `Actionable Alerts` removed from the sidebar).
- **Non-Closing Deep-Link Navigation:**
  - `CopilotDrawer` renders as a non-blocking right-side docked/floating panel (`max-w-[480px]`).
  - Clicking any cited SKU, PO, or Alert deep-link navigates the main workspace (`InventoryPage` + `StockoutCycleDrawer`, `POTrackerPage` + PO drawer, or `AlertsPage`) **while keeping the Lex chat window open** side-by-side without covering or dismissing the chat.
- **Multi-Turn Conversation & Session Management:**
  - Supports multi-turn follow-ups within a persisted chat session, creating a **New Chat**, switching between saved sessions, and clean wiping when **Reset Seed** runs.
- **Quick-Start Analytical Prompts (Zero Hardcoded Fake Answers):**
  - Empty chat state displays realistic analytical starter chips (urgency ranking, blindspot audit, supplier statistical comparison + chart, open-PO bottleneck diagram, and `ROP/T` explanation) that send real queries to Lex.

### 2.2 Read-Only Reasoning + In-Chat Human-Confirmed Action Execution & Intent Revision
- **Strict Read-Only Tool Execution During LLM Reasoning:**
  - During `generate_with_tools`, Lex has **zero direct DB write tools** (`read_only_only=True`). Lex can never silently mutate POs, stock, alerts, emails, or supplier records on its own.
- **In-Chat Proposed Action Cards (`[Confirm & Execute]` / `[Revise / Clarify Intent]`):**
  - When the Manager asks Lex to perform an InvPlan action (e.g., create/dispatch a manual PO, approve/reject a pending draft email, switch to backup supplier, update PO ETA/notes, withdraw stock, dismiss/reopen an alert, apply an `ROP/T` recommendation, recalculate planning, record a GRN, save a supplier memory note, or schedule a `LexReminder`), Lex inspects live DB state and returns a structured **`proposed_actions`** plan inside the chat bubble.
  - Each action card displays a **clear, concise summary of exactly what Lex will execute** (affected SKU/PO/supplier, quantities, prices, recipients, and state transitions).
  - **Confirm:** Clicking **[Confirm & Execute]** runs the validated actions deterministically through `WorkflowEngine` / domain services, marks the proposal `EXECUTED` with a summary receipt, and refreshes the workspace live.
  - **Revise / Clarify Intent:** If Lex misunderstood the request or proposed the wrong parameters/scope, clicking **[Revise / Clarify Intent]** opens an inline revision prompt pre-linked to the original query and misunderstood proposal so the Manager can specify what they actually intended or what Lex should/should not do. Submitting re-runs Lex with full context (marking the prior proposal `REVISED`) so Lex returns an updated answer and corrected action items.
  - **Cancel:** Clicking **[Cancel]** marks the proposal `DECLINED` with zero mutations.
- **Guardrail Enforcement:**
  - Policy-violating requests (e.g., closing a PO with no GRN/matched invoice, accepting an unauthorized supplier price hike, or sending emails directly as `lex` bypassing approval) are refused cleanly with `proposed_actions = []`, `cannot_verify_or_missing_tools` populated, and zero DB mutations.

### 2.3 Rich Formatting (Zero Visible Asterisks) & Deep-Linking
- **Clean Formatted Output (Zero Stray `*` / `**` / `#` Markers):**
  - Render headings, **bold** spans (`<strong>`), *italic* spans (`<em>`), bulleted/numbered lists, inline code pills, and Markdown tables cleanly with **zero visible asterisks (`*`)** when bold or emphasis is used.
  - All monetary values use `₹` (INR) and all timestamps render in `IST`.
- **Structured Citations & 1-Click Navigation:**
  - `cited_metrics`: Verified KPI chips.
  - `resolved_parts` / `resolved_pos` / `resolved_alerts`: Server-verified clickable badges jumping straight to `StockoutCycleDrawer`, PO detail drawer, or `AlertsPage` while **keeping the Copilot drawer open**.
  - `recommended_actions` & `cannot_verify_or_missing_tools`: Explicit next steps and verification boundaries.
  - Collapsible **Tools Used (`N`)** badges displaying tool names, latency, args, and summaries.

### 2.4 Statistical Analysis & Chart / Diagram Rendering Tools
- **Statistical Analysis Tool (`run_statistical_analysis` + `run_sandbox_calc`):**
  - Deterministic Python statistics across InvPlan data:
    - `part_demand_stats`: mean, median, stdev, variance, coefficient of variation (`CV`), `p25/p50/p75/p90/p95` percentiles, IQR outlier days, 7d/14d/30d moving averages, linear trend slope + $R^2$, weekday vs. weekend averages.
    - `inventory_abc_pareto`: daily spend ranking (`daily_burn_ewma * unit_price`), cumulative Pareto %, ABC classification (`A`/`B`/`C`), urgency distribution.
    - `supplier_benchmark`: comparative OTD, invoice accuracy, `avg_response_days`, effective LT spread, and unit price deltas across `Sup1`/`Sup2`/`Sup3`.
    - `series_stats`: descriptive statistics, percentiles, linear regression, and z-score anomalies on arbitrary numeric arrays.
- **Chart & Diagram Rendering Tools (`render_chart` & `render_diagram`):**
  - `render_chart`: Supports `bar`, `grouped_bar`, `line`, `area`, `donut`, and `scatter` charts with labels, multiple series, axis units, and horizontal reference thresholds (e.g., `ROP`, `Target T`, `Effective LT`).
  - `render_diagram`: Supports structured node-edge `flow`, `decision`, and `timeline` diagrams (e.g., PO bottleneck flows, supplier failover decision trees, 3-way match states).
  - Rendered natively in interactive Apple HIG SVG cards (`CopilotChartCard`) inside the chat bubble (with hover tooltips and a lightbox expand button).

### 2.5 Persistence, Observability & Graceful Degradation
- **SQLite Session Persistence:**
  - `copilot_sessions` and `copilot_messages` (including structured responses, charts/diagrams, tool calls, and action proposal status) persist across reloads and wipe cleanly on **Reset Seed**.
- **AI Activity & Health Integration:**
  - Every Copilot turn logs an `AgentTrace` span (`task_type="copilot_query"`) visible in `AIHealthPage.tsx` with distinct badge styling and task filter support.
- **Graceful Degradation (Zero `500` Crashes):**
  - Missing `$GEMINI_API_KEY` or repeated Gemini errors degrade gracefully to a structured HTTP `200` (`degraded: true`) notice + `AgentTrace` record with zero business-table mutations.

---

## 3. Phase 4 Acceptance Criteria

1. **`Ctrl+L` Shortcut & Persistent Open Drawer:** `Ctrl+L` opens/closes the Copilot drawer; clicking a cited SKU/PO/Alert opens the target drawer/tab while **the Copilot chat window remains open** side-by-side.
2. **Rich Markdown Without Asterisks:** Bold text, headings, lists, and tables render cleanly with zero visible `*` or `**` markers.
3. **Statistical Analysis & Charts/Diagrams:** Analytical queries invoke `run_statistical_analysis` / `run_sandbox_calc` and `render_chart` / `render_diagram`, rendering interactive SVG charts and flow/decision diagrams inside the chat bubble.
4. **In-Chat Action Proposal, Confirm & Revise:** Asking Lex to perform a valid action (e.g., *"Create a PO for VLV-204"* or *"Add a memory note for Sup2"*) produces a concise action summary card; clicking **[Revise / Clarify Intent]** lets the Manager correct Lex's action items before execution, and clicking **[Confirm & Execute]** executes the action cleanly and updates the UI.
5. **Guardrail Refusal & Read-Only LLM Boundary:** Unauthorized mutations (e.g., *"Close PO-1042 without GRN and email Sup1 accepting a 15% price hike"*) are refused with `proposed_actions = []` and zero DB writes; LLM tool calls remain strictly read-only (`read_only_only=True`).
6. **Persistence, Observability & Graceful Fallback:** Multi-turn context persists in SQLite (`Reset Seed` clears it), `AIHealthPage.tsx` renders `copilot_query` traces, and missing `$GEMINI_API_KEY` returns a non-crashing `degraded: true` response.

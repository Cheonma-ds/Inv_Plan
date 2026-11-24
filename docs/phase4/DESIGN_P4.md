# Phase 4 Technical Design & Architecture Specification (`DESIGN_P4.md`)

**Document Status:** Phase 4 Engineering Design v2.0 (Aligned to [`PRD_P4.md`](PRD_P4.md) & [`AGENTS.md`](../../AGENTS.md))  
**Core Mandate:** Deliver Manager Copilot through decoupled vertical slices preserving all 5 AST checks in [`tests/test_architecture.py`](../../tests/test_architecture.py).

---

## 1. Architectural Boundaries (`tests/test_architecture.py`)

All 5 existing AST invariants remain strictly enforced:
1. `backend/inventory/`, `po/`, `mailroom/`, `planning/`, and `agents/` **never** import from each other.
2. Only `backend/orchestrator/` composes cross-module workflows, binds domain tools (`lex_tools.py`), executes confirmed actions, and exposes FastAPI routes (`routes/copilot.py`).
3. Zero `datetime.now()` or `datetime.utcnow()` calls anywhere in domain, orchestrator, or observability modules — always `core.clock.now(session)`.
4. `backend/po/` never touches `InventoryTransaction` or `Part.on_hand_stock` / `Part.quarantined_stock`.
5. `backend/observability/` remains 100% agent-agnostic and domain-agnostic (`agent_name` keyed).

---

## 2. Read-Only Tool Boundary, Statistical Tools & Chart/Diagram Tools

### 2.1 `ToolRegistry` Filtering (`backend/agents/tools.py`)
- Add `read_only_view() -> ToolRegistry` and `list_definitions(*, read_only_only: bool = False)` on `ToolRegistry`.
- `build_lex_tool_registry(session, ..., read_only_only: bool = False)` (`backend/orchestrator/lex_tools.py`):
  - When `read_only_only=True`, **only** registers `read_only=True` tools and excludes all 3 write tools (`schedule_lex_reminder`, `propose_memory_note`, `create_advisory_alert`).
  - Also ensure `_list_alerts()` skips any `ActionItem.dismiss_resolved_at` session writes when `read_only_only=True` (and `POST /api/copilot/query` never commits business entity changes).

### 2.2 New Read-Only Statistical & Visual Tools (`backend/orchestrator/lex_tools.py`, `read_only=True`)
1. **`run_statistical_analysis(mode, part_sku_or_id?, days?, urgency_filter?, values?, labels?)`:**
   - `mode="part_demand_stats"`: Computes 90d (or N-day) daily demand `mean`, `median`, `stdev`, `variance`, `cv` (coefficient of variation), `min`, `max`, percentiles (`p25`, `p50`, `p75`, `p90`, `p95`), `zero_days_pct`, IQR outlier days, 7d/14d/30d moving averages, OLS linear regression `slope_per_day` & `r_squared`, and weekday-vs-weekend averages.
   - `mode="inventory_abc_pareto"`: Computes daily spend (`daily_burn_ewma * primary_unit_price`), 30d projected spend, cumulative spend %, ABC class (`A` ≤ 80%, `B` ≤ 95%, `C` > 95%), urgency counts, and runway summary stats across all SKUs.
   - `mode="supplier_benchmark"`: Compares `Sup1`/`Sup2`/`Sup3` reliability, OTD, invoice accuracy, `avg_response_days`, `cycles_90d`, and (if `part_sku_or_id` given) primary vs. backup unit price delta, effective LT delta, and MOQ.
   - `mode="series_stats"`: Runs descriptive stats, percentiles, linear trend, and z-score outlier detection (`|z| >= 2.0`) on any supplied numeric `values` list.
2. **`render_chart(chart_type, title, labels, series, subtitle?, x_label?, y_label?, unit?, reference_lines?)`:**
   - Validates `chart_type` in `{"bar", "grouped_bar", "line", "area", "donut", "scatter"}`, finite numeric values, matching `len(values) == len(labels)`, and optional horizontal `reference_lines` (`[{label, value, color?}]`).
   - Returns `{ "rendered": True, "kind": "chart", "spec": ... }`.
3. **`render_diagram(diagram_type, title, nodes, edges, subtitle?)`:**
   - Validates `diagram_type` in `{"flow", "decision", "timeline"}`, `nodes` (`[{id, label, detail?, tone?}]`), and `edges` (`[{source, target, label?}]`, referencing valid node IDs).
   - Returns `{ "rendered": True, "kind": "diagram", "spec": ... }`.

*Capture Mechanism:* `LexAgent.answer_copilot_query()` extracts validated chart/diagram `spec` payloads directly from `render_chart` / `render_diagram` execution results (from `ToolResult` / `tool_calls` data or a per-turn visual collector), so the LLM never wastes tokens duplicating arrays inside final JSON.

---

## 3. Copilot Schemas, Agent Method & In-Chat Action Proposals (`backend/agents/lex/`)

### 3.1 Strict Gemini-Compatible Schemas (`backend/agents/lex/schemas.py`)
Zero `dict[str, Any]` inside `response_schema` (Gemini Developer API rejects `additionalProperties`). All action parameters use typed `CopilotActionParam`:

```python
class CitedMetric(BaseModel):
    name: str
    value: str


class CopilotActionType(StrEnum):
    CREATE_MANUAL_PO = "CREATE_MANUAL_PO"
    APPROVE_DRAFT_EMAIL = "APPROVE_DRAFT_EMAIL"
    REJECT_DRAFT_EMAIL = "REJECT_DRAFT_EMAIL"
    DISPATCH_EDITED_PO = "DISPATCH_EDITED_PO"
    SWITCH_BACKUP_SUPPLIER = "SWITCH_BACKUP_SUPPLIER"
    CANCEL_PO = "CANCEL_PO"
    UPDATE_PO_NOTES_OR_ETA = "UPDATE_PO_NOTES_OR_ETA"
    WITHDRAW_STOCK = "WITHDRAW_STOCK"
    RECORD_GRN = "RECORD_GRN"
    DISMISS_ALERT = "DISMISS_ALERT"
    REOPEN_ALERT = "REOPEN_ALERT"
    APPLY_ROP_RECOMMENDATION = "APPLY_ROP_RECOMMENDATION"
    RECALCULATE_PLANNING = "RECALCULATE_PLANNING"
    ADD_SUPPLIER_MEMORY_NOTE = "ADD_SUPPLIER_MEMORY_NOTE"
    SCHEDULE_LEX_REMINDER = "SCHEDULE_LEX_REMINDER"


class CopilotActionParam(BaseModel):
    key: str
    value: str


class CopilotProposedAction(BaseModel):
    action_type: CopilotActionType
    title: str
    summary: str
    target_ref: str = ""
    params: list[CopilotActionParam] = Field(default_factory=list)


class CopilotTurnContext(BaseModel):
    role: Literal["user", "assistant"]
    content: str
    cited_skus: list[str] = Field(default_factory=list)
    cited_po_numbers: list[str] = Field(default_factory=list)
    proposed_actions_summary: list[str] = Field(default_factory=list)
    action_status: str | None = None
    timestamp: str | None = None


class CopilotQueryInput(BaseModel):
    question: str
    current_date: str
    conversation_history: list[CopilotTurnContext] = Field(default_factory=list)
    revision_context: str | None = None


class CopilotQueryResult(BaseModel):
    answer: str
    cited_skus: list[str] = Field(default_factory=list)
    cited_po_numbers: list[str] = Field(default_factory=list)
    cited_alert_ids: list[int] = Field(default_factory=list)
    cited_metrics: list[CitedMetric] = Field(default_factory=list)
    proposed_actions: list[CopilotProposedAction] = Field(default_factory=list)
    recommended_actions: list[str] = Field(default_factory=list)
    cannot_verify_or_missing_tools: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
```

### 3.2 `LexAgent.answer_copilot_query()` (`backend/agents/lex/agent.py`)
- Wraps execution inside `AgentTracer.span(session, agent_name="lex", task_type="copilot_query", ...)`.
- Passes `conversation_history` (last 8 turns) + optional `revision_context` (original question, prior proposed action summaries, and Manager's correction) so Lex can revise misunderstood actions accurately.
- Returns a `CopilotQueryOutcome` dataclass containing `result: CopilotQueryResult`, `tool_calls: list[dict[str, Any]]`, `visuals: list[dict[str, Any]]` (captured from `render_chart` / `render_diagram`), `trace_id: int | None`, `degraded: bool`, `error_code: str | None`, `error_detail: str | None`.
- **Graceful Degradation:** Catches missing `$GEMINI_API_KEY` (`RuntimeError`) or exhausted retries/LLM errors cleanly, logs `DEGRADED` on `AgentTrace` (`confidence = 0.0`), and returns `degraded=True` with zero crashes and zero DB mutations.

---

## 4. Database Persistence & Migrations (`backend/core/`)

### 4.1 ORM Models (`backend/core/models.py`)
1. **`CopilotSession` (`copilot_sessions`):** `id`, `title`, `created_at`, `updated_at`, `messages` relationship (`cascade="all, delete-orphan"`).
2. **`CopilotMessage` (`copilot_messages`):** `id`, `session_id` (FK CASCADE), `role` (`"user"` | `"assistant"`), `content` (`Text`), `structured_json` (`JSON`, stores citations, metrics, `proposed_actions`, `action_status`, `action_execution_results`, `visuals`, `recommended_actions`, `cannot_verify_or_missing_tools`, `degraded`, `error_code`), `tool_calls_json` (`JSON`), `confidence` (`Float | None`), `trace_id` (`Integer | None`), `created_at`.

### 4.2 Idempotent Migration & Reset Behavior
- `init_db()` (`Base.metadata.create_all`) + `ensure_schema_migrations()` ensure `copilot_sessions` and `copilot_messages` exist on existing SQLite DBs.
- `seed_database(..., reset=True)` automatically clears both tables on **Reset Seed**.

---

## 5. Orchestrator Routes & Action Execution Engine (`backend/orchestrator/`)

### 5.1 Action Executor (`backend/orchestrator/copilot_actions.py`)
- Pure orchestrator module executing manager-confirmed `CopilotProposedAction` items cleanly through `WorkflowEngine` and domain services (`inventory/`, `po/`, `planning/`, `lex_tools.py`), enforcing all InvPlan guardrails (state-machine checks, stock availability, MOQ/unit-price defaults, draft-first email safety).
- Runs inside a single DB transaction—if any action fails validation, raises `ValueError` (HTTP `400`) and rolls back cleanly without partial side effects.

### 5.2 FastAPI Endpoints (`backend/orchestrator/routes/copilot.py`, registered in `routes/__init__.py`)

| Method & Path | Purpose |
|---|---|
| `GET /api/copilot/sessions` | List persisted Copilot sessions ordered by `updated_at DESC`. |
| `POST /api/copilot/sessions` | Create a new Copilot session. |
| `GET /api/copilot/sessions/{session_id}` | Fetch session history with freshly resolved clickable entity links (`resolved_parts`, `resolved_pos`, `resolved_alerts`). |
| `DELETE /api/copilot/sessions/{session_id}` | Delete a session. |
| `POST /api/copilot/query` | Accept `{ question, session_id?, revise_message_id? }`. If `revise_message_id` is set, marks that prior message `action_status = "REVISED"` and injects its original prompt + proposed actions + Manager correction into `revision_context`. Calls `LexAgent.answer_copilot_query()` with `build_lex_tool_registry(db, read_only_only=True)`, resolves citations against SQLite, sets `action_status = "PENDING_CONFIRMATION"` when `proposed_actions` is non-empty, persists message + trace, and returns `CopilotTurnResponse`. |
| `POST /api/copilot/messages/{message_id}/confirm-actions` | Confirms and executes `PENDING_CONFIRMATION` `proposed_actions` via `execute_copilot_actions()`, updates `action_status = "EXECUTED"` + `action_execution_results`, commits, and returns updated message + receipts. |
| `POST /api/copilot/messages/{message_id}/decline-actions` | Marks `PENDING_CONFIRMATION` `proposed_actions` as `"DECLINED"` with zero mutations. |

---

## 6. Frontend Architecture (`frontend/src/`)

### 6.1 `Ctrl+L`, Non-Closing Docked Drawer & Deep-Link Coordination
- **`App.tsx`:**
  - Registers global `Ctrl+L` (`e.ctrlKey && !e.altKey && e.key.toLowerCase() === 'l'`) shortcut to toggle `copilotOpen`.
  - Sets CSS variable `--copilot-offset` (`460px` on `lg+` when `copilotOpen` is `true`, `0px` when closed) so `<main>`, `StockoutCycleDrawer`, and `POTrackerPage` detail drawers shift smoothly left and **never overlap or close `CopilotDrawer`** when deep links are clicked!
- **`ClockBar.tsx`:**
  - Adds **"Ask Lex"** pill button with `Sparkles` icon and `Ctrl+L` shortcut badge.
- **`InventoryPage.tsx` & `AlertsPage.tsx`:**
  - `InventoryPage.tsx` auto-opens `StockoutCycleDrawer` when `focusPartId` is set via navigation.
  - `AlertsPage.tsx` accepts `focusAlertId` to scroll to and highlight the targeted alert card.
- **`CopilotDrawer.tsx`:**
  - Non-blocking right-docked panel (`z-45`, `w-full sm:w-[460px]`, no click-away backdrop).
  - Session selector + `+ New Chat`, empty-state starter prompt chips, multi-turn message list.
  - **Rich Markdown (`CopilotMarkdown.tsx`):** Zero visible `*` or `**` markers; supports headings, bold, italic, lists, inline code pills, and bordered tables.
  - **Visuals (`CopilotChartCard.tsx`):** Native interactive SVG renderer for `bar`, `grouped_bar`, `line`, `area`, `donut`, `scatter`, and `flow`/`decision`/`timeline` diagrams, with hover tooltips and a lightbox expand button.
  - **Proposed Action Confirmation Card:** Clear summary of each action + parameters, **[Confirm & Execute]** button, **[Revise / Clarify Intent]** inline correction input (re-sending to Lex with original context), **[Cancel]**, and `EXECUTED` / `REVISED` / `DECLINED` status pills.
  - **Deep-Link Chips:** Clickable SKU, PO, and Alert badges navigating immediately while keeping `CopilotDrawer` open.
  - **Tool Transparency & Degraded Banner:** Collapsible `Tools Used (N)` inspector and calm amber degradation banner on offline/missing-key fallback.

### 6.2 `AIHealthPage.tsx` Updates
- Add `copilot_query` badge styling in `taskBadgeClass()` and add `copilot_query`, `stock_request_triage`, and `reminder_wakeup` to the trace task filter `<select>`.

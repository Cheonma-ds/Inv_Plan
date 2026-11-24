# Phase 4 Phased Build Slices (`SLICES_P4.md`)

**Execution Philosophy:** Each slice is small, self-contained, independently testable, preserves all 5 AST checks in [`tests/test_architecture.py`](../../tests/test_architecture.py), and finishes with `uv run pytest`, `uv run ruff check backend tests`, and (when frontend changes) `cd frontend && npm run build`.

---

## Dependency Graph

```text
  [P4-1] Read-Only Tool Boundary, Statistical Analysis & Chart/Diagram Tools,
         Copilot Action-Proposal Schemas, LexAgent.answer_copilot_query() & Graceful Fallback
       │
       ▼
  [P4-2] SQLite Copilot Session Persistence, In-Chat Action Executor,
         Deep-Link Resolution, /api/copilot Routes & Eval Adapter Support
       │
       ▼
  [P4-3] Frontend Manager Copilot Drawer (Ctrl+L, Non-Closing Deep-Links,
         Zero-Asterisk Markdown, SVG Charts/Diagrams, Action Confirm/Revise) & AIHealthPage
       │
       ▼
  [P4-4] Full Phase 4 Verification Gate: Offline Suite, Live Gemini Suite & Evals,
         Headless Chromium E2E & Documentation Parity
```

---

## Slice Overview Matrix

| Slice | Goal | New Files | Existing Files Touched | Independent Verification Gate |
|---|---|---|---|---|
| **P4-1** | `read_only_only=True` filter, `run_statistical_analysis` + `render_chart` + `render_diagram` tools, Copilot schemas (`proposed_actions` + zero `additionalProperties`), `LexAgent.answer_copilot_query()`, & graceful degradation | `tests/agents/test_copilot_unit.py` | `backend/agents/tools.py`, `backend/agents/lex/{__init__,schemas,prompts,agent}.py`, `backend/orchestrator/lex_tools.py` | `uv run pytest`, `ruff`; verifies 13 read/stats/chart tools only (3 write tools excluded), `run_statistical_analysis` + `render_chart` + `render_diagram` validation, zero `additionalProperties`, `AgentTracer` `copilot_query` logging, and non-crashing degradation. |
| **P4-2** | Persisted `CopilotSession`/`CopilotMessage` + migrations, `copilot_actions.py` confirm/revise/decline engine, `routes/copilot.py` with entity deep-link resolution, & `eval_adapter.py` / `lex.yaml` | `backend/orchestrator/copilot_actions.py`, `backend/orchestrator/routes/copilot.py`, `tests/orchestrator/test_copilot_routes.py` | `backend/core/{models,migrations}.py`, `backend/orchestrator/routes/__init__.py`, `backend/agents/lex/eval_adapter.py`, `backend/evals/lex.yaml` | `uv run pytest`, `ruff`; verifies `/api/copilot/query`, `/confirm-actions` execution + rollback safety, `/revise` context flow, `/decline-actions`, deep-link resolution, `Reset Seed` clearing, and offline evals. |
| **P4-3** | Frontend `CopilotDrawer.tsx`, `CopilotMarkdown.tsx` (zero visible asterisks), `CopilotChartCard.tsx` (SVG charts & diagrams), `Ctrl+L` shortcut, non-closing deep-links (`App`, `ClockBar`, `InventoryPage`, `POTrackerPage`, `AlertsPage`), & `AIHealthPage` updates | `frontend/src/components/{CopilotDrawer,CopilotMarkdown,CopilotChartCard}.tsx` | `frontend/src/api/{types,client}.ts`, `frontend/src/components/{ClockBar,StockoutCycleDrawer}.tsx`, `frontend/src/App.tsx`, `frontend/src/pages/{InventoryPage,POTrackerPage,AlertsPage,AIHealthPage}.tsx` | `cd frontend && npm run build`, `uv run pytest`, `ruff`; verifies 0 TS/Vite errors and clean layout coordination when Copilot + detail drawers open together. |
| **P4-4** | Full offline, live Gemini, headless Chromium E2E & docs parity verification | *(Verification & docs sync)* | `tests/agents/test_live_lex_copilot_readiness.py`, `docs/phase4/STATUS_P4.md`, `STATUS.md`, `README.md` | `uv run pytest -v`, `RUN_LIVE_GEMINI=1 uv run pytest -v tests/agents/`, live `POST /api/evals/lex/run`, `ruff`, `npm run build`, Playwright E2E, & clean commit. |

---

## Detailed Slice Specifications

### Slice P4-1 — Read-Only Boundary, Statistical & Chart/Diagram Tools, Copilot Schemas & Graceful Degradation
- **Read:** `AGENTS.md`, `docs/phase4/{PRD_P4,DESIGN_P4}.md §2–§3`, `backend/agents/tools.py`, `backend/agents/lex/{__init__,schemas,prompts,agent}.py`, `backend/orchestrator/lex_tools.py`.
- **Leave Untouched:** `frontend/`, `backend/core/models.py`, `backend/orchestrator/routes/`.
- **Tasks:**
  1. Add `read_only_view()` & `list_definitions(read_only_only=...)` on `ToolRegistry` (`backend/agents/tools.py`).
  2. Add `read_only_only: bool = False` to `build_lex_tool_registry()` (`backend/orchestrator/lex_tools.py`), ensure `_list_alerts()` avoids session writes when `read_only_only=True`, and add 3 read-only analytical/visual tools: `run_statistical_analysis`, `render_chart`, and `render_diagram`.
  3. Add `CitedMetric`, `CopilotActionType`, `CopilotActionParam`, `CopilotProposedAction`, `CopilotTurnContext`, `CopilotQueryInput`, `CopilotQueryResult`, and `CopilotQueryOutcome` to `backend/agents/lex/schemas.py` & export via `backend/agents/lex/__init__.py`.
  4. Update `COPILOT_SYSTEM_PROMPT` (`prompts.py`) with guidance on statistical analysis, chart/diagram generation, `proposed_actions` confirmation summaries, and guardrail refusals; implement `LexAgent.answer_copilot_query()` with `AgentTracer.span(..., task_type="copilot_query")`, visual artifact capture, and graceful degradation.
  5. Add `tests/agents/test_copilot_unit.py`.

---

### Slice P4-2 — Persistence, Action Executor, Deep-Link Resolution & `/api/copilot` Routes
- **Read:** `AGENTS.md`, `docs/phase4/{PRD_P4,DESIGN_P4}.md §4–§5`, `backend/core/{models,migrations}.py`, `backend/orchestrator/{workflows.py,routes/__init__.py,routes/observability.py}`, `backend/agents/lex/eval_adapter.py`, `backend/evals/lex.yaml`.
- **Leave Untouched:** `frontend/`, `backend/inventory/`, `backend/po/`, `backend/planning/`.
- **Tasks:**
  1. Add `CopilotSession` and `CopilotMessage` ORM models in `backend/core/models.py` and idempotent SQLite checks in `backend/core/migrations.py`.
  2. Create `backend/orchestrator/copilot_actions.py` dispatching confirmed `CopilotActionType` actions cleanly through `WorkflowEngine` / domain services with atomic rollback on guardrail failure.
  3. Create `backend/orchestrator/routes/copilot.py` implementing `/api/copilot/sessions`, `/api/copilot/sessions/{id}`, `/api/copilot/query` (including `revise_message_id` handling), `/api/copilot/messages/{id}/confirm-actions`, and `/api/copilot/messages/{id}/decline-actions` with entity deep-link resolution; register in `backend/orchestrator/routes/__init__.py`.
  4. Add `copilot_query` support to `LexEvalExecutor` (`eval_adapter.py`) and golden cases to `backend/evals/lex.yaml`.
  5. Add `tests/orchestrator/test_copilot_routes.py`.

---

### Slice P4-3 — Frontend Manager Copilot Drawer, Zero-Asterisk Markdown, Charts/Diagrams & `Ctrl+L`
- **Read:** `AGENTS.md`, `docs/phase4/{PRD_P4,DESIGN_P4}.md §6`, `frontend/src/{App.tsx,api/types.ts,api/client.ts,components/ClockBar.tsx,components/StockoutCycleDrawer.tsx}`, `frontend/src/pages/{InventoryPage,POTrackerPage,AlertsPage,AIHealthPage}.tsx`.
- **Leave Untouched:** `backend/`.
- **Tasks:**
  1. Add Copilot types & API client methods (`listCopilotSessions`, `createCopilotSession`, `getCopilotSession`, `deleteCopilotSession`, `queryCopilot`, `confirmCopilotActions`, `declineCopilotActions`) to `frontend/src/api/{types,client}.ts`.
  2. Build `frontend/src/components/CopilotMarkdown.tsx` (headings, `<strong>`, `<em>`, lists, code pills, tables — zero visible asterisks) and `frontend/src/components/CopilotChartCard.tsx` (native SVG `bar`, `grouped_bar`, `line`, `area`, `donut`, `scatter`, and `flow`/`decision`/`timeline` diagrams + hover tooltips + expand lightbox).
  3. Build `frontend/src/components/CopilotDrawer.tsx` (docked non-closing panel, session switcher + `+ New Chat`, starter prompts, rich Markdown + charts, **Proposed Actions** card with **[Confirm & Execute]**, **[Revise / Clarify Intent]**, and **[Cancel]**, clickable deep-link chips, `Tools Used` inspector, and degraded banner).
  4. Wire `Ctrl+L` global shortcut & `--copilot-offset` layout coordination in `App.tsx`, `ClockBar.tsx`, `StockoutCycleDrawer.tsx`, `POTrackerPage.tsx`, `InventoryPage.tsx` (auto-open `StockoutCycleDrawer` on `focusPartId`), and `AlertsPage.tsx` (`focusAlertId` scroll/highlight) so deep links keep Copilot open side-by-side.
  5. Update `AIHealthPage.tsx` `taskBadgeClass` & `taskFilter` dropdown for `copilot_query`.

---

### Slice P4-4 — Full Phase 4 Verification Gate & Documentation Parity
- **Read:** All modified Phase 4 files + `tests/agents/test_live_lex_copilot_readiness.py`, `docs/phase4/STATUS_P4.md`, `STATUS.md`, `README.md`.
- **Tasks:**
  1. Run offline gate: `uv run pytest -v`, `uv run ruff check backend tests`, `cd frontend && npm run build`.
  2. Run live Gemini gate: `RUN_LIVE_GEMINI=1 uv run pytest -v tests/agents/` (including new live statistical/chart & action-proposal/refusal tests) and `POST /api/evals/lex/run` (`mode="live"`).
  3. Run headless Chromium Playwright E2E verifying `Ctrl+L` toggle, multi-turn statistical query + SVG chart rendering, zero visible asterisks, clickable SKU/PO navigation **keeping Copilot open**, in-chat `[Revise / Clarify Intent]` + `[Confirm & Execute]` action flow, `AIHealthPage` `copilot_query` traces, and zero console/page errors.
  4. Update `docs/phase4/STATUS_P4.md`, `STATUS.md`, and `README.md` with exact verified counts/outputs and commit cleanly (leaving `demo_recordings/` untracked).

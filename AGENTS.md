# AGENTS.md — Operating Manual for Coding Agents

This repository builds **InvPlan**, a Demand-to-Delivery & Inventory Planning prototype. Read this file and `STATUS.md` at the start of every session.

## 1. Context Discipline (Read On-Demand Only)
- **Always read first:** `AGENTS.md` (this file) and `STATUS.md`.
- **Read selectively:** Only open the specific `DESIGN.md` sections (`§1`–`§10`) listed for your active slice in `STATUS.md`, and only open `PRD.md` if a product edge case is ambiguous.
- **Work one vertical slice per session.** Do not refactor unrelated modules or drift into future slices.

## 2. Hard Architecture & Decoupling Rules
1. **No sideways imports between domain modules:** `backend/inventory/`, `backend/po/`, `backend/mailroom/`, `backend/planning/`, and `backend/agents/` must **never** import from each other. They may only import from `backend/core/` and `backend/observability/`.
2. **Only `backend/orchestrator/` composes cross-module workflows** (e.g., Mailroom → Lex → PO state machine → Alerts) and exposes FastAPI routes.
3. **Agents never mutate business tables directly:** `backend/agents/lex/` takes inputs and returns strict Pydantic models (`EmailTriageResult`, `InvoiceExtractionResult`, `EmailDraftResult`). `orchestrator/` enforces guardrails and invokes `po/` or `inventory/` services.
4. **Never use system wall-clock time (`datetime.now()` / `utcnow()`) in domain logic:** Always call `core.clock.now()` (the persisted `SimulatedClock`).
5. **Preserve `EmailTransport` abstraction:** Keep all mailroom operations behind `backend/mailroom/`'s `EmailTransport` protocol so real IMAP/Gmail can be swapped in later.
6. **Keep `observability/` and `agents/base.py` agent-agnostic:** Never hardcode Lex-specific columns in trace/eval/autonomy tables; key everything by `agent_name`.

## 3. Non-Negotiable Product Guardrails (MVP)
- **Replenishment Trigger:** Always evaluate `inventory_position = on_hand_stock + on_order_stock < rop` (order `S - inventory_position`, rounded up to MOQ). A newly triggered PO starts in `PENDING_DISPATCH_APPROVAL` and counts toward `on_order_stock` immediately to prevent duplicate POs.
- **Draft-First Outbound Rule:** **ALL** Lex outbound emails in MVP start as `DRAFT_PENDING_APPROVAL` and require `InventoryManager` approval before dispatch. Track `APPROVED_AS_IS` vs `APPROVED_EDITED` vs `REJECTED` in `autonomy_policies`.
- **5-Persona Mailroom:** `inventory_manager`, `sup1`, `sup2`, `sup3`, `lex`. Manual send *from* `lex` is forbidden (`403`); `lex` inbox is read-only.
- **Deterministic Math:** 3-way invoice matching (`po/matching.py`) and all forecasting/scoring/ROP formulas (`planning/`) are 100% deterministic Python—zero LLM calls.
- **Quarantine Lock:** Stock with a mismatched invoice stays in `quarantined_stock` (excluded from `on_hand_stock`) until a corrected PDF invoice passes 3-way match.
- **LLM Model & Auth:** Default model is `gemini-3.5-flash-lite` via official `google-genai` SDK reading `$GEMINI_API_KEY`. Invoices are PDF attachments via email only. Confidence `< 0.85` forces `AMBIGUOUS` escalation with zero PO state mutation.
- **No Premature Demo Presets:** Do not hardcode preset email scripts in the UI; keep the Mailroom cleanly usable with manual compose/PDF upload.

## 4. Environment & Commands
- **Python (managed by `uv`, Python 3.12):**
  - Run tests: `uv run pytest` (or targeted `uv run pytest tests/<module>`)
  - Lint: `uv run ruff check backend tests`
  - Run backend (`:8000`): `uv run uvicorn backend.main:app --reload --port 8000`
  - Optional Phoenix (`:6006`, requires `uv sync --extra phoenix`): `ENABLE_PHOENIX=true` + `uv run phoenix serve`
- **Frontend (`frontend/`, Node + Vite + Tailwind):**
  - Dev server (`:5173`, proxies `/api` → `:8000`): `cd frontend && npm run dev`
  - Typecheck & build: `cd frontend && npm run build`
- **Local runtime data:** SQLite DB lives at `data/inv_plan.db`, PDFs at `data/attachments/` (gitignored).

## 5. End-of-Session Handoff Checklist
Before finishing a slice:
1. Run `uv run pytest`, `uv run ruff check backend tests`, and (if frontend changed) `cd frontend && npm run build`.
2. **Review slice logic against the plan (`DESIGN.md` / `PRD.md`):** If the implementation deviated from the original plan, analyze why. If the original logic proved invalid or flawed during implementation, document the discrepancy and its proof (e.g., failing edge case, architecture constraint, or test result) in the relevant design/status docs (`DESIGN.md`, `STATUS.md`).
3. Update `STATUS.md` with completed work, exact files added, test results, any caveats/decisions, and the exact next slice to start.
4. **Write a focused next-session handoff prompt:** Prepare a self-contained prompt for the next coding agent covering only the next independent slice/step—specifying which doc sections and files to read, which files/folders to leave untouched, and the acceptance criteria—so the next session stays scoped without bloating context or needing mid-session summarization.
5. Commit your slice cleanly to `git` with a concise message (e.g., `feat(slice-1): deterministic inventory, PO core, and seed data`).

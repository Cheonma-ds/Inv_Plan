# STATUS.md — Live Session Baton

**Current Stage:** Slice 1 Complete → Ready for **Slice 2**  
**Last Updated:** 2026-11-20

## Completed
- **PRD (`PRD.md` v1.0) & Design (`DESIGN.md` v1.0):** Approved baselines.
- **Pre-Slice 0 (`e794193`):** Repo bootstrap (`git`, `uv`, FastAPI + React/Vite/Tailwind skeleton, `AGENTS.md`, `STATUS.md`).
- **Slice 1 — Deterministic Core + Seed Data:**
  - `backend/core/`: `config.py`, `enums.py`, `db.py` (SQLite FK pragma), `models.py` (all 14 tables from `DESIGN.md §3`), `clock.py` (persisted `SimulatedClock` + `ensure_utc()`).
  - `backend/inventory/`: `schemas.py`, `service.py` (`on_order_stock`, `inventory_position`, `consume_stock()` with `PENDING_DISPATCH_APPROVAL` duplicate-PO safety, MOQ rounding, `AcceleratedBurnAlert`, quarantine helpers).
  - `backend/po/`: `state_machine.py` (explicit transition graph + immutable `promised_eta`), `matching.py` (deterministic 3-way match with ₹0.50 price / ₹1.00 line-total tolerance), `service.py` (GRN, quarantine lock without double-counting, release, invoice-before-GRN support via `PENDING_GRN`, backup supplier switch), `actions.py` (`ActionItem` helpers).
  - `backend/seed/`: `data.py` & `reset.py` (deterministic PRNG: 3 suppliers `sup1`/`sup2`/`sup3` with memory notes, 15 parts across `CRITICAL`/`STANDARD`/`LOW` each with primary + backup `PartSupplierTerm`, 12-month consumption ledger including 2 intermittent parts, 36 closed POs + 2 active POs, 8 Lex `AutonomyPolicy` rows locked to `APPROVAL_REQUIRED`).
  - **Tests & Verification:** `19 passed` (`tests/inventory/`, `tests/po/`, `tests/seed/`, plus AST architecture guardrails in `tests/test_architecture.py` enforcing zero sideways imports and zero wall-clock `datetime.now()`/`utcnow()` in domain modules), `uv run ruff check backend tests` passed, `uv run python -m backend.seed.reset` verified.

## Next Active Slice: **Slice 2 — 5-Persona Mailroom + SLA Watcher + Lex Email Agent**
- **Read ONLY:** `AGENTS.md`, `STATUS.md`, `DESIGN.md` sections **`§4.4`**, **`§5`**, **`§6`**, **`§8`**, **`§9`**, and **`§10`**.
- **Modules to Build:**
  - `backend/mailroom/` — `EmailTransport` protocol (`transport.py`), DB-backed 5-persona mailroom service (`service.py`), PDF attachment storage under `data/attachments/`, API enforcement blocking manual send-as-`lex` (`403`).
  - `backend/observability/` — agent-agnostic `AgentTracer` (`tracer.py`, SQLite `agent_traces` + optional OTel span hook) and `autonomy.py` (`APPROVED_AS_IS` / `APPROVED_EDITED` / `REJECTED` policy counters).
  - `backend/agents/` — `base.py` (`BaseAgent`), `llm_client.py` (`google-genai` wrapper for `gemini-3.5-flash-lite` with structured Pydantic output + token/INR cost metering), `lex/` (`schemas.py`, `prompts.py`, `agent.py`: `EmailTriageResult` with `< 0.85` confidence `AMBIGUOUS` guardrail, `EmailDraftResult`, Supplier Memory note candidate extraction).
  - `backend/orchestrator/` — inbound email routing (Mailroom → Lex triage → PO state machine → Alerts/Drafts), synchronous SLA & Milestone Watcher (`sla_watcher.py` triggered on clock advance), Draft approval/rejection workflow, inline Alert CTA execution, and FastAPI REST routes mounted in `backend/main.py` (`clock`, `parts`, `pos`, `alerts`, `mailroom`, `suppliers`, `admin/reset-seed`).
- **Done Criteria:**
  - `uv run pytest` passes mocked integration tests covering Scenarios 1A, 1B, 2, 3, Draft-First approval (`APPROVED_AS_IS` vs `APPROVED_EDITED` vs `REJECTED`), `403` on manual send-as-Lex, and SLA clock advances.
  - At least 1 live smoke test against `gemini-3.5-flash-lite` (skippable if `GEMINI_API_KEY` absent) verifies structured `EmailTriageResult` and `EmailDraftResult`.
  - `tests/test_architecture.py` and `uv run ruff check backend tests` continue to pass.

## Upcoming Slices
- **Slice 3:** PDF Invoice Pipeline + Planning Intelligence Engine (`DESIGN.md §4.3, §6.2, §7`)
- **Slice 4:** React Operations Dashboard & 5-Persona Mailroom UI (`DESIGN.md §9`)
- **Slice 5:** AI Activity & Health UI, YAML Eval Runner (`evals/lex.yaml`) & Optional Phoenix (`DESIGN.md §8, §9`)

## Caveats / Design Decisions Made
- Added `InvoiceMatchStatus.PENDING_GRN` so if a supplier emails an invoice before warehouse GRN is logged, `record_goods_receipt()` automatically reconciles the pending invoice on arrival.
- Added `clock.ensure_utc()` because SQLite returns naive datetimes even on `DateTime(timezone=True)` columns; always normalize DB timestamps through `clock.ensure_utc()` before arithmetic with `clock.now(session)`.
- Demo presets remain intentionally deferred until after Slice 5.

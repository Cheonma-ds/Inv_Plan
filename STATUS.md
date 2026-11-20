# STATUS.md — Live Session Baton

**Current Stage:** Pre-Slice 0 Complete → Ready for **Slice 1**  
**Last Updated:** 2026-11-20

## Completed
- **PRD (`PRD.md` v1.0):** Approved baseline covering all 6 manager pain points, 5-persona Mailroom, Lex Draft-First rule, Supplier Memory, PDF-only invoices, and Lightweight Hybrid Observability & Evals.
- **Design (`DESIGN.md` v1.0):** Approved modular architecture (`§1`–`§10`).
- **Pre-Slice 0 (Repo Bootstrap):**
  - Initialized `git`, `.gitignore`, `pyproject.toml` (`uv`), empty decoupled `backend/` + `tests/` skeleton, and `frontend/` (React + Vite + Tailwind v4 + TypeScript).
  - Created `AGENTS.md` and `STATUS.md`.
  - Verified `uv run pytest` and `cd frontend && npm run build`.

## Next Active Slice: **Slice 1 — Deterministic Core + Seed Data**
- **Read ONLY:** `AGENTS.md`, `STATUS.md`, `DESIGN.md` sections **`§2`**, **`§3`**, **`§4`**, and **`§10`**.
- **Modules to Build:**
  - `backend/core/` — config (`data/inv_plan.db`), SQLAlchemy 2.0 engine/session, all 14 tables from `§3`, enums, and persisted `SimulatedClock` (`core/clock.py`).
  - `backend/inventory/` — parts CRUD/queries, `InventoryTransaction` ledger, `on_order_stock` & `inventory_position` calculation, `consume_stock()` trigger (`ReorderTriggered`, `AcceleratedBurnAlert`), quarantine stock helpers.
  - `backend/po/` — explicit PO state machine (`state_machine.py`), GRN & quarantine handling, deterministic 3-way match math (`matching.py`), `ActionItem` helpers.
  - `backend/seed/` — realistic deterministic seeder (`15` parts across `CRITICAL`/`STANDARD`/`LOW`, `Sup1`/`Sup2`/`Sup3` with primary + backup `PartSupplierTerm` rows, 12 months of consumption history & historical POs reflecting `Sup1` reliable, `Sup2` chronically late & invoice-prone, `Sup3` unresponsive/volatile) + reset helper (`backend.seed.reset`).
- **Done Criteria:**
  - `uv run pytest` passes full unit test suites in `tests/inventory/`, `tests/po/`, `tests/seed/` covering: Inventory Position trigger & no duplicate PO when second withdrawal occurs in `PENDING_DISPATCH_APPROVAL`, every valid/invalid PO transition, 3-way match (clean, price mismatch, qty mismatch, tolerance ₹0.50), quarantine lock & release, and clock persistence.
  - Zero sideways imports between `inventory/` and `po/`.

## Upcoming Slices
- **Slice 2:** Built-In 5-Persona Mailroom + SLA Watcher + Lex Email Agent (`DESIGN.md §4.4, §5, §6, §8, §9`)
- **Slice 3:** PDF Invoice Pipeline + Planning Intelligence Engine (`DESIGN.md §4.3, §6.2, §7`)
- **Slice 4:** React Operations Dashboard & 5-Persona Mailroom UI (`DESIGN.md §9`)
- **Slice 5:** AI Activity & Health UI, YAML Eval Runner (`evals/lex.yaml`) & Optional Phoenix (`DESIGN.md §8, §9`)

## Open Decisions / Notes
- Demo presets are intentionally deferred until after Slice 5 is built and tested live together.
- `arize-phoenix` is an optional extra (`uv sync --extra phoenix`) to keep baseline installs lightweight.

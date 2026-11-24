# STATUS_P3.md — Phase 3 Live Session Baton

**Current Stage:** **Slice P3-1 Complete** — Ready to Start **Slice P3-2**  
**Last Updated:** 2026-11-24  
**Baseline Reference:** `HEAD` (`099474f`), [`STATUS.md`](../../STATUS.md), [`STATUS_P2.md`](../phase2/STATUS_P2.md)  
**Planning Docs:** [`PRD_P3.md`](PRD_P3.md) · [`DESIGN_P3.md`](DESIGN_P3.md) · [`SLICES_P3.md`](SLICES_P3.md)

---

## Slice Progress Tracker

- [x] **Slice P3-1:** Core Clock (`Oct 1 13:00 IST`), Schema Migrations, `on_order` Draft Exclusion, Partial GRN Match & Overdue Fixes
- [ ] **Slice P3-2:** 90-Day Causal Seed Simulator (`Jul–Sep`, 0 Open POs), Daily Burn EWMA ($\alpha=0.3$), Clock Rollover & Cycle Analytics
- [ ] **Slice P3-3:** Alerting Overhaul (`Edit PO`, Non-Resolving `Reject`, `DISMISSED` Hiding + Inventory Warning) & `ROP/T` Sweep
- [ ] **Slice P3-4:** Lex Resilience (Retries/Fallbacks), Multimodal Image Triage & Enforced Email Formatting
- [ ] **Slice P3-5:** Lex v2 Tool-Use Loop, AST-Sandboxed Calc Executor, Dynamic Reminders (`+4d` Deferral) & Memory Alerts
- [ ] **Slice P3-6:** Frontend Core (IST Clock, Sidebar Cleanup, `FormattedEmailBody`, `InventoryPage` Revamp & `StockoutCycleDrawer`)
- [ ] **Slice P3-7:** Frontend `CreateEditPOModal` (Suggested Qty + Reasoning), `AlertsPage`, `SuppliersPlanningPage` & `AIHealthPage` Traces
- [ ] **Slice P3-8:** Full Phase 3 E2E Verification, Live Gemini Eval Sweep & Documentation Parity Gate

---

## Completed Slice Summary — `P3-1`

- **Files Added:**
  - `tests/po/test_phase3_core_fixes.py` (5 tests covering IST clock/date alignment, idempotent Phase 3 SQLite migrations & `lex_reminders`, `on_order_stock` draft exclusion + per-PO GRN subtraction, partial-delivery 3-way match & cumulative closure, and `UNRESPONSIVE_ESCALATED`/`SENT_AWAITING_ACK` → `OVERDUE` transitions).
- **Files Modified:**
  - `backend/core/config.py`, `backend/core/clock.py`, `backend/seed/data.py` (`SEED_START_TIME` & `default_sim_start` set to `2026-10-01T07:30:00+00:00`, added `IST` & `to_ist_date()`).
  - `backend/core/enums.py`, `backend/core/models.py`, `backend/core/migrations.py` (`LexReminderStatus`, `LexReminder` table, additive Phase 3 columns on `parts`, `suppliers`, `inventory_transactions`, `action_items`, and idempotent SQLite migration rules).
  - `backend/inventory/schemas.py`, `backend/inventory/service.py` (`get_on_order_stock()` excludes `PENDING_DISPATCH_APPROVAL` and subtracts per-PO GRNs; `PartSnapshot` exposes `active_po_id`, `active_po_number`, `active_po_status`, `pending_dispatch_qty`; duplicate-PO guard preserved).
  - `backend/po/matching.py`, `backend/po/service.py`, `backend/po/state_machine.py` (`qty_ok = (invoice_qty == grn_received_qty)`, cumulative partial GRN tracking `RECEIVED_PENDING_INVOICE -> ACKNOWLEDGED` / `CLOSED`, and `UNRESPONSIVE_ESCALATED`/`SENT_AWAITING_ACK` → `OVERDUE`).
  - `backend/orchestrator/logistics_workflow.py`, `backend/orchestrator/sla_watcher.py` (date-only `parse_iso_date_utc()` aligned to `07:30:00Z`; `_handle_overdue_po` helper handling `SENT_AWAITING_ACK`, `UNRESPONSIVE_ESCALATED`, `ACKNOWLEDGED`, `DELAYED_CRITICAL`).
  - `tests/seed/test_seed_and_clock.py`, `tests/inventory/test_inventory.py`, `tests/orchestrator/test_prd_fixes.py`, `tests/orchestrator/test_workflows_and_sla.py` (updated for Phase 3 clock & `on_order_stock` contracts).
- **Verification:** `uv run pytest -v` (`99 passed, 3 skipped` — 3 live-Gemini tests skipped offline) and `uv run ruff check backend tests` (`All checks passed!`), including all 5 AST checks in `tests/test_architecture.py`.

---

## Next Session Handoff Prompt — Start Slice `P3-2`

> Implement **Slice P3-2** per `docs/phase3/SLICES_P3.md` and `docs/phase3/DESIGN_P3.md`:
> - **Read:** `AGENTS.md`, `docs/phase3/STATUS_P3.md`, `docs/phase3/PRD_P3.md §2 (Pillars 1 & 2)`, `docs/phase3/DESIGN_P3.md §3.2, §3.3, §4`, and target files: `backend/planning/{rop,scoring,schemas,service,__init__}.py`, `backend/seed/data.py`, `backend/orchestrator/{workflows,routes/parts,routes/suppliers}.py`.
> - **Leave Untouched:** `frontend/`, `backend/agents/`, `backend/mailroom/`.
> - **Acceptance Criteria:**
>   1. Create `backend/planning/ewma_daily.py` implementing 90-day IST daily consumption buckets, daily EWMA ($\alpha = 0.3$), 7-day neutral trend `%`, $\sigma_D$, intermittent floor, supplier `comm_buffer_days`, `Stockout In` urgency tier, and supplier-specific PO quantity suggestion + reasoning.
>   2. Create `backend/planning/cycles.py` reconstructing 90-day daily `on_hand` trajectory + `Stockout → PO → Replenishment → Stockout` markers from DB rows + future stockout projection.
>   3. Update `planning/scoring.py`, `planning/service.py`, and `planning/rop.py` to compute `avg_response_days` and `cycles_90d` from persisted DB rows and consume 90d daily EWMA + $\sigma_D$.
>   4. Wire `settle_daily_burn_on_clock_advance()` into `WorkflowEngine` clock advance/set/jump methods (preserving zero-extra-event `ROP/T` freeze and updating EWMA/$\sigma_D$/`ROP/T` on days with `Withdraw`/Making consumption), plus `ACCELERATED_BURN` (`on_hand > 0` & `predicted_stockout_date < earliest_po_eta`) and `STOCKOUT_ACTIVE` (`on_hand == 0`).
>   5. Rewrite `backend/seed/data.py` with 15 varied 90-day Jul–Sep daily patterns + causal PO/GRN/Invoice/Comm generation ending with **0 active POs** on `Oct 1 13:00 IST`, computing initial `ROP/T` & supplier scores via `planning/`.
>   6. Add `GET /api/parts/{id}/cycle-analytics` & `GET /api/parts/{id}/po-suggestion` and enrich `GET /api/parts` & `GET /api/suppliers`.
>   7. Add `tests/planning/test_daily_ewma_and_cycles.py`, update `tests/seed/test_seed_and_clock.py` (0 open POs), run `uv run pytest` and `uv run ruff check backend tests`, update `docs/phase3/STATUS_P3.md`, and commit cleanly.

---

## Plan vs. Implementation Discrepancies
*(Per `AGENTS.md §5`, document any discrepancy between `PRD_P3.md` / `DESIGN_P3.md` and actual implementation with proof and resolution here.)*

| Slice | Original Plan Reference | Discrepancy / Proof | Resolution Implemented |
|---|---|---|---|
| Baseline Audit | `PRD_P2.md §4.2` vs `HEAD (099474f)` | Post-Phase-2 commit `25a8bdc` re-introduced `Withdraw`, `Create PO`, `ROP/S`, and `Demand` actions in `InventoryPage.tsx`, and pre-existing `get_on_order_stock()` still counted `PENDING_DISPATCH_APPROVAL` while `matching.py` still enforced `invoice_qty == po_qty`. | Reconciled in `PRD_P3.md §1.1` and scheduled explicitly in `P3-1`–`P3-7`. |

# STATUS_P3.md — Phase 3 Live Session Baton

**Current Stage:** Phase 3 Planning Complete (`PRD_P3.md`, `DESIGN_P3.md`, `SLICES_P3.md` Approved for Execution — Ready to Start **Slice P3-1**)  
**Last Updated:** 2026-11-24  
**Baseline Reference:** `HEAD` (`099474f`), [`STATUS.md`](../../STATUS.md), [`STATUS_P2.md`](../phase2/STATUS_P2.md)  
**Planning Docs:** [`PRD_P3.md`](PRD_P3.md) · [`DESIGN_P3.md`](DESIGN_P3.md) · [`SLICES_P3.md`](SLICES_P3.md)

---

## Slice Progress Tracker

- [ ] **Slice P3-1:** Core Clock (`Oct 1 13:00 IST`), Schema Migrations, `on_order` Draft Exclusion, Partial GRN Match & Overdue Fixes
- [ ] **Slice P3-2:** 90-Day Causal Seed Simulator (`Jul–Sep`, 0 Open POs), Daily Burn EWMA ($\alpha=0.3$), Clock Rollover & Cycle Analytics
- [ ] **Slice P3-3:** Alerting Overhaul (`Edit PO`, Non-Resolving `Reject`, `DISMISSED` Hiding + Inventory Warning) & `ROP/T` Sweep
- [ ] **Slice P3-4:** Lex Resilience (Retries/Fallbacks), Multimodal Image Triage & Enforced Email Formatting
- [ ] **Slice P3-5:** Lex v2 Tool-Use Loop, AST-Sandboxed Calc Executor, Dynamic Reminders (`+4d` Deferral) & Memory Alerts
- [ ] **Slice P3-6:** Frontend Core (IST Clock, Sidebar Cleanup, `FormattedEmailBody`, `InventoryPage` Revamp & `StockoutCycleDrawer`)
- [ ] **Slice P3-7:** Frontend `CreateEditPOModal` (Suggested Qty + Reasoning), `AlertsPage`, `SuppliersPlanningPage` & `AIHealthPage` Traces
- [ ] **Slice P3-8:** Full Phase 3 E2E Verification, Live Gemini Eval Sweep & Documentation Parity Gate

---

## Next Session Handoff Prompt — Start Slice `P3-1`

> Implement **Slice P3-1** per `docs/phase3/SLICES_P3.md` and `docs/phase3/DESIGN_P3.md`:
> - **Read:** `AGENTS.md`, `docs/phase3/STATUS_P3.md`, `docs/phase3/PRD_P3.md §1–§2 (Pillar 1 & 4)`, `docs/phase3/DESIGN_P3.md §2 & §3.1, §3.4`, and the target files: `backend/core/{config,enums,models,migrations,clock}.py`, `backend/inventory/{schemas,service}.py`, `backend/po/{matching,service,state_machine}.py`, `backend/orchestrator/{logistics_workflow,sla_watcher}.py`.
> - **Leave Untouched:** `frontend/`, `backend/agents/`, `backend/planning/`, and `backend/seed/data.py` (except updating `SEED_START_TIME` to `2026-10-01T07:30:00+00:00`).
> - **Acceptance Criteria:**
>   1. Sim clock defaults to `2026-10-01T07:30:00+00:00` (`Oct 1 13:00 IST`), `to_ist_date()` exists in `core/clock.py`, and date-only ETA parsing aligns to `07:30:00Z`.
>   2. Additive columns on `parts`, `suppliers`, `inventory_transactions`, `action_items` and new `lex_reminders` table are wired in `models.py` and `migrations.py`.
>   3. `get_on_order_stock()` excludes `PENDING_DISPATCH_APPROVAL` and subtracts per-PO GRNs (`RELEASED + HELD_QUARANTINE`), while `PartSnapshot` exposes `active_po_id`, `active_po_number`, `active_po_status`, and `pending_dispatch_qty` to prevent duplicate POs.
>   4. `matching.py` checks `invoice_qty == grn_received_qty` with cumulative partial GRN tracking in `po/service.py`; `state_machine.py` & `sla_watcher.py` permit `UNRESPONSIVE_ESCALATED -> OVERDUE` and `SENT_AWAITING_ACK -> OVERDUE`.
>   5. Update impacted baseline tests (`tests/seed/`, `tests/inventory/`, `tests/orchestrator/test_workflows_and_sla.py`, `tests/orchestrator/test_prd_fixes.py`), add `tests/po/test_phase3_core_fixes.py`, run `uv run pytest` and `uv run ruff check backend tests` cleanly, update `STATUS_P3.md`, and commit cleanly.

---

## Plan vs. Implementation Discrepancies
*(Per `AGENTS.md §5`, document any discrepancy between `PRD_P3.md` / `DESIGN_P3.md` and actual implementation with proof and resolution here.)*

| Slice | Original Plan Reference | Discrepancy / Proof | Resolution Implemented |
|---|---|---|---|
| Baseline Audit | `PRD_P2.md §4.2` vs `HEAD (099474f)` | Post-Phase-2 commit `25a8bdc` re-introduced `Withdraw`, `Create PO`, `ROP/S`, and `Demand` actions in `InventoryPage.tsx`, and pre-existing `get_on_order_stock()` still counted `PENDING_DISPATCH_APPROVAL` while `matching.py` still enforced `invoice_qty == po_qty`. | Reconciled in `PRD_P3.md §1.1` and scheduled explicitly in `P3-1`–`P3-7`. |

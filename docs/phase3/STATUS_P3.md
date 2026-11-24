# STATUS_P3.md — Phase 3 Live Session Baton

**Current Stage:** **Slice P3-2 Complete** — Ready to Start **Slice P3-3**  
**Last Updated:** 2026-11-24  
**Baseline Reference:** `HEAD` (`099474f`), [`STATUS.md`](../../STATUS.md), [`STATUS_P2.md`](../phase2/STATUS_P2.md)  
**Planning Docs:** [`PRD_P3.md`](PRD_P3.md) · [`DESIGN_P3.md`](DESIGN_P3.md) · [`SLICES_P3.md`](SLICES_P3.md)

---

## Slice Progress Tracker

- [x] **Slice P3-1:** Core Clock (`Oct 1 13:00 IST`), Schema Migrations, `on_order` Draft Exclusion, Partial GRN Match & Overdue Fixes
- [x] **Slice P3-2:** 90-Day Causal Seed Simulator (`Jul–Sep`, 0 Open POs), Daily Burn EWMA ($\alpha=0.3$), Clock Rollover & Cycle Analytics
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

## Completed Slice Summary — `P3-2`

- **Files Added:**
  - `backend/planning/ewma_daily.py` (90-day IST completed-day consumption bucketing, daily EWMA $\alpha = 0.3$, 7-day neutral trend `%`, $\sigma_D$, intermittent safety floor, supplier `comm_buffer_days`, `Stockout In` urgency tier `CRITICAL`/`WARNING`/`HEALTHY`, and supplier-specific PO quantity suggestion + plain-English `reasoning`).
  - `backend/planning/cycles.py` (90-day backward daily closing `on_hand` reconstruction ending identically at live `part.on_hand_stock`, DB lifecycle markers `STOCKOUT`/`LOW_STOCK`/`PO_PLACED`/`REPLENISHMENT` with `actual_lt_days`, and linear depletion + open-PO arrival projection).
  - `tests/planning/test_daily_ewma_and_cycles.py` (5 tests covering same-day demand freeze, pure predicted burn tick invariance, unpredicted demand EWMA/$\sigma_D$/`ROP/T` updates, pre-zero `ACCELERATED_BURN` + `STOCKOUT_ACTIVE` deduplication, cycle analytics endpoint parity, and supplier-specific PO suggestions).
- **Files Modified:**
  - `backend/planning/__init__.py`, `backend/planning/schemas.py`, `backend/planning/forecast.py`, `backend/planning/rop.py`, `backend/planning/scoring.py`, `backend/planning/service.py` (90d daily EWMA & $\sigma_D$ integration with full float precision; `avg_response_days` & `cycles_90d` computed from `PurchaseOrder`/`POCommunication` rows and persisted on `Supplier`; `DemandForecastResult` retains 12 `monthly_buckets` for backward compatibility with `SuppliersPlanningPage.tsx` until `P3-7`).
  - `backend/inventory/schemas.py`, `backend/inventory/service.py` (`ConsumeResult.stockout_active` and pre-zero `ACCELERATED_BURN` detection via `part.daily_burn_ewma < earliest_po_eta`).
  - `backend/orchestrator/workflows.py` (`settle_daily_burn_on_clock_advance()` wired into `set_clock_and_watch`, `advance_clock_and_watch`, and `jump_to_po_eta_and_watch`; deduplicated `ACCELERATED_BURN` and `STOCKOUT_ACTIVE` alert emission in `_sync_burn_and_stockout_alerts()`).
  - `backend/orchestrator/routes/parts.py`, `backend/orchestrator/routes/suppliers.py` (added `GET /api/parts/{part_id}/cycle-analytics` & `GET /api/parts/{part_id}/po-suggestion`; enriched `GET /api/parts` & `GET /api/parts/{part_id}` with burn/runway/urgency/buffer/LT fields and `GET /api/suppliers` with `avg_response_days` & `cycles_90d`).
  - `backend/seed/data.py` (90-day Jul 3 – Sep 30 causal simulation across all 15 SKUs with 48 closed POs and **0 open POs** on `2026-10-01T07:30:00+00:00`; all supplier scores, effective LTs, EWMA burn, and initial `ROP/T` computed directly via `planning/`).
  - `tests/seed/test_seed_and_clock.py`, `tests/inventory/test_inventory.py`, `tests/planning/test_planning_engine.py`, `tests/orchestrator/{test_workflows_and_sla,test_logistics_and_escalation,test_stock_request_workflow,test_prd_fixes}.py` (updated assertions from superseded 4-open-PO / zero-stock-only `ACCELERATED_BURN` baseline to Phase 3 contracts).
- **Verification:** `uv run pytest -v` (`104 passed, 3 skipped` — 3 live-Gemini tests skipped offline) and `uv run ruff check backend tests` (`All checks passed!`), including all 5 AST checks in `tests/test_architecture.py`. `frontend/`, `backend/agents/`, and `backend/mailroom/` left untouched.

---

## Next Session Handoff Prompt — Start Slice `P3-3`

> Implement **Slice P3-3** per `docs/phase3/SLICES_P3.md` and `docs/phase3/DESIGN_P3.md`:
> - **Read:** `AGENTS.md`, `docs/phase3/STATUS_P3.md`, `docs/phase3/PRD_P3.md §2 (Pillar 4 items 1–2 & Pillar 3 item 2)`, `docs/phase3/DESIGN_P3.md §3.4 item 5`, `docs/phase3/SLICES_P3.md (Slice P3-3)`, and target files: `backend/po/{actions,__init__}.py`, `backend/planning/rop.py`, `backend/orchestrator/{workflows,stock_request_workflow,routes/alerts,routes/pos,routes/parts}.py`.
> - **Leave Untouched:** `frontend/`, `backend/agents/`, `backend/seed/data.py`, `backend/mailroom/`.
> - **Acceptance Criteria:**
>   1. Replace `reject_draft` suggested action with `edit_po` (`Edit PO`) on `PO_DISPATCH_APPROVAL` alerts (`_create_auto_replenishment_po`, `switch_to_backup_supplier`, `update_po` revised-PO path in `workflows.py`); implement `WorkflowEngine.dispatch_edited_po()` + `POST /api/pos/{po_id}/dispatch-edited` + `edit_po` CTA handler (updating existing draft PO's supplier/qty/price, superseding/cancelling old `DRAFT_PENDING_APPROVAL` comm, transitioning `PENDING_DISPATCH_APPROVAL -> SENT_AWAITING_ACK`, dispatching edited email, setting `promised_eta` & `next_sla_check_at`, and resolving the parent alert without creating duplicate POs).
>   2. Update `WorkflowEngine.reject_lex_draft()` so rejecting a draft communication sets `CommStatus.REJECTED` and records `ApprovalOutcome.REJECTED` **without** resolving the parent `ActionItem`.
>   3. Update `dismiss_action_item` and `GET /api/alerts` (`backend/po/actions.py`, `backend/orchestrator/routes/alerts.py`, `routes/parts.py`): dismissing an alert rejects any unapproved `DRAFT_PENDING_APPROVAL` linked comm, hides `draft_preview` (`None`) on `DISMISSED` alerts, exposes `unresolved_dismissed_alerts` on `PartSnapshot` / `GET /api/parts` while underlying PO/stock/quarantine conditions persist (`dismiss_resolved_at is None`), auto-sets `dismiss_resolved_at` once resolved, and adds `POST /api/alerts/{id}/reopen` and `POST /api/alerts/{id}/acknowledge-dismissed`.
>   4. Sweep all remaining backend `ROP/S` / `ROP / S` strings to `ROP/T` (e.g., `ROP_RECOMMENDATION` alert titles in `workflows.py`, docstrings/labels in `planning/rop.py`, `routes/parts.py`).
>   5. Add `tests/orchestrator/test_alerts_phase3.py`, run `uv run pytest -v` and `uv run ruff check backend tests`, verify `tests/test_architecture.py` passes, update `docs/phase3/STATUS_P3.md`, and commit cleanly.

---

## Plan vs. Implementation Discrepancies
*(Per `AGENTS.md §5`, document any discrepancy between `PRD_P3.md` / `DESIGN_P3.md` and actual implementation with proof and resolution here.)*

| Slice | Original Plan Reference | Discrepancy / Proof | Resolution Implemented |
|---|---|---|---|
| Baseline Audit | `PRD_P2.md §4.2` vs `HEAD (099474f)` | Post-Phase-2 commit `25a8bdc` re-introduced `Withdraw`, `Create PO`, `ROP/S`, and `Demand` actions in `InventoryPage.tsx`, and pre-existing `get_on_order_stock()` still counted `PENDING_DISPATCH_APPROVAL` while `matching.py` still enforced `invoice_qty == po_qty`. | Reconciled in `PRD_P3.md §1.1` and scheduled explicitly in `P3-1`–`P3-7`. |
| `P3-2` | `DESIGN_P3.md §3.2` vs `SuppliersPlanningPage.tsx:522` | `frontend/` is untouched until `P3-6`/`P3-7`, and `SuppliersPlanningPage.tsx` still reads `rec.forecast.monthly_buckets`. | Kept `DemandForecastResult.monthly_buckets` populated in `backend/planning/forecast.py` while delegating daily burn, $\sigma_D$, intermittent floor, and `ROP/T` calculations to 90-day daily EWMA ($\alpha=0.3$) in `ewma_daily.py`. |

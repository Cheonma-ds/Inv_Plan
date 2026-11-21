# InvPlan — Problems to Resolve (Pre-Slice 4 Audit)

**Audit Scope:** Full review of Slices 1–3 (`backend/core/`, `backend/inventory/`, `backend/po/`, `backend/mailroom/`, `backend/planning/`, `backend/agents/`, `backend/observability/`, `backend/orchestrator/`, `backend/seed/`) against `PRD.md`, `DESIGN.md`, and `AGENTS.md`.  
**Status:** Resolved — **all 18 items (`P1`–`P18`) fixed and verified** (`58 passed, 1 skipped`, `ruff` clean). Ready for Slice 4 (Frontend UI).

> **Required Post-Fix Update:** Once every problem below is resolved and verified, update this file by filling in the **Resolution Ledger** table at the bottom (`Problem ID`, `Summary`, `Solution`, `Proof of Resolution`) with concrete file paths, commit/changes, and executed test/command proof.

---

## Group A — Confirmed Runtime Crashes & Data Corruption

### P1. `seed_database(..., reset=...)` signature mismatch crashes FastAPI startup and `/api/admin/reset-seed`
- **Locations:**
  - `backend/main.py:19` — calls `seed_database(session, reset=False)` during app lifespan when `Supplier` table is empty.
  - `backend/orchestrator/routes/admin.py:12` — calls `seed_database(session, reset=True)`.
  - `backend/seed/data.py:398` — defines `def seed_database(session: Session) -> dict[str, int]:` with **no `reset` parameter**.
  - `backend/seed/reset.py:8-20` — defines `reset_and_seed(target_engine=None)`.
- **Failure Mode:** Starting `uvicorn` against a fresh DB or hitting `POST /api/admin/reset-seed` immediately raises `TypeError: seed_database() got an unexpected keyword argument 'reset'`.
- **Expected Fix:** Unify the contract cleanly: either support `reset: bool = False` inside `seed_database()` (or delegate `/api/admin/reset-seed` cleanly to `reset_and_seed()` using the bound session's engine and call `seed_database(session)` in `main.py`). Ensure `/api/admin/reset-seed` works cleanly even with an active SQLAlchemy session (no SQLite lock/drop-table conflict) and add FastAPI `TestClient` tests covering both empty-DB lifespan seeding and `POST /api/admin/reset-seed`.

### P2. `PATCH /api/parts/{part_id}/rop` crashes with `AttributeError`
- **Locations:**
  - `backend/orchestrator/routes/parts.py:119-135`
  - `backend/inventory/service.py:222-235`
- **Failure Mode:** `update_rop_and_replenishment()` returns a `PartSnapshot` Pydantic model (attribute name `part_id`), not a `Part` ORM model. Line 131 accesses `part.id`, crashing with `AttributeError: 'PartSnapshot' object has no attribute 'id'`.
- **Expected Fix:** Return `part.part_id` (or `part.model_dump()`) in `patch_part_rop`, and add an HTTP route test covering `PATCH /api/parts/{part_id}/rop` (both valid update and invalid `rop >= replenishment_point`).

### P3. `DELAYED_CRITICAL -> OVERDUE` missing from PO state machine crashes `run_sla_watcher()`
- **Locations:**
  - `backend/orchestrator/sla_watcher.py:253-274`
  - `backend/po/state_machine.py:40-43`
- **Failure Mode:** `run_sla_watcher()` queries both `ACKNOWLEDGED` and `DELAYED_CRITICAL` POs and attempts `transition_po(session, po, POStatus.OVERDUE)` once `sim_now >= effective_eta + 1 day` with no GRN. However, `ALLOWED_TRANSITIONS[POStatus.DELAYED_CRITICAL]` only permits `{RECEIVED_PENDING_INVOICE, CANCELLED}`, raising `InvalidPOTransitionError` and aborting the entire watcher run.
- **Expected Fix:** Add `POStatus.OVERDUE` to `ALLOWED_TRANSITIONS[POStatus.DELAYED_CRITICAL]` in `backend/po/state_machine.py` (and verify `DESIGN.md`/`PRD.md` compatibility), plus add unit and SLA watcher tests proving a `DELAYED_CRITICAL` PO cleanly transitions to `OVERDUE` if its revised ETA is missed.

### P4. JSON double-encoding corrupts updated `ROP_RECOMMENDATION` alerts
- **Locations:**
  - `backend/core/models.py:241-244` (`suggested_actions_json` and `cta_payload_json` are SQLAlchemy `JSON` columns)
  - `backend/po/actions.py:39-40` (assigns native Python `list`/`dict`)
  - `backend/orchestrator/workflows.py:1159-1160` (assigns `json.dumps(suggested)` and `json.dumps(cta_payload)` when updating an existing alert)
  - `backend/orchestrator/workflows.py:1511-1512` (workaround masking the bug by calling `json.loads()` on strings)
- **Failure Mode:** On a second `recalculate_planning()` call, SQLAlchemy serializes a JSON *string* into the `JSON` column. `GET /api/alerts` (`backend/orchestrator/routes/alerts.py:57-58`) then returns raw strings instead of a JSON array/object for `suggested_actions` and `cta_payload`.
- **Expected Fix:** Assign native `suggested` (`list[dict]`) and `cta_payload` (`dict`) directly on lines 1159–1160, remove the unused `json` import / string-parsing band-aid in `workflows.py`, and add a test verifying `GET /api/alerts` returns native `list` and `dict` types both after initial creation and after updating an existing `ROP_RECOMMENDATION` alert.

---

## Group B — Domain Logic, Math & PRD Alignment

### P5. Overdue POs without GRN are ignored in Supplier Reliability Scoring, and major PO events do not refresh scores
- **Locations:**
  - `backend/planning/scoring.py:130-158`
  - `backend/orchestrator/sla_watcher.py:267-327`
  - `backend/orchestrator/workflows.py:929-970`
  - Reference: `PRD.md:248`, `PRD.md:268`, `PRD.md:282`, `PRD.md:433` (Acceptance Scenario 5)
- **Problem:**
  1. `compute_supplier_score()` skips any PO where `grn is None`. An `OVERDUE` PO with no physical delivery therefore has **zero** impact on `otd_rate`, `avg_days_late`, or `reliability_score` — directly violating Acceptance Scenario 5 (*"System detects overdue PO with no GRN... and updates `Sup2`'s reliability score"*).
  2. Neither transitioning a PO to `OVERDUE` nor closing a PO after 3-way match refreshes the supplier's persisted reliability metrics automatically.
- **Expected Fix:**
  1. Include `OVERDUE` POs without GRNs in OTD evaluation: treat them as late deliveries (not on-time) with `late_days = max(1.0, (now - promised_eta).total_seconds() / 86400.0)` so overdue non-deliveries immediately penalize OTD and `avg_days_late`.
  2. Trigger a targeted supplier score + effective lead time refresh (or `recalculate_all`) when a PO becomes `OVERDUE` or transitions to `CLOSED` after invoice match.
  3. Add tests proving Scenario 1B lowers the supplier's persisted `reliability_score` and `otd_rate` immediately upon overdue detection, and Scenario 1C updates them on clean/mismatched close.

### P6. Lead-time consistency ($\sigma_L$) in supplier scoring mixes heterogeneous SKUs' raw lead times
- **Locations:**
  - `backend/planning/scoring.py:159-163`
  - Reference: `PRD.md:297` (*"Spread ($\sigma_L$) between quoted and actual lead times"*)
- **Problem:** `compute_supplier_score()` computes `statistics.stdev(actual_lts)` across all parts supplied by that supplier. If `Sup1` supplies Part A (quoted 8d, delivered in 8d) and Part B (quoted 14d, delivered in 14d) with 100% on-time perfection, `stdev([8, 14, ...])` penalizes `Sup1`'s consistency score simply because the parts have different quoted lead times.
- **Expected Fix:** Compute supplier-level lead-time consistency from the spread of **lead-time deviation from quote** (`actual_lt - quoted_lt`) across delivered POs (e.g., `stdev(lt_deltas)` or RMS deviation relative to `avg_quoted_lt`), while keeping per-`(part, supplier)` `compute_effective_lead_time()`'s `lead_time_std_dev` as per-SKU actual lead-time std dev (since a single `(part, supplier)` term shares one quoted LT). Add a unit test verifying a supplier delivering two different-LT parts exactly on quote receives full 25/25 consistency points.

### P7. `evaluate_part_recommendation()` reads stale persisted lead times instead of computing live values
- **Locations:**
  - `backend/planning/service.py:26-41` vs `backend/planning/service.py:61-98`
- **Problem:** `get_planning_overview()` computes fresh `effective_lead_time_days` and `lead_time_std_dev` via `compute_effective_lead_time()`, whereas `evaluate_part_recommendation()` (called by `apply_rop_recommendation`) reads the potentially stale persisted columns on `PartSupplierTerm`. Calling `POST /api/planning/parts/{id}/apply-recommendation` without calling `/recalculate` first can apply different ROP/S numbers than `/api/planning/overview` displayed.
- **Expected Fix:** Have `evaluate_part_recommendation()` compute fresh `compute_effective_lead_time()` for `primary_term` (matching `get_planning_overview()`'s read-only proxy behavior) and persist updated term metrics if needed when applying, or guarantee `/overview` and `evaluate_part_recommendation()` always return identical `ROPRecommendation` values. Add a regression test verifying parity before `/recalculate` is called.

### P8. Cancelling a `QUARANTINED_INVOICE_MISMATCH` PO leaks `part.quarantined_stock`
- **Locations:**
  - `backend/po/state_machine.py:52-55`
  - `backend/po/service.py:316-361` (`switch_po_to_backup_supplier`)
- **Problem:** `ALLOWED_TRANSITIONS` allows `QUARANTINED_INVOICE_MISMATCH -> CANCELLED`, but nothing clears/decrements `part.quarantined_stock` (or marks the held GRN as returned/rejected) when such a PO is cancelled, permanently stranding phantom units in `quarantined_stock`.
- **Expected Fix:** When cancelling a PO that is in `QUARANTINED_INVOICE_MISMATCH` (whether via backup switch or direct `cancel_po`), decrement `part.quarantined_stock` by the held GRN quantity (via `inventory/` service in the orchestrator — see P13) and log a clear note/audit trail. Add a test verifying `quarantined_stock` returns to `0` on cancellation of a quarantined PO.

### P9. Quarantine SLA timer anchors on `latest_inv.received_at` even when the invoice arrived before GRN
- **Locations:**
  - `backend/orchestrator/sla_watcher.py:381-482`
- **Problem:** If a supplier emails a mismatched PDF invoice 3 days *before* the physical shipment arrives and GRN is logged, `latest_inv.received_at` is already 72h in the past the instant quarantine starts. `run_sla_watcher()` immediately fires the 48h reminder (and escalates at 96h only 24h after quarantine actually began).
- **Expected Fix:** Anchor the quarantine elapsed-time calculation at `max(latest_inv.received_at, latest_grn.received_at)` (the actual timestamp the shipment entered quarantine, or the timestamp of the latest mismatched invoice if later). Add a test where an invoice arrives 72h before GRN and confirm the 48h reminder does *not* fire until 48h *after* GRN.

### P10. `on_order_stock` vs `quarantined_stock` overlap / semantic clarity
- **Locations:**
  - `backend/inventory/service.py:44-66`
  - Reference: `PRD.md:134-138`, `AGENTS.md §3` (*`inventory_position = on_hand_stock + on_order_stock < rop`*)
- **Problem:** `get_on_order_stock()` sums all non-`CLOSED`/non-`CANCELLED` PO quantities minus only `RELEASED` GRNs. When goods arrive and enter `QUARANTINED_INVOICE_MISMATCH`, those same units appear in **both** `part.quarantined_stock` and `on_order_stock`, making a part with `0` on-hand and `10` quarantined look like it also has `10` additional units still in transit.
- **Careful Guardrail Constraint:** Simply subtracting `HELD_QUARANTINE` from `on_order_stock` *without* accounting for it in replenishment would cause `inventory_position = on_hand + on_order` to drop below `ROP` during an invoice dispute and trigger a **duplicate PO**.
- **Expected Fix:** Resolve this cleanly and explicitly:
  - Keep `on_order_stock` strictly representing units on open POs **not yet physically received** (subtracting all active GRNs, both `RELEASED` and `HELD_QUARANTINE`), **OR** if `inventory_position` must remain `on_hand + on_order` per `AGENTS.md §3`, explicitly subtract only `RELEASED` GRNs for `inventory_position` while exposing `in_transit_stock` / ensuring quarantined units are never double-counted in UI/API stock totals.
  - Preferred clean design (preserves `PRD.md:137` AND prevents duplicate POs): `on_order_stock` = unreceived qty on open POs (`ordered - all_grn_received`); `in_quarantine` = `part.quarantined_stock` (plus pending-invoice un-released GRN qty); `inventory_position` = `on_hand_stock + on_order_stock + pending_incoming_qty` (Wait — verify against `AGENTS.md §3`: `inventory_position = on_hand_stock + on_order_stock < rop` and `tests/po/test_matching_and_quarantine.py:195` which asserts `get_on_order_stock == 100` in `RECEIVED_PENDING_INVOICE`!).
  - **Concrete Resolution Rule:** To honor `AGENTS.md §3` (`inventory_position = on_hand_stock + on_order_stock`) AND eliminate double-counting:
    - In `RECEIVED_PENDING_INVOICE` (GRN logged, invoice not yet mismatched): `part.quarantined_stock == 0` and `on_order_stock == 100` (no double count).
    - Once in `QUARANTINED_INVOICE_MISMATCH` (`part.quarantined_stock == 100`): either (a) deduct `part.quarantined_stock` from `on_order_stock` and ensure the reorder trigger checks `on_hand + on_order + quarantined < rop` so quarantined goods don't spawn duplicate POs, **or** (b) document clearly on `PartSnapshot` with an explicit `in_transit_stock` field (`on_order_stock - quarantined_stock` clamped `>= 0`) and keep `inventory_position = on_hand_stock + on_order_stock`. Pick **(a)** if updating `get_inventory_position` cleanly with a test covering no-duplicate-PO during quarantine, and document the chosen invariant explicitly in `STATUS.md` and `problems_to_resolve.md`.

### P11. Invoice discrepancy impact & multi-line invoice aggregation bugs in `workflows.py`
- **Locations:**
  - `backend/orchestrator/workflows.py:847-859` (multi-line unit price derivation)
  - `backend/orchestrator/workflows.py:980-982` (manual recomputation of `price_diff`, `total_price_impact`, `qty_diff`)
  - `backend/po/matching.py:30-95`
- **Problems:**
  1. `workflows.py:980-982` ignores `match_result.price_diff_per_unit`, `match_result.total_price_impact`, and `match_result.qty_diff` and recomputes them with a different formula (`(inv_price - po_price) * inv_qty`), which reports `total_price_impact = 0.0` on quantity mismatches or subtotal arithmetic errors and disagrees with `matching.py` (`inv_qty - grn_qty` vs `inv_qty - po_qty`).
  2. For multi-line invoices with differing unit prices (`workflows.py:856-858`), deriving `extracted_unit_price = round(extraction.subtotal / extracted_qty, 2)` cancels out any subtotal arithmetic error (`qty * (subtotal / qty) == subtotal`), preventing `evaluate_three_way_match()` from detecting `math_ok = False`!
- **Expected Fix:**
  1. Always consume `match_result.price_diff_per_unit`, `match_result.total_price_impact`, and `match_result.qty_diff` directly in `_handle_invoice_match_outcome()`.
  2. For multi-line invoices with differing unit prices, compute weighted average unit price from line items (`sum(li.qty * li.unit_price) / extracted_qty`, or `sum(li.line_total) / extracted_qty` if line totals differ from `qty * unit_price`, and validate each line's arithmetic explicitly) — never divide `extraction.subtotal / extracted_qty` in a way that masks arithmetic errors. Add unit tests for both cases.

### P12. Alert Urgency Score is hardcoded to `50` everywhere, breaking priority sorting
- **Locations:**
  - `backend/po/actions.py:22, 59`
  - `backend/orchestrator/workflows.py` (all `create_action_item()` calls)
  - `backend/orchestrator/sla_watcher.py` (all `create_action_item()` calls)
  - Reference: `PRD.md:335` (*"Sorted by Urgency Score (Criticality × Days-to-Stockout × Escalation Severity)"*)
- **Problem:** No caller ever passes `urgency_score`, so every alert gets `50` and `GET /api/alerts` degrades to pure reverse-chronological order (a new `INFO` alert sorts above an older `CRITICAL` stockout alert).
- **Expected Fix:** Implement a deterministic `compute_urgency_score(severity, part_criticality=..., days_to_stockout=...) -> int` helper (e.g., 0–100 scale combining severity base weight + criticality weight + stockout proximity boost) and pass it on all `create_action_item()` calls (and updates in `recalculate_planning()`). Also expose `urgency_score` in `GET /api/alerts` (`backend/orchestrator/routes/alerts.py:44-63`). Add tests verifying `CRITICAL` alerts always outrank `HIGH`/`MEDIUM`/`INFO` alerts regardless of creation timestamp.

### P13. Scenario 1A (`POST_ACK_DELAY`) and Scenario 2 (`PO_EXCEPTION`) missing deterministic stockout-risk & price-variance math
- **Locations:**
  - `backend/orchestrator/workflows.py:497-550` (Scenario 2: `EXCEPTION_INTENTS`)
  - `backend/orchestrator/workflows.py:552-666` (Scenario 1A: `POST_ACK_DELAY`)
  - Reference: `PRD.md:220-226` (Scenario 2) & `PRD.md:231-238` (Scenario 1A)
- **Problems:**
  1. **Scenario 1A:** Uses a static proxy `part.criticality == CRITICAL or part.on_hand_stock <= part.rop` instead of computing `avg_daily_demand` (via `backend/planning/forecast.py`'s `compute_demand_forecast()`), `projected_days_of_supply = on_hand_stock / avg_daily_demand`, and `days_to_new_eta = (revised_eta - sim_now).days`, flagging `HIGH`/critical impact when `part.criticality == CRITICAL` OR `days_to_new_eta >= (projected_days_of_supply - buffer_days)` (e.g., `buffer_days = 3`).
  2. **Scenario 2:** Does not compute deterministic price variance (`price_variance_inr`, `price_variance_pct`, `total_cost_impact_inr`) or projected stockout date (`projected_stockout_date`, `projected_days_of_supply`, `days_short_of_eta`) for the `PO_EXCEPTION` alert summary and `cta_payload`.
- **Expected Fix:** Since `orchestrator/workflows.py` is explicitly allowed to compose `planning/` + `po/` + `inventory/`, compute deterministic demand/stockout projections (via `compute_demand_forecast`) and exact price variance in both Scenario 1A and Scenario 2, include them in the alert `summary` and `cta_payload`, and feed `days_to_stockout` into `compute_urgency_score()`. Add tests verifying a long delay on a `STANDARD` part above ROP that breaches projected days of supply triggers `DELAYED_CRITICAL`, while a safe-buffer non-critical delay stays minor (`ACKNOWLEDGED` + `INFO`), and verifying Scenario 2 alert payloads include exact INR/% variance and stockout dates.

---

## Group C — Module Independence & Architecture Rules (`AGENTS.md §2`)

### P14. `backend/po/service.py` directly mutates `inventory/`-owned tables (`Part` stock & `InventoryTransaction`), leaving `inventory/service.py` quarantine helpers dead
- **Locations:**
  - `backend/po/service.py:131-188` (`_apply_match_outcome` directly writes `part.on_hand_stock`, `part.quarantined_stock`, and inserts `InventoryTransaction`)
  - `backend/inventory/service.py:238-273` (`hold_in_quarantine`, `release_quarantined_stock` — currently unused by production code)
  - Reference: `AGENTS.md §2` Rules 1–2
- **Problem:** Although Python import AST checks pass, `po/` reaches across the domain boundary via shared ORM models in `core/models.py` to mutate `inventory/`-owned stock balances and ledger rows instead of letting `orchestrator/` coordinate `po/` -> `inventory/`.
- **Expected Fix:**
  - Keep `backend/po/service.py` strictly responsible for `PurchaseOrder`, `GoodsReceipt`, `Invoice`, and `ActionItem` state/records. Have `record_goods_receipt()` and `submit_invoice_and_match()` return structured stock-movement directives (e.g., `action: "NONE" | "HOLD_QUARANTINE" | "RELEASE_DIRECT" | "RELEASE_FROM_QUARANTINE"`, `part_id`, `qty`, `note`) — or have `po/service.py` stay purely PO-domain while `orchestrator/workflows.py` invokes `inventory/service.py` (`hold_in_quarantine`, `release_quarantined_stock`, plus a new `receive_usable_stock` and `clear_quarantined_stock` for P8).
  - Update `tests/po/test_matching_and_quarantine.py` and `tests/orchestrator/test_invoice_pipeline.py` so pure `po/` tests assert PO/GRN/Invoice state + stock-effect directives, while orchestrator tests assert full end-to-end `Part` stock balances and `InventoryTransaction` rows.
  - Strengthen `tests/test_architecture.py` (or add a targeted boundary check) to verify `backend/po/` does not instantiate `InventoryTransaction` or mutate `Part.on_hand_stock`/`Part.quarantined_stock`.

### P15. `EmailTransport` abstraction is bypassed during draft approval/rejection
- **Locations:**
  - `backend/mailroom/transport.py:42-165`
  - `backend/mailroom/service.py:85-128` (`approve_draft`, `reject_draft`)
  - Reference: `AGENTS.md §2` Rule 5 (*"Preserve `EmailTransport` abstraction: Keep all mailroom operations behind `backend/mailroom/`'s `EmailTransport` protocol so real IMAP/Gmail can be swapped in later."*)
- **Problem:** `approve_draft()` and `reject_draft()` query and mutate `POCommunication` directly via SQLAlchemy without routing dispatch through `EmailTransport`. Swapping in a real IMAP/Gmail transport would never send approved Lex drafts.
- **Expected Fix:** Extend `EmailTransport` protocol (and `DatabaseEmailTransport`) with `approve_draft(...)` / `reject_draft(...)` (or `dispatch_draft(session, comm_id, ...)`), and route `mailroom/service.py`'s `approve_draft`, `reject_draft`, `get_communication`, and `annotate_triage` through the `transport: EmailTransport` parameter so `mailroom/service.py` performs zero direct DB mutations outside the transport. Verify all mailroom tests pass with a swappable mock/custom `EmailTransport`.

### P16. `POST /api/pos/{po_id}/switch-supplier` duplicates workflow orchestration and calls private `engine._create_lex_draft()`
- **Locations:**
  - `backend/orchestrator/routes/pos.py:232-294`
  - `backend/orchestrator/workflows.py:1454-1508`
- **Problem:** The route duplicates `WorkflowEngine`'s backup-supplier failover logic, calls private `engine._create_lex_draft()`, and forgets to auto-resolve open alerts on the cancelled PO (or clear `quarantined_stock` if cancelling a quarantined PO).
- **Expected Fix:** Extract a single public `WorkflowEngine.switch_to_backup_supplier(session, po_id, backup_supplier_id=None)` method that handles cancellation (including P8 quarantine cleanup), replacement PO creation, Lex draft creation, new dispatch-approval alert, and auto-resolving open alerts on the cancelled PO. Have both `POST /api/pos/{po_id}/switch-supplier` and `execute_alert_cta(action="switch_backup_supplier")` delegate to this single method.

---

## Group D — Missing Endpoints, CTA Gaps & Minor Spec Alignment (`DESIGN.md §6 & §9`)

### P17. Missing API endpoints & alert CTA capabilities specified in PRD / `DESIGN.md §9`
- **Locations:**
  - `backend/orchestrator/routes/parts.py` — no endpoint to set/clear `part.manual_demand_override` (`PRD.md:290`, `DESIGN.md §7`).
  - `backend/orchestrator/routes/clock.py` — no `POST /api/clock` endpoint to set simulated time directly (`DESIGN.md §9:245`).
  - `backend/orchestrator/workflows.py:1358-1539` (`execute_alert_cta`) — missing `cancel_po` CTA (`PRD.md:226`), and `update_po` does not fall back to `item.cta_payload_json` (`proposed_unit_price`, `proposed_qty`, `proposed_eta_date`) when `params` omits them.
- **Expected Fix:**
  1. Add `PATCH /api/parts/{part_id}/demand-override` (or extend `PATCH /api/parts/{part_id}`) to set/clear `manual_demand_override: float | None`.
  2. Add `POST /api/clock` accepting `{"sim_now": "<ISO-8601>"}` (validating non-backwards unless `allow_reset=True`, and running `run_sla_watcher` when moving forward).
  3. Add `cancel_po` support in `execute_alert_cta` (and include it in `PO_EXCEPTION` `suggested_actions` per `PRD.md:226`, cleaning up any quarantined stock per P8 and auto-resolving open PO alerts).
  4. Make `update_po` in `execute_alert_cta` default missing `unit_price`/`qty`/`revised_eta` from `item.cta_payload_json` (`proposed_unit_price`, `proposed_qty`, `proposed_eta_date`) while letting explicit `params` override them.
  5. Add route/workflow tests for all four additions.

### P18. Minor schema field-name alignment & `POST /api/pos/{po_id}` exception consistency check
- **Locations:**
  - `backend/agents/lex/schemas.py:74-86` (`po_number` vs `po_reference`, `total_amount` vs `grand_total` in `DESIGN.md §6.2`)
  - `backend/orchestrator/routes/pos.py:198-230` (`PATCH /api/pos/{po_id}`)
- **Problem:**
  1. `InvoiceExtractionResult` uses `po_number` and `total_amount` instead of `po_reference` and `grand_total` documented in `DESIGN.md §6.2`. While internal and consistent today, Slice 5 YAML evals (`DESIGN.md §8`) and frontend consumers should not hit surprise naming drift.
  2. `PATCH /api/pos/{po_id}` directly mutates terms via `update_po_terms` without checking if the PO is in `CLOSED`/`CANCELLED` terminal status.
- **Expected Fix:**
  1. Either keep `po_number`/`total_amount` and update `DESIGN.md §6.2` (or add Pydantic validation aliases / rename cleanly — check which is cleanest with existing prompts/tests, and make code + `DESIGN.md §6.2` match 100%).
  2. Guard `update_po_terms()` against mutating `CLOSED` or `CANCELLED` POs (`ValueError` -> `400`). Add a unit test.

---

## Resolution Ledger

*(Completed during the Pre-Slice 4 hardening pass — all 18 problems `P1`–`P18` resolved and verified via `uv run pytest -v` and `uv run ruff check backend tests`.)*

| Problem ID | Problem Summary | Solution Implemented | Proof of Resolution |
|---|---|---|---|
| P1 | `seed_database(..., reset=...)` TypeError on startup & `/api/admin/reset-seed` | Updated `seed_database(session: Session, *, reset: bool = False)` in `backend/seed/data.py:399-409` so `reset=True` deletes all rows across `reversed(Base.metadata.sorted_tables)`, flushes, and clears session identity map (`session.expunge_all()`) inside the active session without SQLite table-lock conflicts, while `reset=False` seeds empty DBs on FastAPI lifespan startup. | `tests/seed/test_seed_and_clock.py::test_fastapi_lifespan_empty_db_and_admin_reset_seed_endpoint` & `test_reset_and_seed_populates_complete_realistic_baseline` (`uv run pytest tests/seed/test_seed_and_clock.py -v` → `3 passed`). |
| P2 | `PATCH /api/parts/{part_id}/rop` `AttributeError` (`part.id` on `PartSnapshot`) | Fixed `backend/orchestrator/routes/parts.py:136` to return `"part_id": part.part_id` from the `PartSnapshot` returned by `update_rop_and_replenishment()`. | `tests/orchestrator/test_workflows_and_sla.py::test_fastapi_routes_http_end_to_end` (verifies `200` valid ROP update, `400` when `rop >= replenishment_point`, and `404` for missing part → `PASSED`). |
| P3 | `DELAYED_CRITICAL -> OVERDUE` missing in state machine crashes SLA watcher | Added `POStatus.OVERDUE` to `ALLOWED_TRANSITIONS[POStatus.DELAYED_CRITICAL]` in `backend/po/state_machine.py:40-44` and updated `DESIGN.md §4.2`. | `tests/po/test_state_machine.py::test_delayed_critical_can_transition_to_overdue`, `test_every_allowed_and_forbidden_transition`, and `tests/orchestrator/test_workflows_and_sla.py::test_scenario_1a_standard_part_above_rop_stockout_math_and_delayed_critical_to_overdue` (`PASSED`). |
| P4 | `json.dumps` double-encoding on updated `ROP_RECOMMENDATION` alerts | Assigned native Python `list`/`dict` objects directly to `primary.suggested_actions_json = suggested` and `primary.cta_payload_json = cta_payload` in `backend/orchestrator/workflows.py:1349-1350`, simplified `approve_rop` CTA parsing, and removed `import json` from `workflows.py`. | `tests/planning/test_planning_engine.py::test_planning_api_recalculate_alerts_and_apply_recommendation` (calls `POST /api/planning/recalculate` twice, then `GET /api/alerts`, asserting `isinstance(a["suggested_actions"], list)` and `isinstance(a["cta_payload"], dict)` → `PASSED`). |
| P5 | Overdue POs ignored in supplier scoring & no score refresh on overdue/close | Updated `compute_supplier_score()` in `backend/planning/scoring.py:124-169` to include `OVERDUE` POs without GRN in OTD evaluation (`late_days = max(1.0, (now - promised_eta)/86400)`), added `refresh_supplier_metrics()` in `backend/planning/service.py:59-83`, and invoked it in `backend/orchestrator/sla_watcher.py` (on `OVERDUE`, `ACK_REMINDER`, `UNRESPONSIVE_ESCALATED`) and `backend/orchestrator/workflows.py` (in `_handle_invoice_match_outcome`). | `tests/planning/test_planning_engine.py::test_overdue_po_without_grn_penalizes_supplier_otd_and_reliability_score` and `tests/orchestrator/test_workflows_and_sla.py::test_scenario_1a_standard_part_above_rop_stockout_math_and_delayed_critical_to_overdue` (`PASSED`). |
| P6 | Lead-time consistency ($\sigma_L$) mixes raw LTs across heterogeneous SKUs | Updated `compute_supplier_score()` in `backend/planning/scoring.py:124-180` to compute `lt_std_dev = statistics.stdev(lt_deltas)` over `actual_lt - quoted_lt` deviations per PO (while keeping per-`(part, supplier)` `compute_effective_lead_time()` as per-SKU actual LT std dev). | `tests/planning/test_planning_engine.py::test_lead_time_consistency_across_heterogeneous_sku_lead_times` (supplier delivering 8d-quoted and 14d-quoted SKUs on quote gets `lead_time_std_dev == 0.0`, `25.0/25.0` consistency, `100.0` score → `PASSED`). |
| P7 | `evaluate_part_recommendation()` uses stale persisted lead times | Updated `evaluate_part_recommendation()` in `backend/planning/service.py:26-56` to compute live `compute_effective_lead_time(session, primary_term, sim_now=sim_now)` via an in-memory proxy `PartSupplierTerm`, matching `get_planning_overview()`. | `tests/planning/test_planning_engine.py::test_evaluate_part_recommendation_matches_overview_even_with_stale_persisted_term` (`PASSED`). |
| P8 | Cancelling a quarantined PO leaks `part.quarantined_stock` | Added `clear_quarantined_stock()` in `backend/inventory/service.py:293-303` and `_clear_quarantined_stock_for_cancelled_po()` in `backend/orchestrator/workflows.py:1367-1391`, called from both `switch_to_backup_supplier()` and `cancel_po_workflow()` along with `auto_resolve_po_alerts()`. | `tests/orchestrator/test_invoice_pipeline.py::test_cancelling_or_switching_quarantined_po_clears_quarantined_stock_and_resolves_alerts` and `tests/inventory/test_inventory.py::test_quarantined_stock_deducted_from_on_order_without_spawning_duplicate_po` (`PASSED`). |
| P9 | Quarantine SLA timer anchors on `latest_inv.received_at` instead of quarantine start | Updated `run_sla_watcher()` Section 4 in `backend/orchestrator/sla_watcher.py:397-420` to anchor quarantine elapsed time at `max(latest_inv.received_at, latest_grn.received_at)` so early pre-GRN invoices only start the 48h/96h quarantine clock when physical goods are received and quarantined. | `tests/orchestrator/test_workflows_and_sla.py::test_quarantine_sla_timer_anchors_on_max_of_invoice_and_grn_timestamps` (`PASSED`). |
| P10 | `on_order_stock` vs `quarantined_stock` overlap / double-counting | Implemented Option (a) in `backend/inventory/service.py:44-89, 138`: `get_on_order_stock()` deducts `part.quarantined_stock` (clamped `>= 0`) once a shipment enters `QUARANTINED_INVOICE_MISMATCH` (eliminating double-counting between `on_order_stock` and `quarantined_stock`), while `inventory_position = on_hand_stock + on_order_stock + quarantined_stock` ensures quarantined units still prevent duplicate POs during an invoice dispute. | `tests/inventory/test_inventory.py::test_quarantined_stock_deducted_from_on_order_without_spawning_duplicate_po` (`PASSED`). |
| P11 | Invoice discrepancy impact & multi-line unit-price math bugs in `workflows.py` | Updated `_process_pdf_invoice_email()` (`backend/orchestrator/workflows.py:1003-1017`) to compute weighted average unit price from `sum(li.qty * li.unit_price) / extracted_qty` so subtotal arithmetic errors are caught (`math_ok=False`), and updated `_handle_invoice_match_outcome()` (`workflows.py:1145-1147`) to consume `match_result.price_diff_per_unit`, `match_result.total_price_impact`, and `match_result.qty_diff` directly. | `tests/orchestrator/test_invoice_pipeline.py::test_multiline_invoice_weighted_price_detects_subtotal_math_error_and_match_impacts` (`PASSED`). |
| P12 | Alert `urgency_score` hardcoded to `50` everywhere | Implemented deterministic `compute_urgency_score(severity, *, part_criticality=None, days_to_stockout=None) -> int` in `backend/po/actions.py:26-61` with non-overlapping severity bands (`CRITICAL`: 80–100, `HIGH`: 60–79, `MEDIUM`: 35–54, `INFO`: 10–29), wired into `create_action_item()`, `workflows.py`, `sla_watcher.py`, and exposed `"urgency_score"` in `GET /api/alerts` (`backend/orchestrator/routes/alerts.py:49`). | `tests/po/test_matching_and_quarantine.py::test_compute_urgency_score_strict_severity_hierarchy` and `tests/planning/test_planning_engine.py::test_planning_api_recalculate_alerts_and_apply_recommendation` (`PASSED`). |
| P13 | Scenario 1A & Scenario 2 missing deterministic stockout-risk & price-variance math | Updated `process_inbound_supplier_email()` in `backend/orchestrator/workflows.py:531-828` to compute deterministic `avg_daily_demand`, `projected_days_of_supply`, `projected_stockout_date`, `days_to_new_eta`/`days_to_proposed_eta`, `days_short_of_eta` (with 3-day safety buffer branching for Scenario 1A), and `price_variance_inr`, `price_variance_pct`, `total_cost_impact_inr` for Scenario 2, surfacing them in `summary` and `cta_payload`. | `tests/orchestrator/test_workflows_and_sla.py::test_scenario_1a_standard_part_above_rop_stockout_math_and_delayed_critical_to_overdue` and `test_scenario_2_price_variance_stockout_payload_default_update_po_and_cancel_po_cta` (`PASSED`). |
| P14 | `po/service.py` directly mutates `inventory/`-owned tables; dead quarantine helpers | Removed `Part`/`InventoryTransaction` mutations and imports from `backend/po/service.py`; `_apply_match_outcome()` now records `stock_action` (`"HOLD_QUARANTINE"`, `"RELEASE_DIRECT"`, `"RELEASE_FROM_QUARANTINE"`, `"NONE"`), `stock_qty`, and `stock_note` on `ThreeWayMatchResult`, and `WorkflowEngine._handle_invoice_match_outcome()` coordinates `inventory/service.py` (`hold_in_quarantine`, `release_quarantined_stock`, `receive_stock_direct`). Enforced via AST test in `tests/test_architecture.py`. | `tests/test_architecture.py::test_po_module_does_not_mutate_inventory_tables_directly`, `tests/po/test_matching_and_quarantine.py::test_quarantine_lock_no_double_count_and_clean_release`, and `tests/orchestrator/test_invoice_pipeline.py::test_scenario_1c_price_mismatch_quarantine_draft_then_corrected_v2_release` (`PASSED`). |
| P15 | `EmailTransport` abstraction bypassed on `approve_draft` / `reject_draft` | Extended `EmailTransport` protocol and `DatabaseEmailTransport` in `backend/mailroom/transport.py:67-262` with `get_communication`, `approve_draft`, `reject_draft`, and `annotate_triage`, and routed all functions in `backend/mailroom/service.py` through `transport: EmailTransport = default_transport` with zero direct ORM queries in `service.py`. Enforced via AST test in `tests/test_architecture.py`. | `tests/mailroom/test_mailroom.py::test_swappable_custom_email_transport_handles_all_mailroom_operations` and `tests/test_architecture.py::test_mailroom_service_delegates_strictly_through_email_transport` (`PASSED`). |
| P16 | `POST /api/pos/{po_id}/switch-supplier` duplicates workflow & calls private `_create_lex_draft` | Extracted public `WorkflowEngine.switch_to_backup_supplier()` in `backend/orchestrator/workflows.py:1416-1484` and delegated both `POST /api/pos/{po_id}/switch-supplier` (`backend/orchestrator/routes/pos.py:229-256`) and `execute_alert_cta(action="switch_backup_supplier")` to it. | `tests/orchestrator/test_invoice_pipeline.py::test_cancelling_or_switching_quarantined_po_clears_quarantined_stock_and_resolves_alerts` and `tests/orchestrator/test_workflows_and_sla.py::test_sla_watcher_ack_escalation_pre_eta_overdue_and_quarantine` (`PASSED`). |
| P17 | Missing `manual_demand_override` endpoint, `POST /api/clock`, `cancel_po` CTA, and `update_po` defaults | Added `PATCH /api/parts/{part_id}/demand-override` (`backend/orchestrator/routes/parts.py:144-160`), `POST /api/clock` (`backend/orchestrator/routes/clock.py:48-75`), `cancel_po` CTA + `WorkflowEngine.cancel_po_workflow()` (`workflows.py:1393-1414, 1656-1665`), and `cta_payload_json` fallback defaults for `update_po` in `execute_alert_cta()` (`workflows.py:1563-1589`). | `tests/orchestrator/test_workflows_and_sla.py::test_fastapi_routes_http_end_to_end` and `test_scenario_2_price_variance_stockout_payload_default_update_po_and_cancel_po_cta` (`PASSED`). |
| P18 | `InvoiceExtractionResult` / `DESIGN.md §6.2` field naming alignment & terminal PO edit guard | Added `po_reference`, `grand_total`, and `part_code` input validators & properties on `InvoiceExtractionResult` / `InvoiceLineItem` (`backend/agents/lex/schemas.py:66-120`) while keeping `model_json_schema()` free of `anyOf`, aligned `DESIGN.md §6.2`, and guarded `update_po_terms()` (`backend/po/service.py:113-114`) against mutating `CLOSED` or `CANCELLED` POs. | `tests/agents/test_lex_agent.py::test_invoice_extraction_result_supports_design_aliases_cleanly`, `tests/po/test_matching_and_quarantine.py::test_update_po_terms_rejects_terminal_closed_or_cancelled_pos`, and `tests/orchestrator/test_workflows_and_sla.py::test_fastapi_routes_http_end_to_end` (`PASSED`). |

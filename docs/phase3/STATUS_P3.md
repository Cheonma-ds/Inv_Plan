# STATUS_P3.md — Phase 3 Live Session Baton

**Current Stage:** **Slice P3-7 Complete** — Ready to Start **Slice P3-8**  
**Last Updated:** 2026-11-24  
**Baseline Reference:** `HEAD` (`099474f`), [`STATUS.md`](../../STATUS.md), [`STATUS_P2.md`](../phase2/STATUS_P2.md)  
**Planning Docs:** [`PRD_P3.md`](PRD_P3.md) · [`DESIGN_P3.md`](DESIGN_P3.md) · [`SLICES_P3.md`](SLICES_P3.md)

---

## Slice Progress Tracker

- [x] **Slice P3-1:** Core Clock (`Oct 1 13:00 IST`), Schema Migrations, `on_order` Draft Exclusion, Partial GRN Match & Overdue Fixes
- [x] **Slice P3-2:** 90-Day Causal Seed Simulator (`Jul–Sep`, 0 Open POs), Daily Burn EWMA ($\alpha=0.3$), Clock Rollover & Cycle Analytics
- [x] **Slice P3-3:** Alerting Overhaul (`Edit PO`, Non-Resolving `Reject`, `DISMISSED` Hiding + Inventory Warning) & `ROP/T` Sweep
- [x] **Slice P3-4:** Lex Resilience (Retries/Fallbacks), Multimodal Image Triage & Enforced Email Formatting
- [x] **Slice P3-5:** Lex v2 Tool-Use Loop, AST-Sandboxed Calc Executor, Dynamic Reminders (`+4d` Deferral) & Memory Alerts
- [x] **Slice P3-6:** Frontend Core (IST Clock, Sidebar Cleanup, `FormattedEmailBody`, `InventoryPage` Revamp & `StockoutCycleDrawer`)
- [x] **Slice P3-7:** Frontend `CreateEditPOModal` (Suggested Qty + Reasoning), `AlertsPage`, `SuppliersPlanningPage` & `AIHealthPage` Traces
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

## Completed Slice Summary — `P3-3`

- **Files Added:**
  - `tests/orchestrator/test_alerts_phase3.py` (6 tests covering `PO_DISPATCH_APPROVAL` `edit_po` CTA & `POST /api/pos/{po_id}/dispatch-edited`, non-resolving `reject_lex_draft`, `DISMISSED` draft rejection & `draft_preview = None`, `PartSnapshot.unresolved_dismissed_alerts` + auto-resolution of `dismiss_resolved_at`, `/reopen` & `/acknowledge-dismissed`, and `ROP/T` alert naming).
- **Files Modified:**
  - `backend/po/actions.py`, `backend/po/__init__.py` (`dismiss_action_item` rejects pending `DRAFT_PENDING_APPROVAL` linked comm and resets `dismiss_resolved_at = None`; added `reopen_action_item` and `acknowledge_dismissed_action_item`).
  - `backend/inventory/schemas.py`, `backend/inventory/service.py` (added `UnresolvedDismissedAlert` and `PartSnapshot.unresolved_dismissed_alerts`; `_evaluate_unresolved_dismissed_alerts` surfaces unresolved dismissed alerts and auto-sets `dismiss_resolved_at` when underlying PO/stock/quarantine conditions resolve).
  - `backend/orchestrator/workflows.py` (replaced `reject_draft` with `edit_po` on `PO_DISPATCH_APPROVAL` alerts; added `dispatch_edited_po` + `edit_po` CTA handler; made `reject_lex_draft` non-resolving; updated `ROP_RECOMMENDATION` alert titles/labels to `ROP/T` / `T`).
  - `backend/orchestrator/routes/alerts.py`, `backend/orchestrator/routes/pos.py`, `backend/orchestrator/routes/parts.py` (hid `draft_preview` on `DISMISSED` alerts & exposed `dismiss_resolved_at`; added `POST /api/alerts/{id}/reopen`, `POST /api/alerts/{id}/acknowledge-dismissed`, and `POST /api/pos/{po_id}/dispatch-edited`; committed auto-resolved `dismiss_resolved_at` updates on `GET /api/parts` & `GET /api/parts/{part_id}`).
- **Verification:** `uv run pytest -v` (`110 passed, 3 skipped` — 3 live-Gemini tests skipped offline) and `uv run ruff check backend tests` (`All checks passed!`), including all 5 AST checks in `tests/test_architecture.py`. `frontend/`, `backend/agents/`, `backend/seed/data.py`, and `backend/mailroom/` left untouched.

---

## Completed Slice Summary — `P3-4`

- **Files Added:**
  - `tests/agents/test_lex_formatting_and_resilience.py` (6 tests covering `GeminiLLMClient` exponential-backoff retries on transient errors, fail-fast on non-transient errors, multimodal `image_attachments` + `pdf_bytes` part construction, `LexAgent.draft_email()` structured Markdown/PO#/SKU/currency/sign-off enforcement, `additional_po_updates` `PRICE_CHANGE` guardrail, and `WorkflowEngine.process_inbound_supplier_email()` image passing + graceful `AMBIGUOUS_EMAIL` fallback on LLM failure).
- **Files Modified:**
  - `backend/agents/llm_client.py` (added `image_attachments: list[tuple[str, bytes]] | None = None` to `LLMClientProtocol.generate_structured` & `GeminiLLMClient.generate_structured`; added configurable `timeout_seconds`, `max_retries`, `backoff_base_seconds`, `HttpOptions(timeout=...)`, and exponential-backoff retry loop on transient API/network errors).
  - `backend/agents/lex/prompts.py` (upgraded `TRIAGE_SYSTEM_PROMPT` for multimodal image inspection and `DRAFT_SYSTEM_PROMPT` with mandatory business-email Markdown structure, `\n\n` paragraphs, `**bold**` PO#/SKU/currency, and standard Lex sign-off).
  - `backend/agents/lex/agent.py` (added `image_attachments` support in `triage_email()`, closed `additional_po_updates` `PRICE_CHANGE` guardrail gap, and added `_format_and_verify_email_body()` deterministic normalization/grounding in `draft_email()`).
  - `backend/agents/lex/eval_adapter.py` (added `image_attachments` parameter to `OfflineCaseLLMClient.generate_structured`).
  - `backend/orchestrator/workflows.py` (reads `.jpg`/`.jpeg`/`.png` image attachments into `(mime_type, bytes)` for `triage_email()`, and falls back gracefully to `AMBIGUOUS_EMAIL` via `_build_llm_failure_triage_fallback()` on LLM failure with zero `500` crashes or PO state mutations).
  - `backend/evals/lex.yaml` (added structured formatting assertions on outbound draft cases and added `draft_po_dispatch_structured_markdown`).
- **Verification:** `uv run pytest -v` (`116 passed, 3 skipped` — 3 live-Gemini tests skipped offline) and `uv run ruff check backend tests` (`All checks passed!`), including all 5 AST checks in `tests/test_architecture.py`. `frontend/`, `backend/inventory/`, `backend/planning/`, `backend/po/`, and `backend/seed/data.py` left untouched.

---

## Completed Slice Summary — `P3-5`

- **Files Added:**
  - `backend/agents/tools.py` (`ToolDefinition` / `ToolSpec`, `ToolResult`, `ToolHandler`, `ToolRegistry` — domain-agnostic tool registration, JSON schema definitions, execution dispatch, and compact summary formatting).
  - `backend/agents/sandbox.py` (`run_sandbox_python`, `validate_sandbox_ast`, `SandboxSecurityError`, `SandboxTimeoutError`, `SandboxRuntimeError` — AST-validated + `sys.settrace` step/wall-clock bounded safe Python calculation sandbox allowing `math`, `statistics`, `datetime`/`timedelta`, `decimal`, `json` while blocking unsafe imports, filesystem/network/eval/dunder escapes, wall-clock `.now()`/`.utcnow()`/`.today()`, and resource-exhaustion literals).
  - `backend/orchestrator/lex_tools.py` (`LexToolTracker`, `build_lex_tool_registry` binding all 10 DB/advisory tools: `get_part_snapshot`, `get_consumption_history`, `get_po_details`, `list_open_pos`, `get_supplier_profile`, `get_mailroom_thread`, `run_sandbox_calc`, `schedule_lex_reminder`, `propose_memory_note`, `create_advisory_alert`; plus `propose_and_save_memory_note` Jaccard/substring deduplication & consolidation, `schedule_po_lex_reminder`, `cancel_pending_lex_reminders_for_po`, `create_grounded_advisory_alert`, and `evaluate_memory_advisory_alerts`).
  - `tests/agents/test_sandbox_and_tools.py` (16 parameterized/unit tests covering `ToolRegistry`, safe sandbox calculations, AST/security/timeout/resource-limit blocking, and `GeminiLLMClient.generate_with_tools` multi-turn function calling + `AgentTracer.span.tool_calls` logging).
  - `tests/orchestrator/test_lex_v2_workflows.py` (6 integration tests covering all 10 `lex_tools`, variable `+4d` deferral saving `LexReminder(PENDING)` + `DISPATCH_DETAILS_DEFERRED` without premature draft, day-4 reminder wakeup drafting only when details remain missing and auto-cancelling when details or GRN arrive, tight-runway `HIGH` `DISPATCH_DEFERRAL_STOCKOUT_RISK` escalation, and memory note deduplication + grounded pre-delivery invoice-mismatch advisories).
  - `tests/agents/test_live_gemini_phase3.py` (5 live `gemini-3.5-flash-lite` integration tests covering `generate_with_tools` + sandbox calc + `+4d` `LexReminder` deferral, `LexReminder` wakeup missing-vs-GRN, multimodal PNG dispatch-slip vision extraction, structured Markdown/INR drafting, and `mode="live"` Phase 3 `lex.yaml` eval cases).
- **Files Modified:**
  - `backend/agents/llm_client.py` (added `tool_calls` on `LLMResult`; implemented `LLMClientProtocol.generate_with_tools` and `GeminiLLMClient.generate_with_tools` using `AutomaticFunctionCallingConfig(disable=True)`, `Part.from_function_response`, cumulative token accounting, retry loop, and final schema-enforced synthesis fallback).
  - `backend/agents/lex/schemas.py`, `backend/agents/lex/prompts.py`, `backend/agents/lex/agent.py`, `backend/agents/lex/__init__.py` (added `details_promised_days`, `ReminderWakeupInput`, `ReminderWakeupResult`, `REMINDER_WAKEUP_SYSTEM_PROMPT`, tool-use guidance in `TRIAGE_SYSTEM_PROMPT`, `tool_registry` + `inspect_reminder_wakeup()`, plain-text `subject` / collapsed `****` asterisk hardening + non-prefixed SKU example in `DRAFT_SYSTEM_PROMPT`).
  - `backend/agents/lex/eval_adapter.py` & `backend/evals/lex.yaml` (added `OfflineCaseLLMClient.generate_with_tools`, `tool_registry_factory` on `LexEvalExecutor` with synthetic YAML fixture isolation for `open_pos`/`part_snapshot`/`po_status` cases so live evals don't collide with seeded historical `CLOSED` POs, `reminder_wakeup` eval task, `tool_calls` output from latest `AgentTrace`, and 3 Phase 3 eval cases).
  - `backend/po/logistics.py` (added optional `details_due_at` override to `apply_po_logistics_updates`).
  - `backend/orchestrator/logistics_workflow.py` (added `evaluate_deferral_safety`, `resolve_deferral_schedule`, variable N-day deferral honoring `details_promised_by_date`/`details_promised_days` with 2-day fallback when unspecified, `DISPATCH_DETAILS_DEFERRED` vs `DISPATCH_DEFERRAL_STOCKOUT_RISK` alerts, contextualized `create_supplier_memory_alert`, and pre-delivery memory advisories).
  - `backend/orchestrator/sla_watcher.py` (added `_process_due_lex_reminders` waking Lex when `LexReminder.remind_at <= sim_now`, suppressing duplicate legacy `DEFERRED_2D` checks via `handled_deferred_po_ids`, dynamic wait-days messages, and pre-delivery memory checks).
  - `backend/orchestrator/workflows.py` & `backend/orchestrator/routes/observability.py` (wired `build_lex_tool_registry` + `LexToolTracker`, delegated learned memory deduplication, propagated `details_promised_days` across multi-PO triage updates, cancelled pending reminders on GRN/cancel/backup-switch, evaluated `po_created` memory advisories, passed `eval_sim_now` in `settle_daily_burn_on_clock_advance`, and passed `tool_registry_factory` to `LexEvalExecutor`).
  - `tests/orchestrator/test_logistics_and_escalation.py` & `tests/agents/test_live_gemini_stock_request.py` (aligned stale `"2026-12-03"`/`"2026-11-22"` dates and stale `BRG-402 on_hand_stock == 4` live shortage setup to Phase 3 `Oct 1` seed baseline).
- **Verification:** `RUN_LIVE_GEMINI=1 uv run pytest -v` (`146 passed in 132.73s`, 0 skipped — all 138 offline tests + all 8 live `gemini-3.5-flash-lite` tests passed) and `uv run ruff check backend tests` (`All checks passed!`), including all 5 AST checks in `tests/test_architecture.py`. `frontend/`, `backend/inventory/service.py`, `backend/planning/*`, and `backend/seed/data.py` left untouched.

---

## Completed Slice Summary — `P3-6`

- **Files Added:**
  - `frontend/src/components/FormattedEmailBody.tsx` (safe React Markdown-lite renderer supporting blank-line separated paragraphs, `- ` / bullet lists, and inline `**bold**` spans while stripping any stray `**` markers).
  - `frontend/src/components/StockoutCycleDrawer.tsx` (portaled slide-over analytics drawer backed by `GET /api/parts/{part_id}/cycle-analytics` and `GET /api/parts/{part_id}/po-suggestion`; renders 90-day SVG stock trajectory + consumption bars + projection line + `STOCKOUT`/`LOW_STOCK`/`PO_PLACED`/`REPLENISHMENT` markers with actual LT, predicted stockout date in IST, primary effective LT + communication delay buffer, `EWMA α = 0.30` note, and recommended PO quantity + reasoning).
- **Files Modified:**
  - `frontend/src/utils/format.ts` (`formatSimDateTime` and `formatSimDate` updated to `Asia/Kolkata` / `IST`; added `formatBurnRate` and `stockoutUrgencyBadgeClass`).
  - `frontend/src/api/types.ts` & `frontend/src/api/client.ts` (added Phase 3 `PartSnapshot`/`PartItem` burn/runway/active-PO/`unresolved_dismissed_alerts` types, `CycleAnalyticsResult`, `POSuggestionResult`, `AlertItem.dismiss_resolved_at`, and `getPartCycleAnalytics`, `getPartPoSuggestion`, `reopenAlert`, `acknowledgeDismissedAlert` helpers).
  - `frontend/src/components/ClockBar.tsx` & `frontend/src/App.tsx` (removed duplicate `Actionable Alerts` entry from sidebar `NAV_ITEMS`, defaulted `activeTab` to `'inventory'`, highlighted `Open Alerts` pill in `ClockBar` when open alerts exist or Alerts view is active, and wired `onNavigatePo` into `InventoryPage`).
  - `frontend/src/pages/MailroomPage.tsx` & `frontend/src/pages/POTrackerPage.tsx` (wired `FormattedEmailBody` into message threads and PO linked email threads while keeping editable draft `<textarea>` fields as plain Markdown).
  - `frontend/src/pages/InventoryPage.tsx` (updated table columns to `On Hand`, `On Hold`, `On Order`, `Inventory Position`, `Daily Burn` with neutral-grey trend %, clickable `Stockout In` urgency badge, and `ROP/T` without `30d cov`; removed `Demand Override`, `Edit ROP/S`, and `Demand` actions/modals; dynamically swapped `[Create PO]` ↔ `[View PO]` jumping straight into `POTrackerPage` PO drawer; added `unresolved_dismissed_alerts` warning badge with portaled interactive `[Reopen Alert]` / `[Acknowledge]` popover; preserved existing `[Create PO]` flow until `P3-7`).
- **Verification:** `cd frontend && npm run build` (`tsc -b && vite build`, `✓ built in 1.73s`, 0 TypeScript/Vite errors); `uv run pytest` (`138 passed, 8 skipped` — 8 live-Gemini tests skipped offline) and `uv run ruff check backend tests` (`All checks passed!`). `backend/`, `SuppliersPlanningPage.tsx`, `AIHealthPage.tsx`, and `AlertsPage.tsx` left untouched.

---

## Completed Slice Summary — `P3-7`

- **Files Added:**
  - `frontend/src/components/CreateEditPOModal.tsx` (portaled shared modal supporting `mode="create"` and `mode="edit"`: dynamically queries `GET /api/parts/{part_id}/po-suggestion?supplier_id=...` on open and supplier switch, renders the **Suggestion & Reasoning** card with `Inventory Position`, `Daily Burn (EWMA α=0.30)`, `Effective LT + Comm Buffer`, `MOQ`, `Safety Stock`, `Target Stock T (ROP)`, and plain-English `reasoning`; pre-fills suggested qty in `create` mode and offers 1-click **"Use Suggested Qty"** without clobbering user edits in `edit` mode; generates/regenerates Lex drafts via `POST /api/pos/draft-manual`; dispatches new POs via `POST /api/pos/manual` or edits & dispatches `PENDING_DISPATCH_APPROVAL` POs via `POST /api/pos/{po_id}/dispatch-edited`).
- **Files Modified:**
  - `frontend/src/api/types.ts` & `frontend/src/api/client.ts` (added `DispatchEditedPORequest`, `DispatchEditedPOResponse`, `api.dispatchEditedPo()`, and `avg_response_days?` / `cycles_90d?` on `SupplierItem` and `SupplierScoreResult`).
  - `frontend/src/pages/InventoryPage.tsx` (replaced inline `create_po` modal and state with `<CreateEditPOModal mode="create" ... />`, preserving `withdraw` modal and `StockoutCycleDrawer` integration).
  - `frontend/src/pages/POTrackerPage.tsx` (added `[Edit & Dispatch PO]` banner & button in the slide-over drawer when `poDetail.status === 'PENDING_DISPATCH_APPROVAL'`, wired to `<CreateEditPOModal mode="edit" ... />`).
  - `frontend/src/pages/AlertsPage.tsx` (handled `edit_po` CTA directly via `<CreateEditPOModal mode="edit" ... />` without premature `executeAlertCta`, filtered `[Reject Draft]` defensively on `PO_DISPATCH_APPROVAL`, added defensive UI guard hiding `draft_preview` on `DISMISSED` alerts, rendered read-only draft bodies through `FormattedEmailBody`, and replaced all `ROP/S` strings with `ROP/T` / `T`).
  - `frontend/src/pages/SuppliersPlanningPage.tsx` (added `Avg Response` in days and `Cycles (90d)` tiles on supplier scorecards, added explicit `EWMA α = 0.30` note in the page subtitle and Dynamic `ROP/T` header, and replaced all `ROP/S` / `Order-Up-To (S)` / `S=` labels with `ROP/T` / `Order-Up-To (T)` / `T=`).
  - `frontend/src/pages/AIHealthPage.tsx` (added tool-call count badge on `AgentTrace` header rows and structured **Tool Execution Steps** cards inside expanded traces rendering tool name, step index, duration, `ERROR` badge, JSON `Args`, and `Result Summary` / error details).
- **Verification:** Global `rg` confirmed zero remaining `ROP/S` / `ROP / S` / `rop_s` / `Order-Up-To (S)` strings in `frontend/src/`; `cd frontend && npm run build` (`tsc -b && vite build`, `✓ built in 1.69s`, 0 TypeScript/Vite errors); `uv run pytest -q` (`138 passed, 8 skipped` — 8 live-Gemini tests skipped offline); `uv run ruff check backend tests` (`All checks passed!`). `backend/`, `format.ts`, `ClockBar.tsx`, `StockoutCycleDrawer.tsx`, and `MailroomPage.tsx` left untouched.

---

## Next Session Handoff Prompt — Start Slice `P3-8`

> Execute **Slice P3-8** (Final Phase 3 Verification & Documentation Parity Gate) per `docs/phase3/SLICES_P3.md`, `docs/phase3/PRD_P3.md §3`, and `AGENTS.md §5`:
> - **Read:** `AGENTS.md`, `docs/phase3/STATUS_P3.md`, `docs/phase3/PRD_P3.md (§3 Acceptance Criteria)`, `docs/phase3/DESIGN_P3.md`, `docs/phase3/SLICES_P3.md (Slice P3-8)`, `STATUS.md`, and `README.md`.
> - **Scope:** Verification & documentation parity only (fix any genuine E2E/live-eval bug discovered during verification if needed, keeping changes minimal and re-running full checks). Leave untracked `demo_recordings/` unstaged.
> - **Acceptance Criteria:**
>   1. Run full offline `uv run pytest -v`, `uv run ruff check backend tests`, and `cd frontend && npm run build`.
>   2. Run live Gemini verification (`RUN_LIVE_GEMINI=1 uv run pytest -v tests/agents/`) and execute the `lex` eval suite in `mode: "live"` via `POST /api/evals/lex/run`, verifying 100% pass rate.
>   3. Run a full headless Playwright/Chromium browser walkthrough against the running backend + frontend across all 6 acceptance criteria in `PRD_P3.md §3` (Oct 1 13:00 IST seed & 0 open POs, same-day withdraw vs `+1d` EWMA rollover & `ROP/T` freeze/recalc invariants, `Stockout In` urgency badges & 90d cycle drawer, `[Create PO]` suggested qty + `[View PO]` jump, `[Edit PO]` modal from Alerts + `DISMISSED` draft hiding & inventory warning badge, formatted emails & Lex tool-call traces in `AIHealthPage`).
>   4. Sync `docs/phase3/STATUS_P3.md`, `STATUS.md`, and `README.md` with exact verified outputs and commit cleanly.

---

## Plan vs. Implementation Discrepancies
*(Per `AGENTS.md §5`, document any discrepancy between `PRD_P3.md` / `DESIGN_P3.md` and actual implementation with proof and resolution here.)*

| Slice | Original Plan Reference | Discrepancy / Proof | Resolution Implemented |
|---|---|---|---|
| Baseline Audit | `PRD_P2.md §4.2` vs `HEAD (099474f)` | Post-Phase-2 commit `25a8bdc` re-introduced `Withdraw`, `Create PO`, `ROP/S`, and `Demand` actions in `InventoryPage.tsx`, and pre-existing `get_on_order_stock()` still counted `PENDING_DISPATCH_APPROVAL` while `matching.py` still enforced `invoice_qty == po_qty`. | Reconciled in `PRD_P3.md §1.1` and scheduled explicitly in `P3-1`–`P3-7`. |
| `P3-2` | `DESIGN_P3.md §3.2` vs `SuppliersPlanningPage.tsx:522` | `frontend/` is untouched until `P3-6`/`P3-7`, and `SuppliersPlanningPage.tsx` still reads `rec.forecast.monthly_buckets`. | Kept `DemandForecastResult.monthly_buckets` populated in `backend/planning/forecast.py` while delegating daily burn, $\sigma_D$, intermittent floor, and `ROP/T` calculations to 90-day daily EWMA ($\alpha=0.3$) in `ewma_daily.py`. |
| `P3-5` | `DESIGN_P3.md §2.1` & `§5.2` vs `DispatchDetailsStatus.DEFERRED_2D` | `DispatchDetailsStatus.DEFERRED_2D = "DEFERRED_2D"` is asserted directly by Phase 1/2 tests (`tests/po/test_logistics.py`) while variable N-day deferrals (`+4d`, etc.) require flexible due timestamps without double-firing legacy 2-day SLA nudges. | Preserved `DEFERRED_2D` as the status enum, added optional `details_due_at` on `apply_po_logistics_updates()`, persisted the exact deadline on `PO.dispatch_details_due_at` & `LexReminder.remind_at`, and suppressed duplicate legacy checks via `handled_deferred_po_ids` in `sla_watcher.py`. |
| `P3-5` | `PRD_P3.md §2` pre-delivery memory advisories vs `sup3` price-hike seed note | Broad `"price"` keyword matching in `pre_delivery` invoice watch falsely matched `sup3`'s `"prone to mid-quarter price hikes"` note on Pre-ETA, breaking `test_mandatory_memory_note_alert_and_auto_resolve_preserves_notifications` (expected 1 auto-resolved alert, got 2). | Narrowed `pre_delivery` invoice-mismatch watch triggers to explicit invoice/PDF billing keywords while evaluating general price/reliability risks contextually at `po_created` for critical parts. |

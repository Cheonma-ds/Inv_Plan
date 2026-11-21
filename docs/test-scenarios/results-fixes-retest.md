# InvPlan PRD Fixes Live Re-Verification Report (`F-1.1`–`F-4.3`)

- **Execution Date:** 2026-11-21 (Simulated Clock start: `2026-11-20T09:00:00Z`)
- **Model:** Live `gemini-3.5-flash-lite` via `google-genai` (`$GEMINI_API_KEY`)
- **Isolated Environment:** `DATABASE_URL=sqlite:///data/test_runs/scn_fixes.db`, `ATTACHMENTS_DIR=data/test_runs/attachments` (FastAPI on `:8002`; production `data/inv_plan.db` untouched at `434,176` bytes)
- **Live 16-Scenario Retest Totals:** **61 Gemini calls** | **109,519 tokens** (`93,735` input + `15,784` output) | **₹0.9883 INR** total cost
- **Scorecard Summary:** **16 / 16 `PASS` (100%)**, **0 `PARTIAL / GAP`**, **0 `FAIL`**

---

## 1. Offline & Live Quality Gates

- **Lint:** `uv run ruff check backend tests` → `All checks passed!`
- **Offline Test Suite:** `uv run pytest -q` → `92 passed, 3 skipped in 10.92s` (includes `tests/test_architecture.py`, `tests/core/test_migrations.py`, and `tests/orchestrator/test_prd_fixes.py`)
- **Frontend Build:** `cd frontend && npm run build` → `✓ built in 1.53s`
- **Live Gemini Pytest Suite:** `RUN_LIVE_GEMINI=1 uv run pytest -v tests/agents/` → `9 passed in 13.02s`
- **Live YAML Eval Suite (`POST /api/evals/lex/run`, `mode="live"`):** `29/30 passed (96.7%)` — all 4 new PRD-fix cases (`triage_foreign_currency_usd_quote`, `triage_multi_po_delay_splitting`, `draft_po_dispatch_inr_currency_compliance`, `stock_request_csv_bom_multi_sku`) passed; the single failing assertion was `triage_unmatched_po_guardrail` expecting the deterministic `[Guardrail Escalation]` prefix when `gemini-3.5-flash-lite` had already independently classified `PO-9999` as `AMBIGUOUS`.

---

## 2. Re-Verification Scorecard (16 Affected Scenarios)

| Scenario | Fixes Verified | Prior Baseline | Current Status | Gemini Calls | Tokens (`in+out`) | Cost (INR) | Observed Result |
|---|---|---|---|---:|---:|---:|---|
| `SCN-01` | `F-4.2`, `F-1.4` | `PASS` (`+₹330` impact) | `PASS` | 2 | 3,291 (`2,762+529`) | ₹0.0307 | `price_only_impact_inr=330.0`, `total_cost_impact_inr=830.0`, `"830"` in alert summary, INR-clean `REVISED_PO` draft (`APPROVED_EDITED`). |
| `SCN-03` | `F-4.1` | `PARTIAL / GAP` | `PASS` | 2 | 3,266 (`2,779+487`) | ₹0.0298 | Hedged reply escalated cleanly as `AMBIGUOUS` (`PO-1038` unchanged), generated clarification draft (`draft_comm_id=2`), reject tracked `rejected_count=1`. |
| `SCN-06` | `F-2.2`, `F-1.4` | `PARTIAL / GAP` | `PASS` | 4 | 7,390 (`6,400+990`) | ₹0.0653 | Post-ack price hike transitioned `ACKNOWLEDGED → EXCEPTION_REVIEW` (preserving `acknowledged_at`), `update_po` → `PENDING_DISPATCH_APPROVAL` + `REVISED_PO` draft (`id=4`). |
| `SCN-08` | `F-2.1`, `F-3.2` | `PARTIAL / GAP` | `PASS` | 2 | 3,766 (`3,306+460`) | ₹0.0324 | Multimodal triage extracted `proposed_currency="USD"` from quote PDF; `CURRENCY_MISMATCH` alert blocked blind `update_po` (`400`), kept `₹100.0`, drafted `request_inr_quote`. |
| `SCN-11` | `F-1.3`, `F-1.4` | `PARTIAL / GAP` | `PASS` | 5 | 8,279 (`6,953+1,326`) | ₹0.0772 | `EXPEDITE_REQUEST` draft included `cc_personas=['inventory_manager', 'procurement']`, `PO` transitioned to `DELAYED_CRITICAL` with `revised_eta`, zero `USD`/`$`. |
| `SCN-12` | `F-2.3` | `PASS` (escalated) | `PASS` | 5 | 9,071 (`7,424+1,647`) | ₹0.0883 | Single multi-PO delay email split cleanly across `PO-1041` (`DELAYED_CRITICAL`) and `PO-1039` (`ACKNOWLEDGED`, `revised_eta=2026-12-05T09:00:00+00:00`). |
| `SCN-14` | `F-4.3` | `PASS` (`17:00Z` offset) | `PASS` | 4 | 6,084 (`5,188+896`) | ₹0.0553 | Date-only ETA normalized to `2026-11-22T09:00:00+00:00`; clock `+3d 2h` reliably triggered `OVERDUE` (`ETA + 24h`) and Procurement escalation. |
| `SCN-19` | `F-3.2`, `F-1.4` | `PARTIAL / GAP` | `PASS` | 6 | 12,113 (`10,634+1,479`) | ₹0.1043 | Multimodal dispatch advice PDF triage extracted `carrier_name="SafeExpress"` + `actual_dispatch_date`; follow-up AWB `SE-901122` completed logistics. |
| `SCN-28` | `F-2.3` | `PASS` (generic review) | `PASS` | 6 | 11,888 (`9,936+1,952`) | ₹0.1118 | Consolidated PDF (`inv_sup1_consolidated_2pos.pdf`) raised `INVOICE_EXTRACTION_REVIEW` listing `referenced_po_numbers=['PO-1041', 'PO-1042']`; both POs left untouched. |
| `SCN-30` | `F-3.1` | `PARTIAL / GAP` | `PASS` | 6 | 11,088 (`9,639+1,449`) | ₹0.0972 | Multi-PDF email (`inv_sup2_overbilled_v1.pdf` + `cn_sup2_credit_note.pdf`) raised `INVOICE_WITH_CREDIT_NOTE_REVIEW` (`net_amount=7500.0`); `approve_net_settlement` closed PO & released 5 units. |
| `SCN-31` | `F-1.1` | `FAIL` (`BUG-01`) | `PASS` | 6 | 10,158 (`8,791+1,367`) | ₹0.0898 | `DAMAGED` GRN held 12 units in `quarantined_stock` across clean `MATCHED` invoice, raised `DAMAGED_GOODS_HOLD`, drafted claim (`draft_damage_claim`), and released `good_qty=8` on Manager CTA. |
| `SCN-40` | `F-3.3` | `PARTIAL / GAP` | `PASS` | 1 | 1,724 (`1,424+300`) | ₹0.0165 | `making` `.csv` BOM attachment (`making_bom_line3.csv`) created 2 pending `StockRequest` items (`[1, 2]`) with zero premature stock deduction prior to approval. |
| `SCN-44` | `F-3.1` | `PARTIAL / GAP` | `PASS` | 7 | 12,569 (`10,901+1,668`) | ₹0.1107 | Multi-PDF email (overbilled Invoice + Packing List) quarantined 90 units (`quarantined_stock=90`) and surfaced `packing_list_conflict=True` in mismatch alert & draft. |
| `SCN-45` | `F-3.3` | `PARTIAL / GAP` | `PASS` | 1 | 2,212 (`1,895+317`) | ₹0.0199 | Supplier `.jpg` photo attachment stored cleanly in Mailroom without `400`/`500` and surfaced `HIGH` `PO_EXCEPTION` inspection alert. |
| `SCN-46` | `F-1.2`, `F-3.3` | `FAIL` (`BUG-02`) | `PASS` | 2 | 3,370 (`2,938+432`) | ₹0.0294 | Encrypted `locked_invoice.pdf` returned `200`, stored email, and raised `HIGH` `INVOICE_EXTRACTION_REVIEW`; `.xlsx`, `.zip`, and 0-byte attachments still returned `400`. |
| `SCN-47` | `F-1.4` | `PASS` (`USD` in draft) | `PASS` | 2 | 3,250 (`2,765+485`) | ₹0.0296 | Re-triggered `PO_DISPATCH` draft grounded monetary amounts strictly in `INR`/`₹` with zero `USD`/`$` hallucinations. |

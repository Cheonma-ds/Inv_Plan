# InvPlan Batch 2 Live Execution Report (`SCN-21`–`SCN-50`)

- **Execution Date:** 2026-11-21 (Simulated Clock start: `2026-11-20T09:00:00Z`)
- **Model:** Live `gemini-3.5-flash-lite` via `google-genai` (`$GEMINI_API_KEY`)
- **Isolated Environment:** `DATABASE_URL=sqlite:///data/test_runs/scn_batch2.db`, `ATTACHMENTS_DIR=data/test_runs/attachments` (FastAPI on `:8001`; production `data/inv_plan.db` untouched at `434,176` bytes)
- **Batch 2 Totals (`SCN-21`–`SCN-50`):** **130 Gemini calls** | **187,896 tokens** (`159,348` input + `28,548` output) | **₹1.7233 INR** total cost
- **Combined Suite Totals (`SCN-01`–`SCN-50`):** **205 Gemini calls** | **291,208 tokens** | **₹2.6831 INR**
- **Batch 2 Scorecard Summary:** **24 `PASS`**, **4 `PARTIAL / GAP`**, **2 `FAIL`**

---

## 1. Batch 2 Summary & Scorecard

| Scenario | Status | Scope Tag | Gemini Calls | Total Tokens (`in+out`) | Total Cost (INR) | Trace JSON | 1-Line Outcome Summary |
|---|---|---|---|---:|---:|---|---|
| `SCN-21` | `PASS` | `IN-SCOPE` | 1 | 974 (`791+183`) | ₹0.0096 | [`scn-21.json`](traces/batch2/scn-21.json) | `inventory_manager` outbound & non-CC `sup1` reply triggered 0 false acks/traces. |
| `SCN-22` | `PASS` | `IN-SCOPE` | 4 | 6,028 (`5,097+931`) | ₹0.0556 | [`scn-22.json`](traces/batch2/scn-22.json) | Clean PDF (`APX-2026-881`, 18% IGST) → `MATCHED`, `CLOSED`, `on_hand: 2→12`. |
| `SCN-23` | `PASS` | `IN-SCOPE` | 6 | 8,108 (`6,818+1,290`) | ₹0.0755 | [`scn-23.json`](traces/batch2/scn-23.json) | Early invoice (`INVOICE_PENDING_GRN`) → short `GRN=18` → `QUARANTINED_INVOICE_MISMATCH`. |
| `SCN-24` | `PASS` | `IN-SCOPE` | 8 | 12,318 (`10,563+1,755`) | ₹0.1108 | [`scn-24.json`](traces/batch2/scn-24.json) | `v1` (`₹150`) quarantined `100` → `v2` (`₹100`) released `100` (`on_hand: 14→114`), `CLOSED`. |
| `SCN-25` | `PASS` | `IN-SCOPE` | 10 | 16,448 (`14,141+2,307`) | ₹0.1472 | [`scn-25.json`](traces/batch2/scn-25.json) | `v1` (`qty 40`) & `v2` (`₹201.20`) failed without double-quarantine; `v3` (`₹200.25`) passed. |
| `SCN-26` | `PASS` | `IN-SCOPE` | 3 | 4,252 (`3,522+730`) | ₹0.0406 | [`scn-26.json`](traces/batch2/scn-26.json) | Caught hidden `₹1,200` freight line on `PO-1040` (`MISMATCH_BOTH`), quarantined `10`. |
| `SCN-27` | `PASS` | `IN-SCOPE` | 4 | 6,444 (`5,526+918`) | ₹0.0579 | [`scn-27.json`](traces/batch2/scn-27.json) | Skewed/smudged scan triggered `conf=0.80 < 0.85` → `INVOICE_EXTRACTION_REVIEW`, 0 mutation. |
| `SCN-28` | `PASS` | `PARTIALLY IN-SCOPE` | 6 | 9,020 (`7,522+1,498`) | ₹0.0851 | [`scn-28.json`](traces/batch2/scn-28.json) | Consolidated 2-PO PDF escalated cleanly (`po_number=None`), 0 corrupt mutations. |
| `SCN-29` | `PASS` | `IN-SCOPE` | 4 | 6,293 (`5,268+1,025`) | ₹0.0590 | [`scn-29.json`](traces/batch2/scn-29.json) | Resolved `PO-1041` from PDF, capped `conf=0.50` for blank `invoice_number` → `HIGH` alert. |
| `SCN-30` | `PARTIAL / GAP` | `PARTIALLY IN-SCOPE` | 5 | 7,977 (`6,763+1,214`) | ₹0.0732 | [`scn-30.json`](traces/batch2/scn-30.json) | Single-attachment API kept 2nd PDF (`CN-BF-109`); PO quarantined safely (no silent close). |
| `SCN-31` | `FAIL` | `PARTIALLY IN-SCOPE` | 4 | 5,999 (`5,046+953`) | ₹0.0558 | [`scn-31.json`](traces/batch2/scn-31.json) | **Bug:** Clean invoice overrode `GRN condition=DAMAGED` (`HELD_QUARANTINE`) & released 12 damaged units! |
| `SCN-32` | `PASS` | `IN-SCOPE` | 8 | 10,658 (`9,026+1,632`) | ₹0.0980 | [`scn-32.json`](traces/batch2/scn-32.json) | Quarantine 48h reminder & 96h `CRITICAL` escalation fired; Procurement escalated. |
| `SCN-33` | `PASS` | `IN-SCOPE` | 2 | 2,479 (`2,104+375`) | ₹0.0227 | [`scn-33.json`](traces/batch2/scn-33.json) | Making request held at `on_hand=4`; Manager approval issued `2` & dispatched `PO-1041` (`4`). |
| `SCN-34` | `PASS` | `IN-SCOPE` | 4 | 5,407 (`4,612+795`) | ₹0.0491 | [`scn-34.json`](traces/batch2/scn-34.json) | Shortage → edited min-qty inquiry (`APPROVED_EDITED`) → Making `2` pcs → `on_hand: 2→0`. |
| `SCN-35` | `PASS` | `IN-SCOPE` | 7 | 10,041 (`8,553+1,488`) | ₹0.0914 | [`scn-35.json`](traces/batch2/scn-35.json) | Surfaced `0 on-hand (10 quarantined)` & blocked issuance CTAs cleanly. |
| `SCN-36` | `PASS` | `IN-SCOPE` | 4 | 4,524 (`3,764+760`) | ₹0.0429 | [`scn-36.json`](traces/batch2/scn-36.json) | Split 3-SKU Making email into 3 independent alerts (`S1`/`S2`/`S3`) & updated all balances. |
| `SCN-37` | `PASS` | `IN-SCOPE` | 3 | 3,970 (`3,389+581`) | ₹0.0360 | [`scn-37.json`](traces/batch2/scn-37.json) | Sequential race blocked Req #2 (`400: Cannot withdraw 3... only 0 on hand`), `on_hand=0`. |
| `SCN-38` | `PASS` | `IN-SCOPE` | 1 | 1,545 (`1,316+229`) | ₹0.0141 | [`scn-38.json`](traces/batch2/scn-38.json) | Vague `"5 or 6 bearings"` triggered `AMBIGUOUS_STOCK_REQUEST` (`HIGH`), 0 stock change. |
| `SCN-39` | `PASS` | `IN-SCOPE` | 3 | 4,342 (`3,661+681`) | ₹0.0402 | [`scn-39.json`](traces/batch2/scn-39.json) | `on_hand→0` with open `PO-1041` (`ETA T+9d`) fired `ACCELERATED_BURN` (`CRITICAL`), 0 dup PO. |
| `SCN-40` | `PARTIAL / GAP` | `PARTIALLY IN-SCOPE` | 0 | 0 | ₹0.0000 | [`scn-40.json`](traces/batch2/scn-40.json) | Missing `lex` CC blocked `400`; `.csv` BOM rejected `400` (`Only .pdf attachments supported`). |
| `SCN-41` | `PASS` | `IN-SCOPE` | 4 | 5,533 (`4,540+993`) | ₹0.0536 | [`scn-41.json`](traces/batch2/scn-41.json) | PDF (`45 × ₹130`) overrode lying body (`10 × ₹100`) → `MISMATCH_BOTH`, `APPROVED_EDITED`. |
| `SCN-42` | `PASS` | `IN-SCOPE` | 4 | 5,962 (`5,038+924`) | ₹0.0550 | [`scn-42.json`](traces/batch2/scn-42.json) | `PO-9999` wrong-customer PDF triggered PO conflict guardrail; `PO-1041` untouched (`0` quar). |
| `SCN-43` | `PASS` | `IN-SCOPE` | 1 | 1,677 (`1,413+264`) | ₹0.0156 | [`scn-43.json`](traces/batch2/scn-43.json) | `"Invoice attached"` with 0 files returned `200`, raised `INVOICE_MISSING_PDF` (`MEDIUM`). |
| `SCN-44` | `PARTIAL / GAP` | `PARTIALLY IN-SCOPE` | 5 | 7,580 (`6,498+1,082`) | ₹0.0682 | [`scn-44.json`](traces/batch2/scn-44.json) | `inv_sup2_100pcs.pdf` vs `GRN=90` quarantined `90` (`MISMATCH_QTY`); single-attachment API limit. |
| `SCN-45` | `PARTIAL / GAP` | `PARTIALLY IN-SCOPE` | 0 | 0 | ₹0.0000 | [`scn-45.json`](traces/batch2/scn-45.json) | `.jpg` photo rejected cleanly (`400: Only .pdf attachments are supported in MVP.`). |
| `SCN-46` | `FAIL` | `OUT-OF-SCOPE` / `IN-SCOPE` | 0 | 0 | ₹0.0000 | [`scn-46.json`](traces/batch2/scn-46.json) | **Bug:** `.xlsx`/`.zip`/`0-byte PDF` gave `400`, but `locked_invoice.pdf` threw **`500 Internal Server Error`**! |
| `SCN-47` | `PASS` | `IN-SCOPE` | 2 | 2,547 (`2,048+499`) | ₹0.0255 | [`scn-47.json`](traces/batch2/scn-47.json) | `SUPPLIER_MEMORY_LEARNED` → Manager edited note → `PO-1041` draft enforced price lock & dispatch date. |
| `SCN-48` | `PASS` | `IN-SCOPE` | 0 | 0 | ₹0.0000 | [`scn-48.json`](traces/batch2/scn-48.json) | Deterministic recalc (`0` LLM calls): `sup2` `56.0→38.4`, 15 `ROP_RECOMMENDATION`s, inline CTA applied. |
| `SCN-49` | `PASS` | `IN-SCOPE` | 6 | 8,748 (`7,525+1,223`) | ₹0.0782 | [`scn-49.json`](traces/batch2/scn-49.json) | Exact `1/0/0` (`PO_DISPATCH`), `0/1/0` (`DISPATCH_DETAILS_REQUEST`), `0/0/1` (`INVOICE_DISCREPANCY`). |
| `SCN-50` | `PASS` | `IN-SCOPE` | 21 | 29,022 (`24,804+4,218`) | ₹0.2626 | [`scn-50.json`](traces/batch2/scn-50.json) | Full 7-persona, 9-step cascade completed end-to-end (`PO-1041 CANCELLED`, `PO-1042 CLOSED`, `on_hand=3`). |

---

## 2. Discovered Bugs & Product/Scope Gaps

### Bugs (Critical / High)
1. **`GRN condition=DAMAGED` (`HELD_QUARANTINE`) is overwritten and released to usable `on_hand_stock` when a numerically clean invoice arrives (`SCN-31`):**
   - **Evidence:** Logging `GRN=12, condition="DAMAGED"` on `PO-1041` set `grn_status="HELD_QUARANTINE"` and `po_status="RECEIVED_PENDING_INVOICE"`. When `inv_sup1_damaged_shipment.pdf` (`12 × ₹450`) arrived, 3-way match returned `MATCHED` (`stock_action="RELEASE_DIRECT"`), closed `PO-1041` (`CLOSED`), and added all `12` damaged units to usable `on_hand_stock` (`2 → 14`, `quarantined_stock=0`).
   - **Impact:** Damaged goods enter usable shop-floor inventory without Manager clearance.
2. **Password-protected PDF (`locked_invoice.pdf`) causes unhandled `500 Internal Server Error` (`SCN-46`):**
   - **Evidence:** `locked_invoice.pdf` starts with `%PDF-1.4` (passing `save_pdf_attachment`), contains an `/Encrypt` dictionary, and caused `POST /api/mailroom/send` to crash with HTTP `500 Internal Server Error` instead of returning `400` or escalating to `INVOICE_EXTRACTION_REVIEW`.

### Confirmed Scope Gaps
1. **Mailroom `/api/mailroom/send` only accepts a single attachment (`SCN-30`, `SCN-44`):**
   - **Evidence:** `attachment: UploadFile | None = File(default=None)` only retains one uploaded file. In `SCN-30`, sending both `inv_sup2_overbilled_v1.pdf` and `cn_sup2_credit_note.pdf` kept only `cn_sup2_credit_note.pdf`.
2. **Non-PDF attachments (`.jpg`, `.csv`, `.xlsx`, `.zip`) rejected at upload (`400`) (`SCN-40`, `SCN-45`, `SCN-46`):**
   - **Evidence:** `save_pdf_attachment()` enforces `.pdf` only (`400: Only .pdf attachments are supported in MVP.`), preventing suppliers/Making from attaching `.jpg` damage photos or `.csv` BOMs to emails.

### Prompt / Guardrail Tuning Needed
1. **`PO_DISPATCH` draft occasionally hallucinates `USD` instead of `INR`/`₹` (`SCN-47`):**
   - **Evidence:** On `PO-1041` (`SNS-802`, `₹600.00`), Lex wrote `"600.00 USD, totaling 5400.00 USD"` in the `PO_DISPATCH` draft body.

---

## 3. Per-Scenario Detailed Logs (`SCN-21`–`SCN-50`)

- **`SCN-21` (`PASS`):** `PO-1041` (`BRG-402`, `sup1`, `SENT_AWAITING_ACK`). Step 1 (`inventory_manager` → `sup1` CC `lex`), Step 2 (`sup1` → `inventory_manager`, no `lex`), Step 3 (`inventory_manager` → `sup1` CC `lex` forwarding): all returned `200` with `triage=None`, `PO-1041.status=SENT_AWAITING_ACK` (1 trace total for initial dispatch).
- **`SCN-22` (`PASS`):** `PO-1041` (`BRG-402`, `sup1`, `qty=10`, `₹450`), `GRN=10 OK` → `RECEIVED_PENDING_INVOICE`. Sent `inv_sup1_clean_po1048.pdf` (`APX-2026-881`, `subtotal=4500.0, tax=810.0, total=5310.0, conf=1.0`) → `MATCHED`, `PO-1041=CLOSED`, `on_hand: 2→12`, `quarantined=0`.
- **`SCN-23` (`PASS`):** `PO-1041` (`FLT-108`, `sup1`, `qty=20`, `₹320`). Step 1: `inv_sup1_early_po1049.pdf` before GRN → `INVOICE_PENDING_GRN` (`on_hand=17`). Step 2 (`+2d`): `GRN=18 OK` → auto-triggered 3-way match (`MISMATCH_QTY: PO=20, GRN=18, Invoice=20`), `QUARANTINED_INVOICE_MISMATCH`, `quarantined=18`, `on_hand=17`, discrepancy draft approved.
- **`SCN-24` (`PASS`):** `PO-1041` (`BLT-301`, `sup2`, `qty=100`, `₹100`), `GRN=100 OK`. `v1` (`₹150`) → `MISMATCH_PRICE`, `quarantined=100`, `on_hand=14`, discrepancy draft approved. `v2` (`₹100`, `+1d`) → `MATCHED`, `CLOSED`, `quarantined=0`, `on_hand=114`.
- **`SCN-25` (`PASS`):** `PO-1041` (`GSK-512`, `sup2`, `qty=50`, `₹200`), `GRN=50 OK`. `v1` (`qty=40`) → `MISMATCH_QTY`, `quarantined=50`. `v2` (`₹201.20`) → `MISMATCH_PRICE` (`+₹1.20`), `quarantined=50` (no double-add). `v3` (`₹200.25`) → `MATCHED` (`+₹0.25` within `±₹0.50`), `CLOSED`, `on_hand=69`, `quarantined=0`.
- **`SCN-26` (`PASS`):** Seeded `PO-1040` (`SHF-605`, `sup2`, `qty=10`, `₹800`, `RECEIVED_PENDING_INVOICE`). Sent `inv_sup2_freight_surcharge.pdf` (`+₹1,200` freight line, total `₹9,200`) → `MISMATCH_BOTH` (`total_price_impact=1200.0`), `QUARANTINED_INVOICE_MISMATCH`, `quarantined=10`, discrepancy draft approved.
- **`SCN-27` (`PASS`):** `PO-1041` (`BLT-301`, `sup2`, `qty=25`, `RECEIVED_PENDING_INVOICE`). Sent `inv_sup2_scanned_blurry.pdf` → `invoice_extraction.confidence=0.80 < 0.85` (`"Unit price is partially obscured by a brown blob"`), `HIGH` `INVOICE_EXTRACTION_REVIEW` alert, `PO-1041` stayed `RECEIVED_PENDING_INVOICE`.
- **`SCN-28` (`PASS`):** `PO-1041` (`BRG-402`, `10×₹450`) & `PO-1042` (`FLT-108`, `15×₹320`) both `RECEIVED_PENDING_INVOICE`. Sent `inv_sup1_consolidated_2pos.pdf` → `po_number=None`, `matched_po_number=None`, guardrail escalated; both POs stayed `RECEIVED_PENDING_INVOICE`.
- **`SCN-29` (`PASS`):** `PO-1041` (`BLT-301`, `sup2`, `qty=30`, `RECEIVED_PENDING_INVOICE`). Brand-new email `"Bill attached"` / `"Please process payment."` + `inv_sup2_missing_invno.pdf` → resolved `matched_po_id=41` from PDF, `[Guardrail: Missing invoice_number in PDF]` capped `conf=0.50`, `HIGH` `INVOICE_EXTRACTION_REVIEW` alert, `PO-1041` stayed `RECEIVED_PENDING_INVOICE`.
- **`SCN-30` (`PARTIAL / GAP`):** `PO-1041` (`VLV-204`, `sup2`, `GRN=5 OK`). Sent 2 PDFs; single-file endpoint kept `cn_sup2_credit_note.pdf` → `QUARANTINED_INVOICE_MISMATCH` (`quarantined=5, on_hand=3`).
- **`SCN-31` (`FAIL`):** `PO-1041` (`BRG-402`, `sup1`, `qty=12, ₹450`). `GRN=12, condition=DAMAGED` (`HELD_QUARANTINE`). Clean `inv_sup1_damaged_shipment.pdf` triggered `RELEASE_DIRECT`, closing `PO-1041` and releasing 12 damaged units to `on_hand` (`2→14`).
- **`SCN-32` (`PASS`):** `PO-1041` (`VLV-204`, `sup2`, `qty=10, ₹1,500`, `GRN=10 OK`). `inv_sup2_quarantine_sla_v1.pdf` (`₹1,800`) → `quarantined=10`. `+49h`: `QUARANTINE_REMINDER_DRAFTED`. `+97h`: `QUARANTINE_ESCALATED` (`CRITICAL`) + `escalate-procurement` (`200`).
- **`SCN-33` (`PASS`):** `making` requested `2` `BRG-402`. `on_hand` stayed `4` until `approve_stock_withdrawal` CTA → `on_hand=2`, `PO-1041` (`qty=4`) created & approved (`SENT_AWAITING_ACK`).
- **`SCN-34` (`PASS`):** `BRG-402` (`on_hand=2`). `making` requested `10` → `STOCK_SHORTAGE` → edited `STOCK_REQUEST_CLARIFICATION` (`APPROVED_EDITED`) → `making` replied min `2` → `approve_stock_withdrawal` → `on_hand=0`.
- **`SCN-35` (`PASS`):** `BLT-301` (`on_hand=0, quarantined=10`). `making` requested `5` → `CRITICAL` `STOCK_SHORTAGE` surfaced `10 units quarantined on invoice mismatch` with no issue CTA.
- **`SCN-36` (`PASS`):** Single `making` email (`10 WSH-104`, `3 FLT-108`, `4 SNS-802`) → 3 independent alerts (`STOCK_WITHDRAWAL_REQUEST`×2, `STOCK_SHORTAGE`×1) → approved/issued (`110`, `2`, `0`).
- **`SCN-37` (`PASS`):** Two competing `BRG-402` requests (`4` and `3` against `on_hand=4`). Req #1 approved (`on_hand=0`); Req #2 blocked (`400: Cannot withdraw 3 units of BRG-402; only 0 on hand`).
- **`SCN-38` (`PASS`):** `"a handful of those bearings—maybe 5 or 6 pcs"` → `HIGH` `AMBIGUOUS_STOCK_REQUEST`, `BRG-402=4` & `BRG-405=5` untouched.
- **`SCN-39` (`PASS`):** `PMP-701` (`on_hand=2`, open `PO-1041` ETA `2026-11-29`). `making` requested `2` → approved (`on_hand=0`), 0 duplicate PO, `CRITICAL` `ACCELERATED_BURN` alert fired.
- **`SCN-40` (`PARTIAL / GAP`):** Step 1 without `lex` CC → `400`. Step 2 with `making_bom_line3.csv` → `400: Only .pdf attachments are supported in MVP.`
- **`SCN-41` (`PASS`):** `PO-1038` (`RLY-901`, `sup3`, `GRN=10 OK`). Body claimed `₹100 / 10 pcs`, PDF `inv_sup3_contradicts_body.pdf` billed `₹130 / 45 pcs` → `MISMATCH_BOTH`, `QUARANTINED_INVOICE_MISMATCH`, discrepancy draft `APPROVED_EDITED`.
- **`SCN-42` (`PASS`):** `PO-1041` (`BRG-402`, `sup1`, `RECEIVED_PENDING_INVOICE`). Sent `inv_wrong_customer_po9999.pdf` → `PO-1041` stayed `RECEIVED_PENDING_INVOICE`, `quarantined=0`, `INVOICE_EXTRACTION_REVIEW` alert #3.
- **`SCN-43` (`PASS`):** `PO-1040` (`RECEIVED_PENDING_INVOICE`). `"Invoice BF-5001"` with 0 attachments → `200`, `MEDIUM` `INVOICE_MISSING_PDF` alert, `PO-1040` unchanged.
- **`SCN-44` (`PARTIAL / GAP`):** `PO-1041` (`BLT-301`, `sup2`, `GRN=90 OK`). `inv_sup2_100pcs.pdf` → `MISMATCH_QTY` (`90` vs `100`), `quarantined=90`, `on_hand=14` (single-file upload limit).
- **`SCN-45` (`PARTIAL / GAP`):** `damaged_shipment_photo.jpg` rejected cleanly (`400: Only .pdf attachments are supported in MVP.`).
- **`SCN-46` (`FAIL`):** `.xlsx`, `0-byte PDF`, `.zip` rejected cleanly (`400`), **but** `locked_invoice.pdf` threw `500 Internal Server Error`.
- **`SCN-47` (`PASS`):** `sup3` reply → `SUPPLIER_MEMORY_LEARNED` (`memory_note_id=4`) → `PATCH /api/suppliers/3/memory/4` (`200`) → `PO-1041` draft incorporated written price lock & firm dispatch date (minor note: draft said `USD`).
- **`SCN-48` (`PASS`):** `POST /api/planning/recalculate` (`0` LLM calls): `sup2` score `56.0 → 38.4`, 15 `ROP_RECOMMENDATION`s generated, `approve_rop` CTA applied (`PMP-701: rop=7, S=16`).
- **`SCN-49` (`PASS`):** `PO_DISPATCH` (`1/0/0`, `100.0%`), `DISPATCH_DETAILS_REQUEST` (`0/1/0`, `0.0%`), `INVOICE_DISCREPANCY` (`0/0/1`, `0.0%`).
- **`SCN-50` (`PASS`):** All 9 steps across all 7 personas completed (`PO-1041` `CANCELLED`, `PO-1042` `CLOSED` via `BD-77880` & `v1→v2` invoice, `BRG-402` `on_hand=3, quarantined=0`).

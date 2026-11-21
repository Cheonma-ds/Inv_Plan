# InvPlan Batch 1 Live Execution Report (`SCN-01`–`SCN-20`)

- **Execution Date:** 2026-11-21 (Simulated Clock start: `2026-11-20T09:00:00Z`)
- **Model:** Live `gemini-3.5-flash-lite` via `google-genai` (`$GEMINI_API_KEY`)
- **Isolated Environment:** `DATABASE_URL=sqlite:///data/test_runs/scn_batch1.db`, `ATTACHMENTS_DIR=data/test_runs/attachments` (FastAPI on `:8001`; production `data/inv_plan.db` untouched at `434,176` bytes)
- **Batch Totals:** **75 Gemini calls** | **103,312 tokens** (`86,968` input + `16,344` output) | **₹0.9598 INR** total cost
- **Scorecard Summary:** **15 `PASS`**, **5 `PARTIAL / GAP`**, **0 `FAIL`**

---

## 1. Batch Summary & Scorecard

| Scenario | Status | Scope Tag | Gemini Calls | Total Tokens (`in+out`) | Total Cost (INR) | Trace JSON | 1-Line Outcome Summary |
|---|---|---|---|---:|---:|---|---|
| `SCN-01` | `PASS` | `IN-SCOPE` | 2 | 2,570 (`2,079+491`) | ₹0.0255 | [`scn-01.json`](traces/batch1/scn-01.json) | Caught buried `+22%` price hike & `qty=15` bump on `PO-1038`; `APPROVED_EDITED` tracked. |
| `SCN-02` | `PASS` | `IN-SCOPE` | 3 | 3,824 (`3,140+684`) | ₹0.0370 | [`scn-02.json`](traces/batch1/scn-02.json) | Extracted compound price+ETA slip (`11.4d` stockout gap); switched `PO-1041`→`PO-1042` (`sup2`). |
| `SCN-03` | `PARTIAL / GAP` | `IN-SCOPE` | 1 | 1,470 (`1,203+267`) | ₹0.0143 | [`scn-03.json`](traces/batch1/scn-03.json) | `<0.85` guardrail held (`conf=0.65`, `AMBIGUOUS`, 0 PO mutations); no clarification draft to reject. |
| `SCN-04` | `PASS` | `IN-SCOPE` | 4 | 6,273 (`5,329+944`) | ₹0.0574 | [`scn-04.json`](traces/batch1/scn-04.json) | Step 1 off-thread reply escalated (`conf=0.40`, 0 mutations); Step 2 resolved `PO-1041` only. |
| `SCN-05` | `PASS` | `IN-SCOPE` | 3 | 3,854 (`3,171+683`) | ₹0.0372 | [`scn-05.json`](traces/batch1/scn-05.json) | Classified `OUT_OF_STOCK` (`SNS-802-ALT` not accepted); switched `PO-1041`→`PO-1042` (`sup1`). |
| `SCN-06` | `PARTIAL / GAP` | `PARTIALLY IN-SCOPE` | 3 | 5,334 (`4,542+792`) | ₹0.0486 | [`scn-06.json`](traces/batch1/scn-06.json) | Caught post-ack `PRICE_CHANGE` (`+18.7%`) & updated price, but PO stayed `ACKNOWLEDGED` & no `REVISED_PO` draft. |
| `SCN-07` | `PASS` | `IN-SCOPE` | 2 | 2,789 (`2,346+443`) | ₹0.0259 | [`scn-07.json`](traces/batch1/scn-07.json) | Classified `PARTIAL_AVAILABILITY` (`proposed_qty=4`); `PO-1041`→`EXCEPTION_REVIEW`, `CRITICAL` alert. |
| `SCN-08` | `PARTIAL / GAP` | `PARTIALLY IN-SCOPE` | 1 | 1,550 (`1,206+344`) | ₹0.0163 | [`scn-08.json`](traces/batch1/scn-08.json) | Guarded `PO-1038.unit_price` (`100.0`), but treated `$18.50 USD` as `₹18.50 INR` (no `USD` warning). |
| `SCN-09` | `PASS` | `IN-SCOPE` | 6 | 5,590 (`4,665+925`) | ₹0.0527 | [`scn-09.json`](traces/batch1/scn-09.json) | Exact 24h/48h/72h `CRITICAL` ghosting ladder → `UNRESPONSIVE_ESCALATED` → Procurement + Backup `sup2`. |
| `SCN-10` | `PASS` | `IN-SCOPE` | 6 | 6,785 (`5,747+1,038`) | ₹0.0624 | [`scn-10.json`](traces/batch1/scn-10.json) | Late ack recovered `UNRESPONSIVE_ESCALATED → ACKNOWLEDGED` with full `DTDC`/`DT-8801` logistics. |
| `SCN-11` | `PARTIAL / GAP` | `IN-SCOPE` | 5 | 6,974 (`5,782+1,192`) | ₹0.0665 | [`scn-11.json`](traces/batch1/scn-11.json) | `DELAYED_CRITICAL` + `revised_eta` + `EXPEDITE_REQUEST` worked, but `procurement` omitted from draft CCs. |
| `SCN-12` | `PASS` | `PARTIALLY IN-SCOPE` | 4 | 5,867 (`4,975+892`) | ₹0.0538 | [`scn-12.json`](traces/batch1/scn-12.json) | Safely escalated 2-PO delay email as `AMBIGUOUS` (`conf=0.50`) with zero silent drop of either PO. |
| `SCN-13` | `PASS` | `IN-SCOPE` | 4 | 4,734 (`3,912+822`) | ₹0.0454 | [`scn-13.json`](traces/batch1/scn-13.json) | `PRE_ETA_CHECKIN` fired at `ETA - 3d`; confession updated `revised_eta` & `promised_dispatch_date`. |
| `SCN-14` | `PASS` | `IN-SCOPE` | 4 | 4,954 (`4,147+807`) | ₹0.0465 | [`scn-14.json`](traces/batch1/scn-14.json) | Auto-transitioned `OVERDUE` at `ETA + 24h`; `sup2` score dropped `56.0 → 38.6`; Procurement escalated. |
| `SCN-15` | `PASS` | `IN-SCOPE` | 2 | 2,795 (`2,327+468`) | ₹0.0265 | [`scn-15.json`](traces/batch1/scn-15.json) | Clean ack populated all 3 logistics fields (`COMPLETE`) + `INFO` alert + distinct timestamps. |
| `SCN-16` | `PASS` | `IN-SCOPE` | 4 | 6,377 (`5,462+915`) | ₹0.0575 | [`scn-16.json`](traces/batch1/scn-16.json) | Bare ack → `AWAITING_IMMEDIATE` → `APPROVED_EDITED` request → follow-up resolved `COMPLETE`. |
| `SCN-17` | `PASS` | `IN-SCOPE` | 5 | 7,348 (`6,261+1,087`) | ₹0.0668 | [`scn-17.json`](traces/batch1/scn-17.json) | `DEFERRED_2D` (`INFO` only) → `+2d 1h` `TIMED_OUT` + nudge → `BD-8821` resolved `COMPLETE`. |
| `SCN-18` | `PASS` | `IN-SCOPE` | 5 | 7,163 (`6,102+1,061`) | ₹0.0652 | [`scn-18.json`](traces/batch1/scn-18.json) | Anti-loop held: 2nd deferral kept `TIMED_OUT` & raised `HIGH` `repeated_deferral=true` alert. |
| `SCN-19` | `PARTIAL / GAP` | `PARTIALLY IN-SCOPE` | 6 | 9,747 (`8,431+1,316`) | ₹0.0863 | [`scn-19.json`](traces/batch1/scn-19.json) | Incremental drip preserved `actual_dispatch_date` + `SE-901122`, but non-invoice PDF carrier `SafeExpress` not parsed. |
| `SCN-20` | `PASS` | `IN-SCOPE` | 5 | 7,314 (`6,141+1,173`) | ₹0.0682 | [`scn-20.json`](traces/batch1/scn-20.json) | Overwrote `BlueDart/BD-OLD1` → `Delhivery/DEL-9948102`, `revised_eta=2026-11-27`, `INFO` diff alert. |

---

## 2. Discovered Bugs & Product/Scope Gaps

### Bugs
1. **`EXPEDITE_REQUEST` drafts omit `procurement` from `cc_personas` (`SCN-11`, `SCN-20`):**
   - **Evidence:** In `SCN-11` (`CRITICAL_DELAY` alert #5, `assigned_team="Procurement"`), the generated `EXPEDITE_REQUEST` draft (`comm_id=5`) had `to_personas=["sup3"]` and `cc_personas=["inventory_manager"]`—`procurement` was missing from `cc_personas`. Same in `SCN-20` (`comm_id=5`, `cc_personas=["inventory_manager"]`).
   - **Impact:** Violates the `SCN-11` CC rule (`CC: inventory_manager, procurement` for `CRITICAL` delays).

### Confirmed Scope Gaps
1. **Non-invoice PDF attachments are not passed to Gemini during email triage (`SCN-08`, `SCN-19`):**
   - **Evidence (`SCN-19`):** When `sup2` sent `dispatch_advice_sup2_po1045.pdf` (containing `Carrier: SafeExpress`) with body `"Material left our gate today! Dispatch note attached."`, Lex did not read the PDF and left `carrier_name=None` (`missing_dispatch_fields=["carrier_name"]`), explicitly advising `"Check the attached dispatch note to verify if carrier and tracking details are provided inside the document."`
   - **Evidence (`SCN-08`):** `quote_sup3_usd_po1040.pdf` was not inspected during email triage.
2. **No currency extraction / foreign-currency (`USD`) mismatch guardrail in email triage (`SCN-08`):**
   - **Evidence:** `EmailTriageResult` has no `currency` field. On `PO-1038`, Lex saw `"18.50 per unit (imported batch, USD billing)"` and classified `PRICE_CHANGE` (`proposed_unit_price=18.5`), formatting the alert as `Price: ₹100.00 → ₹18.50 (₹-81.50 / -81.5%)` with zero mention of `USD` in `summary_for_manager` or `reasoning`.
3. **Post-acknowledgment `PRICE_CHANGE` cannot transition `ACKNOWLEDGED → EXCEPTION_REVIEW` or auto-draft `REVISED_PO` (`SCN-06`):**
   - **Evidence:** After `PO-1041` was `ACKNOWLEDGED`, `sup2`'s price reversal (`₹1,500 → ₹1,780`) correctly raised a `CRITICAL` `PO_EXCEPTION` alert (#4) and `update_po` CTA updated `unit_price=1780.0`, **but** `PO-1041.status` remained `ACKNOWLEDGED` (state diagram lacks `ACKNOWLEDGED → EXCEPTION_REVIEW`) and `update_po` returned `draft_comm_id=None` instead of generating a `REVISED_PO` draft.
4. **Multi-PO emails escalate as `AMBIGUOUS` rather than splitting (`SCN-12`):**
   - **Evidence:** `EmailTriageResult` is single-PO (`matched_po_number: str | None`). When `sup2` reported delays on both `PO-1041` (`CRITICAL`) and `PO-1039` (`LOW`) in one email, Lex safely escalated as `AMBIGUOUS` (`confidence=0.50`) with zero silent drops.

### Prompt / Guardrail Tuning Needed
1. **`AMBIGUOUS_EMAIL` does not auto-generate an optional clarification draft (`SCN-03`):**
   - **Evidence:** In `SCN-03`, `AMBIGUOUS_EMAIL` (`conf=0.65`) had `draft_comm_id=None`, so the Manager could not test rejecting a clarification draft (`autonomy_policies.rejected_count` remained `0`).
2. **ETA date normalization stores `17:00:00+00:00` (`SCN-14`):**
   - **Evidence:** Date-only strings like `"2026-11-22"` are stored as `2026-11-22T17:00:00+00:00`, meaning `ETA + 24h` (`OVERDUE`) fires at `2026-11-23T17:00:00Z` rather than `09:00:00Z` (requiring `+3d 10h` instead of `+3d 2h` in `SCN-14`).
3. **Exception alert `total_cost_impact_inr` when both price and qty change (`SCN-01`):**
   - **Evidence:** In `SCN-01`, `Price: ₹100 → ₹122`, `Qty: 10 → 15` reported `total impact +₹330.00` (`22 × 15`), whereas total PO value delta `(15×122) - (10×100)` is `+₹830.00`.

---

## 3. Per-Scenario Detailed Logs (`SCN-01`–`SCN-20`)

### `SCN-01`: Hidden Price Hike & MOQ Bump Buried in Polite Confirmation
- **Preconditions & Mapping:** Seeded `PO-1038` (`id=38`, `sup3`, `RLY-901`, `STANDARD`, `SENT_AWAITING_ACK`, `qty=10`, `unit_price=₹100.00`, `promised_eta=2026-12-03`).
- **Steps & Clock:**
  - `T+0 (2026-11-20T09:00:00Z)`: `POST /api/mailroom/send` from `sup3` → `inventory_manager` (CC `lex`), `po_id=38`.
- **Verbatim Lex Outputs & Traces:**
  - `trace#1` (`email_triage`, `2531ms`, `1267+326` tok, `₹0.016197`): `intent=PRICE_CHANGE`, `matched_po_number="PO-1038"`, `confidence=0.95`, `proposed_unit_price=122.0`, `proposed_qty=15`, `proposed_eta_date="2026-12-03"`. Summary: *"Deccan Electro-Spares accepted the delivery date of Dec 3 for PO-1038, but increased the unit price from 100 to 122 due to copper surcharges and raised the minimum quantity to 15."* Memory note saved: *"Enforces a minimum batch size of 15 pieces and implements sudden copper coil surcharges on relay modules."*
  - `trace#2` (`email_draft`, `1491ms`, `812+165` tok, `₹0.009274`): Drafted `REVISED_PO` (`comm_id=2`) `"Revised Purchase Order PO-1038 - PLC Safety Relay Module 24V"` → `["sup3"]`, CC `["inventory_manager"]`.
- **HITL Actions:** Executed `update_po` on alert #2 (`unit_price=122.0, qty=15`), then approved draft #2 with edited body appending *"Note: ₹122 is approved for this batch only; Q1 pricing review required."* (`APPROVED_EDITED`).
- **Resulting State:** `PO-1038` moved `SENT_AWAITING_ACK → EXCEPTION_REVIEW → PENDING_DISPATCH_APPROVAL → SENT_AWAITING_ACK` (`unit_price=122.0, qty=15`). `REVISED_PO.approved_edited_count=1`.
- **Checklist:** `[x]` Not `ACKNOWLEDGED`; `[x]` Entered `EXCEPTION_REVIEW`; `[x]` Deterministic `+22%` / `+₹22.00` variance shown; `[x]` `APPROVED_EDITED` recorded. **Status: `PASS`**.

### `SCN-02`: Compound Exception — Price Hike + Lead Time Slip + Stockout Impact + Backup Failover
- **Preconditions & Mapping:** Consumed `2` of `BRG-402` (`part_id=1`, `CRITICAL`) → created `PO-1041` (`id=41`, `sup1`, `qty=4`, `₹450.00`). Approved dispatch draft (`APPROVED_AS_IS`) → `SENT_AWAITING_ACK`.
- **Steps & Clock:**
  - `T+0 (2026-11-20T09:00:00Z)`: `sup1` sent `PO-1041 update` (`₹540/unit`, `Dec 10`).
- **Verbatim Lex Outputs & Traces:**
  - `trace#1` (`email_draft`, `1343ms`, `791+167` tok, `₹0.009192`): Initial `PO_DISPATCH` for `PO-1041`.
  - `trace#2` (`email_triage`, `2238ms`, `1565+357` tok, `₹0.018856`): `intent=PRICE_CHANGE`, `matched_po_number="PO-1041"`, `confidence=0.95`, `proposed_unit_price=540.0`, `proposed_eta_date="2026-12-10"`, `delay_reason="Raw billet costs surged and furnace is backed up"`.
  - `trace#3` (`email_draft`, `1030ms`, `784+160` tok, `₹0.008971`): Backup `PO_DISPATCH` for `PO-1042` (`sup2`).
- **HITL Actions:** Executed `switch_backup_supplier` (`sup2`) on `CRITICAL` alert #3, then approved `PO-1042` draft (`APPROVED_AS_IS`).
- **Resulting State:** Alert #3 showed `Price: ₹450.00 → ₹540.00 (+₹90.00 / +20.0%)` and `Stockout projected 2026-11-29 (9.0d supply vs 20.3d to ETA — 11.4d short)`. `PO-1041`=`CANCELLED`, `PO-1042`=`SENT_AWAITING_ACK` (`sup2`, `qty=4`, `₹480.00`). `BRG-402` `on_order_stock=4`, `inventory_position=6`.
- **Checklist:** `[x]` Deterministic stockout & `+20%` variance; `[x]` `PO-1041` `CANCELLED` & `PO-1042` sent; `[x]` Zero duplicate `on_order_stock`. **Status: `PASS`**.

### `SCN-03`: Ambiguous Supplier Reply Triggering `< 0.85` Confidence Guardrail
- **Preconditions & Mapping:** Seeded `PO-1038` (`id=38`, `sup3`, `RLY-901`, `SENT_AWAITING_ACK`, `qty=10`, `₹100.00`, `promised_eta=2026-12-03`).
- **Steps & Clock:**
  - `T+0 (2026-11-20T09:00:00Z)`: `sup3` sent hedged reply (`"Checking with plant on whether we can hold last quarter's rate... Will try our best for early Dec."`).
- **Verbatim Lex Outputs & Traces:**
  - `trace#1` (`email_triage`, `1795ms`, `1203+267` tok, `₹0.014307`): `intent=AMBIGUOUS`, `matched_po_number="PO-1038"`, `confidence=0.65`, `details_promised_later=true`. Reasoning: *"The supplier has acknowledged receipt of PO-1038 but is uncertain about both pricing and dispatch timeline, which introduces ambiguity rather than a clean acknowledgment or confirmed change."*
- **HITL Actions:** Checked drafts (`[]`); patched PO note `"Called Sup3, waiting for written quote by tomorrow."` via `PATCH /api/pos/38`.
- **Resulting State:** `PO-1038` remained `SENT_AWAITING_ACK` (`₹100.00`, `2026-12-03`). `HIGH` `AMBIGUOUS_EMAIL` alert #1 created. `autonomy_policies.rejected_count=0` (no draft generated).
- **Checklist:** `[x]` `PO-1038.status == SENT_AWAITING_ACK`; `[x]` `HIGH` `AMBIGUOUS_EMAIL` alert created; `[~]` No clarification draft generated to reject. **Status: `PARTIAL / GAP`**.

### `SCN-04`: Off-Thread Reply Without PO Number When Supplier Has Two Open POs
- **Preconditions & Mapping:** Consumed `10` of `GSK-512` (`part_id=8`) → `PO-1041` (`sup2`, `qty=25`, `₹200`); raised ROP to `40`/`50` and consumed `1` → `PO-1042` (`sup2`, `qty=16`, `₹200`). Approved both drafts → both `SENT_AWAITING_ACK`.
- **Steps & Clock:**
  - `T+0`: `sup2` sent brand-new thread `"Viton Gasket order confirmation"` (no `po_id`, no `PO#`).
  - `T+2h`: Manager asked for PO# on thread; `sup2` replied `"Referring to PO-1041 (25 pcs), dispatch Nov 27 via DTDC tracking DT-10293."`
- **Verbatim Lex Outputs & Traces:**
  - `trace#1` (`email_draft`, `1805ms`, `801+175` tok, `₹0.009456`), `trace#2` (`email_draft`, `1364ms`, `801+164` tok, `₹0.009179`).
  - `trace#3` (`email_triage`, `1950ms`, `1762+303` tok, `₹0.018736`): `intent=AMBIGUOUS`, `matched_po_number=None`, `confidence=0.40`. Reasoning: *"both PO-1041 and PO-1042 are open for this exact part and price. Without a PO number in the email, it cannot be reliably matched to a single open PO."*
  - `trace#4` (`email_triage`, `1532ms`, `1965+302` tok, `₹0.019990`): `intent=ACKNOWLEDGED`, `matched_po_number="PO-1041"`, `confidence=0.98`, `promised_dispatch_date="2026-11-27"`, `carrier_name="DTDC"`, `tracking_number="DT-10293"`.
- **Resulting State:** Step 1: 0 mutations; `HIGH` `AMBIGUOUS_EMAIL` alert #3. Step 2: `PO-1041`=`ACKNOWLEDGED` (`COMPLETE`, `DTDC`, `DT-10293`), `PO-1042`=`SENT_AWAITING_ACK`, `PO_ACKNOWLEDGED_COMPLETE` alert #4.
- **Checklist:** `[x]` Neither PO mutated on Step 1; `[x]` Step 2 resolved `PO-1041` only. **Status: `PASS`**.

### `SCN-05`: Out-of-Stock Rejection With Unsolicited Substitute Part Offer
- **Preconditions & Mapping:** Consumed `1` of `SNS-802` (`part_id=3`, `CRITICAL`) → `PO-1041` (`sup3`, `qty=9`, `₹600.00`), approved dispatch → `SENT_AWAITING_ACK`.
- **Steps & Clock:**
  - `T+0`: `sup3` replied offering `SNS-802-ALT` at `₹550` for tomorrow.
- **Verbatim Lex Outputs & Traces:**
  - `trace#1` (`email_draft`, `1772ms`, `802+186` tok, `₹0.009740`).
  - `trace#2` (`email_triage`, `1814ms`, `1598+337` tok, `₹0.018560`): `intent=OUT_OF_STOCK`, `matched_po_number="PO-1041"`, `confidence=0.95`, `proposed_unit_price=550.0`. Summary flagged `SNS-802-ALT`.
  - `trace#3` (`email_draft`, `1394ms`, `771+160` tok, `₹0.008889`): Backup `PO_DISPATCH` for `PO-1042` (`sup1`).
- **HITL Actions:** Executed `switch_backup_supplier` (`sup1`) on `CRITICAL` alert #2; approved `PO-1042` draft (`APPROVED_AS_IS`).
- **Resulting State:** `PO-1041`=`CANCELLED`, `PO-1042`=`SENT_AWAITING_ACK` (`sup1`, `₹640.00`). `SNS-802-ALT` not added to catalog.
- **Checklist:** `[x]` Not `ACKNOWLEDGED`; `[x]` Substitute not accepted; `[x]` Backup failover clean. **Status: `PASS`**.

### `SCN-06`: Supplier Reversal Mid-Thread (Acknowledges First, Reverses to Price Hike 4 Hours Later)
- **Preconditions & Mapping:** Consumed `2` of `VLV-204` (`part_id=2`, `CRITICAL`) → `PO-1041` (`sup2`, `qty=9`, `₹1,500.00`), approved dispatch → `SENT_AWAITING_ACK`.
- **Steps & Clock:**
  - `T+0`: `sup2` confirmed `PO-1041` at `₹1,500`, dispatching `Nov 30` via `BlueDart` (`"tracking will follow once booked"`).
  - `T+4h`: `sup2` reversed price to `₹1,780/unit`, ETA `Dec 3`.
- **Verbatim Lex Outputs & Traces:**
  - `trace#1` (`email_draft`, `1169ms`, `807+167` tok, `₹0.009292`).
  - `trace#2` (`email_triage`, `1532ms`, `1784+295` tok, `₹0.018673`): `intent=ACKNOWLEDGED`, `conf=0.98`, `promised_dispatch_date="2026-11-30"`, `carrier_name="BlueDart"`, `details_promised_later=true` → `DEFERRED_2D`.
  - `trace#3` (`email_triage`, `1801ms`, `1951+330` tok, `₹0.020607`): `intent=PRICE_CHANGE`, `conf=0.98`, `proposed_unit_price=1780.0`, `proposed_eta_date="2026-12-03"`. Memory note saved.
- **HITL Actions:** Executed `update_po` (`unit_price=1780.0`) on `CRITICAL` alert #4.
- **Resulting State:** Alert #4 quantified `₹1,500.00 → ₹1,780.00 (+₹280.00 / +18.7%, total impact +₹2,520.00)`. `update_po` updated `unit_price=1780.0`, but `PO-1041` stayed `ACKNOWLEDGED` and `draft_comm_id=None`.
- **Checklist:** `[x]` Step 2 captured `PRICE_CHANGE` & alert quantified delta; `[ ]` No `ACKNOWLEDGED → EXCEPTION_REVIEW` transition or `REVISED_PO` draft. **Status: `PARTIAL / GAP`**.

### `SCN-07`: Partial Availability Split Proposal + MOQ Conflict
- **Preconditions & Mapping:** Consumed `1` of `PMP-701` (`part_id=4`, `CRITICAL`) → `PO-1041` (`sup3`, `qty=8`, `₹2,000.00`), approved dispatch → `SENT_AWAITING_ACK`.
- **Steps & Clock:**
  - `T+0`: `sup3` offered `4` units by `Nov 26`, remaining `4` on `Dec 12`.
- **Verbatim Lex Outputs & Traces:**
  - `trace#1` (`email_draft`, `1863ms`, `804+132` tok, `₹0.008392`).
  - `trace#2` (`email_triage`, `1588ms`, `1542+311` tok, `₹0.017552`): `intent=PARTIAL_AVAILABILITY`, `proposed_qty=4`, `proposed_eta_date="2026-11-26"`, `confidence=0.98`.
- **HITL Actions:** Executed `add_note` on `CRITICAL` alert #2 (`"Approved split: ship 4 immediately..."`).
- **Resulting State:** `PO-1041` moved to `EXCEPTION_REVIEW`; alert #2 showed `6.7d` of supply on hand (`2026-11-27` stockout).
- **Checklist:** `[x]` `proposed_qty=4`; `[x]` `EXCEPTION_REVIEW` + `CRITICAL` alert. **Status: `PASS`**.

### `SCN-08`: Supplier Replies & Attaches Quote in USD (`$`) Instead of INR (`₹`)
- **Preconditions & Mapping:** Seeded `PO-1038` (`id=38`, `sup3`, `RLY-901`, `SENT_AWAITING_ACK`, `qty=10`, `₹100.00`).
- **Steps & Clock:**
  - `T+0`: `sup3` sent email `"at 18.50 per unit (imported batch, USD billing). Delivery in 14 days."` with `quote_sup3_usd_po1040.pdf`.
- **Verbatim Lex Outputs & Traces:**
  - `trace#1` (`email_triage`, `1790ms`, `1206+344` tok, `₹0.016267`): `intent=PRICE_CHANGE`, `conf=0.95`, `proposed_unit_price=18.5`, `proposed_eta_date="2026-12-04"`. Summary: *"proposing a unit price of 18.50, which is significantly lower than the original PO unit price of 100.00."*
- **Resulting State:** `PO-1038` moved to `EXCEPTION_REVIEW`, `unit_price` remained `100.0`. However, alert #1 displayed `Price: ₹100.00 → ₹18.50 (₹-81.50 / -81.5%)` with zero mention of `USD`.
- **Checklist:** `[x]` `unit_price` never overwritten to `18.50`; `[ ]` Alert did not mention `USD` currency discrepancy. **Status: `PARTIAL / GAP`**.

### `SCN-09`: Full Ghosting Escalation Ladder on Critical Part + Backup Switch + Procurement Escalation
- **Preconditions & Mapping:** Consumed `2` of `BRG-402` (`part_id=1`, `CRITICAL`) → `PO-1041` (`sup1`, `qty=4`, `₹450.00`), approved dispatch at `T+0` (`2026-11-20T09:00:00Z`) → `SENT_AWAITING_ACK`.
- **Steps & Clock:**
  - `T+25h`: `ACK_REMINDER_DRAFTED` (Follow-up #1) → approved `APPROVED_AS_IS`.
  - `T+49h`: `ACK_REMINDER_DRAFTED` (Follow-up #2) → approved `APPROVED_AS_IS`.
  - `T+73h`: `UNRESPONSIVE_ESCALATED` (`follow_up_count=2`), `CRITICAL` `UNRESPONSIVE_SUPPLIER` alert #6.
- **Verbatim Lex Outputs & Traces:** 6 `email_draft` traces (`trace#1`–`#6`, total `4,665+925` tok, `₹0.0527`, including `PO-1038` background reminders).
- **HITL Actions:** `POST /api/pos/41/escalate-procurement`, `switch_backup_supplier` (`sup2`), approved `PO-1042` dispatch (`APPROVED_AS_IS`).
- **Resulting State:** `PO-1041`=`CANCELLED`, `escalated_to_procurement_at=2026-11-23T10:00:00+00:00`, `procurement` inbox received escalation email, `PO-1042`=`SENT_AWAITING_ACK`.
- **Checklist:** `[x]` 24h/48h/72h cadence; `[x]` `UNRESPONSIVE_ESCALATED`, Procurement email, & backup PO all succeeded. **Status: `PASS`**.

### `SCN-10`: Late Supplier Confirmation Arrives *After* `UNRESPONSIVE_ESCALATED` and Backup Draft Generation
- **Preconditions & Mapping:** Same `SCN-09` ladder on `PO-1041` (`BRG-402`, `sup1`) reaching `UNRESPONSIVE_ESCALATED` at `T+73h`.
- **Steps & Clock:**
  - `T+74h`: `sup1` replied confirming `PO-1041` (`4 pcs at ₹450, shipping Dec 1 via DTDC tracking DT-8801`).
- **Verbatim Lex Outputs & Traces:**
  - `trace#6` (`email_triage`, `1939ms`, `1866+324` tok, `₹0.019921`): `intent=ACKNOWLEDGED`, `conf=0.98`, `promised_dispatch_date="2026-12-01"`, `carrier_name="DTDC"`, `tracking_number="DT-8801"`.
- **Resulting State:** `PO-1041` transitioned `UNRESPONSIVE_ESCALATED → ACKNOWLEDGED` (`COMPLETE`, `DTDC`, `DT-8801`); `PO_ACKNOWLEDGED_COMPLETE` alert #7 raised.
- **Checklist:** `[x]` `UNRESPONSIVE_ESCALATED → ACKNOWLEDGED` worked cleanly; `[x]` Logistics populated & alert created. **Status: `PASS`**.

### `SCN-11`: Post-Ack Delay on `CRITICAL` Part Triggering Stockout Countdown & Expedite Draft
- **Preconditions & Mapping:** Consumed `1` of `SNS-802` (`part_id=3`, `CRITICAL`) → `PO-1041` (`sup3`), approved dispatch, `sup3` acknowledged (`ETA Nov 26`).
- **Steps & Clock:**
  - `T+2d`: `sup3` reported Pune plant machine breakdown, new ETA `Dec 7`.
- **Verbatim Lex Outputs & Traces:**
  - `trace#4` (`email_triage`, `2085ms`, `1703+318` tok, `₹0.018742`): `intent=POST_ACK_DELAY`, `conf=0.98`, `proposed_eta_date="2026-12-07"`, `delay_reason="Machine breakdown at Pune plant"`.
  - `trace#5` (`email_draft`, `1461ms`, `923+229` tok, `₹0.011586`): `EXPEDITE_REQUEST` (`comm_id=5`) → `["sup3"]`, CC `["inventory_manager"]` (missing `procurement`).
- **HITL Actions:** Approved expedite draft with edited body appending *"Can you air-freight 4 pcs by Nov 27?"* (`APPROVED_EDITED`).
- **Resulting State:** `PO-1041`=`DELAYED_CRITICAL`, `promised_eta` preserved (`2026-11-26T17:00:00+00:00`), `revised_eta=2026-12-07T17:00:00+00:00`, `CRITICAL_DELAY` alert #5 (`assigned_team="Procurement"`).
- **Checklist:** `[x]` `DELAYED_CRITICAL` + `promised_eta` preserved; `[ ]` Draft CC omitted `procurement`. **Status: `PARTIAL / GAP`**.

### `SCN-12`: Single Email Reporting Delays on Two Different POs (One `CRITICAL`, One `LOW` With Safe Buffer)
- **Preconditions & Mapping:** `PO-1041` (`sup2`, `VLV-204`, `CRITICAL`, `ACKNOWLEDGED`) + seeded `PO-1039` (`sup2`, `BLT-301`, `STANDARD`, `ACKNOWLEDGED`).
- **Steps & Clock:**
  - `T+1d`: `sup2` sent 1 email reporting 10-day delay (`Dec 5`) for both `PO-1041` and `PO-1039`.
- **Verbatim Lex Outputs & Traces:**
  - `trace#4` (`email_triage`, `1475ms`, `1629+283` tok, `₹0.017394`): `intent=AMBIGUOUS`, `matched_po_number=None`, `confidence=0.50`. Reasoning: *"The email references two separate purchase orders (PO-1041 and PO-1039) in a single message, which violates single-PO handling rules and requires AMBIGUOUS classification."*
- **Resulting State:** `HIGH` `AMBIGUOUS_EMAIL` alert #4 raised; neither PO silently dropped or mutated.
- **Checklist:** `[x]` Neither PO silently dropped; `[x]` Safe `AMBIGUOUS` escalation per single-PO schema boundary. **Status: `PASS`**.

### `SCN-13`: Proactive Pre-ETA Check-In Uncovers Undisclosed Delay Before Overdue
- **Preconditions & Mapping:** Seeded `PO-1039` (`id=39`, `sup2` score `56.0 < 75`, `ACKNOWLEDGED`, `promised_eta=2026-11-30T09:00:00Z`, `pre_eta_checked=False`).
- **Steps & Clock:**
  - `T+7d 1h (2026-11-27T10:00:00Z = ETA - 3d)`: `PRE_ETA_CHECKIN_DRAFTED` (`comm_id=3`), `pre_eta_checked=True`. Manager approved (`APPROVED_AS_IS`).
  - `T+7d 2h`: `sup2` confessed pickup delay (`dispatch Nov 28, ETA Dec 3`).
- **Verbatim Lex Outputs & Traces:**
  - `trace#3` (`email_draft`, `1476ms`, `787+143` tok, `₹0.008562`).
  - `trace#4` (`email_triage`, `1678ms`, `1574+335` tok, `₹0.018358`): `intent=POST_ACK_DELAY`, `conf=0.95`, `proposed_eta_date="2026-12-03"`, `promised_dispatch_date="2026-11-28"`.
- **Resulting State:** `PO-1039` updated `revised_eta=2026-12-03`, `promised_dispatch_date=2026-11-28`; `MINOR_DELAY_ACCEPTED` + `PO_LOGISTICS_UPDATED` alerts created.
- **Checklist:** `[x]` `pre_eta_checked` flipped `False → True` at `ETA - 3d`; `[x]` Undisclosed delay surfaced before ETA. **Status: `PASS`**.

### `SCN-14`: Silent Non-Delivery Past `ETA + 1d` (`OVERDUE`) While `on_hand` Hits Zero
- **Preconditions & Mapping:** `PO-1041` (`sup2`, `VLV-204`, `CRITICAL`, `ACKNOWLEDGED`, `promised_eta=2026-11-22T17:00:00Z`), consumed remaining stock (`on_hand=0`), `sup2.reliability_score=56.0`.
- **Steps & Clock:**
  - `T+3d 10h (2026-11-23T19:00:00Z > ETA + 24h)`: `OVERDUE_DETECTED` (`draft_comm_id=4`). Escalated to Procurement (`POST /api/pos/41/escalate-procurement`).
- **Verbatim Lex Outputs & Traces:** 4 traces (`trace#1`–`#4`, total `4,147+807` tok, `₹0.0465`).
- **Resulting State:** `PO-1041`=`OVERDUE`; `sup2.reliability_score` dropped `56.0 → 38.6`; `procurement` inbox received escalation email.
- **Checklist:** `[x]` `OVERDUE` triggered automatically; `[x]` `sup2` score dropped deterministically; `[x]` Procurement escalation succeeded. **Status: `PASS`**.

### `SCN-15`: Clean Acknowledgment With Full Dispatch, Carrier & Tracking Details
- **Preconditions & Mapping:** Consumed `2` of `BRG-402` (`part_id=1`) → `PO-1041` (`sup1`) at `T+0`; approved dispatch at `T+2h`.
- **Steps & Clock:**
  - `T+5h`: `sup1` acknowledged with `2026-11-24`, `BlueDart`, `BD-440192`. Dismissed `INFO` alert.
- **Verbatim Lex Outputs & Traces:**
  - `trace#2` (`email_triage`, `1710ms`, `1536+330` tok, `₹0.017993`): `intent=ACKNOWLEDGED`, `conf=0.98`, all 3 logistics fields extracted.
- **Resulting State:** `PO-1041`=`ACKNOWLEDGED`, `dispatch_details_status=COMPLETE`, `missing_dispatch_fields=[]`, `PO_ACKNOWLEDGED_COMPLETE` alert, `created_at < sent_at < acknowledged_at`.
- **Checklist:** `[x]` All 3 fields extracted; `[x]` `INFO` alert created; `[x]` Distinct timestamps. **Status: `PASS`**.

### `SCN-16`: Acknowledgment Missing Carrier & Tracking Without Promise (`AWAITING_IMMEDIATE`)
- **Preconditions & Mapping:** `PO-1041` (`sup2`, `VLV-204`, `SENT_AWAITING_ACK`).
- **Steps & Clock:**
  - `T+2h`: `sup2` sent bare ack (`"Confirmed PO-1041, price and Nov 30 delivery look fine."`). Manager edited `DISPATCH_DETAILS_REQUEST` draft (`APPROVED_EDITED`).
  - `T+4h`: `sup2` replied `"Shipping Nov 26 via Delhivery, AWB DL-77310."`
- **Verbatim Lex Outputs & Traces:** `trace#2` (`ACKNOWLEDGED`, `conf=0.98`), `trace#3` (`DISPATCH_DETAILS_REQUEST` draft), `trace#4` (`GENERAL_REPLY`, `conf=0.98`).
- **Resulting State:** Step 1: `AWAITING_IMMEDIATE`, `missing_dispatch_fields=["promised_dispatch_date", "carrier_name", "tracking_number"]`. Step 2: `COMPLETE`, `MISSING_DISPATCH_DETAILS` auto-resolved.
- **Checklist:** `[x]` All 3 missing fields listed & requested; `[x]` Auto-resolution on Step 2; `[x]` `APPROVED_EDITED` tracked. **Status: `PASS`**.

### `SCN-17`: Supplier Promises Tracking "In 2 Days" (`DEFERRED_2D`) → Clock Advance → Nudge → Resolved
- **Preconditions & Mapping:** `PO-1041` (`sup2`, `VLV-204`, `SENT_AWAITING_ACK`).
- **Steps & Clock:**
  - `T+0`: `sup2` promised BlueDart tracking in 2 days (`dispatch Nov 27`).
  - `T+2d 1h`: Clock advance triggered `TIMED_OUT` + `DISPATCH_DETAILS_NUDGE` draft; Manager approved (`APPROVED_AS_IS`).
  - `T+2d 3h`: `sup2` replied `"Here you go — BlueDart tracking BD-8821."`
- **Verbatim Lex Outputs & Traces:** `trace#2` (`ACKNOWLEDGED`, `conf=0.95`), `trace#4` (`email_draft` nudge), `trace#5` (`GENERAL_REPLY`, `conf=0.95`).
- **Resulting State:** `DEFERRED_2D` (`draft=None`) → `TIMED_OUT` → `COMPLETE` (`BD-8821`, `BlueDart`); `MISSING_DISPATCH_DETAILS_DUE` & `DISPATCH_DETAILS_DEFERRED` auto-resolved.
- **Checklist:** `[x]` Zero nudge at `T+0`; `[x]` Nudge drafted after `T+2d`; `[x]` Resolved `COMPLETE`. **Status: `PASS`**.

### `SCN-18`: Anti-Loop Trap — Supplier Defers a Second Time After Already Timing Out
- **Preconditions & Mapping:** `PO-1041` (`sup2`, `VLV-204`) advanced to `TIMED_OUT` (`missing=['carrier_name', 'tracking_number']`) + nudge approved.
- **Steps & Clock:**
  - `T+3d`: `sup2` replied `"will definitely share carrier and tracking in another 2 days."`
- **Verbatim Lex Outputs & Traces:**
  - `trace#5` (`email_triage`, `1885ms`, `1975+319` tok, `₹0.020481`): `intent=GENERAL_REPLY`, `conf=0.90`.
- **Resulting State:** `PO-1041` stayed `TIMED_OUT` (did NOT reset to `DEFERRED_2D`); `HIGH` `PO_LOGISTICS_UPDATED` alert #5 raised with `repeated_deferral=true`.
- **Checklist:** `[x]` Did not reset into passive `DEFERRED_2D` loop; `[x]` `HIGH` warning surfaced. **Status: `PASS`**.

### `SCN-19`: Partial Logistics Drip Across 3 Messages + PDF Dispatch Advice
- **Preconditions & Mapping:** `PO-1041` (`sup2`, `VLV-204`) acknowledged with `AWAITING_IMMEDIATE` (`['promised_dispatch_date', 'carrier_name', 'tracking_number']`).
- **Steps & Clock:**
  - `T+1d`: `sup2` sent `"Material left our gate today! Dispatch note attached."` + `dispatch_advice_sup2_po1045.pdf`.
  - `T+1d 4h`: `sup2` sent `"AWB is SE-901122."`
- **Verbatim Lex Outputs & Traces:**
  - `trace#5` (`email_triage`, `1936ms`, `2058+294` tok, `₹0.020374`): `actual_dispatch_date="2026-11-21"`, `carrier_name=None`.
  - `trace#6` (`email_triage`, `1619ms`, `2153+274` tok, `₹0.020469`): `tracking_number="SE-901122"`, `actual_dispatch_date="2026-11-21"`, `carrier_name=None`.
- **Resulting State:** `actual_dispatch_date` preserved and `SE-901122` merged, **but** `carrier_name` stayed `None` (`AWAITING_IMMEDIATE`) because non-invoice PDF was not parsed.
- **Checklist:** `[x]` Step 2 did not erase `actual_dispatch_date`; `[x]` `PO_LOGISTICS_UPDATED` alerts created; `[ ]` `SafeExpress` not extracted from PDF. **Status: `PARTIAL / GAP`**.

### `SCN-20`: Carrier Switch + Revised ETA Inside `GENERAL_REPLY` With Courier Waybill PDF
- **Preconditions & Mapping:** `PO-1041` (`sup2`, `VLV-204`, `ACKNOWLEDGED`, `BlueDart`, `BD-OLD1`, `promised_eta=2026-11-25`).
- **Steps & Clock:**
  - `T+2d`: `sup2` sent `"handed PO-1041 to Delhivery today (Nov 22), AWB DEL-9948102. Revised ETA is Nov 27."` + `delhivery_waybill_po1046.pdf`.
- **Verbatim Lex Outputs & Traces:**
  - `trace#4` (`email_triage`, `1709ms`, `1891+329` tok, `₹0.020204`): `intent=POST_ACK_DELAY`, `conf=0.95`, `carrier_name="Delhivery"`, `tracking_number="DEL-9948102"`, `actual_dispatch_date="2026-11-22"`, `proposed_eta_date="2026-11-27"`.
- **Resulting State:** `carrier_name="Delhivery"`, `tracking_number="DEL-9948102"`, `actual_dispatch_date=2026-11-22`, `revised_eta=2026-11-27`, `promised_eta` preserved (`2026-11-25`), `INFO` `PO_LOGISTICS_UPDATED` alert #5 listed exact field diffs (`BlueDart → Delhivery`, `BD-OLD1 → DEL-9948102`).
- **Checklist:** `[x]` Carrier & tracking overwritten cleanly; `[x]` `INFO` alert listed changed fields; `[x]` `promised_eta` preserved. **Status: `PASS`**.

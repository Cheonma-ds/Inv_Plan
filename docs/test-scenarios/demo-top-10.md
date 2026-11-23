# Top 10 High-Impact Demo Scenarios — Video Call Playbook

This guide gives exact **UI clicks, copy-paste emails, and fixture attachments** to demo the 10 highest-impact scenarios from [`lex-scenarios.md`](lex-scenarios.md) live in the browser (`http://localhost:5173`).

> **Before You Start (`Terminal Setup`):**
> 1. Start the backend: `uv run uvicorn backend.main:app --reload --port 8000`
> 2. Start the frontend: `cd frontend && npm run dev`
> 3. Open `http://localhost:5173`. Use **`Reset Seed`** in the top clock bar whenever you want a clean starting state (`2026-11-20 09:00 UTC`, 15 parts, seeded open POs `PO-1037`–`PO-1040`).
> 4. Note: In the UI, stock withdrawals always start as a **Making Team** email in **Mailroom** (`CC: lex`) and are approved in **Actionable Alerts** (no silent stock deductions).

---

## Quick Demo Map

| # | Scenario | Pain Point Solved | Est. Time |
|---:|---|---|---|
| 1 | **`SCN-09`** | Supplier Ghosting on Critical Parts + Backup Failover | ~2 min |
| 2 | **`SCN-02`** | Compound Price Hike + ETA Slip + Stockout Projection | ~2 min |
| 3 | **`SCN-08`** | Foreign Currency (`USD`) Quote Trap | ~1 min |
| 4 | **`SCN-12`** | Multi-PO Delay Email Split by Part Criticality | ~1.5 min |
| 5 | **`SCN-19`** | Carrier & Tracking Trapped in PDFs + Incremental Drip | ~2 min |
| 6 | **`SCN-30`** | Multi-PDF Overbilled Invoice + Credit Note Net Settlement | ~2 min |
| 7 | **`SCN-31`** | Damaged Shipment GRN Held Despite Clean Invoice | ~2 min |
| 8 | **`SCN-34`** | Shop-Floor Shortage & Minimum-Quantity Negotiation | ~2 min |
| 9 | **`SCN-40`** | Production `.csv` BOM Attachment Multi-SKU Request | ~1 min |
| 10 | **`SCN-48`** | Dynamic ROP/S Planning & Supplier Scorecards (`0` LLM calls) | ~1.5 min |

---

## 1. `SCN-09` — Critical Supplier Ghosting Ladder & Backup Failover
**Pain Point:** POs sit unacknowledged until the production line runs dry.

1. **Reset & Trigger Critical PO:**
   - Click **`Reset Seed`** in the top bar.
   - Go to **Mailroom** → switch persona to **`making`**.
   - Compose to **`inventory_manager`**, CC **`lex`**:
     - **Subject:** `Need 2 pcs VLV-204`
     - **Body:** `Line 1 needs 2 units of VLV-204 today.`
   - Go to **Actionable Alerts** → click **`Approve Issue & Trigger PO`** (`PO-1041` created for `sup2`).
   - Click **`Approve & Send PO`** on the `PO_DISPATCH_APPROVAL` alert (`PO-1041` is now `SENT_AWAITING_ACK`).
2. **Run the Ghosting Clock Ladder (Top Clock Bar):**
   - Click **`+1d`** (`24h` elapsed) → show the **24h `ACK_REMINDER_DRAFT`** alert; click **`Approve & Send`**.
   - Click **`+1d`** (`48h` elapsed) → show the **48h `ACK_REMINDER_DRAFT`** alert; click **`Approve & Send`**.
   - Click **`+1d`** (`72h` elapsed) → `PO-1041` automatically escalates to **`UNRESPONSIVE_ESCALATED`** (`CRITICAL` alert).
3. **Execute Failover:**
   - On the `CRITICAL` alert, click **`Switch to Apex Precision Components (sup1)`** (and/or **`Escalate to Procurement`**).
   - Approve the replacement `PO-1042` dispatch draft.
4. **Point Out on Screen:** Zero duplicate `on_order_stock`, full audit trail in **PO Tracker**, and `sup2` responsiveness penalized automatically.

---

## 2. `SCN-02` — Compound Commercial Exception + Stockout Gap + Failover
**Pain Point:** Hidden price hikes and delivery delays buried in polite replies cause unexpected stockouts.

1. **Reset & Trigger `BRG-402` PO:**
   - Click **`Reset Seed`**.
   - In **Mailroom** as **`making`**, send to **`inventory_manager`** (`CC: lex`):
     - **Subject:** `Need 2 pcs BRG-402`
     - **Body:** `Line 1 needs 2 units of BRG-402 today.`
   - In **Actionable Alerts**, click **`Approve Issue & Trigger PO`**, then **`Approve & Send PO`** (`PO-1041` to `sup1`, `qty=4`, `₹450.00`).
2. **Simulate Supplier Price + ETA Exception:**
   - Go to **Mailroom** → switch persona to **`sup1`** → open the `PO-1041` thread (or compose to **`inventory_manager`**, CC **`lex`**, linked to `PO-1041`):
     - **Subject:** `Re: PO-1041 - BRG-402`
     - **Body:** `Hi, raw billet costs surged so unit price for PO-1041 is now ₹540/unit and furnace backlog pushes delivery ETA to 2026-12-10.`
3. **Inspect & Resolve in Actionable Alerts:**
   - Go to **Actionable Alerts** → show the `CRITICAL` **`PO_EXCEPTION`** card:
     - Unit price delta (`₹450 → ₹540 / +20.0%`), total spend impact (`+₹360.00`), and deterministic stockout warning (`~9.0d` supply vs. `20.3d` to ETA — `11.4d` short).
   - Click **`Switch to Bharat Industrial Fasteners (sup2)`** → approve the replacement `PO-1042` draft.
4. **Point Out on Screen:** Lex never accepts a price hike silently; deterministic stockout math drives the failover.

---

## 3. `SCN-08` — Foreign Currency (`USD`) Pricing Trap
**Pain Point:** `$18.50 USD` parsed naively against a `₹100 INR` PO looks like a massive discount and corrupts ERP pricing.

1. **Reset:** Click **`Reset Seed`** (uses seeded `PO-1038` to `sup3`, `RLY-901`, `10 pcs @ ₹100.00 INR`, `SENT_AWAITING_ACK`).
2. **Send Foreign-Currency Quote:**
   - Go to **Mailroom** → switch persona to **`sup3`**.
   - Compose to **`inventory_manager`**, CC **`lex`**, linked to **`PO-1038`**:
     - **Subject:** `Quotation & Confirmation for PO-1038`
     - **Body:** `Hi, please see our quote for PO-1038 at 18.50 USD per unit (imported batch, USD billing). Delivery in 14 days.`
3. **Point Out on Screen (Actionable Alerts & PO Tracker):**
   - Lex extracts `proposed_currency: USD` and raises a `HIGH` **`CURRENCY_MISMATCH`** alert (`Quoted 18.50 USD vs PO currency INR`).
   - **`PO-1038` unit price stays `₹100.00`** (never overwritten to `₹18.50`).
   - Click **`Request INR Quote`** to see Lex draft a `DRAFT_PENDING_APPROVAL` reply asking `sup3` for an INR quotation.

---

## 4. `SCN-12` — Multi-PO Delay Email Split by Criticality
**Pain Point:** Suppliers batch updates for urgent and non-urgent orders in one email.

1. **Reset & Prepare Two Open `sup2` POs:**
   - Click **`Reset Seed`** (`PO-1039` to `sup2` for `BLT-301` is already `ACKNOWLEDGED` with buffered `STANDARD` stock).
   - In **Mailroom** as **`making`**, request `2 pcs VLV-204` (`CC: lex`) → in **Actionable Alerts**, approve withdrawal + approve `PO-1041` dispatch (`sup2`, `CRITICAL`).
   - Switch to **`sup2`** in **Mailroom** and reply on `PO-1041`:
     - **Subject:** `Re: PO-1041`
     - **Body:** `Confirmed PO-1041 for 2026-11-25, dispatch Nov 22 via BlueDart BD-9911.`
   - Click **`+1d`** in the top clock bar.
2. **Send Single Multi-PO Delay Email:**
   - As **`sup2`** in **Mailroom**, send a new email to **`inventory_manager`**, CC **`lex`**:
     - **Subject:** `Schedule update for PO-1041 and PO-1039`
     - **Body:** `Both shipments from our side are delayed due to Diwali transport backlog. New ETA for both PO-1041 and PO-1039 is 2026-12-05.`
3. **Point Out on Screen:**
   - In **PO Tracker**: `PO-1041` (`CRITICAL`) moves to **`DELAYED_CRITICAL`** and Lex drafts an **`EXPEDITE_REQUEST`** (CC `inventory_manager` + **`procurement`**), while `PO-1039` (`STANDARD`) quietly updates `revised_eta` to `2026-12-05` in **`ACKNOWLEDGED`** without alert noise.

---

## 5. `SCN-19` — Multimodal PDF Triage + Incremental Tracking Completion
**Pain Point:** Carrier names sit inside PDF dispatch advices and tracking numbers arrive separately later.

1. **Reset & Acknowledge `PO-1039` / New `VLV-204` PO:**
   - Click **`Reset Seed`**.
   - In **Mailroom** as **`making`**, request `2 pcs VLV-204` (`CC: lex`) → approve withdrawal + approve `PO-1041` dispatch in **Actionable Alerts**.
   - Switch to **`sup2`** in **Mailroom** → reply on `PO-1041`:
     - **Subject:** `Re: PO-1041`
     - **Body:** `Confirmed PO-1041, price and Nov 30 delivery look fine.`
   - Click **`+1d`** in the top clock bar.
2. **Send Dispatch Advice PDF (with Carrier inside PDF):**
   - Wait/note: `docs/test-scenarios/fixtures/dispatch_advice_sup2_po1045.pdf` references `PO-1045` (`BRG-402`). For a clean live match on `PO-1041` (`VLV-204`), either use an exported 1-page PDF mentioning `PO-1041` & `Carrier: SafeExpress` (or compose as `sup2` with `Material for PO-1041 left our gate today via SafeExpress, AWB will follow` — or if `PO-1045` was created, attach `dispatch_advice_sup2_po1045.pdf` directly!).
   - Show in **PO Tracker** → `PO-1041` drawer: `carrier_name = SafeExpress` and `actual_dispatch_date` populated automatically while `tracking_number` remains pending.
3. **Send Follow-Up AWB Email:**
   - Reply on the same `sup2` thread: `AWB for PO-1041 is SE-901122.`
4. **Point Out on Screen:** `PO-1041` logistics status automatically flips to **`COMPLETE`** with `SafeExpress` + `SE-901122` and zero overwriting of `actual_dispatch_date`.

---

## 6. `SCN-30` — Multi-PDF Overbilled Invoice + Credit Note Net Settlement
**Pain Point:** Suppliers attach both an overbilled invoice and a credit note in one email instead of re-issuing a single invoice.

1. **Reset & Size `PO-1041` (`VLV-204`, `qty=5`) to Match the Fixture PDF:**
   - Click **`Reset Seed`**.
   - Go to **Inventory & Parts** → find **`VLV-204`** → click **`Edit ROP / S`** → set **`ROP = 4`**, **`S = 8`** → Save.
   - Go to **Mailroom** (`making`) → send to `inventory_manager` (`CC: lex`): `Need 2 pcs VLV-204 today.`
   - In **Actionable Alerts**, approve withdrawal + approve `PO-1041` dispatch (`qty=5`, `₹1,500`, total **`₹7,500`**).
   - In **Mailroom** (`sup2`), acknowledge `PO-1041` (`Confirmed PO-1041 BlueDart BD-30.`).
   - Go to **PO Tracker** → open **`PO-1041`** → click **Log GRN** (`Received Qty = 5`, **`Condition = GOOD`**).
2. **Send Multi-PDF Email (`Invoice` + `Credit Note`):**
   - In **Mailroom** (`sup2`), compose to **`inventory_manager`**, CC **`lex`**, linked to **`PO-1041`**:
     - **Subject:** `Invoice BF-4801 + Credit Note CN-BF-109 for PO-1041`
     - **Body:** `Attached BF-4801 (₹9,000) and Credit Note CN-BF-109 (-₹1,500) bringing net payable to ₹7,500 as per PO-1041.`
     - **Attachments (attach both files):**
       - `docs/test-scenarios/fixtures/inv_sup2_overbilled_v1.pdf`
       - `docs/test-scenarios/fixtures/cn_sup2_credit_note.pdf`
3. **Point Out on Screen (Actionable Alerts):**
   - Show the **`INVOICE_WITH_CREDIT_NOTE_REVIEW`** alert displaying both attached PDFs and **`₹9,000.00 + (-₹1,500.00) = Net ₹7,500.00 vs. PO ₹7,500.00`**.
   - Click **`Approve Net Settlement & Release Stock`** → **`PO-1041` closes** (`CLOSED`) and `VLV-204` `on_hand_stock` increases by `5`.

---

## 7. `SCN-31` — Damaged Shipment GRN Quarantine Gate
**Pain Point:** A numerically clean PDF invoice should never release physically damaged goods into usable production stock.

1. **Reset & Size `PO-1041` (`BRG-402`, `qty=12`) to Match the Fixture PDF:**
   - Click **`Reset Seed`**.
   - Go to **Inventory & Parts** → find **`BRG-402`** → click **`Edit ROP / S`** → set **`ROP = 5`**, **`S = 14`** → Save.
   - Go to **Mailroom** (`making`) → send to `inventory_manager` (`CC: lex`): `Need 2 pcs BRG-402 today.`
   - In **Actionable Alerts**, approve withdrawal + approve `PO-1041` dispatch (`qty=12`, `₹450`, total `₹5,400`).
   - In **Mailroom** (`sup1`), acknowledge `PO-1041` (`Confirmed PO-1041 BlueDart BD-31.`).
   - Go to **PO Tracker** → open **`PO-1041`** → click **Log GRN** (`Received Qty = 12`, **`Condition = DAMAGED`**).
   - Point out in **Inventory & Parts**: `BRG-402` immediately shows **`Quarantined: 12`** (`On-Hand: 2`).
2. **Send Numerically Clean PDF Invoice:**
   - In **Mailroom** (`sup1`), compose to **`inventory_manager`**, CC **`lex`**, linked to **`PO-1041`**:
     - **Subject:** `Invoice APX-2026-930 for PO-1041`
     - **Body:** `Attached invoice APX-2026-930 for PO-1041.`
     - **Attachment:** `docs/test-scenarios/fixtures/inv_sup1_damaged_shipment.pdf`
3. **Point Out on Screen & Resolve (Actionable Alerts):**
   - Even though the invoice is **`MATCHED`** (`12 × ₹450`), `PO-1041` does **not** close and all `12` damaged units stay quarantined (`on_hand` stays `2`).
   - Open the **`DAMAGED_GOODS_HOLD`** alert → click **`Draft Damage Claim / Replacement`** (shows `procurement` auto-CC'd) → enter **`8`** in the usable qty field and click **`Release Usable Qty`**.
   - Show in **Inventory & Parts**: `BRG-402` `on_hand` moves `2 → 10` (`+8` good units) and `quarantined_stock` clears to `0`.

---

## 8. `SCN-34` — Shop-Floor Stock Shortage & Minimum-Quantity Negotiation
**Pain Point:** Production teams request more stock than is on hand while replenishment is already in transit.

1. **Reset & Seed Open PO + Low Stock:**
   - Click **`Reset Seed`**.
   - In **Mailroom** (`making`), send to `inventory_manager` (`CC: lex`): `Need 2 pcs BRG-402 for Line 1.`
   - In **Actionable Alerts**, approve withdrawal (`on_hand` drops `4 → 2`) + approve `PO-1041` (`qty=4` incoming from `sup1`).
2. **Send Shortage Request:**
   - In **Mailroom** (`making`), send a new email to **`inventory_manager`**, CC **`lex`**:
     - **Subject:** `Urgent: 10 units BRG-402 for Line 2`
     - **Body:** `Urgent: Line 2 overhaul needs 10 units of BRG-402 immediately.`
3. **Negotiate Minimum Qty & Approve Partial Issue:**
   - Go to **Actionable Alerts** → show the **`STOCK_SHORTAGE`** alert (`Requested: 10`, `On-Hand: 2`, `Shortfall: 8`, plus incoming `PO-1041` ETA) and Lex's pre-drafted **`STOCK_REQUEST_CLARIFICATION`** email.
   - Click **`Approve & Send`** on the clarification draft.
   - Switch to **`making`** in **Mailroom** → reply on that thread:
     - `Understood, we can keep Line 2 running with a minimum of 2 units right now until the shipment arrives.`
   - Return to **Actionable Alerts** → click **`Approve & Issue`** (`2` units) → `BRG-402` moves cleanly `2 → 0` with Manager sign-off.

---

## 9. `SCN-40` — `.csv` BOM Attachment Multi-SKU Stock Request
**Pain Point:** Maintenance engineers attach BOM `.csv` spreadsheets instead of typing separate stock requests.

1. **Reset:** Click **`Reset Seed`**.
2. **Send BOM CSV from Making Team:**
   - Go to **Mailroom** → switch persona to **`making`**.
   - Compose to **`inventory_manager`** (`CC: lex` auto-locked):
     - **Subject:** `Line 3 Maintenance BOM`
     - **Body:** `Hi, please issue all parts listed in the attached BOM CSV for Line 3 maintenance tomorrow.`
     - **Attachment:** `docs/test-scenarios/fixtures/making_bom_line3.csv`
3. **Point Out on Screen:**
   - Go to **Actionable Alerts** → Lex parsed the `.csv` into **2 independent pending `StockRequest` alerts** (`BRG-402` and `GSK-512`), while **`on_hand_stock` remains unchanged** until the Manager clicks approve.
   - *(Optional bonus check)* Show in **Mailroom** that attaching `.xlsx` or `.zip` returns a clean `400` client error, while encrypted `locked_invoice.pdf` from a supplier safely returns `200` and raises `INVOICE_EXTRACTION_REVIEW`.

---

## 10. `SCN-48` — Dynamic ROP/S Planning & Supplier Reliability Scorecards
**Pain Point:** Static reorder points ignore actual supplier lateness and billing errors.

1. **Reset:** Click **`Reset Seed`**.
2. **Open Suppliers & Planning:**
   - Go to **Suppliers & Planning** tab.
   - Point out the 3 live Supplier Scorecards (`Sup1 Apex` high reliability vs. **`Sup2 Bharat`** penalized for late deliveries and v1 invoice mismatches; `Quoted LT` vs. `Effective LT` bars).
3. **Recalculate & Apply:**
   - Click **`Recalculate Planning`** (`0` LLM calls — 100% deterministic EWMA + safety stock math).
   - Expand a **`ROP_RECOMMENDATION`** card (e.g., `VLV-204` or `BLT-301`) showing 12-month demand history, effective lead time, and recommended `ROP` / `S`.
   - Click **`Apply Recommendation`** → show the updated `ROP` / `S` reflected immediately in **Inventory & Parts**.
   - *(Optional close)* Open **AI Activity & Health** to show live **Lex Draft Trust Metrics** (`APPROVED_AS_IS` / `APPROVED_EDITED` / `REJECTED`), **1-Click YAML Eval Runner**, and **Agent Activity Feed** with INR cost tracking.

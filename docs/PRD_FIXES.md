# Fixes & Gap Closure Product Requirements Document (`PRD_FIXES.md`)

**Document Status:** Implemented & Verified v1.0 (`F-1.1`–`F-4.3`, Verified 2026-11-21)  
**Builds On:** [`PRD.md`](../PRD.md) (v1.0) · [`PRD_P2.md`](phase2/PRD_P2.md) (v1.0)  
**Evidence Baseline:** [`results-scn-01-to-20.md`](test-scenarios/results-scn-01-to-20.md) & [`results-scn-21-to-50.md`](test-scenarios/results-scn-21-to-50.md)  
**Verification Report:** [`results-fixes-retest.md`](test-scenarios/results-fixes-retest.md) (`16/16` live scenarios `PASS`, `92` offline unit/integration tests `PASS`)  
**MVP Agent:** **Lex** (`gemini-3.5-flash-lite` via `google-genai` and `$GEMINI_API_KEY`)

---

## 1. Executive Summary & Live Test Findings

All 50 end-to-end stress scenarios (`SCN-01`–`SCN-50`) were executed live against `gemini-3.5-flash-lite` across two isolated test batches (**205 Gemini calls**, **291,208 tokens**, **₹2.6831 INR** total cost):

- **Batch 1 (`SCN-01`–`SCN-20`):** 15 `PASS`, 5 `PARTIAL / GAP`, 0 `FAIL`
- **Batch 2 (`SCN-21`–`SCN-50`):** 24 `PASS`, 4 `PARTIAL / GAP`, 2 `FAIL`
- **Overall (`SCN-01`–`SCN-50`):** **39 `PASS` (78%)**, **9 `PARTIAL / GAP` (18%)**, **2 `FAIL` (4%)**

While the deterministic core, Making-team stock request loop, 2-day logistics SLA, and 3-way match math held up strongly, live testing uncovered **4 concrete bugs** (including 2 critical safety/crash failures in `SCN-31` and `SCN-46`), **5 product/scope gaps**, and **3 prompt/calculation tuning issues**.

### Master Issue & Priority Matrix

| ID | Priority | Category | Title | Failing / Partial Scenarios |
|---|---|---|---|---|
| `BUG-01` | **P0 (Critical)** | Bug | Damaged GRN (`condition=DAMAGED`) released directly into usable `on_hand_stock` on clean invoice | `SCN-31` (`FAIL`) |
| `BUG-02` | **P0 (Critical)** | Bug | Password-protected PDF triggers unhandled `500 Internal Server Error` | `SCN-46` (`FAIL`) |
| `BUG-03` | **P1 (High)** | Bug | `EXPEDITE_REQUEST` drafts omit `procurement` from `cc_personas` | `SCN-11`, `SCN-20` |
| `BUG-04` | **P1 (High)** | Bug | `PO_DISPATCH` drafts hallucinate `USD` (`$`) instead of `INR` (`₹`) | `SCN-11`, `SCN-19`, `SCN-47` |
| `GAP-01` | **P1 (High)** | Scope Gap | Foreign currency (`USD`) treated numerically as `INR` with no mismatch guardrail | `SCN-08` |
| `GAP-02` | **P1 (High)** | Scope Gap | Post-ack commercial reversal stays `ACKNOWLEDGED` & `update_po` skips `REVISED_PO` draft | `SCN-06` |
| `GAP-03` | **P1 (High)** | Scope Gap | Mailroom `/api/mailroom/send` drops multiple attachments; credit-note netting unsupported | `SCN-30`, `SCN-44` |
| `GAP-04` | **P2 (Medium)** | Scope Gap | Non-invoice PDFs skipped during email triage; `.csv` BOMs & `.jpg` photos rejected `400` | `SCN-08`, `SCN-19`, `SCN-40`, `SCN-45` |
| `GAP-05` | **P2 (Medium)** | Scope Gap | Multi-PO supplier emails & consolidated multi-PO PDFs escalate instead of splitting | `SCN-12`, `SCN-28` |
| `TUNE-01` | **P2 (Medium)** | Tuning | `AMBIGUOUS_EMAIL` raises alert with `draft_comm_id=None` (no clarification draft to approve/reject) | `SCN-03` |
| `TUNE-02` | **P2 (Medium)** | Tuning | `total_cost_impact_inr` on combined price+qty exception ignores quantity delta | `SCN-01` |
| `TUNE-03` | **P3 (Low)** | Tuning | Date-only ETA normalization (`17:00:00Z`) shifts `ETA+24h` `OVERDUE` check vs `09:00:00Z` | `SCN-14` |

### Non-Negotiable Architecture Guardrails Preserved
All fixes in this PRD must continue to satisfy:
1. **Strict Module Decoupling:** `inventory/`, `po/`, `mailroom/`, `planning/`, and `agents/` never import from each other; only `orchestrator/` composes workflows.
2. **Zero Direct DB Mutation by Lex:** Lex returns strict Pydantic schemas; deterministic code performs all arithmetic, currency checks, and stock/PO mutations.
3. **Draft-First MVP Rule & Zero Silent Actions:** Every Lex outbound email enters `DRAFT_PENDING_APPROVAL`, and every autonomous PO/quarantine action creates an `ActionItem` alert.
4. **Simulated Clock Fidelity:** All timestamps and SLA checks use `core.clock.now()`.

---

## 2. Workstream 1 — Critical Safety & Correctness Bugs (`BUG-01`–`BUG-04`)

### `F-1.1` Damaged Goods Quarantine Gate (`BUG-01` — Fixes `SCN-31`)
- **Problem Observed:** In `SCN-31`, logging `GRN(received_qty=12, condition="DAMAGED")` set `GRN.status=HELD_QUARANTINE` and `PO.status=RECEIVED_PENDING_INVOICE`. When a numerically clean PDF invoice (`12 × ₹450`) arrived, 3-way matching returned `MATCHED` (`stock_action="RELEASE_DIRECT"`), closed `PO-1041` (`CLOSED`), and added all 12 damaged units into usable `on_hand_stock` (`2 → 14`).
- **Requirements:**
  1. Treat `grn.condition` values other than `"GOOD"` / `"OK"` (specifically `"DAMAGED"`) as a **physical quality hold** independent of invoice math.
  2. When a GRN is logged with `condition="DAMAGED"`, immediately place `received_qty` into `part.quarantined_stock` (or hold in quarantine upon receipt/match—ensuring zero double-counting) and **never** emit `RELEASE_DIRECT` / `RELEASE_FROM_QUARANTINE` into usable `on_hand_stock` automatically, even if the invoice passes 3-way match.
  3. When an invoice matches numerically against a `DAMAGED` GRN:
     - Record `Invoice.match_status = MATCHED` (so the invoice audit is accurate—do not falsely blame billing),
     - Keep `GRN.status = HELD_QUARANTINE` and `received_qty` in `quarantined_stock`,
     - Keep `PO` open in `QUARANTINED_INVOICE_MISMATCH` (or dedicated quality-hold state / review status—never `CLOSED`),
     - Create a `HIGH` / `CRITICAL` **`DAMAGED_GOODS_HOLD`** `ActionItem` alert stating: *"Invoice numbers match PO, but GRN is marked DAMAGED — units remain quarantined until Manager inspection/disposition."*
  4. Provide Manager inline CTAs on the alert:
     - **Release Usable Qty** (accepts partial or full `good_qty` into `on_hand_stock`, adjusts/releases quarantine),
     - **Draft Damage Claim / Replacement Request** (generates `DRAFT_PENDING_APPROVAL` email to supplier CC'ing `inventory_manager` & `procurement`),
     - **Escalate to Procurement**.

### `F-1.2` Encrypted, Corrupt & Unreadable PDF Resilience (`BUG-02` — Fixes `SCN-46`)
- **Problem Observed:** In `SCN-46`, `.xlsx`, `.zip`, and 0-byte PDFs failed cleanly with `400`, but `locked_invoice.pdf` (valid `%PDF-1.4` header with `/Encrypt` dictionary) triggered an unhandled `500 Internal Server Error`.
- **Requirements:**
  1. Before calling Gemini multimodal extraction (and inside PDF validation / `extract_invoice` exception handling), inspect PDF readability/encryption (e.g., via `pypdf` / header + `/Encrypt` check or catching Gemini `ClientError` / PDF parse errors).
  2. Never allow an unreadable or password-protected PDF to return `500`.
  3. **Expected Behavior (Accept Email + Safe Escalation):** Store the inbound supplier email in the Mailroom so the message is never lost, skip/abort automated extraction cleanly, leave PO/stock untouched, and create a `HIGH` **`INVOICE_EXTRACTION_REVIEW`** alert explaining: *"Attached PDF (`<filename>`) is password-protected or unreadable and could not be parsed automatically."* (plus an optional `DRAFT_PENDING_APPROVAL` reply asking the supplier for an unlocked PDF).

### `F-1.3` Mandatory `procurement` CC on `EXPEDITE_REQUEST` (`BUG-03` — Fixes `SCN-11`, `SCN-20`)
- **Problem Observed:** In `SCN-11` (`CRITICAL_DELAY` alert with `assigned_team="Procurement"`), Lex's `EXPEDITE_REQUEST` draft (`scn-11.json`) had `cc_personas=["inventory_manager"]` without `"procurement"`.
- **Requirements:**
  1. Pass `required_cc_personas` (or explicit CC policy context) into `LexAgent.draft_email()` and enforce deterministically in `orchestrator/` that **`EXPEDITE_REQUEST`** drafts (and any critical escalation drafts targeting suppliers) **always** include both `"inventory_manager"` and `"procurement"` in `cc_personas` (deduplicated).
  2. Update `DRAFT_SYSTEM_PROMPT` to explain when `procurement` must be CC'd.

### `F-1.4` Explicit `INR` Currency Grounding in Draft Prompts (`BUG-04` — Fixes `SCN-11`, `SCN-19`, `SCN-47`)
- **Problem Observed:** Raw prompts in `scn-11.json`, `scn-19.json`, and `scn-47.json` passed numeric `unit_price` and `total_value` with no `currency` field, causing Gemini to repeatedly draft `"$600.00"` / `"1500.0 USD"` instead of `INR` / `₹`.
- **Requirements:**
  1. Include `currency: "INR"` (sourced from `PartSupplierTerm.currency`, defaulting to `"INR"`) in `EmailDraftInput` / `context_facts` for all PO-related drafts (`PO_DISPATCH`, `REVISED_PO`, `INVOICE_DISCREPANCY`, `EXPEDITE_REQUEST`, etc.).
  2. Update `DRAFT_SYSTEM_PROMPT` with an explicit rule: **always express monetary amounts in the PO's currency (`INR` / `₹`), never `USD` or `$` unless the PO currency itself is foreign**.

---

## 3. Workstream 2 — Commercial, State-Machine & Triage Gaps (`GAP-01`, `GAP-02`, `GAP-05`)

### `F-2.1` Foreign Currency Detection & Mismatch Guardrail (`GAP-01` — Fixes `SCN-08`)
- **Problem Observed:** In `SCN-08`, `sup3` quoted `"18.50 per unit (imported batch, USD billing)"`. Because `EmailTriageResult` lacked a currency field, Lex extracted `proposed_unit_price=18.5` (`conf=0.95`), and the alert displayed `Price: ₹100.00 → ₹18.50 (-81.5%)` with zero `USD` warning.
- **Requirements:**
  1. Add `proposed_currency: str | None = None` (ISO code such as `"INR"`, `"USD"`, `"EUR"`) to `EmailTriageResult`, and instruct `TRIAGE_SYSTEM_PROMPT` to extract currency whenever a supplier quotes a price.
  2. Add a deterministic **Currency Mismatch Guardrail** in `orchestrator/`:
     - Compare `proposed_currency` (when non-null) against the PO's contracted currency (`"INR"`).
     - If `proposed_currency != "INR"`:
       - Do **not** present the delta as a straight INR discount (`₹100 → ₹18.50`) or allow `update_po` to blindly overwrite `PO.unit_price` with `18.50`.
       - Raise a `HIGH` **`CURRENCY_MISMATCH`** / `PO_EXCEPTION` alert explicitly stating: *"Supplier quoted 18.50 USD vs PO currency INR — FX conversion or INR re-quote required before updating PO price."*
       - Include CTAs: **Request INR Quote** (drafts reply rejecting foreign-currency billing), **Switch to Backup Supplier**, **Cancel PO**.

### `F-2.2` Post-Acknowledgment Commercial Reversal Flow (`GAP-02` — Fixes `SCN-06`)
- **Problem Observed:** In `SCN-06`, after `PO-1041` was `ACKNOWLEDGED`, `sup2` reversed the price (`₹1,500 → ₹1,780`). The alert fired and `update_po` updated `unit_price=1780.0`, **but** `PO-1041` stayed in `ACKNOWLEDGED` and `update_po` returned `draft_comm_id=None` (no `REVISED_PO` draft generated).
- **Requirements:**
  1. Update `backend/po/state_machine.py` to permit `ACKNOWLEDGED → EXCEPTION_REVIEW` (and `DELAYED_CRITICAL → EXCEPTION_REVIEW` if a supplier changes price/qty while delayed).
  2. When a post-ack email is triaged as `PRICE_CHANGE`, `PARTIAL_AVAILABILITY`, or `OUT_OF_STOCK`, transition the PO into `EXCEPTION_REVIEW` (preserving `acknowledged_at` and `promised_eta` for auditability).
  3. Ensure `execute_alert_cta(..., action="update_po")` consistently transitions `EXCEPTION_REVIEW → PENDING_DISPATCH_APPROVAL` and generates a `REVISED_PO` `DRAFT_PENDING_APPROVAL` email—whether the exception happened before or after initial acknowledgment.

### `F-2.3` Multi-PO Email Splitting & Multi-PO Invoice Escalation (`GAP-05` — Fixes `SCN-12`, `SCN-28`)
- **Problem Observed:**
  - In `SCN-12`, a single email reporting delays on two POs (`PO-1041` `CRITICAL` and `PO-1039` `STANDARD`) escalated as `AMBIGUOUS` (`conf=0.50`) because `EmailTriageResult` only holds a single `matched_po_number`.
  - In `SCN-28`, a consolidated 2-PO PDF invoice escalated cleanly (`po_number=None`), protecting both POs from false quarantine, though without explicitly listing both detected POs in the alert.
- **Requirements:**
  1. **Multi-PO Supplier Emails (`SCN-12`):** Extend `EmailTriageResult` with an optional flat list `additional_po_updates` (or a structured `po_updates` list while preserving backward-compatible top-level fields / schema safety without `anyOf` issues) OR support iterative per-PO resolution in `orchestrator/` so when a supplier explicitly cites `PO-A` and `PO-B` in one email, each PO undergoes its own Scenario 1A/2 evaluation (`PO-1041` → `DELAYED_CRITICAL` expedite alert; `PO-1039` → `ACKNOWLEDGED` quiet `revised_eta` update if buffered). Fall back to `AMBIGUOUS` only when POs are ambiguous/unresolvable.
  2. **Consolidated Multi-PO PDF Invoices (`SCN-28`):** Add `referenced_po_numbers: list[str]` to `InvoiceExtractionResult` so Lex explicitly reports all detected PO numbers (e.g., `["PO-1041", "PO-1042"]`) in the `INVOICE_EXTRACTION_REVIEW` alert (*"Consolidated invoice references multiple POs (PO-1041, PO-1042). Single-PO 3-way match requires separate per-PO invoices."*), optionally drafting a polite request for split invoices.

---

## 4. Workstream 3 — Multi-Attachment & Multimodal Document Gaps (`GAP-03`, `GAP-04`)

### `F-3.1` Multi-Attachment Mailroom Support & Credit-Note Awareness (`GAP-03` — Fixes `SCN-30`, `SCN-44`)
- **Problem Observed:** `POST /api/mailroom/send` accepted only a single `attachment` file, silently dropping the overbilled invoice in `SCN-30` when both Invoice + Credit Note PDFs were sent, and limiting `SCN-44` to 1 file. Additionally, Lex extracted `cn_sup2_credit_note.pdf` as an invoice (`-₹1,500`), causing confusing discrepancy math.
- **Requirements:**
  1. **Multi-Attachment Mailroom (`EmailTransport`, DB & UI):**
     - Support uploading and storing multiple attachments per email (`attachments: list[UploadFile]` on `POST /api/mailroom/send`; `attachment_paths_json` alongside backward-compatible `attachment_path` on `POCommunication`; multi-file upload chip list in `MailroomPage.tsx`).
  2. **Multi-PDF Processing:**
     - When an email contains multiple `.pdf` attachments, extract/classify each PDF (e.g., `TAX_INVOICE`, `CREDIT_NOTE`, `PACKING_LIST`, `DISPATCH_ADVICE`, `QUOTE`).
     - In `SCN-44` (Invoice PDF + Packing List PDF): run 3-way match on the Tax Invoice, cross-check Packing List shipped qty against Invoice & GRN, and highlight any Packing List vs. Invoice conflict in the alert!
     - In `SCN-30` (Overbilled Invoice PDF + Credit Note PDF): add `document_type` (`INVOICE` | `CREDIT_NOTE` | `OTHER`) to `InvoiceExtractionResult`; **never** feed a standalone `CREDIT_NOTE` into 3-way unit-price math as if it were a billed invoice. If both Invoice + Credit Note arrive together, compute net adjusted amount (`invoice_total + credit_note_total`, where credit note is negative) or surface a structured **`INVOICE_WITH_CREDIT_NOTE_REVIEW`** alert showing `Invoice: ₹9,000 + Credit Note: -₹1,500 = Net ₹7,500 vs PO ₹7,500` with a 1-click **Approve Net Settlement & Release Stock** CTA!

### `F-3.2` Attachment-Aware Email Triage for Non-Invoice PDFs (`GAP-04` — Fixes `SCN-08`, `SCN-19`)
- **Problem Observed:** In `SCN-19` (`dispatch_advice_sup2_po1045.pdf` containing `Carrier: SafeExpress`) and `SCN-08` (`quote_sup3_usd_po1040.pdf`), Lex received only `has_pdf_attachment: true` during `email_triage` and never saw the PDF contents, missing `SafeExpress` and the PDF USD quote details.
- **Requirements:**
  1. When an inbound supplier email includes `.pdf` attachment(s) and is **not** handled solely as an `INVOICE_SUBMISSION` (or when `email_triage` sees `has_pdf_attachment=true` with logistics/quote cues), pass the PDF attachment bytes directly into `triage_email` (multimodal Gemini call, bounded to reasonable page/file size) so Lex extracts `carrier_name`, `tracking_number`, `actual_dispatch_date`, `promised_dispatch_date`, `proposed_unit_price`, and `proposed_currency` directly from attached dispatch advices, waybills, and quotes!

### `F-3.3` Making `.csv` BOMs & Shipment Damage `.jpg`/`.png` Support (`GAP-04` — Fixes `SCN-40`, `SCN-45`, `SCN-46`)
- **Problem Observed:** `save_pdf_attachment()` rejected `.csv`, `.jpg`, `.xlsx`, and `.zip` uniformly with `400: Only .pdf attachments are supported in MVP.`, blocking legitimate Making CSV BOMs (`SCN-40`) and transit damage photos (`SCN-45`).
- **Requirements:**
  1. Replace single-extension `.pdf` lock with an explicit **Allowed Mailroom Attachment Policy**:
     - **`.pdf`**: Allowed for all personas (invoices, quotes, waybills, dispatch advice).
     - **`.csv`**: Allowed for `making` (and `inventory_manager`) stock requests. When `making` sends a `.csv` BOM attachment (`CC: lex`), parse/sanitize the CSV text (bounded row count) and pass it into `triage_stock_request()` so Lex extracts all BOM SKUs & quantities into independent `StockRequest` items (still requiring Manager approval before deducting any stock!).
     - **`.jpg` / `.jpeg` / `.png`**: Allowed as evidentiary attachments (e.g., damaged shipment photos in `SCN-45`). Store safely for Mailroom viewing and surface a `HIGH` manual-inspection / damage alert (or optional multimodal image summary in `email_triage` without crashing).
     - **Unsupported / Dangerous Formats (`.xlsx`, `.zip`, `.exe`, etc.) & 0-byte files:** Continue rejecting cleanly with descriptive `400` client validation feedback (never `500`).

---

## 5. Workstream 4 — Prompt, Calculation & SLA Tuning (`TUNE-01`–`TUNE-03`)

### `F-4.1` Optional Clarification Draft on Ambiguous Escalations (`TUNE-01` — Fixes `SCN-03`)
- **Problem Observed:** In `SCN-03`, `AMBIGUOUS_EMAIL` (`conf=0.65`) set `draft_comm_id=None`, preventing the Manager from approving, editing, or rejecting a follow-up clarification email directly from the alert.
- **Requirements:**
  1. When `AMBIGUOUS_EMAIL` is triggered on an identifiable sender/PO (or `AMBIGUOUS_STOCK_REQUEST` from `making`), generate an optional `DRAFT_PENDING_APPROVAL` clarification draft (`EmailCategory.GENERAL` or `STOCK_REQUEST_CLARIFICATION`) asking specifically for the missing/ambiguous details (e.g., firm unit price, quantity, ETA, or SKU).
  2. Attach `draft_comm_id` to the alert so the Manager can **Approve & Send**, **Edit & Send**, or **Reject** (correctly updating `autonomy_policies` feedback metrics).

### `F-4.2` Accurate Total Spend Delta on Combined Price + Qty Exceptions (`TUNE-02` — Fixes `SCN-01`)
- **Problem Observed:** In `SCN-01`, `PO-1038` changed from `10 × ₹100` (`₹1,000`) to `15 × ₹122` (`₹1,830`), but the alert displayed `+₹330.00` (`22 × 15`) instead of `+₹830.00`.
- **Requirements:**
  1. In `orchestrator/` exception payload calculation:
     - `price_variance_inr = round(new_price - old_price, 2)`
     - `price_only_impact_inr = round((new_price - old_price) * effective_qty, 2)`
     - `total_cost_impact_inr = round((new_price * new_qty) - (old_price * old_qty), 2)`
  2. Display both unit price delta and total PO spend delta clearly on `AlertsPage.tsx` when `qty` also changes.

### `F-4.3` Consistent Date-Only ETA Normalization (`TUNE-03` — Fixes `SCN-14`)
- **Problem Observed:** Date-only ETA strings (`"2026-11-22"`) normalized to `17:00:00Z`, whereas `SimulatedClock` starts at `09:00:00Z`, causing `ETA + 24h` (`OVERDUE`) to fire at `17:00Z` instead of the expected `09:00Z` / business-day boundary (`+3d 2h` vs `+3d 10h`).
- **Requirements:**
  1. Standardize date-only ETA parsing in `orchestrator/` (e.g., align date-only ETAs to `09:00:00Z` or document explicit `17:00:00Z` EOD semantics consistently across `seed/data.py`, `sla_watcher.py`, and `alerts`/`POTracker` ETA displays) so `ETA + 1d` triggers predictably when advancing clock days from `SEED_START_TIME` (`09:00:00Z`).

---

## 6. Acceptance Criteria & Verification Plan

This fixes PRD is **complete** when:
1. **All 12 Workstream Items (`F-1.1`–`F-4.3`)** are implemented with zero violations of `tests/test_architecture.py` (no sideways imports, no wall-clock `datetime.now()`, all mailroom traffic behind `EmailTransport`).
2. **Offline Test & Build Gates Pass:**
   - `uv run pytest` passes 100% (including new unit/integration tests for `DAMAGED` GRN quarantine, encrypted PDF handling, `EXPEDITE_REQUEST` procurement CC, `INR` draft currency, `USD` triage guardrail, `ACKNOWLEDGED → EXCEPTION_REVIEW`, multi-attachment upload + credit-note review, non-invoice PDF triage, `.csv` BOM + `.jpg` support, multi-PO email splitting, and `AMBIGUOUS` clarification drafts).
   - `uv run ruff check backend tests` passes with zero errors.
   - `cd frontend && npm run build` passes with zero TypeScript/Vite errors (covering multi-attachment Mailroom upload/display, `DAMAGED_GOODS_HOLD` / `INVOICE_WITH_CREDIT_NOTE_REVIEW` / `CURRENCY_MISMATCH` alerts).
3. **Live Gemini Re-Verification (`SCN-01`–`SCN-50`):**
   - Re-running the **11 previously `FAIL` / `PARTIAL / GAP` scenarios** (`SCN-03`, `SCN-06`, `SCN-08`, `SCN-11`, `SCN-19`, `SCN-30`, `SCN-31`, `SCN-40`, `SCN-44`, `SCN-45`, `SCN-46`) plus `SCN-01`, `SCN-12`, `SCN-14`, `SCN-28`, `SCN-47` live against `gemini-3.5-flash-lite` achieves **`PASS`** on every scenario with zero `500` errors and zero regressions across the remaining 34 scenarios.

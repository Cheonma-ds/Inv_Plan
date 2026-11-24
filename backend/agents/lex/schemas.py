from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, model_validator

from backend.core.enums import EmailCategory


class EmailIntent(StrEnum):
    ACKNOWLEDGED = "ACKNOWLEDGED"
    PRICE_CHANGE = "PRICE_CHANGE"
    LEAD_TIME_CHANGE = "LEAD_TIME_CHANGE"
    PARTIAL_AVAILABILITY = "PARTIAL_AVAILABILITY"
    OUT_OF_STOCK = "OUT_OF_STOCK"
    POST_ACK_DELAY = "POST_ACK_DELAY"
    INVOICE_SUBMISSION = "INVOICE_SUBMISSION"
    GENERAL_REPLY = "GENERAL_REPLY"
    AMBIGUOUS = "AMBIGUOUS"


class OpenPOContext(BaseModel):
    po_number: str
    part_sku: str
    part_name: str
    qty: int
    unit_price: float
    currency: str = "INR"
    status: str
    quoted_lead_time_days: int
    promised_eta: str | None = None
    revised_eta: str | None = None
    promised_dispatch_date: str | None = None
    actual_dispatch_date: str | None = None
    carrier_name: str | None = None
    tracking_number: str | None = None
    dispatch_details_status: str | None = None


class ThreadMessageContext(BaseModel):
    from_persona: str
    subject: str
    body: str
    timestamp: str


class EmailTriageInput(BaseModel):
    sender_persona: str
    sender_name: str
    subject: str
    body: str
    has_pdf_attachment: bool = False
    has_image_attachment: bool = False
    attachment_filenames: list[str] = Field(default_factory=list)
    current_date: str
    open_pos: list[OpenPOContext] = Field(default_factory=list)
    thread_history: list[ThreadMessageContext] = Field(default_factory=list)
    supplier_memory_notes: list[str] = Field(default_factory=list)


class AdditionalPOUpdate(BaseModel):
    matched_po_number: str
    intent: EmailIntent
    confidence: float = Field(ge=0.0, le=1.0)
    proposed_unit_price: float | None = None
    proposed_currency: str | None = None
    proposed_qty: int | None = None
    proposed_eta_date: str | None = None
    promised_dispatch_date: str | None = None
    actual_dispatch_date: str | None = None
    carrier_name: str | None = None
    tracking_number: str | None = None
    details_promised_later: bool = False
    details_promised_days: int | None = Field(default=None, ge=0)
    details_promised_by_date: str | None = None
    delay_reason: str | None = None
    summary_for_manager: str = ""
    suggested_action: str = ""


class EmailTriageResult(BaseModel):
    matched_po_number: str | None = None
    intent: EmailIntent
    confidence: float = Field(ge=0.0, le=1.0)
    proposed_unit_price: float | None = None
    proposed_currency: str | None = None
    proposed_qty: int | None = None
    proposed_eta_date: str | None = None
    promised_dispatch_date: str | None = None
    actual_dispatch_date: str | None = None
    carrier_name: str | None = None
    tracking_number: str | None = None
    details_promised_later: bool = False
    details_promised_days: int | None = Field(default=None, ge=0)
    details_promised_by_date: str | None = None
    delay_reason: str | None = None
    summary_for_manager: str
    suggested_action: str
    new_memory_note: str | None = None
    reasoning: str
    additional_po_updates: list[AdditionalPOUpdate] = Field(default_factory=list)


class InvoiceLineItem(BaseModel):
    description: str
    part_sku: str | None = None
    qty: int
    unit_price: float
    line_total: float

    @model_validator(mode="before")
    @classmethod
    def _accept_part_code_alias(cls, data: Any) -> Any:
        if isinstance(data, dict) and "part_code" in data and "part_sku" not in data:
            copied = dict(data)
            copied["part_sku"] = copied.pop("part_code")
            return copied
        return data

    @property
    def part_code(self) -> str | None:
        return self.part_sku


class PDFDocumentType(StrEnum):
    INVOICE = "INVOICE"
    CREDIT_NOTE = "CREDIT_NOTE"
    PACKING_LIST = "PACKING_LIST"
    OTHER = "OTHER"


class InvoiceExtractionResult(BaseModel):
    document_type: PDFDocumentType = PDFDocumentType.INVOICE
    invoice_number: str
    po_number: str | None = None
    referenced_po_numbers: list[str] = Field(default_factory=list)
    supplier_name: str | None = None
    invoice_date: str | None = None
    currency: str = "INR"
    line_items: list[InvoiceLineItem] = Field(default_factory=list)
    subtotal: float
    tax_amount: float = 0.0
    total_amount: float
    confidence: float = Field(ge=0.0, le=1.0)
    notes: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _accept_design_aliases(cls, data: Any) -> Any:
        if isinstance(data, dict):
            copied = dict(data)
            if "po_reference" in copied and "po_number" not in copied:
                copied["po_number"] = copied.pop("po_reference")
            if "grand_total" in copied and "total_amount" not in copied:
                copied["total_amount"] = copied.pop("grand_total")
            return copied
        return data

    @property
    def po_reference(self) -> str | None:
        return self.po_number

    @property
    def grand_total(self) -> float:
        return self.total_amount


class EmailDraftInput(BaseModel):
    category: EmailCategory
    po_number: str = ""
    part_sku: str
    part_name: str
    qty: int
    unit_price: float = 0.0
    currency: str = "INR"
    recipient_persona: str
    recipient_name: str
    current_date: str
    thread_subject: str | None = None
    supplier_memory_notes: list[str] = Field(default_factory=list)
    required_cc_personas: list[str] = Field(default_factory=list)
    context_facts: dict[str, Any] = Field(default_factory=dict)


class EmailDraftResult(BaseModel):
    to_personas: list[str]
    cc_personas: list[str]
    subject: str
    body: str


class StockRequestIntent(StrEnum):
    STOCK_REQUEST = "STOCK_REQUEST"
    MINIMUM_QTY_CONFIRMATION = "MINIMUM_QTY_CONFIRMATION"
    GENERAL_MESSAGE = "GENERAL_MESSAGE"
    AMBIGUOUS = "AMBIGUOUS"


class StockRequestItem(BaseModel):
    part_sku: str | None = None
    part_name: str | None = None
    requested_qty: int | None = None


class StockPartContext(BaseModel):
    part_sku: str
    part_name: str
    criticality: str


class PendingStockRequestContext(BaseModel):
    part_sku: str
    part_name: str
    requested_qty: int
    status: str


class StockRequestTriageInput(BaseModel):
    sender_persona: str = "making"
    sender_name: str = "Making Team"
    subject: str
    body: str
    attachment_csv_text: str | None = None
    current_date: str
    catalog_parts: list[StockPartContext] = Field(default_factory=list)
    thread_history: list[ThreadMessageContext] = Field(default_factory=list)
    pending_requests: list[PendingStockRequestContext] = Field(default_factory=list)


class StockRequestTriageResult(BaseModel):
    intent: StockRequestIntent
    confidence: float = Field(ge=0.0, le=1.0)
    requested_items: list[StockRequestItem] = Field(default_factory=list)
    urgency_note: str | None = None
    summary_for_manager: str
    suggested_action: str
    reasoning: str


class ReminderWakeupInput(BaseModel):
    reminder_id: int
    po_id: int
    po_number: str
    part_sku: str
    part_name: str
    supplier_name: str
    current_date: str
    remind_at: str
    reminder_reason: str
    suggested_action: str = ""
    po_status: str
    grn_count: int = 0
    missing_dispatch_fields: list[str] = Field(default_factory=list)
    days_to_stockout: float | None = None
    thread_id: str | None = None


class ReminderWakeupResult(BaseModel):
    should_draft_followup: bool
    summary_for_manager: str
    followup_context_note: str = ""
    reasoning: str

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

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
    status: str
    quoted_lead_time_days: int
    promised_eta: str | None = None
    revised_eta: str | None = None


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
    current_date: str
    open_pos: list[OpenPOContext] = Field(default_factory=list)
    thread_history: list[ThreadMessageContext] = Field(default_factory=list)
    supplier_memory_notes: list[str] = Field(default_factory=list)


class EmailTriageResult(BaseModel):
    matched_po_number: str | None = None
    intent: EmailIntent
    confidence: float = Field(ge=0.0, le=1.0)
    proposed_unit_price: float | None = None
    proposed_qty: int | None = None
    proposed_eta_date: str | None = None
    delay_reason: str | None = None
    summary_for_manager: str
    suggested_action: str
    new_memory_note: str | None = None
    reasoning: str


class InvoiceLineItem(BaseModel):
    description: str
    part_sku: str | None = None
    qty: int
    unit_price: float
    line_total: float


class InvoiceExtractionResult(BaseModel):
    invoice_number: str
    po_number: str | None = None
    supplier_name: str | None = None
    invoice_date: str | None = None
    currency: str = "INR"
    line_items: list[InvoiceLineItem] = Field(default_factory=list)
    subtotal: float
    tax_amount: float = 0.0
    total_amount: float
    confidence: float = Field(ge=0.0, le=1.0)
    notes: str | None = None


class EmailDraftInput(BaseModel):
    category: EmailCategory
    po_number: str
    part_sku: str
    part_name: str
    qty: int
    unit_price: float
    recipient_persona: str
    recipient_name: str
    current_date: str
    thread_subject: str | None = None
    supplier_memory_notes: list[str] = Field(default_factory=list)
    context_facts: dict[str, Any] = Field(default_factory=dict)


class EmailDraftResult(BaseModel):
    to_personas: list[str]
    cc_personas: list[str]
    subject: str
    body: str

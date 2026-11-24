from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.core.db import Base


class SystemState(Base):
    __tablename__ = "system_state"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)


class Part(Base):
    __tablename__ = "parts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    sku: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    criticality: Mapped[str] = mapped_column(String(32), nullable=False)
    on_hand_stock: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    quarantined_stock: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rop: Mapped[int] = mapped_column(Integer, nullable=False)
    replenishment_point: Mapped[int] = mapped_column(Integer, nullable=False)
    target_coverage_days: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    manual_demand_override: Mapped[float | None] = mapped_column(Float, nullable=True)
    daily_burn_ewma: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    daily_burn_prev_7d: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    daily_demand_std: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    burn_carry: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    last_burn_settled_date: Mapped[str | None] = mapped_column(String(16), nullable=True)

    supplier_terms: Mapped[list["PartSupplierTerm"]] = relationship(
        back_populates="part", cascade="all, delete-orphan"
    )
    transactions: Mapped[list["InventoryTransaction"]] = relationship(
        back_populates="part", cascade="all, delete-orphan"
    )
    purchase_orders: Mapped[list["PurchaseOrder"]] = relationship(back_populates="part")


class Supplier(Base):
    __tablename__ = "suppliers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    persona_key: Mapped[str] = mapped_column(String(32), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    email: Mapped[str] = mapped_column(String(200), nullable=False)
    phone: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    reliability_score: Mapped[float] = mapped_column(Float, nullable=False, default=80.0)
    otd_rate: Mapped[float] = mapped_column(Float, nullable=False, default=0.85)
    invoice_accuracy_rate: Mapped[float] = mapped_column(Float, nullable=False, default=0.90)
    response_rate: Mapped[float] = mapped_column(Float, nullable=False, default=0.85)
    avg_response_days: Mapped[float] = mapped_column(Float, nullable=False, default=0.5)
    cycles_90d: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    memory_notes: Mapped[list["SupplierMemoryNote"]] = relationship(
        back_populates="supplier", cascade="all, delete-orphan"
    )
    part_terms: Mapped[list["PartSupplierTerm"]] = relationship(
        back_populates="supplier", cascade="all, delete-orphan"
    )
    purchase_orders: Mapped[list["PurchaseOrder"]] = relationship(back_populates="supplier")


class SupplierMemoryNote(Base):
    __tablename__ = "supplier_memory_notes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    supplier_id: Mapped[int] = mapped_column(
        ForeignKey("suppliers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    note: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    supplier: Mapped["Supplier"] = relationship(back_populates="memory_notes")


class PartSupplierTerm(Base):
    __tablename__ = "part_supplier_terms"
    __table_args__ = (
        UniqueConstraint("part_id", "supplier_id", name="uq_part_supplier"),
        UniqueConstraint("part_id", "supplier_rank", name="uq_part_supplier_rank"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    part_id: Mapped[int] = mapped_column(
        ForeignKey("parts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    supplier_id: Mapped[int] = mapped_column(
        ForeignKey("suppliers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    supplier_rank: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    agreed_unit_price: Mapped[float] = mapped_column(Float, nullable=False)
    currency: Mapped[str] = mapped_column(String(8), nullable=False, default="INR")
    quoted_lead_time_days: Mapped[int] = mapped_column(Integer, nullable=False)
    effective_lead_time_days: Mapped[float] = mapped_column(Float, nullable=False)
    lead_time_std_dev: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    moq: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    last_validated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    part: Mapped["Part"] = relationship(back_populates="supplier_terms")
    supplier: Mapped["Supplier"] = relationship(back_populates="part_terms")


class InventoryTransaction(Base):
    __tablename__ = "inventory_transactions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    part_id: Mapped[int] = mapped_column(
        ForeignKey("parts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    qty_delta: Mapped[int] = mapped_column(Integer, nullable=False)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    note: Mapped[str] = mapped_column(Text, nullable=False, default="")
    is_predicted_burn: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    part: Mapped["Part"] = relationship(back_populates="transactions")


class PurchaseOrder(Base):
    __tablename__ = "purchase_orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    po_number: Mapped[str] = mapped_column(String(32), unique=True, nullable=False, index=True)
    part_id: Mapped[int] = mapped_column(ForeignKey("parts.id"), nullable=False, index=True)
    supplier_id: Mapped[int] = mapped_column(ForeignKey("suppliers.id"), nullable=False, index=True)
    qty: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_price: Mapped[float] = mapped_column(Float, nullable=False)
    status: Mapped[str] = mapped_column(String(48), nullable=False, index=True)
    notes: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    acknowledged_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    promised_dispatch_date: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    actual_dispatch_date: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    carrier_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    tracking_number: Mapped[str | None] = mapped_column(String(120), nullable=True)
    dispatch_details_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="NONE"
    )
    dispatch_details_due_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    missing_dispatch_fields_json: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, default=list
    )
    escalated_to_procurement_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    procurement_escalation_note: Mapped[str] = mapped_column(Text, nullable=False, default="")
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    promised_eta: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revised_eta: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    follow_up_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    pre_eta_checked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    next_sla_check_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    part: Mapped["Part"] = relationship(back_populates="purchase_orders")
    supplier: Mapped["Supplier"] = relationship(back_populates="purchase_orders")
    communications: Mapped[list["POCommunication"]] = relationship(back_populates="po")
    goods_receipts: Mapped[list["GoodsReceipt"]] = relationship(
        back_populates="po", cascade="all, delete-orphan"
    )
    invoices: Mapped[list["Invoice"]] = relationship(
        back_populates="po", cascade="all, delete-orphan"
    )


class POCommunication(Base):
    __tablename__ = "po_communications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    po_id: Mapped[int | None] = mapped_column(
        ForeignKey("purchase_orders.id"), nullable=True, index=True
    )
    thread_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    direction: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    category: Mapped[str] = mapped_column(String(32), nullable=False, default="GENERAL")
    from_persona: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    to_personas: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    cc_personas: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    subject: Mapped[str] = mapped_column(String(300), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    attachment_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    attachment_paths_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    ai_classification: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ai_extracted_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    ai_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    approval_outcome: Mapped[str | None] = mapped_column(String(32), nullable=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)

    po: Mapped["PurchaseOrder | None"] = relationship(back_populates="communications")


class GoodsReceipt(Base):
    __tablename__ = "goods_receipts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    po_id: Mapped[int] = mapped_column(
        ForeignKey("purchase_orders.id", ondelete="CASCADE"), nullable=False, index=True
    )
    received_qty: Mapped[int] = mapped_column(Integer, nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    condition: Mapped[str] = mapped_column(String(64), nullable=False, default="GOOD")
    status: Mapped[str] = mapped_column(String(32), nullable=False)

    po: Mapped["PurchaseOrder"] = relationship(back_populates="goods_receipts")


class Invoice(Base):
    __tablename__ = "invoices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    po_id: Mapped[int] = mapped_column(
        ForeignKey("purchase_orders.id", ondelete="CASCADE"), nullable=False, index=True
    )
    comm_id: Mapped[int | None] = mapped_column(
        ForeignKey("po_communications.id"), nullable=True, index=True
    )
    invoice_number: Mapped[str] = mapped_column(String(64), nullable=False)
    pdf_path: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    extracted_qty: Mapped[int] = mapped_column(Integer, nullable=False)
    extracted_unit_price: Mapped[float] = mapped_column(Float, nullable=False)
    extracted_total: Mapped[float] = mapped_column(Float, nullable=False)
    match_status: Mapped[str] = mapped_column(String(32), nullable=False)
    discrepancy_summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    po: Mapped["PurchaseOrder"] = relationship(back_populates="invoices")


class ActionItem(Base):
    __tablename__ = "action_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    severity: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    urgency_score: Mapped[int] = mapped_column(Integer, nullable=False, default=50)
    po_id: Mapped[int | None] = mapped_column(
        ForeignKey("purchase_orders.id"), nullable=True, index=True
    )
    part_id: Mapped[int | None] = mapped_column(ForeignKey("parts.id"), nullable=True, index=True)
    draft_comm_id: Mapped[int | None] = mapped_column(
        ForeignKey("po_communications.id"), nullable=True
    )
    title: Mapped[str] = mapped_column(String(250), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    suggested_actions_json: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False, default=list
    )
    cta_payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="OPEN", index=True)
    assigned_team: Mapped[str] = mapped_column(String(64), nullable=False, default="Inventory")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    dismiss_resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class LexReminder(Base):
    __tablename__ = "lex_reminders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    po_id: Mapped[int | None] = mapped_column(
        ForeignKey("purchase_orders.id", ondelete="CASCADE"), nullable=True, index=True
    )
    part_id: Mapped[int | None] = mapped_column(
        ForeignKey("parts.id", ondelete="CASCADE"), nullable=True, index=True
    )
    supplier_id: Mapped[int | None] = mapped_column(
        ForeignKey("suppliers.id", ondelete="CASCADE"), nullable=True, index=True
    )
    comm_id: Mapped[int | None] = mapped_column(
        ForeignKey("po_communications.id", ondelete="SET NULL"), nullable=True, index=True
    )
    remind_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    context_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="PENDING", index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    triggered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class AgentTrace(Base):
    __tablename__ = "agent_traces"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    agent_name: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    task_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    trigger_summary: Mapped[str] = mapped_column(String(300), nullable=False)
    linked_entity_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    linked_entity_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    input_summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    output_summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    tool_calls_json: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False, default=list
    )
    raw_prompt: Mapped[str] = mapped_column(Text, nullable=False, default="")
    raw_response: Mapped[str] = mapped_column(Text, nullable=False, default="")
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost_inr: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    human_feedback: Mapped[str | None] = mapped_column(String(32), nullable=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)


class EvalRun(Base):
    __tablename__ = "eval_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    agent_name: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    suite_name: Mapped[str] = mapped_column(String(128), nullable=False)
    total_cases: Mapped[int] = mapped_column(Integer, nullable=False)
    passed_cases: Mapped[int] = mapped_column(Integer, nullable=False)
    pass_rate: Mapped[float] = mapped_column(Float, nullable=False)
    results_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    model_name: Mapped[str] = mapped_column(String(64), nullable=False)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)


class AutonomyPolicy(Base):
    __tablename__ = "autonomy_policies"
    __table_args__ = (
        UniqueConstraint("agent_name", "action_category", name="uq_autonomy_agent_category"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    agent_name: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    action_category: Mapped[str] = mapped_column(String(64), nullable=False)
    mode: Mapped[str] = mapped_column(String(32), nullable=False, default="APPROVAL_REQUIRED")
    approved_as_is_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    approved_edited_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rejected_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class StockRequest(Base):
    __tablename__ = "stock_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    comm_id: Mapped[int] = mapped_column(
        ForeignKey("po_communications.id", ondelete="CASCADE"), nullable=False, index=True
    )
    thread_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    part_id: Mapped[int] = mapped_column(
        ForeignKey("parts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    requested_qty: Mapped[int] = mapped_column(Integer, nullable=False)
    approved_qty: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="PENDING_REVIEW", index=True
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    analysis_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CopilotSession(Base):
    __tablename__ = "copilot_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False, default="New Copilot Chat")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )

    messages: Mapped[list["CopilotMessage"]] = relationship(
        back_populates="session",
        cascade="all, delete-orphan",
        order_by="CopilotMessage.id.asc()",
    )


class CopilotMessage(Base):
    __tablename__ = "copilot_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("copilot_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    structured_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    tool_calls_json: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False, default=list
    )
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    trace_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    session: Mapped["CopilotSession"] = relationship(back_populates="messages")

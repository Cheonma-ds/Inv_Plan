from typing import Any

from sqlalchemy.orm import Session

from backend.core.enums import ApprovalOutcome
from backend.mailroom.schemas import OutboundMessage, StoredEmail
from backend.mailroom.transport import (
    CommunicationNotFoundError,
    DatabaseEmailTransport,
    EmailTransport,
    InvalidDraftStateError,
)

__all__ = [
    "CommunicationNotFoundError",
    "InvalidDraftStateError",
    "annotate_triage",
    "approve_draft",
    "default_transport",
    "get_communication",
    "get_thread",
    "list_drafts",
    "list_inbox",
    "list_sent",
    "reject_draft",
    "save_draft",
    "send_email",
]

default_transport: EmailTransport = DatabaseEmailTransport()


def send_email(
    session: Session,
    msg: OutboundMessage,
    *,
    allow_agent_send: bool = False,
    transport: EmailTransport = default_transport,
) -> StoredEmail:
    return transport.send(session, msg, allow_agent_send=allow_agent_send)


def save_draft(
    session: Session,
    msg: OutboundMessage,
    *,
    transport: EmailTransport = default_transport,
) -> StoredEmail:
    return transport.save_draft(session, msg)


def list_inbox(
    session: Session,
    persona_key: str,
    *,
    transport: EmailTransport = default_transport,
) -> list[StoredEmail]:
    return transport.list_inbox(session, persona_key)


def list_sent(
    session: Session,
    persona_key: str,
    *,
    transport: EmailTransport = default_transport,
) -> list[StoredEmail]:
    return transport.list_sent(session, persona_key)


def list_drafts(
    session: Session,
    persona_key: str,
    *,
    transport: EmailTransport = default_transport,
) -> list[StoredEmail]:
    return transport.list_drafts(session, persona_key)


def get_thread(
    session: Session,
    thread_id: str,
    *,
    transport: EmailTransport = default_transport,
) -> list[StoredEmail]:
    return transport.get_thread(session, thread_id)


def get_communication(
    session: Session,
    comm_id: int,
    *,
    transport: EmailTransport = default_transport,
) -> StoredEmail:
    return transport.get_communication(session, comm_id)


def approve_draft(
    session: Session,
    comm_id: int,
    *,
    edited_subject: str | None = None,
    edited_body: str | None = None,
    transport: EmailTransport = default_transport,
) -> tuple[StoredEmail, ApprovalOutcome]:
    return transport.approve_draft(
        session,
        comm_id,
        edited_subject=edited_subject,
        edited_body=edited_body,
    )


def reject_draft(
    session: Session,
    comm_id: int,
    *,
    transport: EmailTransport = default_transport,
) -> StoredEmail:
    return transport.reject_draft(session, comm_id)


def annotate_triage(
    session: Session,
    comm_id: int,
    *,
    classification: str,
    extracted_json: dict[str, Any],
    confidence: float,
    po_id: int | None = None,
    transport: EmailTransport = default_transport,
) -> StoredEmail:
    return transport.annotate_triage(
        session,
        comm_id,
        classification=classification,
        extracted_json=extracted_json,
        confidence=confidence,
        po_id=po_id,
    )

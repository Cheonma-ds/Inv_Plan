from typing import Any

from sqlalchemy.orm import Session

from backend.core import clock
from backend.core.enums import ApprovalOutcome, CommStatus
from backend.core.models import POCommunication
from backend.mailroom.schemas import OutboundMessage, StoredEmail
from backend.mailroom.transport import DatabaseEmailTransport, EmailTransport, _to_stored_email


class CommunicationNotFoundError(ValueError):
    pass


class InvalidDraftStateError(ValueError):
    pass


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


def get_communication(session: Session, comm_id: int) -> StoredEmail:
    row = session.get(POCommunication, comm_id)
    if row is None:
        raise CommunicationNotFoundError(f"Communication {comm_id} not found")
    return _to_stored_email(row)


def approve_draft(
    session: Session,
    comm_id: int,
    *,
    edited_subject: str | None = None,
    edited_body: str | None = None,
) -> tuple[StoredEmail, ApprovalOutcome]:
    row = session.get(POCommunication, comm_id)
    if row is None:
        raise CommunicationNotFoundError(f"Draft communication {comm_id} not found")
    if row.status != CommStatus.DRAFT_PENDING_APPROVAL.value:
        raise InvalidDraftStateError(
            f"Communication {comm_id} is in status '{row.status}', not DRAFT_PENDING_APPROVAL"
        )

    was_edited = False
    if edited_subject is not None and edited_subject.strip() != row.subject.strip():
        row.subject = edited_subject.strip()
        was_edited = True
    if edited_body is not None and edited_body.strip() != row.body.strip():
        row.body = edited_body.strip()
        was_edited = True

    outcome = ApprovalOutcome.APPROVED_EDITED if was_edited else ApprovalOutcome.APPROVED_AS_IS
    row.status = CommStatus.SENT.value
    row.approval_outcome = outcome.value
    row.timestamp = clock.now(session)
    session.flush()
    return _to_stored_email(row), outcome


def reject_draft(session: Session, comm_id: int) -> StoredEmail:
    row = session.get(POCommunication, comm_id)
    if row is None:
        raise CommunicationNotFoundError(f"Draft communication {comm_id} not found")
    if row.status != CommStatus.DRAFT_PENDING_APPROVAL.value:
        raise InvalidDraftStateError(
            f"Communication {comm_id} is in status '{row.status}', not DRAFT_PENDING_APPROVAL"
        )

    row.status = CommStatus.REJECTED.value
    row.approval_outcome = ApprovalOutcome.REJECTED.value
    session.flush()
    return _to_stored_email(row)


def annotate_triage(
    session: Session,
    comm_id: int,
    *,
    classification: str,
    extracted_json: dict[str, Any],
    confidence: float,
    po_id: int | None = None,
) -> StoredEmail:
    row = session.get(POCommunication, comm_id)
    if row is None:
        raise CommunicationNotFoundError(f"Communication {comm_id} not found")
    row.ai_classification = classification
    row.ai_extracted_json = extracted_json
    row.ai_confidence = confidence
    if po_id is not None:
        row.po_id = po_id
    session.flush()
    return _to_stored_email(row)

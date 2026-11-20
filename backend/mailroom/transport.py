import uuid
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.core import clock
from backend.core.enums import ApprovalOutcome, CommDirection, CommStatus, PersonaKey
from backend.core.models import POCommunication
from backend.mailroom.schemas import OutboundMessage, StoredEmail

SUPPLIER_PERSONAS = {PersonaKey.SUP1.value, PersonaKey.SUP2.value, PersonaKey.SUP3.value}
DELIVERED_STATUSES = (CommStatus.SENT.value, CommStatus.RECEIVED.value)


class ManualLexSendForbiddenError(PermissionError):
    """Raised when a caller attempts to manually send an email as persona 'lex'."""


class CommunicationNotFoundError(ValueError):
    """Raised when a requested POCommunication record does not exist."""


class InvalidDraftStateError(ValueError):
    """Raised when attempting to approve or reject a non-draft communication."""


def _to_stored_email(row: POCommunication) -> StoredEmail:
    return StoredEmail(
        id=row.id,
        po_id=row.po_id,
        thread_id=row.thread_id,
        direction=row.direction,
        status=row.status,
        category=row.category,
        from_persona=row.from_persona,
        to_personas=list(row.to_personas or []),
        cc_personas=list(row.cc_personas or []),
        subject=row.subject,
        body=row.body,
        attachment_path=row.attachment_path,
        ai_classification=row.ai_classification,
        ai_extracted_json=row.ai_extracted_json,
        ai_confidence=row.ai_confidence,
        approval_outcome=row.approval_outcome,
        timestamp=clock.ensure_utc(row.timestamp),
    )


class EmailTransport(Protocol):
    def send(
        self,
        session: Session,
        msg: OutboundMessage,
        *,
        allow_agent_send: bool = False,
    ) -> StoredEmail: ...

    def save_draft(self, session: Session, msg: OutboundMessage) -> StoredEmail: ...

    def list_inbox(self, session: Session, persona_key: str) -> list[StoredEmail]: ...

    def list_sent(self, session: Session, persona_key: str) -> list[StoredEmail]: ...

    def list_drafts(self, session: Session, persona_key: str) -> list[StoredEmail]: ...

    def get_thread(self, session: Session, thread_id: str) -> list[StoredEmail]: ...

    def get_communication(self, session: Session, comm_id: int) -> StoredEmail: ...

    def approve_draft(
        self,
        session: Session,
        comm_id: int,
        *,
        edited_subject: str | None = None,
        edited_body: str | None = None,
    ) -> tuple[StoredEmail, ApprovalOutcome]: ...

    def reject_draft(self, session: Session, comm_id: int) -> StoredEmail: ...

    def annotate_triage(
        self,
        session: Session,
        comm_id: int,
        *,
        classification: str,
        extracted_json: dict[str, Any],
        confidence: float,
        po_id: int | None = None,
    ) -> StoredEmail: ...


class DatabaseEmailTransport:
    """Built-in 5-persona DB-backed EmailTransport synced to SimulatedClock."""

    def send(
        self,
        session: Session,
        msg: OutboundMessage,
        *,
        allow_agent_send: bool = False,
    ) -> StoredEmail:
        if msg.from_persona == PersonaKey.LEX and not allow_agent_send:
            raise ManualLexSendForbiddenError(
                "Manual sending from persona 'lex' is forbidden. "
                "Lex emails must be generated as drafts and approved by InventoryManager."
            )

        is_supplier = msg.from_persona.value in SUPPLIER_PERSONAS
        direction = CommDirection.INBOUND.value if is_supplier else CommDirection.OUTBOUND.value
        status = CommStatus.RECEIVED.value if is_supplier else CommStatus.SENT.value
        thread_id = msg.thread_id or f"thr-{uuid.uuid4().hex[:12]}"

        row = POCommunication(
            po_id=msg.po_id,
            thread_id=thread_id,
            direction=direction,
            status=status,
            category=msg.category.value,
            from_persona=msg.from_persona.value,
            to_personas=[p.value for p in msg.to_personas],
            cc_personas=[p.value for p in msg.cc_personas],
            subject=msg.subject.strip(),
            body=msg.body.strip(),
            attachment_path=msg.attachment_path,
            timestamp=clock.now(session),
        )
        session.add(row)
        session.flush()
        return _to_stored_email(row)

    def save_draft(self, session: Session, msg: OutboundMessage) -> StoredEmail:
        thread_id = msg.thread_id or f"thr-{uuid.uuid4().hex[:12]}"
        row = POCommunication(
            po_id=msg.po_id,
            thread_id=thread_id,
            direction=CommDirection.OUTBOUND.value,
            status=CommStatus.DRAFT_PENDING_APPROVAL.value,
            category=msg.category.value,
            from_persona=msg.from_persona.value,
            to_personas=[p.value for p in msg.to_personas],
            cc_personas=[p.value for p in msg.cc_personas],
            subject=msg.subject.strip(),
            body=msg.body.strip(),
            attachment_path=msg.attachment_path,
            timestamp=clock.now(session),
        )
        session.add(row)
        session.flush()
        return _to_stored_email(row)

    def list_inbox(self, session: Session, persona_key: str) -> list[StoredEmail]:
        PersonaKey(persona_key)  # validate persona
        rows = session.scalars(
            select(POCommunication)
            .where(POCommunication.status.in_(DELIVERED_STATUSES))
            .order_by(POCommunication.timestamp.desc(), POCommunication.id.desc())
        ).all()
        matched = [
            _to_stored_email(r)
            for r in rows
            if persona_key in (r.to_personas or []) or persona_key in (r.cc_personas or [])
        ]
        return matched

    def list_sent(self, session: Session, persona_key: str) -> list[StoredEmail]:
        PersonaKey(persona_key)
        rows = session.scalars(
            select(POCommunication)
            .where(
                POCommunication.from_persona == persona_key,
                POCommunication.status.in_(DELIVERED_STATUSES),
            )
            .order_by(POCommunication.timestamp.desc(), POCommunication.id.desc())
        ).all()
        return [_to_stored_email(r) for r in rows]

    def list_drafts(self, session: Session, persona_key: str) -> list[StoredEmail]:
        PersonaKey(persona_key)
        rows = session.scalars(
            select(POCommunication)
            .where(
                POCommunication.from_persona == persona_key,
                POCommunication.status == CommStatus.DRAFT_PENDING_APPROVAL.value,
            )
            .order_by(POCommunication.timestamp.desc(), POCommunication.id.desc())
        ).all()
        return [_to_stored_email(r) for r in rows]

    def get_thread(self, session: Session, thread_id: str) -> list[StoredEmail]:
        rows = session.scalars(
            select(POCommunication)
            .where(POCommunication.thread_id == thread_id)
            .order_by(POCommunication.timestamp.asc(), POCommunication.id.asc())
        ).all()
        return [_to_stored_email(r) for r in rows]

    def get_communication(self, session: Session, comm_id: int) -> StoredEmail:
        row = session.get(POCommunication, comm_id)
        if row is None:
            raise CommunicationNotFoundError(f"Communication {comm_id} not found")
        return _to_stored_email(row)

    def approve_draft(
        self,
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

    def reject_draft(self, session: Session, comm_id: int) -> StoredEmail:
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
        self,
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

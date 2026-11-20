import uuid
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.core import clock
from backend.core.enums import CommDirection, CommStatus, PersonaKey
from backend.core.models import POCommunication
from backend.mailroom.schemas import OutboundMessage, StoredEmail

SUPPLIER_PERSONAS = {PersonaKey.SUP1.value, PersonaKey.SUP2.value, PersonaKey.SUP3.value}
DELIVERED_STATUSES = (CommStatus.SENT.value, CommStatus.RECEIVED.value)


class ManualLexSendForbiddenError(PermissionError):
    """Raised when a caller attempts to manually send an email as persona 'lex'."""


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

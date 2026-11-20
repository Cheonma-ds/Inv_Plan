from pathlib import Path

import pytest

from backend.core.enums import ApprovalOutcome, CommDirection, CommStatus, EmailCategory, PersonaKey
from backend.mailroom import (
    CommunicationNotFoundError,
    InvalidAttachmentError,
    InvalidDraftStateError,
    ManualLexSendForbiddenError,
    OutboundMessage,
    StoredEmail,
    annotate_triage,
    approve_draft,
    get_communication,
    get_thread,
    list_drafts,
    list_inbox,
    list_sent,
    reject_draft,
    save_draft,
    save_pdf_attachment,
    send_email,
)


def test_send_routes_to_inbox_cc_and_sent(seeded_session):
    sent = send_email(
        seeded_session,
        OutboundMessage(
            from_persona=PersonaKey.INVENTORY_MANAGER,
            to_personas=[PersonaKey.SUP1],
            cc_personas=[PersonaKey.LEX],
            subject="Check stock for PART-001",
            body="Please confirm availability.",
        ),
    )

    assert sent.status == CommStatus.SENT.value
    assert any(m.id == sent.id for m in list_inbox(seeded_session, PersonaKey.SUP1.value))
    assert any(m.id == sent.id for m in list_inbox(seeded_session, PersonaKey.LEX.value))
    mgr_sent = list_sent(seeded_session, PersonaKey.INVENTORY_MANAGER.value)
    assert any(m.id == sent.id for m in mgr_sent)
    assert not any(m.id == sent.id for m in list_inbox(seeded_session, PersonaKey.SUP2.value))

    thread = get_thread(seeded_session, sent.thread_id)
    assert len(thread) == 1 and thread[0].id == sent.id


def test_manual_send_from_lex_is_forbidden(seeded_session):
    with pytest.raises(ManualLexSendForbiddenError):
        send_email(
            seeded_session,
            OutboundMessage(
                from_persona=PersonaKey.LEX,
                to_personas=[PersonaKey.SUP1],
                subject="Unauthorized manual Lex email",
                body="Should be blocked.",
            ),
        )


def test_draft_approval_as_is_edited_and_rejected(seeded_session):
    draft1 = save_draft(
        seeded_session,
        OutboundMessage(
            from_persona=PersonaKey.LEX,
            to_personas=[PersonaKey.SUP1],
            cc_personas=[PersonaKey.INVENTORY_MANAGER],
            subject="[PO-9001] Dispatch",
            body="Original draft body.",
            category=EmailCategory.PO_DISPATCH,
        ),
    )
    assert draft1.status == CommStatus.DRAFT_PENDING_APPROVAL.value
    assert any(m.id == draft1.id for m in list_drafts(seeded_session, PersonaKey.LEX.value))
    assert not any(m.id == draft1.id for m in list_inbox(seeded_session, PersonaKey.SUP1.value))

    approved1, outcome1 = approve_draft(seeded_session, draft1.id)
    assert outcome1 == ApprovalOutcome.APPROVED_AS_IS
    assert approved1.status == CommStatus.SENT.value
    assert any(m.id == draft1.id for m in list_inbox(seeded_session, PersonaKey.SUP1.value))

    draft2 = save_draft(
        seeded_session,
        OutboundMessage(
            from_persona=PersonaKey.LEX,
            to_personas=[PersonaKey.SUP2],
            subject="[PO-9002] Reminder",
            body="Original reminder.",
            category=EmailCategory.ACK_REMINDER,
        ),
    )
    approved2, outcome2 = approve_draft(
        seeded_session,
        draft2.id,
        edited_body="Edited reminder body with urgency.",
    )
    assert outcome2 == ApprovalOutcome.APPROVED_EDITED
    assert approved2.body == "Edited reminder body with urgency."

    draft3 = save_draft(
        seeded_session,
        OutboundMessage(
            from_persona=PersonaKey.LEX,
            to_personas=[PersonaKey.SUP3],
            subject="[PO-9003] Check-in",
            body="Skip me.",
            category=EmailCategory.PRE_ETA_CHECKIN,
        ),
    )
    rejected = reject_draft(seeded_session, draft3.id)
    assert rejected.status == CommStatus.REJECTED.value
    assert not any(m.id == draft3.id for m in list_inbox(seeded_session, PersonaKey.SUP3.value))


def test_save_pdf_attachment_validation(tmp_path: Path):
    saved = save_pdf_attachment(
        "Invoice #101.pdf",
        b"%PDF-1.4\n%fake-pdf-content\n",
        attachments_dir=tmp_path,
    )
    assert Path(saved).exists()
    assert saved.endswith(".pdf")

    with pytest.raises(InvalidAttachmentError):
        save_pdf_attachment("invoice.txt", b"not a pdf", attachments_dir=tmp_path)

    with pytest.raises(InvalidAttachmentError):
        save_pdf_attachment("fake.pdf", b"not really pdf bytes", attachments_dir=tmp_path)


def test_swappable_custom_email_transport_handles_all_mailroom_operations(db_session):
    """Verify P15: Every mailroom service function (including approve_draft, reject_draft,
    get_communication, and annotate_triage) delegates strictly through the EmailTransport
    protocol with zero direct SQLAlchemy queries in mailroom/service.py.
    """
    from datetime import datetime, timezone

    class InMemoryMockTransport:
        def __init__(self) -> None:
            self.store: dict[int, StoredEmail] = {}
            self._next_id = 1

        def _alloc(self, msg: OutboundMessage, status: str, direction: str) -> StoredEmail:
            cid = self._next_id
            self._next_id += 1
            stored = StoredEmail(
                id=cid,
                po_id=msg.po_id,
                thread_id=msg.thread_id or f"mock-thr-{cid}",
                direction=direction,
                status=status,
                category=msg.category.value,
                from_persona=msg.from_persona.value,
                to_personas=[p.value for p in msg.to_personas],
                cc_personas=[p.value for p in msg.cc_personas],
                subject=msg.subject,
                body=msg.body,
                attachment_path=msg.attachment_path,
                ai_classification=None,
                ai_extracted_json=None,
                ai_confidence=None,
                approval_outcome=None,
                timestamp=datetime(2026, 11, 20, 9, 0, tzinfo=timezone.utc),
            )
            self.store[cid] = stored
            return stored

        def send(
            self, session, msg: OutboundMessage, *, allow_agent_send: bool = False
        ) -> StoredEmail:
            if msg.from_persona == PersonaKey.LEX and not allow_agent_send:
                raise ManualLexSendForbiddenError("Lex manual send forbidden")
            return self._alloc(msg, CommStatus.SENT.value, CommDirection.OUTBOUND.value)

        def save_draft(self, session, msg: OutboundMessage) -> StoredEmail:
            return self._alloc(
                msg, CommStatus.DRAFT_PENDING_APPROVAL.value, CommDirection.OUTBOUND.value
            )

        def list_inbox(self, session, persona_key: str) -> list[StoredEmail]:
            return [
                e
                for e in self.store.values()
                if e.status in (CommStatus.SENT.value, CommStatus.RECEIVED.value)
                and (persona_key in e.to_personas or persona_key in e.cc_personas)
            ]

        def list_sent(self, session, persona_key: str) -> list[StoredEmail]:
            return [
                e
                for e in self.store.values()
                if e.status == CommStatus.SENT.value and e.from_persona == persona_key
            ]

        def list_drafts(self, session, persona_key: str) -> list[StoredEmail]:
            return [
                e
                for e in self.store.values()
                if e.status == CommStatus.DRAFT_PENDING_APPROVAL.value
                and e.from_persona == persona_key
            ]

        def get_thread(self, session, thread_id: str) -> list[StoredEmail]:
            return [e for e in self.store.values() if e.thread_id == thread_id]

        def get_communication(self, session, comm_id: int) -> StoredEmail:
            if comm_id not in self.store:
                raise CommunicationNotFoundError(f"Missing {comm_id}")
            return self.store[comm_id]

        def approve_draft(
            self,
            session,
            comm_id: int,
            *,
            edited_subject: str | None = None,
            edited_body: str | None = None,
        ) -> tuple[StoredEmail, ApprovalOutcome]:
            draft = self.get_communication(session, comm_id)
            if draft.status != CommStatus.DRAFT_PENDING_APPROVAL.value:
                raise InvalidDraftStateError("Not a draft")
            edited = bool(
                (edited_subject and edited_subject != draft.subject)
                or (edited_body and edited_body != draft.body)
            )
            outcome = (
                ApprovalOutcome.APPROVED_EDITED if edited else ApprovalOutcome.APPROVED_AS_IS
            )
            updated = draft.model_copy(
                update={
                    "subject": edited_subject or draft.subject,
                    "body": edited_body or draft.body,
                    "status": CommStatus.SENT.value,
                    "approval_outcome": outcome.value,
                }
            )
            self.store[comm_id] = updated
            return updated, outcome

        def reject_draft(self, session, comm_id: int) -> StoredEmail:
            draft = self.get_communication(session, comm_id)
            if draft.status != CommStatus.DRAFT_PENDING_APPROVAL.value:
                raise InvalidDraftStateError("Not a draft")
            updated = draft.model_copy(
                update={
                    "status": CommStatus.REJECTED.value,
                    "approval_outcome": ApprovalOutcome.REJECTED.value,
                }
            )
            self.store[comm_id] = updated
            return updated

        def annotate_triage(
            self,
            session,
            comm_id: int,
            *,
            classification: str,
            extracted_json: dict,
            confidence: float,
            po_id: int | None = None,
        ) -> StoredEmail:
            email = self.get_communication(session, comm_id)
            updated = email.model_copy(
                update={
                    "ai_classification": classification,
                    "ai_extracted_json": extracted_json,
                    "ai_confidence": confidence,
                    "po_id": po_id if po_id is not None else email.po_id,
                }
            )
            self.store[comm_id] = updated
            return updated

    mock_tx = InMemoryMockTransport()
    draft = save_draft(
        db_session,
        OutboundMessage(
            from_persona=PersonaKey.LEX,
            to_personas=[PersonaKey.SUP1],
            subject="Mock draft",
            body="Original body",
        ),
        transport=mock_tx,
    )
    assert len(list_drafts(db_session, PersonaKey.LEX.value, transport=mock_tx)) == 1

    approved, outcome = approve_draft(
        db_session,
        draft.id,
        edited_body="Updated body via custom transport",
        transport=mock_tx,
    )
    assert outcome == ApprovalOutcome.APPROVED_EDITED
    assert approved.status == CommStatus.SENT.value
    assert len(list_inbox(db_session, PersonaKey.SUP1.value, transport=mock_tx)) == 1

    annotated = annotate_triage(
        db_session,
        draft.id,
        classification="ACKNOWLEDGED",
        extracted_json={"ok": True},
        confidence=0.98,
        po_id=7,
        transport=mock_tx,
    )
    assert annotated.ai_classification == "ACKNOWLEDGED"
    assert get_communication(db_session, draft.id, transport=mock_tx).po_id == 7

    draft2 = save_draft(
        db_session,
        OutboundMessage(
            from_persona=PersonaKey.LEX,
            to_personas=[PersonaKey.SUP2],
            subject="Reject me",
            body="Body",
        ),
        transport=mock_tx,
    )
    rejected = reject_draft(db_session, draft2.id, transport=mock_tx)
    assert rejected.status == CommStatus.REJECTED.value

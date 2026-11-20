from pathlib import Path

import pytest

from backend.core.enums import ApprovalOutcome, CommStatus, EmailCategory, PersonaKey
from backend.mailroom import (
    InvalidAttachmentError,
    ManualLexSendForbiddenError,
    OutboundMessage,
    approve_draft,
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

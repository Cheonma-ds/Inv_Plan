from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy.orm import Session

from backend.core.db import get_session
from backend.core.enums import EmailCategory, PersonaKey
from backend.mailroom import (
    CommunicationNotFoundError,
    InvalidAttachmentError,
    InvalidDraftStateError,
    ManualLexSendForbiddenError,
    OutboundMessage,
    get_thread,
    list_drafts,
    list_inbox,
    list_sent,
    save_mailroom_attachment,
)
from backend.orchestrator.deps import get_workflow_engine
from backend.orchestrator.workflows import WorkflowEngine

router = APIRouter(prefix="/api/mailroom", tags=["mailroom"])


class ApproveDraftRequest(BaseModel):
    edited_subject: str | None = None
    edited_body: str | None = None


def _parse_persona_list(raw: str) -> list[PersonaKey]:
    if not raw or not raw.strip():
        return []
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    return [PersonaKey(p) for p in parts]


@router.get("/{persona}/inbox")
def get_inbox(persona: str, session: Session = Depends(get_session)) -> list[dict]:
    try:
        emails = list_inbox(session, persona)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return [e.model_dump(mode="json") for e in emails]


@router.get("/{persona}/sent")
def get_sent(persona: str, session: Session = Depends(get_session)) -> list[dict]:
    try:
        emails = list_sent(session, persona)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return [e.model_dump(mode="json") for e in emails]


@router.get("/{persona}/drafts")
def get_drafts(persona: str, session: Session = Depends(get_session)) -> list[dict]:
    try:
        emails = list_drafts(session, persona)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return [e.model_dump(mode="json") for e in emails]


@router.get("/threads/{thread_id}")
def get_email_thread(thread_id: str, session: Session = Depends(get_session)) -> list[dict]:
    emails = get_thread(session, thread_id)
    return [e.model_dump(mode="json") for e in emails]


@router.post("/send")
async def send_email_endpoint(
    from_persona: str = Form(...),
    to_personas: str = Form(...),
    cc_personas: str = Form(default=""),
    subject: str = Form(...),
    body: str = Form(...),
    po_id: int | None = Form(default=None),
    thread_id: str | None = Form(default=None),
    category: str = Form(default=EmailCategory.GENERAL.value),
    attachment: UploadFile | None = File(default=None),
    attachments: list[UploadFile] = File(default_factory=list),
    session: Session = Depends(get_session),
    engine: WorkflowEngine = Depends(get_workflow_engine),
) -> dict:
    try:
        sender = PersonaKey(from_persona.strip())
        to_list = _parse_persona_list(to_personas)
        cc_list = _parse_persona_list(cc_personas)
        email_cat = EmailCategory(category.strip())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if not to_list:
        raise HTTPException(
            status_code=400, detail="At least one recipient in to_personas is required."
        )

    if (
        sender == PersonaKey.MAKING
        and PersonaKey.INVENTORY_MANAGER in to_list
        and PersonaKey.LEX not in to_list
        and PersonaKey.LEX not in cc_list
    ):
        raise HTTPException(
            status_code=400,
            detail="Making Team emails to Inventory Manager must include 'lex' in To or CC.",
        )

    upload_files: list[UploadFile] = []
    if attachment is not None and attachment.filename:
        upload_files.append(attachment)
    for extra in attachments:
        if extra is not None and extra.filename:
            upload_files.append(extra)

    saved_paths: list[str] = []
    for up in upload_files:
        raw_bytes = await up.read()
        try:
            saved = save_mailroom_attachment(
                up.filename or "",
                raw_bytes,
                sender_persona=sender.value,
            )
            saved_paths.append(saved)
        except InvalidAttachmentError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    msg = OutboundMessage(
        from_persona=sender,
        to_personas=to_list,
        cc_personas=cc_list,
        subject=subject,
        body=body,
        po_id=po_id,
        thread_id=thread_id or None,
        category=email_cat,
        attachment_path=saved_paths[0] if saved_paths else None,
        attachment_paths=saved_paths,
    )

    try:
        result = engine.send_mailroom_email(session, msg)
    except ManualLexSendForbiddenError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    session.commit()
    triage = result["triage"]
    triage_serialized = None
    if triage is not None:
        triage_serialized = {
            k: (v.model_dump(mode="json") if isinstance(v, BaseModel) else v)
            for k, v in triage.items()
        }
    return {
        "email": result["email"].model_dump(mode="json"),
        "triage": triage_serialized,
    }


@router.post("/drafts/{comm_id}/approve")
def approve_draft_endpoint(
    comm_id: int,
    payload: ApproveDraftRequest | None = None,
    session: Session = Depends(get_session),
    engine: WorkflowEngine = Depends(get_workflow_engine),
) -> dict:
    try:
        result = engine.approve_lex_draft(
            session,
            comm_id,
            edited_subject=payload.edited_subject if payload else None,
            edited_body=payload.edited_body if payload else None,
        )
    except CommunicationNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except InvalidDraftStateError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    session.commit()
    return {
        "email": result["email"].model_dump(mode="json"),
        "approval_outcome": result["approval_outcome"],
        "policy_counts": result["policy_counts"],
        "po_transitioned_to": result["po_transitioned_to"],
    }


@router.post("/drafts/{comm_id}/reject")
def reject_draft_endpoint(
    comm_id: int,
    session: Session = Depends(get_session),
    engine: WorkflowEngine = Depends(get_workflow_engine),
) -> dict:
    try:
        result = engine.reject_lex_draft(session, comm_id)
    except CommunicationNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except InvalidDraftStateError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    session.commit()
    return {
        "email": result["email"].model_dump(mode="json"),
        "approval_outcome": result["approval_outcome"],
        "policy_counts": result["policy_counts"],
    }

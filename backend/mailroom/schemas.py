from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from backend.core.enums import EmailCategory, PersonaKey


class OutboundMessage(BaseModel):
    from_persona: PersonaKey
    to_personas: list[PersonaKey] = Field(min_length=1)
    cc_personas: list[PersonaKey] = Field(default_factory=list)
    subject: str = Field(min_length=1, max_length=300)
    body: str = Field(min_length=1)
    po_id: int | None = None
    thread_id: str | None = None
    category: EmailCategory = EmailCategory.GENERAL
    attachment_path: str | None = None


class StoredEmail(BaseModel):
    id: int
    po_id: int | None
    thread_id: str
    direction: str
    status: str
    category: str
    from_persona: str
    to_personas: list[str]
    cc_personas: list[str]
    subject: str
    body: str
    attachment_path: str | None
    ai_classification: str | None
    ai_extracted_json: dict[str, Any] | None
    ai_confidence: float | None
    approval_outcome: str | None
    timestamp: datetime

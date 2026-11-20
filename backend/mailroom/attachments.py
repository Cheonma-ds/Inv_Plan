import re
import uuid
from pathlib import Path

from backend.core.config import settings


class InvalidAttachmentError(ValueError):
    pass


def save_pdf_attachment(
    filename: str,
    content: bytes,
    *,
    attachments_dir: Path | None = None,
) -> str:
    """Validate and persist an uploaded PDF attachment to local storage."""
    if not filename or not filename.lower().endswith(".pdf"):
        raise InvalidAttachmentError("Only .pdf attachments are supported in MVP.")
    if not content:
        raise InvalidAttachmentError("Uploaded PDF file is empty.")
    if not content.startswith(b"%PDF"):
        raise InvalidAttachmentError("Uploaded file does not have a valid PDF header.")

    target_dir = attachments_dir or settings.attachments_dir
    target_dir.mkdir(parents=True, exist_ok=True)

    safe_stem = re.sub(r"[^A-Za-z0-9._-]", "_", Path(filename).stem)[:64] or "invoice"
    stored_name = f"{uuid.uuid4().hex[:10]}_{safe_stem}.pdf"
    dest = target_dir / stored_name
    dest.write_bytes(content)
    return str(dest)

import re
import uuid
from pathlib import Path

from backend.core.config import settings
from backend.core.enums import PersonaKey

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png"}
CSV_ALLOWED_PERSONAS = {PersonaKey.MAKING.value, PersonaKey.INVENTORY_MANAGER.value}


class InvalidAttachmentError(ValueError):
    pass


def inspect_pdf_readability(pdf_bytes: bytes) -> str | None:
    """Return a descriptive error if a PDF is empty, invalid, encrypted, or truncated."""
    if not pdf_bytes:
        return "PDF file is empty (0 bytes)"
    if not pdf_bytes.lstrip().startswith(b"%PDF"):
        return "File does not have a valid PDF header"
    if b"/Encrypt" in pdf_bytes:
        return "PDF is password-protected or encrypted"
    if b"%%EOF" not in pdf_bytes:
        return "PDF appears truncated or corrupted (missing EOF marker)"
    return None


def save_mailroom_attachment(
    filename: str,
    content: bytes,
    *,
    sender_persona: str | None = None,
    attachments_dir: Path | None = None,
) -> str:
    """Validate and persist a Mailroom attachment (.pdf, .csv for Making/Manager, .jpg/.png)."""
    if not filename:
        raise InvalidAttachmentError("Attachment filename is required.")
    if not content:
        raise InvalidAttachmentError(f"Uploaded file '{filename}' is empty (0 bytes).")

    ext = Path(filename).suffix.lower()
    if ext == ".pdf":
        if not content.startswith(b"%PDF"):
            raise InvalidAttachmentError(
                f"Uploaded PDF '{filename}' does not have a valid %PDF header."
            )
    elif ext == ".csv":
        if sender_persona and sender_persona not in CSV_ALLOWED_PERSONAS:
            raise InvalidAttachmentError(
                "CSV attachments are only supported for Making Team or Inventory Manager requests."
            )
        try:
            decoded = content.decode("utf-8")
        except UnicodeDecodeError:
            decoded = content.decode("latin-1", errors="replace")
        if not decoded.strip():
            raise InvalidAttachmentError(f"Uploaded CSV '{filename}' is empty.")
    elif ext in IMAGE_EXTENSIONS:
        is_jpeg = ext in {".jpg", ".jpeg"} and content.startswith(b"\xff\xd8\xff")
        is_png = ext == ".png" and content.startswith(b"\x89PNG\r\n\x1a\n")
        if not (is_jpeg or is_png):
            raise InvalidAttachmentError(
                f"Uploaded image '{filename}' does not have valid {ext} magic bytes."
            )
    else:
        raise InvalidAttachmentError(
            f"Unsupported attachment format '{ext or 'unknown'}' for '{filename}'. "
            "Supported formats: .pdf, .csv (Making/Manager), .jpg, .jpeg, .png."
        )

    target_dir = attachments_dir or settings.attachments_dir
    target_dir.mkdir(parents=True, exist_ok=True)

    safe_stem = re.sub(r"[^A-Za-z0-9._-]", "_", Path(filename).stem)[:64] or "attachment"
    stored_name = f"{uuid.uuid4().hex[:10]}_{safe_stem}{ext}"
    dest = target_dir / stored_name
    dest.write_bytes(content)
    return str(dest)


def save_pdf_attachment(
    filename: str,
    content: bytes,
    *,
    attachments_dir: Path | None = None,
) -> str:
    """Backward-compatible helper validating and persisting an uploaded PDF attachment."""
    if not filename or not filename.lower().endswith(".pdf"):
        raise InvalidAttachmentError("Only .pdf attachments are supported by save_pdf_attachment.")
    return save_mailroom_attachment(
        filename,
        content,
        attachments_dir=attachments_dir,
    )

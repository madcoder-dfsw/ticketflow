import os
import uuid
from django.core.exceptions import ValidationError

ALLOWED_ATTACHMENT_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg", ".txt", ".log", ".csv"}
MAX_ATTACHMENT_SIZE_MB = 5
MAX_ATTACHMENT_SIZE_BYTES = MAX_ATTACHMENT_SIZE_MB * 1024 * 1024


def validate_attachment_file(file_obj) -> None:
    """Reject unsupported file types and oversized uploads before hitting object storage."""
    if not file_obj:
        return

    if file_obj.size > MAX_ATTACHMENT_SIZE_BYTES:
        raise ValidationError(
            f"Attachment size ({file_obj.size / (1024 * 1024):.1f} MB) exceeds the {MAX_ATTACHMENT_SIZE_MB} MB limit."
        )

    ext = os.path.splitext(file_obj.name)[1].lower()
    if ext not in ALLOWED_ATTACHMENT_EXTENSIONS:
        allowed = ", ".join(sorted(ALLOWED_ATTACHMENT_EXTENSIONS))
        raise ValidationError(f"Unsupported file extension '{ext}'. Allowed types: {allowed}.")


def ticket_attachment_upload_path(instance, filename: str) -> str:
    """Generate a safe, collision-free object storage key under ticket-attachments/."""
    ext = os.path.splitext(filename)[1].lower()
    ticket_ref = instance.ticket.reference_code if instance.ticket_id else "unassigned"
    return f"ticket-attachments/{ticket_ref}/{uuid.uuid4().hex}{ext}"
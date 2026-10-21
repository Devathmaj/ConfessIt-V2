"""Record-shaped adapters around chat_encryption for AES-GCM payloads."""

from __future__ import annotations

from typing import Any

from .chat_encryption import (
    CHAT_TYPE_CASE_CLOSED,
    CHAT_TYPE_MATCHMAKING,
    CHAT_TYPE_MATCHMAKING_PREVIEW,
    CHAT_TYPE_REPORT_EVIDENCE,
    EncryptedPayload,
    decrypt_text,
    encrypt_text,
)


def encrypt_matchmaking_message(
    *, message_id: str, conversation_id: str, sender_regno: str, plaintext: str
) -> EncryptedPayload:
    return encrypt_text(
        plaintext,
        chat_type=CHAT_TYPE_MATCHMAKING,
        record_id=message_id,
        chat_id=conversation_id,
        sender_id=sender_regno,
    )


def decrypt_matchmaking_message(row: Any) -> str:
    return decrypt_text(
        row.body_ciphertext,
        row.body_nonce,
        row.body_key_version,
        chat_type=CHAT_TYPE_MATCHMAKING,
        record_id=str(row.id),
        chat_id=str(row.conversation_id),
        sender_id=str(row.sender_regno),
    )


def encrypt_case_closed_message(
    *, message_id: str, session_id: str, sender_user_id: str, plaintext: str
) -> EncryptedPayload:
    return encrypt_text(
        plaintext,
        chat_type=CHAT_TYPE_CASE_CLOSED,
        record_id=message_id,
        chat_id=session_id,
        sender_id=sender_user_id,
    )


def decrypt_case_closed_message(row: Any) -> str:
    return decrypt_text(
        row.body_ciphertext,
        row.body_nonce,
        row.body_key_version,
        chat_type=CHAT_TYPE_CASE_CLOSED,
        record_id=str(row.id),
        chat_id=str(row.session_id),
        sender_id=str(row.sender_user_id),
    )


def encrypt_matchmaking_preview(
    *,
    conversation_id: str,
    message_id: str,
    sender_regno: str,
    plaintext: str,
) -> EncryptedPayload:
    return encrypt_text(
        plaintext,
        chat_type=CHAT_TYPE_MATCHMAKING_PREVIEW,
        record_id=message_id,
        chat_id=conversation_id,
        sender_id=sender_regno,
    )


def decrypt_matchmaking_preview(row: Any) -> str | None:
    """Decrypt a conversation preview, or return None if absent or decryption fails."""
    if not getattr(row, "last_message_preview_ciphertext", None):
        return None
    message_id = getattr(row, "last_message_preview_message_id", None)
    if message_id is None:
        return None
    return decrypt_text(
        row.last_message_preview_ciphertext,
        row.last_message_preview_nonce,
        row.last_message_preview_key_version,
        chat_type=CHAT_TYPE_MATCHMAKING_PREVIEW,
        record_id=str(message_id),
        chat_id=str(row.id),
        sender_id=str(getattr(row, "last_message_preview_sender_regno", "")),
    )


def _evidence_context(*, report_kind: str, report_id: str, evidence_id: str) -> str:
    return f"{report_kind}:{report_id}:{evidence_id}"


def encrypt_report_evidence(
    *,
    evidence_id: str,
    report_id: str,
    report_kind: str,
    message_id: str,
    chat_id: str,
    sender_ref: str,
    plaintext: str,
) -> EncryptedPayload:
    # Bind turn identity and report/evidence IDs into AAD context.
    return encrypt_text(
        plaintext,
        chat_type=CHAT_TYPE_REPORT_EVIDENCE,
        record_id=message_id,
        chat_id=chat_id,
        sender_id=sender_ref,
        context=_evidence_context(
            report_kind=report_kind, report_id=report_id, evidence_id=evidence_id
        ),
    )


def decrypt_report_evidence(row: Any) -> str:
    return decrypt_text(
        row.body_ciphertext,
        row.body_nonce,
        row.body_key_version,
        chat_type=CHAT_TYPE_REPORT_EVIDENCE,
        record_id=str(row.message_id),
        chat_id=str(row.chat_id),
        sender_id=str(row.sender_ref),
        context=_evidence_context(
            report_kind=str(row.report_kind),
            report_id=str(row.report_id),
            evidence_id=str(row.id),
        ),
    )

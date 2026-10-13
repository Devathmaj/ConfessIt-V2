"""Shared builders for encrypted chat rows in tests.

Tests used to construct ``MessageRecord``/``CaseClosedMessageRecord`` with a
plaintext ``body``. The models no longer have that column: every body is
AES-256-GCM ciphertext bound to the row's id, thread id and sender. These
helpers keep the fixtures readable while always going through the real
encryption path, so a test cannot accidentally seed a row the application
could not decrypt.

They are not production code and never weaken the encryption configuration:
the suite sets a fixed test key in ``conftest.py`` before ``app.config`` loads.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import select

from app.db_models import CaseClosedMessageRecord, MessageRecord
from app.postgres import new_id
from app.services.chat_encryption import ChatDecryptionError
from app.services.chat_message_crypto import (
    decrypt_case_closed_message,
    decrypt_matchmaking_message,
    encrypt_case_closed_message,
    encrypt_matchmaking_message,
)


def make_message_record(
    *,
    conversation_id: str,
    sender_regno: str,
    receiver_regno: str,
    body: str,
    created_at: datetime,
    delete_after: datetime,
    is_read: bool = False,
    removed_at: Optional[datetime] = None,
    removed_by_regno: Optional[str] = None,
    id: Optional[str] = None,
) -> MessageRecord:
    message_id = str(id) if id is not None else new_id()
    payload = encrypt_matchmaking_message(
        message_id=message_id,
        conversation_id=str(conversation_id),
        sender_regno=sender_regno,
        plaintext=body,
    )
    return MessageRecord(
        id=message_id,
        conversation_id=str(conversation_id),
        sender_regno=sender_regno,
        receiver_regno=receiver_regno,
        body_ciphertext=payload.ciphertext,
        body_nonce=payload.nonce,
        body_key_version=payload.key_version,
        created_at=created_at,
        delete_after=delete_after,
        is_read=is_read,
        removed_at=removed_at,
        removed_by_regno=removed_by_regno,
    )


def make_case_closed_message_record(
    *,
    session_id: str,
    sender_user_id: str,
    body: str,
    created_at: datetime,
    delete_after: datetime,
    is_read: bool = False,
    removed_at: Optional[datetime] = None,
    removed_by_regno: Optional[str] = None,
    id: Optional[str] = None,
) -> CaseClosedMessageRecord:
    message_id = str(id) if id is not None else new_id()
    payload = encrypt_case_closed_message(
        message_id=message_id,
        session_id=str(session_id),
        sender_user_id=str(sender_user_id),
        plaintext=body,
    )
    return CaseClosedMessageRecord(
        id=message_id,
        session_id=str(session_id),
        sender_user_id=str(sender_user_id),
        body_ciphertext=payload.ciphertext,
        body_nonce=payload.nonce,
        body_key_version=payload.key_version,
        created_at=created_at,
        delete_after=delete_after,
        is_read=is_read,
        removed_at=removed_at,
        removed_by_regno=removed_by_regno,
    )


def find_message_by_body(db, plaintext: str) -> Optional[MessageRecord]:
    """The matchmaking row whose decrypted body equals ``plaintext`` (or None).

    Decrypts only the test database's tiny row set, never production data.
    """
    with db.sessions() as session:
        rows = session.scalars(select(MessageRecord)).all()
        for row in rows:
            try:
                if decrypt_matchmaking_message(row) == plaintext:
                    return row
            except ChatDecryptionError:
                continue
    return None


def read_message_body(row: MessageRecord) -> str:
    return decrypt_matchmaking_message(row)


def read_case_closed_body(row: CaseClosedMessageRecord) -> str:
    return decrypt_case_closed_message(row)

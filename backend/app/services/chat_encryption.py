"""Encrypt persisted chat bodies with versioned AES-256-GCM keys.

Each record stores its key version and fresh nonce; stable message metadata is authenticated
as AAD. Errors must never expose plaintext, keys, nonces, or ciphertext.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
import secrets
from dataclasses import dataclass
from typing import Optional

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from ..config import settings

# 12 bytes is the nonce size AES-GCM is defined around. Larger nonces are legal
# but hashed internally by the primitive; pinning 12 keeps the stored format
# unambiguous.
NONCE_BYTES = 12
# AES-256 has exactly one legal key size. Anything else is a configuration error.
KEY_BYTES = 32
# AESGCM.encrypt appends the 16-byte tag to the ciphertext, so a shorter payload
# can never authenticate; rejecting it early keeps malformed rows away from the
# primitive.
GCM_TAG_BYTES = 16

KEY_ENV_PATTERN = re.compile(r"^CHAT_ENCRYPTION_KEY_V(\d+)$")

# Chat-type labels are authenticated as part of the AAD, not stored as their
# own column: the table already identifies the thread kind, and accepting a
# caller-supplied value would make it easier to authenticate a different label
# than the one the row actually represents.
CHAT_TYPE_MATCHMAKING = "matchmaking"
CHAT_TYPE_MATCHMAKING_PREVIEW = "matchmaking_preview"
CHAT_TYPE_CASE_CLOSED = "case_closed"
CHAT_TYPE_REPORT_EVIDENCE = "report_evidence"


class ChatEncryptionError(Exception):
    """Base class for sanitized encryption failures."""

    code = "chat_encryption_error"

    def __init__(self, message: str, *, code: Optional[str] = None):
        super().__init__(message)
        if code is not None:
            self.code = code


class ChatEncryptionConfigError(ChatEncryptionError, RuntimeError):
    """The active key is missing or malformed; the application must not start."""

    code = "invalid_configuration"


class ChatDecryptionError(ChatEncryptionError):
    """Stored ciphertext or its authenticated metadata did not verify."""

    code = "decryption_failed"


@dataclass(frozen=True)
class EncryptedPayload:
    """The three fields persisted for one encrypted body."""

    ciphertext: bytes
    nonce: bytes
    key_version: str


def _decode_key(version: str, encoded: str) -> bytes:
    """Decode and validate one configured key, naming only the variable on error."""
    variable = f"CHAT_ENCRYPTION_KEY_{version.upper()}"
    try:
        material = base64.b64decode(encoded.strip(), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ChatEncryptionConfigError(
            f"{variable} is not valid Base64. Generate one with: "
            'python -c "import base64, secrets; '
            'print(base64.b64encode(secrets.token_bytes(32)).decode())"',
            code="invalid_key_encoding",
        ) from exc
    if len(material) != KEY_BYTES:
        raise ChatEncryptionConfigError(
            f"{variable} must decode to exactly {KEY_BYTES} bytes for AES-256 "
            f"(got {len(material)}).",
            code="invalid_key_length",
        )
    return material


def configured_keys() -> dict[str, bytes]:
    """Every key version available to this process, validated.

    Settings declares V1..V8 so the values can live in the backend's dotenv
    file without tripping ``extra="forbid"``. The environment is scanned as
    well, so a deployment that exports a higher version (V9, V10, ...) is
    picked up without a code change. Environment values win, matching
    pydantic-settings' own precedence.
    """
    raw: dict[str, str] = {}
    for name, value in settings.model_dump().items():
        match = KEY_ENV_PATTERN.match(name)
        if match and value:
            raw[f"v{match.group(1)}"] = str(value)
    for name, value in os.environ.items():
        match = KEY_ENV_PATTERN.match(name)
        if match and value and value.strip():
            raw[f"v{match.group(1)}"] = value
    return {version: _decode_key(version, encoded) for version, encoded in raw.items()}


def _active_version() -> str:
    version = str(settings.CHAT_ENCRYPTION_ACTIVE_KEY_VERSION or "").strip().lower()
    if not version:
        raise ChatEncryptionConfigError(
            "CHAT_ENCRYPTION_ACTIVE_KEY_VERSION is not set.",
            code="missing_active_version",
        )
    return version


def active_key() -> tuple[str, bytes]:
    """The version and key material used for new encryptions."""
    version = _active_version()
    keys = configured_keys()
    key = keys.get(version)
    if key is None:
        raise ChatEncryptionConfigError(
            f"Chat encryption is enabled but CHAT_ENCRYPTION_KEY_{version.upper()} "
            "is missing. Set it to a Base64-encoded 32-byte key; new messages will "
            "never be stored in plaintext.",
            code="missing_active_key",
        )
    return version, key


def validate_startup_configuration() -> None:
    """Fail closed at application startup when encryption cannot work.

    Called from the FastAPI startup hook. The scheduler process never calls this
    and never receives the keys: it only deletes rows by their indexed deadline.
    """
    version, _key = active_key()
    # Validate every configured historical key too: a malformed V1 would
    # otherwise surface only later, as an unreadable message, long after the
    # operator could connect it to the environment change.
    configured_keys()
    # No key material or length is logged; only the version label.
    _ = version


def _aad(
    *,
    chat_type: str,
    record_id: str,
    chat_id: str,
    sender_id: str,
    key_version: str,
    context: str = "",
) -> bytes:
    """Deterministic authenticated metadata.

    A JSON array (rather than a delimiter-joined string) removes ambiguity
    from IDs that might contain punctuation, and ``ensure_ascii=True`` makes
    the bytes identical on every platform. The element order is fixed here so
    encryption and decryption cannot drift.

    ``context`` is empty for ordinary chat rows. Reported-message evidence
    uses it to bind the report and evidence identifiers on top of the original
    message id, chat id and sender id carried in the three slots above.
    """
    return json.dumps(
        [
            "confessit-chat-v1",
            str(chat_type),
            str(record_id),
            str(chat_id),
            str(sender_id),
            str(key_version),
            str(context),
        ],
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")


def encrypt_text(
    plaintext: str,
    *,
    chat_type: str,
    record_id: str,
    chat_id: str,
    sender_id: str,
    context: str = "",
) -> EncryptedPayload:
    """Encrypt one UTF-8 body with the active key and a fresh nonce."""
    version, key = active_key()
    nonce = secrets.token_bytes(NONCE_BYTES)
    aad = _aad(
        chat_type=chat_type,
        record_id=record_id,
        chat_id=chat_id,
        sender_id=sender_id,
        key_version=version,
        context=context,
    )
    ciphertext = AESGCM(key).encrypt(nonce, (plaintext or "").encode("utf-8"), aad)
    return EncryptedPayload(ciphertext=ciphertext, nonce=nonce, key_version=version)


def decrypt_text(
    ciphertext: bytes,
    nonce: bytes,
    key_version: str,
    *,
    chat_type: str,
    record_id: str,
    chat_id: str,
    sender_id: str,
    context: str = "",
) -> str:
    """Decrypt one body, selecting the key version recorded on the row.

    Any authentication failure, unknown version or malformed field raises
    :class:`ChatDecryptionError`. The message deliberately contains no record
    content; callers add the record id when logging.
    """
    version = str(key_version or "").strip().lower()
    if not version:
        raise ChatDecryptionError("Encrypted message has no key version.")
    keys = configured_keys()
    key = keys.get(version)
    if key is None:
        raise ChatDecryptionError(
            f"Key version {version!r} is not configured; the message cannot be read.",
            code="unknown_key_version",
        )
    nonce_bytes = bytes(nonce or b"")
    ciphertext_bytes = bytes(ciphertext or b"")
    if len(nonce_bytes) != NONCE_BYTES:
        raise ChatDecryptionError("Encrypted message has an invalid nonce.")
    if len(ciphertext_bytes) < GCM_TAG_BYTES:
        raise ChatDecryptionError("Encrypted message has no authentication tag.")
    aad = _aad(
        chat_type=chat_type,
        record_id=record_id,
        chat_id=chat_id,
        sender_id=sender_id,
        key_version=version,
        context=context,
    )
    try:
        plaintext = AESGCM(key).decrypt(nonce_bytes, ciphertext_bytes, aad)
    except InvalidTag as exc:
        raise ChatDecryptionError(
            "Encrypted message failed authentication.", code="invalid_tag"
        ) from exc
    except Exception as exc:  # pragma: no cover - defensive, primitive-specific
        raise ChatDecryptionError(
            "Encrypted message could not be decrypted.", code="decrypt_error"
        ) from exc
    try:
        return plaintext.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ChatDecryptionError(
            "Decrypted message is not valid UTF-8.", code="invalid_utf8"
        ) from exc

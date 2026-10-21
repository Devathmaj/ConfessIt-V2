"""Azure AI Language PII detection for love-note moderation previews."""

from dataclasses import dataclass
from typing import Any, List, Optional, Sequence, Tuple

from ..logger import get_logger

logger = get_logger(__name__)

# Pinned to ``2023-04-01``: the default of azure-ai-textanalytics 5.4.0 and the
# last stable version before the 2025-05-15 preview line. Pinning explicitly
# means a future SDK bump cannot silently change the service contract.
API_VERSION = "2023-04-01"

# Azure documents a 5,120-character limit per document for PII recognition and
# language detection. ``LOVE_NOTE_BODY_MAX`` already caps a note far below
# this, but the check is kept so an over-long body is refused as an error
# rather than silently trimmed and treated as safe.
TEXT_MAX_LENGTH = 5120

LANGUAGE_ENGLISH = "en"
LANGUAGE_MALAYALAM = "ml"

# The marker the service uses in ``redacted_text``: each detected entity is
# replaced by ``*`` repeated to the entity's own length (verified against the
# deployed resource). Merging re-uses the same marker so a merged preview
# reads exactly like a single-pass one.
REDACTION_CHARACTER = "*"

# Malayalam Unicode block (U+0D00-U+0D7F), used only to decide whether the
# Malayalam PII pass is needed -- a script check, not a language classifier.
_MALAYALAM_RANGE = (0x0D00, 0x0D7F)

# Bounded HTTP behavior for the Language client (seconds); there is no
# dedicated setting because the caller treats a failure as "no preview", so
# the only knob that matters is that a hung provider cannot hang the request.
REQUEST_TIMEOUT_SECONDS = 10.0


class PiiDetectionError(Exception):
    """A PII-detection failure carrying a branchable code and safe message.

    Messages never contain the API key, the note body (only lengths), or raw
    provider bodies, so they are safe to log and, where appropriate, to
    surface as API details.
    """

    def __init__(self, detail: str, code: str) -> None:
        super().__init__(detail)
        self.detail = detail
        self.code = code


@dataclass
class PiiDetectionConfig:
    endpoint: Optional[str]
    api_key: Optional[str]
    # Read for parity with the deployed resource; not passed to the SDK
    # because the official client has no region parameter (see module doc).
    region: Optional[str]
    timeout_seconds: float


def config_from_settings(settings: Any) -> PiiDetectionConfig:
    """Read PII detection configuration from the application settings."""
    return PiiDetectionConfig(
        endpoint=(getattr(settings, "PII_DETECTION_ENDPOINT", None) or None),
        api_key=getattr(settings, "PII_DETECTION_API_KEY", None),
        region=(getattr(settings, "PII_DETECTION_REGION", None) or None),
        timeout_seconds=REQUEST_TIMEOUT_SECONDS,
    )


def _require_config(config: PiiDetectionConfig) -> None:
    if not config.endpoint or not str(config.endpoint).strip():
        raise PiiDetectionError(
            "PII detection is not configured: PII_DETECTION_ENDPOINT is missing. "
            "Set it in the runtime environment.",
            code="missing_config",
        )
    if not config.api_key or not str(config.api_key).strip():
        raise PiiDetectionError(
            "PII detection is not configured: PII_DETECTION_API_KEY is missing. "
            "Set it in the runtime environment.",
            code="missing_config",
        )


def get_pii_client(config: PiiDetectionConfig):
    """Build the official SDK client for this configuration."""
    try:
        from azure.ai.textanalytics import TextAnalyticsClient
        from azure.core.credentials import AzureKeyCredential
    except ImportError as exc:
        raise PiiDetectionError(
            "PII detection is unavailable: the azure-ai-textanalytics "
            "package is not installed.",
            code="unavailable",
        ) from exc
    _require_config(config)
    endpoint = str(config.endpoint or "").strip().rstrip("/")
    key = str(config.api_key or "").strip()
    # Endpoint + key only: the official client defines no region argument,
    # and inventing one would be an unsupported constructor call.
    return TextAnalyticsClient(
        endpoint, AzureKeyCredential(key), api_version=API_VERSION
    )


def _map_sdk_error(exc: BaseException) -> PiiDetectionError:
    """Map SDK/transport failures to branchable codes without secrets."""
    name = type(exc).__name__
    # azure-core HttpResponseError carries status_code + error.code/message.
    status = getattr(exc, "status_code", None)
    try:
        status_int = int(status) if status is not None else None
    except (TypeError, ValueError):
        status_int = None
    err_obj = getattr(exc, "error", None)
    err_code = str(getattr(err_obj, "code", "") or "").lower()
    err_msg = str(getattr(err_obj, "message", "") or str(exc) or "").lower()
    combined = f"{err_code} {err_msg}"
    if status_int in (401, 403) or "unauthorized" in combined or "forbidden" in combined:
        return PiiDetectionError(
            "The PII detection provider refused the credentials. "
            "Check PII_DETECTION_API_KEY.",
            code="auth",
        )
    if status_int == 429 or "toomanyrequests" in err_code:
        return PiiDetectionError(
            "The PII detection provider rate-limited the request. Retry shortly.",
            code="rate_limited",
        )
    if status_int is not None and 500 <= status_int <= 599:
        return PiiDetectionError(
            "The PII detection provider is temporarily unavailable. Retry shortly.",
            code="unavailable",
        )
    lowered_name = name.lower()
    if "timeout" in lowered_name or "timeout" in combined:
        return PiiDetectionError(
            "The PII detection request timed out or could not connect. "
            "Retry the operation.",
            code="timeout",
        )
    if "service_request" in lowered_name or "connection" in lowered_name or "network" in lowered_name:
        return PiiDetectionError(
            "Could not reach the PII detection provider. Check PII_DETECTION_ENDPOINT.",
            code="network",
        )
    if status_int is not None:
        return PiiDetectionError(
            f"The PII detection request failed (status {status_int}).",
            code="model_error",
        )
    return PiiDetectionError(
        "The PII detection request failed.",
        code="model_error",
    )


def _call_sdk(client: Any, operation: str, documents: Sequence[str], *, timeout: float, **kwargs: Any) -> Any:
    """Run one SDK operation on one document and classify any failure.

    Shared by language detection and PII recognition so transport failures,
    timeouts and provider bodies are mapped in one place. Neither the payload
    nor the key is ever logged.
    """
    try:
        return getattr(client, operation)(
            list(documents), timeout=float(timeout), **kwargs
        )
    except Exception as exc:
        # HttpResponseError, ServiceRequestError, etc. Never include body/key.
        if isinstance(exc, PiiDetectionError):
            raise
        logger.warning("PII detection SDK call failed: %s", type(exc).__name__)
        raise _map_sdk_error(exc) from exc


@dataclass(frozen=True)
class DetectedLanguage:
    """The primary language the service detected for one note body."""

    name: str
    iso6391_name: Optional[str]
    confidence_score: float


@dataclass(frozen=True)
class PiiEntity:
    """One detected entity, in the coordinate system the service returned."""

    category: str
    offset: int
    length: int


@dataclass
class PiiPassResult:
    """One recognition pass: the language used and its redacted output."""

    language: str
    redacted_text: str
    entities: Tuple[PiiEntity, ...]


@dataclass
class LoveNoteRedaction:
    """The persisted moderation preview and how it was produced."""

    moderation_body: str
    languages: Tuple[str, ...]
    entity_count: int


def _document_error_code(result: Any) -> Optional[str]:
    """The service's error code when ``result`` is a DocumentError, else None."""
    if getattr(result, "is_error", False):
        error = getattr(result, "error", None)
        return str(getattr(error, "code", "") or "document_error")
    return None


def detect_language(
    text: str,
    *,
    config: Optional[PiiDetectionConfig] = None,
    client: Any = None,
) -> DetectedLanguage:
    """Detect the primary language of one note body.

    Raises :class:`PiiDetectionError` -- never returns a default language --
    when the service reports a document error or the call fails. Callers
    treat that as "no preview", not as English.
    """
    if config is None:
        from ..config import settings as _settings

        config = config_from_settings(_settings)
    _require_config(config)
    if client is None:
        client = get_pii_client(config)
    results = _call_sdk(
        client,
        "detect_language",
        [text],
        timeout=config.timeout_seconds,
        disable_service_logs=True,
    )
    if not results:
        raise PiiDetectionError(
            "The PII detection provider returned no language result.",
            code="malformed",
        )
    result = results[0]
    error_code = _document_error_code(result)
    if error_code is not None:
        raise PiiDetectionError(
            f"Language detection did not succeed ({error_code}).",
            code="language_unavailable",
        )
    primary = getattr(result, "primary_language", None)
    iso_code = getattr(primary, "iso6391_name", None)
    if primary is None or iso_code is None:
        raise PiiDetectionError(
            "The PII detection provider returned no primary language.",
            code="malformed",
        )
    iso_code = str(iso_code).strip()
    # The service answers "(Unknown)" when it cannot decide; surface that as
    # unknown rather than pretending it is a language code.
    normalized = iso_code.lower()
    if not normalized or normalized.startswith("(") or normalized in ("unknown", "und"):
        normalized = None
    return DetectedLanguage(
        name=str(getattr(primary, "name", "") or ""),
        iso6391_name=normalized,
        confidence_score=float(getattr(primary, "confidence_score", 0.0) or 0.0),
    )


def recognize_pii(
    text: str,
    language: str,
    *,
    config: Optional[PiiDetectionConfig] = None,
    client: Any = None,
) -> PiiPassResult:
    """Run one PII recognition pass over ``text`` in ``language``."""
    if config is None:
        from ..config import settings as _settings

        config = config_from_settings(_settings)
    _require_config(config)
    if client is None:
        client = get_pii_client(config)
    results = _call_sdk(
        client,
        "recognize_pii_entities",
        [text],
        timeout=config.timeout_seconds,
        language=language,
        disable_service_logs=True,
    )
    if not results:
        raise PiiDetectionError(
            "The PII detection provider returned no recognition result.",
            code="malformed",
        )
    result = results[0]
    error_code = _document_error_code(result)
    if error_code is not None:
        raise PiiDetectionError(
            f"PII recognition did not succeed ({error_code}).",
            code="pii_unavailable",
        )
    redacted_text = getattr(result, "redacted_text", None)
    if not isinstance(redacted_text, str):
        raise PiiDetectionError(
            "The PII detection provider returned no redacted text.",
            code="malformed",
        )
    entities: List[PiiEntity] = []
    for entry in getattr(result, "entities", None) or []:
        try:
            offset = int(getattr(entry, "offset"))
            length = int(getattr(entry, "length"))
        except (TypeError, ValueError):
            continue
        if offset < 0 or length <= 0:
            continue
        entities.append(
            PiiEntity(
                category=str(getattr(entry, "category", "") or ""),
                offset=offset,
                length=length,
            )
        )
    return PiiPassResult(
        language=language,
        redacted_text=redacted_text,
        entities=tuple(entities),
    )


def _contains_malayalam_script(text: str) -> bool:
    return any(0x0D00 <= ord(char) <= 0x0D7F for char in text)


def _has_latin_letters(text: str) -> bool:
    return any("a" <= char.lower() <= "z" for char in text)


def passes_for(
    text: str, detected: Optional[DetectedLanguage]
) -> Tuple[str, ...]:
    """Which PII passes this note's script and detected language call for."""
    latin_letters = _has_latin_letters(text)
    malayalam_script = _contains_malayalam_script(text)
    detected_code = detected.iso6391_name if detected is not None else None
    if detected_code == LANGUAGE_MALAYALAM:
        primary = LANGUAGE_MALAYALAM
    elif detected_code == LANGUAGE_ENGLISH:
        primary = LANGUAGE_ENGLISH
    elif malayalam_script:
        primary = LANGUAGE_MALAYALAM
    else:
        primary = LANGUAGE_ENGLISH
    passes: List[str] = [primary]
    if latin_letters:
        # Both models, whichever one the service picked as primary: this is
        # the Manglish case, and detection swings between the two.
        if LANGUAGE_ENGLISH not in passes:
            passes.append(LANGUAGE_ENGLISH)
        if LANGUAGE_MALAYALAM not in passes:
            passes.append(LANGUAGE_MALAYALAM)
    elif malayalam_script and LANGUAGE_MALAYALAM not in passes:
        passes.append(LANGUAGE_MALAYALAM)
    return tuple(passes)


def merge_redactions(text: str, passes: Sequence[PiiPassResult]) -> LoveNoteRedaction:
    """Combine one or more passes into the single preview the moderator sees."""
    if not passes:
        raise PiiDetectionError(
            "No PII recognition pass produced a result.",
            code="malformed",
        )
    if len(passes) == 1:
        only = passes[0]
        return LoveNoteRedaction(
            moderation_body=only.redacted_text,
            languages=(only.language,),
            entity_count=len(only.entities),
        )

    spans: List[Tuple[int, int]] = []
    for result in passes:
        for entity in result.entities:
            start = entity.offset
            end = entity.offset + entity.length
            # The body is the coordinate space every pass ran against; a span
            # outside it is a provider bug, and dropping it is safer than
            # slicing the wrong characters into the preview.
            if start < 0 or end <= start or end > len(text):
                continue
            spans.append((start, end))

    if not spans:
        merged = text
        entity_count = 0
    else:
        spans.sort()
        union: List[Tuple[int, int]] = []
        for start, end in spans:
            if union and start <= union[-1][1]:
                union[-1] = (union[-1][0], max(union[-1][1], end))
            else:
                union.append((start, end))
        chunks: List[str] = []
        cursor = 0
        for start, end in union:
            chunks.append(text[cursor:start])
            chunks.append(REDACTION_CHARACTER * (end - start))
            cursor = end
        chunks.append(text[cursor:])
        merged = "".join(chunks)
        entity_count = len(union)
    return LoveNoteRedaction(
        moderation_body=merged,
        languages=tuple(result.language for result in passes),
        entity_count=entity_count,
    )


def redact_love_note_body(
    body: str,
    *,
    config: Optional[PiiDetectionConfig] = None,
) -> LoveNoteRedaction:
    """Produce the privacy-safe moderation preview for one love note body."""
    if config is None:
        from ..config import settings as _settings

        config = config_from_settings(_settings)
    text = body or ""
    if len(text) > TEXT_MAX_LENGTH:
        raise PiiDetectionError(
            f"Submitted text exceeds the PII detection limit ({TEXT_MAX_LENGTH} characters).",
            code="invalid_input",
        )
    if not text.strip():
        raise PiiDetectionError(
            "Empty text cannot be redacted.",
            code="invalid_input",
        )
    _require_config(config)
    client = get_pii_client(config)
    try:
        detected = detect_language(text, config=config, client=client)
        languages = passes_for(text, detected)
        passes = [
            recognize_pii(text, language, config=config, client=client)
            for language in languages
        ]
        redaction = merge_redactions(text, passes)
        logger.info(
            "PII redaction complete for love note preview (%s chars, language=%s confidence=%.2f, "
            "passes=%s, entities=%s)",
            len(text),
            detected.iso6391_name or "unknown",
            detected.confidence_score,
            ",".join(redaction.languages),
            redaction.entity_count,
        )
        return redaction
    finally:
        try:
            close = getattr(client, "close", None)
            if callable(close):
                close()
        except Exception:
            pass


def pii_error_to_http(exc: PiiDetectionError) -> Any:
    """Map a PII-detection failure to a retryable HTTP error.

    Missing configuration is a deployment error with a distinct message;
    every other failure is a transient provider failure. Never includes
    secrets or the submitted text.
    """
    from fastapi import HTTPException as _HTTPException
    from fastapi import status as _status

    if getattr(exc, "code", None) == "missing_config":
        detail = "Privacy-safe moderation previews are not configured."
    else:
        detail = (
            "The privacy-safe moderation preview is temporarily unavailable. "
            "Please retry shortly."
        )
    return _HTTPException(
        status_code=_status.HTTP_503_SERVICE_UNAVAILABLE,
        detail=detail,
    )

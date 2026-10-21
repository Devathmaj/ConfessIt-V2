"""Central Azure AI Content Safety entry point (confessions + comments)."""

from dataclasses import dataclass
from typing import Any, Dict, Optional, Union

from ..logger import get_logger

logger = get_logger(__name__)

API_VERSION = "2024-09-01"
CATEGORIES = ("Hate", "SelfHarm", "Sexual", "Violence")
OUTPUT_TYPE = "FourSeverityLevels"

# Azure documents max 10k Unicode characters per Analyze Text request.
TEXT_MAX_LENGTH = 10000

# Central item vocabulary: the item types moderated today. AI outputs stay
# reserved so a future batch plugs handling into _POLICIES rather than each
# feature wiring its own endpoint/key/timeout/policy.
CONTENT_CONFESSION = "confession"
CONTENT_COMMENT = "comment"
CONTENT_CHAT_MESSAGE = "chat_message"
CONTENT_CASE_CLOSED_MESSAGE = "case_closed_message"
CONTENT_PROFILE_PICTURE = "profile_picture"
CONTENT_IMAGE = "image"
CONTENT_AI_OUTPUT = "ai_output"

SUPPORTED_CONTENT_TYPES: tuple[str, ...] = (
    CONTENT_CONFESSION,
    CONTENT_COMMENT,
    CONTENT_CHAT_MESSAGE,
    CONTENT_CASE_CLOSED_MESSAGE,
    CONTENT_PROFILE_PICTURE,
)

# Item types whose payload is image bytes and therefore go through Analyze
# Image instead of Analyze Text. The dispatcher routes; callers never choose.
_IMAGE_CONTENT_TYPES: frozenset[str] = frozenset({CONTENT_PROFILE_PICTURE})

# The exact display name every automated report carries. Shown in the admin
# report queue as the reporter name; never a real participant.
AUTOMATED_REPORTER_DISPLAY_NAME = "Azure Content Safety"

# Stored discriminator for automated reports (reports.source). Human reports
# carry "user"; automated confession reports carry this value.
AUTOMATED_SOURCE = "azure_content_safety"
HUMAN_SOURCE = "user"

# removed_by_regno audit value for automated hides. Plain text like every
# removed_by_regno in this schema; names no staff account.
AUTOMATED_REMOVED_BY = "azure-content-safety"

# Central severity policy. The maximum per-category severity decides.
# 0: no escalation. 2/4/6: report + hide (everything >1 flagged+hidden),
# or -- for blocking items such as profile pictures -- reject outright.
NO_ACTION = "none"
REPORT_ONLY = "report"
REPORT_AND_HIDE = "report_and_hide"
REJECT = "reject"

# Central per-item policy: text can be stored hidden and reported; violating
# profile pictures are blocked before storage and staff are notified.
_POLICIES: dict[str, dict] = {
    CONTENT_CONFESSION: {
        "report_threshold": 2,
        "hide_threshold": 2,
        "block": False,
    },
    CONTENT_COMMENT: {
        "report_threshold": 2,
        "hide_threshold": 2,
        "block": False,
    },
    CONTENT_CHAT_MESSAGE: {
        "report_threshold": 2,
        "hide_threshold": 2,
        "block": False,
    },
    CONTENT_CASE_CLOSED_MESSAGE: {
        "report_threshold": 2,
        "hide_threshold": 2,
        "block": False,
    },
    CONTENT_PROFILE_PICTURE: {
        "block": True,
        "block_threshold": 2,
    },
}


class ContentSafetyError(Exception):
    """A Content Safety failure carrying a branchable code and safe message.

    Messages never contain the API key, the confession text (only lengths),
    or raw provider bodies, so they are safe to log and, where appropriate,
    to surface as API details.
    """

    def __init__(self, detail: str, code: str) -> None:
        super().__init__(detail)
        self.detail = detail
        self.code = code


@dataclass
class ContentSafetyConfig:
    endpoint: Optional[str]
    api_key: Optional[str]
    region: Optional[str]
    timeout_seconds: float


def config_from_settings(settings: Any) -> ContentSafetyConfig:
    """Read Content Safety configuration from the application settings."""
    return ContentSafetyConfig(
        endpoint=(getattr(settings, "CONTENT_SAFETY_ENDPOINT", None) or None),
        api_key=getattr(settings, "CONTENT_SAFETY_API_KEY", None),
        region=(getattr(settings, "CONTENT_SAFETY_REGION", None) or None),
        timeout_seconds=float(
            getattr(settings, "CONTENT_SAFETY_TIMEOUT_SECONDS", 10.0) or 10.0
        ),
    )


def _require_config(config: ContentSafetyConfig) -> None:
    if not config.endpoint or not str(config.endpoint).strip():
        raise ContentSafetyError(
            "Content moderation is not configured: CONTENT_SAFETY_ENDPOINT is missing. "
            "Set it in the runtime environment.",
            code="missing_config",
        )
    if not config.api_key or not str(config.api_key).strip():
        raise ContentSafetyError(
            "Content moderation is not configured: CONTENT_SAFETY_API_KEY is missing. "
            "Set it in the runtime environment.",
            code="missing_config",
        )


def analyze_text_url_for(config: ContentSafetyConfig) -> str:
    """Full Analyze Text URL for this configuration."""
    base = str(config.endpoint or "").strip().rstrip("/")
    return f"{base}/contentsafety/text:analyze?api-version={API_VERSION}"


def analyze_image_url_for(config: ContentSafetyConfig) -> str:
    """Full Analyze Image URL for this configuration."""
    base = str(config.endpoint or "").strip().rstrip("/")
    return f"{base}/contentsafety/image:analyze?api-version={API_VERSION}"


def max_severity(scores: Dict[str, int]) -> int:
    """Maximum severity across the four categories (0, 2, 4 or 6).

    Scores are never averaged and a lower score never cancels a higher one.
    """
    if not scores:
        return 0
    return max(int(scores.get(cat, 0) or 0) for cat in CATEGORIES)


def classify_action(maximum: int) -> str:
    """Policy action for a maximum severity: none, report, or report+hide.

    Kept for the confession policy; future items resolve through
    :func:`classify_for_content` so thresholds stay in ``_POLICIES``.
    """
    return classify_for_content(CONTENT_CONFESSION, maximum)


def classify_for_content(content_type: str, maximum: int) -> str:
    """Policy action for one item type and one maximum severity.

    Blocking items resolve first: a severe profile picture is a rejection,
    not a report+hide, regardless of any report/hide thresholds. Everything
    else keeps the canonical principal (2/4/6 report+hide).
    """
    policy = _POLICIES.get(content_type)
    if policy is None:
        raise ContentSafetyError(
            f"Content moderation is not implemented for {content_type!r}.",
            code="unsupported_item",
        )
    if policy.get("block") and maximum >= int(policy.get("block_threshold", 6)):
        return REJECT
    if maximum >= int(policy.get("hide_threshold", 6)):
        return REPORT_AND_HIDE
    if maximum >= int(policy.get("report_threshold", 4)):
        return REPORT_ONLY
    return NO_ACTION


def should_report(maximum: int) -> bool:
    return classify_action(maximum) in (REPORT_ONLY, REPORT_AND_HIDE)


def should_hide(maximum: int) -> bool:
    return classify_action(maximum) == REPORT_AND_HIDE


def should_report_for(content_type: str, maximum: int) -> bool:
    return classify_for_content(content_type, maximum) in (
        REPORT_ONLY,
        REPORT_AND_HIDE,
    )


def should_hide_for(content_type: str, maximum: int) -> bool:
    return classify_for_content(content_type, maximum) == REPORT_AND_HIDE


def should_block_for(content_type: str, maximum: int) -> bool:
    """Whether this item must be rejected outright (not stored/published)."""
    policy = _POLICIES.get(content_type)
    if policy is None:
        raise ContentSafetyError(
            f"Content moderation is not implemented for {content_type!r}.",
            code="unsupported_item",
        )
    if not policy.get("block"):
        return False
    return maximum >= int(policy.get("block_threshold", 6))


def build_report_reason(scores: Dict[str, int], maximum: int) -> str:
    """Human-readable reason stored on the existing report row.

    Per-category scores, the maximum, and a note that the report was
    automatically generated -- fits reports.reason (String(1000)).
    """
    return build_report_reason_for(CONTENT_CONFESSION, scores, maximum)


def build_report_reason_for(
    content_type: str, scores: Dict[str, int], maximum: int
) -> str:
    """Reason for one item type; confession wording is the Batch 1 contract."""
    if content_type not in _POLICIES:
        raise ContentSafetyError(
            f"Content moderation is not implemented for {content_type!r}.",
            code="unsupported_item",
        )
    parts = ", ".join(f"{cat}={int(scores.get(cat, 0) or 0)}" for cat in CATEGORIES)
    return (
        f"Automatically generated by Azure AI Content Safety. "
        f"Max severity {int(maximum)}. Scores: {parts}."
    )


def _normalize_category(value: Any) -> Optional[str]:
    """Map an SDK category (enum or str) to our canonical ``CATEGORIES`` name."""
    if value is None:
        return None
    text = getattr(value, "value", value)
    text = str(text or "").strip()
    for canonical in CATEGORIES:
        if text.lower() == canonical.lower():
            return canonical
    return None


def get_content_safety_client(config: ContentSafetyConfig):
    """Build the official SDK client for this configuration."""
    try:
        from azure.ai.contentsafety import ContentSafetyClient
        from azure.core.credentials import AzureKeyCredential
    except ImportError as exc:
        raise ContentSafetyError(
            "Content moderation is unavailable: the azure-ai-contentsafety "
            "package is not installed.",
            code="unavailable",
        ) from exc
    _require_config(config)
    endpoint = str(config.endpoint or "").strip().rstrip("/")
    key = str(config.api_key or "").strip()
    return ContentSafetyClient(
        endpoint, AzureKeyCredential(key), api_version=API_VERSION
    )


def _map_sdk_error(exc: BaseException) -> ContentSafetyError:
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
        return ContentSafetyError(
            "The moderation provider refused the credentials. "
            "Check CONTENT_SAFETY_API_KEY.",
            code="auth",
        )
    if status_int == 429 or "toomanyrequests" in err_code or "rate" in combined and "limit" in combined:
        return ContentSafetyError(
            "The moderation provider rate-limited the request. Retry shortly.",
            code="rate_limited",
        )
    if status_int == 400 and ("exceed" in combined or "too long" in combined or "10000" in combined):
        return ContentSafetyError(
            "The submitted text exceeds the moderation service limit.",
            code="invalid_input",
        )
    if status_int is not None and 500 <= status_int <= 599:
        return ContentSafetyError(
            "The moderation provider is temporarily unavailable. Retry shortly.",
            code="unavailable",
        )
    lowered_name = name.lower()
    if "timeout" in lowered_name or "timeout" in combined:
        return ContentSafetyError(
            "The moderation request timed out or could not connect. "
            "Retry the operation.",
            code="timeout",
        )
    if "service_request" in lowered_name or "connection" in lowered_name or "network" in lowered_name:
        return ContentSafetyError(
            "Could not reach the moderation provider. Check CONTENT_SAFETY_ENDPOINT.",
            code="network",
        )
    if status_int is not None:
        return ContentSafetyError(
            f"The moderation request failed (status {status_int}).",
            code="model_error",
        )
    return ContentSafetyError(
        "The moderation request failed.",
        code="model_error",
    )


def _call_sdk(
    client: Any, operation: str, options: Any, config: ContentSafetyConfig
) -> Any:
    """Run one SDK analyze operation and classify any failure.

    Shared by Analyze Text and Analyze Image so credentials, timeouts and
    provider bodies are mapped in one place; the client is closed either way,
    and neither the payload nor the key is ever logged.
    """
    try:
        # Per-call client keeps credential/timeout rotation test-safe.
        # azure-core honors timeout kwarg on the operation when supported.
        if hasattr(client, "__enter__"):
            with client as live:
                return getattr(live, operation)(
                    options, timeout=float(config.timeout_seconds)
                )
        return getattr(client, operation)(options, timeout=float(config.timeout_seconds))
    except Exception as exc:
        # HttpResponseError, ServiceRequestError, etc. Never include body/key.
        if isinstance(exc, ContentSafetyError):
            raise
        logger.warning("Content Safety SDK call failed: %s", type(exc).__name__)
        raise _map_sdk_error(exc) from exc
    finally:
        try:
            close = getattr(client, "close", None)
            if callable(close):
                close()
        except Exception:
            pass


def _scores_from_response(response: Any) -> Dict[str, int]:
    """Validate one analyze response into {category: severity}.

    Both operations answer with the same ``categories_analysis`` shape and the
    same four categories; both must fail closed on a malformed or incomplete
    body rather than silently score a missing category as zero.
    """
    try:
        items = getattr(response, "categories_analysis", None)
        if not isinstance(items, list):
            raise ValueError("missing categoriesAnalysis")
        scores: Dict[str, int] = {}
        for entry in items:
            cat = getattr(entry, "category", None)
            sev = getattr(entry, "severity", None)
            canonical = _normalize_category(cat)
            if canonical is not None:
                scores[canonical] = int(sev)
        for cat in CATEGORIES:
            if cat not in scores:
                raise ValueError(f"missing category {cat}")
            if scores[cat] not in (0, 2, 4, 6):
                raise ValueError(f"unexpected severity {scores[cat]!r} for {cat}")
    except ContentSafetyError:
        raise
    except Exception as exc:
        raise ContentSafetyError(
            "The moderation provider returned a malformed analysis response.",
            code="malformed",
        ) from exc
    return scores


def analyze_text(
    text: str,
    *,
    config: Optional[ContentSafetyConfig] = None,
) -> Dict[str, int]:
    """Analyze one text body and return {category: severity}."""
    if config is None:
        from ..config import settings as _settings

        config = config_from_settings(_settings)
    _require_config(config)
    body_text = text or ""
    # Azure counts Unicode code points; len() on str is exactly that in Python.
    if len(body_text) > TEXT_MAX_LENGTH:
        raise ContentSafetyError(
            f"Submitted text exceeds the moderation limit ({TEXT_MAX_LENGTH} characters).",
            code="invalid_input",
        )
    if not body_text.strip():
        raise ContentSafetyError(
            "Empty text cannot be moderated.",
            code="invalid_input",
        )
    # Official SDK (ContentSafetyClient + AnalyzeTextOptions, api-version
    # 2024-09-01). Sync client runs in the existing sync service path
    # (threadpool), so no event-loop blocking beyond the previous httpx usage.
    try:
        from azure.ai.contentsafety.models import AnalyzeTextOptions
    except ImportError as exc:
        raise ContentSafetyError(
            "Content moderation is unavailable: the azure-ai-contentsafety "
            "package is not installed.",
            code="unavailable",
        ) from exc
    try:
        client = get_content_safety_client(config)
    except ContentSafetyError:
        raise
    except Exception as exc:
        raise ContentSafetyError(
            "Content moderation is not configured.",
            code="missing_config",
        ) from exc
    # Pass plain strings (not the TextCategory enum): the SDK stores enums
    # as "TextCategory.HATE" locally, while strings round-trip as
    # "Hate"/"SelfHarm"/"Sexual"/"Violence" and serialize correctly.
    options = AnalyzeTextOptions(
        text=body_text,
        categories=list(CATEGORIES),
        output_type=OUTPUT_TYPE,
    )
    response = _call_sdk(client, "analyze_text", options, config)
    scores = _scores_from_response(response)
    # Never log the text itself or secrets; lengths + per-category scores
    # + max are operational and safe.
    logger.info(
        "Content Safety analysis complete (%s chars, max=%s, scores Hate=%s SelfHarm=%s Sexual=%s Violence=%s)",
        len(body_text),
        max_severity(scores),
        int(scores.get("Hate", 0) or 0),
        int(scores.get("SelfHarm", 0) or 0),
        int(scores.get("Sexual", 0) or 0),
        int(scores.get("Violence", 0) or 0),
    )
    return scores


def analyze_image(
    image: bytes,
    *,
    config: Optional[ContentSafetyConfig] = None,
) -> Dict[str, int]:
    """Analyze one image's bytes and return {category: severity}."""
    if config is None:
        from ..config import settings as _settings

        config = config_from_settings(_settings)
    _require_config(config)
    body = image or b""
    if not body:
        raise ContentSafetyError(
            "An empty image cannot be moderated.",
            code="invalid_input",
        )
    # Official SDK (ContentSafetyClient + AnalyzeImageOptions/ImageData,
    # api-version 2024-09-01).
    try:
        from azure.ai.contentsafety.models import AnalyzeImageOptions, ImageData
    except ImportError as exc:
        raise ContentSafetyError(
            "Content moderation is unavailable: the azure-ai-contentsafety "
            "package is not installed.",
            code="unavailable",
        ) from exc
    try:
        client = get_content_safety_client(config)
    except ContentSafetyError:
        raise
    except Exception as exc:
        raise ContentSafetyError(
            "Content moderation is not configured.",
            code="missing_config",
        ) from exc
    # Pass plain strings (not the ImageCategory enum) for the same reason as
    # Analyze Text: strings serialize as the documented category names.
    options = AnalyzeImageOptions(
        image=ImageData(content=body),
        categories=list(CATEGORIES),
        output_type=OUTPUT_TYPE,
    )
    response = _call_sdk(client, "analyze_image", options, config)
    scores = _scores_from_response(response)
    # Never log the image bytes or secrets; size + per-category scores
    # + max are operational and safe.
    logger.info(
        "Content Safety image analysis complete (%s bytes, max=%s, scores Hate=%s SelfHarm=%s Sexual=%s Violence=%s)",
        len(body),
        max_severity(scores),
        int(scores.get("Hate", 0) or 0),
        int(scores.get("SelfHarm", 0) or 0),
        int(scores.get("Sexual", 0) or 0),
        int(scores.get("Violence", 0) or 0),
    )
    return scores


def analyze_confession_text(
    text: str,
    *,
    config: Optional[ContentSafetyConfig] = None,
) -> Dict[str, int]:
    """Backward-compatible alias for :func:`analyze_text` (confessions).

    Kept so existing callers and test mocks on this name keep working; new
    code should call :func:`moderate_content` centrally instead.
    """
    return analyze_text(text, config=config)


# ---------------------------------------------------------------- central call
from dataclasses import dataclass as _dataclass


@_dataclass
class ModerationDecision:
    """One central moderation verdict for one submitted item.

    Callers branch only on these flags -- never on raw severities. ``reason``
    is the prebuilt automated-report text for items that report; items that
    never report leave it empty.
    """

    content_type: str
    scores: Dict[str, int]
    max_severity: int
    action: str
    should_report: bool
    should_hide: bool
    should_block: bool
    report_reason: str


def _decision_for(
    content_type: str, scores: Dict[str, int]
) -> ModerationDecision:
    maximum = max_severity(scores)
    action = classify_for_content(content_type, maximum)
    return ModerationDecision(
        content_type=content_type,
        scores=dict(scores),
        max_severity=maximum,
        action=action,
        should_report=should_report_for(content_type, maximum),
        should_hide=should_hide_for(content_type, maximum),
        should_block=should_block_for(content_type, maximum),
        report_reason=(
            build_report_reason_for(content_type, scores, maximum)
            if should_report_for(content_type, maximum)
            else ""
        ),
    )


def moderate_content(
    content_type: str,
    content: Union[str, bytes],
    *,
    config: Optional[ContentSafetyConfig] = None,
) -> ModerationDecision:
    """Moderate one item centrally: analyze + per-item policy in one call."""
    if content_type not in _POLICIES:
        raise ContentSafetyError(
            f"Content moderation is not implemented for {content_type!r}.",
            code="unsupported_item",
        )
    if config is None:
        from ..config import settings as _settings

        config = config_from_settings(_settings)
    # Route through the confession analyzer for text items so existing mocks
    # on that name keep intercepting; all text items share the same Analyze
    # Text operation. Image items are the only ones on Analyze Image.
    if content_type in _IMAGE_CONTENT_TYPES:
        scores = analyze_image(content, config=config)
    else:
        scores = analyze_confession_text(content, config=config)
    return _decision_for(content_type, scores)


def moderate_confession_text(
    text: str,
    *,
    config: Optional[ContentSafetyConfig] = None,
) -> ModerationDecision:
    """Central confession moderation (thin wrapper over :func:`moderate_content`)."""
    return moderate_content(CONTENT_CONFESSION, text, config=config)


def moderate_comment_text(
    text: str,
    *,
    config: Optional[ContentSafetyConfig] = None,
) -> ModerationDecision:
    """Central comment moderation (same principal/logic as confessions)."""
    return moderate_content(CONTENT_COMMENT, text, config=config)


def moderate_chat_message_text(
    text: str,
    *,
    config: Optional[ContentSafetyConfig] = None,
) -> ModerationDecision:
    """Central chat-message moderation (same principal/logic as confessions)."""
    return moderate_content(CONTENT_CHAT_MESSAGE, text, config=config)


def moderate_case_closed_message_text(
    text: str,
    *,
    config: Optional[ContentSafetyConfig] = None,
) -> ModerationDecision:
    """Central Case Closed turn moderation (same principal/logic as confessions)."""
    return moderate_content(CONTENT_CASE_CLOSED_MESSAGE, text, config=config)


def moderate_profile_picture(
    image: bytes,
    *,
    config: Optional[ContentSafetyConfig] = None,
) -> ModerationDecision:
    """Central profile-picture moderation (prepared image bytes)."""
    return moderate_content(CONTENT_PROFILE_PICTURE, image, config=config)


def moderation_error_to_http(
    exc: ContentSafetyError, *, content_type: str = CONTENT_CONFESSION
) -> Any:
    """Map a moderation failure to a retryable HTTP error (central)."""
    from fastapi import HTTPException as _HTTPException
    from fastapi import status as _status

    if getattr(exc, "code", None) == "missing_config":
        detail = "Content moderation is not configured."
    else:
        detail = (
            "Content moderation is temporarily unavailable. "
            "Please retry shortly."
        )
    return _HTTPException(
        status_code=_status.HTTP_503_SERVICE_UNAVAILABLE,
        detail=detail,
    )

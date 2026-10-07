"""Central Azure AI Content Safety entry point (confessions + comments).

All moderation goes through this module so features never carry their own
local configuration, HTTP client, or severity thresholds. Callers use the
central dispatcher :func:`moderate_content` (or the thin
:func:`moderate_confession_text` / :func:`moderate_comment_text` wrappers)
and branch only on the returned decision -- never on raw numeric severities
and never by reading ``CONTENT_SAFETY_*`` settings directly.

Confessions, confession comments and chat messages share the same principal/
logic/report/hide mechanism (everything >1 flagged+hidden). Images (Cloudinary
pre-upload) and AI-output validation are reserved item types for later batches.
Calling :func:`moderate_content` with any other item raises
``ContentSafetyError(code="unsupported_item")`` fail-closed.

Batch 1 flow (see ConfessionService.create_confession):
  1. Analyze the confession body with Analyze Text (2024-09-01).
  2. Take the maximum per-category severity (0, 2, 4 or 6).
  3. Severity 0: no automatic escalation.
     Severity 2/4/6: automatic report + hide via the existing mechanism
     (everything >1 flagged and hidden).
  4. The existing Azure AI Foundry embedding workflow still runs for every
     successfully moderated confession, including flagged ones.

Only this module talks to the Content Safety endpoint, so credentials,
timeouts and severity policy live in one place. Callers never scatter
numeric severity checks.

Transport uses the official SDK per
https://github.com/Azure/azure-sdk-for-python/tree/main/sdk/contentsafety/azure-ai-contentsafety
(``ContentSafetyClient`` + ``AnalyzeTextOptions``, api-version 2024-09-01):

    client = ContentSafetyClient(endpoint, AzureKeyCredential(key),
                                 api_version="2024-09-01")
    client.analyze_text(AnalyzeTextOptions(
        text="<confession>",
        categories=[HATE, SELF_HARM, SEXUAL, VIOLENCE],
        output_type="FourSeverityLevels"))

REST equivalent (2024-09-01, per
https://learn.microsoft.com/en-us/rest/api/contentsafety/text-operations/analyze-text):

    POST {endpoint}/contentsafety/text:analyze?api-version=2024-09-01

Only the submitted text is analyzed. The request analyzes the entire body
subject to Azure's 10k-character limit; longer input is refused as an error
rather than silently truncated and treated as safe.
"""

from dataclasses import dataclass
from typing import Any, Dict, Optional

from ..logger import get_logger

logger = get_logger(__name__)

API_VERSION = "2024-09-01"
CATEGORIES = ("Hate", "SelfHarm", "Sexual", "Violence")
OUTPUT_TYPE = "FourSeverityLevels"

# Azure documents max 10k Unicode characters per Analyze Text request.
TEXT_MAX_LENGTH = 10000

# Central item vocabulary. Confessions, their comments and chat messages are
# moderated today; images/AI outputs remain reserved so future batches plug
# handling into _POLICIES instead of each feature wiring its own
# endpoint/key/timeout/policy.
CONTENT_CONFESSION = "confession"
CONTENT_COMMENT = "comment"
CONTENT_CHAT_MESSAGE = "chat_message"
CONTENT_IMAGE = "image"
CONTENT_AI_OUTPUT = "ai_output"

SUPPORTED_CONTENT_TYPES: tuple[str, ...] = (
    CONTENT_CONFESSION,
    CONTENT_COMMENT,
    CONTENT_CHAT_MESSAGE,
)

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
# 0: no escalation. 2/4/6: report + hide (everything >1 flagged+hidden).
NO_ACTION = "none"
REPORT_ONLY = "report"
REPORT_AND_HIDE = "report_and_hide"

# Per-item handling. Confessions, their comments and chat messages share the
# same principal/logic (everything >1 flagged+hidden); images/AI outputs plug
# in here in later batches. Central dispatcher reads this -- callers never
# hardcode thresholds. None of these block: a flagged row is still stored
# (born hidden for chat, so it never reaches the recipient) and carries an
# automated report for audit.
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


def max_severity(scores: Dict[str, int]) -> int:
    """Maximum severity across the four categories (0, 2, 4 or 6).

    Categories are evaluated independently; the highest wins. Scores are
    never averaged and a lower score never cancels a higher one.
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
    """Policy action for one item type and one maximum severity."""
    policy = _POLICIES.get(content_type)
    if policy is None:
        raise ContentSafetyError(
            f"Content moderation is not implemented for {content_type!r}.",
            code="unsupported_item",
        )
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
    """Whether this item must be rejected outright (not stored/published).

    Confessions never block: even severity 6 is stored hidden with an
    embedding. Future items (e.g. comments/chat) may block instead; their
    policies declare ``"block": True`` with their own thresholds.
    """
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

    Records the per-category scores and the maximum, and states that the
    report was automatically generated. Fits well within reports.reason
    (String(1000)).
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
    """Build the official SDK client for this configuration.

    Uses ``azure-ai-contentsafety`` ``ContentSafetyClient`` with API version
    ``2024-09-01`` (the SDK default is older). One client per call: creation
    is cheap next to the network round-trip, and per-call construction keeps
    credential/timeout rotation test-safe. Callers must close it (we use it
    as a context manager in :func:`analyze_text`).
    """
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


def analyze_text(
    text: str,
    *,
    config: Optional[ContentSafetyConfig] = None,
) -> Dict[str, int]:
    """Analyze one text body and return {category: severity}.

    Shared by every text item (confessions today; comments/chat/AI outputs
    when they land). Raises ContentSafetyError on missing config, over-long
    input, timeouts, auth failures and provider errors. Never returns a
    default score on failure: callers treat failure as unknown, never as safe.
    """
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
    # Official SDK per
    # https://github.com/Azure/azure-sdk-for-python/tree/main/sdk/contentsafety/azure-ai-contentsafety
    # (ContentSafetyClient + AnalyzeTextOptions, api-version 2024-09-01).
    # Sync client runs in the existing sync service path (threadpool), so no
    # event-loop blocking beyond the previous httpx usage.
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
    try:
        # Per-call client keeps credential/timeout rotation test-safe.
        # azure-core honors timeout kwarg on the operation when supported.
        if hasattr(client, "__enter__"):
            with client as live:
                response = live.analyze_text(
                    options, timeout=float(config.timeout_seconds)
                )
        else:
            response = client.analyze_text(
                options, timeout=float(config.timeout_seconds)
            )
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


def analyze_confession_text(
    text: str,
    *,
    config: Optional[ContentSafetyConfig] = None,
) -> Dict[str, int]:
    """Backward-compatible alias for :func:`analyze_text` (confessions).

    Kept so existing callers and test mocks on this name keep working;
    new code should call :func:`moderate_content` centrally instead of
    analyzing directly.
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
    text: str,
    *,
    config: Optional[ContentSafetyConfig] = None,
) -> ModerationDecision:
    """Moderate one item centrally: analyze + per-item policy in one call.

    This is the only entry future features should use. Configuration is
    loaded here, so comment/chat/image/AI-output paths never carry their own
    local ``config_from_settings``/endpoint/key handling. Today only
    ``"confession"`` is supported; any other ``content_type`` raises
    ``ContentSafetyError(code="unsupported_item")`` fail-closed so the
    confession policy can never silently apply elsewhere.

    Failures are unknown, never safe: every ``ContentSafetyError`` from the
    analyzer propagates unchanged for the caller to turn into a retryable
    error before persistence.
    """
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
    # Text operation until image/AI-output analyzers land.
    scores = analyze_confession_text(text, config=config)
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


def moderation_error_to_http(
    exc: ContentSafetyError, *, content_type: str = CONTENT_CONFESSION
) -> Any:
    """Map a moderation failure to a retryable HTTP error (central).

    Missing configuration is a deployment error with a distinct message;
    every other failure is a transient provider failure. Never includes
    secrets or the submitted text. Callers raise the returned exception
    before persistence so nothing unmoderated leaks.
    """
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

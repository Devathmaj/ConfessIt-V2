"""Centralized Azure embedding service for confession semantic search.

Only this module talks to the embedding provider, so the application can change
providers later without rewriting search. Confession and query embeddings always
use the same configured model (``text-embedding-3-small``) and dimensions (1536).

Configuration (app.config.Settings, from the runtime environment)::

    EMBEDDING_ENDPOINT="https://confessit-model.services.ai.azure.com"
    EMBEDDING_API_KEY="..."                # never logged, never committed
    EMBEDDING_MODEL="text-embedding-3-small"
    EMBEDDING_DIMENSIONS="1536"
    EMBEDDING_SEARCH_ENABLED="true"

Azure API contract implemented here (Foundry v1 embeddings, per
https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/embeddings and
.../api-version-lifecycle):

    POST {endpoint}/openai/v1/embeddings
    Headers: api-key: <EMBEDDING_API_KEY>, Content-Type: application/json
    Body:    {"model": "<deployment>", "input": ["<normalized text>"], "dimensions": 1536}

``base_url`` accepts both ``*.openai.azure.com/openai/v1/`` and
``*.services.ai.azure.com/openai/v1/``. No ``api-version`` query param on v1.
``model`` in the body is the Azure deployment name (which often equals the
model id ``text-embedding-3-small``).

Response::

    {"data": [{"embedding": [1536 floats], "index": 0}], ...}

``deployment`` is ``EMBEDDING_DEPLOYMENT`` when set, else ``EMBEDDING_MODEL``.
Azure distinguishes a deployment name from the underlying model id on some
resources; this module accounts for that distinction explicitly instead of
inventing a name silently. The response embedding is validated to contain
exactly 1536 finite numbers; vectors are never truncated, padded, or reshaped.

Only the confession's own public text is embedded (body plus the optional public
recipient name). Comments, replies, usernames, emails, author ids, reports,
moderation notes, and engagement counts are never sent.
"""

import math
import threading
import time
from dataclasses import dataclass
from typing import Any, Optional

import httpx

from ..logger import get_logger

logger = get_logger(__name__)

EXPECTED_MODEL = "text-embedding-3-small"
EXPECTED_DIMENSIONS = 1536

# Bounds for natural-language search queries and confession documents sent to
# the provider. Queries are short phrases; the confession body cap is 500, so
# 2000 comfortably holds "To <recipient>: <body>" without accepting abuse.
SEARCH_QUERY_MAX_LENGTH = 1000
EMBEDDING_INPUT_MAX_LENGTH = 2000

_client: httpx.Client | None = None
_client_lock = threading.Lock()


class EmbeddingError(Exception):
    """An embedding failure carrying a branchable code and a safe message.

    Messages never contain the API key or confession text (only lengths, ids,
    and provider codes), so they are safe to log and, where appropriate, to
    surface as API details.
    """

    def __init__(self, detail: str, code: str) -> None:
        super().__init__(detail)
        self.detail = detail
        self.code = code


@dataclass
class EmbeddingConfig:
    endpoint: str
    api_key: Optional[str]
    model: str
    dimensions: int
    enabled: bool
    api_version: str
    deployment: Optional[str]
    timeout_seconds: float
    max_retries: int


def config_from_settings(settings: Any) -> EmbeddingConfig:
    """Read embedding configuration from the application settings object."""
    return EmbeddingConfig(
        endpoint=(getattr(settings, "EMBEDDING_ENDPOINT", None) or "").strip().rstrip("/"),
        api_key=getattr(settings, "EMBEDDING_API_KEY", None),
        model=(getattr(settings, "EMBEDDING_MODEL", None) or "").strip(),
        dimensions=int(getattr(settings, "EMBEDDING_DIMENSIONS", EXPECTED_DIMENSIONS)),
        enabled=bool(getattr(settings, "EMBEDDING_SEARCH_ENABLED", True)),
        api_version=(getattr(settings, "EMBEDDING_API_VERSION", None) or "2024-02-01").strip(),
        deployment=(getattr(settings, "EMBEDDING_DEPLOYMENT", None) or None),
        timeout_seconds=float(getattr(settings, "EMBEDDING_TIMEOUT_SECONDS", 10.0)),
        max_retries=int(getattr(settings, "EMBEDDING_MAX_RETRIES", 2)),
    )


def deployment_name_for(config: EmbeddingConfig) -> str:
    """Deployment path segment for the Azure URL.

    When the resource distinguishes a deployment name from the model id, the
    operator sets EMBEDDING_DEPLOYMENT explicitly. Otherwise the model name is
    used as the deployment name and documented as such.
    """
    name = (config.deployment or "").strip()
    return name or config.model


def embeddings_url_for(config: EmbeddingConfig) -> str:
    """Full Azure embeddings request URL for this configuration.

    Foundry v1: ``{endpoint}/openai/v1/embeddings`` with no api-version and no
    deployment in the path. The deployment travels in the JSON body as
    ``model`` (see _call_azure_embedding_api).
    """
    return f"{config.endpoint}/openai/v1/embeddings"


def normalize_text(value: str) -> str:
    """Collapse whitespace consistently for indexing and querying."""
    return " ".join((value or "").split())


def build_confession_document(body: Any, recipient_name: Any = None) -> str:
    """Build the single embedding input for a confession.

    Only the confession's own public text: the body plus the optional public
    recipient name (displayed on the card, so it materially improves search for
    "confessions to ..."). Never comments, identities, reports, moderation
    notes, or engagement metrics.
    """
    text_body = normalize_text(str(body or ""))
    recipient = normalize_text(str(recipient_name or ""))
    if recipient:
        # Truncate the body side so the combined document stays bounded; the
        # recipient prefix is short and always kept whole.
        combined = f"To {recipient}: {text_body}"
    else:
        combined = text_body
    if len(combined) > EMBEDDING_INPUT_MAX_LENGTH:
        combined = combined[:EMBEDDING_INPUT_MAX_LENGTH]
    return normalize_text(combined)


def validate_vector(vector: Any, *, expected_dimensions: int = EXPECTED_DIMENSIONS) -> list[float]:
    """Enforce exactly 1536 finite numbers; never truncate, pad, or reshape."""
    if not isinstance(vector, (list, tuple)):
        raise EmbeddingError(
            "The provider returned a malformed embedding (not a list).",
            code="malformed",
        )
    if len(vector) != expected_dimensions:
        raise EmbeddingError(
            f"The provider returned {len(vector)} dimensions, expected "
            f"{expected_dimensions}: vectors must never be truncated, padded, "
            "or reshaped.",
            code="dimension_mismatch",
        )
    cleaned: list[float] = []
    for value in vector:
        if isinstance(value, bool):
            raise EmbeddingError(
                "The provider returned a malformed embedding (non-numeric value).",
                code="malformed",
            )
        if not isinstance(value, (int, float)):
            raise EmbeddingError(
                "The provider returned a malformed embedding (non-numeric value).",
                code="malformed",
            )
        number = float(value)
        if not math.isfinite(number):
            raise EmbeddingError(
                "The provider returned a non-finite embedding value.",
                code="malformed",
            )
        cleaned.append(number)
    return cleaned


def _require_config(config: EmbeddingConfig) -> None:
    """Actionable errors for missing/invalid configuration without secrets."""
    if not config.enabled:
        raise EmbeddingError(
            "Semantic search is disabled (EMBEDDING_SEARCH_ENABLED is not true).",
            code="disabled",
        )
    if config.model != EXPECTED_MODEL:
        raise EmbeddingError(
            f"EMBEDDING_MODEL must be '{EXPECTED_MODEL}' (got {config.model!r}).",
            code="invalid_config",
        )
    if config.dimensions != EXPECTED_DIMENSIONS:
        raise EmbeddingError(
            f"EMBEDDING_DIMENSIONS must be {EXPECTED_DIMENSIONS} (got {config.dimensions}).",
            code="invalid_config",
        )
    if not config.endpoint:
        raise EmbeddingError(
            "Embedding is not configured: EMBEDDING_ENDPOINT is missing.",
            code="missing_config",
        )
    if not config.api_key or not str(config.api_key).strip():
        raise EmbeddingError(
            "Embedding is not configured: EMBEDDING_API_KEY is missing. "
            "Set it in the runtime environment.",
            code="missing_config",
        )


def get_http_client(timeout_seconds: float) -> httpx.Client:
    """Reuse one properly configured HTTP client across embedding calls."""
    global _client
    with _client_lock:
        if _client is None:
            _client = httpx.Client(timeout=httpx.Timeout(timeout_seconds))
            return _client
        # Recreate if the configured timeout drifted (tests mutate settings).
        try:
            current = _client.timeout
            read = getattr(current, "read", None)
            if read is not None and abs(float(read) - float(timeout_seconds)) > 1e-9:
                _client.close()
                _client = httpx.Client(timeout=httpx.Timeout(timeout_seconds))
        except Exception:
            pass
        return _client


def _is_transient_status(status_code: int) -> bool:
    return status_code == 429 or 500 <= status_code <= 599


def _classify_http_error(status_code: int, body_snippet: str) -> EmbeddingError:
    lowered = (body_snippet or "").lower()
    if "virtual network" in lowered or "cogsvc-vnet" in lowered:
        return EmbeddingError(
            "The embedding resource blocks this network (403 Virtual Network). "
            "Use the private endpoint allowed for the resource or allow this "
            "network in Azure Networking/Firewall; see https://aka.ms/cogsvc-vnet.",
            code="network_blocked",
        )
    if status_code in (401, 403):
        return EmbeddingError(
            "The embedding provider refused the credentials. "
            "Check EMBEDDING_API_KEY.",
            code="auth",
        )
    if status_code == 429:
        return EmbeddingError(
            "The embedding provider rate-limited the request. Retry shortly.",
            code="rate_limited",
        )
    if status_code == 404 or "deployment" in lowered and "not found" in lowered:
        return EmbeddingError(
            "The embedding model or deployment was not found. Check "
            "EMBEDDING_ENDPOINT, EMBEDDING_DEPLOYMENT, and EMBEDDING_MODEL.",
            code="model_error",
        )
    if 500 <= status_code <= 599:
        return EmbeddingError(
            "The embedding provider is temporarily unavailable. Retry shortly.",
            code="unavailable",
        )
    return EmbeddingError(
        f"The embedding request failed (status {status_code}).",
        code="model_error",
    )


def _redacted_snippet(text_value: str, limit: int = 300) -> str:
    """A log-safe snippet of provider output (never keys or confession text)."""
    cleaned = " ".join((text_value or "").split())
    for marker in ("api-key", "apikey", "bearer ", "authorization"):
        if marker in cleaned.lower():
            return "Response contained credential-shaped text; withheld."
    return cleaned[:limit]


def _call_azure_embedding_api(document: str, config: EmbeddingConfig) -> list[float]:
    """POST one normalized document to Azure and return its validated vector."""
    url = embeddings_url_for(config)
    headers = {
        "api-key": str(config.api_key or "").strip(),
        "Content-Type": "application/json",
    }
    payload = {
        "model": deployment_name_for(config),
        "input": [document],
        "dimensions": config.dimensions,
    }
    client = get_http_client(config.timeout_seconds)
    attempts = max(0, int(config.max_retries)) + 1
    last_error: EmbeddingError | None = None
    for attempt in range(attempts):
        try:
            response = client.post(url, headers=headers, json=payload)
        except (httpx.TimeoutException, httpx.ConnectError) as exc:
            last_error = EmbeddingError(
                "The embedding request timed out or could not connect. "
                "Retry the operation.",
                code="timeout" if isinstance(exc, httpx.TimeoutException) else "network",
            )
            logger.warning(
                "Embedding request transport failure (attempt %s/%s): %s",
                attempt + 1, attempts, type(exc).__name__,
            )
        except httpx.HTTPError as exc:
            last_error = EmbeddingError(
                "Could not reach the embedding provider. Check EMBEDDING_ENDPOINT.",
                code="network",
            )
            logger.warning("Embedding request failed: %s", type(exc).__name__)
        else:
            if response.status_code == 200:
                try:
                    data = response.json()
                except Exception as exc:
                    raise EmbeddingError(
                        "The provider returned a response that was not valid JSON.",
                        code="malformed",
                    ) from exc
                try:
                    items = data.get("data") if isinstance(data, dict) else None
                    first = items[0] if isinstance(items, list) and items else None
                    vector = first.get("embedding") if isinstance(first, dict) else None
                except Exception as exc:
                    raise EmbeddingError(
                        "The provider returned a malformed embedding response.",
                        code="malformed",
                    ) from exc
                if vector is None:
                    raise EmbeddingError(
                        "The provider returned a malformed embedding response.",
                        code="malformed",
                    )
                return validate_vector(vector, expected_dimensions=config.dimensions)
            snippet = _redacted_snippet(getattr(response, "text", "") or "")
            error = _classify_http_error(response.status_code, snippet)
            # Permanent configuration failures are never retried indefinitely.
            if error.code in ("auth", "model_error", "network_blocked") or (
                error.code == "unavailable" and response.status_code < 500
            ):
                raise error
            if response.status_code in (401, 403, 400, 404):
                raise error
            last_error = error
            logger.warning(
                "Embedding request status %s (attempt %s/%s)",
                response.status_code, attempt + 1, attempts,
            )
            if not _is_transient_status(response.status_code):
                raise error
        if attempt < attempts - 1:
            # Bounded exponential backoff for transient failures only.
            time.sleep(min(2.0 ** attempt * 0.5, 4.0))
    assert last_error is not None
    raise last_error


def embed_text(
    value: str,
    *,
    config: Optional[EmbeddingConfig] = None,
    purpose: str = "text",
) -> list[float]:
    """Embed one normalized document or query with the configured model."""
    if config is None:
        from ..config import settings as _settings

        config = config_from_settings(_settings)
    _require_config(config)
    document = normalize_text(value or "")
    if not document:
        raise EmbeddingError(
            "Empty text cannot be embedded.",
            code="invalid_input",
        )
    if len(document) > EMBEDDING_INPUT_MAX_LENGTH and purpose == "confession":
        document = normalize_text(document[:EMBEDDING_INPUT_MAX_LENGTH])
    # Never log the text itself: only its length and purpose.
    logger.info("Requesting confession embedding (%s chars, purpose=%s)", len(document), purpose)
    vector = _call_azure_embedding_api(document, config)
    return validate_vector(vector, expected_dimensions=config.dimensions)


def embed_confession(
    body: Any,
    recipient_name: Any = None,
    *,
    config: Optional[EmbeddingConfig] = None,
) -> list[float]:
    """Embed a confession's public text (body + optional recipient name)."""
    document = build_confession_document(body, recipient_name)
    if not document:
        raise EmbeddingError(
            "Empty confession text cannot be embedded.",
            code="invalid_input",
        )
    return embed_text(document, config=config, purpose="confession")


def embed_query(
    query: str,
    *,
    config: Optional[EmbeddingConfig] = None,
) -> list[float]:
    """Embed a natural-language search query with the same model."""
    normalized = normalize_text(query or "")
    if not normalized:
        raise EmbeddingError(
            "Search query must not be blank.",
            code="invalid_input",
        )
    if len(normalized) > SEARCH_QUERY_MAX_LENGTH:
        raise EmbeddingError(
            f"Search query is too long (max {SEARCH_QUERY_MAX_LENGTH} characters).",
            code="invalid_input",
        )
    return embed_text(normalized, config=config, purpose="query")

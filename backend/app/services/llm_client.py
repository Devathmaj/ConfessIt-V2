"""Provider-configurable LLM client for pre-publication story generation.

One module owns every provider-specific setting so story-generation logic
never does. Configuration comes from the environment only::

    LLM_BASE_URL  e.g. https://api.groq.com/openai/v1 (Groq, OpenAI-compatible)
    LLM_API_KEY   the provider secret; never logged, never sent to clients
    LLM_MODEL     e.g. openai/gpt-oss-120b (Groq) or a Foundry deployment name

The OpenAI Python SDK talks to any OpenAI-compatible ``/v1`` surface, which
is what makes the provider switch a configuration change rather than a code
change.

Provider-specific differences that require configuration changes (not code):

* Groq exposes an OpenAI-compatible API at
  ``https://api.groq.com/openai/v1`` and honours ``response_format`` JSON
  Schema Structured Outputs for supported models. Set ``LLM_MODEL`` to the
  Groq model id (initial development: ``openai/gpt-oss-120b``).
* Microsoft Foundry also serves an OpenAI-compatible chat-completions
  surface, but the base URL is deployment-specific
  (``https://<resource>.openai.azure.com/openai/v1`` or the Foundry
  project endpoint) and ``LLM_MODEL`` must be the *deployment name*, not
  the underlying model id. Some Foundry deployments do not support strict
  JSON Schema mode; the client then falls back to plain JSON-object mode
  and validates locally, recording the fallback. Verify structured-output
  support for the chosen deployment and api-version.

Structured Outputs are requested with a strict JSON Schema whenever the
provider accepts one. Every payload is still validated locally with
Pydantic: a model that drifts from the schema is a validation failure,
never an exception in gameplay.

Nothing here runs during gameplay. Generation happens before publication;
the engine executes published packages deterministically.
"""

import json
import time
from dataclasses import dataclass
from typing import Any, Optional

from ..logger import get_logger

logger = get_logger(__name__)


class LLMError(Exception):
    """An admin-safe generation failure carrying a branchable code.

    ``detail`` is safe to show to administrators: it never contains the API
    key, request bodies beyond ids, or tracebacks.
    """

    def __init__(self, detail: str, code: str) -> None:
        super().__init__(detail)
        self.detail = detail
        self.code = code


@dataclass
class LLMConfig:
    base_url: Optional[str]
    model: Optional[str]
    # The key travels only into the SDK client; it is never logged or stored.
    api_key: Optional[str] = None


def config_from_settings(settings: Any) -> LLMConfig:
    """Read provider configuration from the application settings object."""
    return LLMConfig(
        base_url=getattr(settings, "LLM_BASE_URL", None),
        api_key=getattr(settings, "LLM_API_KEY", None),
        model=getattr(settings, "LLM_MODEL", None),
    )


def _redacted(detail: str) -> str:
    """Strip anything shaped like a secret from an error sentence."""
    lowered = detail.lower()
    for marker in ("bearer ", "api-key", "apikey"):
        if marker in lowered:
            return "The provider refused the request (credential or request error)."
    return detail[:500]


def generate_structured_json(
    *,
    system: str,
    user: str,
    json_schema: dict,
    schema_name: str,
    operation: str,
    timeout_s: int = 120,
    max_tokens: int = 8000,
    config: Optional[LLMConfig] = None,
) -> tuple[dict, dict]:
    """Ask the model for one JSON document honouring ``json_schema``.

    Returns ``(payload, meta)`` where ``meta`` records the model, the
    operation id and whether strict schema mode or the JSON-object fallback
    was used. Raises :class:`LLMError` with an admin-safe message on
    timeouts, rate limits, network failures, malformed responses,
    unsupported schemas and model errors.
    """
    if config is None:
        from ..config import settings

        config = config_from_settings(settings)
    operation_id = f"{operation}-{int(time.time() * 1000) % 1_000_000_000}"
    if not config.base_url or not config.model:
        raise LLMError(
            "Story generation is not configured: LLM_BASE_URL and LLM_MODEL "
            "must both be set.",
            code="missing_config",
        )
    if not config.api_key:
        raise LLMError(
            "Story generation is not configured: LLM_API_KEY is missing.",
            code="missing_config",
        )
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise LLMError(
            "Story generation is unavailable: the OpenAI SDK is not installed.",
            code="unsupported_schema",
        ) from exc

    client = OpenAI(base_url=config.base_url, api_key=config.api_key)
    strict_format: dict[str, Any] = {
        "type": "json_schema",
        "json_schema": {"name": schema_name, "strict": True, "schema": json_schema},
    }
    logger.info(
        "Plot Twist generation started",
        extra={"operation_id": operation_id, "operation": operation},
    )
    payload: Optional[dict] = None
    used_fallback = False
    try:
        try:
            response = client.chat.completions.create(
                model=config.model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                response_format=strict_format,  # type: ignore[arg-type]
                timeout=timeout_s,
                max_tokens=max_tokens,
            )
        except Exception as strict_exc:
            # Strict JSON Schema unsupported by this deployment: one retry in
            # plain JSON-object mode, validated locally afterwards.
            message = _redacted(str(strict_exc))
            if "response_format" not in message and "json_schema" not in message:
                raise
            used_fallback = True
            logger.info(
                "Plot Twist strict schema unsupported, using JSON-object fallback",
                extra={"operation_id": operation_id},
            )
            response = client.chat.completions.create(
                model=config.model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                response_format={"type": "json_object"},  # type: ignore[arg-type]
                timeout=timeout_s,
                max_tokens=max_tokens,
            )
    except LLMError:
        raise
    except Exception as exc:
        raise _classify_provider_error(exc) from exc

    try:
        content = response.choices[0].message.content or ""
        payload = json.loads(content)
    except Exception as exc:
        raise LLMError(
            "The model returned a response that was not valid JSON.",
            code="malformed",
        ) from exc
    if not isinstance(payload, dict):
        raise LLMError(
            "The model returned a JSON array where an object was required.",
            code="malformed",
        )
    logger.info(
        "Plot Twist generation finished",
        extra={"operation_id": operation_id, "fallback": used_fallback},
    )
    meta = {
        "operation_id": operation_id,
        "model": config.model,
        "strict_schema": not used_fallback,
    }
    return payload, meta


def _classify_provider_error(exc: Exception) -> LLMError:
    """Map SDK / transport failures to admin-safe codes without secrets."""
    name = type(exc).__name__.lower()
    message = _redacted(str(exc))
    if "timeout" in name or "timed out" in message.lower():
        return LLMError(
            "The story-generation request timed out. Retry the operation.",
            code="timeout",
        )
    if "ratelimit" in name or "429" in message or "rate limit" in message.lower():
        return LLMError(
            "The AI provider rate-limited the request. Wait briefly and retry.",
            code="rate_limited",
        )
    if "connection" in name or "network" in name or "unreachable" in message.lower():
        return LLMError(
            "Could not reach the AI provider. Check LLM_BASE_URL and retry.",
            code="network",
        )
    if "auth" in name or "401" in message or "403" in message:
        return LLMError(
            "The AI provider refused the credentials. Check LLM_API_KEY.",
            code="model_error",
        )
    if "bad request" in message.lower() or "400" in message:
        return LLMError(
            f"The provider could not process the request: {message}",
            code="model_error",
        )
    return LLMError(
        f"Story generation failed: {message}", code="model_error"
    )

"""Provider-neutral LLM client for pre-publication story generation.

Settings select Groq or Microsoft Foundry through one structured-JSON contract. The client
uses strict schema output when supported, validates every response locally, and reports
provider-safe errors; gameplay executes only validated, published packages.
"""

import json
import time
from dataclasses import dataclass
from typing import Any, Optional

from ..logger import get_logger

logger = get_logger(__name__)

# Provider defaults are fixed; schema validation governs generated story structure.
_GROQ_REASONING_EFFORT = "medium"


def _groq_sampling_params(model: Optional[str]) -> dict[str, Any]:
    """Return model-specific sampling options; caller supplies token budget and timeout."""
    name = (model or "").strip().lower()
    if name == "qwen/qwen3.8-27b" or name.startswith("qwen/"):
        return {"temperature": 0.6, "top_p": 0.95, "reasoning_effort": "default"}
    return {"reasoning_effort": _GROQ_REASONING_EFFORT}


# Keep reasoning effort low so Foundry leaves completion tokens for the JSON output.
_FOUNDRY_REASONING_EFFORT = "low"


def _foundry_sampling_params(model: Optional[str]) -> dict[str, Any]:
    """Decoding params for one Foundry deployment (see the constant above)."""
    name = (model or "").strip().lower()
    if name.startswith("gpt-5"):
        return {"reasoning_effort": _FOUNDRY_REASONING_EFFORT}
    if name.startswith(("o1", "o3", "o4")) and (
        len(name) == 2 or not name[2].isalnum()
    ):
        return {"reasoning_effort": _FOUNDRY_REASONING_EFFORT}
    return {}

# Strict-mode attempts per request. Content mismatches (the model emitting
# JSON that fails the enforced schema) are stochastic: the same prompt
# usually succeeds on retry, so they are retried rather than failed. Only
# the fallback below bounds the total; validation afterwards bounds truth.
_STRICT_CONTENT_RETRIES = 2


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
    provider: str = "groq"
    # The key travels only into the SDK client; it is never logged or stored.
    api_key: Optional[str] = None


def config_from_settings(settings: Any) -> LLMConfig:
    """Read provider configuration from the application settings object."""
    return LLMConfig(
        base_url=getattr(settings, "LLM_BASE_URL", None),
        api_key=getattr(settings, "LLM_API_KEY", None),
        model=getattr(settings, "LLM_MODEL", None),
        provider=(getattr(settings, "LLM_PROVIDER", None) or "groq"),
    )


def select_provider(config: LLMConfig) -> str:
    """Which SDK talks to the configured surface: ``groq`` or ``foundry``.

    The value is trimmed and lowercased before validation. ``openai`` is a
    legacy spelling of the foundry path (the OpenAI SDK), and ``auto`` keeps
    even older deployments working: it picks Groq when ``LLM_BASE_URL``
    names Groq, else the OpenAI-compatible client. An unset provider is
    Groq -- the provider the application shipped with.
    """
    requested = (config.provider or "groq").strip().lower()
    if requested in ("groq", "foundry", "openai"):
        return requested
    if requested != "auto":
        raise LLMError(
            f"Unknown LLM_PROVIDER {config.provider!r}: expected 'groq' or "
            "'foundry'.",
            code="missing_config",
        )
    if config.base_url and "groq" in config.base_url.lower():
        return "groq"
    return "openai"


def _redacted(detail: str) -> str:
    """Strip anything shaped like a secret from an error sentence."""
    lowered = detail.lower()
    for marker in ("bearer ", "api-key", "apikey"):
        if marker in lowered:
            return "The provider refused the request (credential or request error)."
    return detail[:500]


def _is_content_mismatch(exc: Exception) -> bool:
    """Whether a strict-mode refusal blames the generated JSON, not the schema.

    Schema rejections (unknown response_format, bad $ref) mean the
    deployment cannot do strict mode at all and trigger the JSON-object
    fallback. Content mismatches mean the model fumbled one document --
    retrying the same strict request usually succeeds, and the fallback
    below still applies when retries run out.

    Matched broadly on purpose: SDK versions render the same Groq 400
    differently (some include the ``json_validate_failed`` code, others only
    the ``Failed to validate JSON ... failed_generation`` message). Any
    variant must retry/fall back, never surface as a caller-facing 400.
    """
    message = str(exc).lower()
    return (
        "json_validate_failed" in message
        or "does not match the expected schema" in message
        or "failed to validate json" in message
        or "failed_generation" in message
    )


def _is_schema_unsupported(exc: Exception) -> bool:
    """Whether a strict-mode refusal blames the schema mechanism itself."""
    message = _redacted(str(exc))
    return "response_format" in message or "json_schema" in message


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
    # The provider is resolved before the credential checks so an
    # unsupported LLM_PROVIDER is reported as such, never masked by an
    # unrelated missing-config error.
    provider = select_provider(config)
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
    logger.info(
        "Plot Twist generation started",
        extra={"operation_id": operation_id, "operation": operation, "provider": provider},
    )
    waits = (0.0,) + _RATE_LIMIT_WAITS
    for index, wait in enumerate(waits):
        if wait:
            logger.info(
                "Plot Twist generation waiting out the token budget",
                extra={"operation_id": operation_id, "wait_s": wait},
            )
            time.sleep(wait)
        try:
            if provider == "groq":
                payload, used_fallback = _generate_via_groq(
                    config, system, user, json_schema, schema_name, operation_id,
                    timeout_s, max_tokens,
                )
            else:
                payload, used_fallback = _generate_via_foundry(
                    config, system, user, json_schema, schema_name, operation_id,
                    timeout_s, max_tokens,
                )
            break
        except LLMError as exc:
            if exc.code != "rate_limited" or index >= len(waits) - 1:
                raise
            # The Retry-After hint (when the provider sends one) lives on
            # the SDK error chained as the cause, not on this LLMError.
            hint = _retry_after_seconds(getattr(exc, "__cause__", None) or exc)
            if hint is not None:
                time.sleep(hint)
    logger.info(
        "Plot Twist generation finished",
        extra={"operation_id": operation_id, "fallback": used_fallback, "provider": provider},
    )
    meta = {
        "operation_id": operation_id,
        "model": config.model,
        "provider": provider,
        "strict_schema": not used_fallback,
    }
    return payload, meta


def _strict_format(schema_name: str, json_schema: dict) -> dict[str, Any]:
    return {
        "type": "json_schema",
        "json_schema": {"name": schema_name, "strict": True, "schema": json_schema},
    }


def _fallback_messages(system: str, user: str, json_schema: dict) -> list[dict[str, Any]]:
    """Messages for the JSON-object fallback.

    No provider-enforced schema applies here, so the model must still see the
    contract or it invents its own field names (a live Plot Twist run returned
    ``state_vars`` entries shaped ``id``/``var_type``/``bounds`` instead of the
    real ``name``/``kind``/``min_value``/``max_value``). The schema travels in
    the prompt: exact names and nesting, every ``required`` property present,
    nothing extra.
    """
    return [
        {"role": "system", "content": system},
        {
            "role": "user",
            "content": (
                user
                + "\n\nReturn your answer as a JSON object matching this JSON "
                "Schema exactly (same field names and nesting; include every "
                "property named in `required`; add nothing it does not "
                "define):\n"
                + json.dumps(json_schema, separators=(",", ":"))
            ),
        },
    ]


def _parse_json_content(content: Any) -> dict:
    try:
        payload = json.loads(content or "")
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
    return payload


def _parse_completion(choice: Any) -> dict:
    """Parse one chat choice; name truncation instead of blaming JSON.

    Reasoning models count hidden reasoning against the output budget, so an
    exhausted budget comes back as empty or partial content with
    ``finish_reason='length'`` -- a budget problem, not a syntax one.
    """
    content = getattr(getattr(choice, "message", None), "content", None)
    try:
        return _parse_json_content(content)
    except LLMError:
        if getattr(choice, "finish_reason", None) == "length":
            raise LLMError(
                "The model's answer was cut off by the output-token budget "
                "before it finished (hidden reasoning counts against it). "
                "Retry the operation.",
                code="truncated",
            ) from None
        raise


def _generate_via_groq(
    config: LLMConfig,
    system: str,
    user: str,
    json_schema: dict,
    schema_name: str,
    operation_id: str,
    timeout_s: int,
    max_tokens: int,
) -> tuple[dict, bool]:
    """Strict Structured Outputs through the Groq SDK, JSON fallback on refusal."""
    try:
        from groq import Groq
    except ImportError as exc:
        raise LLMError(
            "Story generation is unavailable: the Groq SDK is not installed.",
            code="unsupported_schema",
        ) from exc

    # The Groq SDK already routes under /openai/v1; passing the configured
    # OpenAI-style base URL verbatim would double the path, so a trailing
    # /openai/v1 is trimmed (a custom proxy prefix is otherwise kept whole).
    base_url = (config.base_url or "").rstrip("/")
    if base_url.lower().endswith("/openai/v1"):
        base_url = base_url[: -len("/openai/v1")]
    client = Groq(api_key=config.api_key, base_url=base_url or None)
    sampling = _groq_sampling_params(config.model)
    base_messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    for attempt in range(1 + _STRICT_CONTENT_RETRIES):
        try:
            response = client.chat.completions.create(
                model=config.model,
                messages=base_messages,  # type: ignore[arg-type]
                response_format=_strict_format(schema_name, json_schema),  # type: ignore[arg-type]
                max_completion_tokens=max_tokens,
                timeout=timeout_s,
                **sampling,  # type: ignore[arg-type]
            )
            return _parse_completion(response.choices[0]), False
        except LLMError:
            raise
        except Exception as strict_exc:
            if _is_content_mismatch(strict_exc):
                logger.info(
                    "Plot Twist strict output failed validation, retrying",
                    extra={"operation_id": operation_id, "attempt": attempt + 1},
                )
                continue
            if not _is_schema_unsupported(strict_exc):
                raise _classify_provider_error(strict_exc) from strict_exc
            break
    logger.info(
        "Plot Twist strict schema unavailable, using JSON-object fallback",
        extra={"operation_id": operation_id},
    )
    # Plain JSON-object mode. Groq requires the word "json" in the messages
    # for this mode, so it is appended rather than assumed; the schema itself
    # travels in the prompt because the provider no longer enforces it.
    fallback_messages = _fallback_messages(system, user, json_schema)
    try:
        response = client.chat.completions.create(
            model=config.model,
            messages=fallback_messages,  # type: ignore[arg-type]
            response_format={"type": "json_object"},  # type: ignore[arg-type]
            max_completion_tokens=max_tokens,
            timeout=timeout_s,
            **sampling,  # type: ignore[arg-type]
        )
    except Exception as exc:
        raise _classify_provider_error(exc) from exc
    return _parse_completion(response.choices[0]), True


def _foundry_base_url(base_url: Optional[str]) -> str:
    """The OpenAI SDK base URL for the configured Foundry endpoint.

    The SDK appends the route itself (``/chat/completions``), so an endpoint
    copied from the Foundry portal that already names one --
    ``.../openai/v1/responses`` or ``.../openai/v1/chat/completions`` -- is
    trimmed back to the OpenAI-compatible root; without that the request
    path would be doubled (a 404 at the provider). Anything else is kept
    verbatim so custom proxies and gateways are untouched.
    """
    url = (base_url or "").strip().rstrip("/")
    for suffix in ("/chat/completions", "/responses"):
        if url.lower().endswith(suffix):
            return url[: -len(suffix)].rstrip("/")
    return url


def _generate_via_foundry(
    config: LLMConfig,
    system: str,
    user: str,
    json_schema: dict,
    schema_name: str,
    operation_id: str,
    timeout_s: int,
    max_tokens: int,
) -> tuple[dict, bool]:
    """Strict Structured Outputs through Microsoft Foundry (OpenAI SDK)."""
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise LLMError(
            "Story generation is unavailable: the OpenAI SDK is not installed.",
            code="unsupported_schema",
        ) from exc

    base_url = _foundry_base_url(config.base_url)
    if not base_url.lower().startswith(("https://", "http://")):
        raise LLMError(
            "LLM_BASE_URL must be an http(s) URL for the foundry provider, "
            "e.g. https://<resource>.services.ai.azure.com/openai/v1.",
            code="missing_config",
        )
    # The key travels only into the SDK client; it is never logged or stored.
    client = OpenAI(base_url=base_url, api_key=config.api_key)
    sampling = _foundry_sampling_params(config.model)
    base_messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    for attempt in range(1 + _STRICT_CONTENT_RETRIES):
        try:
            response = client.chat.completions.create(
                model=config.model,
                messages=base_messages,  # type: ignore[arg-type]
                response_format=_strict_format(schema_name, json_schema),  # type: ignore[arg-type]
                timeout=timeout_s,
                # Reasoning models (gpt-5-mini and the o-series) reject
                # max_tokens and require max_completion_tokens; the latter is
                # accepted across the OpenAI-compatible surfaces.
                max_completion_tokens=max_tokens,
                **sampling,  # type: ignore[arg-type]
            )
            return _parse_completion(response.choices[0]), False
        except LLMError:
            raise
        except Exception as strict_exc:
            if _is_content_mismatch(strict_exc):
                logger.info(
                    "Plot Twist strict output failed validation, retrying",
                    extra={"operation_id": operation_id, "attempt": attempt + 1},
                )
                continue
            if not _is_schema_unsupported(strict_exc):
                raise _classify_provider_error(strict_exc) from strict_exc
            break
    logger.info(
        "Plot Twist strict schema unavailable, using JSON-object fallback",
        extra={"operation_id": operation_id},
    )
    # The provider no longer enforces the schema, so the prompt carries it.
    fallback_messages = _fallback_messages(system, user, json_schema)
    try:
        response = client.chat.completions.create(
            model=config.model,
            messages=fallback_messages,  # type: ignore[arg-type]
            response_format={"type": "json_object"},  # type: ignore[arg-type]
            timeout=timeout_s,
            max_completion_tokens=max_tokens,
            **sampling,  # type: ignore[arg-type]
        )
    except Exception as exc:
        raise _classify_provider_error(exc) from exc
    return _parse_completion(response.choices[0]), True


def _retry_after_seconds(exc: Exception) -> Optional[float]:
    """Retry-After hint from a provider error, if it carries one."""
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if not headers:
        return None
    try:
        get = headers.get
    except AttributeError:
        return None
    for key in ("retry-after-ms", "retry-after"):
        value = get(key)
        if value is None:
            continue
        try:
            seconds = float(value)
        except (TypeError, ValueError):
            continue
        if key == "retry-after-ms":
            seconds /= 1000.0
        return max(1.0, min(seconds, 120.0))
    return None


def _is_rate_limited(exc: Exception) -> bool:
    """Whether the failure is a rate/token-budget refusal worth waiting out.

    Matches status and body spellings across providers: 429s, Groq 413
    token-per-minute refusals (``rate_limit_exceeded``), and TPM mentions.
    """
    name = type(exc).__name__.lower()
    message = str(exc).lower()
    if "ratelimit" in name or "429" in message:
        return True
    compact = message.replace("-", "_").replace(" ", "_")
    if "rate_limit" in compact or "tokens_per_minute" in compact or "`tpm`" in compact:
        return True
    return False


# Rate-limit waits per attempt before giving up to the caller: the free
# TPM window refills continuously, so short spaced waits usually succeed
# where an instant retry would burn the budget again.
_RATE_LIMIT_WAITS = (20.0, 45.0, 60.0)


def _classify_provider_error(exc: Exception) -> LLMError:
    """Map SDK / transport failures to admin-safe codes without secrets."""
    name = type(exc).__name__.lower()
    message = _redacted(str(exc))
    if "timeout" in name or "timed out" in message.lower():
        return LLMError(
            "The story-generation request timed out. Retry the operation.",
            code="timeout",
        )
    if _is_rate_limited(exc):
        return LLMError(
            "The AI provider's token budget is exhausted for the minute. "
            "Wait a minute and retry the operation, or move to a paid tier.",
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

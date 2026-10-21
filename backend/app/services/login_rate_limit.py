# app/services/login_rate_limit.py

import math
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple

from sqlalchemy import case, delete
from sqlalchemy.dialects.postgresql import insert as pg_insert

from ..accounts import normalize_account_type
from ..config import cookie_secure, settings
from ..db_models import LoginRateLimitRecord
from ..postgres import Database

# Scope names are reported to the operator and asserted by tests, so they are not
# scattered string literals.
SCOPE_CLIENT = "client"
SCOPE_IP = "ip"
SCOPE_ACCOUNT = "account"
SCOPE_ACCOUNT_LONG = "account_long"

# Only the exact minted shape is accepted; anything else is treated as no cookie, so a
# caller cannot choose its rate-limit key or overflow the key column.
_CLIENT_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{16,64}")


@dataclass(frozen=True)
class RateLimitVerdict:
    """Whether a request may proceed, and which limit refused it if not.

    ``retry_after`` is seconds until that window resets, ready for ``Retry-After``.
    """

    allowed: bool
    scope: Optional[str] = None
    retry_after: int = 0


def _utcnow() -> datetime:
    """The clock every limit reads. Module-level so tests can replace this one function."""
    return datetime.now(timezone.utc)


def resolve_client_id(request: Any) -> Tuple[str, bool]:
    """The caller's server-generated client id, and whether it is freshly minted.

    The opaque cookie value is never derived from the caller, so it cannot be rewritten
    per request; ``is_new`` tells the caller to persist it in the response cookie.
    """
    cookies = getattr(request, "cookies", None) or {}
    candidate = cookies.get(settings.LOGIN_CLIENT_COOKIE_NAME)
    if isinstance(candidate, str) and _CLIENT_ID_PATTERN.fullmatch(candidate):
        return candidate, False
    return secrets.token_urlsafe(16), True


def client_cookie_options(request: Any) -> Dict[str, Any]:
    """Keyword arguments for the client-id cookie.

    ``HttpOnly``, ``SameSite=Lax``, and ``Secure`` following the client-facing scheme or
    ``COOKIE_SECURE``.
    """
    return {
        "key": settings.LOGIN_CLIENT_COOKIE_NAME,
        "max_age": settings.LOGIN_CLIENT_COOKIE_MAX_AGE,
        "httponly": True,
        "samesite": "lax",
        "secure": cookie_secure(request.url.scheme),
        "path": "/",
    }


def cleanup_expired_login_rate_limits(db: Database, now: Optional[datetime] = None) -> int:
    """Drop elapsed windows and return how many rows went.

    Windows expire by construction and the sweep runs inside every evaluation, so the
    table only holds windows still being hit.
    """
    now = now if now is not None else _utcnow()
    with db.sessions.begin() as session:
        result = session.execute(
            delete(LoginRateLimitRecord).where(LoginRateLimitRecord.expires_at <= now)
        )
        return int(result.rowcount or 0)


def _bump(session: Any, key: str, now: datetime, window_seconds: int) -> Tuple[int, datetime]:
    """Count one request against ``key`` and return the window it landed in.

    A single INSERT ... ON CONFLICT DO UPDATE ... RETURNING makes read, reset and increment
    atomic; racers queue on the row lock. The CASE arms read the pre-update row, so an open
    window keeps its state and an elapsed one is replaced wholesale.
    """
    duration = timedelta(seconds=window_seconds)
    cutoff = now - duration
    window_open = LoginRateLimitRecord.window_started_at > cutoff

    statement = (
        pg_insert(LoginRateLimitRecord)
        .values(key=key, window_started_at=now, hits=1, expires_at=now + duration)
        .on_conflict_do_update(
            index_elements=[LoginRateLimitRecord.key],
            set_={
                "hits": case(
                    (window_open, LoginRateLimitRecord.hits + 1),
                    else_=1,
                ),
                "window_started_at": case(
                    (window_open, LoginRateLimitRecord.window_started_at),
                    else_=now,
                ),
                "expires_at": case(
                    (window_open, LoginRateLimitRecord.expires_at),
                    else_=now + duration,
                ),
            },
        )
        .returning(LoginRateLimitRecord.hits, LoginRateLimitRecord.window_started_at)
    )
    row = session.execute(statement).one()
    return int(row[0]), row[1]


def evaluate_login_rate_limits(
    db: Database,
    *,
    account_type: str,
    regno: str,
    client_id: str,
    ip: str,
) -> RateLimitVerdict:
    """Apply every login-request limit and say whether the request may proceed.

    All counters increment before any verdict, so a refused request still drains the other
    budgets. The account key is the submitted regno, not a resolved id, so the cooldown is
    not an existence oracle. Order only decides which exhausted scope is reported.
    """
    account_key = f"{normalize_account_type(account_type)}:{regno}"
    budgets: Tuple[Tuple[str, str, int, int], ...] = (
        (SCOPE_CLIENT, f"login:client:{client_id}", settings.LOGIN_CLIENT_WINDOW_SECONDS, settings.LOGIN_CLIENT_LIMIT),
        (SCOPE_IP, f"login:ip:{ip}", settings.LOGIN_IP_WINDOW_SECONDS, settings.LOGIN_IP_LIMIT),
        (SCOPE_ACCOUNT, f"login:account:{account_key}", settings.LOGIN_ACCOUNT_COOLDOWN_SECONDS, 1),
        (SCOPE_ACCOUNT_LONG, f"login:account-long:{account_key}", settings.LOGIN_ACCOUNT_LONG_WINDOW_SECONDS, settings.LOGIN_ACCOUNT_LONG_WINDOW_LIMIT),
    )

    now = _utcnow()
    with db.sessions.begin() as session:
        session.execute(
            delete(LoginRateLimitRecord).where(LoginRateLimitRecord.expires_at <= now)
        )
        counts = [
            (scope, window, limit, *_bump(session, key, now, window))
            for scope, key, window, limit in budgets
        ]

    for scope, window, limit, hits, window_started_at in counts:
        if hits > limit:
            remaining = (window_started_at + timedelta(seconds=window) - now).total_seconds()
            return RateLimitVerdict(
                allowed=False,
                scope=scope,
                retry_after=max(1, math.ceil(remaining)),
            )
    return RateLimitVerdict(allowed=True)

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
from ..config import settings
from ..db_models import LoginRateLimitRecord
from ..postgres import Database

# The scope names are what the caller reports to the operator and what the tests assert
# on, so they live here rather than being string literals at each call site.
SCOPE_CLIENT = "client"
SCOPE_IP = "ip"
SCOPE_ACCOUNT = "account"
SCOPE_ACCOUNT_LONG = "account_long"

# The client id is minted by this module and only ever accepted back in the shape it was
# minted in. Anything else -- an over-long cookie, punctuation a proxy appended, a value
# somebody typed into their cookie jar -- is treated as no cookie at all, so a caller
# cannot choose the key its requests are counted under or overflow the key column.
_CLIENT_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{16,64}")


@dataclass(frozen=True)
class RateLimitVerdict:
    """Whether a login request may proceed, and why not when it may not.

    ``scope`` names which of the limits refused the request; ``retry_after`` is the
    number of seconds until that limit's window resets, ready to be put straight into a
    ``Retry-After`` header.
    """

    allowed: bool
    scope: Optional[str] = None
    retry_after: int = 0


def _utcnow() -> datetime:
    """The clock every limit in this module reads.

    Module-level on purpose: tests move time forward by replacing this one function, so
    a five-minute window can be exercised without the suite waiting five minutes, and
    only this module's idea of "now" has to move with it.
    """
    return datetime.now(timezone.utc)


def resolve_client_id(request: Any) -> Tuple[str, bool]:
    """The caller's server-generated client id, and whether it is freshly minted.

    The id comes from an opaque cookie and is generated here when absent or malformed.
    Nothing about it is derived from the caller: not the user-agent, not any header, not
    the requested username. That is what makes it an independent rate-limit key -- the
    identity cannot be rewritten per request the way a header can, and it stays with one
    browser across requests while the usernames underneath it change.

    Returns ``(client_id, is_new)``. ``is_new`` tells the caller to persist the id in
    the response cookie; a request that already carried a valid id needs no cookie.
    """
    cookies = getattr(request, "cookies", None) or {}
    candidate = cookies.get(settings.LOGIN_CLIENT_COOKIE_NAME)
    if isinstance(candidate, str) and _CLIENT_ID_PATTERN.fullmatch(candidate):
        return candidate, False
    return secrets.token_urlsafe(16), True


def client_cookie_options(request: Any) -> Dict[str, Any]:
    """Keyword arguments for setting the client-id cookie on a response.

    ``HttpOnly`` keeps the id out of page scripts, ``SameSite=Lax`` keeps it away from
    cross-site requests while still sending it to the API from a sibling port of the
    same site, and ``Secure`` follows the scheme the request actually arrived on: the
    cookie must work on a local http deployment and be strict behind TLS, so the flag
    cannot be a constant chosen at start-up.
    """
    return {
        "key": settings.LOGIN_CLIENT_COOKIE_NAME,
        "max_age": settings.LOGIN_CLIENT_COOKIE_MAX_AGE,
        "httponly": True,
        "samesite": "lax",
        "secure": request.url.scheme == "https",
        "path": "/",
    }


def cleanup_expired_login_rate_limits(db: Database, now: Optional[datetime] = None) -> int:
    """Drop every window that has already elapsed. Returns how many rows went.

    Windows expire by construction (``expires_at`` is written with the window), so the
    sweep needs no bookkeeping beyond running often enough -- it runs inside every
    evaluation, which means the table only ever holds windows somebody is still hitting.
    """
    now = now if now is not None else _utcnow()
    with db.sessions.begin() as session:
        result = session.execute(
            delete(LoginRateLimitRecord).where(LoginRateLimitRecord.expires_at <= now)
        )
        return int(result.rowcount or 0)


def _bump(session: Any, key: str, now: datetime, window_seconds: int) -> Tuple[int, datetime]:
    """Count one request against ``key`` and return the window it landed in.

    One INSERT ... ON CONFLICT DO UPDATE with RETURNING, so the read of the current
    count, the decision to reset an elapsed window and the increment are a single
    statement. Concurrent requests for the same key queue on the row lock and each see
    the count left by the previous one -- the pattern ``confession_service`` uses with a
    conditional UPDATE, expressed here as an upsert because the row may not exist yet.

    The three CASE arms all read the pre-update row, so they agree with each other: a
    window that is still open keeps its start, its count and its expiry, and a window
    that has run out is replaced wholesale rather than incremented into a second window.
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
    """Apply every login-request limit to one request and say whether it may proceed.

    All counters are incremented before any verdict is formed, so a request refused by
    one limit still counts against the others: a caller that is over the client limit
    and keeps hammering the endpoint is also draining its IP budget, and a refused
    request cannot be used to *avoid* being counted.

    The account key is built from the submitted registration number, never from a
    resolved account id. The lookup has not happened yet at this point, and keying on
    resolution would make the cooldown fire for existing regnos and stay silent for
    invented ones -- a per-username timing and status oracle. Keyed on what was typed,
    the cooldown answers identically for both.

    Evaluation order (client, ip, account, account long window) only decides which
    scope is *reported* when several are exhausted; every limit is consulted either way.
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

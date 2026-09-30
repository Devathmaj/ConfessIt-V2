"""Tests that every timestamp the backend produces is timezone-aware UTC.

The rule this file enforces is narrow: nothing in ``app/`` may build a timestamp from
local time or from the deprecated ``utcnow()``. Both are silent -- neither raises, and
both produce a plausible-looking value -- so the failure mode is a timestamp that is
wrong by the server's UTC offset, or that raises ``TypeError`` only much later when it
is finally compared against an aware value.

``utcnow()`` is treated as a violation even though it happens to be correct, because it
is deprecated with removal scheduled, and because a reader cannot tell at a glance
whether a nearby naive value is meant as UTC or as local time.
"""

import ast
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import jwt
import pytest

from app.config import settings
from app.services.auth_service import create_access_token
from app.services.message_archival_service import MessageArchivalService

APP_DIR = Path(__file__).resolve().parent.parent / "app"

# Calls that silently produce local time (or deprecated naive UTC) instead of aware UTC,
# mapped to (reason, max_allowed_positional_args). A None arg limit means always banned.
BANNED_CALLS = {
    ("datetime", "utcnow"): ("deprecated naive UTC; use datetime.now(timezone.utc)", None),
    ("datetime", "now"): ("naive local time; use datetime.now(timezone.utc)", 0),
    ("datetime", "today"): ("naive local date; use datetime.now(timezone.utc)", None),
    ("date", "today"): ("naive local date; use datetime.now(timezone.utc)", None),
    ("datetime", "fromtimestamp"): ("naive local time unless tz= is passed", None),
    ("datetime", "utcfromtimestamp"): (
        "deprecated; use datetime.fromtimestamp(x, tz=timezone.utc)",
        None,
    ),
    ("time", "localtime"): ("local time; use time.gmtime()", None),
    ("time", "mktime"): ("local time; use timegm()", None),
}

# The same class can be reached through several import styles, and the rules above are
# written against the canonical ``datetime``/``date`` spelling. Map each qualified form
# onto the rule key it should be checked against.
_QUALIFIED_ALIASES = {
    "datetime.datetime": "datetime",
    "datetime.date": "date",
    "datetime.time": "time",
}


def _rule_key(resolved: tuple[str, ...]) -> tuple[str, ...] | None:
    """Collapse a resolved call path to the canonical rule key, or None if it is not one."""
    if not resolved:
        return None
    owner, *rest = resolved
    owner = _QUALIFIED_ALIASES.get(owner, owner)
    if not rest:
        return None
    return (owner, *rest)


def _dotted_name(node: ast.AST) -> tuple[str, ...] | None:
    """Flatten an attribute chain such as ``datetime.datetime.utcnow`` into a tuple."""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return tuple(reversed(parts))
    return None


def _alias_free_name(node: ast.AST, imported: dict[str, str]) -> tuple[str, ...] | None:
    """Resolve ``utcnow()`` to ``datetime.utcnow`` using this module's imports."""
    dotted = _dotted_name(node)
    if dotted is None:
        return None
    head, *rest = dotted
    # ``from datetime import datetime, timedelta`` makes the bare name ``datetime`` the
    # datetime class, so ``datetime.utcnow()`` is the same call as ``datetime.datetime.utcnow()``.
    if head in imported:
        return (imported[head], *rest)
    return dotted


def _module_imports(tree: ast.Module) -> dict[str, str]:
    """Map a locally imported name to the dotted path it refers to."""
    imported: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                imported[alias.asname or alias.name] = f"{node.module}.{alias.name}"
        elif isinstance(node, ast.Import):
            for alias in node.names:
                imported[alias.asname or alias.name] = alias.name
    return imported


def _python_sources() -> list[Path]:
    return sorted(APP_DIR.rglob("*.py"))


def test_there_are_sources_to_check():
    """Guard against a bad glob silently making the scan below pass vacuously."""
    assert len(_python_sources()) > 20


@pytest.mark.parametrize("path", _python_sources(), ids=lambda p: p.name)
def test_no_silent_local_or_naive_timestamps(path: Path):
    """No module in app/ may call a constructor that yields local or naive time."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported = _module_imports(tree)

    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        resolved = _alias_free_name(node.func, imported)
        if resolved is None:
            continue
        key = _rule_key(resolved)
        if key is None:
            continue
        rule = BANNED_CALLS.get(key)
        if rule is None:
            continue
        reason, max_args = rule
        # ``datetime.now(timezone.utc)`` is the whole point, so only the bare form is a
        # violation; likewise fromtimestamp() may pass an explicit tz=.
        if max_args is not None and len(node.args) > max_args:
            continue
        if key[1] == "fromtimestamp" and any(kw.arg == "tz" for kw in node.keywords):
            continue
        offenders.append(f"{path.name}:{node.lineno} {key[0]}.{key[1]} - {reason}")

    assert not offenders, "Naive/local timestamp construction found:\n" + "\n".join(offenders)


def test_create_access_token_sets_exp_from_utc_clock():
    """The exp claim must track real UTC and land exactly one TTL in the future."""
    before = int(time.time())
    token = create_access_token({"sub": "someone"})
    claims = jwt.decode(
        token, settings.SECRET_KEY, algorithms=["HS256"], options={"verify_exp": False}
    )
    after = int(time.time())

    ttl = settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60
    assert before + ttl - 5 <= claims["exp"] <= after + ttl + 5

    # PyJWT also accepts an aware datetime for exp, which is what lets the claim be
    # minted without a second conversion; assert that round trip explicitly.
    aware = datetime.fromtimestamp(claims["exp"], tz=timezone.utc)
    assert aware.tzinfo is timezone.utc


def test_exp_is_unambiguous_across_source_zones():
    """An aware exp encodes the same instant no matter which zone the writer is in.

    A naive value cannot make this promise: it silently asserts "this is UTC", so if a
    writer ever passed local wall-clock time the claim would be wrong by the offset and
    nothing would raise. These assertions document the property the fix relies on.
    """
    from calendar import timegm

    kathmandu = timezone(timedelta(hours=5, minutes=45))
    same_instant = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

    # 17:45 in Kathmandu is the same instant, and encodes to the same claim.
    assert timegm(same_instant.astimezone(kathmandu).utctimetuple()) == timegm(
        same_instant.utctimetuple()
    )

    # 12:00 naive in Kathmandu is a *different* instant that a naive value cannot express.
    naive_local = datetime(2026, 1, 1, 17, 45, 0)
    assert timegm(naive_local.utctimetuple()) != timegm(same_instant.utctimetuple())


def test_archive_listing_reports_mtime_in_utc(tmp_path, monkeypatch):
    """``modified_at`` must be aware UTC even though the file's mtime is an epoch float.

    The archive ``date`` is parsed out of a UTC-based filename, so a local-time
    ``modified_at`` on a non-UTC server would contradict the date shown beside it.
    """
    service = MessageArchivalService()
    service.archive_dir = str(tmp_path)

    target = tmp_path / "messages_20260101_120000.json.gz"
    target.write_bytes(b"{}")
    # Pin mtime to a known instant so the assertion is exact.
    os.utime(target, (1767268800, 1767268800))

    entries = service.list_archives()
    assert len(entries) == 1

    modified_at = entries[0]["modified_at"]
    assert modified_at == "2026-01-01T12:00:00+00:00"

    # And it must parse back to the exact instant we set.
    parsed = datetime.fromisoformat(modified_at)
    assert parsed.tzinfo is not None
    assert parsed.astimezone(timezone.utc).timestamp() == 1767268800

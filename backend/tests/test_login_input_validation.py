"""The sign-in endpoint accepts only strict registration numbers.

Login is the door to the account surface, so the value it accepts is whitelisted before
it is trusted anywhere: the 400 here is answered before the request counts against any
rate limit and before it can reach a lookup, a rate-limit key or a log line. The
experiments below drive the real endpoint and assert the shape of the gate:

* Unicode letters and digits from any script are accepted, alongside the small
  punctuation a regno can legitimately carry (``.``, ``/``, ``-``, ``_``);
* surrounding whitespace is trimmed and the trimmed value is what is looked up;
* control characters, hostile punctuation, whitespace embedded in the value, values
  over ``USER_REGNO_MAX`` long and non-string values are all refused with a 400;
* a refused value spends no rate-limit budget, and the staff doors enforce the same gate.
"""

import contextlib
import threading

import pytest
from anyio.from_thread import start_blocking_portal
from fastapi.testclient import TestClient

from app.accounts import AccountType
from app.db_models import UserRecord
from app.main import app

LOGIN_ENDPOINT = f"/auth/login/{AccountType.USERS.value}/magic"


class _FromHost:
    """Presents every request as arriving from one fixed, storable address."""

    def __init__(self, host="127.0.0.1", port=51234):
        self.host = host
        self.port = port

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            scope = {**scope, "client": (self.host, self.port)}
        await app(scope, receive, send)


def _browser(host="127.0.0.1"):
    """One browser: a client with its own cookie jar, and therefore its own client id."""
    return _attach_portal(TestClient(_FromHost(host)))


_browser_stack: contextlib.ExitStack | None = None
_browser_lock = threading.Lock()


@pytest.fixture(autouse=True)
def _reuse_browser_portal():
    """Serve every ``_browser()`` request from one portal per test.

    A bare ``TestClient`` starts a fresh BlockingPortal (an OS thread plus an
    event loop) for every request and tears it down afterwards, and this file
    builds a fresh client per assertion. That churn is what turned the suite from
    minutes into an hour: attaching one portal per test keeps requests on a
    single warm loop, and the stack stops it when the test ends. Lifespan is
    deliberately not run, as before.
    """
    global _browser_stack
    with contextlib.ExitStack() as stack:
        _browser_stack = stack
        try:
            yield
        finally:
            _browser_stack = None


def _attach_portal(client):
    """Attach the test-scoped portal to a fresh client, if a test is running."""
    stack = _browser_stack
    if stack is None:
        return client
    with _browser_lock:
        client.portal = stack.enter_context(start_blocking_portal())
        stack.callback(setattr, client, "portal", None)
        stack.callback(client.close)
    return client


def _seed_user(db, regno):
    with db.sessions.begin() as session:
        session.add(UserRecord(
            regno=regno,
            name=regno,
            email=f"{regno}@example.com",
            year=2,
            department="Test_Class",
            gender="other",
        ))


# ----------------------------------------------------------------------
# What passes the gate
# ----------------------------------------------------------------------


def test_a_plain_regno_that_exists_signs_in(db):
    _seed_user(db, "MA9")
    assert _browser().post(LOGIN_ENDPOINT, json={"regno": "MA9"}).status_code == 200


def test_surrounding_whitespace_is_trimmed_before_the_lookup(db):
    _seed_user(db, "spaced-user")
    response = _browser().post(LOGIN_ENDPOINT, json={"regno": "  spaced-user  "})
    assert response.status_code == 200


def test_unicode_letters_and_digits_from_any_script_are_accepted(db):
    _seed_user(db, "समझ")
    assert _browser().post(LOGIN_ENDPOINT, json={"regno": "समझ"}).status_code == 200


def test_the_safe_punctuation_a_regno_can_carry_is_accepted(db):
    for regno in ("22/CS0455", "MA-1", "A.B_C"):
        _seed_user(db, regno)
    for regno in ("22/CS0455", "MA-1", "A.B_C"):
        assert _browser().post(LOGIN_ENDPOINT, json={"regno": regno}).status_code == 200, regno


# ----------------------------------------------------------------------
# What the gate refuses
# ----------------------------------------------------------------------


def test_empty_and_whitespace_only_values_are_rejected(db):
    for value in ("", "   ", "\t", "\n"):
        assert _browser().post(LOGIN_ENDPOINT, json={"regno": value}).status_code == 400, repr(value)


def test_control_characters_are_rejected(db):
    for value in ("MA\n9", "MA\r\n9", "MA\t9", "MA\x001"):
        assert _browser().post(LOGIN_ENDPOINT, json={"regno": value}).status_code == 400, repr(value)


def test_embedded_whitespace_is_rejected(db):
    assert _browser().post(LOGIN_ENDPOINT, json={"regno": "MA 9"}).status_code == 400


def test_hostile_punctuation_is_rejected(db):
    for value in (
        "<script>", 'Ma"9', "MA&9", "MA=9", "MA;9", "MA\\9",
        "MA`9", "MA|9", "MA@x", "MA'9", "MA(9)", "MA[9]", "MA{9}",
    ):
        assert _browser().post(LOGIN_ENDPOINT, json={"regno": value}).status_code == 400, repr(value)


def test_an_overlong_regno_is_rejected(db):
    assert _browser().post(LOGIN_ENDPOINT, json={"regno": "X" * 101}).status_code == 400


def test_a_non_string_regno_is_rejected(db):
    for value in (["MA9"], {"regno": "MA9"}, 42, True):
        assert _browser().post(LOGIN_ENDPOINT, json={"regno": value}).status_code == 400, repr(value)


def test_a_malformed_json_body_is_refused_cleanly(db):
    response = _browser().post(
        LOGIN_ENDPOINT, content="{oops", headers={"content-type": "application/json"}
    )
    assert response.status_code == 400


def test_a_body_that_is_not_a_json_object_is_refused(db):
    for body in ("[1, 2, 3]", "42", "null", '"MA9"'):
        response = _browser().post(
            LOGIN_ENDPOINT, content=body, headers={"content-type": "application/json"}
        )
        assert response.status_code == 400, body


def test_a_body_without_a_regno_field_is_refused(db):
    response = _browser().post(LOGIN_ENDPOINT, json={"email": "unused@example.com"})
    assert response.status_code == 400


# ----------------------------------------------------------------------
# The gate's place in the request
# ----------------------------------------------------------------------


def test_rejected_input_is_not_counted_against_the_client_limit(db):
    """The 400 comes before the rate counters, so garbage cannot drain a budget.

    Six refusals would blow the five-per-window client budget if they were counted; the
    valid request that follows still succeeds, which proves they were not.
    """
    browser = _browser()
    for _ in range(6):
        assert browser.post(LOGIN_ENDPOINT, json={"regno": "<bad>"}).status_code == 400
    _seed_user(db, "fresh-user")
    assert browser.post(LOGIN_ENDPOINT, json={"regno": "fresh-user"}).status_code == 200


def test_the_gate_applies_to_the_staff_doors_too(db):
    for account_type in (AccountType.ADMIN.value, AccountType.MODERATOR.value):
        response = _browser().post(
            f"/auth/login/{account_type}/magic", json={"regno": "admin\nadmin"}
        )
        assert response.status_code == 400, account_type


# ----------------------------------------------------------------------
# Unknown registration numbers are refused plainly -- and still timed out
# ----------------------------------------------------------------------


def test_an_unknown_registration_number_is_plainly_refused(db):
    response = _browser().post(LOGIN_ENDPOINT, json={"regno": "never-registered"})
    assert response.status_code == 404


def test_hammering_with_an_unknown_registration_number_hits_the_cooldown(db):
    """Unknown is a result of the lookup, not an excuse to skip the budget.

    The rate limiter runs before the account lookup, so a registration number that does
    not exist pays the same per-account cooldown as one that does: a second request for
    the same unknown number in the same window is refused with a 429.
    """
    browser = _browser()

    assert browser.post(LOGIN_ENDPOINT, json={"regno": "never-registered"}).status_code == 404
    cooled = browser.post(LOGIN_ENDPOINT, json={"regno": "never-registered"})

    assert cooled.status_code == 429
    assert int(cooled.headers["Retry-After"]) > 0


def test_walking_sequential_unknown_registration_numbers_hits_the_browser_budget(db):
    """Different unknown numbers still share the per-browser budget.

    Sequential requests for distinct registration numbers each respect the per-account
    cooldown, so only the browser limit can stop the walk -- exactly as it does when the
    numbers are real.
    """
    browser = _browser()

    for index in range(5):
        assert browser.post(LOGIN_ENDPOINT, json={"regno": f"ghost-{index}"}).status_code == 404
    throttled = browser.post(LOGIN_ENDPOINT, json={"regno": "ghost-5"})

    assert throttled.status_code == 429
    assert "browser" in throttled.json()["detail"]
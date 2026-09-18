"""Slots decide who may match, and the empty table is the one that decides everybody may.

``matchmaking_slots`` was the admin screen's entire subject for a while: rows could be created,
listed and deleted, and nothing consulted them when a pairing was drawn. Every test below exists
because that is a mistake no failure will ever report -- an operator could spend an afternoon pairing
years against each other, change nothing about who was matched with whom, and see a fully green
suite throughout.

The rule being pinned, in the terms the admin screen uses:

* **No rows at all** -- every cohort may match with every other. A deployment nobody has configured
  must not come up matching nobody, because the screen it would be fixed from opens on one box
  holding every cohort.
* **One row or more** -- the rows are the whole rule. A cohort no row names can match nobody, and is
  not a candidate for anybody else either. The first slot an operator saves is therefore the moment
  every cohort they did not mention stops matching, which is the sharp edge of the feature and worth
  having a test that says so out loud.
* **A slot box is a clique that includes self-pairs.** A box holding A, B and C permits A->A, A->B,
  A->C, B->B, B->C and C->C. The editor stores that box as three rows and the rule is rebuilt from
  them, so these tests write three rows and expect the whole clique -- the nine ordered pairs,
  self-pairs included, are enumerated rather than sampled.
* **Overlap is a union, not a chain.** A-B together with B-C does not permit A->C.

A draw spends its attempt whether or not it found anybody, and the tests assert on
``last_matchmaking_at`` alongside the outcome for that reason: an unconfigured cohort able to retry
for free would be a way to poll the endpoint for a configuration change.
"""

import itertools
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from app.db_models import ConversationRecord, MatchRecord, MatchmakingSlotRecord, UserRecord
from app.models import UserDetails
from app.services import matchmaking_service

# Three cohorts, distinct as whole pairs. "3 CSE" rather than "3 ECE" so a test that mixes up the
# year half cannot pass by accident on a shared department.
COHORT_A = (2, "CSE")
COHORT_B = (2, "ECE")
COHORT_C = (3, "CSE")

# A cohort no slot will name.
COHORT_UNLISTED = (9, "Unlisted")


@pytest.fixture(autouse=True)
def no_rare_same_gender_roll(monkeypatch):
    """Switch off the rare same-gender roll for this file.

    It flips the candidate filter from "different gender" to "same gender", which would make the
    gender argument of every pair below load-bearing in a way that has nothing to do with slots.
    """
    monkeypatch.setattr(matchmaking_service, "SAME_GENDER_MATCH_CHANCE", 0)


def add_user(db, regno: str, year: int, department: str, gender: str) -> None:
    with db.sessions.begin() as session:
        session.add(
            UserRecord(
                regno=regno,
                name=f"Person {regno}",
                email=f"{regno}@example.com",
                year=year,
                department=department,
                gender=gender,
            )
        )


def add_slot(db, year_a: int, department_a: str, year_b: int, department_b: str) -> None:
    """Write a slot row directly.

    Not through ``create_matchmaking_slot_service``: these tests are about what the rows *mean*
    once written, and the 409 on a duplicate is a property of the admin endpoint rather than of the
    rule.
    """
    with db.sessions.begin() as session:
        session.add(
            MatchmakingSlotRecord(
                year_a=year_a,
                department_a=department_a,
                year_b=year_b,
                department_b=department_b,
            )
        )


def add_slot_box(db, cohorts) -> None:
    """Store one editor slot box as the clique of rows the editor would write for it."""
    for (year_a, department_a), (year_b, department_b) in itertools.combinations(cohorts, 2):
        add_slot(db, year_a, department_a, year_b, department_b)


def as_user(db, regno: str) -> UserDetails:
    """A fresh ``UserDetails`` for a regno, as a request would resolve it.

    Read from the row rather than hand-built, so the draw is given the cohort the database holds.
    """
    with db.sessions() as session:
        row = session.scalar(select(UserRecord).where(UserRecord.regno == regno))
    return UserDetails(
        id=str(row.id),
        regno=row.regno,
        name=row.name,
        email=row.email,
        year=row.year,
        department=row.department,
        gender=row.gender,
        matchmaking_enabled=row.matchmaking_enabled,
        notifications_enabled=row.notifications_enabled,
        account_type="users",
        last_matchmaking_at=row.last_matchmaking_at,
    )


def draw(db, regno: str):
    return matchmaking_service.find_match_service(as_user(db, regno), db)


def attempt_was_spent(db, regno: str) -> bool:
    with db.sessions() as session:
        row = session.scalar(select(UserRecord).where(UserRecord.regno == regno))
    return row.last_matchmaking_at is not None


def clear_cooldown(db, regno: str) -> None:
    """Age a spent attempt past the cooldown, so one test can draw twice."""
    with db.sessions.begin() as session:
        row = session.scalar(select(UserRecord).where(UserRecord.regno == regno))
        row.last_matchmaking_at = datetime.now(timezone.utc) - timedelta(hours=5)


def test_with_no_slot_configured_every_cohort_may_match(db):
    """The unconfigured state is all-to-all, and it has to be so from the very first draw.

    A fresh deployment has no rows and cannot be told apart from one whose operator deliberately
    cleared the screen, so the empty table has to mean the permissive thing. Otherwise installing
    this app would silently disable matchmaking for everybody, and the operator who went to fix it
    would be looking at a screen that looks correct.
    """
    add_user(db, "alice", *COHORT_A, "female")
    add_user(db, "bob", *COHORT_UNLISTED, "male")

    assert draw(db, "alice")["matched_with"]["regno"] == "bob"


def test_the_first_slot_stops_every_cohort_it_does_not_name(db):
    """The operator's scenario: save one two-cohort box and the rest stop matching.

    Asserted in the direction the user experiences it -- the unconfigured cohort drawing -- because
    the permissive default is what makes this surprising, and a test that only checked the
    configured side would still pass if the rule were simply "ignore the slots".
    """
    add_slot(db, *COHORT_A, *COHORT_B)
    add_user(db, "alice", *COHORT_UNLISTED, "female")
    add_user(db, "bob", *COHORT_A, "male")

    with pytest.raises(HTTPException) as excinfo:
        draw(db, "alice")

    assert excinfo.value.status_code == 404
    assert "not configured for matchmaking" in excinfo.value.detail


def test_the_unconfigured_message_names_the_cohort(db):
    """The chip to drag into a slot, not just the fact that one is missing.

    The user cannot configure anything -- only an operator can -- so a message with no cohort in it
    has to be relayed before it can be acted on, and relaying "you are not configured" is the whole
    conversation. Rendered through ``cohort_label`` rather than spelled out here so the message
    cannot drift from the palette the operator is reading the same labels off.
    """
    from app.services.matchmaking_slot_service import cohort_label

    add_slot(db, *COHORT_A, *COHORT_B)
    add_user(db, "alice", *COHORT_UNLISTED, "female")
    add_user(db, "bob", *COHORT_A, "male")

    with pytest.raises(HTTPException) as excinfo:
        draw(db, "alice")

    assert cohort_label(*COHORT_UNLISTED) in excinfo.value.detail


def test_a_configured_cohort_with_no_partner_gets_the_other_message(db):
    """The two refusals are fixed by two different people, so they must not read the same.

    "No potential matches" is fixed by nobody: it is a fact about the other participants right now,
    and waiting is the correct response. "Your cohort is not configured" is fixed by an operator and
    waiting cannot fix it. Collapsing them would tell a user to wait four hours for a draw that can
    never succeed, which is the specific outcome these two messages exist to prevent.
    """
    add_slot(db, *COHORT_A, *COHORT_B)
    add_user(db, "alice", *COHORT_A, "female")
    add_user(db, "carol", *COHORT_A, "female")  # same gender: never a candidate on the default roll

    with pytest.raises(HTTPException) as excinfo:
        draw(db, "alice")

    assert excinfo.value.status_code == 404
    assert "No potential matches found" in excinfo.value.detail
    assert "not configured" not in excinfo.value.detail


_SLOT_BOX = [COHORT_A, COHORT_B, COHORT_C]

_ORDERED_PAIRS = [
    (first, second) for first in _SLOT_BOX for second in _SLOT_BOX
]


@pytest.mark.parametrize(
    "seeker_cohort,target_cohort",
    _ORDERED_PAIRS,
    ids=[f"{d_a}{y_a}->{d_b}{y_b}" for (y_a, d_a), (y_b, d_b) in _ORDERED_PAIRS],
)
def test_a_slot_box_permits_every_pair_including_self(db, seeker_cohort, target_cohort):
    """A box holding A, B and C permits all of A->A, A->B, A->C, B->B, B->C and C->C.

    Enumerated rather than sampled, and in both directions, because the two halves of that claim
    fail independently: a rule that only ever paired *different* cohorts would pass every
    cross-cohort case here, and a rule that only paired them one way round would pass five of the
    six. The self-pairs are the half most likely to be lost, since nothing else in the row
    distinguishes them -- the row naming A is the same row whether or not anybody is drawn from
    inside it.
    """
    add_slot_box(db, _SLOT_BOX)
    add_user(db, "seeker", *seeker_cohort, "female")
    add_user(db, "target", *target_cohort, "male")

    result = draw(db, "seeker")

    assert result["status"] == "matched"
    assert result["matched_with"]["regno"] == "target"


def test_two_boxes_sharing_a_cohort_do_not_permit_their_other_halves(db):
    """Overlap is a union, not a chain.

    One box holding A and B, another holding B and C. A and C are each in a slot and neither is
    unconfigured, so this cannot be reported as a configuration problem -- it is a pairing the
    operator did not ask for, and it is refused. A rule that inferred A->C from A->B->C would let
    two operators each keeping a box to themselves produce a pairing neither wanted.
    """
    add_slot(db, *COHORT_A, *COHORT_B)
    add_slot(db, *COHORT_B, *COHORT_C)
    add_user(db, "alice", *COHORT_A, "female")
    add_user(db, "bob", *COHORT_C, "male")

    with pytest.raises(HTTPException) as excinfo:
        draw(db, "alice")

    assert excinfo.value.status_code == 404
    assert "not configured" not in excinfo.value.detail


def test_an_unconfigured_cohort_is_not_a_candidate_for_a_configured_one(db):
    """The rule is symmetric, and the asymmetry is the bug this catches.

    ``unlisted`` satisfies every other filter -- enabled, unblocked, opposite gender -- so an
    implementation that filtered the *candidate* side but not the *requester* side would happily
    pair a configured cohort with an unconfigured one. Asserted against the partner that was drawn
    rather than against a refusal, because ``bob`` is eligible and the draw succeeds either way: the
    test has to notice which of the two eligible partners it got.
    """
    add_slot(db, *COHORT_A, *COHORT_B)
    add_user(db, "alice", *COHORT_A, "female")
    add_user(db, "bob", *COHORT_B, "male")
    add_user(db, "unlisted", *COHORT_UNLISTED, "male")

    result = draw(db, "alice")

    assert result["matched_with"]["regno"] == "bob"


def test_an_unconfigured_cohort_spends_nothing(db):
    """No match, no conversation, and no attempt.

    The attempt is the part that matters. It used to be spent, on the reasoning that the user had
    confirmed the draw and watched the animation, so refunding would let this endpoint be polled for
    a configuration change without paying the cooldown. That reasoning does not hold for this
    refusal: nobody is withholding a configuration from anybody, the cooldown would buy exactly
    nothing, and the lockout is the entire complaint. The draw is refused before it writes, so the
    user can click again the instant an operator adds the slot.
    """
    add_slot(db, *COHORT_A, *COHORT_B)
    add_user(db, "alice", *COHORT_UNLISTED, "female")
    add_user(db, "bob", *COHORT_A, "male")

    with pytest.raises(HTTPException) as excinfo:
        draw(db, "alice")

    assert excinfo.value.status_code == 404
    assert not attempt_was_spent(db, "alice")
    with db.sessions() as session:
        assert session.scalar(select(func.count()).select_from(MatchRecord)) == 0
        assert session.scalar(select(func.count()).select_from(ConversationRecord)) == 0


def test_an_unconfigured_cohort_can_retry_the_instant_it_is_configured(db):
    """The consequence of not spending the attempt, stated as the behaviour it enables.

    The user has to be able to draw again without clearing a cooldown, so this does the whole
    sequence: refused, an operator adds the missing slot, drawn immediately. If the first draw
    consumed the attempt this fails on the cooldown rather than on the slot.
    """
    add_slot(db, *COHORT_A, *COHORT_B)
    add_user(db, "alice", *COHORT_UNLISTED, "female")
    add_user(db, "bob", *COHORT_A, "male")

    with pytest.raises(HTTPException):
        draw(db, "alice")

    add_slot(db, *COHORT_UNLISTED, *COHORT_A)
    # Deliberately no ``clear_cooldown``: nothing was spent, so nothing needs clearing.

    assert draw(db, "alice")["matched_with"]["regno"] == "bob"


def test_a_refused_draw_for_an_unconfigured_cohort_never_reached_a_candidate_query(db):
    """Refused on the rule, not by coming up empty.

    Both refusals are 404s and both once arrived from the same ``if not candidate_users`` branch, so
    this separates them at the source: with a partner available to draw and still refused, the only
    thing that can have stopped it is the cohort.
    """
    add_slot(db, *COHORT_A, *COHORT_B)
    add_user(db, "alice", *COHORT_UNLISTED, "female")
    add_user(db, "bob", *COHORT_A, "male")

    with pytest.raises(HTTPException) as excinfo:
        draw(db, "alice")

    assert excinfo.value.code == matchmaking_service.COHORT_NOT_CONFIGURED
    assert "has not been used" in excinfo.value.detail


def test_a_slot_created_between_two_draws_governs_the_second(db):
    """The rule is read per draw, not decided once at import.

    An operator edits this on a screen while people are using it, so a value resolved at startup
    would leave the configuration looking applied and matching nothing differently for the life of
    the process. One draw before the row exists and one after is the only way to show the switch is
    live rather than cached.
    """
    add_user(db, "alice", *COHORT_UNLISTED, "female")
    add_user(db, "bob", *(3, "Elsewhere"), "male")

    assert draw(db, "alice")["matched_with"]["regno"] == "bob"

    add_slot(db, *COHORT_A, *COHORT_B)
    clear_cooldown(db, "alice")

    with pytest.raises(HTTPException) as excinfo:
        draw(db, "alice")

    assert "not configured for matchmaking" in excinfo.value.detail


def test_the_draw_endpoint_reports_the_unconfigured_cohort(db, _confession_author_client):
    """Through the router, because the detail string is what the client actually reads.

    The whole refusal is one string in a 404 body, and the matchmaking page renders it through
    ``describeApiError``. Nothing here can fail on the rule -- the tests above cover that -- so this
    exists to pin that the message survives the trip through HTTP rather than being flattened into a
    generic failure by the client.

    Also asserts the attempt survives the trip, since a client that has to learn this from the
    absence of a cooldown rather than from the message would be a much worse contract.
    """
    add_slot(db, *COHORT_A, *COHORT_B)

    response = _confession_author_client.post("/matchmaking/find")

    assert response.status_code == 404
    assert "not configured for matchmaking" in response.json()["detail"]
    assert "has not been used" in response.json()["detail"]
    assert not attempt_was_spent(db, "authoruser")


def test_the_unconfigured_cohort_refusal_carries_its_own_code(db, _confession_author_client):
    """The page needs to tell this refusal apart from bad luck, and cannot do it from the wording.

    A sentence that says "try again later" and a sentence that means "never, until an operator acts"
    are different answers to the same 404, and the client has to know which it got. Asserted on the
    wire because the code is a contract: a client branching on it is entitled to depend on it, so it
    belongs in a test that would fail if the handler stopped being registered.
    """
    add_slot(db, *COHORT_A, *COHORT_B)

    body = _confession_author_client.post("/matchmaking/find").json()

    assert body["code"] == matchmaking_service.COHORT_NOT_CONFIGURED


def test_a_refusal_with_nobody_available_is_coded_separately(db, _confession_author_client):
    """The other half: the two codes must differ, or there is nothing to branch on.

    ``detail`` staying a plain string is part of the contract too -- ``describeApiError`` reads it
    as one, and the toast it feeds is unchanged.
    """
    # The author's own cohort, so the draw gets past the slot check: a slot naming nobody's cohort
    # would be refused as unconfigured and would not reach the branch under test.
    add_slot(db, 2, "Class", *COHORT_B)

    body = _confession_author_client.post("/matchmaking/find").json()

    assert body["code"] == matchmaking_service.NO_CANDIDATES
    assert isinstance(body["detail"], str)
    assert "No potential matches" in body["detail"]


def test_an_ordinary_404_still_has_only_a_detail(db, _confession_author_client):
    """Registering a handler for the subclass must not widen it to every 404.

    FastAPI walks the exception's bases when looking for a handler, so this is the test that keeps
    the draw handler from quietly becoming the handler for anything else the app happens to
    refuse.
    """
    response = _confession_author_client.get("/conversations/00000000-0000-0000-0000-000000000000")

    assert response.status_code == 404
    assert "code" not in response.json()


def test_the_eligibility_check_reports_a_cohort_with_no_slot(db, _confession_author_client):
    """So the page can refuse the click instead of the draw.

    This is the whole reason the check grew a third answer. Without it the user confirms, watches
    five seconds of animation and is then refused at the end of it -- which is exactly the
    experience the refusal was supposed to avoid.
    """
    add_slot(db, *COHORT_A, *COHORT_B)

    # The fixture's author is (2, "Class"), which no slot names.
    body = _confession_author_client.get("/matchmaking/check").json()

    assert body["status"] == "cohort_not_configured"
    assert "not in any matchmaking slot" in body["message"]


def test_the_eligibility_check_still_reports_a_cooldown(db, _confession_author_client):
    """The new answer must not swallow the old one.

    A user who is both on a cooldown and in an unconfigured cohort is told about the cooldown,
    because that check comes first and is the cheaper answer.

    Asked of the service rather than through the client: the client fixture resolves the caller to a
    hand-built ``UserDetails`` with no ``last_matchmaking_at``, so a cooldown written to the row
    would never be seen by the code under test.
    """
    add_slot(db, *COHORT_A, *COHORT_B)
    with db.sessions.begin() as session:
        row = session.scalar(select(UserRecord).where(UserRecord.regno == "authoruser"))
        row.last_matchmaking_at = datetime.now(timezone.utc) - timedelta(minutes=5)

    answer = matchmaking_service.check_matchmaking_cooldown_service(as_user(db, "authoruser"), db)

    assert answer["status"] == "cooldown"


def test_the_eligibility_check_says_eligible_for_a_configured_cohort(db, _confession_author_client):
    """The ordinary answer is untouched when the cohort is in a slot."""
    add_slot(db, 2, "Class", *COHORT_B)

    body = _confession_author_client.get("/matchmaking/check").json()

    assert body["status"] == "eligible"


def test_the_eligibility_check_reports_no_cohort_problem_with_no_slots_at_all(db, _confession_author_client):
    """With nothing configured every cohort may match, so there is nobody to refuse.

    Without this the check would tell every user their cohort is missing from the slot table the
    moment an operator opened the admin screen to create their first one.
    """
    assert _confession_author_client.get("/matchmaking/check").json()["status"] == "eligible"

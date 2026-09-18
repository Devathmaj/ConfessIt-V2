"""Seed a development database with participants and staff accounts.

Accounts are spread over three tables -- ``users``, ``admin`` and ``moderator`` -- because the
table a row is written to *is* its account type. There is no ``role`` column to set, and no
privilege that can be named in a payload: an account is staff or it is not, by existing.

Five things about the current schema are easy to get wrong from memory, and each of them is a
crash rather than a slightly wrong row:

* **No ``role`` column.** The previous version of this script passed ``role="user"``, which the
  account tables have not had for some time. SQLAlchemy rejects an unknown keyword outright, so
  the script could not have run at all.
* **Interests are a closed vocabulary.** ``ck_users_interests_known`` requires every element to be
  one of ``limits.INTEREST_VALUES`` -- case included, so "music" is not "Music". A hand-written
  list of interests is a ``CheckViolation`` on the first insert, which aborts the whole
  transaction and leaves the database empty. This is not hypothetical: the sibling seeder
  ``app/test.py`` still carries the pre-vocabulary values and fails exactly this way. The
  interests here are *derived* from the constant instead, so widening the vocabulary needs no
  edit here and cannot drift out of step with it.
* **Email is required and unique.** Every account table has a NOT NULL, uniquely indexed
  ``email``, so a seed that omits it is rejected by the column rather than defaulted.
* **Staff rows have no participant columns.** ``year``, ``department``, ``bio``, ``emoji``,
  ``interests`` and the matchmaking toggles exist only on ``users``. Passing one to
  ``ModeratorRecord`` is a ``TypeError``, so staff seeds carry only the shared columns.
* **Year is a bounded number, like interests are a closed set.** ``users.year`` is an ``Integer`` and
  ``ck_users_year_known`` holds it between ``limits.USER_YEAR_MIN`` and ``limits.USER_YEAR_MAX``. A
  hand-written year outside that range is a ``CheckViolation`` on the first insert, which aborts the
  transaction and leaves the database empty, so the years here are derived from those bounds rather
  than typed. Department has no such constraint and stays free text -- the departments that exist
  belong to the institution, not to this schema.

Re-running is the normal case while working on a schema, so an account that already exists is
left untouched rather than overwritten, and uniqueness is checked *per table* -- which is how
PostgreSQL enforces it here, since ``regno`` and ``email`` are unique within a table and never
across the three.
"""

import os
import sys

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from sqlalchemy import select

from app.accounts import AccountType
from app.db_models import ACCOUNT_MODELS
from app.limits import INTEREST_VALUES, USER_YEAR_MAX, USER_YEAR_MIN
from app.postgres import PostgresClient

# A school run of this app has a real gender mix, and the matchmaking and filtering screens sort
# and group by gender, so a seed that is all one value hides layout and filter bugs. Each year is
# given a slightly different split for the same reason.
#
# Derived from the year rather than written out as a twelve-row table, which is what the previous
# four-row version was. A table that has to be kept in step with the range is a second place to
# forget: raising the bounds to thirteen would have left the seed silently filling twelve years,
# with no error and one year that simply had nobody in it. Deriving it means widening the range in
# ``limits.py`` is the only edit, and this follows.
def _year_gender_counts(year: int) -> tuple[int, int]:
    """Male and female participants to seed in ``year``.

    The variation is real rather than cosmetic: a seed where every year holds the same number of
    people hides the sort order and the row count of every year-grouped screen, and a cohort filter
    where all twelve options have identical counts looks like a rendering bug when it is not.
    """
    return (10 + year % 3, 11 + (year + 1) % 3)


# Departments are free text, so these are typed here and only here. Three is enough for the
# matchmaking palette and the cohort filter to have something to group by, and with twelve years
# they give thirty-six cohorts -- enough that the cohort filter is a real filter rather than a
# lookup.
SEED_DEPARTMENTS = ("CSE", "ECE", "MECH")

# How many interests a seeded participant holds. Two is enough for a profile to show something
# and for matchmaking to have something to match on, without making the seed data look curated.
SEED_INTEREST_COUNT = 2

# The staff a developer needs to sign in as to reach the moderation and admin screens at all.
# The admin regno is the bare word "admin" because that is what the docs and the walkthroughs
# tell people to type; the moderators are numbered so there is more than one to switch between.
ADMIN_SEED = {
    "regno": "admin",
    "name": "Test Admin",
    "email": "admin@test.com",
    "gender": "male",
    "profile_picture_url": None,
    "notifications_enabled": True,
}

MODERATOR_SEEDS = (
    {
        "regno": "moderator1",
        "name": "Test Moderator 1",
        "email": "moderator1@test.com",
        # All three genders appear across the staff seeds, so a screen that renders or filters a
        # staff list is exercised against the whole vocabulary rather than one value.
        "gender": "male",
    },
    {
        "regno": "moderator2",
        "name": "Test Moderator 2",
        "email": "moderator2@test.com",
        "gender": "female",
    },
    {
        "regno": "moderator3",
        "name": "Test Moderator 3",
        "email": "moderator3@test.com",
        "gender": "other",
    },
)


def _interests_for(index: int) -> list[str]:
    """Two interests for the participant at ``index``, rotating through the vocabulary.

    Built from ``INTEREST_VALUES`` rather than written out, which is the only way this stays
    correct: a literal list is a list that eventually disagrees with the constraint enforcing it,
    and the failure is a ``CheckViolation`` on the first insert with nothing to indicate which
    value was wrong. Taking a rotating window also means the whole vocabulary is covered once the
    seed runs, instead of the first two entries over and over.
    """
    size = len(INTEREST_VALUES)
    start = index % size
    return [INTEREST_VALUES[(start + offset) % size] for offset in range(SEED_INTEREST_COUNT)]


def _split_count(count: int, parts: int, rotate: int) -> list[int]:
    """``count`` divided across ``parts`` buckets, as evenly as the number divides at all.

    ``divmod`` keeps the split exact: the parts always sum back to ``count``, so widening
    ``SEED_DEPARTMENTS`` thins out the cohorts instead of changing how many participants the
    year was supposed to hold. The remainder goes to consecutive buckets starting at ``rotate``
    rather than always to the first, so the uneven cohorts do not all pile into CSE and leave
    MECH uniformly short in every year.
    """
    base, remainder = divmod(count, parts)
    counts = [base] * parts
    for offset in range(remainder):
        counts[(rotate + offset) % parts] += 1
    return counts


def _cohort_distributions() -> list[tuple[int, str, int, int]]:
    """Every ``(year, department, male_count, female_count)`` cohort the seed fills.

    The full cross-product of the year range and the departments, so each year holds every
    department and each department spans every year. That is what makes the two fields worth having
    separate: with one joined ``class_name`` there was no way to ask either question, and a seed
    that only ever paired a year with a single department could not answer them either. It is also
    the only shape that exercises the filtering it exists for -- a cohort filter over a handful of
    non-overlapping cohorts is a lookup, not a filter, and would pass while the pair comparison
    were wrong.

    The two halves are rotated differently so a cohort is not systematically the male-heavy one.
    """
    size = len(SEED_DEPARTMENTS)
    distributions: list[tuple[int, str, int, int]] = []
    for year_index, year in enumerate(range(USER_YEAR_MIN, USER_YEAR_MAX + 1)):
        male_count, female_count = _year_gender_counts(year)
        male_counts = _split_count(male_count, size, year_index)
        female_counts = _split_count(female_count, size, year_index + 1)
        for department_index, department in enumerate(SEED_DEPARTMENTS):
            distributions.append(
                (year, department, male_counts[department_index], female_counts[department_index])
            )
    return distributions


def _participant_seeds() -> list[dict]:
    """Build the participant rows, one dict per row, keyed to the ``users`` columns.

    Regnos are prefixed by gender (``MA``/``FE``) so somebody reading a login table can tell at a
    glance which half of a cohort a regno belongs to, and so a participant regno can never
    collide with a seeded staff one.
    """
    seeds: list[dict] = []
    counters = {"male": 0, "female": 0}
    for year, department, male_count, female_count in _cohort_distributions():
        # The two halves of a cohort are described by one tuple each rather than by two near
        # identical blocks, so a field added to ``_participant`` cannot be added to one gender
        # and forgotten in the other.
        for gender, prefix, count in (("male", "MA", male_count), ("female", "FE", female_count)):
            for _ in range(count):
                # Numbering continues across cohorts rather than restarting per year, so a regno
                # identifies the account rather than the cohort it happened to be drawn in.
                counters[gender] += 1
                regno = f"{prefix}{counters[gender]}"
                seeds.append(_participant(regno, gender, year, department, len(seeds)))
    return seeds


def _participant(regno: str, gender: str, year: int, department: str, index: int) -> dict:
    return {
        "regno": regno,
        "name": f"Test User {regno}",
        "email": f"{regno.lower()}@test.com",
        "gender": gender,
        "year": year,
        "department": department,
        "emoji": None,
        "bio": f"Seeded {department} {year} {gender} participant.",
        "interests": _interests_for(index),
        "profile_picture_url": None,
        "matchmaking_enabled": True,
        "notifications_enabled": True,
        "love_notes_receive_enabled": True,
        "love_notes_send_enabled": False,
    }


def _staff_seeds() -> list[tuple[str, dict]]:
    """The admin and the three moderators, as ``(account_type, values)`` pairs.

    Each moderator dict is completed from ``ADMIN_SEED``'s shape rather than repeated, because a
    staff row carries exactly the columns ``AccountColumns`` declares -- and a value in here that
    is not one of those columns is a ``TypeError`` at insert rather than a silently ignored key.
    """
    shared = {
        "profile_picture_url": None,
        "notifications_enabled": True,
        # Left unblocked explicitly so a re-seed cannot be read as "staff start blocked".
        "is_blocked": False,
    }
    return (
        [(AccountType.ADMIN.value, {**ADMIN_SEED, **shared})]
        + [
            (
                AccountType.MODERATOR.value,
                {**moderator, **shared},
            )
            for moderator in MODERATOR_SEEDS
        ]
    )


def seed_database() -> None:
    """Insert any missing participants and staff accounts. Safe to run repeatedly."""
    # A one-shot script owns its own engine and disposes it in the finally block; only the served
    # application shares the singleton from app.postgres.
    client = PostgresClient()
    try:
        # The seed is how a fresh database gets its tables, so it cannot assume a booted
        # application already created them. This is a no-op against an existing schema.
        client.initialize()

        accounts: list[tuple[str, dict]] = [
            (AccountType.USERS.value, values) for values in _participant_seeds()
        ] + _staff_seeds()

        inserted: list[tuple[str, str]] = []
        skipped: list[tuple[str, str]] = []
        with client.sessions.begin() as session:
            for account_type, values in accounts:
                model = ACCOUNT_MODELS[account_type]
                existing = session.scalar(
                    select(model).where(model.regno == values["regno"])
                )
                if existing is not None:
                    # Left exactly as it is: a developer may have edited this account by hand, and
                    # a re-seed should not undo that. Uniqueness is per table, so this looks only
                    # in the table being written to.
                    skipped.append((values["regno"], account_type))
                    continue
                session.add(model(**values))
                inserted.append((values["regno"], account_type))

        for account_type in (
            AccountType.USERS.value,
            AccountType.ADMIN.value,
            AccountType.MODERATOR.value,
        ):
            table = model_label(account_type)
            added = [regno for regno, kind in inserted if kind == account_type]
            print(f"{table}: {len(added)} inserted.")

        if skipped:
            print(f"\n{len(skipped)} account(s) already existed and were left unchanged:")
            for regno, account_type in skipped:
                print(f"  {regno:<12} {account_type}")

        print(
            "\nSign in with any seeded regno (e.g. 'admin', 'moderator1', 'MA1'). "
            "Passwords are not set here -- request a magic link from the sign-in screen."
        )
    finally:
        client.close()


def model_label(account_type: str) -> str:
    return f"{account_type} ({ACCOUNT_MODELS[account_type].__tablename__})"


if __name__ == "__main__":
    seed_database()

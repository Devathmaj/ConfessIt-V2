"""Seed a development database with participants and staff accounts.

Accounts are spread over three tables -- ``users``, ``admin`` and ``moderator``
-- because the table a row is written to *is* its account type. There is no
``role`` column and no privilege that can be named in a payload: an account is
staff or it is not, by existing.

Five things about the current schema are easy to get wrong from memory, and
each is a crash rather than a slightly wrong row:

* **No ``role`` column.** A previous version of this script passed
  ``role="user"``; SQLAlchemy rejects an unknown keyword outright.
* **Interests are a closed vocabulary.** ``ck_users_interests_known`` requires
  every element of ``limits.INTEREST_VALUES``, case included. A hand-written
  list is a ``CheckViolation`` on the first insert, which aborts the whole
  transaction and leaves the database empty -- that is exactly how the sibling
  seeder ``app/test.py`` fails. The interests here are *derived* from the
  constant instead, so widening the vocabulary needs no edit here.
* **Email is required and unique.** Every account table has a NOT NULL,
  uniquely indexed ``email``.
* **Staff rows have no participant columns.** ``year``, ``department``,
  ``bio``, ``interests`` and the matchmaking toggles exist only on ``users``;
  passing one to ``ModeratorRecord`` is a ``TypeError``. (``emoji`` is not a
  participant column either -- the profile-emoji option was removed entirely.)
* **Year is a bounded number, like interests are a closed set.**
  ``ck_users_year_known`` holds ``users.year`` between ``limits.USER_YEAR_MIN``
  and ``USER_YEAR_MAX``, so the years here are derived from those bounds
  rather than typed. Department has no such constraint and stays free text.

Re-running is the normal case while working on a schema: an account that
already exists is left untouched rather than overwritten, and uniqueness is
checked *per table* -- which is how PostgreSQL enforces it here.
"""

import os
import sys

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from sqlalchemy import select

from app.accounts import AccountType
from app.db_models import ACCOUNT_MODELS
from app.limits import INTEREST_VALUES, USER_YEAR_MAX, USER_YEAR_MIN
from app.postgres import PostgresClient

# A real gender mix matters: the matchmaking and filtering screens sort and
# group by gender, so an all-one-value seed hides layout and filter bugs, and
# each year gets a slightly different split for the same reason.
#
# Derived from the year rather than written out as a twelve-row table. A table
# kept in step with the range is a second place to forget; deriving it means
# widening the range in ``limits.py`` is the only edit, and this follows.
def _year_gender_counts(year: int) -> tuple[int, int]:
    """Male and female participants to seed in ``year``.

    The variation is real rather than cosmetic: a seed where every year holds
    the same number of people hides sort order and row counts on every
    year-grouped screen.
    """
    return (10 + year % 3, 11 + (year + 1) % 3)


# Departments are free text, so these are typed here and only here: enough for
# the matchmaking palette and cohort filter to group by, giving twelve years x
# three departments = thirty-six cohorts, a real filter rather than a lookup.
SEED_DEPARTMENTS = ("CSE", "ECE", "MECH")

# Two interests per participant: enough for a profile to show something and
# for matchmaking to match on, without looking curated.
SEED_INTEREST_COUNT = 2

# The staff sign-ins needed to reach the moderation and admin screens. The
# admin regno is the bare word "admin" because that is what the docs and
# walkthroughs tell people to type; moderators are numbered so there is more
# than one to switch between.
ADMIN_SEED = {
    "regno": "admin",
    "name": "Test Admin",
    "email": "admin@test.com",
}

MODERATOR_SEEDS = (
    {
        "regno": "moderator1",
        "name": "Test Moderator 1",
        "email": "moderator1@test.com",
    },
    {
        "regno": "moderator2",
        "name": "Test Moderator 2",
        "email": "moderator2@test.com",
    },
    {
        "regno": "moderator3",
        "name": "Test Moderator 3",
        "email": "moderator3@test.com",
    },
)


def _interests_for(index: int) -> list[str]:
    """Two interests for the participant at ``index``, rotating through the vocabulary.

    Built from ``INTEREST_VALUES`` rather than written out: a literal list is
    eventually a list that disagrees with the constraint enforcing it, and the
    failure is a ``CheckViolation`` with nothing to indicate which value was
    wrong. A rotating window also covers the whole vocabulary once the seed
    runs, instead of the first two entries over and over.
    """
    size = len(INTEREST_VALUES)
    start = index % size
    return [INTEREST_VALUES[(start + offset) % size] for offset in range(SEED_INTEREST_COUNT)]


def _split_count(count: int, parts: int, rotate: int) -> list[int]:
    """``count`` divided across ``parts`` buckets, as evenly as the number divides at all.

    ``divmod`` keeps the split exact, so widening ``SEED_DEPARTMENTS`` thins
    out the cohorts instead of changing the year's total. The remainder goes to
    consecutive buckets starting at ``rotate``, so the uneven cohorts do not
    all pile into CSE and leave MECH uniformly short.
    """
    base, remainder = divmod(count, parts)
    counts = [base] * parts
    for offset in range(remainder):
        counts[(rotate + offset) % parts] += 1
    return counts


def _cohort_distributions() -> list[tuple[int, str, int, int]]:
    """Every ``(year, department, male_count, female_count)`` cohort the seed fills.

    The full cross-product of the year range and the departments, so each year
    holds every department -- the only shape that exercises the filtering this
    exists for: a cohort filter over a handful of non-overlapping cohorts is a
    lookup, not a filter. The two halves are rotated differently so a cohort is
    not systematically the male-heavy one.
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

    Regnos are prefixed by gender (``MA``/``FE``) so a login table shows at a
    glance which half of a cohort a regno belongs to, and a participant regno
    can never collide with a seeded staff one.
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
        "bio": f"Seeded {department} {year} {gender} participant.",
        "interests": _interests_for(index),
        "profile_picture_public_id": None, "profile_picture_format": None,
        "matchmaking_enabled": True,
        "notifications_enabled": True,
        "love_notes_receive_enabled": True,
        "love_notes_send_enabled": False,
    }


def _staff_seeds() -> list[tuple[str, dict]]:
    """The admin and the three moderators, as ``(account_type, values)`` pairs.

    Each moderator dict is completed from ``ADMIN_SEED``'s shape rather than
    repeated: a staff row carries exactly the columns ``AccountRecord``
    declares, and anything else is a ``TypeError`` at insert. Gender and the
    two picture columns are deliberately absent -- they exist on ``users``
    only, so a staff seed carrying them would write columns the staff tables
    do not have.
    """
    shared = {
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
        # Apply the Alembic revisions first; this is a no-op against an existing schema.
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

"""Seed a development database with participants and staff accounts.

Accounts are spread over three tables -- ``users``, ``admin`` and ``moderator`` -- because the
table a row is written to *is* its account type. There is no ``role`` column to set, and no
privilege that can be named in a payload: an account is staff or it is not, by existing.

Four things about the current schema are easy to get wrong from memory, and each of them is a
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
* **Staff rows have no participant columns.** ``class_name``, ``bio``, ``emoji``, ``interests``
  and the matchmaking toggles exist only on ``users``. Passing one to ``ModeratorRecord`` is a
  ``TypeError``, so staff seeds carry only the shared columns.

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
from app.limits import INTEREST_VALUES
from app.postgres import PostgresClient

# A school run of this app has a real gender mix, and the matchmaking and filtering screens sort
# and group by gender, so a seed that is all one value hides layout and filter bugs. Each year is
# given a slightly different split for the same reason.
CLASS_DISTRIBUTIONS = (
    ("1st year", 12, 13),
    ("2nd year", 13, 12),
    ("3rd year", 12, 13),
    ("4th year", 13, 12),
)

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


def _participant_seeds() -> list[dict]:
    """Build the participant rows, one dict per row, keyed to the ``users`` columns.

    Regnos are prefixed by gender (``MA``/``FE``) so somebody reading a login table can tell at a
    glance which half of a cohort a regno belongs to, and so a participant regno can never
    collide with a seeded staff one.
    """
    seeds: list[dict] = []
    counters = {"male": 0, "female": 0}
    for class_name, male_count, female_count in CLASS_DISTRIBUTIONS:
        # The two halves of a year are described by one tuple each rather than by two near
        # identical blocks, so a field added to ``_participant`` cannot be added to one gender
        # and forgotten in the other.
        for gender, prefix, count in (("male", "MA", male_count), ("female", "FE", female_count)):
            for _ in range(count):
                # Numbering continues across years rather than restarting per class, so a regno
                # identifies the account rather than the class it happened to be drawn in.
                counters[gender] += 1
                regno = f"{prefix}{counters[gender]}"
                seeds.append(_participant(regno, gender, class_name, len(seeds)))
    return seeds


def _participant(regno: str, gender: str, class_name: str, index: int) -> dict:
    return {
        "regno": regno,
        "name": f"Test User {regno}",
        "email": f"{regno.lower()}@test.com",
        "gender": gender,
        "class_name": class_name,
        "emoji": None,
        "bio": f"Seeded {class_name} {gender} participant.",
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

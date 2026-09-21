import hashlib
import secrets
from datetime import datetime, timedelta, timezone

import psycopg
from sqlalchemy import select

from .accounts import AccountType
from .db_models import ACCOUNT_MODELS, LoginTokenRecord
from .postgres import PostgresClient

# 100 participants, split evenly. A school run of this app has a real gender mix and the
# matchmaking screens filter and sort by it, so a seed that is all one value hides layout and
# filter bugs. The count is deliberately round so the per-gender half is obvious.
PARTICIPANTS_PER_GENDER = 50

# Seeded profile variety, cycled through so the list, profile and matchmaking screens have
# more than one distinct value to render. Nothing here is load-bearing for the application.
#
# Year and department are two fields rather than one joined string, because that is what the
# column is now: seeding a pre-joined "2nd Year CSE" would exercise a shape the schema can no
# longer store. ``YEARS`` spans the ``limits.USER_YEAR_MIN``..``USER_YEAR_MAX`` range the CHECK
# constraint holds the column to, and holds numbers because the column is an ``Integer`` -- an
# invented year here would be refused by the database rather than stored.
YEARS = tuple(range(1, 13))
DEPARTMENTS = ("Test_Department_A", "Test_Department_B", "Test_Department_C")
INTERESTS = ("music", "games", "films", "books", "sports")


def create_magic_link_token() -> str:
    """
    Generates a secure, random token for magic link authentication.
    """
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """
    Hashes a token using SHA-256 for secure storage.
    """
    return hashlib.sha256(token.encode()).hexdigest()


def _participants() -> list[dict]:
    """Build the 100 participant rows: 50 male, then 50 female.

    Regnos are prefixed by gender so a person reading a login table can tell at a glance
    which half of the cohort a regno belongs to, and so a regno never collides with the
    staff accounts seeded alongside them.
    """
    rows: list[dict] = []
    for prefix, gender in (("M", "male"), ("F", "female")):
        for index in range(1, PARTICIPANTS_PER_GENDER + 1):
            regno = f"{prefix}{index:02d}"
            # Every tenth account opts out of matchmaking, and only half send love notes, so
            # both halves of each toggle are represented without anyone having to hand-edit
            # the seed before checking that a disabled control looks disabled.
            rows.append(
                {
                    "regno": regno,
                    "name": f"Test User {prefix}{index:02d}",
                    "email": f"{regno.lower()}@test.com",
                    "gender": gender,
                    "year": YEARS[index % len(YEARS)],
                    "department": DEPARTMENTS[index % len(DEPARTMENTS)],
                    "bio": f"Seeded {gender} participant {regno}.",
                    "interests": list(INTERESTS[index % len(INTERESTS) : index % len(INTERESTS) + 2]),
                    "profile_picture_public_id": None, "profile_picture_format": None,
                    "matchmaking_enabled": index % 10 != 0,
                    "notifications_enabled": True,
                    "love_notes_receive_enabled": True,
                    "love_notes_send_enabled": index % 2 == 1,
                }
            )
    return rows


def _staff() -> list[tuple[str, dict]]:
    """One admin and one moderator, the two staff tables a developer needs to sign in as.

    They are seeded on every run even when the participants are not, because a moderator
    account is the only way to reach the moderation screens at all.

    Each carries an address, which every account table now has a column for. It is not
    load-bearing -- sign-in is regno-based and the column is nullable -- but a staff row with
    no address on it is a row the admin list renders with a blank contact line, which looks
    like a bug rather than like a column nobody wrote. The addresses are distinct from the
    participants' and from each other, so this seed does not also become the example of a
    shared address; that case is covered by tests instead.
    """
    return [
        (AccountType.ADMIN.value, {
            "regno": "admin", "name": "Test Admin", "email": "admin@test.com",
            "notifications_enabled": True,
        }),
        (AccountType.MODERATOR.value, {
            "regno": "moderator", "name": "Test Moderator", "email": "moderator@test.com",
            "notifications_enabled": True,
        }),
    ]


def main():
    """
    Seed 100 participants, one admin and one moderator, then print a magic link token for each.

    Participants live in ``users`` and staff live in ``admin`` and ``moderator``. There is no
    ``role`` to set -- the table a row is written to *is* the account type -- and each token
    records both the account id and the table it came from, which is how redemption knows
    where to look.
    """
    try:
        # A one-shot script owns its own engine and disposes it in the finally block.
        client = PostgresClient()
        # The seed is how a fresh database gets its tables, so it cannot assume a booted
        # application already created them. initialize() is a no-op on an existing schema.
        client.initialize()

        # (account_type, values) per entry. Participant fields stay on the participant row; a
        # staff row carries only the columns the staff tables share.
        accounts: list[tuple[str, dict]] = [
            (AccountType.USERS.value, values) for values in _participants()
        ] + _staff()

        issued_tokens = []
        with client.sessions.begin() as session:
            for account_type, values in accounts:
                model = ACCOUNT_MODELS[account_type]
                account_row = session.scalar(select(model).where(model.regno == values["regno"]))
                if not account_row:
                    account_row = model(**values)
                    session.add(account_row)
                    session.flush()
                else:
                    # Re-running is the normal case while iterating on a schema, so an
                    # existing account is left exactly as it is and only a fresh token is
                    # issued for it.
                    print(f"Account {values['regno']} already exists in {account_type}.")

                token = create_magic_link_token()
                issued_at = datetime.now(timezone.utc)
                token_row = LoginTokenRecord(
                    account_id=account_row.id, account_type=account_type,
                    token_hash=hash_token(token), issued_at=issued_at,
                    expires_at=issued_at + timedelta(hours=1), used=False, revoked=False,
                    request_ip="127.0.0.1", request_user_agent="Test Script", attempt_count=0,
                    session_status="issued",
                )
                session.add(token_row)
                issued_tokens.append((values["regno"], values["name"], account_type, token))

        for account_type in (AccountType.USERS.value, AccountType.ADMIN.value, AccountType.MODERATOR.value):
            seeded = [entry for entry in issued_tokens if entry[2] == account_type]
            print(f"\nSeeded {len(seeded)} account(s) in {account_type}.")

        print("\nMagic link tokens, valid for one hour:")
        for regno, name, account_type, token in issued_tokens:
            print(f"  {regno:<10} {account_type:<10} {name:<22} {token}")

    except psycopg.Error as e:
        print(f"Could not connect to PostgreSQL: {e}")
    except Exception as e:
        print(f"An error occurred: {e}")
    finally:
        # Ensure the client connection is closed
        if 'client' in locals() and client:
            client.close()

if __name__ == "__main__":
    main()

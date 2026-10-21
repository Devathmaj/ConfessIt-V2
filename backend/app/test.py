import os
import secrets
from datetime import datetime, timedelta, timezone

import psycopg
from sqlalchemy import select

from .accounts import AccountType
from .config import settings
from .db_models import ACCOUNT_MODELS, LoginTokenRecord
from .limits import INTEREST_VALUES, USER_YEAR_MAX, USER_YEAR_MIN
from .postgres import PostgresClient
from .services.auth_service import TOKEN_HASH_DOMAIN_MAGIC, hash_token

# Seed 50 participants per gender so development data exercises both matchmaking cohorts.
PARTICIPANTS_PER_GENDER = 50

# Cycle valid year, department, and interest values to give development profiles variety.
YEARS = tuple(range(USER_YEAR_MIN, USER_YEAR_MAX + 1))
DEPARTMENTS = ("Test_Department_A", "Test_Department_B", "Test_Department_C")
INTERESTS = INTEREST_VALUES
# Wrap the selection so every seeded participant receives two interests.
SEED_INTEREST_COUNT = 2


def create_magic_link_token() -> str:
    """
    Generates a secure, random token for magic link authentication.
    """
    return secrets.token_urlsafe(32)


def _participants() -> list[dict]:
    """Build 100 participant rows with gender-prefixed, staff-distinct regnos."""
    rows: list[dict] = []
    for prefix, gender in (("M", "male"), ("F", "female")):
        for index in range(1, PARTICIPANTS_PER_GENDER + 1):
            regno = f"{prefix}{index:02d}"
            # Include opted-out accounts so both states of each toggle are testable.
            rows.append(
                {
                    "regno": regno,
                    "name": f"Test User {prefix}{index:02d}",
                    "email": f"{regno.lower()}@test.com",
                    "gender": gender,
                    "year": YEARS[index % len(YEARS)],
                    "department": DEPARTMENTS[index % len(DEPARTMENTS)],
                    "bio": f"Seeded {gender} participant {regno}.",
                    "interests": [
                        INTERESTS[(index + offset) % len(INTERESTS)]
                        for offset in range(SEED_INTEREST_COUNT)
                    ],
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

    Seeded on every run even when the participants are not: a moderator account
    is the only way to reach the moderation screens at all. Each carries an
    address (a nullable, non-load-bearing column) so the admin list does not
    render a blank contact line that looks like a bug. The addresses are distinct
    from the participants' and from each other; the shared-address case is
    covered by tests instead.
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

    Debug-gated: this mints live bearer tokens (102 per run) and must never run in
    production. Refuses unless ENABLE_DEBUG_ENDPOINTS is on or ALLOW_TEST_SEED=1.
    """
    if not settings.ENABLE_DEBUG_ENDPOINTS and os.getenv("ALLOW_TEST_SEED") != "1":
        print(
            "Refusing to seed test accounts: set ENABLE_DEBUG_ENDPOINTS=true "
            "or ALLOW_TEST_SEED=1 to run this debug-only seeder."
        )
        return
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
                    token_hash=hash_token(token, domain=TOKEN_HASH_DOMAIN_MAGIC), issued_at=issued_at,
                    expires_at=issued_at + timedelta(minutes=settings.LOGIN_TOKEN_EXPIRE_MINUTES),
                    used=False, revoked=False,
                    request_ip="127.0.0.1", request_user_agent="Test Script", attempt_count=0,
                    session_status="issued",
                )
                session.add(token_row)
                issued_tokens.append((values["regno"], values["name"], account_type, token))

        for account_type in (AccountType.USERS.value, AccountType.ADMIN.value, AccountType.MODERATOR.value):
            seeded = [entry for entry in issued_tokens if entry[2] == account_type]
            print(f"\nSeeded {len(seeded)} account(s) in {account_type}.")

        print(f"\nMagic link tokens, valid for {settings.LOGIN_TOKEN_EXPIRE_MINUTES} minutes:")
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

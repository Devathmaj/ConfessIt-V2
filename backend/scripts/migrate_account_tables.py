"""One-off migration from the single role-bearing ``users`` table to three account tables.

Before this change, every account -- participant, admin and moderator alike -- was a row in
``users`` with a ``role`` column saying which. The split moves staff into their own tables
(``admin`` and ``moderator``) and deletes the column. That is not a shape change the running
application can absorb on its own, because two things about it are decisions about data
rather than about schema:

* the staff rows have to *move*, carrying their primary keys with them, so every foreign key
  and every magic-link token that already points at them still resolves; and
* ``login_tokens`` was keyed on ``users.id`` through a foreign key. A token can now belong to
  an account in any of the three tables, so the column becomes a plain uuid plus an
  ``account_type`` saying where to read it. The old column cannot simply be dropped first,
  because that is the only record of which account a live token belongs to.

Everything is done in one transaction, and ``--dry-run`` rolls that transaction back, so the
whole script is safe to run on a database that has already been migrated: each step checks
what is actually present first and skips what is not needed.

Usage::

    python -m scripts.migrate_account_tables                  # migrate settings.DATABASE_URL
    python -m scripts.migrate_account_tables --dry-run        # report, change nothing
    python -m scripts.migrate_account_tables --database-url postgresql+psycopg://...
"""

from __future__ import annotations

import argparse
import sys
from typing import Iterable

from sqlalchemy import text

from app.config import settings
from app.postgres import RETIRED_COLUMNS, PostgresClient


# Which old role value became which new table. Anything not listed here stayed a participant,
# which is the same fail-closed reading the application uses for an unrecognised value.
ROLE_TO_ACCOUNT_TABLE: dict[str, str] = {"admin": "admin", "moderator": "moderator"}

# The columns every account table has. Copied verbatim from the users row so a staff account
# keeps its id, its timestamps and its last-login record through the move.
#
# ``gender`` and the two profile-picture columns are deliberately absent. They are participant
# fields now -- only a participant is a matchmaking candidate and only a participant can upload
# a picture -- so a staff table created from ``Base.metadata`` has no such column, and copying
# one across would fail the INSERT on a fresh run. On a database split by an earlier run of
# this script the columns are still there; ``PostgresClient.apply_retired_columns`` drops them
# on the next boot, which is where that cleanup lives rather than here.
SHARED_ACCOUNT_COLUMNS: tuple[str, ...] = (
    "id",
    "regno",
    "name",
    # Every account table carries an address, so a staff row keeps the one it had as a
    # ``users`` row rather than having it silently dropped on the way across.
    "email",
    "notifications_enabled",
    "is_blocked",
    "last_login_at",
    "last_login_ip",
    "last_login_user_agent",
    "created_at",
    "updated_at",
)

# The columns a staff row must not carry. They are dropped from ``users`` at the end of the
# run, and anything holding a value for them in ``login_tokens``-style references is what the
# pre-flight check below looks for.
PARTICIPANT_ONLY_COLUMNS: tuple[str, ...] = (
    "bio",
    # Two columns where there was once one ``class_name``. Both are NOT NULL on ``users`` and both
    # describe only a participant, so both are dropped from a staff row rather than being carried
    # across as empty strings -- a moderator is not in a year and a department, and a row that
    # said so would be a cohort of one.
    "year",
    "department",
    "interests",
    "matchmaking_enabled",
    "love_notes_receive_enabled",
    "love_notes_send_enabled",
    "last_matchmaking_at",
)

# Tables that hold a foreign key, or a regno, pointing at a participant. A staff row with a
# participant row in any of these is a contradiction in the data rather than something to
# silently delete, so the migration stops and names it.
PARTICIPANT_REFERENCES: tuple[tuple[str, str], ...] = (
    ("confessions", "author_id"),
    ("confession_comments", "user_id"),
    ("love_notes", "sender_id"),
    ("love_notes", "recipient_id"),
    ("matches", "user_1_regno"),
    ("matches", "user_2_regno"),
    ("conversations", "initiator_regno"),
    ("conversations", "receiver_regno"),
    ("notifications", "recipient_regno"),
)


def _columns(connection, table: str) -> set[str]:
    return {
        row[0]
        for row in connection.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = :name"
            ),
            {"name": table},
        )
    }


def _table_exists(connection, table: str) -> bool:
    return bool(
        connection.execute(
            text("SELECT 1 FROM pg_tables WHERE schemaname = 'public' AND tablename = :name"),
            {"name": table},
        ).fetchone()
    )


def _report(steps: list[str], message: str) -> None:
    steps.append(message)


# ---------------------------------------------------------------- pre-flight


def _preflight(connection) -> list[str]:
    """Check the assumptions the move depends on, and return the problems found.

    Refuses rather than repairing. Deleting a participant's confession because an admin
    account happened to share its id is the kind of mistake that is only noticed later, and
    a migration that stops with a list is more useful than one that guesses.
    """
    problems: list[str] = []

    users_columns = _columns(connection, "users")
    if "role" not in users_columns:
        # Already migrated, or never had roles. Nothing to move.
        return problems

    staff = connection.execute(
        text("SELECT id, regno, role FROM users WHERE role = ANY(:roles) ORDER BY regno"),
        {"roles": list(ROLE_TO_ACCOUNT_TABLE)},
    ).all()
    if not staff:
        return problems

    for row_id, regno, _role in staff:
        for table, column in PARTICIPANT_REFERENCES:
            if not _table_exists(connection, table):
                continue
            if column.endswith("_regno"):
                present = connection.execute(
                    text(f'SELECT 1 FROM "{table}" WHERE "{column}" = :value LIMIT 1'),
                    {"value": regno},
                ).fetchone()
            else:
                # The referencing column is a uuid and the id came back from the driver as a
                # uuid already, but the cast is spelled out so the check cannot depend on
                # how a driver chooses to adapt the value.
                present = connection.execute(
                    text(
                        f'SELECT 1 FROM "{table}" '
                        f'WHERE "{column}" = CAST(:value AS uuid) LIMIT 1'
                    ),
                    {"value": str(row_id)},
                ).fetchone()
            if present:
                problems.append(
                    f'{regno} (role={_role}) is referenced by {table}.{column}; '
                    "a staff account cannot be a participant of a confession, note or match"
                )

    # A regno already taken in the destination table would make the move ambiguous, because
    # regno is what a caller logs in with. The same id on both sides is not ambiguous, though:
    # that is a move that already happened and only the old copy is left behind, which the
    # delete below clears. Two different ids under one regno is two accounts a person cannot
    # tell apart at login, and which one they meant is not a question this script can answer.
    for role, table in ROLE_TO_ACCOUNT_TABLE.items():
        if not _table_exists(connection, table):
            continue
        clashing = connection.execute(
            text(
                f'SELECT u.regno, u.id, t.id FROM users u JOIN "{table}" t ON t.regno = u.regno '
                "WHERE u.role = :role ORDER BY u.regno"
            ),
            {"role": role},
        ).all()
        problems.extend(
            f"regno {regno!r} exists in {table} as {destination} and in users as {source}; "
            "two accounts share one login, so resolve before migrating"
            for regno, source, destination in clashing
            if source != destination
        )

    return problems


# ---------------------------------------------------------------- steps


def _create_staff_tables(connection, steps: list[str]) -> None:
    """Create ``admin`` and ``moderator`` if they are absent.

    A deployment that has never run the new code has neither table, and the move below
    writes into them. The shape is taken from the models rather than written out here, so
    this cannot drift from what the application expects.
    """
    from app.db_models import Base

    created = []
    for role, table in ROLE_TO_ACCOUNT_TABLE.items():
        if not _table_exists(connection, table):
            Base.metadata.tables[table].create(bind=connection)
            created.append(f"{table} (for role={role})")
    if created:
        _report(steps, "created staff tables: " + ", ".join(created))
    else:
        _report(steps, "staff tables already present")

    _add_shared_email_column(connection, steps)


def _add_shared_email_column(connection, steps: list[str]) -> None:
    """Give every account table an ``email`` column, and make ``users.email`` nullable.

    An address is how a person is reached, which is true of a moderator as much as a student,
    so all three tables carry one now. A staff table created by an earlier run of this script
    predates that and would not have the column at all, so it is added here rather than being
    left to the boot path.

    Two things this deliberately does not do. It does not deduplicate: a staff table cannot
    already hold two rows with one address, because the column did not exist. And it does not
    refuse if ``users`` holds a duplicate -- that cannot happen either, the column has been
    unique there throughout.

    The NOT NULL is dropped on ``users`` rather than left in place. The model declares the
    column nullable on all three tables so that a row written by a script, a seed, or this
    migration itself is not rejected for a field the API only *requires* of a participant.
    Every existing row has a value, so relaxing the constraint loses nothing and refuses
    nothing.
    """
    for table in ("users", "admin", "moderator"):
        if not _table_exists(connection, table):
            continue
        if "email" not in _columns(connection, table):
            connection.execute(
                text(f'ALTER TABLE "{table}" ADD COLUMN IF NOT EXISTS "email" varchar(320)')
            )
            _report(steps, f'added email column to {table}')

    if _table_exists(connection, "users"):
        nullable = connection.execute(
            text(
                "SELECT is_nullable FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = 'users' "
                "AND column_name = 'email'"
            )
        ).scalar()
        if nullable == "NO":
            connection.execute(text("ALTER TABLE users ALTER COLUMN email DROP NOT NULL"))
            _report(steps, "users.email is now nullable, as it is on the other two tables")


def _drop_participant_fields_from_staff(connection, steps: list[str]) -> None:
    """Remove ``RETIRED_COLUMNS`` from a staff table an earlier run gave them to.

    The column split that made these participant-only is applied at boot by
    ``PostgresClient.apply_retired_columns``, but this script can reach a state the boot path
    cannot: a database where ``users.role`` is still present (so the move below is about to
    run) and the staff tables were created by a version that put ``gender`` on all three. The
    copy would then omit a NOT NULL column and fail. Dropping them here, before the move, is
    what keeps the script's own promise that it is safe to run on any database in any order.
    """
    for table in ROLE_TO_ACCOUNT_TABLE.values():
        if not _table_exists(connection, table):
            continue
        present = _columns(connection, table)
        retired = [column for column in RETIRED_COLUMNS.get(table, ()) if column in present]
        for column in retired:
            connection.execute(
                text(f'ALTER TABLE "{table}" DROP COLUMN IF EXISTS "{column}"')
            )
        if retired:
            _report(
                steps,
                f"dropped participant-only column(s) from {table}: {', '.join(retired)}",
            )


def _move_staff_rows(connection, steps: list[str]) -> None:
    """Copy staff rows out of ``users`` into their own tables, keeping the primary key.

    The id is preserved deliberately. Eleven tables reference a participant by id, and the
    magic-link tokens reference an account by id; moving the row with its key means none of
    those have to be rewritten, and an account that was an admin yesterday is still the same
    account today.
    """
    if "role" not in _columns(connection, "users"):
        _report(steps, "users has no role column; nothing to move")
        return

    available = _columns(connection, "users")
    for role, table in ROLE_TO_ACCOUNT_TABLE.items():
        # created_at/updated_at are new, so an un-migrated users table may not have them
        # yet. The copy is built from the columns both sides actually have rather than
        # assumed, and the destination defaults fill in whatever is left out.
        shared = [column for column in SHARED_ACCOUNT_COLUMNS if column in available]
        shared_sql = ", ".join(f'"{column}"' for column in shared)
        result = connection.execute(
            text(
                f'INSERT INTO "{table}" ({shared_sql}) '
                f"SELECT {shared_sql} FROM users WHERE role = :role "
                "ON CONFLICT DO NOTHING"
            ),
            {"role": role},
        )
        _report(steps, f"copied {result.rowcount} role={role} row(s) into {table}")


def _migrate_login_tokens(connection, steps: list[str]) -> None:
    """Give every login token an account id and the account table it belongs to.

    ``account_id`` is added nullable first, backfilled from the old ``user_id``, and only
    made NOT NULL once it is actually populated -- a token whose account no longer exists is
    reported and left alone rather than having an id invented for it. ``user_id`` goes last,
    because until the backfill has run it is the only record of who the token is for.
    """
    columns = _columns(connection, "login_tokens")
    if "user_id" not in columns:
        _report(steps, "login_tokens has no user_id column; token identity already migrated")
        return

    connection.execute(text("ALTER TABLE login_tokens ADD COLUMN IF NOT EXISTS account_id uuid"))
    connection.execute(
        text("ALTER TABLE login_tokens ADD COLUMN IF NOT EXISTS account_type varchar(16)")
    )

    backfilled = connection.execute(
        text(
            "UPDATE login_tokens SET account_id = user_id "
            "WHERE account_id IS NULL AND user_id IS NOT NULL"
        )
    )
    _report(steps, f"backfilled account_id on {backfilled.rowcount} login token(s)")

    if "role" in _columns(connection, "users"):
        promoted = connection.execute(
            text(
                "UPDATE login_tokens SET account_type = users.role "
                "FROM users WHERE login_tokens.user_id = users.id "
                "AND users.role = ANY(:roles)"
            ),
            {"roles": list(ROLE_TO_ACCOUNT_TABLE)},
        )
        _report(steps, f"labelled {promoted.rowcount} staff login token(s) with their table")

    # Anything still unlabelled belongs to a participant, which is also the only value a
    # token can safely be assumed to mean.
    connection.execute(
        text("UPDATE login_tokens SET account_type = 'users' WHERE account_type IS NULL")
    )

    orphans = connection.execute(
        text("SELECT count(*) FROM login_tokens WHERE account_id IS NULL")
    ).scalar()
    if orphans:
        _report(
            steps,
            f"WARNING: {orphans} login token(s) have no resolvable account and were left "
            "nullable; they cannot be redeemed and can be deleted separately",
        )
    else:
        connection.execute(
            text("ALTER TABLE login_tokens ALTER COLUMN account_id SET NOT NULL")
        )
        _report(steps, "account_id is NOT NULL on every login token")

    connection.execute(text("ALTER TABLE login_tokens ALTER COLUMN account_type SET NOT NULL"))

    connection.execute(text("ALTER TABLE login_tokens DROP CONSTRAINT IF EXISTS login_tokens_user_id_fkey"))
    connection.execute(text("ALTER TABLE login_tokens DROP COLUMN IF EXISTS user_id"))
    _report(steps, "dropped login_tokens.user_id and its foreign key")


def _drop_staff_from_users(connection, steps: list[str]) -> None:
    """Remove the moved staff rows, then the columns that only made sense for one table."""
    if "role" not in _columns(connection, "users"):
        _report(steps, "users has no role column; nothing to delete")
        return

    deleted = connection.execute(
        text("DELETE FROM users WHERE role = ANY(:roles)"),
        {"roles": list(ROLE_TO_ACCOUNT_TABLE)},
    )
    _report(steps, f"deleted {deleted.rowcount} staff row(s) from users")

    connection.execute(text("ALTER TABLE users DROP CONSTRAINT IF EXISTS users_role_valid"))
    connection.execute(text("ALTER TABLE users DROP COLUMN IF EXISTS role"))
    connection.execute(text("ALTER TABLE users DROP COLUMN IF EXISTS reported_count"))
    _report(steps, "dropped users.role, users.reported_count and users_role_valid")


def _check_cross_table_regnos(connection, steps: list[str]) -> None:
    """Report a regno that exists in more than one account table.

    Informational only, and deliberately not a failure. Sign-in is category scoped -- each
    login page looks the regno up in the one table it is for -- and a session is then pinned
    to that row by primary key, so a regno shared by a participant and a moderator resolves
    correctly on both pages. Such a regno is a supported state.

    It is still worth printing, because it is the kind of thing that surprises a human reading
    a moderation queue ("two people, same regno?") and because it is the shape of data that
    any future code that searches tables by regno would mishandle.
    """
    tables = ["users", *ROLE_TO_ACCOUNT_TABLE.values()]
    present = [table for table in tables if _table_exists(connection, table)]
    for index, first in enumerate(present):
        for second in present[index + 1:]:
            rows = connection.execute(
                text(
                    f'SELECT a.regno FROM "{first}" a JOIN "{second}" b ON b.regno = a.regno '
                    "ORDER BY a.regno"
                )
            ).all()
            for row in rows:
                _report(
                    steps,
                    f"NOTE: regno {row[0]!r} exists in both {first} and {second}; "
                    "each sign-in page resolves its own account",
                )


# ---------------------------------------------------------------- driver


def migrate(connection, dry_run: bool) -> int:
    steps: list[str] = []

    problems = _preflight(connection)
    if problems:
        print("Refusing to migrate. Fix these first:")
        for problem in problems:
            print(f"  - {problem}")
        return 1

    _create_staff_tables(connection, steps)
    _drop_participant_fields_from_staff(connection, steps)
    _move_staff_rows(connection, steps)
    _migrate_login_tokens(connection, steps)
    _drop_staff_from_users(connection, steps)
    _check_cross_table_regnos(connection, steps)

    # The rollback for a dry run belongs to the caller, which owns the transaction it began.
    if dry_run:
        print("\nDry run. The following would be applied, and has been rolled back:")
    else:
        print("\nApplied:")
    for step in steps:
        print(f"  {step}")
    return 0


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database-url",
        default=None,
        help="Database to migrate. Defaults to the application's own DATABASE_URL.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would change and roll back, leaving the database untouched.",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    url = args.database_url or settings.DATABASE_URL
    print(f"Migrating {url.split('@')[-1]}")

    client = PostgresClient(url)
    try:
        if args.dry_run:
            # One transaction for the whole run so the rollback is a real no-op rather than
            # a best effort: DDL in PostgreSQL is transactional, so this is exact.
            with client.engine.connect() as connection:
                transaction = connection.begin()
                try:
                    return migrate(connection, dry_run=True)
                finally:
                    transaction.rollback()
        with client.engine.begin() as connection:
            return migrate(connection, dry_run=False)
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())

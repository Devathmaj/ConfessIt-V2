"""Two workers booting at once must converge the schema, not fight over it.

Every worker runs ``PostgresClient.initialize()`` on startup, and that sequence is a series
of "read the catalogue, then ALTER" steps. Two workers doing it at the same moment on a
database that still has work to do -- a first boot, or a deploy that adds a CHECK
constraint -- both see the constraint missing and both issue ``ADD CONSTRAINT``. The loser
is refused with ``duplicate_object`` and dies during startup, so the deploy ends with half
its workers up and a schema that is neither old nor new.

The convergence is now serialised across processes by a session-scoped
``pg_advisory_lock``: the second worker waits for the first, then finds the work done and
changes nothing. Each constraint statement also tolerates the "somebody else got there
first" errors on its own, which covers a rolling deploy where the *previous* build -- which
does not take the lock -- is still converging the same database.

The tests below pin the mechanism from both ends: the lock is actually held for the whole
convergence and released afterwards even when the convergence fails, and two real processes
initialising an empty database at the same time produce the same schema as one process
doing it alone.
"""

import os
import subprocess
import sys
import threading
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.pool import NullPool

from app.db_models import Base
from app.postgres import (
    PG_DUPLICATE_OBJECT,
    PG_DUPLICATE_TABLE,
    SCHEMA_INIT_LOCK_KEY,
    PostgresClient,
)

BACKEND_ROOT = Path(__file__).resolve().parents[1]


# --- helpers ----------------------------------------------------------------------


def _lock_key_sql() -> str:
    """The advisory-lock key as PostgreSQL stores it in ``pg_locks``.

    ``pg_advisory_lock(bigint)`` splits the key across ``classid`` (high 32 bits) and
    ``objid`` (low 32 bits), with ``objsubid = 1`` to mark the single-key form.
    """
    return (
        "SELECT count(*) FROM pg_locks "
        "WHERE locktype = 'advisory' AND granted AND objsubid = 1 "
        "AND ((classid::bigint << 32) | objid::bigint) = :key "
        "AND pid <> pg_backend_pid()"
    )


def _lock_is_held(db) -> bool:
    with db.engine.connect() as connection:
        held = connection.scalar(text(_lock_key_sql()), {"key": SCHEMA_INIT_LOCK_KEY})
    return bool(held)


def _take_schema_lock(connection) -> None:
    connection.execute(
        text("SELECT pg_advisory_lock(:key)"), {"key": SCHEMA_INIT_LOCK_KEY}
    )


def _release_schema_lock(connection) -> None:
    connection.execute(
        text("SELECT pg_advisory_unlock(:key)"), {"key": SCHEMA_INIT_LOCK_KEY}
    )


def _url_string(url) -> str:
    """A URL as a connectable string.

    ``str(URL)`` deliberately masks the password, and a scratch database built from a
    masked URL fails to connect -- with an authentication error, which reads like a
    misconfigured server rather than a test helper.
    """
    return url.render_as_string(hide_password=False)


def _admin_url(database_url: str) -> str:
    """The same server, addressed through a database that always exists."""
    return _url_string(make_url(database_url).set(database="postgres"))


def _can_create_databases(database_url: str) -> bool:
    engine = create_engine(
        _admin_url(database_url), poolclass=NullPool, connect_args={"connect_timeout": 10}
    )
    try:
        with engine.connect() as connection:
            return bool(
                connection.scalar(
                    text(
                        "SELECT rolsuper OR rolcreatedb FROM pg_roles "
                        "WHERE rolname = current_user"
                    )
                )
            )
    finally:
        engine.dispose()


def _create_database(database_url: str, name: str) -> None:
    engine = create_engine(
        _admin_url(database_url),
        poolclass=NullPool,
        isolation_level="AUTOCOMMIT",
        connect_args={"connect_timeout": 10},
    )
    try:
        with engine.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{name}"'))
    finally:
        engine.dispose()


def _drop_database(database_url: str, name: str) -> None:
    engine = create_engine(
        _admin_url(database_url),
        poolclass=NullPool,
        isolation_level="AUTOCOMMIT",
        connect_args={"connect_timeout": 10},
    )
    try:
        with engine.connect() as connection:
            # Nothing should be connected, but a failed test must not leave a database
            # behind that blocks the next run.
            connection.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :name AND pid <> pg_backend_pid()"
                ),
                {"name": name},
            )
            connection.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
    finally:
        engine.dispose()


SCHEMA_FINGERPRINT_SQL = """
SELECT 'table', table_name, table_type, '', '', ''
  FROM information_schema.tables
 WHERE table_schema = 'public'
UNION ALL
SELECT 'column', table_name, column_name, data_type, is_nullable,
       coalesce(column_default, '')
  FROM information_schema.columns
 WHERE table_schema = 'public'
UNION ALL
SELECT 'constraint', conrelid::regclass::text, conname, contype::text,
       pg_get_constraintdef(oid), ''
  FROM pg_constraint
 WHERE connamespace = 'public'::regnamespace
UNION ALL
SELECT 'index', tablename, indexname, '', indexdef, ''
  FROM pg_indexes
 WHERE schemaname = 'public'
ORDER BY 1, 2, 3
"""


def _schema_fingerprint(database_url: str):
    """Everything about the schema that the convergence is responsible for.

    Compared as a set of rows rather than as a count: "both workers succeeded" is not
    enough if the second one left a column, a constraint or an index behind, and the
    loser of a ``create_all`` race is exactly the process that would.
    """
    engine = create_engine(
        database_url, poolclass=NullPool, connect_args={"connect_timeout": 10}
    )
    try:
        with engine.connect() as connection:
            return sorted(
                tuple(row) for row in connection.execute(text(SCHEMA_FINGERPRINT_SQL))
            )
    finally:
        engine.dispose()


_CHILD_SOURCE = """
import sys

from app.postgres import PostgresClient

# The same narrow pool the one-shot scripts ask for: the lock connection must not come
# out of the application's pool, or this would wait for itself until pool_timeout.
client = PostgresClient(database_url=sys.argv[1], pool_size=1, max_overflow=0)
try:
    client.initialize()
finally:
    client.close()
print("initialised")
"""


def _initialise_in_process(database_url: str) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", _CHILD_SOURCE, database_url],
        cwd=str(BACKEND_ROOT),
        env=dict(os.environ),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


# ---------------------------------------------------------------------------------
# The lock is held for the whole convergence, and released afterwards
# ---------------------------------------------------------------------------------


def test_initialising_waits_while_another_worker_holds_the_schema_lock(db):
    """The second worker waits for the first instead of converging beside it.

    This is the mechanism the whole fix rests on: while the lock is held, no second
    ``initialize()`` may get past it. Without the lock the convergence returns almost
    immediately, which is exactly how two workers end up both issuing ``ADD CONSTRAINT``.
    """
    holder = db.engine.connect()
    finished = threading.Event()

    def converge():
        try:
            db.initialize()
        finally:
            finished.set()

    try:
        _take_schema_lock(holder)
        thread = threading.Thread(target=converge, daemon=True)
        thread.start()
        assert not finished.wait(1.5), (
            "initialize() converged while another session held the schema lock"
        )
    finally:
        _release_schema_lock(holder)
        holder.close()

    assert finished.wait(120), "initialize() never finished after the lock was released"
    thread.join(timeout=1)
    assert not thread.is_alive()


def test_initialisation_releases_the_schema_lock(db):
    """A lock left held would make every later boot wait forever, with nothing in the log."""
    db.initialize()

    assert _lock_is_held(db) is False


def test_initialisation_releases_the_schema_lock_when_it_fails(db, monkeypatch):
    """The lock is released by the ``finally``, not by the happy path.

    A convergence step that raises must not leave the lock behind: the next boot -- or the
    restart the operator is about to do -- would hang on start-up instead of retrying.
    """
    def refuse_to_start() -> None:
        raise RuntimeError("convergence failed")

    monkeypatch.setattr(db, "assert_no_legacy_plaintext_chat_schema", refuse_to_start)

    with pytest.raises(RuntimeError, match="convergence failed"):
        db.initialize()

    assert _lock_is_held(db) is False


def test_a_single_connection_pool_can_still_initialise(db):
    """The lock connection comes from its own engine, not from the application's pool.

    ``scripts/recreate_chat_schema.py`` asks for ``pool_size=1, max_overflow=0``. If the
    lock were taken from that pool it would be held for the whole convergence, and every
    step would then wait for the connection it is holding -- a pool timeout on startup
    instead of a converged schema.
    """
    narrow = PostgresClient(
        database_url=_url_string(db.engine.url),
        pool_size=1,
        max_overflow=0,
        pool_timeout=1,
    )
    try:
        # The lock engine is a second engine, and it does not pool: a lock connection
        # that went back to a pool would still be holding the lock.
        assert narrow._lock_engine is not narrow.engine
        assert isinstance(narrow._lock_engine.pool, NullPool)
        assert narrow._lock_engine.url == narrow.engine.url
        narrow.initialize()
    finally:
        narrow.close()


def test_the_schema_lock_key_is_not_shared_with_another_lock():
    """An advisory lock is a number the callers agree on, so a collision would block work
    that has nothing to do with it."""
    from app import limits
    from app.accounts import ACCOUNT_CAP_LOCK_KEYS
    from app.services import couple_gameplay, matchmaking_slot_service
    from app.services import two_key_escape_gameplay

    others = set(ACCOUNT_CAP_LOCK_KEYS.values()) | {
        limits.PLOT_TWIST_SESSION_LOCK_KEY,
        limits.CASE_CLOSED_SESSION_LOCK_KEY,
        limits.AI_GENERATION_LOCK_KEY,
        couple_gameplay._COUPLE_SESSION_LOCK_KEY,
        matchmaking_slot_service._SLOT_WRITE_LOCK_KEY,
        two_key_escape_gameplay._ESCAPE_SESSION_LOCK_KEY,
    }
    assert SCHEMA_INIT_LOCK_KEY not in others


# ---------------------------------------------------------------------------------
# The steps tolerate a worker that is not taking the lock
# ---------------------------------------------------------------------------------


def test_a_constraint_added_by_another_worker_is_not_a_boot_failure(db):
    """A rolling deploy leaves the old build -- which takes no lock -- converging too.

    The ``ADD CONSTRAINT`` then loses with ``duplicate_object``. That is the desired end
    state arriving from somebody else, not an error, and the batch has to keep going.
    """
    sql = (
        'ALTER TABLE "confessions" ADD CONSTRAINT "ck_schema_lock_probe" '
        "CHECK (length(body) >= 0) NOT VALID"
    )
    try:
        with db.engine.begin() as connection:
            first = db._run_constraint_ddl(
                connection, sql, tolerate=PG_DUPLICATE_OBJECT
            )
        assert first is True

        with db.engine.begin() as connection:
            second = db._run_constraint_ddl(
                connection, sql, tolerate=PG_DUPLICATE_OBJECT
            )
        assert second is False, "a constraint that is already there is not a failure"
    finally:
        with db.engine.begin() as connection:
            connection.execute(
                text('ALTER TABLE "confessions" DROP CONSTRAINT IF EXISTS "ck_schema_lock_probe"')
            )


def test_a_constraint_statement_that_really_fails_still_raises(db):
    """Only the race is tolerated. A typo in a constraint must not be swallowed."""
    with pytest.raises(ProgrammingError):
        with db.engine.begin() as connection:
            db._run_constraint_ddl(
                connection,
                'ALTER TABLE "confessions" ADD CONSTRAINT "ck_schema_lock_probe" '
                "CHECK (no_such_column > 0) NOT VALID",
                tolerate=PG_DUPLICATE_OBJECT,
            )


def test_a_table_created_by_another_worker_is_retried(db, monkeypatch):
    """``create_all`` is a check-then-create, so on a fresh database it can lose the race.

    The retry finds the tables the other worker created and creates the rest. Without it,
    the loser dies on the first table two workers both saw as missing.
    """
    real_create_all = Base.metadata.create_all
    calls = []

    class _DuplicateTable(Exception):
        sqlstate = PG_DUPLICATE_TABLE

    def racing_create_all(bind, **kwargs):
        calls.append(bind)
        if len(calls) == 1:
            raise ProgrammingError("CREATE TABLE", {}, _DuplicateTable("duplicate table"))
        return real_create_all(bind, **kwargs)

    monkeypatch.setattr(Base.metadata, "create_all", racing_create_all)

    db._create_tables()

    assert len(calls) == 2, "the duplicate_table refusal was not retried"


def test_a_table_creation_failure_that_is_not_a_race_still_raises(db, monkeypatch):
    def broken_create_all(bind, **kwargs):
        raise ProgrammingError(
            "CREATE TABLE",
            {},
            type("_Other", (Exception,), {"sqlstate": "42601"})("syntax error"),
        )

    monkeypatch.setattr(Base.metadata, "create_all", broken_create_all)

    with pytest.raises(ProgrammingError):
        db._create_tables()


# ---------------------------------------------------------------------------------
# Two real processes, one empty database
# ---------------------------------------------------------------------------------


def test_two_processes_initialising_an_empty_database_both_succeed(db):
    """The finding itself, end to end: two workers, a fresh database, no crash.

    A single run gives the schema to compare against; the pair run against a database that
    was empty when they were started has to reach exactly the same one. The processes use
    the narrowest pool the application ever asks for, which also pins the lock connection
    outside it.
    """
    database_url = _url_string(db.engine.url)
    if not _can_create_databases(database_url):
        pytest.skip(
            "the test role cannot create databases, so a concurrent first boot cannot be "
            "staged here"
        )

    suffix = uuid.uuid4().hex[:12]
    baseline = f"confessit_f9_baseline_{suffix}"
    contested = f"confessit_f9_pair_{suffix}"
    baseline_url = _url_string(make_url(database_url).set(database=baseline))
    contested_url = _url_string(make_url(database_url).set(database=contested))

    try:
        # One process, so the result is the schema a single boot produces.
        _create_database(database_url, baseline)
        single = _initialise_in_process(baseline_url)
        single_out, single_err = single.communicate(timeout=300)
        assert single.returncode == 0, (
            f"the single-run baseline failed:\n{single_out}\n{single_err}"
        )

        # Two processes at once, against a database that is empty when they start.
        _create_database(database_url, contested)
        first = _initialise_in_process(contested_url)
        second = _initialise_in_process(contested_url)
        first_out, first_err = first.communicate(timeout=300)
        second_out, second_err = second.communicate(timeout=300)

        assert first.returncode == 0, (
            f"the first worker died during startup:\n{first_out}\n{first_err}"
        )
        assert second.returncode == 0, (
            f"the second worker died during startup:\n{second_out}\n{second_err}"
        )

        assert _schema_fingerprint(contested_url) == _schema_fingerprint(baseline_url)
    finally:
        _drop_database(database_url, contested)
        _drop_database(database_url, baseline)

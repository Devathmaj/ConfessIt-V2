"""PostgreSQL engine and SQLAlchemy session lifecycle."""
import threading
import uuid
from contextlib import contextmanager
from typing import Any

from sqlalchemy import CheckConstraint, Table, create_engine, text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from .config import settings
from .db_models import Base
from .logger import get_logger

logger = get_logger(__name__)


# The advisory lock that serialises the schema convergence across the processes that
# share this database.
#
# Every worker that boots runs ``PostgresClient.initialize()``, and the convergence
# sequence is a series of "read the catalogue, then ALTER" steps. Two workers doing that
# at the same moment on a database that still has work to do -- a first boot, or a deploy
# that adds a CHECK constraint -- both see the constraint missing and both issue
# ``ADD CONSTRAINT``; the loser is refused with ``duplicate_object`` and that worker dies
# during startup, leaving the schema half-converged and the deploy flapping.
#
# A session-scoped ``pg_advisory_lock`` settles it. The second worker waits for the first
# to finish and then finds the work already done: every step in the sequence is written to
# be a no-op against a converged database, so the second pass costs a catalogue read and
# changes nothing.
#
# The key is arbitrary but fixed, and it sits in a range of its own. An advisory lock is
# a number the callers agree on, so reusing one for an unrelated lock would make the two
# block each other for no reason: the account-cap keys are 7_240_xxx (``app.accounts``)
# and the game-session keys are 7_250_xxx (``app.limits``).
SCHEMA_INIT_LOCK_KEY = 7_260_001

# The PostgreSQL error codes the convergence sequence treats as "somebody else got there
# first" rather than as a failure. Spelled out rather than matched on the message text,
# because the message is localised and the code is not.
PG_DUPLICATE_TABLE = "42P07"        # duplicate_table
PG_DUPLICATE_OBJECT = "42710"      # duplicate_object -- a constraint or index is there
PG_UNDEFINED_OBJECT = "42704"       # undefined_object -- it is not there after all


def _postgres_error_code(exc: BaseException) -> str | None:
    """The ``SQLSTATE`` behind a database error, or None if it is not one.

    psycopg puts the code on the exception's ``sqlstate``; SQLAlchemy wraps it, and the
    wrapper keeps the original as ``orig``. Both are checked, because the error can reach
    the caller as either.
    """
    original = getattr(exc, "orig", None)
    for candidate in (original, exc):
        code = getattr(candidate, "sqlstate", None) or getattr(candidate, "pgcode", None)
        if code:
            return str(code)
    return None


def new_id() -> str:
    """Create a canonical UUID string for API models that need client-side IDs."""
    return str(uuid.uuid4())


def is_valid_id(value: Any) -> bool:
    try:
        uuid.UUID(str(value))
        return True
    except (ValueError, TypeError, AttributeError):
        return False


def get_row(session, record, row_id: Any):
    """The row named by ``row_id``, or None when the id names no row at all.

    A malformed id must never reach a typed column. It goes straight into a uuid
    column, and PostgreSQL raises ``invalid input syntax for type uuid`` on a
    value that is not a uuid -- an unhandled 500 for a request that named nothing
    at all, on a route anybody can call.

    A malformed id names no row, which is what every endpoint that takes one
    already answers "not found" for, so refusing it here changes no answer except
    turning a 500 into that one.

    This is the one place the rule is written down, so a route added later
    inherits it rather than having to remember it. Where the id reaches a column
    through a WHERE rather than a primary-key lookup, guard it with
    :func:`is_valid_id` instead: the same rule, applied before the query.
    """
    if not is_valid_id(row_id):
        return None
    return session.get(record, str(row_id))


def _server_default_sql(column, dialect) -> str | None:
    """Render a column's server default as a SQL fragment, or None if it has none.

    Needed because ``ALTER TABLE ... ADD COLUMN`` has to repeat the default inline. The
    three shapes a model can declare -- a bare string, a ``text()`` clause, and a SQL
    function such as ``func.now()`` -- render differently, and each is handled here rather
    than by string-formatting whatever ``.arg`` happens to hold.
    """
    default = column.server_default
    if default is None:
        return None
    arg = default.arg
    if isinstance(arg, str):
        escaped = arg.replace("'", "''")
        return f"'{escaped}'"
    compile_ = getattr(arg, "compile", None)
    if compile_ is not None:
        return str(compile_(dialect=dialect))
    return None


# The SQL expression that turns a row's ``regno`` into the placeholder address written by
# :meth:`PostgresClient.apply_not_null_constraints`. Named and module-level so it can be read,
# reviewed and tested as one thing rather than found inline in a long f-string.
#
# It has to be injective in ``regno``, because ``email`` carries a unique index per table and a
# collision would abort the UPDATE -- taking the application down on boot, the one outcome that
# method exists to avoid. A plain sanitise is not injective: it maps every illegal character to
# ``-``, so ``a/b`` and ``a?b`` both become ``a-b``, and truncating to 64 characters collides on
# anything longer.
#
# So a regno that is already a legal local part is used verbatim -- which keeps the ordinary case
# readable, ``22CS0455@unassigned.invalid`` -- and anything that sanitising or truncating would
# alter gets a hash of the *original* regno appended. Distinct regnos therefore cannot produce the
# same address. 47 + 1 + 16 is the 64-character local-part ceiling, and 16 hex digits of md5 is
# 64 bits, which is not a birthday-bound risk at any plausible row count.
PLACEHOLDER_EMAIL_SQL = (
    "CASE WHEN regno ~ '^[A-Za-z0-9._%+-]{1,64}$' "
    "THEN regno || '@unassigned.invalid' "
    "ELSE left(regexp_replace(regno, '[^A-Za-z0-9._%+-]', '-', 'g'), 47) "
    "|| '+' || substr(md5(regno), 1, 16) "
    "|| '@unassigned.invalid' END"
)


# Columns the models no longer declare, on the tables that used to carry them.
#
# The counterpart to :meth:`PostgresClient.apply_missing_columns`, and far more restricted on
# purpose. Adding a column is always safe -- the model declares it, so every write that reaches
# the table knows about it -- while removing one is a decision about data, which is why this is
# a named list rather than "anything the models do not mention": a column created by a script or
# an operator is not the application's to delete.
#
# The entries below are two groups of one kind: columns that were once on every account table
# and are now participant-only, and a column the model dropped altogether. ``gender`` is the
# load-bearing one of the first group: it is NOT NULL with no server default, so a deployed
# ``admin`` table that kept it would reject every ORM insert the application makes into it,
# because the model no longer writes the column. Dropping ``gender`` also drops
# ``ck_accounts_gender`` with it -- PostgreSQL removes any CHECK constraint involving a dropped
# column -- which is the whole reason the staff side of that constraint needs no entry here.
# ``emoji`` is the second group on its own: the profile-emoji option was removed entirely, so
# the ``users`` column that held it has no reader and no writer left.
RETIRED_COLUMNS: dict[str, tuple[str, ...]] = {
    "admin": ("gender", "profile_picture_public_id", "profile_picture_format"),
    "moderator": ("gender", "profile_picture_public_id", "profile_picture_format"),
    "users": ("emoji",),
    # Conversation previews used to be stored as plaintext. They are now an
    # encrypted copy of the newest visible message (see ConversationRecord), so
    # the old text column is both unreadable by the model and a second durable
    # copy of chat content the encryption change exists to remove.
    "conversations": ("last_message_preview",),
    # The failed-move counter Two-Key Escape kept briefly: scoring counts
    # start-to-finish elapsed time only, so the column has no reader left.
    # The consumed-charges list it kept even more briefly: keys are
    # reusable, so per-key consumption was retired with it. The movement
    # gate went the same way: keys never required a Navigator move between
    # uses, so its two counters have no reader left either. The display
    # score went last of all: no other game carries a score.
    "two_key_escape_sessions": (
        "failed_moves",
        "used_key_indexes",
        "moves_since_last_key",
        "has_used_key",
        "score",
    ),
}

# Columns whose type widened after deployment, which ``create_all`` cannot apply:
# it only creates tables that do not exist. Each entry maps (table, column) to the
# SQL type the models now declare. Widening is always safe to apply (every stored
# value still fits), and narrowing never appears here -- that would need a decision
# about values that no longer fit, not a boot step.
WIDENED_COLUMNS: dict[tuple[str, str], str] = {
    # Mismatched-network redemptions increment this on every refusal while SQL bounds
    # it at 2^31-1; a 16-bit column would overflow into a 500 under sustained probing.
    ("login_tokens", "attempt_count"): "integer",
}

# Free-text columns the staff queues and the notification inbox search with
# ILIKE '%...%' substring matches (see utils.ilike_contains). A leading wildcard
# defeats every btree index, so without these each keystroke is a sequential scan
# over the whole table; a trigram GIN index is what makes '%substring%' plannable.
# Declared here rather than as Index() in db_models because the index method needs
# the pg_trgm extension, which plain model metadata cannot install -- and a model
# declaring an index the database cannot build would fail every boot instead of
# degrading to a scan. Entries are (table, column); names derive as ix_trgm_<table>_<column>.
TRGM_INDEX_COLUMNS: tuple[tuple[str, str], ...] = (
    # ``messages.body`` is gone: the body is ciphertext now, so a trigram index
    # could not answer a text search and the report queue no longer searches it.
    ("message_reports", "reason"),
    ("message_reports", "reporter_regno"),
    ("message_reports", "reported_user_regno"),
    ("confessions", "body"),
    ("confessions", "recipient_name"),
    ("confession_comments", "body"),
    ("reports", "reason"),
    ("reports", "reported_by_name"),
    ("love_notes", "moderation_body"),
    ("love_note_reports", "reason"),
    ("love_note_reports", "reporter_regno"),
    ("notifications", "heading"),
    ("notifications", "body"),
    ("staff_notifications", "heading"),
    ("staff_notifications", "body"),
    ("users", "name"),
    ("users", "regno"),
    ("users", "email"),
    ("users", "department"),
    ("admin", "name"),
    ("admin", "regno"),
    ("admin", "email"),
    ("moderator", "name"),
    ("moderator", "regno"),
    ("moderator", "email"),
)


class PostgresClient:
    """Own the PostgreSQL engine and provide SQLAlchemy sessions.

    A client owns exactly one connection pool, so a process must create at most one of
    them and share it. Use :func:`get_postgres_client` for application code; the
    constructor stays public for one-shot scripts and tests that need an isolated engine.
    """

    def __init__(
        self,
        database_url: str | None = None,
        *,
        pool_size: int | None = None,
        max_overflow: int | None = None,
        pool_timeout: int | None = None,
        pool_recycle: int | None = None,
    ):
        # Fail-closed at construction: a one-shot script or test that asks for a
        # pool the server cannot hold fails here, not under load.
        _size = settings.DATABASE_POOL_SIZE if pool_size is None else pool_size
        _overflow = settings.DATABASE_MAX_OVERFLOW if max_overflow is None else max_overflow
        _budget = (_size + _overflow) * max(settings.WEB_WORKERS, 1)
        if _budget > 100:
            raise RuntimeError(
                f"Postgres connection budget is {_budget} "
                f"({_size} pool + {_overflow} overflow "
                f"x {settings.WEB_WORKERS} workers), which exceeds the default max_connections "
                "of 100. Lower DATABASE_POOL_SIZE/DATABASE_MAX_OVERFLOW, or raise the server limit."
            )
        # create_engine is lazy: it opens no sockets until a connection is first checked
        # out, so a single shared instance costs nothing until the app actually serves.
        #
        # The session timezone is pinned to UTC rather than inherited from whatever the
        # database image or cluster happens to default to. Every timestamp column is
        # TIMESTAMPTZ, so a stored instant is always correct, but a non-UTC session changes
        # how a value is *read back* -- aware, so the instant survives, yet the text in logs
        # and the values an operator sees in psql would stop matching what the app wrote.
        # The application is UTC end to end, so the session is told so.
        #
        # Kept on the instance because :meth:`schema_lock` connects the same way from an
        # engine of its own.
        self._connect_args = {"options": "-c timezone=UTC", "connect_timeout": 10}
        self.engine = create_engine(
            database_url or settings.DATABASE_URL,
            pool_pre_ping=True,
            pool_size=pool_size if pool_size is not None else settings.DATABASE_POOL_SIZE,
            max_overflow=max_overflow if max_overflow is not None else settings.DATABASE_MAX_OVERFLOW,
            pool_timeout=pool_timeout if pool_timeout is not None else settings.DATABASE_POOL_TIMEOUT,
            pool_recycle=pool_recycle if pool_recycle is not None else settings.DATABASE_POOL_RECYCLE,
            # Bound the TCP/TLS handshake itself: without this a dead endpoint
            # stalls each new connection on the OS retransmit schedule (~130 s on
            # Windows) instead of failing fast like an exhausted pool does. Seen
            # when several connections race to a host that resolves to both ::1
            # and 127.0.0.1 while only IPv4 is listening.
            connect_args=self._connect_args,
        )
        self.sessions = sessionmaker(self.engine, expire_on_commit=False, class_=Session)

        # The engine the schema-initialisation lock connection comes from. It is built
        # here rather than inside :meth:`schema_lock` so that initialising a running
        # process never builds an engine, and it uses a ``NullPool`` so that the lock
        # connection is never returned to a pool while it is still holding the lock.
        self._lock_engine = create_engine(
            database_url or settings.DATABASE_URL,
            poolclass=NullPool,
            connect_args=self._connect_args,
        )

    def initialize(self) -> None:
        """Bring the database schema in line with the models, and keep it that way.

        Serialised across every process that shares this database: the convergence
        sequence is a series of "read the catalogue, then ALTER" steps, and two of them
        running at once is how a deploy ends with half the workers up. See
        :data:`SCHEMA_INIT_LOCK_KEY` and :meth:`schema_lock`.
        """
        with self.schema_lock():
            self._converge_schema()

    @contextmanager
    def schema_lock(self):
        """Hold the schema-initialisation advisory lock for the duration of the block.

        A **session-scoped** ``pg_advisory_lock``, taken on one connection of its own and
        released explicitly before that connection is closed. The lock is session-scoped
        because it has to outlive the individual transactions the convergence runs in, and
        it is released by hand because a session-scoped lock outlives those transactions
        too: a connection that was discarded without unlocking would hold it until the
        backend died, and the symptom of that -- a deploy that hangs on start-up with
        nothing in the log -- is far harder to read than the one this prevents.

        The connection comes from a small engine of its own (:attr:`_lock_engine`), not from
        the application's pool. It is checked out for the whole convergence, and the
        convergence steps each check out a connection of their own, so taking this one from
        the pool would deadlock any deployment whose pool is a single connection wide --
        which is exactly what ``scripts/recreate_chat_schema.py`` asks for (``pool_size=1,
        max_overflow=0``). The ``NullPool`` keeps the lock connection outside that
        accounting and closes it, rather than returning it, when the block ends.

        The lock is *blocking* on purpose. The second worker waits for the first to finish
        and then finds the work already done, which is what "converged" means; refusing
        the lock instead would only move the crash to the next boot.

        The implicit transaction the lock statement opens is committed straight away, so
        the connection sits idle rather than "idle in transaction". A session-scoped lock
        survives the commit, but an open snapshot would hold the database's vacuum back
        for the whole of the convergence.
        """
        connection = self._lock_engine.connect()
        try:
            connection.execute(
                text("SELECT pg_advisory_lock(:key)"), {"key": SCHEMA_INIT_LOCK_KEY}
            )
            connection.commit()
            try:
                yield
            finally:
                try:
                    connection.execute(
                        text("SELECT pg_advisory_unlock(:key)"),
                        {"key": SCHEMA_INIT_LOCK_KEY},
                    )
                    connection.commit()
                except Exception:
                    # The lock belongs to the session, so it dies with the session. Closing
                    # the backend is what releases it, so the connection must not be
                    # returned anywhere it could be reused while still holding it.
                    logger.error(
                        "Could not release the schema-initialisation advisory lock; "
                        "closing the connection so the lock dies with its session"
                    )
        finally:
            connection.close()

    def _converge_schema(self) -> None:
        """The convergence sequence itself. Serialised by :meth:`schema_lock`."""
        # Refuse to boot against the old plaintext chat tables. The encrypted
        # schema changes NOT NULL columns and drops the body column, which
        # create_all cannot apply to an existing table. This repository has no
        # Alembic (schema converges through this method); the migration is the
        # one-shot, explicitly destructive script in scripts/recreate_chat_schema.py
        # because the old rows are disposable demo data and a data-preserving
        # backfill would be far more code than the schema is worth.
        self.assert_no_legacy_plaintext_chat_schema()
        # pgvector must exist before any table using vector(1536) is created.
        # Without this, create_all fails on a fresh database with "type vector
        # does not exist". Requires the pgvector extension package on the
        # server (pgvector/pgvector:pg16 image); see docker-compose.yml.
        self.apply_vector_extension()
        self._create_tables()
        # Columns before constraints: a CHECK added to an already-created table can name a
        # column that only exists in the models, and PostgreSQL refuses the constraint if the
        # column is not there yet. Adding the columns first is what lets a partially-migrated
        # database reach a consistent state in one boot.
        self.apply_missing_columns()
        self.apply_retired_columns()
        self.apply_unique_indexes()
        # The refresh path's stale-copy lookup reads this column on every refusal.
        self.apply_refresh_replay_index()
        self.apply_check_constraints()
        # Needs the extension installed first, and nothing else depends on it, so it
        # runs with the other additive DDL rather than in table-creation order.
        self.apply_trgm_indexes()
        # HNSW cosine index for confession embeddings where the server supports
        # it; exact search remains correct without it on a small corpus.
        self.apply_embedding_vector_index()
        # NOT NULL last, and for the same ordering reason: it needs the columns to exist, and
        # unlike the others it may have to *rewrite rows* before it can be applied.
        self.apply_not_null_constraints()
        # After the columns exist and their nullability is settled: a type change needs
        # the column present, and widening never invalidates rows.
        self.apply_widened_columns()
        # After the columns exist, so it can name session_expires_at.
        self.backfill_session_deadlines()
        # After every additive step, so the session lifecycle converges on old databases.
        self.apply_game_session_convergence()
        # Trivia Drop's single-active-question rule, for the same reason: the
        # table is new, but the index is the backstop behind the advisory lock.
        self.apply_trivia_drop_convergence()
        # Automated confession moderation (Batch 1): system-actor reports reuse
        # the reports table with a nullable reporter and a source column.
        self.apply_report_source_convergence()

    def _create_tables(self) -> None:
        """Create the tables the models declare.

        ``create_all`` is a check-then-create: it asks the catalogue whether a table is
        there and creates it if not, so two creators on a fresh database both see it
        missing and both create it, and the loser is refused with ``duplicate_table``.
        The schema lock keeps the workers of this build apart; this is for the one that is
        not taking it -- a rolling deploy, where the previous build is still up while
        the new one boots. The retry finds the tables the other worker created and
        creates the rest.
        """
        try:
            Base.metadata.create_all(self.engine)
        except ProgrammingError as exc:
            if _postgres_error_code(exc) != PG_DUPLICATE_TABLE:
                raise
            logger.info(
                "A concurrent worker created a table first; creating the remaining tables"
            )
            Base.metadata.create_all(self.engine)

    def assert_no_legacy_plaintext_chat_schema(self) -> None:
        """Fail clearly when an old plaintext chat schema is still installed.

        ``create_all`` only creates missing tables, and ``apply_missing_columns``
        cannot turn the old NOT NULL ``body`` text column into the encrypted
        columns without inventing data. Rather than silently starting with a
        table the models cannot write to, point the operator at the one-shot
        reset. The detection is narrow: only these two chat tables are checked,
        so a clean database or one already on the encrypted schema boots
        normally.
        """
        with self.engine.connect() as connection:
            legacy = connection.execute(
                text(
                    "SELECT table_name FROM information_schema.columns "
                    "WHERE table_schema = 'public' "
                    "AND column_name = 'body' "
                    "AND table_name IN ('messages', 'case_closed_messages')"
                )
            ).all()
        if legacy:
            names = ", ".join(sorted({row[0] for row in legacy}))
            raise RuntimeError(
                f"Legacy plaintext chat schema detected ({names}.body). "
                "This build stores AES-256-GCM ciphertext instead. For a disposable "
                "development/demo database, run "
                "`python scripts/recreate_chat_schema.py --yes` from the backend "
                "directory to drop only the chat/report tables and recreate them. "
                "Do not run that against a database whose chat data must be preserved."
            )

    def apply_missing_columns(self) -> None:
        """Add columns declared in the models but missing from an already-created table.

        ``create_all`` is ``CREATE TABLE IF NOT EXISTS``: on a database that already has
        ``users``, it does nothing at all, so a column added to a model is never applied to
        a deployed database. The account split depends on this -- ``created_at`` and
        ``updated_at`` are new on all three account tables, and without them an existing
        deployment would fail every insert with a column that the model writes and the table
        does not have.

        Each column is added on its own with ``IF NOT EXISTS``, so a partially-migrated
        database converges rather than aborting the boot. What this deliberately does *not*
        do is drop anything: a column the models no longer declare is left in place, because
        removing one is a decision about data rather than about schema shape. Those live in
        ``RETIRED_COLUMNS`` and are removed by :meth:`apply_retired_columns` -- deliberately a
        named list rather than "everything the models stopped mentioning", so a column written
        by a script or an operator is never the application's to delete. The one-off removal of
        the retired ``users.role`` and ``users.reported_count`` columns is in
        ``scripts/migrate_account_tables.py``, which has to run before the boot path can even
        see those tables in their new shape.
        """
        with self.engine.begin() as connection:
            for table in Base.metadata.sorted_tables:
                existing = {
                    row[0]
                    for row in connection.execute(
                        text(
                            "SELECT column_name FROM information_schema.columns "
                            "WHERE table_schema = 'public' AND table_name = :name"
                        ),
                        {"name": table.name},
                    )
                }
                if not existing:
                    # Table itself is absent; create_all already created it in full.
                    continue
                for column in table.columns:
                    if column.name in existing:
                        continue
                    ddl = column.type.compile(dialect=connection.dialect)
                    extra = ""
                    default_sql = _server_default_sql(column, connection.dialect)
                    if not column.nullable and default_sql is None:
                        # A NOT NULL column with no default cannot be added to a table that
                        # has rows. Refuse to guess a value: skipping it leaves a clear
                        # error on first write, where inventing one would fabricate data.
                        logger.warning(
                            "Not adding NOT NULL column %s.%s: it has no server default",
                            table.name,
                            column.name,
                        )
                        continue
                    if default_sql is not None:
                        extra = f" DEFAULT {default_sql}"
                    connection.execute(
                        text(
                            f'ALTER TABLE "{table.name}" ADD COLUMN IF NOT EXISTS '
                            f'"{column.name}" {ddl}{extra}'
                        )
                    )
                    logger.info(
                        "Added column %s.%s", table.name, column.name
                    )

    def apply_retired_columns(self) -> None:
        """Drop the columns in ``RETIRED_COLUMNS`` from tables that still have them.

        Run on every boot and idempotent: each column is looked up first, so an already
        converged database costs one catalogue query per table and changes nothing.

        Dropping the column is the whole migration in each case. PostgreSQL removes any
        CHECK constraint involving a dropped column, so ``ck_accounts_gender`` leaves ``admin``
        and ``moderator`` with the ``gender`` they were holding, while ``users`` -- which keeps
        the column -- keeps the constraint. Nothing reads the dropped values: the picture columns
        were always NULL on staff (the upload route is participant-only), gender was only ever
        read by matchmaking, which queries ``users``, and ``emoji`` had every reader deleted
        with the profile-emoji option itself.

        The values are logged as dropped rather than silently discarded, because this is the one
        step in :meth:`initialize` that destroys stored data. It cannot be made conditional on
        the data being empty -- a non-null ``gender`` on a staff row, or a profile emoji anybody
        chose, is exactly the state these changes declare invalid -- but an operator watching the
        boot log should be able to see that it happened.
        """
        with self.engine.begin() as connection:
            for table_name, columns in RETIRED_COLUMNS.items():
                present = {
                    row[0]
                    for row in connection.execute(
                        text(
                            "SELECT column_name FROM information_schema.columns "
                            "WHERE table_schema = 'public' AND table_name = :name"
                        ),
                        {"name": table_name},
                    )
                }
                if not present:
                    continue
                for column in columns:
                    if column not in present:
                        continue
                    connection.execute(
                        text(
                            f'ALTER TABLE "{table_name}" DROP COLUMN "{column}"'
                        )
                    )
                    logger.info(
                        "Dropped retired column %s.%s", table_name, column
                    )

    def apply_unique_indexes(self) -> None:
        """Add unique indexes declared in the models but missing from the database.

        The same gap as :meth:`apply_missing_columns`, one level up: ``create_all`` does
        nothing to a table that already exists, so a ``unique=True`` added to a column on a
        deployed database is never applied and the constraint the models claim is enforced
        simply is not. That is not hypothetical here -- ``email`` is declared unique on all
        three account tables, and ``admin`` and ``moderator`` predate that, so without this
        two staff rows could claim one address while the model says they cannot.

        Added per index rather than as a table constraint so the failure mode is a named
        index PostgreSQL will refuse to create over existing duplicates, instead of a
        constraint that reports the same conflict less legibly. Existing rows are *not*
        deduplicated first: picking a winner among two rows claiming one address is a
        decision about data, and this is the wrong place to make it silently.
        """
        with self.engine.begin() as connection:
            installed = {
                name
                for (name,) in connection.execute(
                    text(
                        "SELECT indexname FROM pg_indexes "
                        "WHERE schemaname = 'public'"
                    )
                )
            }
            for table in Base.metadata.sorted_tables:
                for column in table.columns:
                    if not column.unique:
                        continue
                    index_name = f"ix_{table.name}_{column.name}"
                    if index_name in installed:
                        continue
                    connection.execute(
                        text(
                            f'CREATE UNIQUE INDEX IF NOT EXISTS "{index_name}" '
                            f'ON "{table.name}" ("{column.name}")'
                        )
                    )
                    logger.info(
                        "Added unique index %s on %s.%s",
                        index_name,
                        table.name,
                        column.name,
                    )

    def apply_refresh_replay_index(self) -> None:
        """Index the hashes of refresh tokens that were rotated away.

        A refused refresh looks the presented token up by the hash it replaced, to
        tell a stale copy -- the browser that lost a rotation race -- from a value
        no session ever held. Without an index that lookup is a sequential scan of
        the session table, and it sits on the refusal path: exactly the path a
        caller probing for live tokens produces.

        Partial, because only a session that has rotated at least once has a value
        there: the index then holds one row per rotating session rather than one
        per session. ``IF NOT EXISTS`` and outside any data decision, so it is safe
        to retry on every boot.
        """
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    'CREATE INDEX IF NOT EXISTS "ix_login_tokens_refresh_token_replaced_hash" '
                    'ON "login_tokens" ("refresh_token_replaced_hash") '
                    "WHERE refresh_token_replaced_hash IS NOT NULL"
                )
            )

    def apply_trgm_indexes(self) -> None:
        """Install pg_trgm GIN indexes for the ILIKE-searched columns.

        ``create_all`` cannot do this one: the index method needs the pg_trgm
        extension, and model metadata has nowhere to put ``CREATE EXTENSION``.
        Installing the extension needs superuser (or rds_superuser); the bundled
        Postgres image grants it to the app role, but a managed database may not.
        So a missing privilege degrades to a warning and sequential scans -- search
        stays correct, only slower -- rather than refusing to boot. Each index is
        ``IF NOT EXISTS`` and the whole step runs outside any data decision, so it
        is safe to retry on every boot.
        """
        try:
            with self.engine.begin() as connection:
                connection.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
                installed = connection.scalar(
                    text("SELECT count(*) FROM pg_extension WHERE extname = 'pg_trgm'")
                )
                if not installed:
                    logger.warning(
                        "pg_trgm extension unavailable; ILIKE searches will use "
                        "sequential scans until it is installed"
                    )
                    return
                for table, column in TRGM_INDEX_COLUMNS:
                    index_name = f"ix_trgm_{table}_{column}"
                    connection.execute(
                        text(
                            f'CREATE INDEX IF NOT EXISTS "{index_name}" '
                            f'ON "{table}" USING gin ("{column}" gin_trgm_ops)'
                        )
                    )
        except Exception as e:
            logger.warning("Skipping trigram indexes: %s", e)

    def apply_vector_extension(self) -> bool:
        """Install the pgvector extension so vector(1536) columns can exist.

        Installing the extension needs superuser (or rds_superuser); the bundled
        pgvector image grants it to the app role, but a managed database may not.
        Returns True when the extension is installed. A missing extension is a
        warning here rather than a boot refusal so the failure surfaces where it
        belongs (table creation / embedding writes); the README and compose files
        document the pgvector/pgvector:pg16 image requirement.
        """
        try:
            with self.engine.begin() as connection:
                connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
                installed = connection.scalar(
                    text("SELECT count(*) FROM pg_extension WHERE extname = 'vector'")
                )
                if not installed:
                    logger.warning(
                        "pgvector extension unavailable; confession embeddings "
                        "require the pgvector extension (see docker-compose.yml)"
                    )
                    return False
                return True
        except Exception as e:
            logger.warning("Skipping pgvector extension: %s", e)
            return False

    def apply_embedding_vector_index(self) -> None:
        """Best-effort HNSW cosine index on confession_embeddings.embedding.

        Exact nearest-neighbor search is correct without it on a small corpus;
        the index only speeds retrieval as the corpus grows. Visibility filters
        (removed_at IS NULL) still apply in the query itself, so approximate
        recall is filtered after ranking rather than assumed. Unsupported
        servers (old pgvector, missing extension) degrade to exact search with
        a warning instead of refusing to boot.
        """
        try:
            with self.engine.begin() as connection:
                installed = connection.scalar(
                    text("SELECT count(*) FROM pg_extension WHERE extname = 'vector'")
                )
                if not installed:
                    return
                # Table may not exist yet on a database where create_all could not
                # create it (extension installed late); skip quietly in that case.
                exists = connection.scalar(
                    text(
                        "SELECT count(*) FROM information_schema.tables "
                        "WHERE table_schema = 'public' AND table_name = 'confession_embeddings'"
                    )
                )
                if not exists:
                    return
                connection.execute(
                    text(
                        'CREATE INDEX IF NOT EXISTS "ix_confession_embeddings_embedding_hnsw" '
                        'ON "confession_embeddings" USING hnsw ("embedding" vector_cosine_ops)'
                    )
                )
        except Exception as e:
            logger.warning("Skipping embedding HNSW index (exact search remains): %s", e)

    def apply_check_constraints(self) -> None:
        """Bring the database's CHECK constraints in line with the models.

        ``create_all`` only creates tables that do not exist, so on an already-deployed
        database a constraint added to a model is silently never applied. Walking the
        metadata here and adding whatever is absent keeps the running schema in step with the
        models without needing a migration framework or hand-written DDL that can drift
        from them.

        Constraints are added ``NOT VALID``: PostgreSQL then enforces them on every new or
        updated row while leaving already-stored rows alone. That matters for a limit added
        to a column that was previously unbounded, where existing rows may predate the
        limit. Validating them is a separate, deliberate step so that tightening a limit
        cannot take the app down on boot, and so the decision about what to do with
        over-length history stays explicit rather than silent.

        Superseded constraints are dropped, which is the other half of the job. Adding only
        means a limit that changes value leaves its predecessor behind holding the same
        column to the old number. That is worse than looking untidy: the two constraints are
        independent, so a direct write that skips the model has to satisfy both, and the
        *looser* ceiling stops being a limit on anything. Dropping the old name is what makes
        the new number the only rule in force. Dropping is safe to do at boot precisely
        because the replacement is added in the same transaction -- there is no window in
        which the column is unguarded.

        Two workers converging the same database at once used to be able to collide here:
        both read the catalogue, both saw a constraint missing, and the loser was refused
        with ``duplicate_object`` and died during start-up. The schema lock keeps this
        build's workers apart; each statement also tolerates that error and its
        ``undefined_object`` counterpart, which covers a rolling deploy where the previous
        build is still converging the same database without taking the lock.
        """
        with self.engine.begin() as connection:
            installed = {
                (name, table_name)
                for name, table_name in connection.execute(
                    text(
                        "SELECT conname, conrelid::regclass::text FROM pg_constraint "
                        "WHERE contype = 'c'"
                    )
                )
            }
            for table in Base.metadata.sorted_tables:
                for constraint in table.constraints:
                    if not isinstance(constraint, CheckConstraint):
                        continue
                    if (constraint.name, table.name) in installed:
                        continue
                    sqltext = str(constraint.sqltext).strip()
                    if not sqltext:
                        continue
                    added = self._run_constraint_ddl(
                        connection,
                        f'ALTER TABLE "{table.name}" ADD CONSTRAINT "{constraint.name}" '
                        f"CHECK ({sqltext}) NOT VALID",
                        tolerate=PG_DUPLICATE_OBJECT,
                    )
                    if added:
                        logger.info(
                            "Added CHECK constraint %s on %s (not validated)",
                            constraint.name,
                            table.name,
                        )
                    else:
                        logger.info(
                            "CHECK constraint %s on %s was added by another worker first",
                            constraint.name,
                            table.name,
                        )
                for superseded in self._superseded_constraints(table, installed):
                    dropped = self._run_constraint_ddl(
                        connection,
                        f'ALTER TABLE "{table.name}" DROP CONSTRAINT "{superseded}"',
                        tolerate=PG_UNDEFINED_OBJECT,
                    )
                    if dropped:
                        logger.info(
                            "Dropped superseded CHECK constraint %s on %s",
                            superseded,
                            table.name,
                        )
                    else:
                        logger.info(
                            "Superseded CHECK constraint %s on %s was already dropped",
                            superseded,
                            table.name,
                        )

    @staticmethod
    def _run_constraint_ddl(connection, sql: str, *, tolerate: str) -> bool:
        """Run one constraint DDL statement, tolerating a concurrent worker.

        Returns whether this process was the one that applied the statement. The
        ``tolerate`` code is the ``SQLSTATE`` that means somebody else got there first --
        ``duplicate_object`` for an ``ADD`` and ``undefined_object`` for a ``DROP``.

        The schema lock (:meth:`schema_lock`) already keeps the workers of this build
        apart, so this is for the one that is not taking it: a rolling deploy, where the
        previous build is still up and converging the same database while this one boots.

        Each statement gets its own SAVEPOINT, and that is the load-bearing part. A DDL
        error aborts the whole PostgreSQL transaction, so without one the tolerated
        failure would take the rest of the batch down with it and the boot would fail on
        a line that had nothing to do with the race. Rolling back to the savepoint leaves
        the outer transaction usable and the batch running.
        """
        try:
            with connection.begin_nested():
                connection.execute(text(sql))
        except ProgrammingError as exc:
            if _postgres_error_code(exc) != tolerate:
                raise
            return False
        return True

    def apply_not_null_constraints(self) -> None:
        """Tighten a column the models declare NOT NULL but the table still allows to be null.

        The same gap as :meth:`apply_check_constraints`, and the one thing it cannot do on its
        own. ``create_all`` skips a table that exists, and the ``NOT NULL`` in ``db_models``
        is a fact about a *newly created* table; on a database that predates it the column
        stays nullable forever while the models claim otherwise. Nothing warns about that gap,
        because a CHECK constraint is satisfied by NULL and so cannot express "and not null"
        without the awkward ``col IS NOT NULL`` arm -- which would be wrong anyway: it would
        leave already-stored NULLs in place and only block new ones.

        So the two halves are done together, per column, in the order that makes both succeed:

        1. **Backfill.** Every row where the column is NULL is given a placeholder derived
           from the row's own ``regno``. Nothing is fabricated that looks like real data --
           ``.invalid`` is a reserved TLD (RFC 2606) that can never resolve, so a placeholder
           is visibly a placeholder rather than quietly becoming somebody's real inbox -- and
           the address is unique per table, so the ``email`` unique index still holds. The
           count is logged, because a row that had to be invented an address is something an
           operator should go and fix.
        2. **Set the constraint.** Only once no NULL remains.

        The alternative to step 1 is refusing to boot and naming the offending rows, which
        keeps every byte of real data and costs a deploy until somebody has done it by hand.
        That was the other option and it was not taken: the failure mode of an account with a
        placeholder address is an admin correcting one field, while the failure mode of a boot
        loop is an application that does not start at all.
        """
        # Which column on which table gets a regno-derived placeholder. Every column being
        # tightened here is on an account table, so ``regno`` is always available to derive
        # one from.
        backfills: dict[str, tuple[str, ...]] = {
            "users": ("email",),
            "admin": ("email",),
            "moderator": ("email",),
        }

        with self.engine.begin() as connection:
            for table_name, columns in backfills.items():
                present = {
                    row[0]
                    for row in connection.execute(
                        text(
                            "SELECT column_name FROM information_schema.columns "
                            "WHERE table_schema = 'public' AND table_name = :name"
                        ),
                        {"name": table_name},
                    )
                }
                if not present:
                    continue
                for column_name in columns:
                    if column_name not in present:
                        continue

                    # Is the column already NOT NULL? information_schema reports it, and
                    # asking first means the common case -- an already-migrated database --
                    # costs one catalogue lookup and no table scan.
                    already_not_null = connection.execute(
                        text(
                            "SELECT is_nullable FROM information_schema.columns "
                            "WHERE table_schema = 'public' AND table_name = :table "
                            "AND column_name = :column"
                        ),
                        {"table": table_name, "column": column_name},
                    ).scalar()
                    if already_not_null == "NO":
                        continue

                    missing = connection.execute(
                        text(
                            f'SELECT count(*) FROM "{table_name}" '
                            f'WHERE "{column_name}" IS NULL'
                        )
                    ).scalar_one()
                    if missing:
                        # See ``PLACEHOLDER_EMAIL_SQL`` for why the local part is built this way.
                        connection.execute(
                            text(
                                f'UPDATE "{table_name}" SET "{column_name}" = '
                                f"{PLACEHOLDER_EMAIL_SQL} "
                                f'WHERE "{column_name}" IS NULL'
                            )
                        )
                        logger.warning(
                            "Backfilled %s null %s values on %s with placeholder "
                            "addresses; correct them from the Profile Review screen",
                            missing, column_name, table_name,
                        )

                    connection.execute(
                        text(
                            f'ALTER TABLE "{table_name}" '
                            f'ALTER COLUMN "{column_name}" SET NOT NULL'
                        )
                    )
                    logger.info(
                        "Tightened %s.%s to NOT NULL", table_name, column_name
                    )

    def apply_widened_columns(self) -> None:
        """Widen a column whose model type outgrew the deployed one.

        The same gap as :meth:`apply_missing_columns`, one level deeper: ``create_all``
        skips tables that exist, so a column declared ``SmallInteger`` at deploy time
        stays 16-bit forever while the models claim ``Integer``. Each entry in
        :data:`WIDENED_COLUMNS` is checked against the catalogue and altered only when
        it differs, so an already-converged database costs one lookup and changes
        nothing. Only widenings are listed -- every stored value still fits, so no
        decision about data is involved.
        """
        with self.engine.begin() as connection:
            for (table_name, column_name), target in WIDENED_COLUMNS.items():
                current = connection.execute(
                    text(
                        "SELECT data_type FROM information_schema.columns "
                        "WHERE table_schema = 'public' AND table_name = :table "
                        "AND column_name = :column"
                    ),
                    {"table": table_name, "column": column_name},
                ).scalar()
                if current is None or current == target:
                    continue
                connection.execute(
                    text(
                        f'ALTER TABLE "{table_name}" '
                        f'ALTER COLUMN "{column_name}" TYPE {target}'
                    )
                )
                logger.info(
                    "Widened %s.%s from %s to %s", table_name, column_name, current, target
                )

    @staticmethod
    def _superseded_constraints(
        table: Table, installed: set[tuple[str, str]]
    ) -> list[str]:
        """Names of installed CHECK constraints on ``table`` that a model has replaced.

        A constraint counts as superseded when the models declare a constraint on the same
        table whose name is the same string with a different numeric suffix. That is what
        makes a limit encode its own value -- ``ck_confessions_body_length_500`` supersedes
        ``ck_confessions_body_length_2000`` -- and it needs no registry of old names to
        forget: tightening the constant and restarting is the whole migration.

        The unsuffixed name is superseded too, because it is the shape this convention is
        replacing. A constraint called ``ck_messages_body_length`` cannot be detected as stale
        by the numeric rule alone -- it has no numeric suffix -- so on any database that already
        has one, renaming the model to ``ck_messages_body_length_100`` would *add* the tight rule
        and leave the loose one beside it. Both would then apply to the same column, and
        PostgreSQL enforces each independently: the 2000 ceiling would keep permitting writes
        the API rejects, which is exactly the drift this method exists to prevent. Dropping it
        is safe because the replacement is added in the same transaction.

        Only a *numeric* suffix qualifies for the value-bearing rule, and only an exact
        family-key match qualifies for the unsuffixed one. Matching on arbitrary prefixes would
        be worse than not cleaning up at all, since it would eventually drop an unrelated
        constraint.
        """
        families: dict[str, set[str]] = {}
        for constraint in table.constraints:
            if not isinstance(constraint, CheckConstraint) or not constraint.name:
                continue
            prefix, _, suffix = constraint.name.rpartition("_")
            # Only a clean numeric suffix makes this a value-bearing family. "ck_a_b" has an
            # empty prefix and is skipped; "ck_abc_1" is treated as family "ck_abc".
            if prefix and suffix.isdigit():
                families.setdefault(prefix, set()).add(constraint.name)

        superseded = []
        for name, table_name in installed:
            if table_name != table.name:
                continue
            prefix, _, suffix = name.rpartition("_")
            known = families.get(prefix)
            if known and suffix.isdigit() and name not in known:
                superseded.append(name)
            elif name in families:
                # The whole installed name is a family declared by this table, e.g.
                # "ck_messages_body_length" against a declared family "ck_messages_body_length"
                # (from ``ck_messages_body_length_100``). Its own suffix is the word "length"
                # rather than a number, so the rule above cannot see it -- and leaving it in place
                # is how a 2000-character ceiling survives alongside a 100-character one.
                superseded.append(name)
        return superseded

    def backfill_session_deadlines(self) -> None:
        """Give sessions that predate the deadline column the deadline their age implies.

        ``login_tokens.session_expires_at`` is written at redemption
        (``consumed_at + LOGIN_SESSION_EXPIRE_HOURS``) and never moved, but a session
        redeemed by an earlier build has no value yet. Without this, the first request
        after deploy would read "no deadline" as "already past" and sign out every
        currently-logged-in account the moment the new build ships. Instead the deadline
        is derived once, from the login time the row already records, so a session minted
        less than a day ago keeps its remaining time and one older than that ends as it
        should.

        Idempotent by construction: it only touches rows whose deadline is still NULL,
        so re-running on later boots does nothing.
        """
        with self.engine.begin() as connection:
            result = connection.execute(
                text(
                    "UPDATE login_tokens "
                    "SET session_expires_at = consumed_at + make_interval(hours => :hours) "
                    "WHERE session_expires_at IS NULL AND consumed_at IS NOT NULL"
                ),
                {"hours": settings.LOGIN_SESSION_EXPIRE_HOURS},
            )
            if result.rowcount:
                logger.info(
                    "Backfilled session deadlines for %s login token(s)",
                    result.rowcount,
                )

    def apply_game_session_convergence(self) -> None:
        """Converge pre-refactor game tables to the session lifecycle.

        Three jobs, all idempotent, all safe to re-run on every boot:

        1. Drop the old one-row-per-day uniqueness on ``wordle_games``,
           ``anagram_games``, ``couple_games`` and ``reaction_time_games``.
           History now shares days, so the unique index/constraint would refuse
           the second session of a day that the lifecycle legitimately creates.
           (Reaction Time shipped that uniqueness briefly before joining the
           shared lifecycle; a database from those days converges here.)
        2. Backfill the new lifecycle columns on legacy rows: ``status`` defaults
           to ``'ended'`` at the column level, and ``period_start_date`` mirrors
           ``game_date`` where it is still NULL, so old rows read as history.
        3. Install one partial unique index per game table so at most one
           active-or-pending session exists per game. The index (not a CHECK, not
           application code) is what serializes a scheduler run racing an admin
           Update: the loser gets an integrity error instead of a second active.
        """
        with self.engine.begin() as connection:
            for table in (
                "wordle_games",
                "anagram_games",
                "couple_games",
                "reaction_time_games",
                "connections_games",
                "two_key_escape_games",
                "plot_twist_games",
                "case_closed_games",
            ):
                present = {
                    row[0]
                    for row in connection.execute(
                        text(
                            "SELECT column_name FROM information_schema.columns "
                            "WHERE table_schema = 'public' AND table_name = :name"
                        ),
                        {"name": table},
                    )
                }
                if not present:
                    continue
                # 1. Drop legacy uniqueness on game_date: the auto index from
                # ``unique=True`` plus the lingering key constraint name.
                connection.execute(
                    text(f'DROP INDEX IF EXISTS "ix_{table}_game_date"')
                )
                connection.execute(
                    text(
                        f'ALTER TABLE "{table}" DROP CONSTRAINT IF EXISTS '
                        f'"{table}_game_date_key"'
                    )
                )
                # 2. Backfill lifecycle columns for rows written before them.
                if "period_start_date" in present:
                    connection.execute(
                        text(
                            f'UPDATE "{table}" SET period_start_date = game_date '
                            f'WHERE period_start_date IS NULL'
                        )
                    )
                if "period_end_date" in present:
                    connection.execute(
                        text(
                            f'UPDATE "{table}" SET period_end_date = game_date + 1 '
                            f'WHERE period_end_date IS NULL'
                        )
                    )
                if "started_at" in present:
                    connection.execute(
                        text(
                            f'UPDATE "{table}" SET started_at = created_at '
                            f'WHERE started_at IS NULL'
                        )
                    )
                # 3. One active-or-pending session per game table.
                connection.execute(
                    text(
                        f'CREATE UNIQUE INDEX IF NOT EXISTS "uq_{table}_single_active" '
                        f'ON "{table}" ((1)) WHERE status IN (\'active\', \'pending\')'
                    )
                )
            # 4. A pending (manual, not yet configured) session holds no word, so
            # the answer columns must accept NULL. Adding a column is convergent
            # but relaxing an existing NOT NULL is not -- without this, every
            # pending insert fails and the scheduler mistakes the failure for a
            # race. Idempotent: dropping a constraint that is already gone is a
            # no-op.
            for table in ("wordle_games", "anagram_games"):
                present = {
                    row[0]
                    for row in connection.execute(
                        text(
                            "SELECT column_name FROM information_schema.columns "
                            "WHERE table_schema = 'public' AND table_name = :name"
                        ),
                        {"name": table},
                    )
                }
                if "answer" in present:
                    connection.execute(
                        text(f'ALTER TABLE "{table}" ALTER COLUMN "answer" DROP NOT NULL')
                    )
            logger.info("Game session lifecycle converged")

    def apply_trivia_drop_convergence(self) -> None:
        """Install Trivia Drop's at-most-one-active-question rule.

        One partial unique index on ``trivia_drop_questions``: at most one row
        may hold ``status = 'active'``. The index (not application code) is
        what serializes an admin Deploy racing a scheduled release past the
        advisory lock: the loser gets an integrity error instead of a second
        live question. Idempotent, like every other convergence step.
        """
        with self.engine.begin() as connection:
            present = {
                row[0]
                for row in connection.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema = 'public' AND table_name = :name"
                    ),
                    {"name": "trivia_drop_questions"},
                )
            }
            if not present:
                return
            connection.execute(
                text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS "
                    '"uq_trivia_drop_questions_single_active" '
                    'ON "trivia_drop_questions" ((1)) WHERE status = \'active\''
                )
            )
            logger.info("Trivia Drop session lifecycle converged")

    def apply_report_source_convergence(self) -> None:
        """Converge the reports tables for automated moderation.

        Automated confession/comment/message reports reuse the same table as
        manual ones with a system actor: ``reported_by_id`` (``reports``) or
        ``reporter_regno`` (``message_reports``) becomes nullable (NULL means
        no users row behind the report) and ``source`` discriminates
        ``"user"`` from ``"azure_content_safety"``. History backfills to
        ``"user"`` via the column default; ``apply_missing_columns`` adds the
        column itself, this step relaxes nullability and installs the
        idempotency backstops.

        Partial unique indexes allow at most one automated report per
        confession, per comment and per message while leaving manual reports
        untouched: multiple users may still report the same target, each under
        their own reporter id. Idempotent and safe to re-run on every boot.
        """
        try:
            with self.engine.begin() as connection:
                present = {
                    row[0]
                    for row in connection.execute(
                        text(
                            "SELECT column_name FROM information_schema.columns "
                            "WHERE table_schema = 'public' AND table_name = :name"
                        ),
                        {"name": "reports"},
                    )
                }
                if not present:
                    return
                # Backfill + relax: existing manual rows always carry a reporter,
                # new automated rows carry NULL. Fresh tables created from the
                # models are already nullable; this only affects databases that
                # predate the change.
                if "source" in present:
                    connection.execute(
                        text(
                            "UPDATE \"reports\" SET \"source\" = 'user' "
                            "WHERE \"source\" IS NULL"
                        )
                    )
                if "reported_by_id" in present:
                    is_nullable = connection.execute(
                        text(
                            "SELECT is_nullable FROM information_schema.columns "
                            "WHERE table_schema = 'public' AND table_name = 'reports' "
                            "AND column_name = 'reported_by_id'"
                        )
                    ).scalar()
                    if is_nullable == "NO":
                        connection.execute(
                            text(
                                "ALTER TABLE \"reports\" "
                                "ALTER COLUMN \"reported_by_id\" DROP NOT NULL"
                            )
                        )
                        logger.info(
                            "Relaxed reports.reported_by_id to NULL for system-actor reports"
                        )
                # One automated report per confession and per comment. Manual
                # reports keep their own (target, reported_by_id) uniqueness.
                connection.execute(
                    text(
                        "CREATE UNIQUE INDEX IF NOT EXISTS "
                        '"uq_reports_confession_automated" '
                        'ON "reports" ("confession_id") '
                        "WHERE source = 'azure_content_safety'"
                    )
                )
                connection.execute(
                    text(
                        "CREATE UNIQUE INDEX IF NOT EXISTS "
                        '"uq_reports_comment_automated" '
                        'ON "reports" ("comment_id") '
                        "WHERE source = 'azure_content_safety'"
                    )
                )
                # Chat messages keep their own report table; the same system-actor
                # shape is converged here so an automated message report needs no
                # receiver. Manual message reports always carry a reporter.
                message_columns = {
                    row[0]
                    for row in connection.execute(
                        text(
                            "SELECT column_name FROM information_schema.columns "
                            "WHERE table_schema = 'public' AND table_name = :name"
                        ),
                        {"name": "message_reports"},
                    )
                }
                if message_columns:
                    if "source" in message_columns:
                        connection.execute(
                            text(
                                "UPDATE \"message_reports\" SET \"source\" = 'user' "
                                "WHERE \"source\" IS NULL"
                            )
                        )
                    if "reporter_regno" in message_columns:
                        is_nullable = connection.execute(
                            text(
                                "SELECT is_nullable FROM information_schema.columns "
                                "WHERE table_schema = 'public' AND table_name = 'message_reports' "
                                "AND column_name = 'reporter_regno'"
                            )
                        ).scalar()
                        if is_nullable == "NO":
                            connection.execute(
                                text(
                                    "ALTER TABLE \"message_reports\" "
                                    "ALTER COLUMN \"reporter_regno\" DROP NOT NULL"
                                )
                            )
                            logger.info(
                                "Relaxed message_reports.reporter_regno to NULL for system-actor reports"
                            )
                    connection.execute(
                        text(
                            "CREATE UNIQUE INDEX IF NOT EXISTS "
                            '"uq_message_reports_automated" '
                            'ON "message_reports" ("message_id") '
                            "WHERE source = 'azure_content_safety'"
                        )
                    )
                logger.info("Report source lifecycle converged")
        except Exception as e:
            logger.warning("Skipping report source convergence: %s", e)

    def close(self) -> None:
        self.engine.dispose()
        self._lock_engine.dispose()


# Existing service annotations use this name; it now denotes only the engine/session provider.
Database = PostgresClient

_client: PostgresClient | None = None
_client_lock = threading.Lock()


def validate_postgres_budget(
    pool_size: int | None = None, max_overflow: int | None = None
) -> int:
    """Validate the worst-case connection budget before any engine is built.

    Fail-closed at startup rather than under load: Postgres ships with
    max_connections=100, so a budget above that is a hard outage, not tuning.
    Returns the computed budget for callers that need it.
    """
    size = settings.DATABASE_POOL_SIZE if pool_size is None else pool_size
    overflow = settings.DATABASE_MAX_OVERFLOW if max_overflow is None else max_overflow
    max_conns = (size + overflow) * max(settings.WEB_WORKERS, 1)
    if max_conns > 100:
        # Postgres ships with max_connections=100, so this is a hard outage, not a
        # tuning nit. Fail loudly at startup rather than under load.
        raise RuntimeError(
            f"Postgres connection budget is {max_conns} "
            f"({size} pool + {overflow} overflow "
            f"x {settings.WEB_WORKERS} workers), which exceeds the default max_connections "
            "of 100. Lower DATABASE_POOL_SIZE/DATABASE_MAX_OVERFLOW, or raise the server limit."
        )
    return max_conns


def get_postgres_client() -> PostgresClient:
    """Return the process-wide client, creating it on first use.

    Lazy rather than import-time so that a test (or a script) can point
    ``settings.DATABASE_URL`` at its own database before the engine is built.
    Thread-safe with double-checked locking; the connection budget is validated
    before the engine is created so a misconfigured pool fails fast.
    """
    global _client
    if _client is not None:
        return _client
    with _client_lock:
        if _client is None:
            validate_postgres_budget()
            _client = PostgresClient()
    return _client


def close_postgres_client() -> None:
    """Dispose the shared engine's connections. Called on application shutdown."""
    global _client
    with _client_lock:
        if _client is not None:
            _client.close()
            _client = None


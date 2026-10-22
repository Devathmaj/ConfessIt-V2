"""PostgreSQL engine and SQLAlchemy session lifecycle."""
import threading
import uuid
from contextlib import contextmanager
from typing import Any

from sqlalchemy import CheckConstraint, Table, create_engine, text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from .accounts import AccountType
from .config import settings
from .db_models import Base
from .logger import get_logger

logger = get_logger(__name__)


# Advisory lock serialising schema convergence across workers: the convergence is a series of
# "read the catalogue, then ALTER" steps, two booting workers would both issue ADD CONSTRAINT
# and the loser dies on duplicate_object with a half-converged schema. The fixed key sits in a
# range of its own (account caps are 7_240_xxx, game sessions 7_250_xxx) so no lock blocks it.
SCHEMA_INIT_LOCK_KEY = 7_260_001

# SQLSTATE codes treated as "somebody else got there first", read from the code rather
# than the message: the message is localised, the code is not.
PG_DUPLICATE_TABLE = "42P07"        # duplicate_table
PG_DUPLICATE_OBJECT = "42710"      # duplicate_object -- a constraint or index is there
PG_UNDEFINED_OBJECT = "42704"       # undefined_object -- it is not there after all


def _postgres_error_code(exc: BaseException) -> str | None:
    """The ``SQLSTATE`` behind a database error, or None; checked on ``exc`` and its wrapped ``orig`` because it can arrive as either."""
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
    """The row named by ``row_id``, or None when the id names no row at all (a malformed id must never reach a uuid column)."""
    if not is_valid_id(row_id):
        return None
    return session.get(record, str(row_id))


def _server_default_sql(column, dialect) -> str | None:
    """Render a column's server default as an inline ``ADD COLUMN`` fragment, handling the three shapes a model can declare."""
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


# Placeholder address written by apply_not_null_constraints. Must be injective in regno:
# email carries a unique index per table, a collision would abort the UPDATE and the boot
# the method exists to avoid (a plain sanitise is not injective: a/b and a?b both map to
# a-b). Legal local parts pass verbatim; others get sanitise(47) + '+' + 16 md5 hex chars.
PLACEHOLDER_EMAIL_SQL = (
    "CASE WHEN regno ~ '^[A-Za-z0-9._%+-]{1,64}$' "
    "THEN regno || '@unassigned.invalid' "
    "ELSE left(regexp_replace(regno, '[^A-Za-z0-9._%+-]', '-', 'g'), 47) "
    "|| '+' || substr(md5(regno), 1, 16) "
    "|| '@unassigned.invalid' END"
)


# Columns the models no longer declare, on the tables that still carry them. A named list,
# not "anything the models omit": removing a column is a decision about data, and one a
# script or operator created is not the application's to delete. ``gender`` is load-bearing:
# NOT NULL with no default would reject every ORM staff insert; dropping it drops ck_accounts_gender.
RETIRED_COLUMNS: dict[str, tuple[str, ...]] = {
    "admin": ("gender", "profile_picture_public_id", "profile_picture_format"),
    "moderator": ("gender", "profile_picture_public_id", "profile_picture_format"),
    "users": ("emoji",),
    # Previews are now an encrypted copy of the newest visible message (see ConversationRecord).
    "conversations": ("last_message_preview",),
    # Two-Key Escape columns with no reader left: scoring counts start-to-finish elapsed
    # time only (``failed_moves``), keys are reusable, and no other game carries a score.
    "two_key_escape_sessions": (
        "failed_moves",
        "used_key_indexes",
        "moves_since_last_key",
        "has_used_key",
        "score",
    ),
}

# Columns whose type widened after deployment, which ``create_all`` cannot apply to
# existing tables. Widening is always safe (every stored value still fits); narrowing
# never appears here -- that would need a decision about data, not a boot step.
WIDENED_COLUMNS: dict[tuple[str, str], str] = {
    # Mismatched-network redemptions increment this on every refusal while SQL bounds
    # it at 2^31-1; a 16-bit column would overflow into a 500 under sustained probing.
    ("login_tokens", "attempt_count"): "integer",
}

# Free-text columns the staff queues and notification inbox search with ILIKE '%...%' (see
# utils.ilike_contains): a leading wildcard defeats every btree index, so a trigram GIN
# index is what makes the search plannable. Declared here rather than as Index() in
# db_models because the pg_trgm extension must be installed first, which metadata cannot.
TRGM_INDEX_COLUMNS: tuple[tuple[str, str], ...] = (
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

    A client owns exactly one pool, so a process must create at most one and share it via
    :func:`get_postgres_client`; the constructor stays public for one-shot scripts/tests
    that need an isolated engine.
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
        # create_engine is lazy (no sockets until a first checkout), and the session
        # timezone is pinned to UTC: every column is TIMESTAMPTZ so the instant is always
        # correct, but a non-UTC session changes how values are read back in logs/psql.
        # Kept on the instance for :meth:`schema_lock`, which connects the same way.
        self._connect_args = {
            "options": (
                f"-c timezone=UTC "
                f"-c statement_timeout={settings.DATABASE_STATEMENT_TIMEOUT_MS}"
            ),
            "connect_timeout": 10,
        }
        self.engine = create_engine(
            database_url or settings.DATABASE_URL,
            pool_pre_ping=True,
            pool_size=pool_size if pool_size is not None else settings.DATABASE_POOL_SIZE,
            max_overflow=max_overflow if max_overflow is not None else settings.DATABASE_MAX_OVERFLOW,
            pool_timeout=pool_timeout if pool_timeout is not None else settings.DATABASE_POOL_TIMEOUT,
            pool_recycle=pool_recycle if pool_recycle is not None else settings.DATABASE_POOL_RECYCLE,
            # Bound the TCP/TLS handshake itself: a dead endpoint would otherwise stall
            # each new connection on the OS retransmit schedule (~130 s on Windows)
            # instead of failing fast like an exhausted pool does.
            connect_args=self._connect_args,
        )
        self.sessions = sessionmaker(self.engine, expire_on_commit=False, class_=Session)

        # Engine for the schema-initialisation lock connection only: NullPool so the
        # lock connection is never returned to a pool while it may hold the lock.
        self._lock_engine = create_engine(
            database_url or settings.DATABASE_URL,
            poolclass=NullPool,
            connect_args=self._connect_args,
        )

    def initialize(self) -> None:
        """Bring the database schema in line with the models, serialised by :meth:`schema_lock` (see :data:`SCHEMA_INIT_LOCK_KEY`)."""
        with self.schema_lock():
            self._converge_schema()

    @contextmanager
    def schema_lock(self):
        """Hold the schema-initialisation advisory lock (session-scoped) for the block.

        Taken on a ``NullPool`` connection of its own -- pooled, it would deadlock the
        convergence steps' own checkouts -- and released by hand: a connection discarded
        unlocked holds the lock until the backend dies. Blocking on purpose; the lock
        statement commits so the connection is never idle-in-transaction, holding vacuum.
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
                    # The lock dies with its session, so the connection must not be
                    # returned anywhere it could be reused while still holding it.
                    logger.error(
                        "Could not release the schema-initialisation advisory lock; "
                        "closing the connection so the lock dies with its session"
                    )
        finally:
            connection.close()

    def _converge_schema(self) -> None:
        """The convergence sequence itself. Serialised by :meth:`schema_lock`."""
        # Refuse to boot against the old plaintext chat tables: the encrypted schema drops
        # the body column, which create_all cannot apply to an existing table. There is no
        # Alembic; the migration is the destructive one-shot scripts/recreate_chat_schema.py.
        self.assert_no_legacy_plaintext_chat_schema()
        # pgvector must exist before any vector(1536) table is created, else create_all
        # fails on a fresh database with "type vector does not exist".
        self.apply_vector_extension()
        self._create_tables()
        # Columns before constraints: a CHECK naming a column only the models declare is
        # refused until the column exists; adding columns first lets a partially-migrated
        # database converge in one boot.
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
        # Repairs staff rows that predate the preference after_insert hook and any raw-SQL writes.
        self.backfill_staff_notification_preferences()
        # After every additive step, so the session lifecycle converges on old databases.
        self.apply_game_session_convergence()
        # Trivia Drop's single-active-question rule, for the same reason: the
        # table is new, but the index is the backstop behind the advisory lock.
        self.apply_trivia_drop_convergence()
        # Automated confession moderation (Batch 1): system-actor reports reuse
        # the reports table with a nullable reporter and a source column.
        self.apply_report_source_convergence()

    def _create_tables(self) -> None:
        """Create the tables the models declare, retrying once on a concurrent worker's ``duplicate_table`` (a rolling deploy races the schema lock)."""
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
        """Fail clearly when a legacy plaintext chat schema is still installed (no convergence step can migrate it; point the operator at the one-shot reset)."""
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
        """Add model-declared columns missing from an already-created table (``create_all`` never touches existing tables)."""
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
                        # NOT NULL with no default cannot be added to a table that has
                        # rows; refuse to guess a value rather than fabricate data.
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
        """Drop the columns in ``RETIRED_COLUMNS`` from tables that still have them (dropped values are logged before they go)."""
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
        """Add model-declared unique indexes missing from the database (``create_all`` never touches existing tables).

        Added per index so duplicates give a named refusal, not a vague constraint error;
        existing rows are never deduped first -- picking a winner is a data decision.
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
        """Index rotated-away refresh token hashes: the refusal-path lookup must not scan the session table (partial, since only rotated sessions have a value)."""
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    'CREATE INDEX IF NOT EXISTS "ix_login_tokens_refresh_token_replaced_hash" '
                    'ON "login_tokens" ("refresh_token_replaced_hash") '
                    "WHERE refresh_token_replaced_hash IS NOT NULL"
                )
            )

    def apply_trgm_indexes(self) -> None:
        """Install pg_trgm GIN indexes for the ILIKE-searched columns (CREATE EXTENSION needs superuser; a refusal degrades to sequential scans, not a boot failure)."""
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
        """Install the pgvector extension so vector(1536) columns can exist; True when installed, a warning (not a boot refusal) when not."""
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
        """Best-effort HNSW cosine index on confession_embeddings.embedding (exact search stays correct without it; unsupported servers degrade)."""
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
        """Bring the database's CHECK constraints in line with the models: add absent, drop superseded.

        Added ``NOT VALID`` so pre-existing rows cannot take the app down on boot
        (validating stays a separate, explicit decision); superseded drops are safe
        because the replacement is added in the same transaction, and each statement
        tolerates a rolling-deploy worker's duplicate_object/undefined_object.
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
        """Run one constraint DDL statement under its own SAVEPOINT, tolerating one SQLSTATE.

        A DDL error aborts the whole transaction, so without the SAVEPOINT the tolerated
        failure would take the rest of the batch down with it.
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
        """Tighten model-declared NOT NULL columns a table still allows to be null (backfill a regno-derived placeholder first; see ``PLACEHOLDER_EMAIL_SQL``)."""
        # Columns that get a regno-derived placeholder (always available: account tables only).
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

                    # Ask first: an already-migrated database costs one lookup, no scan.
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
        """Widen columns whose model type outgrew the deployed one (only widenings -- every stored value still fits, so no data decision)."""
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
        """Names of installed CHECK constraints on ``table`` that a model has replaced (same family, different numeric suffix -- the name encodes the limit)."""
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
                # The unsuffixed installed name is a family declared by this table, but its
                # "suffix" is a word, not a digit, so the rule above cannot see it -- leaving
                # it is how a 2000-char ceiling survives beside a 100-char one.
                superseded.append(name)
        return superseded

    def backfill_session_deadlines(self) -> None:
        """Backfill ``session_expires_at`` for sessions predating the column (a NULL deadline reads as "already past", signing out every account at deploy)."""
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

    def backfill_staff_notification_preferences(self) -> None:
        """Insert the all-on preference row for staff accounts that predate the after_insert hook or were written by raw SQL."""
        inserted = 0
        with self.engine.begin() as connection:
            for account_type, table in (
                (AccountType.ADMIN.value, "admin"),
                (AccountType.MODERATOR.value, "moderator"),
            ):
                result = connection.execute(
                    text(
                        "INSERT INTO staff_notification_preferences "
                        "(account_type, account_id, reports_enabled, "
                        "love_notes_enabled, games_enabled) "
                        f"SELECT :account_type, id, TRUE, TRUE, TRUE FROM {table} "
                        "ON CONFLICT (account_type, account_id) DO NOTHING"
                    ),
                    {"account_type": account_type},
                )
                inserted += result.rowcount or 0
        if inserted:
            logger.info(
                "Backfilled %s staff notification preference row(s)", inserted
            )

    def apply_game_session_convergence(self) -> None:
        """Converge pre-refactor game tables to the session lifecycle (drop per-day uniqueness, backfill lifecycle columns, one active session per table).

        The single-active partial unique index -- not application code -- is what
        serializes a scheduler run racing an admin update: the loser gets an integrity
        error instead of a second active session.
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
                # 1. Drop legacy game_date uniqueness: the ``unique=True`` auto index plus the key constraint.
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
            # 4. A pending (not yet configured) session holds no word, so ``answer`` must
            # accept NULL -- adding a column converges, relaxing an existing NOT NULL
            # does not, and without this every pending insert fails on such databases.
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
        """Install Trivia Drop's at-most-one-active-question rule via a partial unique index (the database, not application code, backs the advisory lock)."""
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
        """Converge the reports tables for automated moderation (system actor: nullable reporter + ``source`` discriminator).

        Partial unique indexes allow one automated report per confession/comment/message
        while manual reports keep their own uniqueness. Idempotent.
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
                # Backfill + relax: manual rows always carry a reporter, automated ones NULL.
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
                # One automated report per confession/comment; manual reports keep their own uniqueness.
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
                # message_reports gets the same system-actor shape: automated reports need no receiver.
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
    """Validate the worst-case connection budget (pool + overflow x workers) against Postgres' default max_connections=100 before any engine is built."""
    size = settings.DATABASE_POOL_SIZE if pool_size is None else pool_size
    overflow = settings.DATABASE_MAX_OVERFLOW if max_overflow is None else max_overflow
    max_conns = (size + overflow) * max(settings.WEB_WORKERS, 1)
    if max_conns > 100:
        raise RuntimeError(
            f"Postgres connection budget is {max_conns} "
            f"({size} pool + {overflow} overflow "
            f"x {settings.WEB_WORKERS} workers), which exceeds the default max_connections "
            "of 100. Lower DATABASE_POOL_SIZE/DATABASE_MAX_OVERFLOW, or raise the server limit."
        )
    return max_conns


def get_postgres_client() -> PostgresClient:
    """Return the process-wide client, creating it on first use (lazy so tests can repoint DATABASE_URL first; double-checked locking)."""
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


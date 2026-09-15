"""PostgreSQL engine and SQLAlchemy session lifecycle."""
import uuid
from typing import Any

from sqlalchemy import CheckConstraint, Table, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from .config import settings
from .db_models import Base
from .logger import get_logger

logger = get_logger(__name__)


def new_id() -> str:
    """Create a canonical UUID string for API models that need client-side IDs."""
    return str(uuid.uuid4())


def is_valid_id(value: Any) -> bool:
    try:
        uuid.UUID(str(value))
        return True
    except (ValueError, TypeError, AttributeError):
        return False


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
        # create_engine is lazy: it opens no sockets until a connection is first checked
        # out, so a single shared instance costs nothing until the app actually serves.
        #
        # The session timezone is pinned to UTC rather than inherited from whatever the
        # database image or cluster happens to default to. Every timestamp column is
        # TIMESTAMPTZ, so a stored instant is always correct, but a non-UTC session changes
        # how a value is *read back* -- aware, so the instant survives, yet the text in logs
        # and the values an operator sees in psql would stop matching what the app wrote.
        # The application is UTC end to end, so the session is told so.
        self.engine = create_engine(
            database_url or settings.DATABASE_URL,
            pool_pre_ping=True,
            pool_size=pool_size if pool_size is not None else settings.DATABASE_POOL_SIZE,
            max_overflow=max_overflow if max_overflow is not None else settings.DATABASE_MAX_OVERFLOW,
            pool_timeout=pool_timeout if pool_timeout is not None else settings.DATABASE_POOL_TIMEOUT,
            pool_recycle=pool_recycle if pool_recycle is not None else settings.DATABASE_POOL_RECYCLE,
            connect_args={"options": "-c timezone=UTC"},
        )
        self.sessions = sessionmaker(self.engine, expire_on_commit=False, class_=Session)

    def initialize(self) -> None:
        Base.metadata.create_all(self.engine)
        # Columns before constraints: a CHECK added to an already-created table can name a
        # column that only exists in the models, and PostgreSQL refuses the constraint if the
        # column is not there yet. Adding the columns first is what lets a partially-migrated
        # database reach a consistent state in one boot.
        self.apply_missing_columns()
        self.apply_unique_indexes()
        self.apply_check_constraints()

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
        removing one is a decision about data rather than about schema shape. The
        one-off removal of the retired ``users.role`` and ``users.reported_count`` columns
        is in ``scripts/migrate_account_tables.py``.
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
                    connection.execute(
                        text(
                            f'ALTER TABLE "{table.name}" ADD CONSTRAINT "{constraint.name}" '
                            f"CHECK ({sqltext}) NOT VALID"
                        )
                    )
                    logger.info(
                        "Added CHECK constraint %s on %s (not validated)",
                        constraint.name,
                        table.name,
                    )
                for superseded in self._superseded_constraints(table, installed):
                    connection.execute(
                        text(
                            f'ALTER TABLE "{table.name}" DROP CONSTRAINT "{superseded}"'
                        )
                    )
                    logger.info(
                        "Dropped superseded CHECK constraint %s on %s",
                        superseded,
                        table.name,
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

        Only a *numeric* suffix qualifies. A rule that changes its SQL without changing its
        name cannot be detected this way, which is why rules that grow are written under a
        new name instead. Matching on arbitrary prefixes would be worse than not cleaning up
        at all, since it would eventually drop an unrelated constraint.
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
        return superseded

    def close(self) -> None:
        self.engine.dispose()


# Existing service annotations use this name; it now denotes only the engine/session provider.
Database = PostgresClient

_client: PostgresClient | None = None


def get_postgres_client() -> PostgresClient:
    """Return the process-wide client, creating it on first use.

    Lazy rather than import-time so that a test (or a script) can point
    ``settings.DATABASE_URL`` at its own database before the engine is built.
    """
    global _client
    if _client is None:
        _client = PostgresClient()
        max_conns = (
            settings.DATABASE_POOL_SIZE + settings.DATABASE_MAX_OVERFLOW
        ) * max(settings.WEB_WORKERS, 1)
        if max_conns > 100:
            # Postgres ships with max_connections=100, so this is a hard outage, not a
            # tuning nit. Fail loudly at startup rather than under load.
            raise RuntimeError(
                f"Postgres connection budget is {max_conns} "
                f"({settings.DATABASE_POOL_SIZE} pool + {settings.DATABASE_MAX_OVERFLOW} overflow "
                f"x {settings.WEB_WORKERS} workers), which exceeds the default max_connections "
                "of 100. Lower DATABASE_POOL_SIZE/DATABASE_MAX_OVERFLOW, or raise the server limit."
            )
    return _client


def close_postgres_client() -> None:
    """Dispose the shared engine's connections. Called on application shutdown."""
    global _client
    if _client is not None:
        _client.close()
        _client = None


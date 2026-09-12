"""PostgreSQL engine and SQLAlchemy session lifecycle."""
import uuid
from typing import Any

from sqlalchemy import CheckConstraint, create_engine, text
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
        self.apply_check_constraints()

    def apply_check_constraints(self) -> None:
        """Add CHECK constraints declared in the models but missing from the database.

        ``create_all`` only creates tables that do not exist, so on an already-deployed
        database a constraint added to a model is silently never applied. Walking the
        metadata here and adding whatever is absent keeps the running schema in step with
        the models without needing a migration framework or hand-written DDL that can
        drift from them.

        Constraints are added ``NOT VALID``: PostgreSQL then enforces them on every new or
        updated row while leaving already-stored rows alone. That matters for a limit added
        to a column that was previously unbounded, where existing rows may predate the
        limit. Validating them is a separate, deliberate step so that tightening a limit
        cannot take the app down on boot, and so the decision about what to do with
        over-length history stays explicit rather than silent.
        """
        with self.engine.begin() as connection:
            installed = {
                name
                for (name,) in connection.execute(
                    text("SELECT conname FROM pg_constraint WHERE contype = 'c'")
                )
            }
            for table in Base.metadata.sorted_tables:
                for constraint in table.constraints:
                    if not isinstance(constraint, CheckConstraint) or constraint.name in installed:
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


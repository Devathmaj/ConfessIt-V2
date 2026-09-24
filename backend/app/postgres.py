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
}


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
            # Bound the TCP/TLS handshake itself: without this a dead endpoint
            # stalls each new connection on the OS retransmit schedule (~130 s on
            # Windows) instead of failing fast like an exhausted pool does. Seen
            # when several connections race to a host that resolves to both ::1
            # and 127.0.0.1 while only IPv4 is listening.
            connect_args={"options": "-c timezone=UTC", "connect_timeout": 10},
        )
        self.sessions = sessionmaker(self.engine, expire_on_commit=False, class_=Session)

    def initialize(self) -> None:
        Base.metadata.create_all(self.engine)
        # Columns before constraints: a CHECK added to an already-created table can name a
        # column that only exists in the models, and PostgreSQL refuses the constraint if the
        # column is not there yet. Adding the columns first is what lets a partially-migrated
        # database reach a consistent state in one boot.
        self.apply_missing_columns()
        self.apply_retired_columns()
        self.apply_unique_indexes()
        self.apply_check_constraints()
        # NOT NULL last, and for the same ordering reason: it needs the columns to exist, and
        # unlike the others it may have to *rewrite rows* before it can be applied.
        self.apply_not_null_constraints()
        # After the columns exist, so it can name session_expires_at.
        self.backfill_session_deadlines()
        # After every additive step, so the session lifecycle converges on old databases.
        self.apply_game_session_convergence()

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
           ``anagram_games`` and ``couple_games``. History now shares days, so the
           unique index/constraint would refuse the second session of a day that
           the lifecycle legitimately creates.
        2. Backfill the new lifecycle columns on legacy rows: ``status`` defaults
           to ``'ended'`` at the column level, and ``period_start_date`` mirrors
           ``game_date`` where it is still NULL, so old rows read as history.
        3. Install one partial unique index per game table so at most one
           active-or-pending session exists per game. The index (not a CHECK, not
           application code) is what serializes a scheduler run racing an admin
           Update: the loser gets an integrity error instead of a second active.
        """
        with self.engine.begin() as connection:
            for table in ("wordle_games", "anagram_games", "couple_games"):
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


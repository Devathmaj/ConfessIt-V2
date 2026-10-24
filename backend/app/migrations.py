"""Alembic integration for applying schema revisions at startup and from scripts."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from sqlalchemy import Engine, inspect

from .db_models import Base
from .logger import get_logger

logger = get_logger(__name__)

BACKEND_ROOT = Path(__file__).resolve().parent.parent
ALEMBIC_INI = BACKEND_ROOT / "alembic.ini"
MIGRATIONS_DIR = BACKEND_ROOT / "migrations"
BASELINE_REVISION = "20261024_130741"
PUBLIC_SCHEMA = "public"


def _alembic_config(connection=None) -> Config:
    config = Config(str(ALEMBIC_INI))
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    if connection is not None:
        config.attributes["connection"] = connection
    return config


def _current_revision(connection) -> str | None:
    return MigrationContext.configure(connection).get_current_revision()


def _adopt_existing_schema(connection, present: set[str]) -> None:
    missing = sorted(set(Base.metadata.tables) - present)
    if missing:
        raise RuntimeError(
            "The database contains application tables but not the full current schema "
            f"(missing: {', '.join(missing)}). Refusing to stamp it at the Alembic "
            "baseline because later revisions assume every table exists."
        )
    command.stamp(_alembic_config(connection), BASELINE_REVISION)
    logger.info(
        "Existing schema adopted at Alembic baseline %s; no table or row was modified.",
        BASELINE_REVISION,
    )


def upgrade_to_head(engine: Engine) -> None:
    """Bring the database to the latest revision without rewriting existing data.

    A database created before Alembic already has the full current schema, so it is
    stamped at the baseline instead of running the baseline's CREATE statements. An
    empty database runs the baseline normally. Both paths are serialised by the
    caller's schema lock.
    """
    application_tables = set(Base.metadata.tables)

    with engine.begin() as connection:
        tables = set(inspect(connection).get_table_names(schema=PUBLIC_SCHEMA))
        present = tables & application_tables
        versioned = "alembic_version" in tables
        if present and (not versioned or _current_revision(connection) is None):
            _adopt_existing_schema(connection, present)

    with engine.begin() as connection:
        command.upgrade(_alembic_config(connection), "head")

    logger.info("Database schema is at Alembic revision head.")

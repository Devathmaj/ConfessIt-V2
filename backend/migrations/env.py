"""Alembic runtime configuration for the ConfessIt backend."""

from __future__ import annotations

import os
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

_original_cwd = Path.cwd()
try:
    if _original_cwd != BACKEND_ROOT:
        os.chdir(BACKEND_ROOT)
    from app.config import settings
    from app.db_models import Base
finally:
    os.chdir(_original_cwd)

config = context.config

if config.config_file_name is not None and config.attributes.get("connection") is None:
    fileConfig(config.config_file_name)

_UNMANAGED_INDEX_NAMES = {
    "ix_confession_embeddings_embedding_hnsw",
    "ix_login_tokens_refresh_token_replaced_hash",
    "uq_case_closed_games_single_active",
    "uq_connections_games_single_active",
    "uq_couple_games_single_active",
    "uq_anagram_games_single_active",
    "uq_plot_twist_games_single_active",
    "uq_reaction_time_games_single_active",
    "uq_two_key_escape_games_single_active",
    "uq_wordle_games_single_active",
    "uq_trivia_drop_questions_single_active",
    "uq_message_reports_automated",
    "uq_reports_comment_automated",
    "uq_reports_confession_automated",
}
_UNMANAGED_INDEX_PREFIXES = ("ix_trgm_",)

# The session tables used to carry a per-day unique index. The startup
# convergence removed it when sessions gained history, so it is not part of the
# database Alembic is baselining even though the models still declare it.
_UNMANAGED_MODEL_INDEX_NAMES = {
    "ix_anagram_games_game_date",
    "ix_case_closed_games_game_date",
    "ix_connections_games_game_date",
    "ix_couple_games_game_date",
    "ix_plot_twist_games_game_date",
    "ix_reaction_time_games_game_date",
    "ix_two_key_escape_games_game_date",
    "ix_wordle_games_game_date",
}


def _database_url() -> str:
    configured = config.get_main_option("sqlalchemy.url") or ""
    if configured and configured != "driver://user:pass@localhost/dbname":
        return configured
    return settings.DATABASE_URL


def include_object(object_, name, type_, reflected, compare_to):
    if type_ != "index" or name is None:
        return True
    if name in _UNMANAGED_INDEX_NAMES or name in _UNMANAGED_MODEL_INDEX_NAMES:
        return False
    if name.startswith(_UNMANAGED_INDEX_PREFIXES):
        return False
    return True


target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        include_object=include_object,
    )

    with context.begin_transaction():
        context.run_migrations()


def _run_migrations(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        include_object=include_object,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connection = config.attributes.get("connection")
    if connection is not None:
        _run_migrations(connection)
        return

    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = _database_url()
    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        _run_migrations(connection)


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

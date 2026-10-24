#!/usr/bin/env python3
"""Reset chat and report tables and recreate schema from models."""

from __future__ import annotations

import argparse
import os
import sys

_BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if not os.path.isfile(os.path.join(_BACKEND_ROOT, "app", "postgres.py")):
    raise RuntimeError(f"Refusing to import app from shadowed path: {_BACKEND_ROOT}")
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)

CHAT_TABLES = (
    "chat_evidence_access",
    "chat_message_evidence",
    "message_reports",
    "case_closed_message_reports",
    "messages",
    "case_closed_messages",
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Actually drop the tables. Without this flag the script only prints what it would do.",
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help="Use the derived test database (<PGDATABASE>_Test) instead of PGDATABASE.",
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help="Explicit SQLAlchemy PostgreSQL URL. Overrides --test/PGDATABASE.",
    )
    args = parser.parse_args()

    from sqlalchemy import text
    from sqlalchemy.engine import make_url

    from app.config import settings
    from app.db_models import Base
    from app.postgres import PostgresClient

    if args.database_url:
        database_url = args.database_url
    elif args.test:
        database_url = settings.TEST_DATABASE_URL
    else:
        database_url = settings.DATABASE_URL

    safe = make_url(database_url).render_as_string(hide_password=True)
    print(f"[chat-schema-reset] target: {safe}", flush=True)
    print(
        "[chat-schema-reset] will drop: " + ", ".join(CHAT_TABLES),
        flush=True,
    )
    if not args.yes:
        print("[chat-schema-reset] dry run; rerun with --yes to apply.", flush=True)
        return 0

    client = PostgresClient(database_url=database_url, pool_size=1, max_overflow=0)
    try:
        with client.engine.begin() as connection:
            for table in CHAT_TABLES:
                connection.execute(text(f'DROP TABLE IF EXISTS "{table}" CASCADE'))
            # The Alembic baseline is already stamped at head, so recreate only the
            # tables this script dropped, in dependency order.
            dropped_tables = [
                table for table in Base.metadata.sorted_tables if table.name in CHAT_TABLES
            ]
            Base.metadata.create_all(bind=connection, tables=dropped_tables, checkfirst=True)
        client.apply_trgm_indexes()
        with client.engine.begin() as connection:
            connection.execute(
                text(
                    'CREATE UNIQUE INDEX IF NOT EXISTS "uq_message_reports_automated" '
                    'ON "message_reports" ("message_id") '
                    "WHERE source = 'azure_content_safety'"
                )
            )
    finally:
        client.close()
    print("[chat-schema-reset] complete: encrypted chat schema installed.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""00:01 UTC game reset trigger: one pass, then exit.

Run by the ``game-scheduler`` cron sidecar (daily tick plus one catch-up pass
at container start) and safe to run by hand. The pass itself is
:func:`app.services.game_scheduler.run_reset_cycle`, which calls the same
per-game entry point as boot and ``POST /admin/games/run-resets`` -- so every
update-handling rule holds on this path: per-game advisory locks serialize a
tick racing an admin Update, the partial unique index refuses a duplicate
active session, pending sessions activate in-period, hidden games are
skipped, Trivia Drop releases through its own entry point, and closed league
events are finalized afterwards. Nothing here writes sessions directly.

Exit status is 0 only when every game and the league scan succeeded, so a
failed tick surfaces loudly in the container logs and in cron's own exit
reporting. A repeated run is always safe: each game's evaluation is
idempotent, and a second run for the same boundary changes nothing.
"""

import os
import re
import sys
import time
import traceback
from datetime import datetime, timezone

# Importing `app.*` works however this script is invoked: running a script by
# path puts the script's own directory (scripts/) on sys.path rather than the
# working directory, so without this a cron tick dies with
# "ModuleNotFoundError: No module named 'app'". Resolving from __file__ keeps
# the cron, manual and test invocations identical.
_BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Isolated import guard: only trust the resolved backend root if it actually
# holds the app package -- a shadowed directory (CWD/pythonpath attack) fails
# fast instead of importing hostile `app.*`.
if not os.path.isfile(os.path.join(_BACKEND_ROOT, "app", "postgres.py")):
    raise RuntimeError(f"Refusing to import app from shadowed path: {_BACKEND_ROOT}")
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)


def _sanitize_failure(text: str) -> str:
    """Redact DSN/password/secret material from a traceback before printing.

    SQLAlchemy/DBAPI errors echo the connection URL (user:password@host); cron
    logs are retained, so the raw traceback is a credential leak.
    """
    redacted = re.sub(r"://[^@\\s]+@", "://***@", text)
    for secret_name in ("DB_PASSWORD", "SECRET_KEY", "TOKEN_HASH_PEPPER", "REDIS_PASSWORD"):
        secret = os.getenv(secret_name)
        if secret and len(secret) >= 4:
            redacted = redacted.replace(secret, "***")
    return redacted

# Tries × delay before giving up on the database. The sidecar starts alongside
# the rest of the stack, so postgres may still be accepting its first
# connections; each tick is a fresh process, so a persistent outage fails this
# run loudly and the next tick retries.
READINESS_ATTEMPTS = 30
READINESS_DELAY_SECONDS = 2.0


def _wait_for_db(client) -> None:
    from sqlalchemy import text

    for attempt in range(READINESS_ATTEMPTS):
        try:
            with client.engine.connect() as connection:
                connection.execute(text("SELECT 1"))
            return
        except Exception:
            if attempt == READINESS_ATTEMPTS - 1:
                raise
            time.sleep(READINESS_DELAY_SECONDS)


def main() -> int:
    from app.postgres import PostgresClient
    from app.services.game_scheduler import reset_cycle_succeeded, run_reset_cycle

    started = datetime.now(timezone.utc)
    print(f"[game-resets] starting reset cycle at {started.isoformat()}", flush=True)
    # One-shot process: small pool (not the prod WEB_WORKERS-sized pool) so a
    # cron tick holds at most 2 connections instead of the serving budget.
    client = PostgresClient(pool_size=2, max_overflow=0)
    try:
        _wait_for_db(client)
        summary = run_reset_cycle(client)
        for game_id, info in summary["games"].items():
            detail = f" session={info['session_id']}" if info.get("session_id") else ""
            error = f" error={info['error']}" if info.get("error") else ""
            print(
                f"[game-resets] game={game_id} action={info.get('action')}"
                f"{detail}{error}",
                flush=True,
            )
        league = summary.get("league", {})
        league_error = f" error={league['error']}" if league.get("error") else ""
        print(
            f"[game-resets] league action={league.get('action')}{league_error}",
            flush=True,
        )
        succeeded = reset_cycle_succeeded(summary)
        print(
            f"[game-resets] finished at {datetime.now(timezone.utc).isoformat()} "
            f"result={'ok' if succeeded else 'FAILED'}",
            flush=True,
        )
        return 0 if succeeded else 1
    except Exception:
        # Sanitized: raw tracebacks echo the DB DSN (user:password@host).
        print(
            _sanitize_failure(traceback.format_exc()),
            flush=True,
        )
        return 1
    finally:
        try:
            client.close()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())

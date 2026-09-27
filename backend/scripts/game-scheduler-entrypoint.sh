#!/bin/sh
# Entrypoint for the game-scheduler cron sidecar: one reset trigger a day.
#
# - Writes the 00:01 UTC crontab, embedding this container's own environment
#   (cron jobs run with a near-empty environment, so DATABASE_URL is baked
#   into a wrapper the schedule calls rather than trusted to leak through).
# - Runs one catch-up pass immediately, so a restart after missed midnights
#   still opens today's periods instead of waiting for the next 01:00 tick.
#   The pass is idempotent, so this is a no-op on an ordinary restart.
# - Hands PID 1 to `cron -f`; each tick's output is redirected to PID 1's
#   stdout/stderr, which is what `docker compose logs` shows.
set -eu

PYTHON_BIN="${PYTHON_BIN:-/usr/local/bin/python}"
APP_DIR="${APP_DIR:-/app}"
CRON_FILE="${CRON_FILE:-/etc/cron.d/game-resets}"
WRAPPER=/usr/local/bin/run-game-resets

escape_single_quotes() {
  # Turn each ' into '\'' so the value survives single-quoting below.
  printf '%s' "$1" | sed "s/'/'\\\\''/g"
}

if [ -z "${DATABASE_URL:-}" ]; then
  echo "[game-scheduler] DATABASE_URL is not set; refusing to start" >&2
  exit 1
fi

# The indirection exists because cron(8) does not expand the environment and
# mis-handles quoted values in cron.d files; a shell wrapper quotes exactly once.
{
  printf '#!/bin/sh\n'
  printf 'export DATABASE_URL='"'"'%s'"'"'\n' "$(escape_single_quotes "$DATABASE_URL")"
  if [ -n "${TZ:-}" ]; then
    printf 'export TZ='"'"'%s'"'"'\n' "$(escape_single_quotes "$TZ")"
  fi
  printf 'cd %s\n' "$APP_DIR"
  printf 'exec %s %s/scripts/run_game_resets.py\n' "$PYTHON_BIN" "$APP_DIR"
} > "$WRAPPER"
chmod 755 "$WRAPPER"

{
  printf 'CRON_TZ=UTC\n'
  printf 'TZ=UTC\n'
  printf '1 0 * * * root %s > /proc/1/fd/1 2>&1\n' "$WRAPPER"
} > "$CRON_FILE"
chmod 644 "$CRON_FILE"

echo "[game-scheduler] running catch-up reset pass at container start"
if "$WRAPPER"; then
  echo "[game-scheduler] catch-up pass complete"
else
  # A failed catch-up must not take the daemon down with it: the daily tick
  # retries, and the exit reporting above is what pages the operator.
  echo "[game-scheduler] catch-up pass reported failures; continuing to cron" >&2
fi

echo "[game-scheduler] starting cron (game resets daily at 00:01 UTC)"
exec cron -f

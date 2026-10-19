#!/bin/sh
# Entrypoint for the game-scheduler cron sidecar: one reset trigger a day.
#
# - Validates every environment value it interpolates, then writes the 00:01 UTC
#   crontab, embedding this container's own environment (cron jobs run with a
#   near-empty environment, so every setting the tick needs is baked into a
#   wrapper the schedule calls rather than trusted to leak through).
# - Runs one catch-up pass immediately, so a restart after missed midnights
#   still opens today's periods instead of waiting for the next 01:00 tick.
#   The pass is idempotent, so this is a no-op on an ordinary restart.
# - Hands PID 1 to `cron -f`; each tick's output is redirected to PID 1's
#   stdout/stderr, which is what `docker compose logs` shows.
#
# The wrapper and the cron line are generated shell, and the tick runs as the
# unprivileged appuser (cron runs as root, the job does not). A value smuggled
# in through the environment -- a newline splitting the wrapper into a second
# command, `$()` in an unquoted path -- would otherwise execute with the
# writer's privilege. So: values are validated before anything is written,
# every expansion in the generated code is single-quoted (where $, backtick
# and backslash are inert), and paths additionally reject those characters
# outright, since no legitimate path contains them. Secrets (DATABASE_URL, TZ)
# may legitimately contain $, backticks and backslashes -- passwords do -- so
# for values the single-quoting is the control, not rejection.
set -eu

PYTHON_BIN="${PYTHON_BIN:-/usr/local/bin/python}"
APP_DIR="${APP_DIR:-/app}"
CRON_FILE="${CRON_FILE:-/etc/cron.d/game-resets}"
CRON_USER="${CRON_USER:-appuser}"
WRAPPER="${WRAPPER:-/usr/local/bin/run-game-resets}"

escape_single_quotes() {
  # Turn each ' into '\'' so the value survives single-quoting below. This is
  # the only transformation a single-quoted value needs: inside single quotes
  # $, `, \, ! and newlines are all literal.
  printf '%s' "$1" | sed "s/'/'\\\\''/g"
}

reject_line_breaks() {
  # A newline or CR in any interpolated value would split the generated wrapper
  # or cron file into extra lines that run as shell. No legitimate value --
  # path, URL, password, timezone -- contains one, so refuse before writing
  # anything.
  _nl='
'
  _cr=$(printf '\r')
  case "$1" in
    *"$_nl"*|*"$_cr"*)
      echo "[game-scheduler] refusing to start: $2 contains a line break" >&2
      exit 1
      ;;
  esac
}

reject_shell_metachars() {
  # For paths only: $, backtick and backslash never occur in APP_DIR,
  # PYTHON_BIN, CRON_FILE or CRON_USER, and each is a breakout vector in any
  # context weaker than single quotes. Values keep them (see above).
  _dollar='$'
  _backtick='`'
  _backslash='\'
  case "$1" in
    *"$_dollar"*|*"$_backtick"*|*"$_backslash"*)
      echo "[game-scheduler] refusing to start: $2 contains a shell metacharacter" >&2
      exit 1
      ;;
  esac
}

# Read the named setting into $_setting_value.
#
# The name always comes from the literal REQUIRED_SETTINGS list, never from the
# environment, so the indirect expansion cannot be steered into evaluating
# anything else. An assignment never re-splits, re-globs or re-quotes what it
# stores, so a secret full of spaces, quotes, $ or backticks survives intact.
# The unset form (${VAR-}, not ${VAR}) keeps `set -u` quiet for a variable the
# deployment simply does not set -- the emptiness check is the caller's job.
read_setting() {
  eval "_setting_value=\${$1-}"
}

# Every setting the app's Settings model requires. cron(8) does not pass the
# container environment to the jobs it runs, so each of these has to be baked
# into the generated wrapper: the tick builds the same Settings object the API
# does, and a single missing required field aborts the whole pass before one
# game is evaluated -- a reset that never runs is invisible (no session rolls
# over, no league event finalises, no expired chat is purged) and the only
# symptom is a non-zero exit in the container log.
#
# This list is asserted against app.config.Settings in
# backend/tests/test_scheduler_entrypoint.py, so a newly required setting
# cannot be added to the app without the wrapper following it.
REQUIRED_SETTINGS="DATABASE_URL TEST_DATABASE_URL REDIS_URL SECRET_KEY TOKEN_HASH_PEPPER"

# All validation happens before any file is written, so a refused value leaves
# no half-generated wrapper behind.
for _setting in $REQUIRED_SETTINGS; do
  read_setting "$_setting"
  if [ -z "$_setting_value" ]; then
    # Fail closed and loud: the tick builds the same Settings object the API
    # does, so a missing required field would abort every reset -- silently,
    # at 00:01 UTC, long after the operator stopped watching the container.
    # Refusing to start here names the missing setting instead.
    echo "[game-scheduler] $_setting is not set; refusing to start" >&2
    exit 1
  fi
  reject_line_breaks "$_setting_value" "$_setting"
done

reject_line_breaks "${TZ:-}" "TZ"
reject_line_breaks "$APP_DIR" "APP_DIR"
reject_shell_metachars "$APP_DIR" "APP_DIR"
reject_line_breaks "$PYTHON_BIN" "PYTHON_BIN"
reject_shell_metachars "$PYTHON_BIN" "PYTHON_BIN"
reject_line_breaks "$CRON_FILE" "CRON_FILE"
reject_shell_metachars "$CRON_FILE" "CRON_FILE"
reject_line_breaks "$WRAPPER" "WRAPPER"
reject_shell_metachars "$WRAPPER" "WRAPPER"
reject_line_breaks "$CRON_USER" "CRON_USER"
reject_shell_metachars "$CRON_USER" "CRON_USER"

if ! command -v runuser >/dev/null 2>&1; then
  echo "[game-scheduler] runuser is not installed; refusing to run resets" >&2
  exit 1
fi

# Seconds between catch-up attempts. Digits only: it is interpolated into
# sleep, where anything else would execute.
case "${CATCHUP_RETRY_DELAY_SECONDS:-15}" in
  ''|*[!0-9]*)
    echo "[game-scheduler] refusing to start: CATCHUP_RETRY_DELAY_SECONDS must be digits" >&2
    exit 1
    ;;
esac

# The indirection exists because cron(8) does not expand the environment and
# mis-handles quoted values in cron.d files; a shell wrapper quotes exactly once.
# Every expansion below is wrapped in single quotes (via escape_single_quotes),
# so word splitting, globbing, $ expansion, backticks and backslashes in values
# are all inert where the wrapper runs them.
#
# The wrapper exports every required setting, not just the database URL: the
# tick imports the app, and the app's Settings object refuses to be built
# without them. Exporting only DATABASE_URL left the 00:01 tick dying on a
# pydantic ValidationError before it evaluated a single game -- no rollover,
# no AI generation cycle, no league finalisation, no chat retention -- while
# the catch-up pass at container start (which does inherit the container
# environment) kept succeeding and made the stack look healthy.
{
  printf '#!/bin/sh\n'
  for _setting in $REQUIRED_SETTINGS; do
    read_setting "$_setting"
    printf 'export %s=%s\n' \
      "$_setting" \
      "'$(escape_single_quotes "$_setting_value")'"
  done
  if [ -n "${TZ:-}" ]; then
    printf 'export TZ=%s\n' "'$(escape_single_quotes "$TZ")'"
  fi
  printf 'cd %s\n' "'$(escape_single_quotes "$APP_DIR")'"
  printf 'exec %s %s\n' \
    "'$(escape_single_quotes "$PYTHON_BIN")'" \
    "'$(escape_single_quotes "$APP_DIR")/scripts/run_game_resets.py'"
} > "$WRAPPER"
chmod 755 "$WRAPPER"

# The job runs as the unprivileged appuser: a reset needs the database and the
# app tree, not root. cron itself stays root (it must, to read cron.d and
# change user); only the tick drops privilege.
{
  printf 'CRON_TZ=UTC\n'
  printf 'TZ=UTC\n'
  printf '1 0 * * * %s %s > /proc/1/fd/1 2>&1\n' "$CRON_USER" "$WRAPPER"
} > "$CRON_FILE"
chmod 644 "$CRON_FILE"

echo "[game-scheduler] running catch-up reset pass at container start"
_attempt=1
while [ "$_attempt" -le 5 ]; do
  if runuser -u "$CRON_USER" -- "$WRAPPER"; then
    echo "[game-scheduler] catch-up pass complete"
    break
  fi
  # The usual cause is starting ahead of the database (empty tables on a
  # fresh bring-up): back off and retry rather than waiting a whole day
  # for the next tick. A failed catch-up still must not take the daemon
  # down with it: the daily tick retries, and the exit reporting above is
  # what pages the operator.
  if [ "$_attempt" -eq 5 ]; then
    echo "[game-scheduler] catch-up pass reported failures; continuing to cron" >&2
    break
  fi
  echo "[game-scheduler] catch-up pass attempt $_attempt failed; retrying" >&2
  sleep "${CATCHUP_RETRY_DELAY_SECONDS:-15}"
  _attempt=$((_attempt + 1))
done

echo "[game-scheduler] starting cron (game resets daily at 00:01 UTC)"
exec cron -f

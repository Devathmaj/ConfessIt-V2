#!/bin/sh
# Entrypoint for game-scheduler cron sidecar running daily reset triggers.
set -eu

PYTHON_BIN="${PYTHON_BIN:-/usr/local/bin/python}"
APP_DIR="${APP_DIR:-/app}"
CRON_FILE="${CRON_FILE:-/etc/cron.d/game-resets}"
CRON_USER="${CRON_USER:-appuser}"
WRAPPER="${WRAPPER:-/usr/local/bin/run-game-resets}"

escape_single_quotes() {
  # Escape single quotes for safe shell interpolation.
  printf '%s' "$1" | sed "s/'/'\\\\''/g"
}

reject_line_breaks() {
  # Reject line breaks in interpolated values.
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
  # Reject shell metacharacters in paths and usernames.
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
read_setting() {
  eval "_setting_value=\${$1-}"
}

# Settings required by app.config.Settings to bake into the cron wrapper.
REQUIRED_SETTINGS="PGHOST PGUSER PGPORT PGDATABASE PGPASSWORD REDIS_URL SECRET_KEY TOKEN_HASH_PEPPER"
OPTIONAL_SETTINGS="CHAT_CLEANUP_BATCH_SIZE PGSSLMODE"

# All validation happens before any file is written, so a refused value leaves
# no half-generated wrapper behind.
for _setting in $REQUIRED_SETTINGS; do
  read_setting "$_setting"
  if [ -z "$_setting_value" ]; then
    # Fail fast if any required setting is missing.
    echo "[game-scheduler] $_setting is not set; refusing to start" >&2
    exit 1
  fi
  reject_line_breaks "$_setting_value" "$_setting"
done

# This optional value controls the scheduler's bounded chat cleanup batches.
# Keep the app's default when it is absent; reject malformed values before any
# wrapper or cron file is written.
read_setting CHAT_CLEANUP_BATCH_SIZE
if [ -n "$_setting_value" ]; then
  case "$_setting_value" in
    *[!0-9]*)
      echo "[game-scheduler] refusing to start: CHAT_CLEANUP_BATCH_SIZE must be digits" >&2
      exit 1
      ;;
  esac
fi

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

# Validate retry delay is numeric.
case "${CATCHUP_RETRY_DELAY_SECONDS:-15}" in
  ''|*[!0-9]*)
    echo "[game-scheduler] refusing to start: CATCHUP_RETRY_DELAY_SECONDS must be digits" >&2
    exit 1
    ;;
esac

# Generate wrapper script exporting all required environment variables.
{
  printf '#!/bin/sh\n'
  for _setting in $REQUIRED_SETTINGS; do
    read_setting "$_setting"
    printf 'export %s=%s\n' \
      "$_setting" \
      "'$(escape_single_quotes "$_setting_value")'"
  done
  for _setting in $OPTIONAL_SETTINGS; do
    read_setting "$_setting"
    if [ -n "$_setting_value" ]; then
      printf 'export %s=%s\n' \
        "$_setting" \
        "'$(escape_single_quotes "$_setting_value")'"
    fi
  done
  if [ -n "${TZ:-}" ]; then
    printf 'export TZ=%s\n' "'$(escape_single_quotes "$TZ")'"
  fi
  printf 'cd %s\n' "'$(escape_single_quotes "$APP_DIR")'"
  printf 'exec %s %s\n' \
    "'$(escape_single_quotes "$PYTHON_BIN")'" \
    "'$(escape_single_quotes "$APP_DIR")/scripts/run_game_resets.py'"
} > "$WRAPPER"
# /tmp is a hardened tmpfs and may be mounted noexec. Keep the generated
# wrapper readable and invoke it through /bin/sh instead of executing it.
chmod 644 "$WRAPPER"

# Configure daily cron job running as unprivileged user.
{
  printf 'CRON_TZ=UTC\n'
  printf 'TZ=UTC\n'
  printf '1 0 * * * %s /bin/sh %s > /proc/1/fd/1 2>&1\n' "$CRON_USER" "$WRAPPER"
} > "$CRON_FILE"
chmod 644 "$CRON_FILE"

echo "[game-scheduler] running catch-up reset pass at container start"
_attempt=1
while [ "$_attempt" -le 5 ]; do
  if runuser -u "$CRON_USER" -- /bin/sh "$WRAPPER"; then
    echo "[game-scheduler] catch-up pass complete"
    break
  fi
  # Retry catch-up pass in case database is still initializing.
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

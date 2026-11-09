#!/usr/bin/env bash
set -euo pipefail
APP="$HOME/github-weekly"
USER_UNITS="$HOME/.config/systemd/user"
CURRENT_USER="$(id -un)"

SYSTEMD_USER_AVAILABLE=0
if command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1; then
  SYSTEMD_USER_AVAILABLE=1
fi

if [[ "$SYSTEMD_USER_AVAILABLE" == 1 ]]; then
  if command -v loginctl >/dev/null 2>&1; then
    loginctl enable-linger "$CURRENT_USER" >/dev/null 2>&1 || true
    if [[ "$(loginctl show-user "$CURRENT_USER" -p Linger --value 2>/dev/null || true)" != yes ]]; then
      echo "Could not enable user linger automatically; run: sudo loginctl enable-linger $CURRENT_USER" >&2
    fi
  else
    echo "loginctl is unavailable; run: sudo loginctl enable-linger $CURRENT_USER" >&2
  fi
fi

if ! command -v crontab >/dev/null 2>&1; then
  echo "crontab is required to remove this project's previous scheduled jobs" >&2
  exit 1
fi
if [[ "$SYSTEMD_USER_AVAILABLE" == 0 ]]; then
  if [[ "$(date +%z)" != "+0000" ]]; then
    echo "systemd --user is unavailable and this cron fallback requires a UTC server timezone" >&2
    exit 1
  fi
  if ! command -v flock >/dev/null 2>&1; then
    echo "systemd --user is unavailable and flock is required for the cron watcher fallback" >&2
    exit 1
  fi
fi

mkdir -p "$APP"
tar -xf "$HOME/deploy.tar" -C "$APP"
mkdir -p "$APP/.secrets" "$APP/data"
chmod 700 "$APP/.secrets"
chmod +x "$APP/publish_wechat.sh"
cd "$APP"

# Keep Python dependencies isolated from system Python (PEP 668).
if ! python3 -m venv "$APP/.venv"; then
  echo "Could not create $APP/.venv; install the python3-venv package and rerun" >&2
  exit 1
fi
"$APP/.venv/bin/python" -m pip install --disable-pip-version-check openai

# Install the WeChat browser script's declared npm dependencies.
cd "$APP/wechat"
npm install --omit=dev --no-audit --no-fund
cd "$APP"

if [[ ! -f "$APP/app.env" ]]; then
  echo "Missing $APP/app.env; create it from app.env.example and configure the credentials, then rerun" >&2
  exit 1
fi
chmod 600 "$APP/app.env"

# Remove only old entries that invoke this deployment's pipeline. Keep the
# user's other cron jobs untouched. A CRON_TZ setting is retained whenever
# any other cron jobs remain, since it may govern those jobs too.
cron_before="$(mktemp)"
cron_after="$(mktemp)"
trap 'rm -f "$cron_before" "$cron_after"' EXIT
crontab -l >"$cron_before" 2>/dev/null || true
awk -v app="$APP" '
  index($0, app "/weekly_pipeline.py") && $0 ~ /weekly_pipeline\.py (run|watch)([[:space:]]|$)/ { next }
  { print }
' "$cron_before" >"$cron_after"
has_cron_jobs="$(awk 'NF && $0 !~ /^[[:space:]]*#/ && $0 !~ /^[[:space:]]*[A-Za-z_][A-Za-z0-9_]*[[:space:]]*=/ { found = 1 } END { print found + 0 }' "$cron_after")"
if [[ "$has_cron_jobs" == 0 ]]; then
  awk '!/^[[:space:]]*CRON_TZ[[:space:]]*=/' "$cron_after" >"$cron_before"
  mv "$cron_before" "$cron_after"
  cron_before="$(mktemp)"
fi

if [[ "$SYSTEMD_USER_AVAILABLE" == 1 ]]; then
  if ! grep -q '[^[:space:]]' "$cron_after"; then
    crontab -r 2>/dev/null || true
  else
    crontab "$cron_after"
  fi
else
  # Cron uses the host timezone (verified above as UTC): Monday 01:00 UTC is
  # Monday 09:00 Asia/Shanghai. Put entries before user CRON_TZ declarations.
  # flock's non-blocking miss emits no output and keeps concurrent minutes quiet.
  run_command="/bin/bash -c 'set -a; . \"$APP/app.env\"; set +a; exec \"$APP/.venv/bin/python\" \"$APP/weekly_pipeline.py\" run' >> \"$APP/data/cron.log\" 2>&1"
  watch_command="/bin/bash -c 'set -a; . \"$APP/app.env\"; set +a; exec /usr/bin/flock -n \"$APP/data/watcher-launch.lock\" \"$APP/.venv/bin/python\" \"$APP/weekly_pipeline.py\" watch' >> \"$APP/data/watcher.log\" 2>&1"
  {
    printf '0 1 * * 1 %s\n' "$run_command"
    printf '* * * * * %s\n' "$watch_command"
    cat "$cron_after"
  } >"$cron_before"
  crontab "$cron_before"
fi

if [[ "$SYSTEMD_USER_AVAILABLE" == 1 ]]; then
  mkdir -p "$USER_UNITS"
  install -m 0644 "$APP/github-weekly-run.service" "$USER_UNITS/github-weekly-run.service"
  install -m 0644 "$APP/github-weekly-run.timer" "$USER_UNITS/github-weekly-run.timer"
  install -m 0644 "$APP/github-weekly-preview.service" "$USER_UNITS/github-weekly-preview.service"
  systemctl --user daemon-reload
  systemctl --user enable --now github-weekly-preview.service
  systemctl --user enable --now github-weekly-run.timer
  echo "installed $APP; systemd user watcher and Monday 09:00 Asia/Shanghai timer enabled"
else
  echo "installed $APP; cron fallback uses Monday 01:00 UTC and a flock-guarded minute watcher"
fi

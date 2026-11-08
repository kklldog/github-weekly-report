#!/usr/bin/env bash
set -euo pipefail
APP="$HOME/github-weekly"
mkdir -p "$APP"
tar -xf "$HOME/deploy.tar" -C "$APP"
mkdir -p "$APP/.secrets" "$APP/data"
chmod 700 "$APP/.secrets"
chmod +x "$APP/publish_wechat.sh"
cd "$APP"

# Install the small Python dependency used by the bundled imagegen CLI. The
# pipeline itself otherwise uses the Python standard library.
python3 -m pip install --user --disable-pip-version-check openai >/tmp/github-weekly-pip.log 2>&1 || true

# The WeChat browser script uses Bun via npx and installs its declared npm
# dependencies into the deployment directory.
cd "$APP/wechat"
npm install --omit=dev --no-audit --no-fund >/tmp/github-weekly-npm.log 2>&1 || true
cd "$APP"

cat > "$HOME/github-weekly/cron-entry" <<'EOF'
CRON_TZ=Asia/Shanghai
0 9 * * 1 /usr/bin/python3 /home/kklldog/github-weekly/weekly_pipeline.py run >> /home/kklldog/github-weekly/data/cron.log 2>&1
@reboot /usr/bin/python3 /home/kklldog/github-weekly/weekly_pipeline.py watch >> /home/kklldog/github-weekly/data/watcher.log 2>&1
EOF
crontab "$HOME/github-weekly/cron-entry"
echo "installed $APP"

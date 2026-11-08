#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
: "${WECHAT_TITLE:?WECHAT_TITLE is required}"
: "${WECHAT_CONTENT:?WECHAT_CONTENT is required}"
: "${WECHAT_IMAGES_DIR:?WECHAT_IMAGES_DIR is required}"

export WECHAT_BROWSER_PROFILE_DIR="${WECHAT_BROWSER_PROFILE_DIR:-$HOME/.config/google-chrome}"
export WECHAT_BROWSER_CHROME_PATH="${WECHAT_BROWSER_CHROME_PATH:-/usr/bin/google-chrome}"
export QR_SCREENSHOT_PATH="$PWD/data/wechat-login-qr.png"
rm -f "$QR_SCREENSHOT_PATH"

cmd=(npx -y bun wechat/wechat-browser.ts --title "$WECHAT_TITLE" --content "$WECHAT_CONTENT" --images "$WECHAT_IMAGES_DIR" --submit)
if command -v xvfb-run >/dev/null 2>&1; then
  cmd=(xvfb-run -a --server-args='-screen 0 1440x1200x24' "${cmd[@]}")
fi

"${cmd[@]}" 2>&1 | tee -a data/wechat-publish.log

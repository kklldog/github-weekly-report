#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
: "${WECHAT_POST_DIR:?WECHAT_POST_DIR is required}"
exec node wechat/publish.mjs

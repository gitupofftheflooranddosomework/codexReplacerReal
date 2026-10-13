#!/usr/bin/env bash
set -euo pipefail

PREFIX=${CODEX_REPLACER_BROWSER_MCP_PREFIX:-/opt/serverworkerfabric/browser-mcp}
VERSION=${CODEX_REPLACER_BROWSER_MCP_VERSION:-0.0.41}
SERVICE_USER=${CODEX_REPLACER_SERVICE_USER:-swfbroker}

test "$(id -u)" -eq 0
command -v npm >/dev/null
install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0755 "$PREFIX"
cd "$PREFIX"
test -f package.json || npm init -y >/dev/null
npm install --omit=dev "@playwright/mcp@$VERSION"
test -x "$PREFIX/node_modules/.bin/mcp-server-playwright"
chown -R root:root "$PREFIX"
echo "CODEX_REPLACER_BROWSER_MCP_COMMAND=$PREFIX/node_modules/.bin/mcp-server-playwright"


#!/usr/bin/env bash
set -euo pipefail

VAULT_URL=${CODEX_VAULT_URL:-https://vault.markshaw.ca}
ORGANIZATION_ID=${CODEX_VAULT_ORGANIZATION_ID:-3e3b3667-0137-43d6-b297-bb7abc6fb202}
CONFIG_DIR=${CODEX_VAULT_BW_CONFIG_DIR:-$HOME/.config/codex-replacer/bitwarden-dotmoose}
SESSION_FILE=${CODEX_VAULT_SESSION_FILE:-$HOME/.config/codex-replacer/dotmoose-vault.session}

command -v bw >/dev/null 2>&1 || {
  echo "Bitwarden CLI is not installed." >&2
  exit 1
}

install -d -m 700 "$CONFIG_DIR" "$(dirname "$SESSION_FILE")"
export BITWARDENCLI_APPDATA_DIR="$CONFIG_DIR"
export BW_NOINTERACTION=false
bw config server "$VAULT_URL" >/dev/null

status=$(bw status | python3 -c 'import json,sys; print(json.load(sys.stdin).get("status", "unknown"))')
if [[ "$status" == "unauthenticated" ]]; then
  echo "Sign in with the existing DotMoose Vaultwarden member account."
  bw login
fi

temporary=$(mktemp "${SESSION_FILE}.XXXXXX")
chmod 600 "$temporary"
trap 'rm -f "$temporary"' EXIT
bw unlock --raw > "$temporary"
test -s "$temporary"
export BW_SESSION
BW_SESSION=$(cat "$temporary")
bw sync >/dev/null
count=$(bw list items --organizationid "$ORGANIZATION_ID" | python3 -c 'import json,sys; print(sum(1 for item in json.load(sys.stdin) if item.get("type") == 1 and not item.get("deletedDate")))')
install -m 600 "$temporary" "$SESSION_FILE"
echo "DotMoose vault enrollment complete: ${count} login items are available."

#!/bin/sh
set -eu
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PROFILE_DIR="$HOME/.local/share/codex-replacer/chatgpt-browser"
mkdir -p "$HOME/.config/systemd/user" "$PROFILE_DIR"
chmod 0700 "$HOME/.local/share/codex-replacer" "$PROFILE_DIR"
install -m 0644 "$SCRIPT_DIR/systemd/codex-chatgpt-browser.service" "$HOME/.config/systemd/user/codex-chatgpt-browser.service"
if command -v loginctl >/dev/null 2>&1; then
  loginctl enable-linger "$(id -un)" >/dev/null 2>&1 || true
fi
systemctl --user daemon-reload
systemctl --user enable --now codex-chatgpt-browser.service
printf '%s\n' 'Headed ChatGPT browser installed with a persistent 0700 profile. Complete one approved Google sign-in once; the session is then retained in that profile across MCP and browser restarts.'

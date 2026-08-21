#!/bin/sh
set -eu
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
mkdir -p "$HOME/.config/systemd/user" "$HOME/.local/share/codex-replacer/chatgpt-browser"
install -m 0644 "$SCRIPT_DIR/systemd/codex-chatgpt-browser.service" "$HOME/.config/systemd/user/codex-chatgpt-browser.service"
systemctl --user daemon-reload
systemctl --user enable --now codex-chatgpt-browser.service
printf '%s\n' 'Headed ChatGPT browser installed. Complete one-time sign-in in its Chromium window, then use chatgpt_start_chat.'

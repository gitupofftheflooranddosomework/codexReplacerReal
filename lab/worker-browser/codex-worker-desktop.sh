#!/bin/bash
set -euo pipefail

export HOME=/home/mark
export USER=mark
export LOGNAME=mark
export DISPLAY=:99
export XDG_RUNTIME_DIR=/run/user/$(id -u)
PROFILE_DIR="$HOME/.local/share/codex-worker/browser"

mkdir -p "$PROFILE_DIR" "$HOME/.cache/codex-worker"
chmod 700 "$HOME/.local/share/codex-worker" "$PROFILE_DIR" 2>/dev/null || true
rm -f /tmp/.X99-lock /tmp/.X11-unix/X99 2>/dev/null || true

pids=()
cleanup() {
  for pid in "${pids[@]:-}"; do kill "$pid" 2>/dev/null || true; done
  wait 2>/dev/null || true
}
trap cleanup EXIT INT TERM

Xvfb :99 -screen 0 1440x900x24 -nolisten tcp -ac +extension RANDR &
pids+=("$!")
for _ in $(seq 1 100); do
  xdpyinfo -display :99 >/dev/null 2>&1 && break
  sleep 0.05
done
xdpyinfo -display :99 >/dev/null 2>&1

openbox --sm-disable &
pids+=("$!")

tint2 >/dev/null 2>&1 &
pids+=("$!")

chromium \
  --user-data-dir="$PROFILE_DIR" \
  --password-store=basic \
  --remote-debugging-address=127.0.0.1 \
  --remote-debugging-port=9222 \
  --remote-allow-origins='*' \
  --no-first-run \
  --no-default-browser-check \
  --disable-session-crashed-bubble \
  --disable-background-networking \
  --disable-component-update \
  --disable-features=Translate \
  --window-size=1440,900 \
  --start-maximized \
  about:blank &
pids+=("$!")

x11vnc -display :99 -rfbport 5900 -localhost -forever -shared -nopw -quiet &
pids+=("$!")

websockify --web=/usr/share/novnc 0.0.0.0:6080 127.0.0.1:5900 &
pids+=("$!")

wait -n "${pids[@]}"
exit 1

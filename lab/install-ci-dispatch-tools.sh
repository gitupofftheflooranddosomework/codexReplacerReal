#!/usr/bin/env bash
set -euo pipefail
set +x

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET_BIN="${CODEX_CI_BIN:-$HOME/.local/bin}"
DISPATCH_SRC="$SCRIPT_DIR/codex-ci-dispatch.py"
FABRIC_MANAGER_SRC="$SCRIPT_DIR/serverworkerfabric-headless-manager.py"
DISPATCH_DST="$TARGET_BIN/codex-ci-dispatch"
FABRIC_MANAGER_DST="$TARGET_BIN/serverworkerfabric-headless-manager"

fail() {
  echo "ERROR: $*" >&2
  exit 1
}

command -v python3 >/dev/null || fail "python3 is required"
command -v install >/dev/null || fail "install is required"

for source in "$DISPATCH_SRC" "$FABRIC_MANAGER_SRC"; do
  test -r "$source" || fail "missing source: $source"
  python3 - "$source" <<'PY'
import pathlib,sys
path=pathlib.Path(sys.argv[1])
source=path.read_text(encoding="utf-8")
compile(source,str(path),"exec")
PY
done

install -d -m 0755 "$TARGET_BIN"

install_atomic() {
  local source="$1" destination="$2"
  local tmp
  tmp="$(mktemp "$TARGET_BIN/.swf-install.XXXXXX")"
  trap 'rm -f "$tmp"' RETURN
  install -m 0700 "$source" "$tmp"
  mv -f "$tmp" "$destination"
  trap - RETURN
}

install_atomic "$DISPATCH_SRC" "$DISPATCH_DST"
install_atomic "$FABRIC_MANAGER_SRC" "$FABRIC_MANAGER_DST"

test -x "$DISPATCH_DST" || fail "dispatcher is not executable after install"
test -x "$FABRIC_MANAGER_DST" || fail "Fabric manager is not executable after install"

"$DISPATCH_DST" --help >/dev/null
"$FABRIC_MANAGER_DST" --help >/dev/null

# Deliberately do not install, replace, remove, or repoint codex-ci-headless.
# The legacy local manager remains the dispatcher default rollback path.
echo "CODEX_CI_DISPATCH=$DISPATCH_DST"
echo "CODEX_CI_SWF_MANAGER=$FABRIC_MANAGER_DST"
echo "CODEX_CI_FABRIC_TOOLS_INSTALL=PASS"

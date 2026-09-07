#!/bin/bash
set -euo pipefail

BASE_SOURCE=${CODEX_LAB_VM_BASE_SOURCE:-/tank/vm/codex-lab/codex-lab-base-browser-v2.qcow2}
BASE_NAME=${CODEX_LAB_VM_BASE_NAME:-codex-lab-base-browser-v2.qcow2}
ROOT1=${CODEX_LAB_VM_STORAGE_ROOT_1:-/tank/vm/codex-lab-v2}
ROOT2=${CODEX_LAB_VM_STORAGE_ROOT_2:-/tank2/vm/codex-lab}
ROOT3=${CODEX_LAB_VM_STORAGE_ROOT_3:-/tank3/vm/codex-lab}
CTL_SOURCE=${CODEX_LAB_VM_CTL_SOURCE:-/tmp/vm-labctl.sh.v2}
CTL=/tank/vm/codex-lab/vm-labctl.sh
PUBKEY=${CODEX_LAB_VM_GUEST_PUBLIC_KEY:-/tmp/id_ed25519_codex_lab_vm.pub}
SCHEDULER=http://192.168.122.1:8766

for path in "$BASE_SOURCE" "$CTL_SOURCE" "$PUBKEY" "$ROOT1" "$ROOT2" "$ROOT3"; do
  [ -e "$path" ] || { echo "missing required migration input: $path" >&2; exit 2; }
done

python3 - "$SCHEDULER" <<'PY_CHECK_JOBS'
import json,sys,urllib.request
base=sys.argv[1]
for status in ('running','queued'):
    with urllib.request.urlopen(f'{base}/api/jobs?status={status}&limit=200', timeout=5) as response:
        jobs=json.load(response)['jobs']
    if jobs:
        raise SystemExit(f'refusing migration: {len(jobs)} scheduler jobs are {status}')
PY_CHECK_JOBS

systemctl --user stop codex-lab-scheduler.service
trap 'systemctl --user start codex-lab-scheduler.service >/dev/null 2>&1 || true' EXIT
install -m 0700 "$CTL_SOURCE" "$CTL"

copy_base() {
  local dest=$1 tmp="$1/$BASE_NAME.tmp"
  rm -f "$tmp"
  cp --sparse=always "$BASE_SOURCE" "$tmp"
  qemu-img check "$tmp" >/dev/null
  mv "$tmp" "$dest/$BASE_NAME"
  chmod 0664 "$dest/$BASE_NAME"
}
copy_base "$ROOT1" & p1=$!
copy_base "$ROOT2" & p2=$!
copy_base "$ROOT3" & p3=$!
wait "$p1" "$p2" "$p3"

key=$(cat "$PUBKEY")
for station in 1 2 3 4 5 6; do
  CODEX_LAB_VM_MAX_STATIONS=6 CODEX_LAB_VM_PREWARM=6 "$CTL" reset "$station" "$key"
done

status=$(CODEX_LAB_VM_MAX_STATIONS=6 CODEX_LAB_VM_PREWARM=6 "$CTL" status)
printf '%s\n' "$status"
STATUS_TEXT="$status" python3 - <<'PY_CHECK_STATUS'
import os
rows=[line.split('\t') for line in os.environ['STATUS_TEXT'].splitlines()[1:] if line.strip()]
if len(rows) != 6:
    raise SystemExit(f'expected 6 worker rows, got {len(rows)}')
for station,name,ip,state,auto in rows:
    if state != 'running' or auto != 'enable':
        raise SystemExit(f'worker {station} not ready at libvirt layer: state={state} autostart={auto}')
PY_CHECK_STATUS

systemctl --user start codex-lab-scheduler.service
trap - EXIT
printf 'six-kvm-libvirt-cutover-complete\n'

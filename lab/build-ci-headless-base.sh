#!/usr/bin/env bash
set -euo pipefail

URI=${CODEX_CI_LIBVIRT_URI:-qemu:///system}
NETWORK=${CODEX_CI_NETWORK:-default}
SOURCE=${CODEX_CI_BASE_SOURCE:-/tank/vm/codex-lab-v2/codex-lab-base-browser-v2.qcow2}
OUT_NAME=${CODEX_CI_HEADLESS_BASE:-codex-ci-base-v1.qcow2}
STORES=${CODEX_CI_HEADLESS_STORAGE_ROOTS:-/tank/vm/codex-ci-headless,/tank2/vm/codex-ci-headless,/tank3/vm/codex-ci-headless}
RPC=${CODEX_CI_WORKER_RPC:-/home/mark/.local/share/codex-ci/codex-ci-worker-rpc.py}
CI_PUB=${CODEX_CI_PUBLIC_KEY:-/home/mark/.local/share/codex-ci/ssh/id_ed25519_codex_ci.pub}
ADMIN_KEY=${CODEX_LAB_ADMIN_KEY:-/home/mark/.ssh/id_ed25519_codex_lab_vm}
BUILDER=codex-ci-base-builder
IP=192.168.122.239
MAC=52:54:00:ce:00:ef
TMP=/tank/vm/codex-ci-headless-builder
OVERLAY=$TMP/builder.qcow2
FLAT=$TMP/$OUT_NAME

fail(){ echo "ERROR: $*" >&2; exit 1; }
[[ $(hostname) == home-server ]] || fail "must run on home-server"
[[ $(id -un) == mark ]] || fail "must run as mark"
for f in "$SOURCE" "$RPC" "$CI_PUB" "$ADMIN_KEY"; do [[ -r $f ]] || fail "missing $f"; done
for c in virsh virt-install qemu-img ssh; do command -v "$c" >/dev/null || fail "missing $c"; done
mkdir -p "$TMP"

cleanup(){
  virsh --connect "$URI" destroy "$BUILDER" >/dev/null 2>&1 || true
  virsh --connect "$URI" undefine "$BUILDER" >/dev/null 2>&1 || true
  virsh --connect "$URI" net-update "$NETWORK" delete ip-dhcp-host "<host mac='$MAC' name='$BUILDER' ip='$IP'/>" --live --config >/dev/null 2>&1 || true
}
trap cleanup EXIT
cleanup
rm -f "$OVERLAY" "$FLAT"
qemu-img create -q -f qcow2 -F qcow2 -b "$SOURCE" "$OVERLAY" 100G
virsh --connect "$URI" net-update "$NETWORK" add ip-dhcp-host "<host mac='$MAC' name='$BUILDER' ip='$IP'/>" --live --config >/dev/null
virt-install --connect "$URI" --name "$BUILDER" --memory 4096 --vcpus 4 --cpu host-passthrough \
  --disk "path=$OVERLAY,format=qcow2,bus=virtio" --network "network=$NETWORK,model=virtio,mac=$MAC" \
  --os-variant debian11 --graphics none --noautoconsole --import >/dev/null

ssh_admin(){ ssh -i "$ADMIN_KEY" -o IdentitiesOnly=yes -o BatchMode=yes -o UserKnownHostsFile=/dev/null -o StrictHostKeyChecking=no -o ConnectTimeout=5 "mark@$IP" "$@"; }
ready=0
for _ in $(seq 1 90); do if ssh_admin 'true' >/dev/null 2>&1; then ready=1; break; fi; sleep 2; done
[[ $ready == 1 ]] || fail "builder SSH did not become ready"
ssh_admin 'mkdir -p ~/.local/bin ~/.local/share/codex-ci ~/.ssh; chmod 700 ~/.ssh; cat > ~/.local/bin/codex-ci-worker-rpc.py; chmod 700 ~/.local/bin/codex-ci-worker-rpc.py' < "$RPC"
KEY_BLOB=$(awk '{print $2}' "$CI_PUB")
FORCED="restrict,command=\"/home/mark/.local/bin/codex-ci-worker-rpc.py\" $(cat "$CI_PUB")"
FORCED_B64=$(printf '%s' "$FORCED" | base64 -w0)
ssh_admin "python3 - '$KEY_BLOB' '$FORCED_B64'" <<'PY'
import base64,pathlib,sys
blob,raw=sys.argv[1:]
line=base64.b64decode(raw).decode()
p=pathlib.Path.home()/'.ssh/authorized_keys'; p.touch(mode=0o600,exist_ok=True)
rows=[x for x in p.read_text().splitlines() if blob not in x]; rows.append(line)
p.write_text('\n'.join(rows)+'\n'); p.chmod(0o600)
PY
ssh_admin 'mkdir -p ~/.local/share/codex-ci; : > ~/.local/share/codex-ci/headless-v1; rm -f ~/.local/share/codex-worker/claim.lock ~/.local/share/codex-worker/exclusive.lock ~/.local/share/codex-worker/scheduler.lock; rm -rf /workspace/ci-dispatch/* 2>/dev/null || true; rm -f ~/.config/systemd/user/default.target.wants/codex-worker-desktop.service; systemctl --user disable --now codex-worker-desktop.service >/dev/null 2>&1 || true; pkill -f "chromium.*remote-debugging-port=9222" >/dev/null 2>&1 || true; pkill -f "Xvfb|x11vnc|websockify" >/dev/null 2>&1 || true; sync' || true
virsh --connect "$URI" shutdown "$BUILDER" >/dev/null || true
for _ in $(seq 1 60); do state=$(virsh --connect "$URI" domstate "$BUILDER" 2>/dev/null || true); [[ $state != running ]] && break; sleep 1; done
virsh --connect "$URI" destroy "$BUILDER" >/dev/null 2>&1 || true
qemu-img convert -p -O qcow2 "$OVERLAY" "$FLAT"
IFS=',' read -ra roots <<< "$STORES"
for root in "${roots[@]}"; do root=${root// /}; mkdir -p "$root"; tmp="$root/$OUT_NAME.tmp"; cp --reflink=auto "$FLAT" "$tmp"; chmod 664 "$tmp"; mv -f "$tmp" "$root/$OUT_NAME"; done
sha256sum "$FLAT" "${roots[0]// /}/$OUT_NAME"
echo "headless_base_ready=$OUT_NAME"

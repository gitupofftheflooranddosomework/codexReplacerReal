#!/bin/sh
set -eu

CONNECT=${CODEX_LAB_LIBVIRT_URI:-qemu:///system}
CONTROL_ROOT=${CODEX_LAB_VM_ROOT:-/tank/vm/codex-lab}
STORAGE_ROOT_1=${CODEX_LAB_VM_STORAGE_ROOT_1:-/tank/vm/codex-lab-v2}
STORAGE_ROOT_2=${CODEX_LAB_VM_STORAGE_ROOT_2:-/tank2/vm/codex-lab}
STORAGE_ROOT_3=${CODEX_LAB_VM_STORAGE_ROOT_3:-/tank3/vm/codex-lab}
BASE_NAME=${CODEX_LAB_VM_BASE_NAME:-codex-lab-base-browser-v2.qcow2}
NETWORK=${CODEX_LAB_VM_NETWORK:-default}
MEMORY_MIB=${CODEX_LAB_VM_MEMORY_MIB:-4096}
MAX_MEMORY_MIB=${CODEX_LAB_VM_MAX_MEMORY_MIB:-8192}
VCPUS=${CODEX_LAB_VM_VCPUS:-4}
DISK_GIB=${CODEX_LAB_VM_DISK_GIB:-100}
MAX_STATIONS=${CODEX_LAB_VM_MAX_STATIONS:-9}
PREWARM=${CODEX_LAB_VM_PREWARM:-9}
SSH_READY_SECONDS=${CODEX_LAB_VM_SSH_READY_SECONDS:-180}
ADMIN_KEY=${CODEX_LAB_ADMIN_KEY:-/home/mark/.ssh/id_ed25519_codex_lab_vm}
ADMIN_KNOWN=${CODEX_LAB_ADMIN_KNOWN_HOSTS:-/home/mark/.ssh/codex_lab_scheduler_known_hosts}

authorize_controller() {
  encoded=$(printf '%s' "$pubkey" | base64 -w0)
  deadline=$(( $(date +%s) + SSH_READY_SECONDS ))
  while :; do
    if timeout 10 ssh -i "$ADMIN_KEY" -o IdentitiesOnly=yes -o BatchMode=yes \
      -o ConnectTimeout=5 -o "UserKnownHostsFile=$ADMIN_KNOWN" -o StrictHostKeyChecking=accept-new \
      "mark@$IP" python3 - "$encoded" <<'PY'
import base64, pathlib, sys
key = base64.b64decode(sys.argv[1]).decode().strip()
parts = key.split()
if len(parts) < 2 or parts[0] not in ('ssh-ed25519', 'ssh-rsa', 'ecdsa-sha2-nistp256'):
    raise ValueError('Invalid controller public key')
p = pathlib.Path.home() / '.ssh/authorized_keys'
content = p.read_text() if p.exists() else ''
if not any(parts[1] in line.split() for line in content.splitlines()):
    with p.open('a') as output:
        output.write('\n' + key + '\n')
    p.chmod(0o600)
PY
    then
      return 0
    fi
    now=$(date +%s)
    if [ "$now" -ge "$deadline" ]; then
      echo "$NAME at $IP did not become SSH-ready within ${SSH_READY_SECONDS}s" >&2
      return 1
    fi
    sleep 2
  done
}

station_values() {
  n=$1
  [ "$n" -ge 1 ] && [ "$n" -le "$MAX_STATIONS" ] || { echo "station must be 1..$MAX_STATIONS" >&2; exit 2; }
  NAME=$(printf 'codex-lab-vm-%02d' "$n")
  IP=$(printf '192.168.122.%d' $((229 + n)))
  MAC=$(printf '52:54:00:ca:db:%02x' "$n")
  case $(( (n - 1) % 3 )) in
    0) STORAGE_ROOT="$STORAGE_ROOT_1" ;;
    1) STORAGE_ROOT="$STORAGE_ROOT_2" ;;
    2) STORAGE_ROOT="$STORAGE_ROOT_3" ;;
  esac
  BASE="$STORAGE_ROOT/$BASE_NAME"
  DIR="$STORAGE_ROOT/$NAME"
  DISK="$DIR/$NAME.qcow2"
  NETPLAN="$DIR/50-codex-lab.yaml"
  CLOUD_DISABLE="$DIR/99-disable-network-config.cfg"
}

exists() {
  virsh --connect "$CONNECT" dominfo "$NAME" >/dev/null 2>&1
}

ensure_dhcp() {
  if ! virsh --connect "$CONNECT" net-dumpxml "$NETWORK" | grep -Fq "$MAC"; then
    virsh --connect "$CONNECT" net-update "$NETWORK" add ip-dhcp-host \
      "<host mac='$MAC' name='$NAME' ip='$IP'/>" --live --config >/dev/null
  fi
}

write_guest_network() {
  cat >"$NETPLAN" <<EOF
network:
  version: 2
  ethernets:
    enp1s0:
      dhcp4: true
      dhcp6: false
EOF
  cat >"$CLOUD_DISABLE" <<EOF
network: {config: disabled}
EOF
}

create_station() {
  n=$1
  pubkey=$2
  station_values "$n"
  mkdir -p "$DIR"
  ensure_dhcp
  if exists; then
    state=$(virsh --connect "$CONNECT" domstate "$NAME" | tr -d '\r')
    [ "$state" = "running" ] || virsh --connect "$CONNECT" start "$NAME" >/dev/null
  else
    [ -f "$BASE" ] || { echo "base image missing: $BASE" >&2; exit 3; }
    [ -f "$DISK" ] || qemu-img create -q -f qcow2 -F qcow2 -b "$BASE" "$DISK" "${DISK_GIB}G"
    virt-install --connect "$CONNECT" \
      --name "$NAME" --memory "memory=${MEMORY_MIB},maxmemory=${MAX_MEMORY_MIB}" --vcpus "$VCPUS" --cpu host-passthrough \
      --disk "path=$DISK,format=qcow2,bus=virtio" \
      --network "network=$NETWORK,model=virtio,mac=$MAC" \
      --os-variant debian11 --graphics none --noautoconsole --import >/dev/null
  fi
  if [ "$n" -le "$PREWARM" ]; then virsh --connect "$CONNECT" autostart "$NAME" >/dev/null; fi
  # Rebuilt images contain the host administrator key, but may predate the
  # current broker key. Wait for a fresh clone to finish booting before
  # installing the controller key rather than treating first-boot latency as
  # provisioning failure.
  authorize_controller
  printf '%s\t%s\t%s\n' "$NAME" "$IP" "$(virsh --connect "$CONNECT" domstate "$NAME" | tr -d '\r')"
}

reset_station() {
  n=$1
  pubkey=$2
  station_values "$n"
  if exists; then
    virsh --connect "$CONNECT" destroy "$NAME" >/dev/null 2>&1 || true
    virsh --connect "$CONNECT" undefine "$NAME" >/dev/null 2>&1 || true
  fi
  stamp=$(date -u +%Y%m%dT%H%M%SZ)
  if [ -d "$DIR" ]; then
    mv "$DIR" "$STORAGE_ROOT/archive-$NAME-$stamp"
  fi
  LEGACY_DIR="$CONTROL_ROOT/$NAME"
  if [ "$LEGACY_DIR" != "$DIR" ] && [ -d "$LEGACY_DIR" ]; then
    mv "$LEGACY_DIR" "$CONTROL_ROOT/archive-$NAME-legacy-$stamp"
  fi
  if [ "$n" -le "$PREWARM" ]; then create_station "$n" "$pubkey"; fi
}

case ${1:-} in
  ensure)
    [ $# -eq 3 ] || { echo "usage: $0 ensure STATION PUBLIC_KEY" >&2; exit 2; }
    create_station "$2" "$3"
    ;;
  reset)
    [ $# -eq 3 ] || { echo "usage: $0 reset STATION PUBLIC_KEY" >&2; exit 2; }
    reset_station "$2" "$3"
    ;;
  start)
    [ $# -eq 2 ] || exit 2
    station_values "$2"; virsh --connect "$CONNECT" start "$NAME" >/dev/null; echo "$NAME"
    ;;
  stop)
    [ $# -eq 2 ] || exit 2
    station_values "$2"; virsh --connect "$CONNECT" shutdown "$NAME" >/dev/null; echo "$NAME"
    ;;
  status)
    printf 'station\tname\tip\tstate\tautostart\n'
    i=1
    while [ "$i" -le "$MAX_STATIONS" ]; do
      station_values "$i"
      if exists; then
        state=$(virsh --connect "$CONNECT" domstate "$NAME" | tr -d '\r')
        auto=$(virsh --connect "$CONNECT" dominfo "$NAME" | awk -F: '/^Autostart/{gsub(/^[ \t]+/,"",$2); print $2}')
      else
        state=absent; auto=no
      fi
      printf '%s\t%s\t%s\t%s\t%s\n' "$i" "$NAME" "$IP" "$state" "$auto"
      i=$((i + 1))
    done
    ;;
  *)
    echo "usage: $0 {ensure STATION PUBLIC_KEY|reset STATION PUBLIC_KEY|start STATION|stop STATION|status}" >&2
    exit 2
    ;;
esac

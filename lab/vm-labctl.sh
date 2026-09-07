#!/bin/sh
set -eu

CONNECT=${CODEX_LAB_LIBVIRT_URI:-qemu:///system}
ROOT=${CODEX_LAB_VM_ROOT:-/tank/vm/codex-lab}
BASE=${CODEX_LAB_VM_BASE:-/tank/vm/codex-lab/codex-lab-base.qcow2}
NETWORK=${CODEX_LAB_VM_NETWORK:-default}
MEMORY_MIB=${CODEX_LAB_VM_MEMORY_MIB:-8192}
VCPUS=${CODEX_LAB_VM_VCPUS:-4}
DISK_GIB=${CODEX_LAB_VM_DISK_GIB:-100}
MAX_STATIONS=${CODEX_LAB_VM_MAX_STATIONS:-4}
PREWARM=${CODEX_LAB_VM_PREWARM:-2}

station_values() {
  n=$1
  [ "$n" -ge 1 ] && [ "$n" -le "$MAX_STATIONS" ] || { echo "station must be 1..$MAX_STATIONS" >&2; exit 2; }
  NAME=$(printf 'codex-lab-vm-%02d' "$n")
  IP=$(printf '192.168.122.%d' $((229 + n)))
  MAC=$(printf '52:54:00:ca:db:%02x' "$n")
  DIR="$ROOT/$NAME"
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
    write_guest_network
    virt-customize -q -a "$DISK" \
      --hostname "$NAME" \
      --upload "$NETPLAN:/etc/netplan/50-codex-lab.yaml" \
      --upload "$CLOUD_DISABLE:/etc/cloud/cloud.cfg.d/99-disable-network-config.cfg" \
      --chmod 0600:/etc/netplan/50-codex-lab.yaml \
      --touch /etc/cloud/cloud-init.disabled \
      --run-command 'id mark >/dev/null 2>&1 || useradd -m -s /bin/bash mark' \
      --run-command 'usermod -aG sudo mark' \
      --run-command "printf '%s\\n' 'mark ALL=(ALL) NOPASSWD:ALL' >/etc/sudoers.d/90-codex-lab" \
      --run-command 'chmod 0440 /etc/sudoers.d/90-codex-lab' \
      --run-command 'ssh-keygen -A' \
      --run-command 'systemctl enable ssh >/dev/null 2>&1 || true' \
      --ssh-inject "mark:string:$pubkey" \
      --mkdir /workspace \
      --run-command 'chown mark:mark /workspace'
    virt-install --connect "$CONNECT" \
      --name "$NAME" --memory "$MEMORY_MIB" --vcpus "$VCPUS" --cpu host-passthrough \
      --disk "path=$DISK,format=qcow2,bus=virtio" \
      --network "network=$NETWORK,model=virtio,mac=$MAC" \
      --os-variant debian11 --graphics none --noautoconsole --import >/dev/null
  fi
  if [ "$n" -le "$PREWARM" ]; then virsh --connect "$CONNECT" autostart "$NAME" >/dev/null; fi
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
  if [ -d "$DIR" ]; then
    stamp=$(date -u +%Y%m%dT%H%M%SZ)
    mv "$DIR" "$ROOT/archive-$NAME-$stamp"
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

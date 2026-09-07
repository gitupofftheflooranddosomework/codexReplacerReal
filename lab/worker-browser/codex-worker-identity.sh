#!/bin/bash
set -euo pipefail

iface=${CODEX_WORKER_INTERFACE:-enp1s0}
mac_file="/sys/class/net/$iface/address"
for _ in $(seq 1 100); do
  [ -r "$mac_file" ] && break
  sleep 0.05
done
mac=$(cat "$mac_file")
last=${mac##*:}
station=$((16#$last))
printf -v name 'codex-lab-vm-%02d' "$station"
hostnamectl set-hostname "$name"

marker=/var/lib/codex-worker-identity-initialized
if [ ! -e "$marker" ]; then
  rm -f /etc/ssh/ssh_host_*
  ssh-keygen -A
  touch "$marker"
fi

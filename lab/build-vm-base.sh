#!/bin/bash
set -euo pipefail

CONTROL_ROOT=${CODEX_LAB_VM_ROOT:-/tank/vm/codex-lab}
SOURCE=${CODEX_LAB_VM_SOURCE_BASE:-$CONTROL_ROOT/codex-lab-base.qcow2}
BASE_NAME=${CODEX_LAB_VM_BASE_NAME:-codex-lab-base-browser-v2.qcow2}
DEST1=${CODEX_LAB_VM_STORAGE_ROOT_1:-/tank/vm/codex-lab-v2}
BUILD_ROOT=${CODEX_LAB_VM_BUILD_ROOT:-$DEST1}
DEST2=${CODEX_LAB_VM_STORAGE_ROOT_2:-/tank2/vm/codex-lab}
DEST3=${CODEX_LAB_VM_STORAGE_ROOT_3:-/tank3/vm/codex-lab}
PUBLIC_KEY_FILE=${CODEX_LAB_VM_GUEST_PUBLIC_KEY:-/tmp/id_ed25519_codex_lab_vm.pub}
ASSET_ROOT=${CODEX_LAB_VM_ASSET_ROOT:-/tmp}
TMP="$BUILD_ROOT/.${BASE_NAME}.tmp"
FINAL="$BUILD_ROOT/$BASE_NAME"

for required in "$SOURCE" "$PUBLIC_KEY_FILE" "$ASSET_ROOT/codex-worker-desktop.sh.v2" "$ASSET_ROOT/codex-worker-desktop.service.v2" "$ASSET_ROOT/codex-worker-identity.sh" "$ASSET_ROOT/codex-worker-identity.service" "$ASSET_ROOT/codex-workstation-bootstrap.sh"; do
  [ -e "$required" ] || { echo "missing required input: $required" >&2; exit 2; }
done

mkdir -p "$BUILD_ROOT"
rm -f "$TMP"
qemu-img convert -p -O qcow2 -o compat=1.1,lazy_refcounts=on "$SOURCE" "$TMP"
PUB=$(cat "$PUBLIC_KEY_FILE")

# Critical performance rule: use libvirt so the appliance gets /dev/kvm through
# libvirt rather than silently falling back to software TCG under the mark user.
LIBGUESTFS_BACKEND=libvirt virt-customize --smp 4 --memsize 4096 -a "$TMP" \
  --install chromium,xvfb,x11vnc,novnc,websockify,openbox,dbus-x11,x11-utils,fonts-liberation \
  --run-command "id mark >/dev/null 2>&1 || useradd -m -s /bin/bash mark" \
  --run-command "usermod -aG sudo mark" \
  --run-command "printf '%s\\n' 'mark ALL=(ALL) NOPASSWD:ALL' >/etc/sudoers.d/90-codex-lab" \
  --run-command "chmod 0440 /etc/sudoers.d/90-codex-lab" \
  --ssh-inject "mark:string:$PUB" \
  --run-command "mkdir -p /usr/local/libexec" \
  --upload "$ASSET_ROOT/codex-worker-desktop.sh.v2:/usr/local/libexec/codex-worker-desktop" \
  --upload "$ASSET_ROOT/codex-worker-desktop.service.v2:/etc/systemd/system/codex-worker-desktop.service" \
  --upload "$ASSET_ROOT/codex-worker-identity.sh:/usr/local/libexec/codex-worker-identity" \
  --upload "$ASSET_ROOT/codex-worker-identity.service:/etc/systemd/system/codex-worker-identity.service" \
  --upload "$ASSET_ROOT/codex-workstation-bootstrap.sh:/tmp/codex-workstation-bootstrap.sh" \
  --chmod 0755:/usr/local/libexec/codex-worker-desktop \
  --chmod 0755:/tmp/codex-workstation-bootstrap.sh \
  --chmod 0755:/usr/local/libexec/codex-worker-identity \
  --run-command "mkdir -p /workspace /home/mark/.local/share/codex-worker/browser /home/mark/.local/share/codex-worker/jobs /home/mark/.cache/codex-worker/git-mirrors" \
  --run-command "chown -R mark:mark /workspace /home/mark/.local /home/mark/.cache" \
  --run-command "/tmp/codex-workstation-bootstrap.sh" \
  --run-command "touch /etc/cloud/cloud-init.disabled" \
  --run-command "systemctl enable ssh.service codex-worker-identity.service codex-worker-desktop.service" \
  --run-command "rm -f /etc/ssh/ssh_host_* /var/lib/codex-worker-identity-initialized /var/lib/dbus/machine-id" \
  --run-command ": >/etc/machine-id" \
  --run-command "touch /var/lib/codex-worker-browser-v2 /var/lib/codex-lab-ready"

LIBGUESTFS_BACKEND=libvirt virt-cat -a "$TMP" /var/lib/codex-worker-browser-v2 >/dev/null
qemu-img check "$TMP"
mv "$TMP" "$FINAL"
chmod 0664 "$FINAL"

mkdir -p "$DEST1" "$DEST2" "$DEST3"
# Replicate in parallel so independent ZFS pools are written concurrently.
pids=()
for dest in "$DEST2" "$DEST3"; do
  cp --reflink=auto --sparse=always "$FINAL" "$dest/$BASE_NAME.tmp" &
  pids+=("$!")
done
for pid in "${pids[@]}"; do wait "$pid"; done
for dest in "$DEST2" "$DEST3"; do mv "$dest/$BASE_NAME.tmp" "$dest/$BASE_NAME"; chmod 0664 "$dest/$BASE_NAME"; done

hash=$(sha256sum "$FINAL" | awk '{print $1}')
for path in "$DEST2/$BASE_NAME" "$DEST3/$BASE_NAME"; do
  [ "$(sha256sum "$path" | awk '{print $1}')" = "$hash" ] || { echo "base checksum mismatch: $path" >&2; exit 3; }
done
printf 'base=%s sha256=%s\n' "$FINAL" "$hash"

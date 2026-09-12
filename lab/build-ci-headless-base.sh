#!/usr/bin/env bash
set -euo pipefail

URI=${CODEX_CI_LIBVIRT_URI:-qemu:///system}
NETWORK=${CODEX_CI_NETWORK:-default}
SOURCE=${CODEX_CI_BASE_SOURCE:-/tank/vm/codex-lab-v2/codex-lab-base-browser-v2.qcow2}
OUT_NAME=${CODEX_CI_HEADLESS_BASE:-codex-ci-base-v4.qcow2}
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
for c in virsh virt-install qemu-img ssh virt-customize; do command -v "$c" >/dev/null || fail "missing $c"; done
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
virt-install --connect "$URI" --name "$BUILDER" --memory 4096 --vcpus 2 --cpu host-passthrough \
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

# The DotMoose CI contract requires Node 22.x (>=22.12) and PHP 8.4. Debian
# bookworm does not provide PHP 8.4 natively, and the browser source image can
# carry a newer Node major. Configure signed upstream package sources in the
# disposable builder, install the exact supported majors, and fail the image
# build unless the active runtimes match the contract.
ssh_admin 'bash -s' <<'BUILDER_BOOTSTRAP'
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
sudo -n apt-get update -qq
sudo -n apt-get install -y --no-install-recommends ca-certificates curl gnupg

curl -fsSL https://deb.nodesource.com/setup_22.x -o /tmp/nodesource_setup.sh
sudo -n -E bash /tmp/nodesource_setup.sh

curl -fsSL https://packages.sury.org/debsuryorg-archive-keyring.deb -o /tmp/debsuryorg-archive-keyring.deb
sudo -n dpkg -i /tmp/debsuryorg-archive-keyring.deb
printf '%s\n' 'deb [signed-by=/usr/share/keyrings/debsuryorg-archive-keyring.gpg] https://packages.sury.org/php/ bookworm main' \
  | sudo -n tee /etc/apt/sources.list.d/php-sury.list >/dev/null
sudo -n apt-get update -qq

sudo -n apt-get install -y --allow-downgrades --no-install-recommends \
  nodejs \
  python3 python3-dev python3-venv python3-pip python3-setuptools python3-wheel \
  python3-reportlab python3-pytest python3-requests python3-yaml \
  php8.4-cli php8.4-common php8.4-curl php8.4-mbstring php8.4-xml php8.4-zip \
  php8.4-intl php8.4-sqlite3 php8.4-mysql php8.4-pgsql \
  composer dnsutils

sudo -n update-alternatives --set php /usr/bin/php8.4
node -e 'const [major, minor] = process.versions.node.split(".").map(Number); if (major !== 22 || minor < 12) process.exit(1)'
php -r 'if (PHP_MAJOR_VERSION !== 8 || PHP_MINOR_VERSION !== 4) { fwrite(STDERR, PHP_VERSION."\n"); exit(1); } echo "php84_runtime_ok\n";'
command -v composer dig >/dev/null
python3 -c 'import reportlab, requests, yaml; print("python_runtime_ok")'

sudo -n touch /var/lib/codex-ci-toolchain-v4
sudo -n apt-get clean
sudo -n rm -rf /var/lib/apt/lists/*
rm -f /tmp/nodesource_setup.sh /tmp/debsuryorg-archive-keyring.deb
BUILDER_BOOTSTRAP

ssh_admin 'mkdir -p ~/.local/share/codex-ci; : > ~/.local/share/codex-ci/headless-v1; rm -f ~/.local/share/codex-worker/claim.lock ~/.local/share/codex-worker/exclusive.lock ~/.local/share/codex-worker/scheduler.lock; rm -rf /workspace/ci-dispatch/* 2>/dev/null || true; rm -f ~/.config/systemd/user/default.target.wants/codex-worker-desktop.service; systemctl --user disable --now codex-worker-desktop.service >/dev/null 2>&1 || true; pkill -f "chromium.*remote-debugging-port=9222" >/dev/null 2>&1 || true; pkill -f "Xvfb|x11vnc|websockify" >/dev/null 2>&1 || true; sync' || true
virsh --connect "$URI" shutdown "$BUILDER" >/dev/null || true
for _ in $(seq 1 60); do state=$(virsh --connect "$URI" domstate "$BUILDER" 2>/dev/null || true); [[ $state != running ]] && break; sleep 1; done
virsh --connect "$URI" destroy "$BUILDER" >/dev/null 2>&1 || true
qemu-img convert -p -O qcow2 "$OVERLAY" "$FLAT"
qemu-img check "$FLAT"

# The online builder acquires a machine-id and DHCP client identity. Never
# publish those values into a clone template: systemd-networkd derives its
# DHCP client identifier from machine-id, so cloned guests can collide even
# when libvirt assigns each one a unique MAC and reservation.
LIBGUESTFS_BACKEND=direct virt-customize -a "$FLAT" \
  --run-command 'truncate -s 0 /etc/machine-id' \
  --run-command 'rm -f /var/lib/dbus/machine-id' \
  --run-command 'rm -f /var/lib/dhcp/* /var/lib/NetworkManager/*lease* /var/lib/systemd/network/* 2>/dev/null || true' \
  --run-command 'rm -f /etc/udev/rules.d/70-persistent-net.rules 2>/dev/null || true' \
  --run-command 'sync'

IFS=',' read -ra roots <<< "$STORES"
for root in "${roots[@]}"; do root=${root// /}; mkdir -p "$root"; tmp="$root/$OUT_NAME.tmp"; cp --reflink=auto "$FLAT" "$tmp"; chmod 664 "$tmp"; mv -f "$tmp" "$root/$OUT_NAME"; done
hash=$(sha256sum "$FLAT" | awk '{print $1}')
for root in "${roots[@]}"; do
  root=${root// /}
  [[ $(sha256sum "$root/$OUT_NAME" | awk '{print $1}') == "$hash" ]] || fail "base checksum mismatch: $root/$OUT_NAME"
done
printf 'base=%s sha256=%s\n' "$OUT_NAME" "$hash"
echo "headless_base_ready=$OUT_NAME"

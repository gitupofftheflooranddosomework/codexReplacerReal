#!/bin/bash
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y --no-install-recommends \
  build-essential cmake ninja-build pkg-config clang gdb \
  git git-lfs curl wget ca-certificates gnupg jq ripgrep fd-find \
  python3 python3-dev python3-venv python3-pip pipx \
  golang-go rustc cargo default-jdk-headless \
  php-cli composer \
  tmux screen rsync zip unzip p7zip-full shellcheck sqlite3 \
  postgresql-client mariadb-client redis-tools \
  dnsutils iputils-ping netcat-openbsd lsof strace htop btop tree file less nano vim \
  libssl-dev libffi-dev \
  xterm xfce4-terminal thunar tint2 dbus-x11
systemctl enable --now docker.service >/dev/null 2>&1 || true
usermod -aG docker,sudo mark
install -d -o mark -g mark -m 0755 /workspace /home/mark/.config/openbox /home/mark/bin
cat >/home/mark/.config/openbox/menu.xml <<'XML'
<?xml version="1.0" encoding="UTF-8"?>
<openbox_menu xmlns="http://openbox.org/3.4/menu">
  <menu id="root-menu" label="Codex Worker">
    <item label="Terminal"><action name="Execute"><command>xfce4-terminal</command></action></item>
    <item label="File Manager"><action name="Execute"><command>thunar</command></action></item>
    <item label="Chromium"><action name="Execute"><command>chromium</command></action></item>
    <separator />
    <item label="Reconfigure"><action name="Reconfigure" /></item>
  </menu>
</openbox_menu>
XML
chown -R mark:mark /home/mark/.config/openbox /home/mark/bin
cat >/etc/profile.d/codex-worker.sh <<'PROFILE'
export EDITOR=vim
export VISUAL=vim
export PATH="$HOME/.local/bin:$HOME/bin:$PATH"
alias ll='ls -alF'
alias fd='fdfind'
PROFILE
chmod 0644 /etc/profile.d/codex-worker.sh
touch /var/lib/codex-worker-workstation-v1
apt-get clean
rm -rf /var/lib/apt/lists/*

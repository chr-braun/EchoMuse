#!/usr/bin/env bash
# Bereitet einen Raspberry Pi / Debian-Host für das Echo-Dot-Unlock vor.
# Auf dem MAC ausführen:  ./setup-pi.sh <pi-user>@<pi-ip>
# Danach auf dem PI:      cd EchoMuse && python3 dot-guide.py
set -euo pipefail
[ $# -ge 1 ] || { echo "Usage: $0 user@host  (z.B. root@192.168.178.76)"; exit 1; }
TARGET="$1"

echo "▶ Prüfe Erreichbarkeit …"
ssh "$TARGET" "uname -sm" || { echo "✗ SSH fehlgeschlagen"; exit 1; }

echo "▶ Installiere adb, fastboot, unzip, git, python3 auf dem Pi …"
ssh "$TARGET" "sudo apt-get update -qq && sudo apt-get install -y -qq android-tools-adb android-tools-fastboot unzip git python3 usbutils"

echo "▶ Deaktiviere USB-Autosuspend (sonst reißt ADB beim Unlock ab) …"
ssh "$TARGET" "echo -1 | sudo tee /sys/bus/usb/devices/*/power/autosuspend >/dev/null 2>&1 || true"
ssh "$TARGET" "grep -q autosuspend=-1 /etc/rc.local 2>/dev/null || \
  echo 'echo -1 | tee /sys/bus/usb/devices/*/power/autosuspend > /dev/null' | sudo tee -a /etc/rc.local >/dev/null 2>&1 || true"

echo "▶ Klone EchoMuse (Branch mit allen Fixes + Guides) …"
ssh "$TARGET" "command -v git && test -d ~/EchoMuse || git clone -b install/integration https://github.com/chr-braun/EchoMuse.git ~/EchoMuse"
ssh "$TARGET" "cd ~/EchoMuse && git pull --ff-only 2>/dev/null || true"

cat <<EOF

✅ Pi bereit:  $TARGET

Weiter so:
  1. Die 4 Dateien herunterladen (amonet-biscuit-v1.1.0.zip,
     update-kindle-csm_biscuit-272.6.8.0_user_680767620.bin,
     f1r30s.zip, Magisk-v17.3.zip)
  2. Auf den Pi kopieren:
     scp amonet*.zip FireOS*.bin f1r30s.zip Magisk*.zip $TARGET:~/EchoMuse/
  3. Den Dot per USB an den PI anschließen (nicht an den Mac!)
  4. SSH auf den Pi und Guide starten:
        ssh $TARGET
        cd ~/EchoMuse && python3 dot-guide.py
     Der Guide erkennt Linux, führt brick.sh aus und begleitet alles
     bis zum Dashboard.
EOF

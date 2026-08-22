#!/usr/bin/env bash
# vm-unlock.sh — Mini-Linux-VM auf dem Mac für das Echo-Dot-Unlock.
#
#   ./vm-unlock.sh setup    QEMU + Debian-Mini-VM einrichten und starten
#   ./vm-unlock.sh attach   Angesteckten Echo-Dot in die VM durchreichen
#   ./vm-unlock.sh detach   Dot aus der VM entfernen
#   ./vm-unlock.sh guide    SSH in die VM, startet dot-guide.py dort
#   ./vm-unlock.sh status   VM-/USB-Status
#   ./vm-unlock.sh stop     VM beenden
#
# Die VM ist Debian 12 genericcloud (~400MB), headless (QEMU/HVF),
# SSH über Port 2222, USB-Hotplug über den QEMU-Monitor-Socket.
set -euo pipefail
cd "$(dirname "$0")"

VM_DIR="$HOME/.echomuse-vm"
DISK="$VM_DIR/debian.qcow2"
SEED="$VM_DIR/seed.iso"
MON="$VM_DIR/monitor.sock"
SSH_PORT=2222
IMG_URL="https://cloud.debian.org/images/cloud/bookworm/latest/debian-12-genericcloud-amd64.qcow2"
C_G='\033[1;32m'; C_Y='\033[1;33m'; C_R='\033[1;31m'; C_B='\033[1;36m'; C_0='\033[0m'
say(){  echo -e "${C_B}▶ $*${C_0}"; }
ok(){   echo -e "${C_G}✓ $*${C_0}"; }
warn(){ echo -e "${C_Y}⚠ $*${C_0}"; }
die(){  echo -e "${C_R}✗ $*${C_0}"; exit 1; }

vm_running(){ [ -S "$MON" ] && lsof "$MON" >/dev/null 2>&1; }

mon_cmd(){
  # Sendet einen Befehl an den QEMU-Monitor (HDB? nein: plain TCP-ähnlich über UNIX socket)
  printf '%s\n' "$1" | nc -U "$MON" | grep -v "^QEMU" || true
}

cmd_setup(){
  say "Schritt 1: QEMU installieren (Homebrew)…"
  command -v brew >/dev/null || die "Homebrew fehlt — https://brew.sh"
  command -v qemu-system-x86_64 >/dev/null || brew install qemu
  ok "QEMU $(qemu-system-x86_64 --version | grep -o 'version [^ ]*' | head -1)"

  mkdir -p "$VM_DIR"
  say "Schritt 2: Debian-12-Cloud-Image laden (~400 MB)…"
  [ -f "$DISK" ] || curl -fL --progress-bar -o "$DISK.part" "$IMG_URL" \
    && mv "$DISK.part" "$DISK"
  ok "$(basename "$DISK"): $(du -h "$DISK" | cut -f1)"

  say "Schritt 3: Cloud-Init konfigurieren (User 'echomuse', SSH-Key, Pakete)…"
  PUBKEY=""
  for k in ~/.ssh/id_ed25519.pub ~/.ssh/id_ecdsa.pub ~/.ssh/id_rsa.pub; do
    [ -f "$k" ] && PUBKEY="$(cat "$k")" && break
  done
  [ -n "$PUBKEY" ] || { ssh-keygen -t ed25519 -N "" -f ~/.ssh/id_ed25519 -q;
                        PUBKEY="$(cat ~/.ssh/id_ed25519.pub)"; warn "neuer SSH-Key erzeugt"; }

  cat > "$VM_DIR/user-data" <<EOF
#cloud-config
users:
  - name: echomuse
    sudo: ALL=(ALL) NOPASSWD:ALL
    shell: /bin/bash
    ssh_authorized_keys:
      - $PUBKEY
packages: [adb, fastboot, unzip, git, python3, usbutils]
runcmd:
  - echo -1 > /sys/bus/usb/devices/*/power/autosuspend || true
  - sed -i 's/^#\?USB_AUTOSUSPEND.*/USB_AUTOSUSPEND=0/' /etc/default/usbutils 2>/dev/null || true
growpart:
  mode: auto
EOF
  echo "instance-id: echomuse-$(date +%s)" > "$VM_DIR/meta-data"
  # seed.iso ohne Zusatzpakete bauen — hdiutil ist auf macOS vorhanden
  TMPD=$(mktemp -d); cp "$VM_DIR/user-data" "$TMPD/"; cp "$VM_DIR/meta-data" "$TMPD/"
  hdiutil makehybrid -iso -joliet -default-volume-name cidata \
     -o "$SEED" "$TMPD" >/dev/null && rm -rf "$TMPD"
  ok "seed.iso erstellt"

  say "Schritt 4: Disk vergrößern (8 GB)…"
  qemu-img resize -q "$DISK" 8G 2>/dev/null || true

  say "Schritt 5: VM starten (headless, HVF-Beschleunigung)…"
  qemu-system-x86_64 \
    -name echomuse-unlock \
    -machine q35 -accel hvf -cpu host \
    -m 2048 -smp 2 \
    -drive file="$DISK",if=virtio,format=qcow2 \
    -drive file="$SEED",media=cdrom,readonly=on \
    -device qemu-xhci,id=xhci \
    -netdev user,id=n0,hostfwd=tcp::${SSH_PORT}-:22 \
    -device virtio-net-pci,netdev=n0 \
    -display none -daemonize \
    -monitor unix:"$MON",server,nowait \
    -pidfile "$VM_DIR/qemu.pid"
  ok "VM läuft (PID $(cat "$VM_DIR/qemu.pid"))"

  say "Warte auf ersten Boot + Cloud-Init (Pakete werden installiert, ~2–5 min)…"
  for i in $(seq 1 60); do
    sleep 10
    if ssh -o StrictHostKeyChecking=no -o ConnectTimeout=5 -o BatchMode=yes \
         -p $SSH_PORT echomuse@localhost "test -e /var/lib/cloud/instance/boot-finished" 2>/dev/null; then
      ok "VM bereit! SSH: ssh -p $SSH_PORT echomuse@localhost"
      cmd_attach_hint
      return
    fi
    echo "  … noch nicht bereit (${i}/6 min)"
  done
  warn "Timeout beim Warten — Status mit './vm-unlock.sh status' prüfen."
}

cmd_attach_hint(){ cat <<'EOF'

Nächster Schritt: Echo Dot per USB an den Mac anschließen, dann
  ./vm-unlock.sh attach
Danach:  ./vm-unlock.sh guide
EOF
}

usb_amazon_device(){
  # Liefert "vendorid productid name" des ersten Amazon-Geräts am Mac-USB
  system_profiler SPUSBDataType 2>/dev/null | awk '
    /Product ID:/ {pid=$3}
    /Vendor ID:/  {vid=$3}
    /Manufacturer|amazon/i {if (vid!=""){print vid, pid}}' | head -1
}

cmd_attach(){
  vm_running || die "VM läuft nicht — erst './vm-unlock.sh setup'"
  read -p "Echo Dot jetzt per USB an den Mac anstecken, dann Enter … "
  line=$(usb_amazon_device)
  [ -n "$line" ] || die "Kein Amazon-Gerät am USB gefunden (system_profiler prüfen). Ist es der Dot?"
  set -- $line
  VID=${1#0x}; PID=${2#0x}
  say "Hänge USB-Gerät ${VID}:${PID} in die VM…"
  mon_cmd "device_del echodot" >/dev/null 2>&1 || true
  mon_cmd "device_add usb-host,vendorid=0x${VID},productid=0x${PID},id=echodot" \
    && ok "Durchgereicht." || die "device_add fehlgeschlagen."
  sleep 2
  ssh -o StrictHostKeyChecking=no -p $SSH_PORT echomuse@localhost "lsusb | grep -i -E '1949|amazon' || lsusb | tail -5" \
    && ok "In der VM sichtbar." || warn "lsusb-Zeile nicht bestätigt — im Zweifel './vm-unlock.sh status'."
  cat <<'EOF'

Jetzt den Guide IN DER VM starten:
  ./vm-unlock.sh guide
EOF
}

cmd_detach(){ vm_running || die "VM läuft nicht"; mon_cmd "device_del echodot" >/dev/null; ok "entfernt."; }

cmd_guide(){
  vm_running || die "VM läuft nicht — erst './vm-unlock.sh setup'"
  say "Starte dot-guide.py in der VM …"
  exec ssh -t -o StrictHostKeyChecking=no -p $SSH_PORT echomuse@localhost \
    "cd ~/EchoMuse 2>/dev/null || git clone -b install/integration https://github.com/chr-braun/EchoMuse.git ~/EchoMuse; cd ~/EchoMuse && python3 dot-guide.py"
}

cmd_ssh(){
  vm_running || die "VM läuft nicht"
  exec ssh -t -p $SSH_PORT echomuse@localhost
}

cmd_status(){
  if vm_running; then ok "VM läuft (Monitor: $MON)"
  else warn "VM gestoppt."; fi
  echo "--- USB (Amazon) am Mac ---"
  usb_amazon_device || echo "  keins gefunden"
}

cmd_stop(){
  if vm_running; then
    mon_cmd "quit" >/dev/null 2>&1 || pkill -f echomuse-unlock || true
    sleep 2; ok "gestoppt."
  else warn "lief nicht."; fi
}

case "${1:-help}" in
  setup)  cmd_setup ;;
  attach) cmd_attach ;;
  detach) cmd_detach ;;
  guide)  cmd_guide ;;
  ssh)    cmd_ssh ;;
  status) cmd_status ;;
  stop)   cmd_stop ;;
  *) sed -n '2,11p' "$0" ;;
esac

#!/usr/bin/env bash
# EchoMuse – geführter Installer (Deutsch)
# Start:   ./guided-install.sh
# Ziel:    lokaler Mac (Controller nativ via venv) oder Docker-Host (Unraid)
set -euo pipefail
C_G="\033[1;32m"; C_Y="\033[1;33m"; C_R="\033[1;31m"; C_B="\033[1;36m"; C_0="\033[0m"
say()  { echo -e "${C_B}▶ $*${C_0}"; }
ok()   { echo -e "${C_G}✓ $*${C_0}"; }
warn() { echo -e "${C_Y}⚠ $*${C_0}"; }
die()  { echo -e "${C_R}✗ $*${C_0}"; exit 1; }
ask()  { echo -e "${C_Y}? $*${C_0}"; }

echo ""
echo "═══════════════════════════════════════════════════════"
echo "  EchoMuse Installation — geführter Assistent"
echo "  Controller für Echo Dot 2. Gen → Home Assistant"
echo "═══════════════════════════════════════════════════════"
echo ""
echo "Dieser Guide begleitet dich durch:"
echo "  1. Voraussetzungen prüfen"
echo "  2. Installationsart wählen (Mac nativ / Docker)"
echo "  3. Controller einrichten und starten"
echo "  4. Setup-Token & nächste Schritte"
echo ""

# ── Schritt 1: Checks ────────────────────────────────────────────
say "Schritt 1/4: Voraussetzungen"
REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
ok "Repo gefunden: $REPO_DIR"

IS_UNRAID=false; [ -f /etc/unraid-version ] && IS_UNRAID=true && ok "Unraid erkannt"
command -v git >/dev/null || die "git fehlt"
python3 --version >/dev/null 2>&1 || die "python3 fehlt"

LAN_IP=$(ipconfig getifaddr en0 2>/dev/null || ip route get 1.1.1.1 2>/dev/null | awk '{print $7; exit}' || echo "")
[ -n "$LAN_IP" ] && ok "LAN-IP erkannt: $LAN_IP" || warn "LAN-IP nicht automatisch erkannt"

HAS_DOCKER=false; command -v docker >/dev/null && docker info >/dev/null 2>&1 && HAS_DOCKER=true

for p in 8767 8768 8770; do
  if lsof -iTCP:$p -sTCP:LISTEN >/dev/null 2>&1; then die "Port $p ist belegt — erst freigeben"; fi
done
ok "Ports 8767/8768/8770 frei"

# ── Schritt 2: Art wählen ────────────────────────────────────────
echo ""
say "Schritt 2/4: Installationsart"
MODE=""
if $HAS_DOCKER; then
  read -rp "Docker erkannt. [1] Mac nativ testen  [2] Docker (empf. für Dauerbetrieb) — Wahl [1]: " m
  MODE=${m:-1}
else
  warn "Kein laufendes Docker gefunden."
  read -rp "[1] Mac nativ testen  [2] Docker später auf Unraid — Wahl [1]: " m
  MODE=${m:-1}
fi

# ── Schritt 3a: Mac nativ ────────────────────────────────────────
if [ "$MODE" = "1" ]; then
  echo ""
  say "Schritt 3/4: Controller nativ starten"
  VENV="$REPO_DIR/.venv-controller"
  [ -d "$VENV" ] || python3 -m venv "$VENV"
  # shellcheck disable=SC1091
  source "$VENV/bin/activate"
  pip install -q --upgrade pip
  pip install -q aiohttp websockets numpy scipy pyyaml openwakeword onnxruntime zeroconf bcrypt \
    || die "pip-Install fehlgeschlagen — requirements.txt prüfen"
  cd "$REPO_DIR/controller"
  export SERVER_HOST=0.0.0.0 SERVER_PORT=8767 SERVER_TLS_PORT=8770 API_PORT=8768
  [ -n "$LAN_IP" ] && export SERVER_IP="$LAN_IP"
  ok "Starte Controller … (Strg+C beendet)"
  echo ""
  echo -e "${C_G}→ Dashboard danach: http://${LAN_IP:-localhost}:8768${C_0}"
  echo -e "${C_G}→ Setup-Token steht in den ersten Logzeilen unten!${C_0}"
  echo ""
  exec python em_start.py
fi

# ── Schritt 3b: Docker (Unraid/Mac) ──────────────────────────────
echo ""
say "Schritt 3/4: Docker-Build + Start"
DATA_DIR="/mnt/user/appdata/echomuse"
$IS_UNRAID && { read -rp "Datenpfad [$DATA_DIR]: " d; DATA_DIR=${d:-$DATA_DIR}; } || DATA_DIR="$REPO_DIR/controller-docker-data"
mkdir -p "$DATA_DIR"
$IS_UNRAID && warn "SQLite mag keine gesplitteten User-Shares — Share 'Use cache: Only' setzen!"

read -rp "Server-IP im LAN [$LAN_IP]: " ip; SERVER_IP=${ip:-$LAN_IP}
read -rp "mDNS-Name [echomuse]: " mn; MDNS_NAME=${mn:-echomuse}

say "Baue Image aus diesem Repo (enthält alle lokalen Fixes)…"
docker build -q -t echomuse-controller:local "$REPO_DIR/controller" || die "Build fehlgeschlagen"

cat > "$DATA_DIR/docker-compose.yml" <<EOF
services:
  echomuse-controller:
    image: echomuse-controller:local
    container_name: echomuse-controller
    restart: unless-stopped
    network_mode: host
    environment:
      SERVER_HOST: 0.0.0.0
      SERVER_PORT: "8767"
      SERVER_TLS_PORT: "8770"
      API_PORT: "8768"
      SERVER_IP: "$SERVER_IP"
      MDNS_NAME: "$MDNS_NAME"
    volumes:
      - $DATA_DIR:/app/data
    logging:
      driver: json-file
      options: {max-size: "10m", max-file: "3"}
EOF

docker compose -f "$DATA_DIR/docker-compose.yml" up -d || docker run -d --name echomuse-controller \
  --network host --restart unless-stopped -v "$DATA_DIR:/app/data" \
  -e SERVER_IP="$SERVER_IP" -e MDNS_NAME="$MDNS_NAME" echomuse-controller:local

sleep 5
TOKEN=$(docker logs echomuse-controller 2>&1 | grep -A2 -i "setup token\|┌" | head -6 || true)

# ── Schritt 4: Nächste Schritte ──────────────────────────────────
echo ""
say "Schritt 4/4: Fertig — nächste Schritte"
echo ""
echo -e "${C_G}Dashboard:  http://$SERVER_IP:8768${C_0}"
[ -n "$TOKEN" ] && echo -e "Setup-Token aus dem Log:\n$TOKEN" \
  || echo "Setup-Token:  docker logs echomuse-controller  (Kasten am Anfang)"
echo ""
echo "  1. Dashboard öffnen, Token eingeben, Admin-Account anlegen"
echo "  2. Echo Dot per USB an den Laptop → Provisioning-Wizard im Dashboard"
echo "     (einmalig pro Dot; Anleitung: docs/rooting.md)"
echo "  3. Dot im Dashboard approven → in Home Assistant als Gerät erscheint er"
echo "     automatisch (ESPHome). HA braucht eine laufende Assist-Pipeline."
echo "  4. Wake Word sagen und sprechen :)"
echo ""
ok "Guide beendet."

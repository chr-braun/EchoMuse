#!/usr/bin/env python3
"""
EchoMuse Dot-Guide — Begleiter für das Anlernen eines Echo Dot Gen 2.

Start:   python3 dot-guide.py [--controller http://<ip>:8768]

Führt interaktiv durch:
  1. System-Voraussetzungen (adb, OS-Hinweise für den Unlock)
  2. Dateien-Checkliste (amonet, FireOS 5.5.5.4, f1r30s, Magisk 17.3)
  3. Geräteverbindung + Firmware-Version prüfen
  4. Unlock-Phase (bewusst Delegation an R0rt1z2's Thread — der Teil,
     der Geräte ruinieren kann, wird hier nicht automatisiert)
  5. Übergabe an den Provisioning-Wizard im Dashboard

Nur Stdlib.
"""
import os
import subprocess
import sys
import time
import urllib.request
import webbrowser

OK, WARN, BAD = "\033[1;32m✓\033[0m", "\033[1;33m⚠\033[0m", "\033[1;31m✗\033[0m"
EXPECTED_BUILD = "272.6.8.0_user_680767620"

REQUIRED_FILES = [
    ("amonet-biscuit-v1.1.0.zip", "R0rt1z2's XDA-Thread (Unlock/Root/TWRP)"),
    ("update-kindle-csm_biscuit-272.6.8.0_user_680767620.bin",
     "FireOS 5.5.5.4 — genau dieser Build ist getestet"),
    ("f1r30s.zip", "XDA-Thread — ADB+UART, OTA-Blocker, dm-verity aus "
                   "(nach JEDEM Stock-Flash flashen, sonst bootet nichts)"),
    ("Magisk-v17.3.zip", "github.com/topjohnwu/Magisk/releases/tag/v17.3 "
                         "(17.3 = letzte Version für Android 5.1)"),
]

CONTROLLER = "http://localhost:8768"
for i, a in enumerate(sys.argv):
    if a == "--controller" and i + 1 < len(sys.argv):
        CONTROLLER = sys.argv[i + 1]


def sh(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    except FileNotFoundError:
        return None


def say(msg): print(f"\n\033[1;36m▶ {msg}\033[0m")
def step(n, t): print(f"\n\033[1;36m═══ Schritt {n}: {t} ═══\033[0m")
def pause(msg="Weiter mit Enter …"):
    input(f"\n\033[1;33m{msg}\033[0m")


print("═══════════════════════════════════════════════════")
print("  EchoMuse · Dot-Guide — Echo Dot Gen 2 anlernen")
print("═══════════════════════════════════════════════════")
print("""
Dieser Guide begleitet dich. Wichtig zu wissen:

  • Der UNLOCK (Bootrom-Exploit) wird hier NICHT ausgeführt — er gehört
    R0rt1z2's amonet-biscuit (XDA-Thread) und kann ein Gerät ruinieren.
    Dieser Guide prüft deine Vorbereitung und reicht sauber über.
  • Danach übernimmt der Provisioning-Wizard im Dashboard (Chrome/Edge,
    WebUSB). Der Guide stellt sicher, dass du dort ankommen.""")
pause()

# ── Schritt 1 ────────────────────────────────────────────────────
step(1, "System-Voraussetzungen")
is_mac = sys.platform == "darwin"
is_linux = sys.platform.startswith("linux")

adb = sh(["adb", "version"])
print(f"  {OK if adb else BAD} adb {'installiert' if adb else 'FEHLT — brew install android-platform-tools / apt install adb'}")
fastb = sh(["fastboot", "--version"])
print(f"  {OK if fastb else WARN} fastboot {'vorhanden' if fastb else 'fehlt (erst für den Unlock nötig)'}")

if is_mac:
    print(f"  {WARN} macOS erkannt: Der UNLOCK (brick.sh) schlägt auf macOS laut "
          "Doku fehl. Für den Unlock eine Linux-Live-USB verwenden — alles "
          "danach geht wieder vom Mac.")
if is_linux:
    print(f"  {WARN} Linux: USB-Autosuspend vorher abschalten, sonst reißt ADB ab:")
    print("     echo -1 | sudo tee /sys/bus/usb/devices/*/power/autosuspend")
pause()

# ── Schritt 2 ────────────────────────────────────────────────────
step(2, "Dateien-Checkliste")
search_dirs = [os.getcwd(), os.path.expanduser("~/Downloads")]
missing = []
for fname, why in REQUIRED_FILES:
    found = next((d for d in search_dirs
                  if os.path.exists(os.path.join(d, fname))), None)
    if found:
        print(f"  {OK} {fname}")
        print(f"      gefunden in {found}")
    else:
        print(f"  {BAD} {fname}")
        print(f"      FEHLT — Quelle: {why}")
        missing.append((fname, why))
if missing:
    print(f"\n  {len(missing)} Datei(en) fehlen. Lade sie herunter und lege sie "
          f"in {os.getcwd()} oder ~/Downloads, dann starte diesen Guide neu.")
    if input("Trotzdem weitergehen? [y/N] ").lower() != "y":
        sys.exit(1)
pause()

# ── Schritt 3 ────────────────────────────────────────────────────
step(3, "Gerät verbinden & prüfen")
print("Schließe den Dot per USB an (das Kabel ist seine Stromversorgung!).")
input("Wenn verbunden: Enter … ")
dev = sh(["adb", "devices"])
devices = [l.split("\t")[0] for l in (dev.stdout or "").splitlines()[1:]
           if "\tdevice" in l]
if not devices:
    print(f"  {BAD} Kein Gerät per ADB sichtbar.")
    print("     - USB-Debugging auf dem Dot aktiviert? (Developer-Optionen)")
    print("     - Auf dem Display 'USB-Debugging erlauben' bestätigt?")
    sys.exit(1)
serial = devices[0]
print(f"  {OK} Gerät verbunden: {serial}")

prop = sh(["adb", "shell", "getprop", "ro.build.version.name"])
build = (prop.stdout or "").strip()
if EXPECTED_BUILD in build:
    print(f"  {OK} FireOS-Build: {build} (getesteter Referenz-Build)")
else:
    print(f"  {WARN} FireOS-Build: {build or 'unbekannt'}")
    print(f"      Getestet ist ausschließlich {EXPECTED_BUILD}.")
pause()

# ── Schritt 4 ────────────────────────────────────────────────────
step(4, "Unlock")
print("""Der Unlock läuft mit R0rt1z2's amonet-biscuit (brick.sh):
  • NUR unter Linux (macOS schlägt laut Doku fehl!)
  • Löscht userdata, modifiziert die Partitionstabelle
  • Fehlschlag kann den Dot SOFT-BRICKEN. Nicht an einem Gerät starten,
    das du nicht verlieren kannst.
  • Autoritative Anleitung bleibt der XDA-Thread — dieses Script führt
    nur aus und prüft.""")
answer = input("\nIst der Dot bereits entsperrt (TWRP bootbar)? [J/n] ")
if not answer.lower().startswith("n"):
    pass  # bereits entsperrt → weiter zu Schritt 5
elif is_linux:
    if input("Unlock JETZT hier durchführen? [genau so tippen: ja] ") != "ja":
        print(f"  {WARN} Abgebrochen — Unlock manuell nach XDA-Thread ausführen, "
              "dann diesen Guide erneut starten.")
        sys.exit(0)
    zname = REQUIRED_FILES[0][0]
    zdir = next((d for d in search_dirs
                 if os.path.exists(os.path.join(d, zname))), None)
    if not zdir:
        sys.exit(f"  {BAD} {zname} nicht gefunden.")
    subprocess.run(["unzip", "-o", zname, "-d", "amonet"], cwd=zdir,
                   capture_output=True)
    amonet_dir = os.path.join(zdir, "amonet", "amonet-biscuit")
    if not os.path.isdir(amonet_dir):
        amonet_dir = os.path.join(zdir, "amonet")
    print(f"  amonet entpackt: {amonet_dir}")
    print(f"  Gerät: {serial} — alle anderen ADB-Geräte abstecken!")
    if input(f"  Serial zur Bestätigung eintippen [{serial}]: ").strip() != serial:
        sys.exit(f"  {BAD} Serial stimmt nicht — Abbruch.")
    print("\n  Starte brick.sh … (dauert Minuten; Ausgabe folgt)\n")
    rc = subprocess.call(["sudo", "./brick.sh", serial], cwd=amonet_dir)
    if rc != 0:
        sys.exit(f"  {BAD} brick.sh endete mit Code {rc} — XDA-Thread zur "
                 "Fehlerbehandlung; Gerät ist ggf. in TWRP.")
    print(f"\n  {OK} Unlock fertig — Dot sollte in TWRP sein.")
    pause("Dot ist in TWRP — Enter …")
    binname = REQUIRED_FILES[1][0]
    binpath = next((os.path.join(d, binname) for d in search_dirs
                    if os.path.exists(os.path.join(d, binname))), None)
    if not binpath:
        sys.exit(f"  {BAD} {binname} fehlt.")
    print("  ADB-Sideload der FireOS-Firmware:")
    print("  → Im TWRP: Advanced > ADB Sideload > Swipe")
    input("     Wenn 'Sideload started' erscheint: Enter … ")
    subprocess.call(["adb", "sideload", binpath])
    f1 = next((os.path.join(d, "f1r30s.zip") for d in search_dirs
               if os.path.exists(os.path.join(d, "f1r30s.zip"))), None)
    if f1:
        subprocess.call(["adb", "push", f1, "/sdcard/f1r30s.zip"])
        subprocess.call(["adb", "shell", "twrp", "install", "/sdcard/f1r30s.zip"])
    print(f"  {WARN} Nach JEDEM Stock-Flash IMMER f1r30s.zip flashen, sonst "
          "bootet das OS nicht!")
    input("  'Reboot System' im TWRP wählen, dann Enter … ")
    time.sleep(25)
    build2 = (sh(["adb", "shell", "getprop", "ro.build.version.name"]).stdout or "").strip()
    print(f"  {'OK' if EXPECTED_BUILD in build2 else WARN} FireOS nach Reboot: "
          f"{build2 or 'noch nicht sichtbar'}")
elif not is_linux:
    print(f"  {BAD} Der Unlock benötigt LINUX — auf {sys.platform} wird er "
          "bewusst nicht ausgeführt. Linux-Live-USB verwenden und dort "
          "diesen Guide starten.")
    sys.exit(0)

# ── Schritt 5 ────────────────────────────────────────────────────
step(5, "Übergabe ans Dashboard (Provisioning-Wizard)")
try:
    with urllib.request.urlopen(f"{CONTROLLER}/api/system/setup-state",
                                timeout=5) as r:
        state = r.read()
    print(f"  {OK} Controller erreichbar: {CONTROLLER}")
except Exception as e:
    print(f"  {BAD} Controller NICHT erreichbar ({e})")
    print("     Erst den Controller starten: python3 install-wizard.py")
    sys.exit(1)

print("""Im Dashboard:
  1. Einloggen (beim ersten Start: Setup-Token aus dem Controller-Log)
  2. Tab 'Provisioning' öffnen
  3. Chrome fragt nach USB-Freigabe für den Dot — erlauben
  4. Den Wizard-Schritten folgen (SELinux-Patch, Magisk, WiFi, OTA-Slot)""")
time.sleep(1)
webbrowser.open(CONTROLLER)
print(f"\n{OK} Dashboard geöffnet. Viel Erfolg! Bei Problemen: "
      "'Download diagnostics' im Wizard liefert die Fehlerdaten.")

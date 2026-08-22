#!/usr/bin/env python3
"""
EchoMuse Installations-Assistent — grafischer Installer im Browser.

Start:   python3 install-wizard.py
Öffnet http://localhost:8769 und führt durch die Installation:
  1. Voraussetzungen prüfen
  2. Installationsart wählen (Mac nativ / Docker)
  3. Konfigurieren und installieren (Live-Log)
  4. Setup-Token + nächste Schritte

Nur Python-Stdlib — bewusst ohne Abhängigkeiten, damit der Wizard vor
der eigentlichen Installation läuft.
"""
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent
CONTROLLER_DIR = REPO_DIR / "controller"
WIZARD_PORT = 8769

# ── Zustand ──────────────────────────────────────────────────────────────────
STATE = {
    "phase": "idle",          # idle | checking | ready | installing | running | error
    "log": [],                # Zeilen des Installations-Logs
    "checks": None,           # Ergebnis der Vorprüfungen
    "result": {},             # dashboard_url, token, mode …
    "controller_proc": None,
}
LOCK = threading.Lock()


def log(line: str = "") -> None:
    with LOCK:
        STATE["log"].append(line)
        print(line, flush=True)


def set_phase(p: str) -> None:
    with LOCK:
        STATE["phase"] = p


# ── Prüfungen ────────────────────────────────────────────────────────────────
def detect_lan_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(1)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return ""


def port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("0.0.0.0", port))
            return True
        except OSError:
            return False


def run_checks() -> dict:
    set_phase("checking")
    c = {"repo": str(REPO_DIR), "items": []}

    def item(name, ok, detail=""):
        c["items"].append({"name": name, "ok": bool(ok), "detail": detail})
    py = f"{sys.version_info.major}.{sys.version_info.minor}"
    item("Python", sys.version_info >= (3, 10), f"v{py} gefunden")
    git = _run(["git", "--version"])
    item("git", git[0] is not None, (git[0] or "fehlt").strip())
    lan_ip = detect_lan_ip()
    item("LAN-IP", bool(lan_ip), lan_ip or "nicht erkannt")
    busy = [p for p in (8767, 8768, 8770) if not port_free(p)]
    item("Ports 8767/8768/8770", not busy,
         "frei" if not busy else f"belegt: {', '.join(map(str, busy))}")
    docker = _run(["docker", "info"])
    c["docker"] = docker[0] is not None
    item("Docker", True,
         "verfügbar" if c["docker"] else "nicht aktiv (nur für Docker-Modus nötig)")
    unraid = Path("/etc/unraid-version").exists()
    c["unraid"] = unraid
    if unraid:
        item("Unraid", True, "erkannt")
    c["lan_ip"] = lan_ip
    with LOCK:
        STATE["checks"] = c
        STATE["phase"] = "ready"
    return c


def _run(cmd, **kw):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=kw.pop("timeout", 20), **kw)
        return p.stdout, p.returncode
    except Exception as e:
        return None, str(e)


# ── Installation ─────────────────────────────────────────────────────────────
TOKEN_RE = re.compile(r"│\s*([0-9a-f]{32})\s*│")
DASHBOARD_RE = re.compile(r"dashboard|http://", re.I)


def find_token(text: str) -> str:
    parts = TOKEN_RE.findall(text)
    return "".join(parts[:2]) if parts else ""


def worker_native(params: dict) -> None:
    """Mac nativ: venv + requirements + Controller starten."""
    set_phase("installing")
    venv_dir = REPO_DIR / ".venv-controller"
    pip = str(venv_dir / "bin" / "pip")
    py = str(venv_dir / "bin" / "python")

    def sh(cmd, env=None):
        log(f"$ {' '.join(cmd)}")
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True,
                             env={**os.environ, **(env or {})})
        assert p.stdout
        for line in p.stdout:
            log(line.rstrip())
        p.wait()
        return p.returncode

    log("── Schritt 1/3: virtuelle Umgebung ──")
    if not venv_dir.exists():
        rc = sh([sys.executable, "-m", "venv", str(venv_dir)])
        if rc != 0:
            return fail("venv-Erstellung fehlgeschlagen")
    else:
        log("vorhanden, übersprungen")

    log("── Schritt 2/3: Abhängigkeiten installieren ──")
    rc = sh([pip, "install", "-q", "--upgrade", "pip"])
    rc |= sh([pip, "install", "-q", "-r",
              str(CONTROLLER_DIR / "requirements.txt")])
    if rc != 0:
        return fail("pip-Installation fehlgeschlagen — Log oben prüfen")

    log("── Schritt 3/3: Controller starten ──")
    env = {
        "SERVER_HOST": "0.0.0.0",
        "SERVER_PORT": "8767",
        "SERVER_TLS_PORT": "8770",
        "API_PORT": "8768",
        "SERVER_IP": params.get("server_ip") or "",
        "MDNS_NAME": params.get("mdns_name") or "echomuse",
    }
    env = {k: v for k, v in env.items() if v}
    proc = subprocess.Popen(
        [py, "em_start.py"], cwd=str(CONTROLLER_DIR),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        start_new_session=True,   # überlebt das Ende des Wizards
        env={**os.environ, **env})
    with LOCK:
        STATE["controller_proc"] = proc

    # Auf Bootstrap-Token lauschen (erste ~40s)
    deadline = time.time() + 40
    buf, token = [], ""
    assert proc.stdout
    import selectors
    sel = selectors.DefaultSelector()
    sel.register(proc.stdout, selectors.EVENT_READ)
    while time.time() < deadline and proc.poll() is None:
        if not sel.select(1.0):
            continue
        line = proc.stdout.readline()
        if not line:
            break
        log(line.rstrip())
        buf.append(line)
        token = find_token("".join(buf))
        if token:
            break

    ip = params.get("server_ip") or "localhost"
    with LOCK:
        STATE["result"] = {
            "mode": "native", "dashboard_url": f"http://{ip}:8768",
            "token": token,
            "token_note": "" if token else
            "Token noch nicht erschienen — später in diesem Fenster nachsehen "
            "(oder `cat controller/controller.log`, falls umgeleitet).",
        }
        STATE["phase"] = "running"
    log("Controller läuft als Hintergrundprozess.")


def fail(msg: str) -> None:
    log(f"FEHLER: {msg}")
    with LOCK:
        STATE["result"] = {"error": msg}
        STATE["phase"] = "error"


def worker_docker(params: dict) -> None:
    """Docker-Build + Start (Mac/Unraid)."""
    set_phase("installing")

    def sh(cmd):
        log(f"$ {' '.join(cmd)}")
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True)
        assert p.stdout
        for line in p.stdout:
            log(line.rstrip())
        p.wait()
        return p.returncode

    log("── Schritt 1/3: Image bauen ──")
    if sh(["docker", "build", "-t", "echomuse-controller:local",
           str(CONTROLLER_DIR)]) != 0:
        return fail("Docker-Build fehlgeschlagen")

    log("── Schritt 2/3: Datenverzeichnis + Compose ──")
    data_dir = Path(params.get("data_dir") or
                    (REPO_DIR / "controller-docker-data"))
    data_dir.mkdir(parents=True, exist_ok=True)
    compose = data_dir / "docker-compose.yml"
    compose.write_text(f"""services:
  echomuse-controller:
    image: echomuse-controller:local
    container_name: echomuse-controller
    restart: unless-stopped
    network_mode: host
    environment:
      SERVER_HOST: "0.0.0.0"
      SERVER_PORT: "8767"
      SERVER_TLS_PORT: "8770"
      API_PORT: "8768"
      SERVER_IP: "{params.get('server_ip') or ''}"
      MDNS_NAME: "{params.get('mdns_name') or 'echomuse'}"
    volumes:
      - {data_dir}:/app/data
""")
    log(f"geschrieben: {compose}")

    log("── Schritt 3/3: Container starten ──")
    sh(["docker", "rm", "-f", "echomuse-controller"])
    if sh(["docker", "compose", "-f", str(compose), "up", "-d"]) != 0:
        # Fallback: plain docker run (ältere Hosts ohne Compose-Plugin)
        ip = params.get("server_ip") or ""
        mdns = params.get("mdns_name") or "echomuse"
        if sh(["docker", "run", "-d", "--name", "echomuse-controller",
               "--network", "host", "--restart", "unless-stopped",
               "-e", f"SERVER_IP={ip}", "-e", f"MDNS_NAME={mdns}",
               "-v", f"{data_dir}:/app/data",
               "echomuse-controller:local"]) != 0:
            return fail("Container-Start fehlgeschlagen")

    log("warte auf Controller-Log (Setup-Token)…")
    token = ""
    deadline = time.time() + 45
    while time.time() < deadline and not token:
        out, _ = _run(["docker", "logs", "echomuse-controller"],
                      timeout=10)
        text = out or ""
        token = find_token(text)
        if not token:
            time.sleep(2)
    new_lines = (out or "").splitlines()[-30:]
    for ln in new_lines[-15:]:
        log(ln)

    ip = params.get("server_ip") or "localhost"
    with LOCK:
        STATE["result"] = {
            "mode": "docker", "dashboard_url": f"http://{ip}:8768",
            "token": token,
            "token_note": "" if token else
            "Token nicht gefunden — `docker logs echomuse-controller` prüfen.",
        }
        STATE["phase"] = "running"
    log("Container läuft.")


def start_install(body: dict) -> None:
    with LOCK:
        if STATE["phase"] in ("installing", "checking"):
            return
        STATE["log"] = []
        STATE["result"] = {}
    mode = body.get("mode", "native")
    t = threading.Thread(target=worker_native if mode == "native"
                         else worker_docker, args=(body,), daemon=True)
    t.start()


# ── HTTP ─────────────────────────────────────────────────────────────────────
HTML = """<!doctype html>
<html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>EchoMuse Setup</title>
<style>
:root{--bg:#14171a;--card:#1d2126;--line:#2b3138;--fg:#e8eaed;
 --dim:#9aa3ac;--green:#43c47a;--red:#e06c5b}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
 font:15px/1.55 -apple-system,'Segoe UI',Roboto,sans-serif;
 display:flex;justify-content:center;padding:28px 16px}
main{width:100%;max-width:760px}
h1{font-size:22px;margin:0}
h1 span{color:var(--green)}
.sub{color:var(--dim);margin:4px 0 18px}
.top{display:flex;justify-content:space-between;align-items:flex-start}
.langs button{background:none;border:1px solid var(--line);color:var(--dim);
 border-radius:6px;padding:3px 10px;font-size:12px;cursor:pointer;margin-left:4px}
.langs button.on{border-color:var(--green);color:var(--green)}
.tabs{display:flex;gap:8px;margin-bottom:16px}
.tab{flex:1;text-align:center;padding:10px;border:1px solid var(--line);
 border-radius:10px;cursor:pointer;color:var(--dim)}
.tab.sel{border-color:var(--green);color:var(--fg)}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;
 padding:20px;margin-bottom:14px}
.step{display:flex;gap:12px;align-items:flex-start}
.num{flex:0 0 26px;height:26px;border-radius:50%;background:var(--line);
 display:flex;align-items:center;justify-content:center;font-weight:700;font-size:13px}
.num.active{background:var(--green);color:#10241a}
.step>div:last-child{flex:1}
button{background:var(--green);color:#10241a;border:0;border-radius:8px;
 padding:9px 16px;font-weight:700;font-size:14px;cursor:pointer;margin-top:8px}
button:hover:not(:disabled){filter:brightness(1.1)}
button:disabled{opacity:.45;cursor:default}
button.sec{background:var(--line);color:var(--fg)}
label{display:block;color:var(--dim);font-size:13px;margin:10px 0 4px}
input{width:100%;background:var(--bg);border:1px solid var(--line);
 border-radius:8px;color:var(--fg);padding:9px 12px;font-size:14px}
.check{display:flex;justify-content:space-between;gap:10px;padding:5px 0;
 border-bottom:1px solid var(--line);font-size:14px}
.check small{color:var(--dim)}
.ok{color:var(--green)}.bad{color:var(--red)}
pre{background:#101317;border:1px solid var(--line);border-radius:8px;
 padding:12px;max-height:240px;overflow:auto;font-size:12px;line-height:1.45;
 white-space:pre-wrap;word-break:break-all}
.token{background:#10241a;border:1px solid var(--green);border-radius:8px;
 padding:12px;font-family:ui-monospace,Menlo,monospace;font-size:15px;
 word-break:break-all;margin:8px 0}
.mode{border:1px solid var(--line);border-radius:10px;padding:12px;
 cursor:pointer;margin-bottom:10px}
.mode.sel{border-color:var(--green)}
.mode b{display:block}.mode small{color:var(--dim)}
.hidden{display:none}
.help{color:var(--dim);font-size:13px;margin:6px 0 12px;line-height:1.5}
ol.help li{margin-bottom:6px}
h3{font-size:15px;margin:16px 0 8px}
</style></head><body><main>
<div class="top">
 <div><h1>Echo<span>Muse</span> Setup</h1>
 <div class="sub" data-de="Geführter Assistent · Controller für Echo Dot 2. Gen → Home Assistant"
      data-en="Guided assistant · Controller for Echo Dot 2nd gen → Home Assistant">Geführter Assistent · Controller für Echo Dot 2. Gen → Home Assistant</div></div>
 <div class="langs"><button id="l-de" onclick="setLang('de')">DE</button><button id="l-en" onclick="setLang('en')">EN</button></div>
</div>

<div class="tabs">
 <div class="tab sel" id="tab-c" onclick="showTab('c')" data-de="🖥 Controller" data-en="🖥 Controller">🖥 Controller</div>
 <div class="tab" id="tab-d" onclick="showTab('d')" data-de="🔊 Echo Dot anlernen" data-en="🔊 Set up Echo Dot">🔊 Echo Dot anlernen</div>
</div>

<!-- ═══════════ CONTROLLER ═══════════ -->
<div id="page-c">
<div class="card">
 <div class="step"><div class="num active">1</div><div>
  <b data-de="Voraussetzungen" data-en="Prerequisites">Voraussetzungen</b>
  <div id="checks"><span class="sub" data-de="Prüfe …" data-en="Checking …">Prüfe …</span></div>
  <button id="btn-recheck" class="sec hidden" onclick="recheck()" data-de="Erneut prüfen" data-en="Check again">Erneut prüfen</button>
 </div></div>
</div>

<div class="card">
 <div class="step"><div class="num">2</div><div style="flex:1">
  <b data-de="Installationsart" data-en="Installation type">Installationsart</b>
  <div class="mode sel" id="m-native" onclick="pick('native')">
   <b data-de="Nativ auf diesem Rechner" data-en="Natively on this machine">Nativ auf diesem Rechner</b>
   <small data-de="Zum Testen — virtuelle Umgebung + Abhängigkeiten, Controller läuft direkt" data-en="For testing — virtual environment + dependencies, controller runs directly">Zum Testen — virtuelle Umgebung + Abhängigkeiten, Controller läuft direkt</small></div>
  <div class="mode" id="m-docker" onclick="pick('docker')">
   <b>Docker</b>
   <small data-de="Für Dauerbetrieb (Unraid/Mac/Linux) — baut das Image aus diesem Repo inklusive aller lokalen Fixes" data-en="For permanent use (Unraid/Mac/Linux) — builds the image from this repo including all local fixes">Für Dauerbetrieb (Unraid/Mac/Linux) — baut das Image aus diesem Repo inklusive aller lokalen Fixes</small></div>
 </div></div>
</div>

<div class="card">
 <div class="step"><div class="num">3</div><div style="flex:1">
  <b data-de="Konfiguration" data-en="Configuration">Konfiguration</b>
  <label data-de="Server-IP im LAN (wird über mDNS beworben)" data-en="Server IP on LAN (advertised via mDNS)">Server-IP im LAN (wird über mDNS beworben)</label>
  <input id="ip" placeholder="">
  <label>mDNS-Name</label><input id="mdns" value="echomuse">
  <div id="dd-wrap" class="hidden">
   <label data-de="Datenverzeichnis (Docker)" data-en="Data directory (Docker)">Datenverzeichnis (Docker)</label>
   <input id="datadir"></div>
  <button id="btn-go" onclick="start()" data-de="Installation starten" data-en="Start installation">Installation starten</button>
 </div></div>
</div>

<div class="card hidden" id="card-log">
 <div class="step"><div class="num" id="num4">4</div><div style="flex:1">
  <b data-de="Installation läuft …" data-en="Installing …">Installation läuft …</b>
  <pre id="log"></pre>
 </div></div>
</div>

<div class="card hidden" id="card-done">
 <b data-de="✅ Fertig!" data-en="✅ Done!">✅ Fertig!</b>
 <p><span data-de="Dashboard:" data-en="Dashboard:">Dashboard:</span> <a id="dash" style="color:var(--green)" href="#"></a></p>
 <div data-de="Setup-Token (einmalig, für den ersten Admin-Account):" data-en="Setup token (one-time, for the first admin account):">Setup-Token (einmalig, für den ersten Admin-Account):</div>
 <div class="token" id="token"></div>
 <div class="sub" id="token-note"></div>
 <hr style="border-color:var(--line)">
 <b data-de="Nächste Schritte" data-en="Next steps">Nächste Schritte</b>
 <ol class="help">
  <li data-de="Dashboard öffnen, Token eingeben, Admin-Konto anlegen" data-en="Open the dashboard, enter the token, create the admin account">Dashboard öffnen, Token eingeben, Admin-Konto anlegen</li>
  <li data-de="Echo Dot per USB an den Laptop → Provisioning-Wizard im Dashboard (einmalig pro Gerät)" data-en="Connect the Dot via USB → provisioning wizard in the dashboard (once per device)">Echo Dot per USB an den Laptop → Provisioning-Wizard im Dashboard (einmalig pro Gerät)</li>
  <li data-de="Gerät approven → erscheint automatisch in Home Assistant; HA braucht eine eingerichtete Assist-Pipeline" data-en="Approve the device → appears automatically in Home Assistant; HA needs a configured Assist pipeline">Gerät approven → erscheint automatisch in Home Assistant; HA braucht eine eingerichtete Assist-Pipeline</li>
  <li data-de="Wake Word sagen und sprechen 🙂" data-en="Say the wake word and talk 🙂">Wake Word sagen und sprechen 🙂</li>
 </ol>
</div>
</div>

<!-- ═══════════ ECHO DOT ═══════════ -->
<div id="page-d" class="hidden">
<div class="card">
 <p class="help" data-de="Dieser Abschnitt begleitet das Anlernen eines Echo Dot Gen 2. Wichtig vorab: Der Unlock (Bootrom-Exploit) läuft NUR unter Linux — auf dem Mac stellt dieser Assistent eine Mini-Linux-VM bereit. Er löscht das Gerät und kann es im Fehlerfall ruinieren: nur Dots verwenden, die du nicht verlierst." data-en="This section walks you through setting up an Echo Dot Gen 2. Important: the unlock (bootrom exploit) ONLY runs on Linux — on a Mac this assistant provides a mini Linux VM. It wipes the device and can ruin it on failure: only use Dots you can afford to lose.">Dieser Abschnitt begleitet das Anlernen eines Echo Dot Gen 2. Wichtig vorab: Der Unlock (Bootrom-Exploit) läuft NUR unter Linux — auf dem Mac stellt dieser Assistent eine Mini-Linux-VM bereit. Er löscht das Gerät und kann es im Fehlerfall ruinieren: nur Dots verwenden, die du nicht verlierst.</p>
 <button class="sec" onclick="dotChecks()" data-de="Gerät & Dateien prüfen" data-en="Check device & files">Gerät & Dateien prüfen</button>
 <div id="dot-checks"></div>
 <h3 data-de="Benötigte Dateien" data-en="Required files">Benötigte Dateien</h3>
 <div id="dot-files"><span class="help" data-de="Noch nicht geprüft." data-en="Not checked yet.">Noch nicht geprüft.</span></div>
</div>
<div class="card">
 <div class="step"><div class="num active">→</div><div>
  <b data-de="Rooten & einrichten" data-en="Root & set up">Rooten & einrichten</b>
  <p class="help" data-de="Schritt für Schritt: Unlock per brick.sh (nur Linux/VM), Firmware-Sideload, f1r30s-Pflicht-Flash, dann Übergabe ans Dashboard. Der Guide öffnet sich im Terminal und fragt jeden Schritt ab." data-en="Step by step: unlock via brick.sh (Linux/VM only), firmware sideload, mandatory f1r30s flash, then handover to the dashboard. The guide opens in a terminal and confirms every step.">Schritt für Schritt: Unlock per brick.sh (nur Linux/VM), Firmware-Sideload, f1r30s-Pflicht-Flash, dann Übergabe ans Dashboard. Der Guide öffnet sich im Terminal und fragt jeden Schritt ab.</p>
  <button id="btn-vm" onclick="launch('vm-setup')">1. Linux-VM starten <small>(Unlock)</small></button><br>
  <button class="sec" onclick="launch('dot-guide')">2. Dot-Guide <small data-de="(Terminal)" data-en="(terminal)">(Terminal)</small></button>
 </div></div>
</div>
<div class="card">
 <div class="step"><div class="num">✓</div><div>
  <b data-de="Im Dashboard anlernen" data-en="Provision in the dashboard">Im Dashboard anlernen</b>
  <p class="help" data-de="Nach dem Rooten: Dashboard öffnen → Provisioning → USB-Freigabe für den Dot erteilen (Chrome/Edge) → den Wizard-Schritten folgen. Danach erscheint der Dot in Home Assistant." data-en="After rooting: open the dashboard → Provisioning → grant USB access (Chrome/Edge) → follow the wizard. Afterwards the Dot appears in Home Assistant.">Nach dem Rooten: Dashboard öffnen → Provisioning → USB-Freigabe für den Dot erteilen (Chrome/Edge) → den Wizard-Schritten folgen. Danach erscheint der Dot in Home Assistant.</p>
 </div></div>
</div>
</div>
</main>
<script>
var LANGS={de:{},en:{}};
function t(el){var v=el.getAttribute(lang==="en"?"data-en":"data-de");
 if(v!==null&&v!==undefined)el.textContent=v;}
function applyLang(){
 document.querySelectorAll("[data-de]").forEach(t);
 document.documentElement.lang=lang;
 document.getElementById("l-de").classList.toggle("on",lang==="de");
 document.getElementById("l-en").classList.toggle("on",lang==="en");}
var lang=(navigator.language||"de").slice(0,2)==="de"?"de":"en";
function setLang(l){lang=l;applyLang();}
applyLang();

function showTab(t){
 document.getElementById("page-c").classList.toggle("hidden",t!=="c");
 document.getElementById("page-d").classList.toggle("hidden",t!=="d");
 document.getElementById("tab-c").classList.toggle("sel",t==="c");
 document.getElementById("tab-d").classList.toggle("sel",t==="d");
 if(t==="d")dotChecks();}

var mode="native";
function pick(m){mode=m;
 document.getElementById("m-native").classList.toggle("sel",m==="native");
 document.getElementById("m-docker").classList.toggle("sel",m==="docker");
 document.getElementById("dd-wrap").classList.toggle("hidden",m!=="docker");}

async function poll(){
 try{
 var s=await(await fetch("/api/state")).json();
 var cl=document.getElementById("checks");
 if(s.checks){
  cl.innerHTML=s.checks.items.map(function(i){
   return "<div class='check'><span>"+i.name+"</span><span class='"+(i.ok?"ok":"bad")+"'>"+esc(i.detail||"")+"</span></div>";}).join("");
 }
 if(s.phase==="ready"){
  document.getElementById("btn-recheck").classList.remove("hidden");
  if(s.checks&&s.checks.lan_ip&&!document.getElementById("ip").value)
   document.getElementById("ip").value=s.checks.lan_ip;}
 if(s.log&&s.log.length){
  document.getElementById("card-log").classList.remove("hidden");
  var l=document.getElementById("log");
  l.textContent=s.log.join("\u000A");l.scrollTop=l.scrollHeight;}
 document.getElementById("btn-go").disabled=(s.phase==="installing"||s.phase==="checking");
 if(s.phase==="running"&&s.result&&s.result.dashboard_url){
  document.getElementById("card-done").classList.remove("hidden");
  var d=document.getElementById("dash");d.textContent=s.result.dashboard_url;d.href=s.result.dashboard_url;
  document.getElementById("token").textContent=s.result.token||"—";
  document.getElementById("token-note").textContent=s.result.token_note||"";}
 }catch(e){}
 setTimeout(poll,1200);}

function esc(s){return String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;");}
async function recheck(){await fetch("/api/checks",{method:"POST"});}
async function start(){
 await fetch("/api/start",{method:"POST",headers:{"Content-Type":"application/json"},
  body:JSON.stringify({mode:mode,server_ip:document.getElementById("ip").value,
   mdns_name:document.getElementById("mdns").value,
   data_dir:document.getElementById("datadir").value})});}
async function dotChecks(){
 var d=await(await fetch("/api/dot-checks",{method:"POST"})).json();
 document.getElementById("dot-checks").innerHTML=d.items.map(function(i){
  return "<div class='check'><span>"+i.name+"</span><span class='"+(i.ok?"ok":"bad")+"'>"+esc(i.detail||"")+"</span></div>";}).join("");
 document.getElementById("dot-files").innerHTML=d.files.map(function(f){
  return "<div class='check'><span>"+f.name+"<br><small>"+esc(f.why)+"</small></span><span class='"+(f.ok?"ok":"bad")+"'>"+(f.ok?"✓":"✗")+"</span></div>";}).join("");}
async function launch(t){await fetch("/api/launch",{method:"POST",
 headers:{"Content-Type":"application/json"},body:JSON.stringify({target:t})});
 alert(lang==="de"?"Der Guide wurde in einem neuen Terminalfenster gestartet.":"The guide was launched in a new terminal window.");}
(async function(){await fetch("/api/checks",{method:"POST"});poll();})();
</script></body></html>
"""


# ── Dot-Checks (Echo-Dot-Reiter) ──────────────────────────────────────────────
REQUIRED_FILES = [
    ("amonet-biscuit-v1.1.0.zip", "XDA thread (unlock/root/TWRP)"),
    ("update-kindle-csm_biscuit-272.6.8.0_user_680767620.bin",
     "FireOS 5.5.5.4 - the tested reference build"),
    ("f1r30s.zip", "XDA thread - ADB+UART, OTA blocker, dm-verity off"),
    ("Magisk-v17.3.zip", "github.com/topjohnwu/Magisk/releases/tag/v17.3"),
]
EXPECTED_BUILD = "272.6.8.0_user_680767620"


def run_dot_checks() -> dict:
    r = {"items": [], "files": []}

    def sh(cmd):
        try:
            return subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=15)
        except Exception:
            return None

    adb = sh(["adb", "version"])
    r["items"].append({"name": "adb", "ok": adb is not None,
                       "detail": adb.stdout.splitlines()[0] if adb else "missing"})
    dev = sh(["adb", "devices"])
    serials = [l.split("\t")[0] for l in (dev.stdout or "").splitlines()[1:]
               if "\tdevice" in l] if dev else []
    r["items"].append({"name": "device", "ok": bool(serials),
                       "detail": serials[0] if serials else "none attached"})
    if serials:
        prop = sh(["adb", "shell", "getprop", "ro.build.version.name"])
        build = (prop.stdout or "").strip() if prop else ""
        r["items"].append({"name": "FireOS build", "ok": EXPECTED_BUILD in build,
                           "detail": build or "unknown"})
    search = [os.getcwd(), str(Path.home() / "Downloads"), str(REPO_DIR)]
    for fname, why in REQUIRED_FILES:
        found = next((d for d in search
                      if os.path.exists(os.path.join(d, fname))), None)
        r["files"].append({"name": fname, "why": why,
                           "ok": bool(found), "dir": found or ""})
    return r


def launch_in_terminal(script: str) -> tuple:
    """Öffnet script in einem neuen Terminalfenster des jeweiligen OS."""
    plat = sys.platform
    try:
        if plat == "darwin":
            apple = (f'tell app "Terminal" to do script '
                     f'"cd {REPO_DIR} && python3 {script}; exec bash"')
            subprocess.Popen(["osascript", "-e", apple])
        elif plat == "win32":
            subprocess.Popen(["cmd", "/c", "start", "cmd", "/k",
                              f"cd /d {REPO_DIR} && python3 {script}"])
        else:
            for term in ("x-terminal-emulator", "gnome-terminal", "xterm"):
                if shutil.which(term):
                    args = ([term, "-e"] if term != "gnome-terminal"
                            else [term, "--"]) + \
                           [f"bash -c 'cd {REPO_DIR} && python3 {script}; exec bash'"]
                    subprocess.Popen(args)
                    break
            else:
                return False, "no terminal emulator found"
        return True, "launched"
    except Exception as e:
        return False, str(e)


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/":
            self._send(200, HTML, "text/html; charset=utf-8")
        elif self.path == "/api/state":
            with LOCK:
                snap = {k: (list(v) if isinstance(v, list) else v)
                        for k, v in STATE.items()}
            snap.pop("controller_proc", None)
            self._send(200, json.dumps(snap))
        else:
            self._send(404, "{}")

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length).decode() if length else "{}"
        try:
            body = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            body = {}
        if self.path == "/api/checks":
            threading.Thread(target=run_checks, daemon=True).start()
            self._send(202, "{}")
        elif self.path == "/api/start":
            start_install(body)
            self._send(202, "{}")
        elif self.path == "/api/dot-checks":
            self._send(200, json.dumps(run_dot_checks()))
        elif self.path == "/api/launch":
            self._send(200, _api_launch(body))
        else:
            self._send(404, "{}")

    def log_message(self, *a):  # keep the console quiet
        pass


def _api_launch(body):
    target = body.get("target", "")
    if target == "dot-guide":
        ok_, msg = launch_in_terminal("dot-guide.py")
    elif target == "vm-setup":
        ok_, msg = launch_in_terminal("vm-unlock.sh setup")
    elif target == "controller-guide":
        ok_, msg = launch_in_terminal("guided-install.sh")
    else:
        return {"ok": False, "msg": "unknown target"}
    return {"ok": ok_, "msg": msg}


def main():
    port = WIZARD_PORT
    if not port_free(port):
        print(f"Port {port} belegt — Wizard läuft evtl. schon.")
        sys.exit(1)
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://localhost:{port}"
    print(f"EchoMuse Installations-Assistent: {url}  (Strg+C beendet)")
    threading.Thread(target=lambda: (
        time.sleep(0.8), webbrowser.open(url)), daemon=True).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nWizard beendet.")
        with LOCK:
            proc = STATE.get("controller_proc")
        if proc and proc.poll() is None:
            print("Der native Controller läuft weiter "
                  "(Hintergrundprozess des Systems).")


if __name__ == "__main__":
    main()

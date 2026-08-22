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
<title>EchoMuse Installation</title>
<style>
:root{--bg:#14171a;--card:#1d2126;--line:#2b3138;--fg:#e8eaed;
      --dim:#9aa3ac;--green:#43c47a;--amber:#e5b45b;--red:#e06c5b}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
  font:15px/1.55 -apple-system,'Segoe UI',Roboto,sans-serif;
  display:flex;justify-content:center;padding:32px 16px}
main{width:100%;max-width:760px}
h1{font-size:22px;margin:0 0 4px}h1 span{color:var(--green)}
.sub{color:var(--dim);margin-bottom:24px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;
  padding:20px;margin-bottom:16px}
.step{display:flex;gap:12px;align-items:flex-start;margin-bottom:10px}
.num{flex:0 0 26px;height:26px;border-radius:50%;background:var(--line);
  display:flex;align-items:center;justify-content:center;font-weight:700;
  font-size:13px}
.num.active{background:var(--green);color:#10241a}
button{background:var(--green);color:#10241a;border:0;border-radius:8px;
  padding:10px 18px;font-weight:700;font-size:14px;cursor:pointer}
button:disabled{opacity:.45;cursor:default}
button.sec{background:var(--line);color:var(--fg)}
label{display:block;color:var(--dim);font-size:13px;margin:10px 0 4px}
input{width:100%;background:var(--bg);border:1px solid var(--line);
  border-radius:8px;color:var(--fg);padding:9px 12px;font-size:14px}
.check{display:flex;justify-content:space-between;padding:6px 0;
  border-bottom:1px solid var(--line)}
.ok{color:var(--green)}.bad{color:var(--red)}
pre{background:#101317;border:1px solid var(--line);border-radius:8px;
  padding:12px;max-height:260px;overflow:auto;font-size:12px;line-height:1.5;
  white-space:pre-wrap;word-break:break-all}
.token{background:#10241a;border:1px solid var(--green);border-radius:8px;
  padding:12px;font-family:ui-monospace,Menlo,monospace;font-size:15px;
  word-break:break-all;margin-top:8px}
.mode{border:1px solid var(--line);border-radius:10px;padding:14px;
  cursor:pointer;margin-bottom:10px}
.mode.sel{border-color:var(--green)}
.mode b{display:block}.mode small{color:var(--dim)}
.hidden{display:none}
</style></head><body><main>
<h1>Echo<span>Muse</span> — Installation</h1>
<div class="sub">Geführter Assistent · Controller für Echo Dot 2. Gen → Home Assistant</div>

<div class="card">
 <div class="step"><div class="num active">1</div><div style="flex:1">
  <b>Voraussetzungen</b>
  <div id="checks"><span class="sub">Prüfe …</span></div>
  <button id="btn-recheck" class="sec hidden" onclick="recheck()">Erneut prüfen</button>
 </div></div>
</div>

<div class="card" id="card-mode">
 <div class="step"><div class="num">2</div><div style="flex:1;width:100%">
  <b>Installationsart</b>
  <div class="mode sel" id="m-native" onclick="pick('native')">
   <b>Mac nativ starten</b>
   <small>Zum Testen — venv + Abhängigkeiten, Controller im Vordergrund des Systems</small>
  </div>
  <div class="mode" id="m-docker" onclick="pick('docker')">
   <b>Docker-Container</b>
   <small>Für Dauerbetrieb (Unraid/Mac mit Docker) — baut Image aus diesem Repo inkl. aller Fixes</small>
  </div>
 </div></div>
</div>

<div class="card" id="card-cfg">
 <div class="step"><div class="num">3</div><div style="flex:1;width:100%">
  <b>Konfiguration</b>
  <label>Server-IP im LAN (für mDNS)</label>
  <input id="ip" placeholder="wird automatisch erkannt">
  <label>mDNS-Name</label>
  <input id="mdns" value="echomuse">
  <div id="dd-wrap" class="hidden">
   <label>Datenverzeichnis (Docker)</label>
   <input id="datadir" placeholder="standardmäßig neben dem Repo">
  </div>
  <br><button id="btn-go" onclick="start()">Installation starten</button>
 </div></div>
</div>

<div class="card hidden" id="card-log">
 <div class="step"><div class="num" id="num4">4</div><div style="flex:1;width:100%">
  <b>Installation läuft …</b>
  <pre id="log"></pre>
 </div></div>
</div>

<div class="card hidden" id="card-done">
 <b>✅ Fertig!</b>
 <p>Dashboard: <a id="dash" style="color:var(--green)" href="#"></a></p>
 <div>Setup-Token (einmalig, für den ersten Admin-Account):</div>
 <div class="token" id="token"></div>
 <div class="sub" id="token-note" style="margin-top:6px"></div>
 <hr style="border-color:var(--line)">
 <b>Nächste Schritte</b>
 <ol style="color:var(--dim)">
  <li>Dashboard öffnen, Token eingeben, Admin-Account anlegen</li>
  <li>Echo Dot per USB an den Laptop → Provisioning-Wizard im Dashboard
      (einmalig pro Gerät, Details: docs/rooting.md)</li>
  <li>Gerät approven → erscheint automatisch in Home Assistant (ESPHome);
      HA braucht eine eingerichtete Assist-Pipeline</li>
  <li>Wake Word sagen und sprechen 🙂</li>
 </ol>
</div>

<script>
let mode='native';
function pick(m){mode=m;
 document.getElementById('m-native').classList.toggle('sel',m==='native');
 document.getElementById('m-docker').classList.toggle('sel',m==='docker');
 document.getElementById('dd-wrap').classList.toggle('hidden',m!=='docker');}
async function poll(){
 const s=await(await fetch('/api/state')).json();
 const cl=document.getElementById('checks');
 if(s.checks){
  cl.innerHTML=s.checks.items.map(i=>
   `<div class="check"><span>${i.name}</span><span class="${i.ok?'ok':'bad'}">${i.detail||''}</span></div>`).join('');
 }
 if(s.phase==='ready'){document.getElementById('btn-recheck').classList.remove('hidden');
   if(s.checks&&s.checks.lan_ip&&!document.getElementById('ip').value)
    document.getElementById('ip').value=s.checks.lan_ip;}
 if(s.log){const l=document.getElementById('log');
  l.textContent=s.log.join('\\n');l.scrollTop=l.scrollHeight;}
 const running=s.phase==='installing'||s.phase==='checking';
 document.getElementById('btn-go').disabled=running;
 document.getElementById('card-log').classList.toggle('hidden',
   !['installing','running'].includes(s.phase)||!s.log.length);
 if(s.phase==='running'&&s.result&&s.result.dashboard_url){
  document.getElementById('card-done').classList.remove('hidden');
  const d=document.getElementById('dash');d.textContent=s.result.dashboard_url;
  d.href=s.result.dashboard_url;
  document.getElementById('token').textContent=s.result.token||
   '(siehe Hinweis)';document.getElementById('token-note').textContent=
   s.result.token_note||'';
 }
 setTimeout(poll,1200);
}
async function recheck(){await fetch('/api/checks',{method:'POST'});setTimeout(poll,300);}
async function start(){
 const body={mode,server_ip:document.getElementById('ip').value,
  mdns_name:document.getElementById('mdns').value,
  data_dir:document.getElementById('datadir').value};
 await fetch('/api/start',{method:'POST',
  headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
 poll();}
(async()=>{await fetch('/api/checks',{method:'POST'});poll();})();
</script></body></html>"""


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
        else:
            self._send(404, "{}")

    def log_message(self, *a):  # ruhig halten
        pass


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

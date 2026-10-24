"""Dashboard + Fernsteuerung (nur Python-Standardbibliothek).
  python dashboard.py --host 0.0.0.0 --port 8080
Alles, was Ben selbst schaltet, läuft hierüber – geschützt durch eine PIN, die er beim
ersten Aufruf selbst festlegt. Der Server-Betreiber muss nichts konfigurieren."""
import argparse
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
import zipfile
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(HERE, "logs")
PIN_FILE = os.path.join(HERE, ".pin")
ENV_FILE = os.path.join(HERE, ".env")
ALLOWED = re.compile(r"^(desk/[a-z_]+\.py|[A-Za-z_]+\.(py|html|yaml|txt|md|sh)|VERSION)$")
FAILS = {"n": 0, "until": 0.0}
os.makedirs(LOG, exist_ok=True)


# ---------------------------------------------------------------- Hilfen
def read(p, default=""):
    try:
        with open(p) as f:
            return f.read().strip()
    except FileNotFoundError:
        return default


def read_json(p, default=None):
    try:
        with open(p) as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return default


def tail_jsonl(p, n=60):
    try:
        with open(p) as f:
            return [json.loads(x) for x in f.readlines()[-n:] if x.strip()]
    except FileNotFoundError:
        return []


def equity(p, n=500):
    out = []
    try:
        with open(p) as f:
            for line in f.readlines()[-n:]:
                t, v = line.strip().split(",")
                out.append([t, float(v)])
    except FileNotFoundError:
        pass
    return out


def control_log(msg):
    with open(os.path.join(LOG, "control.log"), "a") as f:
        f.write(f"{time.strftime('%d.%m %H:%M:%S')} {msg}\n")


def pin_hash(pin, salt):
    return hashlib.pbkdf2_hmac("sha256", pin.encode(), bytes.fromhex(salt), 200_000).hex()


def pin_set():
    return os.path.exists(PIN_FILE)


def check_pin(pin):
    if time.time() < FAILS["until"]:
        return False, "Zu viele Fehlversuche – bitte 10 Minuten warten."
    d = read_json(PIN_FILE)
    if not d:
        return False, "Noch keine PIN festgelegt."
    if pin and hmac.compare_digest(pin_hash(str(pin), d["salt"]), d["hash"]):
        FAILS["n"] = 0
        return True, ""
    FAILS["n"] += 1
    if FAILS["n"] >= 5:
        FAILS["until"], FAILS["n"] = time.time() + 600, 0
    return False, "PIN falsch."


def write_private(path, text):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)


def test_keys(key, secret):
    """Kraken-Guthaben abfragen – eigenständig, ohne den restlichen Bot-Code."""
    import base64
    import urllib.parse
    import urllib.request
    path = "/0/private/Balance"
    nonce = str(int(time.time() * 1000))
    post = urllib.parse.urlencode({"nonce": nonce})
    try:
        raw = base64.b64decode(secret, validate=True)
    except Exception:  # noqa: BLE001
        raise ValueError("Secret ist kein gültiger Schlüssel (falsch kopiert?)")
    sha = hashlib.sha256((nonce + post).encode()).digest()
    sig = base64.b64encode(hmac.new(raw, path.encode() + sha, hashlib.sha512).digest()).decode()
    req = urllib.request.Request("https://api.kraken.com" + path, data=post.encode(),
                                 headers={"API-Key": key, "API-Sign": sig, "User-Agent": "kraken-desk"})
    with urllib.request.urlopen(req, timeout=20) as r:
        d = json.load(r)
    if d.get("error"):
        raise ValueError(", ".join(d["error"]))
    b = d["result"]
    return float(b.get("ZEUR", b.get("EUR", 0)))


def self_check():
    """Beim Start prüfen, ob der Bot-Code vollständig ist – Ergebnis ins Dashboard-Protokoll."""
    missing = [f for f in ("run.py", "rules.yaml", "desk/__init__.py", "desk/engine.py", "desk/data.py",
                           "desk/stages.py", "desk/indicators.py", "desk/kraken_live.py")
               if not os.path.exists(os.path.join(HERE, f))]
    py = os.path.join(HERE, "venv", "bin", "python")
    py = py if os.path.exists(py) else sys.executable
    try:
        r = subprocess.run([py, "-c", "import desk.engine, desk.kraken_live, yaml, pandas"], cwd=HERE,
                           capture_output=True, text=True, timeout=60)
        imp = (r.stderr.strip().splitlines() or ["?"])[-1] if r.returncode else ""
    except Exception as e:  # noqa: BLE001
        imp = str(e)
    if missing or imp:
        control_log(f"SELBSTTEST FEHLER – fehlt: {missing or '-'} | Import: {imp or 'ok'} | Ordner: {HERE}")
    else:
        control_log(f"Selbsttest ok (Version {read(os.path.join(HERE, 'VERSION'), '?')})")


# ---------------------------------------------------------------- Live-Kurse
LIVE = {"t": 0.0, "key": None, "data": None, "alt": {}}
LIVE_LOCK = threading.Lock()


def _kraken_public(endpoint, params):
    import urllib.request
    import urllib.parse
    url = "https://api.kraken.com/0/public/" + endpoint + "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=10) as r:
        d = json.load(r)
    if d.get("error"):
        raise RuntimeError(", ".join(d["error"]))
    return d["result"]


def live_prices(pairs):
    """Aktuelle Kurse (öffentlich, ohne Keys). Höchstens alle 15 s ein Abruf bei Kraken."""
    pairs = sorted(set(pairs) | {"XBTEUR"})
    key = ",".join(pairs)
    with LIVE_LOCK:
        if LIVE["key"] == key and time.time() - LIVE["t"] < 15:
            return LIVE["data"]
        missing = [p for p in pairs if p not in LIVE["alt"].values()]
        if missing:
            for k, v in _kraken_public("AssetPairs", {"pair": ",".join(missing)}).items():
                LIVE["alt"][k] = v.get("altname", k)
        out = {}
        for k, v in _kraken_public("Ticker", {"pair": key}).items():
            alt = LIVE["alt"].get(k, k)
            out[alt] = {"last": float(v["c"][0]), "bid": float(v["b"][0]), "ask": float(v["a"][0]),
                        "open24": float(v["o"])}
        LIVE.update(t=time.time(), key=key, data={"t": time.time(), "px": out})
        return LIVE["data"]


def run_bg(cmd):
    """desk.sh-Befehl im Hintergrund starten, Ausgabe ins control.log."""
    def go():
        try:
            r = subprocess.run([os.path.join(HERE, "desk.sh"), cmd], cwd=HERE, capture_output=True, text=True, timeout=1200)
            control_log(f"{cmd}: {(r.stdout + r.stderr).strip()[-600:]}")
        except Exception as e:  # noqa: BLE001
            control_log(f"{cmd}: FEHLER {e}")
    threading.Thread(target=go, daemon=True).start()


def busy():
    lock = os.path.join(HERE, ".lock")
    try:
        import fcntl
        with open(lock, "a") as f:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(f, fcntl.LOCK_UN)
        return False
    except (BlockingIOError, OSError):
        return True


# ---------------------------------------------------------------- Updates von GitHub
GH_REPO = "thecelestis-commits/Kraken-desk"
GH_BRANCH = "main"
GH_TOKEN_FILE = os.path.join(HERE, ".github_token")
GH_CACHE = {"t": 0.0, "latest": None, "err": None}


def _gh_get(url_public, url_api, accept):
    import urllib.request
    tok = read(GH_TOKEN_FILE)
    if tok:
        req = urllib.request.Request(url_api, headers={"Authorization": f"Bearer {tok}", "Accept": accept,
                                                       "User-Agent": "kraken-desk"})
    else:
        req = urllib.request.Request(url_public, headers={"User-Agent": "kraken-desk"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


def gh_latest(force=False):
    if not force and time.time() - GH_CACHE["t"] < 300:
        return GH_CACHE["latest"], GH_CACHE["err"]
    try:
        v = _gh_get(f"https://raw.githubusercontent.com/{GH_REPO}/{GH_BRANCH}/VERSION",
                    f"https://api.github.com/repos/{GH_REPO}/contents/VERSION?ref={GH_BRANCH}",
                    "application/vnd.github.raw").decode().strip()
        GH_CACHE.update(t=time.time(), latest=v, err=None)
    except Exception as e:  # noqa: BLE001
        msg = "Repository nicht erreichbar (privat? dann GitHub-Token eintragen)" if "404" in str(e) else str(e)
        GH_CACHE.update(t=time.time(), latest=None, err=msg)
    return GH_CACHE["latest"], GH_CACHE["err"]


def gh_download():
    return _gh_get(f"https://codeload.github.com/{GH_REPO}/zip/refs/heads/{GH_BRANCH}",
                   f"https://api.github.com/repos/{GH_REPO}/zipball/{GH_BRANCH}",
                   "application/vnd.github+json")


def apply_update(blob):
    z = zipfile.ZipFile(io.BytesIO(blob))
    names = [n for n in z.namelist() if not n.endswith("/")]
    prefix = os.path.commonprefix(names) if len(names) > 1 else ""
    prefix = prefix[: prefix.rfind("/") + 1] if "/" in prefix else ""
    files = {}
    for n in names:
        rel = n[len(prefix):]
        if "__pycache__" in rel:
            continue
        if not ALLOWED.match(rel):
            continue                     # z. B. .gitignore aus dem GitHub-Archiv – wird nie geschrieben
        files[rel] = z.read(n)
    if "run.py" not in files or "desk/engine.py" not in files:
        raise ValueError("Das ist kein Meme-Desk-Paket.")
    for rel, data in files.items():
        dst = os.path.join(HERE, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        tmp = dst + ".new"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, dst)
        if rel.endswith(".sh"):
            os.chmod(dst, 0o755)
    return len(files)


# ---------------------------------------------------------------- Server
class H(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        b = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj, default=str, ensure_ascii=False))

    def _body(self, limit=5_000_000):
        n = int(self.headers.get("Content-Length", 0))
        if n > limit:
            raise ValueError("zu groß")
        return self.rfile.read(n)

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == "/api/status":
            cur = read(os.path.join(HERE, "mode"), "paper")
            mode = q.get("mode", [cur])[0]
            mode = mode if mode in ("live", "paper") else cur
            return self._json(200, {
                "status": read_json(os.path.join(LOG, f"status_{mode}.json"), {}),
                "events": tail_jsonl(os.path.join(LOG, f"{mode}_events.jsonl")),
                "equity": equity(os.path.join(LOG, f"{mode}_equity.csv")),
                "modes": [m for m in ("live", "paper") if os.path.exists(os.path.join(LOG, f"status_{m}.json"))],
                "control": {"mode": cur, "viewing": mode, "enabled": read(os.path.join(HERE, "enabled"), "on") == "on",
                            "pin_set": pin_set(), "keys_set": os.path.exists(ENV_FILE), "busy": busy(),
                            "version": read(os.path.join(HERE, "VERSION"), "?"),
                            "latest": gh_latest()[0], "gh_err": gh_latest()[1],
                            "log": read(os.path.join(LOG, "control.log")).splitlines()[-6:],
                            "runlog": read(os.path.join(LOG, "run.log")).splitlines()[-4:]},
            })
        if u.path == "/api/live":
            cur = read(os.path.join(HERE, "mode"), "paper")
            mode = q.get("mode", [cur])[0]
            mode = mode if mode in ("live", "paper") else cur
            st = read_json(os.path.join(LOG, f"status_{mode}.json"), {}) or {}
            try:
                return self._json(200, {"ok": True, **live_prices([p["pair"] for p in st.get("positions", [])])})
            except Exception as e:  # noqa: BLE001
                return self._json(200, {"ok": False, "msg": str(e)})
        if u.path in ("/", "/index.html"):
            with open(os.path.join(HERE, "dashboard.html"), "rb") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8")
        self._send(404, "nicht gefunden", "text/plain; charset=utf-8")

    def do_POST(self):
        u = urlparse(self.path)
        try:
            if u.path == "/api/update_pull":
                ok, why = check_pin(json.loads(self._body() or b"{}").get("pin"))
                if not ok:
                    return self._json(403, {"ok": False, "msg": why})
                latest, err = gh_latest(force=True)
                if not latest:
                    return self._json(400, {"ok": False, "msg": f"GitHub: {err}"})
                cur = read(os.path.join(HERE, "VERSION"), "?")
                if latest == cur:
                    return self._json(200, {"ok": True, "msg": f"Schon aktuell (Version {cur})."})
                n = apply_update(gh_download())
                control_log(f"Update von GitHub eingespielt ({n} Dateien), Version {read(os.path.join(HERE, 'VERSION'), '?')}")
                GH_CACHE["t"] = 0
                threading.Timer(1.0, lambda: os._exit(0)).start()
                return self._json(200, {"ok": True, "msg": f"Version {latest} eingespielt. Dashboard startet neu …"})

            if u.path == "/api/update":
                ok, why = check_pin(self.headers.get("X-PIN", ""))
                if not ok:
                    return self._json(403, {"ok": False, "msg": why})
                n = apply_update(self._body(20_000_000))
                control_log(f"Update eingespielt ({n} Dateien), Version {read(os.path.join(HERE, 'VERSION'), '?')}")
                threading.Timer(1.0, lambda: os._exit(0)).start()   # systemd startet das Dashboard neu
                return self._json(200, {"ok": True, "msg": f"Update eingespielt ({n} Dateien). Dashboard startet neu …"})

            d = json.loads(self._body() or b"{}")
            if u.path == "/api/setup":
                first = not pin_set()
                if not first:
                    ok, why = check_pin(d.get("pin"))
                    if not ok:
                        return self._json(403, {"ok": False, "msg": why})
                msg = []
                newpin = str(d.get("new_pin") or (d.get("pin") if first else "") or "")
                if (first or newpin) and not re.fullmatch(r"\d{4,8}", newpin):
                    return self._json(400, {"ok": False, "msg": "PIN: 4 bis 8 Ziffern."})
                ght = (d.get("gh_token") or "").strip()
                if ght:
                    write_private(GH_TOKEN_FILE, ght)
                    GH_CACHE["t"] = 0
                    msg.append("GitHub-Token gespeichert")
                key, sec = (d.get("key") or "").strip(), (d.get("secret") or "").strip()
                if key or sec:                       # erst Keys prüfen, dann irgendetwas speichern
                    try:
                        bal = test_keys(key, sec)
                    except Exception as e:  # noqa: BLE001
                        return self._json(400, {"ok": False, "msg": f"Kraken lehnt die Keys ab: {e}"})
                    write_private(ENV_FILE, f"KRAKEN_API_KEY={key}\nKRAKEN_API_SECRET={sec}\n")
                    msg.append(f"Kraken verbunden – Guthaben {bal:.2f} €")
                if newpin:
                    salt = secrets.token_hex(16)
                    write_private(PIN_FILE, json.dumps({"salt": salt, "hash": pin_hash(newpin, salt)}))
                    msg.append("PIN gespeichert")
                control_log("Einrichtung: " + ", ".join(msg))
                return self._json(200, {"ok": True, "msg": ". ".join(msg) or "nichts geändert"})

            if u.path == "/api/control":
                ok, why = check_pin(d.get("pin"))
                if not ok:
                    return self._json(403, {"ok": False, "msg": why})
                act = d.get("action")
                if act == "live" and not os.path.exists(ENV_FILE):
                    return self._json(400, {"ok": False, "msg": "Erst Kraken-Keys eintragen."})
                if act in ("on", "off", "live", "paper"):
                    r = subprocess.run([os.path.join(HERE, "desk.sh"), act], cwd=HERE, capture_output=True, text=True)
                    control_log(f"{act}: {r.stdout.strip()}")
                    if act in ("live", "paper"):
                        run_bg("tick")          # gleich einen Lauf im neuen Modus, damit das Dashboard Daten hat
                    return self._json(200, {"ok": True, "msg": r.stdout.strip()})
                if act in ("tick", "closeall"):
                    if busy():
                        return self._json(409, {"ok": False, "msg": "Der Bot arbeitet gerade – gleich nochmal."})
                    run_bg(act)
                    return self._json(200, {"ok": True, "msg": "Läuft – Ergebnis in ein paar Sekunden." if act == "tick"
                                            else "Notaus läuft: alles wird verkauft, danach ist der Bot aus."})
                return self._json(400, {"ok": False, "msg": "unbekannte Aktion"})
        except Exception as e:  # noqa: BLE001
            return self._json(500, {"ok": False, "msg": f"Fehler: {e}"})
        self._send(404, "nicht gefunden", "text/plain; charset=utf-8")

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=os.environ.get("DASH_HOST", "0.0.0.0"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("DASH_PORT", 8080)))
    a = ap.parse_args()
    print(f"Dashboard läuft auf http://{a.host}:{a.port}", flush=True)
    threading.Thread(target=self_check, daemon=True).start()
    ThreadingHTTPServer((a.host, a.port), H).serve_forever()

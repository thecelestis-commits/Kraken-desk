#!/usr/bin/env bash
# Meme-Desk Installer für Debian/Ubuntu.   Aufruf:  sudo ./install.sh
# Installiert nach /opt/meme-desk, legt Benutzer "memedesk" an, richtet einen
# systemd-Timer (alle 4h kurz nach Kerzenschluss) und das Dashboard (Port 8080) ein.
# Danach muss niemand mehr auf den Server: Ben richtet PIN + Kraken-Keys im Dashboard ein,
# schaltet dort an/aus und Paper/Live und spielt Updates selbst ein.
# Optionen per Umgebung: INSTALL_DIR, DASH_HOST (Standard: Tailscale-IP, sonst 0.0.0.0),
#                        DASH_PORT (8080), NO_SYSTEMD=1
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
DIR="${INSTALL_DIR:-/opt/meme-desk}"
USER_NAME="${DESK_USER:-memedesk}"
# Dashboard nur im Tailnet: Tailscale-IP automatisch nehmen, sonst alle Schnittstellen
if [[ -z "${DASH_HOST:-}" ]] && command -v tailscale >/dev/null && tailscale ip -4 >/dev/null 2>&1; then
  DASH_HOST="$(tailscale ip -4 | head -1)"
fi
DASH_HOST="${DASH_HOST:-0.0.0.0}"
DASH_PORT="${DASH_PORT:-8080}"

say(){ printf '\n\033[1;32m==>\033[0m %s\n' "$*"; }

if [[ -z "${NO_SYSTEMD:-}" && $EUID -ne 0 ]]; then
  echo "Bitte mit sudo starten:  sudo ./install.sh"; exit 1
fi

say "Pakete prüfen"
if ! python3 -c 'import venv, ensurepip' 2>/dev/null; then
  if command -v apt-get >/dev/null; then apt-get update -qq && apt-get install -y -qq python3 python3-venv python3-pip; fi
fi
python3 - <<'PY'
import sys
assert sys.version_info >= (3, 9), "Python 3.9+ nötig"
print("Python", sys.version.split()[0], "ok")
PY

say "Dateien nach $DIR kopieren"
mkdir -p "$DIR"
cp -r "$SRC"/desk "$SRC"/rules.yaml "$SRC"/run.py "$SRC"/backtest.py "$SRC"/dashboard.py \
      "$SRC"/dashboard.html "$SRC"/desk.sh "$SRC"/requirements.txt "$SRC"/README.md "$SRC"/VERSION "$DIR"/
cp "$SRC"/run_watch.py "$DIR"/ 2>/dev/null || true
chmod +x "$DIR/desk.sh"
mkdir -p "$DIR/logs"
[[ -f "$DIR/mode" ]] || echo paper > "$DIR/mode"
[[ -f "$DIR/enabled" ]] || echo on > "$DIR/enabled"

say "Python-Umgebung einrichten (dauert 1–2 Minuten)"
python3 -m venv "$DIR/venv"
"$DIR/venv/bin/pip" install -q --upgrade pip
"$DIR/venv/bin/pip" install -q -r "$DIR/requirements.txt"

# Keys trägt Ben selbst im Dashboard ein. Nur falls per Umgebung mitgegeben, hier speichern.
if [[ ! -f "$DIR/.env" && -n "${KRAKEN_API_KEY:-}" && -n "${KRAKEN_API_SECRET:-}" ]]; then
  umask 077; printf 'KRAKEN_API_KEY=%s\nKRAKEN_API_SECRET=%s\n' "$KRAKEN_API_KEY" "$KRAKEN_API_SECRET" > "$DIR/.env"
fi
if [[ -f "$DIR/.env" ]]; then chmod 600 "$DIR/.env"; fi

if [[ -n "${NO_SYSTEMD:-}" ]]; then
  say "NO_SYSTEMD gesetzt – fertig ohne Dienste. Manuell: $DIR/desk.sh tick"; exit 0
fi

say "Benutzer $USER_NAME anlegen"
id "$USER_NAME" >/dev/null 2>&1 || useradd --system --home "$DIR" --shell /usr/sbin/nologin "$USER_NAME"
chown -R "$USER_NAME:$USER_NAME" "$DIR"

say "systemd-Dienste einrichten"
cat > /etc/systemd/system/meme-desk-run.service <<EOF
[Unit]
Description=Meme-Desk – ein Durchlauf des Fließbands
Wants=network-online.target
After=network-online.target
[Service]
Type=oneshot
User=$USER_NAME
WorkingDirectory=$DIR
EnvironmentFile=-$DIR/.env
ExecStart=$DIR/desk.sh tick
TimeoutStartSec=900
EOF

cat > /etc/systemd/system/meme-desk-run.timer <<EOF
[Unit]
Description=Meme-Desk alle 4 Stunden kurz nach Kerzenschluss
[Timer]
OnCalendar=*-*-* 00,04,08,12,16,20:02:00 UTC
Persistent=true
[Install]
WantedBy=timers.target
EOF

cat > /etc/systemd/system/meme-desk-dashboard.service <<EOF
[Unit]
Description=Meme-Desk Dashboard
After=network-online.target
[Service]
User=$USER_NAME
WorkingDirectory=$DIR
ExecStart=$DIR/venv/bin/python $DIR/dashboard.py --host $DASH_HOST --port $DASH_PORT
Restart=always
RestartSec=5
[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now meme-desk-run.timer meme-desk-dashboard.service
say "Erster Durchlauf (Paper-Modus, dauert ~1 Minute)"
systemctl start meme-desk-run.service || true
journalctl -u meme-desk-run.service -n 15 --no-pager || true

say "FERTIG"
if [[ "$DASH_HOST" == 0.0.0.0 ]]; then echo "Dashboard:  http://<tailnet-adresse-dieser-vm>:$DASH_PORT"; else echo "Dashboard:  http://$DASH_HOST:$DASH_PORT"; fi
echo "Ben öffnet den Link, legt seine PIN fest und trägt dort die Kraken-Keys ein."
echo "Alles Weitere (an/aus, Paper/Live, Updates) macht er im Dashboard."

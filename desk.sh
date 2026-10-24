#!/usr/bin/env bash
# Steuerung des Desks (normalerweise übers Dashboard, das ruft genau diese Befehle auf).
#   desk.sh tick       – ein Durchlauf (macht der Timer alle 4h automatisch)
#   desk.sh on | off   – Bot an/aus (aus = keine neuen Käufe, keine Durchläufe; Börsen-Stops bleiben)
#   desk.sh live|paper – Modus wechseln
#   desk.sh closeall   – Notaus: alle Positionen im aktuellen Modus sofort verkaufen, dann aus
#   desk.sh status
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"
MODE="$(cat "$DIR/mode" 2>/dev/null || echo paper)"
PY="$DIR/venv/bin/python"; [[ -x "$PY" ]] || PY=python3
LIVEFLAG=""; [[ "$MODE" == live ]] && LIVEFLAG="--live"
loadenv(){ if [[ -f "$DIR/.env" ]]; then set -a; . "$DIR/.env"; set +a; fi; }
locked(){ exec 9>"$DIR/.lock"; flock -w 600 9; }

case "${1:-status}" in
  tick)
    [[ "$(cat "$DIR/enabled" 2>/dev/null || echo on)" == on ]] || { echo "Bot ist aus."; exit 0; }
    [[ "$MODE" == live && ! -f "$DIR/.env" ]] && { echo "Live ohne Kraken-Keys – übersprungen."; exit 0; }
    loadenv; locked; mkdir -p logs
    { echo "--- $(date -u '+%d.%m %H:%M') UTC Lauf ($MODE)"; "$PY" run.py $LIVEFLAG 2>&1; } | tee -a logs/run.log | tail -20 ;;
  closeall)
    loadenv; locked; mkdir -p logs
    { echo "--- $(date -u '+%d.%m %H:%M') UTC NOTAUS ($MODE)"; "$PY" run.py $LIVEFLAG --close-all 2>&1; } | tee -a logs/run.log | tail -20
    echo off > "$DIR/enabled" ;;
  on)    echo on  > "$DIR/enabled"; echo "Bot an." ;;
  off)   echo off > "$DIR/enabled"; echo "Bot aus." ;;
  live)  echo live  > "$DIR/mode"; echo "LIVE – ab dem nächsten Lauf echtes Geld." ;;
  paper) echo paper > "$DIR/mode"; echo "Paper. Offene Live-Positionen bleiben bei Kraken (mit Stop)." ;;
  status)
    echo "Modus: $MODE | Bot: $(cat "$DIR/enabled" 2>/dev/null || echo on)"
    systemctl list-timers meme-desk-run.timer --no-pager 2>/dev/null | head -3 || true ;;
  *) sed -n '2,8p' "$0" ;;
esac

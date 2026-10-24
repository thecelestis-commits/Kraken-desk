# Kraken-Desk – Bot mit 6-Stufen-Fließband

Screener → Kartograf → Risikomanager → Gate → Exit-Verwalter → Auditor.
Alle Stufen lesen **eine** Regeldatei: `rules.yaml`.

## Installation (Marco, einmalig)

```bash
unzip meme-desk.zip && cd meme-desk
sudo ./install.sh
```

Mehr ist nicht zu tun. Das Skript installiert nach `/opt/meme-desk`, nimmt für das
Dashboard automatisch die Tailscale-IP der VM (nur im Tailnet erreichbar) und richtet ein:

| Dienst | Was |
|---|---|
| `meme-desk-run.timer` | Durchlauf alle 4h um 00:02, 04:02, … UTC |
| `meme-desk-dashboard.service` | Dashboard + Steuerung auf Port 8080 |

Ausgehend braucht die VM nur HTTPS zu `api.kraken.com`. Eingehend nichts.

## Alles Weitere macht Ben im Dashboard

- **Ersteinrichtung:** PIN festlegen, Kraken-Key und -Secret eintragen (werden sofort geprüft)
- **Bot an/aus**, **Paper/Live**, **Jetzt prüfen**
- **Notaus:** alle Positionen sofort verkaufen, Bot aus
- **Update holen:** zieht die neueste Version aus GitHub (`thecelestis-commits/kraken-desk`, Branch `main`).
  Bei privatem Repo einmal unter „Keys/PIN ändern" einen GitHub-Token (nur Lesezugriff auf dieses Repo) eintragen.
- **Zip:** alternativ eine Update-Zip von Hand hochladen – Keys, PIN, Modus und Kontostand bleiben immer erhalten

Start ist immer Paper. Fallback auf der VM: `sudo /opt/meme-desk/desk.sh status|on|off|live|paper|closeall`

## Sicherheit

- API-Key nur mit Rechten: Guthaben abfragen, Orders abfragen/erstellen/stornieren,
  Hauptbuch abfragen. **Keine** Ein-/Auszahlungsrechte.
- Keys und PIN liegen in `/opt/meme-desk/.env` bzw. `.pin` (nur für Benutzer `memedesk` lesbar, PIN nur als Hash).
- 5 falsche PINs → 10 Minuten Sperre.
- Jede Live-Position hat eine echte Stop-Loss-Order bei Kraken – auch wenn die VM ausfällt.
- Notaus: −30 % vom Höchststand → keine neuen Trades. Tageslimit −6 %.

## Strategie & Tests

4h-Kerzen, die 40 umsatzstärksten EUR-Paare bei Kraken (täglich neu), strenge Marktampel:
gekauft wird nur, wenn BTC über seinem 200-Tage-Schnitt steht.
12-Monats-Test (Okt 2025–Okt 2026, 41 Coins, Binance-Kurse als Ersatz, BTC −27 %,
alle Coins gleichgewichtet −33 %): **−2 %**, max. Drawdown −25 %. Mit lockerer Ampel −24 %,
Dauer-Überwachung (minütlich) −22 % bis −26 % → darum 4h + strenge Ampel.

## Dateien

| Datei | Inhalt |
|---|---|
| `rules.yaml` | alle Regeln |
| `desk/stages.py` | die 6 Stufen |
| `desk/engine.py` | Fließband, gleich für Backtest/Paper/Live |
| `desk/kraken_live.py` | echte Orders (Limit-Order post-only, Fallback Market; Stop-Loss an der Börse) |
| `run.py` | ein Durchlauf |
| `run_watch.py` | Dauer-Überwachung (getestet, schlechter – nicht aktiv) |
| `dashboard.py/.html` | Live-Dashboard |
| `logs/` | Status, Ereignisse, Kontoverlauf |

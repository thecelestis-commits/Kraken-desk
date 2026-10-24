"""Ein Durchlauf des Desks (alle 4h nach Kerzenschluss starten, z. B. 00:05, 04:05, …).
  python run.py            -> Paper-Trading mit echten Kursen, simulierte Orders
  python run.py --live     -> echte Orders (nur wenn live.enabled: true in rules.yaml
                              und KRAKEN_API_KEY / KRAKEN_API_SECRET gesetzt)"""
import argparse
import json
import os
from datetime import datetime, timezone

import pandas as pd

from backtest import load_rules, HERE
from desk.data import asset_pairs, load_universe, ohlc, ticker
from desk.indicators import enrich
from desk.engine import Desk, SimBroker

NEED = ["ema_slow", "atr", "hh", "vol_avg", "vol_eur_24h", "roc"]


def read_old_status(logdir, mode):
    try:
        with open(os.path.join(logdir, f"status_{mode}.json")) as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--close-all", action="store_true", help="Notaus: alle Positionen sofort verkaufen")
    a = ap.parse_args()
    R = load_rules()
    mode = "live" if a.live else "paper"
    mode_file = os.path.join(HERE, "mode")
    switched_live = os.path.exists(mode_file) and open(mode_file).read().strip() == "live"
    if a.live and not (R["live"]["enabled"] or switched_live):
        raise SystemExit("Live ist nicht freigeschaltet (Dashboard-Schalter oder live.enabled).")

    logdir = os.path.join(HERE, R["auditor"]["log_dir"])
    os.makedirs(logdir, exist_ok=True)
    sf = os.path.join(HERE, f"state_{mode}.json")
    st = json.load(open(sf)) if os.path.exists(sf) else None

    iv = R["timeframe_minutes"]
    from desk.data import resolve_universe
    uni = resolve_universe(R)
    held = list(((st or {}).get("desk") or {}).get("positions", {}).keys())
    R["universe"] = uni + [p for p in held if p not in uni]   # offene Positionen immer weiter verwalten
    info = asset_pairs(R["universe"])
    data = load_universe(R["universe"], iv)
    enr = {p: enrich(df, R) for p, df in data.items() if len(df)}
    from desk.indicators import regime_flags, regime_at
    reg_df = ohlc(R["cartographer"]["regime_pair"], 1440)       # Tageskerzen, ~2 Jahre
    flags, btc_daily, btc_ema = regime_flags(reg_df, R["cartographer"]["regime_ema"])

    if a.live:
        from desk.kraken_live import KrakenBroker
        broker = KrakenBroker(R, info, state=(st or {}).get("broker"))
    else:
        broker = SimBroker(R)
    desk = Desk(R, broker=broker)
    times = sorted(set().union(*[df.index for df in enr.values()]))
    reg = regime_at(flags, times + [pd.Timestamp(datetime.now(timezone.utc))])

    if st is None:
        if a.live:
            desk.cash = broker.balance_eur()
            desk.rs.equity = desk.rs.peak = desk.cash
        st = {"last_t": str(times[-1]), "started": str(datetime.now(timezone.utc))}
        print(f"[{mode}] Desk gestartet mit {desk.cash:.2f} EUR. Ab der nächsten Kerze wird gehandelt.")
    else:
        desk.load_state(st["desk"])
        if a.live:  # nie mit mehr Cash rechnen, als wirklich da ist
            desk.cash = min(desk.cash, broker.balance_eur())

    if a.close_all and desk.positions:
        tk = ticker(list(desk.positions))
        alt = {v["key"]: k for k, v in info.items()}
        bids = {alt.get(k, k): v["bid"] for k, v in tk.items()}
        for p in list(desk.positions):
            desk.close_position(datetime.now(timezone.utc), p, bids.get(p, desk.last_close.get(p)), "Notaus (manuell)")
        desk.pending = []

    last = pd.Timestamp(st["last_t"])
    new_times = [] if a.close_all else [t for t in times if t > last]
    spreads = {}
    if new_times:
        tk = ticker(list(enr))
        alt = {v["key"]: k for k, v in info.items()}
        spreads = {alt.get(k, k): v["spread_pct"] for k, v in tk.items()}
    n_events_before = 0          # Auditor ist pro Lauf frisch: alles von diesem Lauf schreiben
    for t in new_times:
        rows, nb = {}, {}
        for p, df in enr.items():
            if t in df.index:
                k = df.index.get_loc(t)
                r = df.iloc[k]
                if not r[NEED].isna().any():
                    rows[p], nb[p] = r, k + 1
        allow = bool(reg.get(t, False)) if R["cartographer"].get("regime_enabled") else True
        desk.process_bar(t, rows, nb, pair_info=info, spreads=spreads if t == new_times[-1] else None,
                         allow_new=allow and t == new_times[-1])

    # Signale der letzten Kerze sofort zum aktuellen Ask ausführen
    if desk.pending:
        tk = ticker([p for p, _, _ in desk.pending])
        alt = {v["key"]: k for k, v in info.items()}
        quotes = {alt.get(k, k): {"open": v["ask"]} for k, v in tk.items()}
        desk.fill_pending(datetime.now(timezone.utc), quotes, info)

    if new_times:
        st["last_t"] = str(new_times[-1])
    st["desk"] = desk.state()
    if a.live:
        st["broker"] = broker.state()
    json.dump(st, open(sf, "w"), indent=1, default=str)

    with open(os.path.join(logdir, f"{mode}_events.jsonl"), "a") as f:
        for e in desk.aud.events[n_events_before:]:
            f.write(json.dumps(e, ensure_ascii=False, default=str) + "\n")
    with open(os.path.join(logdir, f"{mode}_equity.csv"), "a") as f:
        f.write(f"{datetime.now(timezone.utc).isoformat()},{desk.equity():.2f}\n")

    # Status für das Dashboard
    status = {
        "mode": mode,
        "updated": datetime.now(timezone.utc).isoformat(),
        "last_bar": st["last_t"],
        "timeframe_min": iv,
        "regime": {"green": bool(flags.iloc[-1]), "btc": float(btc_daily.iloc[-1]),
                   "ema": float(btc_ema.iloc[-1]), "label": f"BTC über {R['cartographer']['regime_ema']}-Tage-Schnitt"},
        "equity": round(desk.equity(), 2), "cash": round(desk.cash, 2),
        "start_equity": st.setdefault("start_equity", round(desk.rs.peak if not st.get("desk") else desk.equity(), 2)),
        "peak": round(desk.rs.peak, 2), "fees_paid": round(desk.fees_paid, 2),
        "halted": desk.rs.halted, "halted_day": desk.rs.halted_day,
        "positions": [{"pair": p, "entry": pos.entry, "now": desk.last_close.get(p, pos.entry), "stop": pos.stop,
                       "eur": round(pos.cost_eur, 2), "qty": pos.qty, "entry_time": str(pos.entry_time),
                       "trailing": pos.trailing, "be": pos.be_done,
                       "pnl_pct": round((desk.last_close.get(p, pos.entry) / pos.entry - 1) * 100, 2)}
                      for p, pos in desk.positions.items()],
        "funnel": desk.funnel or ((read_old_status(logdir, mode) or {}).get("funnel") or {}),
        "rules": {"risk_pct": R["risk"]["risk_per_trade_pct"], "max_pos": R["risk"]["max_open_positions"],
                  "min_score": R["gate"]["min_score"], "dd_stop": R["risk"]["max_drawdown_pct"]},
    }
    st["start_equity"] = status["start_equity"]
    json.dump(st, open(sf, "w"), indent=1, default=str)
    json.dump(status, open(os.path.join(logdir, f"status_{mode}.json"), "w"), indent=1, default=str)

    # Kurzbericht
    print(f"[{mode}] {datetime.now(timezone.utc):%d.%m.%Y %H:%M} UTC | neue Kerzen: {len(new_times)} | "
          f"Marktampel: {'grün' if flags.iloc[-1] else 'rot'}")
    print(f"Konto: {desk.equity():.2f} EUR (Cash {desk.cash:.2f}) | Desk {'GESCHLOSSEN' if desk.rs.halted else 'offen'}")
    for e in desk.aud.events:
        if e["type"] == "BUY":
            print(f"  KAUF  {e['pair']:<12} {e['eur']:>7.2f} EUR @ {e['price']:.8g}  Stop {e['stop']:.8g}  Score {e['score']}/6")
        else:
            print(f"  VERKAUF {e['pair']:<10} @ {e['price']:.8g}  {e['pnl_eur']:+.2f} EUR  ({e['reason']})")
    for p, pos in desk.positions.items():
        lc = desk.last_close.get(p, pos.entry)
        print(f"  offen {p:<12} Einstieg {pos.entry:.8g}  jetzt {lc:.8g}  ({(lc / pos.entry - 1) * 100:+.1f}%)  Stop {pos.stop:.8g}")
    if desk.aud.violations:
        print("AUDITOR-WARNUNG:", desk.aud.violations)


if __name__ == "__main__":
    main()

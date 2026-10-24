"""Dauer-Überwachung: läuft ununterbrochen (systemd-Dienst).
  python run_watch.py          -> Paper (echte Kurse, simulierte Orders)
  python run_watch.py --live   -> echte Orders (live.enabled: true + Kraken-Keys nötig)
Jede Minute alle Kurse prüfen, Kauf im Moment des Ausbruchs, Stops laufend nachziehen.
Setups werden einmal pro Stunde aus abgeschlossenen 1h-Kerzen neu berechnet."""
import argparse
import json
import os
import signal
import time
import traceback
from datetime import datetime, timezone

import pandas as pd

from backtest import load_rules, HERE
from desk.data import asset_pairs, ohlc, ticker, _get
from desk.indicators import enrich
from desk.engine import SimBroker
from desk.watch import WatchDesk, regime_map

STOP = False


def _sig(*_):
    global STOP
    STOP = True


def now_utc():
    return datetime.now(timezone.utc)


def log(msg):
    print(f"{now_utc():%d.%m %H:%M:%S} {msg}", flush=True)


class Runner:
    def __init__(self, live):
        self.R = load_rules()
        self.mode = "live" if live else "paper"
        if live and not self.R["live"]["enabled"]:
            raise SystemExit("Live ist in rules.yaml ausgeschaltet (live.enabled: false).")
        self.logdir = os.path.join(HERE, self.R["auditor"]["log_dir"])
        os.makedirs(self.logdir, exist_ok=True)
        self.sf = os.path.join(HERE, f"state_watch_{self.mode}.json")
        self.info = asset_pairs(self.R["universe"])
        self.alt = {v["key"]: k for k, v in self.info.items()}
        if live:
            from desk.kraken_live import KrakenBroker
            st = json.load(open(self.sf)) if os.path.exists(self.sf) else {}
            self.broker = KrakenBroker(self.R, self.info, state=st.get("broker"))
        else:
            self.broker = SimBroker(self.R)
        self.desk = WatchDesk(self.R, broker=self.broker)
        self.meta = {}
        if os.path.exists(self.sf):
            st = json.load(open(self.sf))
            self.desk.load_state(st["desk"])
            self.meta = st.get("meta", {})
            if live:
                self.desk.cash = min(self.desk.cash, self.broker.balance_eur())
        else:
            if live:
                self.desk.cash = self.broker.balance_eur()
                self.desk.rs.equity = self.desk.rs.peak = self.desk.cash
            self.meta = {"started": str(now_utc()), "start_equity": round(self.desk.cash, 2)}
        self.last_setup_hour = None
        self.last_equity_log = 0
        self.n_events = 0
        self.btc = None

    # ---- stündlich: Setups aus abgeschlossenen 1h-Kerzen ----
    def refresh_setups(self):
        X = self.desk.X
        btc = ohlc(self.R["cartographer"]["regime_pair"], 60)
        reg = regime_map(btc, X)
        green = bool(reg.iloc[-1]) if len(reg) else False
        self.btc = {"price": float(btc["close"].iloc[-1]),
                    "ema": float(btc["close"].resample("240min", label="left", closed="left").last().dropna()
                                 .ewm(span=X["cartographer"]["regime_ema"], adjust=False).mean().iloc[-1])}
        for p in self.R["universe"]:
            try:
                df = enrich(ohlc(p, 60), X)
                if len(df):
                    self.desk.update_setup(p, df.iloc[-1], len(df), green)
            except Exception as e:  # noqa: BLE001
                log(f"[setup] {p}: {e}")
            time.sleep(1.1)  # Kraken-Limit schonen
        log(f"Setups erneuert – Marktampel {'grün' if green else 'rot'}")

    def forming_volume(self, pair):
        """Volumen der laufenden Stunde, hochgerechnet auf 60 Min."""
        res = _get("OHLC", {"pair": pair, "interval": 60})
        key = [k for k in res if k != "last"][0]
        row = res[key][-1]
        start = pd.Timestamp(int(row[0]), unit="s", tz="UTC")
        mins = max(1.0, (now_utc() - start.to_pydatetime()).total_seconds() / 60)
        return float(row[6]) * 60 / mins, mins

    # ---- jede Minute ----
    def tick(self):
        d = self.desk
        tk = ticker(self.R["universe"])
        now = pd.Timestamp(now_utc())
        for key, q in tk.items():
            p = self.alt.get(key, key)
            if p not in d.setups and p not in d.positions:
                continue
            last = q["last"]
            vol_proj, mins = None, 0
            st = d.setups.get(p)
            if (p not in d.positions and st and st["screen_ok"] and d.regime_green
                    and not pd.isna(st["hh"]) and last > st["hh"]):
                try:
                    vol_proj, mins = self.forming_volume(p)
                except Exception as e:  # noqa: BLE001
                    log(f"[vol] {p}: {e}")
            try:
                d.on_tick(p, now, last, entry_ref=q["ask"], vol_proj=vol_proj, minutes_in=mins,
                          spread=q["spread_pct"], pair_info=self.info.get(p))
            except Exception as e:  # noqa: BLE001
                log(f"[tick] {p}: {e}\n{traceback.format_exc()}")
        d.end_of_tick(now)

    def save(self):
        d = self.desk
        st = {"desk": d.state(), "meta": self.meta}
        if self.mode == "live":
            st["broker"] = self.broker.state()
        tmp = self.sf + ".tmp"
        json.dump(st, open(tmp, "w"), indent=1, default=str)
        os.replace(tmp, self.sf)
        with open(os.path.join(self.logdir, f"{self.mode}_events.jsonl"), "a") as f:
            for e in d.aud.events[self.n_events:]:
                f.write(json.dumps(e, ensure_ascii=False, default=str) + "\n")
                log(f"{e['type']} {e['pair']} @ {e['price']:.8g}" + (f"  {e.get('pnl_eur', 0):+.2f} EUR ({e.get('reason')})" if e["type"] == "SELL" else f"  {e['eur']} EUR"))
        self.n_events = len(d.aud.events)
        if time.time() - self.last_equity_log > 900:
            with open(os.path.join(self.logdir, f"{self.mode}_equity.csv"), "a") as f:
                f.write(f"{now_utc().isoformat()},{d.equity():.2f}\n")
            self.last_equity_log = time.time()
        R = self.R
        status = {
            "mode": self.mode, "engine": "watch", "updated": now_utc().isoformat(),
            "last_bar": now_utc().isoformat(), "timeframe_min": 1,
            "regime": {"green": d.regime_green, "btc": (self.btc or {}).get("price"), "ema": (self.btc or {}).get("ema")},
            "equity": round(d.equity(), 2), "cash": round(d.cash, 2), "start_equity": self.meta.get("start_equity"),
            "peak": round(d.rs.peak, 2), "fees_paid": round(d.fees_paid, 2),
            "halted": d.rs.halted, "halted_day": d.rs.halted_day,
            "positions": [{"pair": p, "entry": pos.entry, "now": d.prices.get(p, pos.entry), "stop": pos.stop,
                           "eur": round(pos.cost_eur, 2), "qty": pos.qty, "entry_time": str(pos.entry_time),
                           "trailing": pos.trailing, "be": pos.be_done,
                           "pnl_pct": round((d.prices.get(p, pos.entry) / pos.entry - 1) * 100, 2)}
                          for p, pos in d.positions.items()],
            "funnel": d.funnel,
            "rules": {"risk_pct": R["risk"]["risk_per_trade_pct"], "max_pos": R["risk"]["max_open_positions"],
                      "min_score": R["watch"]["min_score"], "max_score": 7, "dd_stop": R["risk"]["max_drawdown_pct"]},
        }
        tmp = os.path.join(self.logdir, f"status_{self.mode}.json.tmp")
        json.dump(status, open(tmp, "w"), indent=1, default=str)
        os.replace(tmp, os.path.join(self.logdir, f"status_{self.mode}.json"))

    def run(self, once=False):
        log(f"Dauer-Überwachung gestartet ({self.mode}), Konto {self.desk.equity():.2f} EUR")
        poll = self.R["watch"]["poll_seconds"]
        while not STOP:
            t0 = time.time()
            try:
                hour = now_utc().replace(minute=0, second=0, microsecond=0)
                if self.last_setup_hour != hour and now_utc().minute >= 1:
                    self.refresh_setups()
                    self.last_setup_hour = hour
                self.tick()
                self.save()
            except Exception as e:  # noqa: BLE001  Netzwerk o. Ä. – weiterlaufen
                log(f"[fehler] {e}")
            if once:
                break
            while not STOP and time.time() - t0 < poll:
                time.sleep(1)
        self.save()
        log("beendet, Zustand gespeichert")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--once", action="store_true", help="nur ein Durchlauf (Test)")
    a = ap.parse_args()
    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)
    Runner(a.live).run(once=a.once)

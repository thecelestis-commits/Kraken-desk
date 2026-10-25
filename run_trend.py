"""TREND-DESK: Bens Methode, automatisiert und getestet (2020–2026, siehe README).
BTC und ETH, je 50 % des Kontos. Einmal am Tag nach Tagesschluss (00:00 UTC) entschieden:
  KAUF     wenn Tagesschluss > 50-Tage-Schnitt × 1,02
  VERKAUF  wenn Tagesschluss < 50-Tage-Schnitt × 0,98
  dazwischen: nichts tun (Puffer gegen Hin und Her)
Kauf und Verkauf per Maker-Limit (0,40 %), Rest nach ein paar Versuchen per Market.
Läuft über den 4h-Timer; gehandelt wird nur, wenn Signal und Bestand nicht zusammenpassen.
  python run_trend.py [--live] [--close-all]"""
import argparse
import json
import os
from datetime import datetime, timezone

import pandas as pd
import yaml

from desk.data import asset_pairs, ohlc, ticker

HERE = os.path.dirname(os.path.abspath(__file__))


def load_rules():
    with open(os.path.join(HERE, "rules.yaml")) as f:
        return yaml.safe_load(f)


def now():
    return datetime.now(timezone.utc)


def signal(close, n, band):
    """Zustand an/aus mit Puffer über die ganze Historie (gleiche Logik wie im Backtest)."""
    sma = close.rolling(n).mean()
    on = False
    for x, m in zip(close.values, sma.values):
        if pd.isna(m):
            continue
        if not on and x > m * (1 + band):
            on = True
        elif on and x < m * (1 - band):
            on = False
    return on, float(sma.iloc[-1]), float(close.iloc[-1])


class PaperBroker:
    def __init__(self, T):
        self.fb = T["fee_buy_pct"] / 100
        self.fs = T["fee_sell_pct"] / 100
        self.slip = T["slippage_pct"] / 100

    def buy_eur(self, pair, eur, quote):
        px = quote["ask"] * (1 + self.slip)
        fee = eur * self.fb
        return {"qty": (eur - fee) / px, "price": px, "cost": eur, "fee": fee}

    def sell_qty(self, pair, qty, quote):
        px = quote["bid"] * (1 - self.slip)
        gross = qty * px
        fee = gross * self.fs
        return {"qty": qty, "price": px, "proceeds": gross - fee, "fee": fee}


class LiveBroker:
    def __init__(self, info):
        from desk.kraken_live import KrakenAPI
        self.api = KrakenAPI()
        self.info = info

    def balance(self):
        return self.api.private("Balance")

    def balance_eur(self):
        b = self.balance()
        return float(b.get("ZEUR", b.get("EUR", 0)))

    def _limit_loop(self, pair, side, volume, attempts=6, wait_s=30):
        """Post-only am besten Preis (Maker), nachziehen; Rest per Market."""
        import time
        from desk.stages import round_down
        i = self.info[pair]
        got_v = got_c = got_f = 0.0
        for _ in range(attempts):
            rest = round_down(volume - got_v, i["lot_decimals"])
            if rest < i["ordermin"]:
                break
            q = list(ticker([i["key"]]).values())[0]
            px = round_down(q["bid"] if side == "buy" else q["ask"], i["pair_decimals"])
            try:
                tx = self.api.private("AddOrder", {"pair": pair, "type": side, "ordertype": "limit",
                                                   "price": f"{px}", "volume": f"{rest}", "oflags": "post"})["txid"][0]
            except RuntimeError as e:
                print(f"[live] Limit {side} {pair}: {e}")
                continue
            t_end = time.time() + wait_s
            while time.time() < t_end:
                time.sleep(3)
                o = self.api.private("QueryOrders", {"txid": tx})[tx]
                if o["status"] in ("closed", "canceled", "expired"):
                    break
            else:
                try:
                    self.api.private("CancelOrder", {"txid": tx})
                except RuntimeError:
                    pass
                time.sleep(1.5)
            o = self.api.private("QueryOrders", {"txid": tx})[tx]
            got_v += float(o["vol_exec"]); got_c += float(o["cost"]); got_f += float(o["fee"])
        rest = round_down(volume - got_v, i["lot_decimals"])
        if rest >= i["ordermin"]:
            tx = self.api.private("AddOrder", {"pair": pair, "type": side, "ordertype": "market",
                                               "volume": f"{rest}"})["txid"][0]
            for _ in range(15):
                time.sleep(2)
                o = self.api.private("QueryOrders", {"txid": tx})[tx]
                if o["status"] == "closed":
                    break
            got_v += float(o["vol_exec"]); got_c += float(o["cost"]); got_f += float(o["fee"])
        return got_v, got_c, got_f

    def buy_eur(self, pair, eur, quote):
        from desk.stages import round_down
        vol = round_down(eur * 0.995 / quote["ask"], self.info[pair]["lot_decimals"])
        if vol < self.info[pair]["ordermin"]:
            return None
        v, c, f = self._limit_loop(pair, "buy", vol)
        if v <= 0:
            return None
        return {"qty": v, "price": c / v, "cost": c + f, "fee": f}

    def sell_qty(self, pair, qty, quote):
        v, c, f = self._limit_loop(pair, "sell", qty)
        if v <= 0:
            raise RuntimeError(f"Verkauf {pair} nicht ausgeführt – bitte in Kraken prüfen!")
        return {"qty": v, "price": c / v, "proceeds": c - f, "fee": f}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--close-all", action="store_true")
    a = ap.parse_args()
    R = load_rules()
    T = R["trend"]
    mode = "live" if a.live else "paper"
    if a.live and not (os.path.exists(os.path.join(HERE, "mode"))
                       and open(os.path.join(HERE, "mode")).read().strip() == "live"):
        raise SystemExit("Live ist nicht freigeschaltet.")
    logdir = os.path.join(HERE, "logs")
    os.makedirs(logdir, exist_ok=True)
    sf = os.path.join(HERE, f"state_trend_{mode}.json")
    st = json.load(open(sf)) if os.path.exists(sf) else None
    pairs = T["pairs"]
    info = asset_pairs(pairs)
    alt = {v["key"]: k for k, v in info.items()}
    quotes = {alt.get(k, k): v for k, v in ticker(pairs).items()}
    broker = LiveBroker(info) if a.live else PaperBroker(T)

    if st is None:
        cash = broker.balance_eur() if a.live else float(R["account"]["start_capital_eur"])
        st = {"cash": cash, "pos": {}, "fees": 0.0, "start_equity": round(cash, 2), "started": str(now())}
        print(f"[trend/{mode}] gestartet mit {cash:.2f} EUR")
    events = []

    def value():
        return st["cash"] + sum(p["qty"] * quotes[k]["bid"] for k, p in st["pos"].items())

    def do_sell(p, reason):
        pos = st["pos"][p]
        f = broker.sell_qty(p, pos["qty"], quotes[p])
        st["cash"] += f["proceeds"]
        st["fees"] += f["fee"]
        pnl = f["proceeds"] - pos["cost"]
        events.append({"t": str(now()), "type": "SELL", "pair": p, "price": f["price"],
                       "pnl_eur": round(pnl, 2), "reason": reason})
        del st["pos"][p]

    sig = {}
    for p in pairs:
        d = ohlc(p, 1440)                      # abgeschlossene Tageskerzen (~720)
        on, sma, close = signal(d["close"], T["sma_days"], T["band_pct"] / 100)
        sig[p] = {"on": on, "sma": sma, "close": close, "day": str(d.index[-1].date()),
                  "buy_at": sma * (1 + T["band_pct"] / 100), "sell_at": sma * (1 - T["band_pct"] / 100)}

    if a.close_all:
        for p in list(st["pos"]):
            do_sell(p, "Notaus (manuell)")
    else:
        # Verkäufe zuerst (machen Geld frei)
        for p in list(st["pos"]):
            if not sig[p]["on"]:
                do_sell(p, f"Trend gedreht: Schluss {sig[p]['close']:.6g} < {sig[p]['sell_at']:.6g}")
        for p in pairs:
            if sig[p]["on"] and p not in st["pos"]:
                eq = value()
                eur = min(eq * T["weight_pct"] / 100, st["cash"])
                if eur < T["min_order_eur"]:
                    continue
                f = broker.buy_eur(p, eur, quotes[p])
                if not f:
                    continue
                st["cash"] -= f["cost"]
                st["fees"] += f["fee"]
                st["pos"][p] = {"qty": f["qty"], "entry": f["price"], "cost": f["cost"], "entry_time": str(now())}
                events.append({"t": str(now()), "type": "BUY", "pair": p, "price": f["price"],
                               "eur": round(f["cost"], 2), "score": "Trend", "stop": sig[p]["sell_at"]})
        if a.live:      # Kontostand mit Kraken abgleichen
            st["cash"] = broker.balance_eur()

    st["last_run"] = str(now())
    tmp = sf + ".tmp"
    json.dump(st, open(tmp, "w"), indent=1)
    os.replace(tmp, sf)
    with open(os.path.join(logdir, f"trend_{mode}_events.jsonl"), "a") as fh:
        for e in events:
            fh.write(json.dumps(e, ensure_ascii=False) + "\n")
    eq = value()
    with open(os.path.join(logdir, f"trend_{mode}_equity.csv"), "a") as fh:
        fh.write(f"{now().isoformat()},{eq:.2f}\n")

    btc = sig.get("XBTEUR", next(iter(sig.values())))
    funnel = {}
    for p in pairs:
        s = sig[p]
        dist = (s["close"] / s["sma"] - 1) * 100
        if p in st["pos"]:
            funnel[p] = {"station": 5, "grund": f"im Depot – Trend intakt ({dist:+.1f} % zum Schnitt), raus unter {s['sell_at']:.6g}"}
        elif s["on"]:
            funnel[p] = {"station": 6, "grund": "Signal KAUF – wird gekauft"}
        else:
            funnel[p] = {"station": 2, "grund": f"kein Aufwärtstrend ({dist:+.1f} % zum Schnitt) – Kauf ab Schluss über {s['buy_at']:.6g}"}
    status = {
        "mode": mode, "engine": "trend", "updated": str(now()), "last_bar": btc["day"], "timeframe_min": 1440,
        "regime": {"green": btc["on"], "btc": btc["close"], "ema": btc["sma"]},
        "equity": round(eq, 2), "cash": round(st["cash"], 2), "start_equity": st["start_equity"],
        "peak": round(eq, 2), "fees_paid": round(st["fees"], 2), "halted": False, "halted_day": False,
        "positions": [{"pair": p, "entry": q["entry"], "now": quotes[p]["bid"], "stop": sig[p]["sell_at"],
                       "eur": round(q["cost"], 2), "qty": q["qty"], "entry_time": q["entry_time"],
                       "trailing": True, "be": False,
                       "pnl_pct": round((quotes[p]["bid"] / q["entry"] - 1) * 100, 2)} for p, q in st["pos"].items()],
        "funnel": funnel,
        "signals": {p: {k: v for k, v in s.items()} for p, s in sig.items()},
        "rules": {"risk_pct": T["weight_pct"], "max_pos": len(pairs), "min_score": 0, "max_score": 0, "dd_stop": 0},
    }
    tmp = os.path.join(logdir, f"status_trend_{mode}.json.tmp")
    json.dump(status, open(tmp, "w"), indent=1, default=str)
    os.replace(tmp, os.path.join(logdir, f"status_trend_{mode}.json"))
    print(f"[trend/{mode}] {now():%d.%m %H:%M} UTC | Konto {eq:.2f} EUR (Cash {st['cash']:.2f})")
    for p in pairs:
        s = sig[p]
        print(f"  {p}: Schluss {s['close']:.6g}  Schnitt50 {s['sma']:.6g}  -> {'TREND' if s['on'] else 'kein Trend'}"
              f"{'  [gehalten]' if p in st['pos'] else ''}")
    for e in events:
        print(f"  {e['type']} {e['pair']} @ {e['price']:.6g}" + (f"  {e['pnl_eur']:+.2f} EUR" if e["type"] == "SELL" else f"  {e['eur']} EUR"))


if __name__ == "__main__":
    main()

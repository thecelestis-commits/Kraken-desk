"""TREND-DESK: Bens Methode, automatisiert und getestet (2020–2026, siehe README).
BTC und ETH, je 50 % des Kontos. Einmal am Tag nach Tagesschluss (00:00 UTC) entschieden:
  KAUF     wenn Tagesschluss > 50-Tage-Schnitt × 1,02
  VERKAUF  wenn Tagesschluss < 50-Tage-Schnitt × 0,98
  dazwischen: nichts tun (Puffer gegen Hin und Her)
Kauf und Verkauf per Maker-Limit (0,40 %), Rest nach ein paar Versuchen per Market.
Läuft über den 4h-Timer; gehandelt wird nur, wenn Signal und Bestand nicht zusammenpassen.

LIVE: Maßgeblich ist immer der echte Kontostand bei Kraken (Balance), nicht eine eigene Buchführung.
Jeder Lauf storniert zuerst offene Orders des Bots, liest Balance, handelt, und sichert nach
JEDEM Trade sofort den Zustand. Der Bot verwaltet das ganze Kraken-Konto (EUR, BTC, ETH).
  python run_trend.py [--live] [--close-all]"""
import argparse
import json
import os
import time
from datetime import datetime, timezone

import pandas as pd
import yaml

from desk.data import asset_pairs, ohlc, ticker

HERE = os.path.dirname(os.path.abspath(__file__))
BASE_KEYS = {"XBTEUR": ("XXBT", "XBT"), "ETHEUR": ("XETH", "ETH")}
USERREF = 4711                       # markiert Orders des Trend-Desks


def load_rules():
    with open(os.path.join(HERE, "rules.yaml")) as f:
        return yaml.safe_load(f)


def now():
    return datetime.now(timezone.utc)


def fmt(x, dec):
    """Zahl als Dezimalstring mit fester Stellenzahl, abgerundet (nie e-Schreibweise)."""
    q = 10 ** dec
    return f"{int(x * q + 1e-9) / q:.{dec}f}"


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


# ---------------------------------------------------------------- Paper
class PaperBroker:
    live = False

    def __init__(self, T, st):
        self.fb = T["fee_buy_pct"] / 100
        self.fs = T["fee_sell_pct"] / 100
        self.slip = T["slippage_pct"] / 100
        self.st = st

    def prepare(self):
        pass

    def holdings(self, quotes):
        return self.st["cash"], {p: q["qty"] for p, q in self.st["pos"].items()}

    def buy(self, pair, eur, quote, panic=False):
        px = quote["ask"] * (1 + self.slip)
        fee = eur * self.fb
        return {"qty": (eur - fee) / px, "price": px, "cost": eur, "fee": fee}

    def sell(self, pair, qty, quote, panic=False):
        px = quote["bid"] * (1 - self.slip)
        gross = qty * px
        fee = gross * (0.008 if panic else self.fs)
        return {"qty": qty, "price": px, "proceeds": gross - fee, "fee": fee}


# ---------------------------------------------------------------- Live
class LiveBroker:
    live = True

    def __init__(self, info):
        from desk.kraken_live import KrakenAPI
        self.api = KrakenAPI()
        self.info = info

    def call(self, method, data=None, tries=3):
        """Private Call mit Wiederholung bei Netzfehlern (nicht bei AddOrder – siehe add())."""
        last = None
        for k in range(tries):
            try:
                return self.api.private(method, data)
            except RuntimeError:
                raise
            except Exception as e:  # noqa: BLE001  Timeout, URLError …
                last = e
                time.sleep(2 + 3 * k)
        raise RuntimeError(f"Kraken {method} nicht erreichbar: {last}")

    def balance(self):
        return self.call("Balance")

    def base_qty(self, pair, bal=None):
        bal = bal or self.balance()
        return sum(float(bal.get(k, 0)) for k in BASE_KEYS[pair])

    def eur(self, bal=None):
        bal = bal or self.balance()
        return float(bal.get("ZEUR", bal.get("EUR", 0)))

    def cancel_own(self, pair=None):
        """Alle offenen Orders des Trend-Desks stornieren (optional nur für ein Paar)."""
        oo = self.call("OpenOrders", {"userref": USERREF}).get("open", {})
        for tx, o in oo.items():
            if pair and o.get("descr", {}).get("pair") not in (pair, self.info[pair]["key"]):
                continue
            try:
                self.call("CancelOrder", {"txid": tx})
            except RuntimeError as e:
                print(f"[live] Storno {tx}: {e}")
        if oo:
            time.sleep(2)

    def prepare(self):
        self.cancel_own()

    def holdings(self, quotes):
        bal = self.balance()
        return self.eur(bal), {p: self.base_qty(p, bal) for p in BASE_KEYS}

    def add(self, data):
        """AddOrder ohne blinde Wiederholung: bei Netzfehler ist unklar, ob die Order steht –
        darum anschließend nur über den Kontostand weiterrechnen."""
        data = dict(data, userref=USERREF)
        try:
            return self.api.private("AddOrder", data)["txid"][0]
        except RuntimeError as e:
            print(f"[live] AddOrder {data.get('type')} {data.get('pair')} {data.get('ordertype')}: {e}")
        except Exception as e:  # noqa: BLE001
            print(f"[live] AddOrder Netzfehler: {e} – prüfe über Kontostand")
        return None

    def _wait(self, tx, secs):
        t_end = time.time() + secs
        while tx and time.time() < t_end:
            time.sleep(3)
            try:
                o = self.call("QueryOrders", {"txid": tx})[tx]
            except RuntimeError:
                continue
            if o["status"] in ("closed", "canceled", "expired"):
                return

    def _trade(self, pair, side, target_base, panic=False, attempts=6, wait_s=30):
        """Bewegt den Bestand um target_base (Coin-Menge). Fortschritt wird über die
        Balance gemessen – funktioniert auch bei Teilfüllung, Ablehnung und Netzfehlern."""
        i = self.info[pair]
        bal0 = self.balance()
        b0, e0 = self.base_qty(pair, bal0), self.eur(bal0)
        done = lambda b: (b - b0) if side == "buy" else (b0 - b)   # noqa: E731
        if not panic:
            for _ in range(attempts):
                rest = target_base - done(self.base_qty(pair))
                if rest < i["ordermin"]:
                    break
                q = list(ticker([i["key"]]).values())[0]
                step = 10 ** -i["pair_decimals"]
                if side == "buy":
                    px = fmt(q["bid"], i["pair_decimals"])
                else:
                    px = f"{(int(q['ask'] / step + 0.999999) * step):.{i['pair_decimals']}f}"
                tx = self.add({"pair": pair, "type": side, "ordertype": "limit", "price": px,
                               "volume": fmt(rest, i["lot_decimals"]), "oflags": "post,fciq"})
                if tx:
                    self._wait(tx, wait_s)
                self.cancel_own(pair)
                time.sleep(2)
        # Rest per Market (Taker)
        bal = self.balance()
        rest = target_base - done(self.base_qty(pair, bal))
        if side == "buy":   # nie mehr kaufen, als Euro (inkl. 0,8 % Gebühr) da sind
            q = list(ticker([i["key"]]).values())[0]
            rest = min(rest, self.eur(bal) * 0.985 / q["ask"])
        else:
            rest = min(rest, self.base_qty(pair, bal))
        if rest >= i["ordermin"]:
            tx = self.add({"pair": pair, "type": side, "ordertype": "market",
                           "volume": fmt(rest, i["lot_decimals"]), "oflags": "fciq"})
            self._wait(tx, 30)
        bal1 = self.balance()
        b1, e1 = self.base_qty(pair, bal1), self.eur(bal1)
        moved = done(b1)
        if moved <= 0:
            return None
        eur_delta = abs(e1 - e0)
        return {"qty": moved, "price": eur_delta / moved, "eur_delta": eur_delta}

    def buy(self, pair, eur, quote, panic=False):
        f = self._trade(pair, "buy", eur * 0.985 / quote["ask"], panic)
        if not f:
            return None
        return {"qty": f["qty"], "price": f["price"], "cost": f["eur_delta"], "fee": f["eur_delta"] * 0.004}

    def sell(self, pair, qty, quote, panic=False):
        f = self._trade(pair, "sell", qty, panic)
        if not f:
            return None
        return {"qty": f["qty"], "price": f["price"], "proceeds": f["eur_delta"],
                "fee": f["eur_delta"] * (0.008 if panic else 0.004)}


# ---------------------------------------------------------------- Ablauf
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
    if st is None:
        st = {"cash": float(R["account"]["start_capital_eur"]), "pos": {}, "fees": 0.0, "started": str(now())}
    broker = LiveBroker(info) if a.live else PaperBroker(T, st)
    min_eur = T["min_order_eur"]

    def save():
        tmp = sf + ".tmp"
        json.dump(st, open(tmp, "w"), indent=1)
        os.replace(tmp, sf)

    def log_event(e):
        with open(os.path.join(logdir, f"trend_{mode}_events.jsonl"), "a") as fh:
            fh.write(json.dumps(e, ensure_ascii=False) + "\n")
        print(f"  {e['type']} {e['pair']} @ {e['price']:.6g}"
              + (f"  {e['pnl_eur']:+.2f} EUR ({e['reason']})" if e["type"] == "SELL" else f"  {e['eur']} EUR"))

    # 1) Aufräumen + echter Bestand
    broker.prepare()
    cash, held = broker.holdings(quotes)
    if a.live:
        st["cash"] = cash
        for p in pairs:                       # Bestand aus Kraken übernehmen
            val = held[p] * quotes[p]["bid"]
            if val >= min_eur and p not in st["pos"]:
                st["pos"][p] = {"qty": held[p], "entry": quotes[p]["bid"], "cost": val,
                                "entry_time": str(now()), "taken_over": True}
            elif val < min_eur:
                st["pos"].pop(p, None)
            if p in st["pos"]:
                st["pos"][p]["qty"] = held[p]
        if "start_equity" not in st:
            st["start_equity"] = round(cash + sum(held[p] * quotes[p]["bid"] for p in pairs), 2)
        save()
    st.setdefault("start_equity", round(st["cash"], 2))

    def value():
        return st["cash"] + sum(q["qty"] * quotes[p]["bid"] for p, q in st["pos"].items())

    def do_sell(p, reason, panic=False):
        pos = st["pos"][p]
        f = broker.sell(p, pos["qty"], quotes[p], panic=panic)
        if not f:
            print(f"  [!] Verkauf {p} nicht ausgeführt – nächster Lauf versucht es erneut")
            return
        st["cash"] += f["proceeds"]
        st["fees"] += f["fee"]
        share = min(1.0, f["qty"] / pos["qty"]) if pos["qty"] else 1.0
        pnl = f["proceeds"] - pos["cost"] * share
        pos["qty"] -= f["qty"]
        pos["cost"] *= (1 - share)
        if pos["qty"] * quotes[p]["bid"] < min_eur:
            del st["pos"][p]
        save()
        log_event({"t": str(now()), "type": "SELL", "pair": p, "price": f["price"],
                   "pnl_eur": round(pnl, 2), "reason": reason})

    def do_buy(p, eur):
        f = broker.buy(p, eur, quotes[p])
        if not f:
            print(f"  [!] Kauf {p} nicht ausgeführt – nächster Lauf versucht es erneut")
            return
        st["cash"] -= f["cost"]
        st["fees"] += f["fee"]
        st["pos"][p] = {"qty": f["qty"], "entry": f["price"], "cost": f["cost"], "entry_time": str(now())}
        save()
        log_event({"t": str(now()), "type": "BUY", "pair": p, "price": f["price"],
                   "eur": round(f["cost"], 2), "score": "Trend"})

    sig = {}
    for p in pairs:
        d = ohlc(p, 1440)                      # abgeschlossene Tageskerzen (~720)
        on, sma, close = signal(d["close"], T["sma_days"], T["band_pct"] / 100)
        sig[p] = {"on": on, "sma": sma, "close": close, "day": str(d.index[-1].date()),
                  "buy_at": sma * (1 + T["band_pct"] / 100), "sell_at": sma * (1 - T["band_pct"] / 100)}

    # 2) Handeln – jeder Schritt einzeln abgesichert
    errors = []
    try:
        if a.close_all:
            for p in list(st["pos"]):
                do_sell(p, "Notaus (manuell)", panic=True)
        else:
            for p in list(st["pos"]):
                if not sig[p]["on"]:
                    do_sell(p, f"Trend gedreht: Schluss {sig[p]['close']:.6g} < {sig[p]['sell_at']:.6g}")
            for p in pairs:
                if sig[p]["on"] and p not in st["pos"]:
                    eur = min(value() * T["weight_pct"] / 100, st["cash"])
                    if eur >= min_eur:
                        do_buy(p, eur)
    except Exception as e:  # noqa: BLE001
        errors.append(str(e))
        print(f"[FEHLER] {e}")
    finally:
        if a.live:                            # Abschluss: Kontostand von Kraken übernehmen
            try:
                cash, held = broker.holdings(quotes)
                st["cash"] = cash
                for p in list(st["pos"]):
                    st["pos"][p]["qty"] = held[p]
                    if held[p] * quotes[p]["bid"] < min_eur:
                        del st["pos"][p]
            except Exception as e:  # noqa: BLE001
                errors.append(f"Abgleich: {e}")
        st["last_run"] = str(now())
        st["last_error"] = "; ".join(errors) or None
        save()

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
        else:
            funnel[p] = {"station": 2, "grund": f"kein Aufwärtstrend ({dist:+.1f} % zum Schnitt) – Kauf ab Schluss über {s['buy_at']:.6g}"}
    status = {
        "mode": mode, "engine": "trend", "updated": str(now()), "last_bar": btc["day"], "timeframe_min": 1440,
        "regime": {"green": btc["on"], "btc": btc["close"], "ema": btc["sma"]},
        "equity": round(eq, 2), "cash": round(st["cash"], 2), "start_equity": st["start_equity"],
        "peak": round(eq, 2), "fees_paid": round(st["fees"], 2), "halted": bool(errors), "halted_day": False,
        "error": "; ".join(errors) or None,
        "positions": [{"pair": p, "entry": q["entry"], "now": quotes[p]["bid"], "stop": sig[p]["sell_at"],
                       "eur": round(q["cost"], 2), "qty": q["qty"], "entry_time": q["entry_time"],
                       "trailing": True, "be": False,
                       "pnl_pct": round((quotes[p]["bid"] / q["entry"] - 1) * 100, 2)} for p, q in st["pos"].items()],
        "funnel": funnel,
        "signals": sig,
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
    if errors:
        raise SystemExit(1)


if __name__ == "__main__":
    main()

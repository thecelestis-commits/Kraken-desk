"""Dauer-Überwachung: dieselben 6 Stationen, aber ohne festen Takt.
Setups (Trend, ATR, Ausbruchsniveau) kommen aus abgeschlossenen 1h-Kerzen,
der Kauf passiert im Moment, in dem der Kurs das Niveau durchbricht.
Wird identisch für Live/Paper (Minutentakt) und Backtest (1h-Kerzen als Ersatz) benutzt."""
import copy
from dataclasses import asdict

import pandas as pd

from .stages import RiskState, risk_update_day, risk_officer, gate, Position, Auditor
from .engine import SimBroker
from .indicators import enrich


def watch_rules(R):
    """Leitet aus rules.yaml die Parameter für 1h-Setups ab (eine Quelle, keine Kopie)."""
    W = R["watch"]
    X = copy.deepcopy(R)
    X["timeframe_minutes"] = W["setup_timeframe"]
    X["cartographer"].update(breakout_lookback=W["breakout_lookback"], volume_lookback=24, roc_period=24,
                             htf_minutes=W["htf_minutes"], htf_ema=W["htf_ema"], volume_mult=W["volume_mult"],
                             required=W["required"])
    X["screener"].update(atr_pct_min=0.6, atr_pct_max=6.0)
    X["exits"].update(stop_atr=W["stop_atr"], breakeven_at_r=W["breakeven_at_r"],
                      trail_activate_r=W["trail_activate_r"], trail_atr=W["trail_atr"])
    X["gate"]["min_score"] = W["min_score"]
    return X


def regime_map(btc_1h, X):
    """BTC-Marktampel aus abgeschlossenen 4h-Kerzen, auf 1h-Zeitachse gelegt."""
    m = X["watch"]["htf_minutes"]
    hc = btc_1h["close"].resample(f"{m}min", label="left", closed="left").last().dropna()
    ok = hc > hc.ewm(span=X["cartographer"]["regime_ema"], adjust=False).mean()
    ok.index = ok.index + pd.Timedelta(minutes=m - 60)
    return ok.reindex(btc_1h.index, method="ffill").fillna(False).astype(bool)


class WatchDesk:
    def __init__(self, R, broker=None, auditor=None):
        self.R = R
        self.X = watch_rules(R)
        self.W = R["watch"]
        self.broker = broker or SimBroker(R)
        self.aud = auditor or Auditor(self.X)
        cap = R["account"]["start_capital_eur"]
        self.cash = cap
        self.positions = {}
        self.rs = RiskState(equity=cap, peak=cap)
        self.cooldown = {}          # pair -> ISO-Zeit bis wann gesperrt
        self.setups = {}
        self.funnel = {}
        self.prices = {}
        self.exch_stop = {}
        self.fees_paid = 0.0
        self.regime_green = False
        self.equity_curve = []

    # ---------------------------------------------------------------
    def equity(self):
        return self.cash + sum(p.qty * self.prices.get(p.pair, p.entry) for p in self.positions.values())

    def exposure(self):
        return sum(p.qty * self.prices.get(p.pair, p.entry) for p in self.positions.values())

    # ---------- STATION 1+2: Setup aus der letzten abgeschlossenen Kerze ----------
    def update_setup(self, pair, row, n_bars, regime_green):
        X, s = self.X, self.X["screener"]
        self.regime_green = regime_green
        why = None
        if n_bars < s["min_bars"] or row[["ema_slow", "atr", "hh", "vol_avg", "roc"]].isna().any():
            why = "zu wenig Historie"
        elif not row["vol_eur_24h"] >= s["min_volume_eur_24h"]:
            why = "zu wenig Umsatz"
        elif row["atr_pct"] < s["atr_pct_min"]:
            why = "zu ruhig"
        elif row["atr_pct"] > s["atr_pct_max"]:
            why = "zu wild"
        c = X["cartographer"]
        hh = max(float(row["hh"]), float(row["high"])) if not pd.isna(row["hh"]) else float("nan")
        static = {
            "trend": bool(row["ema_fast"] > row["ema_slow"]),
            "above_fast": bool(row["close"] > row["ema_fast"]),
            "rsi_ok": bool(row["rsi"] < c["rsi_max"]),
            "momentum": bool(row["roc"] > 0),
            "htf_trend": bool(row.get("htf_up", False)),
        }
        self.setups[pair] = {"screen_ok": why is None, "why": why, "static": static, "hh": hh,
                             "vol_avg": float(row["vol_avg"]) if not pd.isna(row["vol_avg"]) else 0.0,
                             "atr": float(row["atr"]), "close": float(row["close"])}
        if pair not in self.positions:
            self._funnel_idle(pair)

    def _funnel_idle(self, pair):
        st = self.setups.get(pair)
        if not st:
            return
        if not st["screen_ok"]:
            self.funnel[pair] = {"station": 1, "grund": st["why"]}
            return
        miss = [k for k in ("trend", "htf_trend") if not st["static"][k]]
        dist = (st["hh"] / self.prices.get(pair, st["close"]) - 1) * 100
        if miss:
            self.funnel[pair] = {"station": 2, "grund": "fehlt: " + ", ".join(miss), "signale": self._sig(st, False, False)}
        elif not self.regime_green:
            self.funnel[pair] = {"station": 2, "grund": "Setup da – Marktampel rot", "signale": self._sig(st, False, False)}
        else:
            self.funnel[pair] = {"station": 2, "grund": f"lauert – Ausbruch bei +{dist:.1f}%",
                                 "signale": self._sig(st, False, False), "lauert": True}

    @staticmethod
    def _sig(st, breakout, vol):
        s = dict(st["static"])
        s.update(breakout=breakout, volume_spike=vol)
        return {k: s[k] for k in ("trend", "above_fast", "breakout", "volume_spike", "rsi_ok", "momentum", "htf_trend")}

    # ---------- Jeder Tick: STATION 5 (Exits) dann STATION 2-4 (Einstieg) ----------
    def on_tick(self, pair, now, last, low=None, high=None, entry_ref=None, vol_proj=None,
                minutes_in=60, spread=None, pair_info=None):
        low = last if low is None else low
        high = last if high is None else high
        self.prices[pair] = last
        if pair in self.positions:
            self._manage(pair, now, last, low, high)
            return
        self._try_entry(pair, now, last, high, entry_ref, vol_proj, minutes_in, spread, pair_info)

    def _manage(self, pair, now, last, low, high):
        pos, W = self.positions[pair], self.W
        live_fill = self.broker.stop_filled(pos)
        px, why = (live_fill, "Stop (Börse)") if live_fill is not None else (None, None)
        if px is None and low <= pos.stop:
            px, why = min(last if low == last else pos.stop, pos.stop), (
                "Trailing-Stop" if pos.trailing else "Einstand-Stop" if pos.be_done else "Stop-Loss")
        if px is None:
            pos.highest = max(pos.highest, high)
            gain_r = (pos.highest - pos.entry) / pos.risk_unit
            if not pos.be_done and gain_r >= W["breakeven_at_r"]:
                pos.stop, pos.be_done = max(pos.stop, pos.entry), True
            atr = self.setups.get(pair, {}).get("atr", pos.risk_unit / W["stop_atr"])
            if gain_r >= W["trail_activate_r"]:
                pos.trailing = True
                pos.stop = max(pos.stop, pos.highest - W["trail_atr"] * atr)
            held_h = (pd.Timestamp(now) - pd.Timestamp(pos.entry_time)).total_seconds() / 3600
            if held_h >= W["time_stop_hours"] and not pos.be_done:
                px, why = last, "Zeit-Stop"
            else:
                ref = self.exch_stop.get(pair, 0)
                if pos.stop > ref * (1 + W["exchange_stop_step_pct"] / 100):
                    self.broker.on_stop_moved(pos)
                    self.exch_stop[pair] = pos.stop
                self.funnel[pair] = {"station": 5, "grund": f"im Depot – Stop {pos.stop:.6g}"
                                     + (" (Trailing)" if pos.trailing else " (Einstand)" if pos.be_done else "")}
        if px is not None:
            s = self.broker.sell(pos, px)
            self.fees_paid += s["fee"]
            net = s["proceeds"] - s["fee"]
            self.cash += net
            pnl = net - pos.cost_eur * (1 + self.broker.fee)
            held = int((pd.Timestamp(now) - pd.Timestamp(pos.entry_time)).total_seconds() // 3600)
            self.aud.exit(now, pos, s["price"], why, pnl, held)
            if pnl < 0:
                self.cooldown[pair] = str(pd.Timestamp(now) + pd.Timedelta(hours=W["cooldown_hours"]))
            del self.positions[pair]
            self.exch_stop.pop(pair, None)
            self._funnel_idle(pair)

    def _try_entry(self, pair, now, last, high, entry_ref, vol_proj, minutes_in, spread, pair_info):
        st, W = self.setups.get(pair), self.W
        if not st or not st["screen_ok"] or pd.isna(st["hh"]):
            return
        if not self.regime_green:
            return
        cd = self.cooldown.get(pair)
        if cd and pd.Timestamp(now) < pd.Timestamp(cd):
            self.funnel[pair] = {"station": 1, "grund": "Cooldown nach Verlust"}
            return
        breakout = high > st["hh"]
        if not breakout:
            self._funnel_idle(pair)
            return
        if minutes_in < W["min_minutes_into_bar"] or vol_proj is None:
            vol = False
        else:
            vol = vol_proj >= W["volume_mult"] * st["vol_avg"]
        sig = self._sig(st, True, vol)
        carto = {"signals": sig, "score": sum(sig.values()),
                 "missing": [k for k in W["required"] if not sig[k]]}
        if carto["missing"] or carto["score"] < W["min_score"]:
            self.aud.reject("Kartograf", "Ausbruch ohne Bestätigung")
            self.funnel[pair] = {"station": 2, "grund": f"Ausbruch, aber Score {carto['score']}/7"
                                 + ("" if vol else " (Volumen fehlt)"), "signale": sig}
            return
        ref = entry_ref if entry_ref is not None else last
        self.rs.equity = self.equity()
        if len(self.positions) >= self.X["risk"]["max_open_positions"]:
            self.funnel[pair] = {"station": 3, "grund": "keine freien Slots", "signale": sig}
            return
        plan, why = risk_officer(ref, st["atr"], self.rs, self.positions, self.exposure(), self.X)
        ok, why2 = gate(carto, plan, self.X, self.cash * 0.995, pair_info, spread)
        if not ok:
            self.aud.reject("Gate", why if plan is None else why2)
            self.funnel[pair] = {"station": 3 if plan is None else 4, "grund": why if plan is None else why2, "signale": sig}
            return
        fill = self.broker.buy(pair, plan["qty"], ref)
        if fill is None:
            self.funnel[pair] = {"station": 4, "grund": "Order nicht ausgeführt", "signale": sig}
            return
        stop = fill["price"] - W["stop_atr"] * st["atr"]
        pos = Position(pair=pair, entry=fill["price"], qty=fill["qty"], stop=stop, risk_unit=fill["price"] - stop,
                       entry_bar=0, entry_time=str(pd.Timestamp(now)), cost_eur=fill["cost"], highest=fill["price"])
        self.cash -= fill["cost"] + fill["fee"]
        self.fees_paid += fill["fee"]
        self.positions[pair] = pos
        self.broker.on_stop_moved(pos)
        self.exch_stop[pair] = pos.stop
        self.aud.entry(now, pos, self.rs.equity, carto)
        self.funnel[pair] = {"station": 5, "grund": f"GEKAUFT – Stop {pos.stop:.6g}", "signale": sig}

    def end_of_tick(self, now):
        self.rs.equity = self.equity()
        risk_update_day(self.rs, pd.Timestamp(now).date(), self.X)

    # ---------------------------------------------------------------
    def state(self):
        return {"cash": self.cash, "positions": {k: asdict(v) for k, v in self.positions.items()},
                "rs": {**asdict(self.rs), "day": str(self.rs.day) if self.rs.day else None},
                "cooldown": self.cooldown, "exch_stop": self.exch_stop, "fees_paid": self.fees_paid,
                "prices": self.prices}

    def load_state(self, s):
        self.cash = s["cash"]
        self.positions = {k: Position(**v) for k, v in s["positions"].items()}
        rs = s["rs"]
        self.rs = RiskState(equity=rs["equity"], peak=rs["peak"],
                            day=pd.Timestamp(rs["day"]).date() if rs["day"] else None,
                            day_start_equity=rs["day_start_equity"], halted=rs["halted"], halted_day=rs["halted_day"])
        self.cooldown = s.get("cooldown", {})
        self.exch_stop = s.get("exch_stop", {})
        self.fees_paid = s.get("fees_paid", 0.0)
        self.prices = s.get("prices", {})


# ---------------- Backtest-Ersatz auf 1h-Kerzen ----------------------------
def simulate(R, data, info=None):
    """Ersatz-Test: Einstieg zum Ausbruchsniveau innerhalb der Stunde (wie live),
    Stops mit Stundentief/-hoch. Volumen der laufenden Stunde ist im Test bekannt
    (live wird hochgerechnet) – leichte Bevorzugung des Tests, bitte beachten."""
    desk = WatchDesk(R)
    X = desk.X
    enr = {p: enrich(df, X) for p, df in data.items() if p != "__regime__" and len(df)}
    reg = regime_map(data["__regime__"], X)
    times = sorted(set().union(*[d.index for d in enr.values()]))
    pos_idx = {p: {t: k for k, t in enumerate(d.index)} for p, d in enr.items()}
    for t in times:
        green = bool(reg.get(t, False))
        for p, df in enr.items():
            k = pos_idx[p].get(t)
            if k is None or k == 0:
                continue
            prev = df.iloc[k - 1]
            desk.update_setup(p, prev, k, green)
            bar = df.iloc[k]
            st = desk.setups[p]
            ref = max(float(bar["open"]), st["hh"]) if (not pd.isna(st["hh"]) and bar["high"] > st["hh"]) else None
            was_open = p in desk.positions
            desk.on_tick(p, t, float(bar["close"]), float(bar["low"]), float(bar["high"]), entry_ref=ref,
                         vol_proj=float(bar["volume"]), minutes_in=60, pair_info=(info or {}).get(p))
            if not was_open and p in desk.positions and bar["low"] <= desk.positions[p].stop:
                desk.on_tick(p, t, float(bar["close"]), float(bar["low"]), float(bar["low"]))  # Worst Case: Stop gleiche Stunde
            desk.prices[p] = float(bar["close"])
        desk.end_of_tick(t)
        desk.equity_curve.append((t, desk.equity()))
    return desk

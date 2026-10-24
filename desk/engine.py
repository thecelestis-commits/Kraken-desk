"""Der Desk: schiebt jede abgeschlossene Kerze über das Fließband.
Wird identisch für Backtest, Paper und Live benutzt – nur der Broker wechselt."""
from dataclasses import asdict

import pandas as pd

from .stages import (screener, cartographer, RiskState, risk_update_day, risk_officer,
                     gate, Position, exit_check_bar, Auditor)


class SimBroker:
    """Simulierte Ausführung mit Gebühr + Schlupf (Backtest & Paper)."""

    def __init__(self, R):
        self.fee = R["costs"]["fee_pct"] / 100
        self.slip = R["costs"]["slippage_pct"] / 100

    def buy(self, pair, qty, ref_price):
        px = ref_price * (1 + self.slip)
        cost = qty * px
        return {"price": px, "qty": qty, "cost": cost, "fee": cost * self.fee}

    def sell(self, pos, ref_price):
        px = ref_price * (1 - self.slip)
        gross = pos.qty * px
        return {"price": px, "proceeds": gross, "fee": gross * self.fee}

    # Hooks für Live – im Sim nichts zu tun
    def on_stop_moved(self, pos):
        pass

    def stop_filled(self, pos):
        return None


class Desk:
    def __init__(self, R, broker=None, auditor=None):
        self.R = R
        self.broker = broker or SimBroker(R)
        self.aud = auditor or Auditor(R)
        cap = R["account"]["start_capital_eur"]
        self.cash = cap
        self.positions = {}
        self.pending = []          # Signale von Kerze t -> Kauf zur Eröffnung t+1
        self.rs = RiskState(equity=cap, peak=cap)
        self.cooldown = {}
        self.bar_idx = 0
        self.last_close = {}
        self.equity_curve = []
        self.fees_paid = 0.0
        self.funnel = {}           # Fließband-Schnappschuss der letzten Kerze: pair -> Station/Grund

    # ---------------------------------------------------------------
    def equity(self):
        return self.cash + sum(p.qty * self.last_close.get(p.pair, p.entry) for p in self.positions.values())

    def exposure(self):
        return sum(p.qty * self.last_close.get(p.pair, p.entry) for p in self.positions.values())

    # ---------------------------------------------------------------
    def process_bar(self, t, rows, n_bars, pair_info=None, spreads=None, allow_new=True):
        """rows: {pair: Kerze t (inkl. Indikatoren)}; n_bars: {pair: Anzahl Kerzen bis t}."""
        R = self.R
        i = self.bar_idx

        # A) offene Signale zur Eröffnung dieser Kerze ausführen
        self.fill_pending(t, rows, pair_info)

        # B) EXIT-VERWALTER auf dieser Kerze
        self._exits(t, rows, i)

        # C) Bewertung + Risikozustand
        for pair, bar in rows.items():
            self.last_close[pair] = float(bar["close"])
        self.rs.equity = self.equity()
        risk_update_day(self.rs, pd.Timestamp(t).date(), R)
        self.equity_curve.append((t, self.rs.equity))

        # D) Neue Signale
        self.funnel = {p: {"station": 0, "grund": "kein aktueller Kurs"} for p in R["universe"]}
        for p in self.positions:
            self.funnel[p] = {"station": 5, "grund": "im Depot – Exit-Verwalter wacht"}
        if allow_new:
            self._signals(rows, n_bars, pair_info, spreads, i)
        else:
            self._signals(rows, n_bars, pair_info, spreads, i, dry=True)
        self.bar_idx += 1

    # ---------------------------------------------------------------
    def fill_pending(self, t, rows, pair_info=None):
        """rows[pair]['open'] = Ausführungs-Referenzpreis (Backtest: Eröffnung, live: aktueller Ask)."""
        R = self.R
        i = self.bar_idx
        for pair, carto, atr_val in self.pending:
            if pair not in rows or pair in self.positions:
                continue
            bar = rows[pair]
            self.rs.equity = self.equity()
            plan, why = risk_officer(bar["open"], atr_val, self.rs, self.positions, self.exposure(), R)
            ok, why2 = gate(carto, plan, R, self.cash * 0.995, pair_info.get(pair) if pair_info else None)
            if not ok:
                self.aud.reject("Gate@Fill", why if plan is None else why2)
                continue
            fill = self.broker.buy(pair, plan["qty"], bar["open"])
            if fill is None:
                self.aud.reject("Broker", "Order abgelehnt")
                continue
            stop = fill["price"] - R["exits"]["stop_atr"] * atr_val
            pos = Position(pair=pair, entry=fill["price"], qty=fill["qty"], stop=stop,
                           risk_unit=fill["price"] - stop, entry_bar=i, entry_time=t,
                           cost_eur=fill["cost"], highest=fill["price"])
            self.cash -= fill["cost"] + fill["fee"]
            self.fees_paid += fill["fee"]
            self.positions[pair] = pos
            self.broker.on_stop_moved(pos)
            self.aud.entry(t, pos, self.rs.equity, carto)
        self.pending = []


    def _exits(self, t, rows, i):
        R = self.R
        for pair in list(self.positions):
            if pair not in rows:
                continue
            pos = self.positions[pair]
            bar = rows[pair]
            live_fill = self.broker.stop_filled(pos)
            if live_fill is not None:
                px, reason = live_fill, "Stop (Börse)"
            else:
                old_stop = pos.stop
                px, reason = exit_check_bar(pos, bar, i, R)
                if px is None and pos.stop != old_stop:
                    self.broker.on_stop_moved(pos)
            if px is not None:
                self.close_position(t, pair, px, reason)

    def close_position(self, t, pair, px, reason):
        pos = self.positions[pair]
        i = self.bar_idx
        s = self.broker.sell(pos, px)
        self.fees_paid += s["fee"]
        net = s["proceeds"] - s["fee"]
        self.cash += net
        pnl = net - pos.cost_eur - pos.cost_eur * self.broker.fee
        self.aud.exit(t, pos, s["price"], reason, pnl, i - pos.entry_bar)
        if pnl < 0:
            self.cooldown[pair] = i + self.R["risk"]["cooldown_bars_after_loss"]
        del self.positions[pair]

    def _signals(self, rows, n_bars, pair_info, spreads, i, dry=False):
        """SCREENER -> KARTOGRAF -> RISIKO -> GATE"""
        R = self.R
        cands = []
        for pair, bar in rows.items():
            if pair in self.positions:
                continue
            ok, why = screener(pair, bar, n_bars[pair], R, self.cooldown, i)
            if not ok:
                self.aud.reject("Screener", why)
                self.funnel[pair] = {"station": 1, "grund": why}
                continue
            carto = cartographer(bar, R)
            if carto["missing"] or carto["score"] < R["gate"]["min_score"]:
                self.aud.reject("Kartograf", "kein Setup")
                self.funnel[pair] = {"station": 2, "grund": f"Score {carto['score']}/6" + (
                    f", fehlt: {', '.join(carto['missing'])}" if carto["missing"] else ""), "signale": carto["signals"]}
                continue
            if dry:
                self.funnel[pair] = {"station": 2, "grund": f"Setup {carto['score']}/6 – aber Marktampel rot",
                                     "signale": carto["signals"]}
                continue
            cands.append((carto["score"], bar["roc"], pair, carto, bar))
        cands.sort(key=lambda x: (x[0], x[1]), reverse=True)
        slots = R["risk"]["max_open_positions"] - len(self.positions)
        for score, _, pair, carto, bar in cands:
            if slots <= 0:
                self.aud.reject("Risiko", "keine freien Slots")
                self.funnel[pair] = {"station": 3, "grund": "keine freien Slots", "signale": carto["signals"]}
                continue
            plan, why = risk_officer(bar["close"], bar["atr"], self.rs, self.positions, self.exposure(), R)
            sp = spreads.get(pair) if spreads else None
            ok, why2 = gate(carto, plan, R, self.cash, pair_info.get(pair) if pair_info else None, sp)
            if not ok:
                self.aud.reject("Gate", why if plan is None else why2)
                self.funnel[pair] = {"station": 3 if plan is None else 4, "grund": why if plan is None else why2,
                                     "signale": carto["signals"]}
                continue
            self.funnel[pair] = {"station": 6, "grund": f"DURCH – Kauf ca. {plan['eur']:.2f} EUR", "signale": carto["signals"]}
            self.pending.append((pair, carto, float(bar["atr"])))
            slots -= 1


    # ---------------------------------------------------------------
    def state(self):
        return {
            "cash": self.cash,
            "positions": {k: {**asdict(v), "entry_time": str(v.entry_time)} for k, v in self.positions.items()},
            "pending": [(p, c, a) for p, c, a in self.pending],
            "rs": {**asdict(self.rs), "day": str(self.rs.day) if self.rs.day else None},
            "cooldown": self.cooldown,
            "bar_idx": self.bar_idx,
            "last_close": self.last_close,
            "fees_paid": self.fees_paid,
            "funnel": self.funnel,
        }

    def load_state(self, s):
        self.cash = s["cash"]
        self.positions = {k: Position(**v) for k, v in s["positions"].items()}
        self.pending = [tuple(x) for x in s["pending"]]
        rs = s["rs"]
        self.rs = RiskState(equity=rs["equity"], peak=rs["peak"],
                            day=pd.Timestamp(rs["day"]).date() if rs["day"] else None,
                            day_start_equity=rs["day_start_equity"], halted=rs["halted"],
                            halted_day=rs["halted_day"])
        self.cooldown = s["cooldown"]
        self.bar_idx = s["bar_idx"]
        self.last_close = s["last_close"]
        self.fees_paid = s.get("fees_paid", 0.0)
        self.funnel = s.get("funnel", {})

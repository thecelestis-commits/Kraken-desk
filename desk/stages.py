"""Die sechs Stufen des Fließbands. Jede Stufe liest nur rules.yaml (R)."""
from dataclasses import dataclass, field
import math


# ---------- 1. SCREENER --------------------------------------------------
def screener(pair, row, n_bars, R, cooldown_until, bar_idx):
    s = R["screener"]
    if n_bars < s["min_bars"]:
        return False, "zu wenig Historie"
    if cooldown_until.get(pair, -1) > bar_idx:
        return False, "Cooldown nach Verlust"
    if not (row["vol_eur_24h"] >= s["min_volume_eur_24h"]):
        return False, "zu wenig Umsatz"
    if row["atr_pct"] < s["atr_pct_min"]:
        return False, "zu ruhig"
    if row["atr_pct"] > s["atr_pct_max"]:
        return False, "zu wild"
    return True, "ok"


# ---------- 2. KARTOGRAF -------------------------------------------------
def cartographer(row, R):
    c = R["cartographer"]
    sig = {
        "trend": row["ema_fast"] > row["ema_slow"],
        "above_fast": row["close"] > row["ema_fast"],
        "breakout": row["close"] > row["hh"],
        "volume_spike": row["volume"] > c["volume_mult"] * row["vol_avg"],
        "rsi_ok": row["rsi"] < c["rsi_max"],
        "momentum": row["roc"] > 0,
    }
    if "htf_up" in row.index:
        sig["htf_trend"] = row["htf_up"]
    sig = {k: bool(v) for k, v in sig.items()}
    score = sum(sig.values())
    missing = [k for k in c["required"] if not sig[k]]
    return {"signals": sig, "score": score, "missing": missing}


# ---------- 3. RISIKOMANAGER ---------------------------------------------
@dataclass
class RiskState:
    equity: float
    peak: float
    day: object = None
    day_start_equity: float = 0.0
    halted: bool = False          # Drawdown-Notaus
    halted_day: bool = False      # Tageslimit


def risk_update_day(rs, day, R):
    if rs.day != day:
        rs.day = day
        rs.day_start_equity = rs.equity
        rs.halted_day = False
    rs.peak = max(rs.peak, rs.equity)
    if rs.equity <= rs.peak * (1 - R["risk"]["max_drawdown_pct"] / 100):
        rs.halted = True
    if rs.equity <= rs.day_start_equity * (1 - R["risk"]["daily_loss_limit_pct"] / 100):
        rs.halted_day = True


def risk_officer(entry_price, atr_val, rs, open_positions, exposure_eur, R):
    r, e = R["risk"], R["exits"]
    if rs.halted:
        return None, "Desk geschlossen (Max-Drawdown)"
    if rs.halted_day:
        return None, "Tageslimit erreicht"
    if len(open_positions) >= r["max_open_positions"]:
        return None, "max. Positionen offen"
    stop = entry_price - e["stop_atr"] * atr_val
    if stop <= 0:
        return None, "Stop unter 0"
    risk_per_unit = entry_price - stop
    risk_eur = rs.equity * r["risk_per_trade_pct"] / 100
    qty = risk_eur / risk_per_unit
    cap_eur = rs.equity * r["max_position_pct"] / 100
    room_eur = rs.equity * r["max_total_exposure_pct"] / 100 - exposure_eur
    eur = min(qty * entry_price, cap_eur, room_eur)
    if eur <= 0:
        return None, "kein Exposure-Spielraum"
    qty = eur / entry_price
    return {"qty": qty, "eur": eur, "stop": stop, "risk_unit": risk_per_unit}, "ok"


# ---------- 4. GATE -------------------------------------------------------
def gate(carto, plan, R, cash, pair_info=None, spread_pct=None, already_open=False):
    g = R["gate"]
    if already_open:
        return False, "Coin schon im Depot"
    if carto["missing"]:
        return False, "Pflichtsignal fehlt: " + ",".join(carto["missing"])
    if carto["score"] < g["min_score"]:
        return False, f"Score {carto['score']} < {g['min_score']}"
    if plan is None:
        return False, "Risikomanager sagt nein"
    if plan["eur"] < g["min_order_eur"]:
        return False, "Order zu klein"
    if plan["eur"] > cash:
        return False, "nicht genug Cash"
    if pair_info:
        if plan["qty"] < pair_info["ordermin"] or plan["eur"] < pair_info["costmin"]:
            return False, "unter Kraken-Mindestorder"
    if spread_pct is not None and spread_pct > g["max_spread_pct"]:
        return False, f"Spread {spread_pct:.2f}% zu hoch"
    return True, "DURCH"


# ---------- 5. EXIT-VERWALTER --------------------------------------------
@dataclass
class Position:
    pair: str
    entry: float
    qty: float
    stop: float
    risk_unit: float
    entry_bar: int
    entry_time: object
    cost_eur: float
    highest: float = 0.0
    be_done: bool = False
    trailing: bool = False
    history: list = field(default_factory=list)


def exit_check_bar(pos, bar, bar_idx, R):
    """Prüft eine Kerze. Gibt (Exit-Preis, Grund) oder (None, None)."""
    e = R["exits"]
    # Stop innerhalb der Kerze getroffen? (Gap -> Eröffnung)
    if bar["low"] <= pos.stop:
        px = min(bar["open"], pos.stop)
        return px, "Trailing-Stop" if pos.trailing else ("Einstand-Stop" if pos.be_done else "Stop-Loss")
    # Stop nachziehen (erst nach der Kerze -> konservativ)
    pos.highest = max(pos.highest, bar["high"])
    gain_r = (pos.highest - pos.entry) / pos.risk_unit
    if not pos.be_done and gain_r >= e["breakeven_at_r"]:
        pos.stop = max(pos.stop, pos.entry)
        pos.be_done = True
    if gain_r >= e["trail_activate_r"]:
        pos.trailing = True
        pos.stop = max(pos.stop, pos.highest - e["trail_atr"] * bar["atr"])
    # Zeit-Stop: nach X Kerzen nie +1R erreicht -> raus zum Schluss
    if bar_idx - pos.entry_bar >= e["time_stop_bars"] and not pos.be_done:
        return bar["close"], "Zeit-Stop"
    return None, None


# ---------- 6. AUDITOR ----------------------------------------------------
class Auditor:
    def __init__(self, R):
        self.R = R
        self.events = []
        self.trades = []
        self.rejects = {}
        self.violations = []

    def reject(self, stage, reason):
        k = f"{stage}: {reason}"
        self.rejects[k] = self.rejects.get(k, 0) + 1

    def entry(self, t, pos, equity, carto):
        r = self.R["risk"]
        risk_eur = pos.qty * (pos.entry - pos.stop)
        if pos.stop >= pos.entry:
            self.violations.append(f"{t} {pos.pair}: Einstieg ohne gültigen Stop")
        if pos.cost_eur > equity * r["max_position_pct"] / 100 * 1.01:
            self.violations.append(f"{t} {pos.pair}: Position zu groß")
        if risk_eur > equity * r["risk_per_trade_pct"] / 100 * 1.05:
            self.violations.append(f"{t} {pos.pair}: Risiko zu hoch")
        self.events.append({"t": str(t), "type": "BUY", "pair": pos.pair, "price": pos.entry,
                            "eur": round(pos.cost_eur, 2), "stop": pos.stop, "score": carto["score"],
                            "signals": carto["signals"]})

    def exit(self, t, pos, px_net, reason, pnl, bars):
        self.trades.append({"pair": pos.pair, "entry_time": str(pos.entry_time), "exit_time": str(t),
                            "entry": pos.entry, "exit": px_net, "eur_in": round(pos.cost_eur, 2),
                            "pnl_eur": round(pnl, 2), "pnl_pct": round(pnl / pos.cost_eur * 100, 2),
                            "r": round(pnl / (pos.qty * pos.risk_unit), 2) if pos.risk_unit else 0,
                            "bars": bars, "reason": reason})
        self.events.append({"t": str(t), "type": "SELL", "pair": pos.pair, "price": px_net,
                            "pnl_eur": round(pnl, 2), "reason": reason})


def round_down(x, decimals):
    f = 10 ** decimals
    return math.floor(x * f) / f

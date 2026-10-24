"""Backtest des Desks auf echten Kraken-Kerzen.
Aufruf:  python backtest.py [--interval 60] [--cache data_cache]"""
import argparse
import json
import os
import pickle

import numpy as np
import pandas as pd
import yaml

from desk.data import load_universe, asset_pairs
from desk.indicators import enrich
from desk.engine import Desk

HERE = os.path.dirname(os.path.abspath(__file__))


def load_rules(path=os.path.join(HERE, "rules.yaml")):
    with open(path) as f:
        return yaml.safe_load(f)


def scale_rules_to_interval(R, interval):
    """Regeln sind für 1h formuliert. Bei anderem Takt Kerzenlängen umrechnen."""
    if interval == R["timeframe_minutes"]:
        return R
    f = R["timeframe_minutes"] / interval
    R = json.loads(json.dumps(R))
    for sec, keys in {"cartographer": ["breakout_lookback", "volume_lookback", "roc_period"],
                      "exits": ["time_stop_bars"], "risk": ["cooldown_bars_after_loss"]}.items():
        for k in keys:
            R[sec][k] = max(2, int(round(R[sec][k] * f)))
    # ATR-%-Fenster skaliert grob mit Wurzel der Zeit
    R["screener"]["atr_pct_min"] *= (interval / R["timeframe_minutes"]) ** 0.5
    R["screener"]["atr_pct_max"] *= (interval / R["timeframe_minutes"]) ** 0.5
    R["timeframe_minutes"] = interval
    return R


def regime_series(R, data):
    c = R["cartographer"]
    if not c.get("regime_enabled"):
        return None
    df = data.get("__regime__")
    if df is None or not len(df):
        return None
    from desk.indicators import regime_flags, regime_at
    flags, _, _ = regime_flags(df, c["regime_ema"])
    times = sorted(set().union(*[d.index for k, d in data.items() if k != "__regime__"]))
    return regime_at(flags, times)


def run(R, data, pair_info):
    enr = {p: enrich(df, R) for p, df in data.items() if len(df) and p != "__regime__"}
    times = sorted(set().union(*[df.index for df in enr.values()]))
    pos_in_df = {p: {t: k for k, t in enumerate(df.index)} for p, df in enr.items()}
    desk = Desk(R)
    regime = regime_series(R, data)
    for t in times:
        rows, nb = {}, {}
        for p, df in enr.items():
            k = pos_in_df[p].get(t)
            if k is None:
                continue
            row = df.iloc[k]
            if row[["ema_slow", "atr", "hh", "vol_avg", "vol_eur_24h", "roc"]].isna().any():
                nb[p] = k + 1
                rows[p] = row
                continue
            rows[p] = row
            nb[p] = k + 1
        rows = {p: r for p, r in rows.items() if not r[["ema_slow", "atr", "hh", "vol_avg", "vol_eur_24h", "roc"]].isna().any()}
        allow = bool(regime.get(t, False)) if regime is not None else True
        if not allow:
            desk.aud.reject("Kartograf", "Marktampel rot (BTC unter EMA)")
        desk.process_bar(t, rows, nb, pair_info=pair_info, allow_new=allow)
    # offene Positionen zum letzten Kurs bewerten (nicht schließen)
    return desk, times


def stats(desk, R, data, times):
    eq = pd.Series([e for _, e in desk.equity_curve], index=[t for t, _ in desk.equity_curve])
    start = R["account"]["start_capital_eur"]
    tr = pd.DataFrame(desk.aud.trades)
    dd = (eq / eq.cummax() - 1).min() * 100
    out = {
        "zeitraum": f"{times[0]:%d.%m.%Y %H:%M} – {times[-1]:%d.%m.%Y %H:%M} UTC",
        "kerzen": len(times),
        "start_eur": start,
        "ende_eur": round(eq.iloc[-1], 2),
        "rendite_pct": round((eq.iloc[-1] / start - 1) * 100, 2),
        "max_drawdown_pct": round(dd, 2),
        "trades": int(len(tr)),
        "offene_positionen": list(desk.positions),
        "gebuehren_eur": round(desk.fees_paid, 2),
    }
    if len(tr):
        wins = tr[tr.pnl_eur > 0]
        loss = tr[tr.pnl_eur <= 0]
        out.update({
            "trefferquote_pct": round(len(wins) / len(tr) * 100, 1),
            "avg_gewinn_eur": round(wins.pnl_eur.mean(), 2) if len(wins) else 0,
            "avg_verlust_eur": round(loss.pnl_eur.mean(), 2) if len(loss) else 0,
            "profit_factor": round(wins.pnl_eur.sum() / -loss.pnl_eur.sum(), 2) if len(loss) and loss.pnl_eur.sum() < 0 else None,
            "avg_R": round(tr.r.mean(), 2),
            "bester_trade": tr.loc[tr.pnl_eur.idxmax(), ["pair", "pnl_eur", "pnl_pct"]].to_dict(),
            "schlechtester_trade": tr.loc[tr.pnl_eur.idxmin(), ["pair", "pnl_eur", "pnl_pct"]].to_dict(),
            "exit_gruende": tr.reason.value_counts().to_dict(),
            "pnl_je_coin": tr.groupby("pair").pnl_eur.sum().round(2).sort_values().to_dict(),
        })
    # Benchmark: gleichgewichtet alle Coins kaufen & halten
    rets = []
    for p, df in data.items():
        if p == "__regime__":
            continue
        d = df[(df.index >= times[0]) & (df.index <= times[-1])]
        if len(d) > 10:
            rets.append(d.close.iloc[-1] / d.open.iloc[0] - 1)
    out["benchmark_buy_hold_pct"] = round(float(np.mean(rets)) * 100, 2) if rets else None
    out["auditor_verstoesse"] = desk.aud.violations
    funnel = sorted(desk.aud.rejects.items(), key=lambda x: -x[1])
    out["aussortiert"] = dict(funnel)
    return out, eq, tr


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=int, default=60)
    ap.add_argument("--cache", default=os.path.join(HERE, "data_cache"))
    ap.add_argument("--tag", default=None)
    ap.add_argument("--no-regime", action="store_true")
    a = ap.parse_args()
    R = scale_rules_to_interval(load_rules(), a.interval)
    os.makedirs(a.cache, exist_ok=True)
    cf = os.path.join(a.cache, f"ohlc_{a.interval}.pkl")
    if os.path.exists(cf):
        data, info = pickle.load(open(cf, "rb"))
    else:
        info = asset_pairs(R["universe"])
        data = load_universe(R["universe"], a.interval)
        pickle.dump((data, info), open(cf, "wb"))
    if "__regime__" not in data:
        from desk.data import ohlc
        data["__regime__"] = ohlc(R["cartographer"]["regime_pair"], a.interval)
        pickle.dump((data, info), open(cf, "wb"))
    if a.no_regime:
        R["cartographer"]["regime_enabled"] = False
    desk, times = run(R, data, info)
    s, eq, tr = stats(desk, R, data, times)
    tag = a.tag or f"{a.interval}m"
    logdir = os.path.join(HERE, R["auditor"]["log_dir"])
    os.makedirs(logdir, exist_ok=True)
    json.dump(s, open(os.path.join(logdir, f"backtest_{tag}.json"), "w"), indent=2, ensure_ascii=False, default=str)
    tr.to_csv(os.path.join(logdir, f"backtest_{tag}_trades.csv"), index=False)
    eq.to_csv(os.path.join(logdir, f"backtest_{tag}_equity.csv"), header=["equity"])
    print(json.dumps(s, indent=2, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()

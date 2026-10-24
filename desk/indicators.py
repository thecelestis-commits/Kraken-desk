import numpy as np
import pandas as pd


def ema(s, n):
    return s.ewm(span=n, adjust=False).mean()


def atr(df, n=14):
    pc = df["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"], (df["high"] - pc).abs(), (df["low"] - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def rsi(s, n=14):
    d = s.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    rs = up / dn.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(100)


def enrich(df, R):
    """Alle Indikatoren, die die Stufen brauchen, in einem Rutsch."""
    c = R["cartographer"]
    out = df.copy()
    out["ema_fast"] = ema(out["close"], c["ema_fast"])
    out["ema_slow"] = ema(out["close"], c["ema_slow"])
    out["atr"] = atr(out, R["exits"]["atr_period"])
    out["atr_pct"] = out["atr"] / out["close"] * 100
    out["rsi"] = rsi(out["close"], c["rsi_period"])
    out["roc"] = out["close"].pct_change(c["roc_period"]) * 100
    out["hh"] = out["high"].rolling(c["breakout_lookback"]).max().shift(1)
    out["vol_avg"] = out["volume"].rolling(c["volume_lookback"]).mean().shift(1)
    bars_per_day = max(1, int(round(1440 / R["timeframe_minutes"])))
    out["vol_eur_24h"] = (out["volume"] * out["close"]).rolling(bars_per_day).sum()
    # Trend im höheren Zeitrahmen (z. B. 4h), nur aus bereits abgeschlossenen Kerzen
    htf = c.get("htf_minutes")
    if htf and htf > R["timeframe_minutes"]:
        hc = out["close"].resample(f"{htf}min", label="left", closed="left").last().dropna()
        he = hc.ewm(span=c.get("htf_ema", 50), adjust=False).mean()
        ok = (hc > he)
        ok.index = ok.index + pd.Timedelta(minutes=htf - R["timeframe_minutes"])
        out["htf_up"] = ok.reindex(out.index, method="ffill").fillna(False).astype(bool)
    return out


def regime_flags(btc, ema_days):
    """Strenge Marktampel: BTC-Tagesschluss über EMA(ema_days).
    btc: Kerzen beliebiger Größe (wird zu Tageskerzen). Rückgabe: Serie, gültig ab dem
    Moment, in dem der Tag abgeschlossen ist (kein Blick in die Zukunft)."""
    daily = btc["close"].resample("1D", label="left", closed="left").last().dropna()
    flags = daily > daily.ewm(span=ema_days, adjust=False).mean()
    flags.index = flags.index + pd.Timedelta(days=1)
    return flags, daily, daily.ewm(span=ema_days, adjust=False).mean()


def regime_at(flags, times):
    s = flags.reindex(flags.index.union(pd.DatetimeIndex(times))).ffill()
    return {t: bool(s.get(t, False)) if not pd.isna(s.get(t)) else False for t in times}

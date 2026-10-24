"""Öffentliche Kraken-Daten: Kerzen, Ticker, Paar-Infos."""
import json
import time
import urllib.request
import urllib.parse

import pandas as pd

API = "https://api.kraken.com/0/public/"


def _get(endpoint, params=None, retries=3):
    url = API + endpoint
    if params:
        url += "?" + urllib.parse.urlencode(params)
    last = None
    for i in range(retries):
        try:
            with urllib.request.urlopen(url, timeout=20) as r:
                d = json.load(r)
            if d.get("error"):
                raise RuntimeError(f"Kraken {endpoint}: {d['error']}")
            return d["result"]
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1.5 * (i + 1))
    raise last


def asset_pairs(pairs):
    res = _get("AssetPairs", {"pair": ",".join(pairs)})
    out = {}
    for key, v in res.items():
        alt = v.get("altname", key)
        out[alt] = {
            "key": key,
            "ordermin": float(v.get("ordermin", 0) or 0),
            "costmin": float(v.get("costmin", 0) or 0),
            "lot_decimals": int(v.get("lot_decimals", 8)),
            "pair_decimals": int(v.get("pair_decimals", 8)),
        }
    return out


def ohlc(pair, interval=60, drop_open_bar=True):
    """Bis zu 720 Kerzen. Die letzte (noch offene) Kerze wird verworfen."""
    res = _get("OHLC", {"pair": pair, "interval": interval})
    key = [k for k in res if k != "last"][0]
    df = pd.DataFrame(res[key], columns=["time", "open", "high", "low", "close", "vwap", "volume", "count"])
    df["time"] = pd.to_datetime(df["time"].astype(int), unit="s", utc=True)
    for c in ["open", "high", "low", "close", "vwap", "volume"]:
        df[c] = df[c].astype(float)
    df = df.set_index("time")[["open", "high", "low", "close", "volume"]]
    if drop_open_bar and len(df):
        df = df.iloc[:-1]
    return df


def ticker(pairs):
    res = _get("Ticker", {"pair": ",".join(pairs)})
    out = {}
    for key, v in res.items():
        ask, bid = float(v["a"][0]), float(v["b"][0])
        out[key] = {"ask": ask, "bid": bid, "last": float(v["c"][0]),
                    "spread_pct": (ask - bid) / ((ask + bid) / 2) * 100}
    return out


STABLES = {"USDC", "USDT", "EURC", "DAI", "PYUSD", "USDG", "RLUSD", "EURQ", "TUSD", "USDS", "EURR", "USDQ",
            "USDE", "FDUSD", "EUROP", "EURT", "USD", "GBP", "CHF", "AUD", "CAD", "JPY", "PAXG", "XAUT"}


def auto_universe(R):
    """Umsatzstärkste EUR-Paare bei Kraken (ohne Stablecoins/Fiat/Gold), täglich neu."""
    cfg = R.get("universe_auto", {})
    pairs = _get("AssetPairs")
    tick = _get("Ticker")
    excl = STABLES | set(cfg.get("exclude", []))
    rows = []
    for k, v in tick.items():
        meta = pairs.get(k)
        if not meta or meta.get("status", "online") != "online":
            continue
        ws = meta.get("wsname", "")
        if not ws.endswith("/EUR"):
            continue
        base = ws.split("/")[0]
        if base in excl or base in ("XBT",) and not cfg.get("include_btc", True):
            continue
        vol = float(v["v"][1]) * float(v["p"][1])
        if vol >= cfg.get("min_volume_eur_24h", 150000):
            rows.append((vol, meta["altname"]))
    rows.sort(reverse=True)
    return [a for _, a in rows[: cfg.get("top_n", 40)]]


def resolve_universe(R):
    if R.get("universe") == "auto":
        R["universe"] = auto_universe(R)
    return R["universe"]


def load_universe(pairs, interval=60, pause=0.4):
    data = {}
    for p in pairs:
        try:
            data[p] = ohlc(p, interval)
        except Exception as e:  # noqa: BLE001
            print(f"[data] {p}: {e}")
        time.sleep(pause)  # Kraken Rate-Limit schonen
    return data

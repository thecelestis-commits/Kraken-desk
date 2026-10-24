"""Echte Orders bei Kraken (Spot). Keys kommen aus Umgebungsvariablen:
KRAKEN_API_KEY, KRAKEN_API_SECRET  – Rechte: Abfragen + Orders erstellen/stornieren.
KEINE Auszahlungsrechte vergeben!"""
import base64
import hashlib
import hmac
import json
import os
import time
import urllib.parse
import urllib.request

from .stages import round_down

API = "https://api.kraken.com"


class KrakenAPI:
    def __init__(self, key=None, secret=None):
        self.key = key or os.environ["KRAKEN_API_KEY"]
        self.secret = base64.b64decode(secret or os.environ["KRAKEN_API_SECRET"])

    def private(self, method, data=None):
        path = f"/0/private/{method}"
        data = dict(data or {})
        data["nonce"] = str(int(time.time() * 1000))
        post = urllib.parse.urlencode(data)
        sha = hashlib.sha256((data["nonce"] + post).encode()).digest()
        sig = base64.b64encode(hmac.new(self.secret, path.encode() + sha, hashlib.sha512).digest())
        req = urllib.request.Request(API + path, data=post.encode(),
                                     headers={"API-Key": self.key, "API-Sign": sig.decode(),
                                              "User-Agent": "kraken-meme-desk"})
        with urllib.request.urlopen(req, timeout=30) as r:
            d = json.load(r)
        if d.get("error"):
            raise RuntimeError(f"Kraken {method}: {d['error']}")
        return d["result"]


class KrakenBroker:
    """Gleiche Schnittstelle wie SimBroker. Jede Position bekommt eine echte
    Stop-Loss-Order an der Börse, die bei jedem Nachziehen ersetzt wird."""

    def __init__(self, R, pair_info, api=None, state=None):
        self.fee = R["costs"]["fee_pct"] / 100
        self.info = pair_info
        self.api = api or KrakenAPI()
        self.stops = (state or {}).get("stops", {})        # pair -> txid
        self.exchange_fills = {}                            # pair -> Fill der Stop-Order

    def state(self):
        return {"stops": self.stops}

    def _wait_fill(self, txid, tries=10):
        for _ in range(tries):
            o = self.api.private("QueryOrders", {"txid": txid})[txid]
            if o["status"] == "closed":
                vol = float(o["vol_exec"])
                return {"price": float(o["cost"]) / vol, "qty": vol, "cost": float(o["cost"]), "fee": float(o["fee"])}
            if o["status"] in ("canceled", "expired"):
                return None
            time.sleep(1.5)
        return None

    def balance_eur(self):
        b = self.api.private("Balance")
        return float(b.get("ZEUR", b.get("EUR", 0)))

    # --- Schnittstelle --------------------------------------------------
    def _order_exec(self, txid):
        o = self.api.private("QueryOrders", {"txid": txid})[txid]
        return o["status"], float(o["vol_exec"]), float(o["cost"]), float(o["fee"])

    def buy(self, pair, qty, ref_price, attempts=3, wait_s=20):
        """Erst günstige Maker-Limit-Orders (post-only am Bid, 3 Versuche à 20 s),
        Rest per Market-Order, damit kein Signal verloren geht."""
        from .data import ticker
        i = self.info[pair]
        target = round_down(qty, i["lot_decimals"])
        if target < i["ordermin"]:
            return None
        got_vol = got_cost = got_fee = 0.0
        for _ in range(attempts):
            rest = round_down(target - got_vol, i["lot_decimals"])
            if rest < i["ordermin"]:
                break
            bid = list(ticker([i["key"]]).values())[0]["bid"]
            px = round_down(bid, i["pair_decimals"])
            try:
                res = self.api.private("AddOrder", {"pair": pair, "type": "buy", "ordertype": "limit",
                                                    "price": f"{px}", "volume": f"{rest}", "oflags": "post"})
            except RuntimeError as e:
                print(f"[live] Limit {pair}: {e}")
                continue
            tx = res["txid"][0]
            t_end = time.time() + wait_s
            status = "open"
            while time.time() < t_end:
                time.sleep(2)
                status, *_ = self._order_exec(tx)
                if status in ("closed", "canceled", "expired"):
                    break
            if status not in ("closed", "canceled", "expired"):
                try:
                    self.api.private("CancelOrder", {"txid": tx})
                except RuntimeError:
                    pass
                time.sleep(1)
            _, v, c, f = self._order_exec(tx)
            got_vol, got_cost, got_fee = got_vol + v, got_cost + c, got_fee + f
        rest = round_down(target - got_vol, i["lot_decimals"])
        if rest >= i["ordermin"]:
            res = self.api.private("AddOrder", {"pair": pair, "type": "buy", "ordertype": "market", "volume": f"{rest}"})
            f = self._wait_fill(res["txid"][0])
            if f:
                got_vol, got_cost, got_fee = got_vol + f["qty"], got_cost + f["cost"], got_fee + f["fee"]
        if got_vol <= 0:
            return None
        return {"price": got_cost / got_vol, "qty": got_vol, "cost": got_cost, "fee": got_fee}

    def sell(self, pos, ref_price):
        if pos.pair in self.exchange_fills:                 # Börse hat schon verkauft
            f = self.exchange_fills.pop(pos.pair)
            self.stops.pop(pos.pair, None)
            return {"price": f["price"], "proceeds": f["cost"], "fee": f["fee"]}
        self._cancel_stop(pos.pair)
        vol = round_down(pos.qty, self.info[pos.pair]["lot_decimals"])
        res = self.api.private("AddOrder", {"pair": pos.pair, "type": "sell", "ordertype": "market", "volume": f"{vol}"})
        f = self._wait_fill(res["txid"][0])
        if f is None:
            raise RuntimeError(f"Verkauf {pos.pair} nicht bestätigt – manuell prüfen!")
        return {"price": f["price"], "proceeds": f["cost"], "fee": f["fee"]}

    def _cancel_stop(self, pair):
        tx = self.stops.pop(pair, None)
        if tx:
            try:
                self.api.private("CancelOrder", {"txid": tx})
            except RuntimeError as e:
                print(f"[live] Stop {pair} stornieren: {e}")

    def on_stop_moved(self, pos):
        self._cancel_stop(pos.pair)
        i = self.info[pos.pair]
        vol = round_down(pos.qty, i["lot_decimals"])
        price = round_down(pos.stop, i["pair_decimals"])
        res = self.api.private("AddOrder", {"pair": pos.pair, "type": "sell", "ordertype": "stop-loss",
                                            "price": f"{price}", "volume": f"{vol}"})
        self.stops[pos.pair] = res["txid"][0]

    def stop_filled(self, pos):
        tx = self.stops.get(pos.pair)
        if not tx:
            return None
        o = self.api.private("QueryOrders", {"txid": tx})[tx]
        if o["status"] == "closed" and float(o["vol_exec"]) > 0:
            vol = float(o["vol_exec"])
            f = {"price": float(o["cost"]) / vol, "cost": float(o["cost"]), "fee": float(o["fee"])}
            self.exchange_fills[pos.pair] = f
            return f["price"]
        return None

"""Minimal Kraken REST client with hard safety rails.

Safety is structural, not configurable:
  * Private calls go through `_private`, which only accepts endpoints in
    ALLOWED_PRIVATE. Withdraw*, WalletTransfer, margin/position endpoints etc.
    are not in the list, so they cannot be called.
  * `add_order` refuses any margin parameter (leverage, reduce_only, margin
    flags), forces spot ordertypes, and rejects orders above
    HARD_MAX_TRADE_EUR notional (incl. fee) before anything is sent.
Use an API key WITHOUT withdraw permission as an additional, exchange-side guard.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import threading
import time
import urllib.parse

import requests

from . import config

API_URL = "https://api.kraken.com"

ALLOWED_PRIVATE = frozenset({
    "Balance", "TradeBalance", "OpenOrders", "ClosedOrders", "QueryOrders",
    "TradesHistory", "TradeVolume", "AddOrder", "CancelOrder", "CancelAll",
})
FORBIDDEN_ORDER_KEYS = frozenset({"leverage", "reduce_only", "margin", "close[ordertype]",
                                  "close[price]", "close[price2]"})
ALLOWED_ORDERTYPES = frozenset({"limit", "market", "stop-loss"})


class SafetyViolation(Exception):
    """Raised when a call would violate a hard limit. Never caught silently."""


class KrakenError(Exception):
    pass


class KrakenClient:
    def __init__(self, key: str | None = None, secret: str | None = None):
        self.key = key or os.environ.get("KRAKEN_API_KEY")
        self.secret = secret or os.environ.get("KRAKEN_API_SECRET")
        self.session = requests.Session()
        self._nonce_lock = threading.Lock()
        self._last_nonce = 0

    # ------------------------------------------------------------ transport
    def public(self, endpoint: str, **params) -> dict:
        for attempt in range(5):
            try:
                r = self.session.get(f"{API_URL}/0/public/{endpoint}", params=params, timeout=20)
                j = r.json()
            except (requests.RequestException, ValueError):
                time.sleep(2 ** attempt)
                continue
            if j.get("error"):
                if any("Rate limit" in e or "Unavailable" in e or "Busy" in e for e in j["error"]):
                    time.sleep(2 ** attempt)
                    continue
                raise KrakenError(f"{endpoint}: {j['error']}")
            return j["result"]
        raise KrakenError(f"{endpoint}: retries exhausted")

    def _nonce(self) -> int:
        with self._nonce_lock:
            n = max(int(time.time() * 1000), self._last_nonce + 1)
            self._last_nonce = n
            return n

    def _private(self, endpoint: str, **data) -> dict:
        if endpoint not in ALLOWED_PRIVATE:
            raise SafetyViolation(f"private endpoint '{endpoint}' is not whitelisted")
        if not (self.key and self.secret):
            raise KrakenError("KRAKEN_API_KEY / KRAKEN_API_SECRET not set")
        path = f"/0/private/{endpoint}"
        data["nonce"] = self._nonce()
        post = urllib.parse.urlencode(data)
        sha = hashlib.sha256((str(data["nonce"]) + post).encode()).digest()
        sig = hmac.new(base64.b64decode(self.secret), path.encode() + sha, hashlib.sha512)
        headers = {"API-Key": self.key, "API-Sign": base64.b64encode(sig.digest()).decode()}
        r = self.session.post(API_URL + path, data=post, headers=headers, timeout=20)
        j = r.json()
        if j.get("error"):
            raise KrakenError(f"{endpoint}: {j['error']}")
        return j["result"]

    # ------------------------------------------------------------ public helpers
    def ohlc(self, pair: str, interval_min: int) -> list:
        res = self.public("OHLC", pair=pair, interval=interval_min)
        res.pop("last", None)
        return next(iter(res.values()))

    def ohlc_since(self, pair: str, interval_min: int, since: int) -> list:
        res = self.public("OHLC", pair=pair, interval=interval_min, since=since)
        res.pop("last", None)
        return next(iter(res.values()))

    def ticker(self, pairs: list[str]) -> dict:
        return self.public("Ticker", pair=",".join(pairs))

    def asset_pairs(self, pairs: list[str]) -> dict:
        return self.public("AssetPairs", pair=",".join(pairs))

    # ------------------------------------------------------------ private helpers
    def balance(self) -> dict:
        return {k: float(v) for k, v in self._private("Balance").items()}

    def open_orders(self) -> dict:
        return self._private("OpenOrders")["open"]

    def query_orders(self, txids: list[str]) -> dict:
        return self._private("QueryOrders", txid=",".join(txids), trades=True) if txids else {}

    def trade_volume(self, pair: str) -> dict:
        return self._private("TradeVolume", pair=pair)

    def cancel_order(self, txid: str) -> dict:
        return self._private("CancelOrder", txid=txid)

    def cancel_all(self) -> dict:
        return self._private("CancelAll")

    def add_order(self, pair: str, side: str, ordertype: str, volume: float, ref_price: float,
                  price: float | None = None, post_only: bool = False, userref: int | None = None,
                  validate: bool = False) -> dict:
        """Place a spot order. `ref_price` is the price used for the notional
        check (limit price, or current ask for market buys)."""
        params = {"pair": pair, "type": side, "ordertype": ordertype, "volume": f"{volume:.8f}"}
        if price is not None:
            params["price"] = f"{price}"
        if post_only:
            params["oflags"] = "post"
        if userref is not None:
            params["userref"] = int(userref)
        if validate:
            params["validate"] = True
        self.check_order(params, ref_price)
        return self._private("AddOrder", **params)

    @staticmethod
    def check_order(params: dict, ref_price: float) -> None:
        bad = FORBIDDEN_ORDER_KEYS & set(params)
        if bad:
            raise SafetyViolation(f"margin parameters not allowed: {sorted(bad)}")
        if params["ordertype"] not in ALLOWED_ORDERTYPES:
            raise SafetyViolation(f"ordertype {params['ordertype']} not allowed")
        if params["type"] not in ("buy", "sell"):
            raise SafetyViolation("side must be buy/sell")
        if not params["pair"].endswith("EUR"):
            raise SafetyViolation("only EUR spot pairs allowed")
        notional = float(params["volume"]) * ref_price * (1 + config.TAKER_FEE)
        # Sells only reduce existing spot holdings, but cap them too so a bug can
        # never dump more than one trade's worth (plus 50 % price drift) at once.
        cap = config.HARD_MAX_TRADE_EUR if params["type"] == "buy" else config.HARD_MAX_TRADE_EUR * 1.5
        if not (notional > 0 and notional <= cap):
            raise SafetyViolation(f"order notional {notional:.2f} EUR exceeds hard cap {cap:.2f} EUR")

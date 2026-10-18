"""Historical data: download Kraken trades and aggregate them to OHLCV bars.

Kraken's OHLC endpoint only returns the last 720 candles, so for a backtest
covering all of 2026 we page through the public Trades endpoint and build
1-minute bars ourselves. Downloads are resumable (cursor file per pair).
"""
from __future__ import annotations

import argparse
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
API = "https://api.kraken.com/0/public"
DEFAULT_START = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _get(endpoint: str, params: dict) -> dict:
    delay = 1.0
    while True:
        try:
            r = requests.get(f"{API}/{endpoint}", params=params, timeout=30)
            j = r.json()
        except (requests.RequestException, ValueError) as e:
            print(f"  network error {e}, retry in {delay:.0f}s", flush=True)
            time.sleep(delay)
            delay = min(delay * 2, 60)
            continue
        if j.get("error"):
            if any("Rate limit" in e or "Too many" in e for e in j["error"]):
                time.sleep(delay)
                delay = min(delay * 2, 60)
                continue
            raise RuntimeError(f"{endpoint}: {j['error']}")
        return j["result"]


def _trades_to_minutes(trades: list) -> pd.DataFrame:
    df = pd.DataFrame(trades, columns=["price", "vol", "time", "side", "type", "misc", "id"][: len(trades[0])])
    df["price"] = df["price"].astype(float)
    df["vol"] = df["vol"].astype(float)
    df["ts"] = pd.to_datetime(df["time"].astype(float), unit="s", utc=True).dt.floor("1min")
    g = df.groupby("ts")
    return pd.DataFrame({
        "open": g["price"].first(), "high": g["price"].max(), "low": g["price"].min(),
        "close": g["price"].last(), "volume": g["vol"].sum(),
    })


def download_trades(pair: str, start: datetime = DEFAULT_START, until: datetime | None = None) -> Path:
    """Download all trades for `pair` since `start` into 1m bars (resumable)."""
    DATA_DIR.mkdir(exist_ok=True)
    out = DATA_DIR / f"{pair}_1m.csv"
    cursor_file = DATA_DIR / f"{pair}.cursor"
    cursor = int(cursor_file.read_text()) if cursor_file.exists() else int(start.timestamp() * 1e9)
    end_ts = (until or datetime.now(timezone.utc)).timestamp()
    buf: list = []
    n_req = 0
    while True:
        res = _get("Trades", {"pair": pair, "since": cursor, "count": 1000})
        last = int(res.pop("last"))
        trades = next(iter(res.values()))
        n_req += 1
        if trades:
            buf.extend(trades)
        done = not trades or last == cursor or float(trades[-1][2]) >= end_ts - 60
        cursor = last
        if len(buf) >= 50_000 or done:
            if buf:
                bars = _trades_to_minutes(buf)
                bars.to_csv(out, mode="a", header=not out.exists())
                buf = []
            cursor_file.write_text(str(cursor))
            stamp = datetime.fromtimestamp(cursor / 1e9, timezone.utc)
            print(f"{pair}: {n_req} requests, up to {stamp:%Y-%m-%d %H:%M}", flush=True)
        if done:
            return out
        time.sleep(0.35)


def load_bars(pair: str, timeframe: str = "5min") -> pd.DataFrame:
    """Load 1m bars from disk and resample to `timeframe` (gaps forward-filled)."""
    raw = pd.read_csv(DATA_DIR / f"{pair}_1m.csv", index_col=0, parse_dates=True)
    # A minute can be split across two download chunks: merge duplicates.
    g = raw.groupby(level=0)
    m1 = pd.DataFrame({"open": g["open"].first(), "high": g["high"].max(), "low": g["low"].min(),
                       "close": g["close"].last(), "volume": g["volume"].sum()}).sort_index()
    r = m1.resample(timeframe, label="left", closed="left")
    bars = pd.DataFrame({"open": r["open"].first(), "high": r["high"].max(), "low": r["low"].min(),
                         "close": r["close"].last(), "volume": r["volume"].sum()})
    close = bars["close"].ffill()
    for c in ("open", "high", "low"):
        bars[c] = bars[c].fillna(close)
    bars["close"] = close
    return bars.dropna()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("pairs", nargs="+")
    args = ap.parse_args()
    for p in args.pairs:
        download_trades(p)

"""Bar-based backtester with a conservative limit-order fill model.

Fill model (per position, one position at a time per strategy/pair/timeframe):
  * Signal on bar t close -> post-only buy limit is live from bar t+1 for
    ENTRY_ORDER_TTL_BARS bars. If the limit would cross (open below limit),
    it is re-priced to the open (= joining the bid, still maker).
  * A limit only fills if price trades THROUGH it (low < limit), not on touch.
  * Take-profit is a resting limit sell (maker), fills if high > tp, checked
    from the bar after the entry fill.
  * Stop-loss is a market order (taker + slippage), filled at min(sl, open).
    Checked from the fill bar on. SL and TP in the same bar -> SL (pessimistic).
  * Time stop: market sell at the close of bar fill+max_bars (taker + slippage).
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd

from . import config
from .data import load_bars
from .strategies import STRATEGIES, param_grid

# Half-spread + impact estimate for market orders, from measured Kraken spreads.
SLIPPAGE = {"XBTEUR": 0.0003, "ETHEUR": 0.0003, "SOLEUR": 0.0008, "XRPEUR": 0.0010,
            "ADAEUR": 0.0010, "LTCEUR": 0.0012}


@dataclass
class Trade:
    entry_time: pd.Timestamp
    exit_time: pd.Timestamp
    entry: float
    exit: float
    kind: str      # tp / sl / time
    pnl_eur: float  # per TRADE_EUR notional, net of fees and slippage


def simulate(df: pd.DataFrame, sig, pair: str, maker=config.MAKER_FEE, taker=config.TAKER_FEE,
             notional=config.TRADE_EUR, ttl=config.ENTRY_ORDER_TTL_BARS) -> list[Trade]:
    o, h, l, c = (df[k].to_numpy() for k in ("open", "high", "low", "close"))
    idx = df.index
    entry = sig.entry.to_numpy()
    limit, tp, sl = sig.limit.to_numpy(), sig.tp.to_numpy(), sig.sl.to_numpy()
    slip = SLIPPAGE.get(pair, 0.001)
    n = len(df)
    trades: list[Trade] = []
    t = 0
    signal_bars = np.flatnonzero(entry)
    si = 0
    while si < len(signal_bars):
        s = signal_bars[si]
        si += 1
        if s < t or s + 1 >= n or not np.isfinite(limit[s]) or not np.isfinite(sl[s]):
            continue
        lim, take, stop = limit[s], tp[s], sl[s]
        if take <= lim or stop >= lim:
            continue
        # --- entry
        fill = -1
        for j in range(s + 1, min(s + 1 + ttl, n)):
            px = min(lim, o[j]) if j == s + 1 else lim
            if l[j] < px:
                fill, lim = j, px
                break
        if fill < 0:
            t = s + 1
            continue
        # keep tp/sl distances if the entry was re-priced
        take, stop = take - (limit[s] - lim), stop - (limit[s] - lim)
        qty = notional / lim
        cost = notional * (1 + maker)
        # --- exit
        exit_px, kind, k = None, None, None
        last = min(fill + sig.max_bars, n - 1)
        for k in range(fill, last + 1):
            if l[k] <= stop:
                exit_px, kind = min(stop, o[k]) * (1 - slip), "sl"
                fee = taker
                break
            if k > fill and h[k] > take:
                exit_px, kind, fee = take, "tp", maker
                break
        if exit_px is None:
            k = last
            exit_px, kind, fee = c[k] * (1 - slip), "time", taker
        proceeds = qty * exit_px * (1 - fee)
        trades.append(Trade(idx[fill], idx[k], lim, exit_px, kind, proceeds - cost))
        t = k + 1
    return trades


def stats(trades: list[Trade]) -> dict:
    if not trades:
        return dict(n=0, pnl=0.0, win=0.0, pf=0.0, avg=0.0, mdd=0.0)
    p = np.array([x.pnl_eur for x in trades])
    eq = np.cumsum(p)
    mdd = float(np.max(np.maximum.accumulate(np.r_[0, eq]) - np.r_[0, eq]))
    gains, losses = p[p > 0].sum(), -p[p < 0].sum()
    return dict(n=len(p), pnl=round(float(p.sum()), 3), win=round(float((p > 0).mean()), 3),
                pf=round(float(gains / losses), 3) if losses > 0 else float("inf"),
                avg=round(float(p.mean()), 4), mdd=round(mdd, 3))


def run_grid(pairs=config.PAIRS, timeframes=config.TIMEFRAMES) -> pd.DataFrame:
    rows = []
    split = pd.Timestamp(config.IS_END, tz="UTC")
    for pair in pairs:
        for tf, rule in timeframes.items():
            try:
                df = load_bars(pair, rule)
            except FileNotFoundError:
                print(f"no data for {pair}, skipped")
                break
            df = df[df.index >= pd.Timestamp(config.BACKTEST_START, tz="UTC")]
            for name, fn in STRATEGIES.items():
                for params in param_grid(name):
                    sig = fn(df, **params)
                    tr = simulate(df, sig, pair)
                    ins = stats([x for x in tr if x.entry_time < split])
                    oos = stats([x for x in tr if x.entry_time >= split])
                    rows.append(dict(pair=pair, tf=tf, strategy=name, params=json.dumps(params),
                                     **{f"is_{k}": v for k, v in ins.items()},
                                     **{f"oos_{k}": v for k, v in oos.items()}))
            print(f"{pair} {tf}: done ({len(df)} bars)", flush=True)
    return pd.DataFrame(rows)

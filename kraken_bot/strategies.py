"""Long-only short-term strategies (spot, no margin -> no shorts).

Every strategy turns a bar DataFrame into a `Signals` object:
  entry[t]       True if a buy should be placed after bar t closed
  limit[t]       limit price of that buy (post-only maker order)
  tp[t], sl[t]   absolute take-profit / stop-loss prices for that entry
  max_bars       time stop (bars after fill)
  reason[t]      human readable justification, logged by the live bot
All values at index t only use data up to and including bar t.
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field

import numpy as np
import pandas as pd


# ---------------------------------------------------------------- indicators
def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def rsi(s: pd.Series, n: int) -> pd.Series:
    d = s.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + up / dn.replace(0, np.nan))


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    pc = df["close"].shift()
    tr = pd.concat([df["high"] - df["low"], (df["high"] - pc).abs(), (df["low"] - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


# ---------------------------------------------------------------- signals
@dataclass
class Signals:
    entry: pd.Series
    limit: pd.Series
    tp: pd.Series
    sl: pd.Series
    max_bars: int
    reason: pd.Series = field(repr=False)


def _fmt(template: str, **cols) -> pd.Series:
    """Per-entry reason strings. Series kwargs vary per bar, scalars are constants."""
    const = {k: v for k, v in cols.items() if not isinstance(v, pd.Series)}
    series = pd.DataFrame({k: v for k, v in cols.items() if isinstance(v, pd.Series)})
    return series.apply(lambda r: template.format(**const, **r.to_dict()), axis=1) if len(series) else pd.Series(dtype=str)


def _signals(entry, limit, a, tp_atr, sl_atr, min_tp, max_bars, reason) -> Signals:
    """Exit levels from ATR multiples, but the target is at least `min_tp`
    (fraction of price) so a winning trade clears the ~0.5-0.65 % round-trip
    fees. The stop widens in proportion to keep the reward/risk ratio."""
    tp_dist = np.maximum(tp_atr * a, min_tp * limit)
    sl_dist = np.maximum(sl_atr * a, min_tp * limit * sl_atr / tp_atr)
    return Signals(entry.fillna(False), limit, limit + tp_dist, limit - sl_dist, max_bars, reason)


def rsi_reversion(df: pd.DataFrame, rsi_n=7, rsi_lo=20, bb_n=20, bb_k=2.0, tp_atr=1.5, sl_atr=2.0, max_bars=24, min_tp=0.01) -> Signals:
    """Oversold snap-back: RSI below threshold AND close below lower Bollinger band.
    Buys slightly below the close, targets a bounce of tp_atr * ATR."""
    c = df["close"]
    r = rsi(c, rsi_n)
    mid = c.rolling(bb_n).mean()
    lower = mid - bb_k * c.rolling(bb_n).std()
    a = atr(df)
    entry = (r < rsi_lo) & (c < lower)
    limit = c - 0.25 * a
    reason = _fmt("RSI{n}={r:.1f}<{lo} und Close {c:.6g} < unteres BB {lb:.6g}: überverkauft, Rebound erwartet",
                  n=rsi_n, r=r[entry], lo=rsi_lo,
                  c=c[entry], lb=lower[entry])
    return _signals(entry, limit, a, tp_atr, sl_atr, min_tp, max_bars, reason)


def bb_breakout(df: pd.DataFrame, bb_n=20, bb_k=2.0, vol_mult=1.5, tp_atr=3.0, sl_atr=1.5, max_bars=36, min_tp=0.01) -> Signals:
    """Volatility breakout: close above upper band with above-average volume.
    Entry is a limit at the broken band (buy the retest)."""
    c = df["close"]
    mid = c.rolling(bb_n).mean()
    upper = mid + bb_k * c.rolling(bb_n).std()
    vavg = df["volume"].rolling(bb_n).mean()
    a = atr(df)
    entry = (c > upper) & (df["volume"] > vol_mult * vavg) & (c.shift() <= upper.shift())
    limit = upper
    reason = _fmt("Ausbruch: Close {c:.6g} > oberes BB {ub:.6g}, Volumen {v:.1f}x Schnitt; Limit am Band (Retest)",
                  c=c[entry], ub=upper[entry], v=(df["volume"] / vavg)[entry])
    return _signals(entry, limit, a, tp_atr, sl_atr, min_tp, max_bars, reason)


def ema_momentum(df: pd.DataFrame, fast=9, slow=21, trend=100, tp_atr=2.5, sl_atr=1.5, max_bars=48, min_tp=0.01) -> Signals:
    """Trend-following: fast EMA crosses above slow EMA while price is above the
    long trend EMA. Limit at the fast EMA (small pullback)."""
    c = df["close"]
    ef, es, et = ema(c, fast), ema(c, slow), ema(c, trend)
    a = atr(df)
    cross = (ef > es) & (ef.shift() <= es.shift())
    entry = cross & (c > et)
    limit = np.minimum(c, ef)
    reason = _fmt("EMA{f} kreuzt EMA{s} nach oben ({ef:.6g}>{es:.6g}), Preis über EMA{t} ({et:.6g}): Aufwärtsmomentum",
                  f=fast, s=slow,
                  t=trend, ef=ef[entry], es=es[entry], et=et[entry])
    return _signals(entry, limit, a, tp_atr, sl_atr, min_tp, max_bars, reason)


def donchian_breakout(df: pd.DataFrame, n=48, tp_atr=3.0, sl_atr=1.5, max_bars=48, min_tp=0.01) -> Signals:
    """Channel breakout: close above the highest high of the previous n bars.
    Limit at the old channel high."""
    c = df["close"]
    hh = df["high"].rolling(n).max().shift()
    a = atr(df)
    entry = (c > hh) & (c.shift() <= hh.shift())
    limit = hh
    reason = _fmt("Donchian-Ausbruch: Close {c:.6g} > {n}-Bar-Hoch {hh:.6g}; Limit am alten Hoch",
                  c=c[entry], n=n, hh=hh[entry])
    return _signals(entry, limit, a, tp_atr, sl_atr, min_tp, max_bars, reason)


def dip_in_uptrend(df: pd.DataFrame, lookback=6, drop_atr=2.5, trend=200, tp_atr=2.0, sl_atr=2.5, max_bars=24, min_tp=0.01) -> Signals:
    """Buy sharp dips inside an uptrend: price fell > drop_atr * ATR over
    `lookback` bars while the trend EMA is rising and price is still above it."""
    c = df["close"]
    a = atr(df)
    et = ema(c, trend)
    drop = c.shift(lookback) - c
    entry = (drop > drop_atr * a) & (c > et) & (et > et.shift(lookback))
    limit = c - 0.5 * a
    reason = _fmt("Dip im Aufwärtstrend: -{d:.6g} in {lb} Bars (= {x:.1f} ATR), Preis über steigender EMA{t}",
                  d=drop[entry], lb=lookback, x=(drop / a)[entry],
                  t=trend)
    return _signals(entry, limit, a, tp_atr, sl_atr, min_tp, max_bars, reason)


STRATEGIES = {
    "rsi_reversion": rsi_reversion,
    "bb_breakout": bb_breakout,
    "ema_momentum": ema_momentum,
    "donchian_breakout": donchian_breakout,
    "dip_in_uptrend": dip_in_uptrend,
}

# Parameter grids searched by the backtester. Deliberately small to limit
# overfitting (every combination is also validated out-of-sample).
GRIDS = {
    "rsi_reversion": dict(rsi_n=[7, 14], rsi_lo=[15, 25], tp_atr=[1.0, 2.0], sl_atr=[2.0, 3.0]),
    "bb_breakout": dict(bb_k=[2.0, 2.5], vol_mult=[1.5, 2.5], tp_atr=[2.0, 4.0], sl_atr=[1.0, 2.0]),
    "ema_momentum": dict(fast=[9, 12], slow=[21, 50], tp_atr=[2.0, 4.0], sl_atr=[1.5, 2.5]),
    "donchian_breakout": dict(n=[24, 96], tp_atr=[2.0, 4.0], sl_atr=[1.0, 2.0]),
    "dip_in_uptrend": dict(lookback=[3, 12], drop_atr=[2.0, 3.0], tp_atr=[1.5, 3.0], sl_atr=[2.0, 3.0]),
}


MIN_TP_GRID = [0.006, 0.012, 0.02]


def param_grid(name: str):
    g = dict(GRIDS[name], min_tp=MIN_TP_GRID)
    for values in itertools.product(*g.values()):
        yield dict(zip(g.keys(), values))

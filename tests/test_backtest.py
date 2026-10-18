import pandas as pd
import pytest

from kraken_bot.backtest import simulate
from kraken_bot.strategies import Signals


def make(bars, entry_at=0, limit=100.0, tp=102.0, sl=98.0, max_bars=5):
    idx = pd.date_range("2026-01-01", periods=len(bars), freq="5min", tz="UTC")
    df = pd.DataFrame(bars, columns=["open", "high", "low", "close"], index=idx)
    df["volume"] = 1.0
    e = pd.Series(False, index=idx)
    e.iloc[entry_at] = True
    s = lambda v: pd.Series(v, index=idx)
    return df, Signals(e, s(limit), s(tp), s(sl), max_bars, s(""))


def run(df, sig):
    return simulate(df, sig, "XBTEUR", maker=0.0025, taker=0.004, notional=10.0, ttl=2)


def test_touch_does_not_fill():
    df, sig = make([[101, 101, 101, 101], [101, 101.5, 100.0, 101], [101, 101, 100.0, 101]])
    assert run(df, sig) == []


def test_trade_through_fills_and_tp_is_maker():
    df, sig = make([[101, 101, 101, 101], [101, 101, 99.9, 100.5], [101, 102.5, 100.5, 102]])
    (t,) = run(df, sig)
    assert t.kind == "tp" and t.entry == 100.0 and t.exit == 102.0
    assert t.pnl_eur == pytest.approx(10 / 100 * 102 * (1 - 0.0025) - 10 * 1.0025)


def test_sl_wins_tie_and_pays_taker_and_slippage():
    df, sig = make([[101, 101, 101, 101], [101, 101, 99.9, 100.5], [100, 103, 97, 99]])
    (t,) = run(df, sig)
    assert t.kind == "sl" and t.exit == pytest.approx(98 * (1 - 0.0003))
    assert t.pnl_eur < -0.2


def test_gap_below_stop_fills_at_open():
    df, sig = make([[101, 101, 101, 101], [101, 101, 99.9, 100.5], [95, 96, 94, 95]])
    (t,) = run(df, sig)
    assert t.exit == pytest.approx(95 * (1 - 0.0003))


def test_time_stop():
    flat = [100.5, 100.8, 100.2, 100.5]
    df, sig = make([[101, 101, 101, 101], [101, 101, 99.9, 100.5]] + [flat] * 6, max_bars=3)
    (t,) = run(df, sig)
    assert t.kind == "time" and t.exit_time == df.index[4]


def test_unfilled_order_expires():
    df, sig = make([[101, 101, 101, 101]] + [[101, 102, 100.5, 101]] * 4)
    assert run(df, sig) == []

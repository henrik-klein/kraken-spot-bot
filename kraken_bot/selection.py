"""Run the backtest grid, pick strategies that survive fees in- AND
out-of-sample, simulate them together as a 100 EUR portfolio and write
selected_strategies.json for the live bot.

    uv run python -m kraken_bot.selection
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from . import config
from .backtest import run_grid, simulate, stats
from .data import load_bars
from .strategies import STRATEGIES

MAX_SELECTED = 8


def choose(res: pd.DataFrame) -> pd.DataFrame:
    ok = res[(res.is_n >= config.MIN_TRADES_IS) & (res.oos_n >= config.MIN_TRADES_OOS)
             & (res.is_pnl > 0) & (res.oos_pnl > 0) & (res.is_pf > 1.1) & (res.oos_pf > 1.1)].copy()
    if ok.empty:
        return ok
    # Robustness: the share of this (pair, tf, strategy)'s parameter grid that is
    # profitable out-of-sample. A lone winner in a sea of losers is likely luck.
    grp = res.groupby(["pair", "tf", "strategy"])
    robust = grp.apply(lambda g: float((g.oos_pnl > 0).mean()), include_groups=False).rename("robust")
    ok = ok.join(robust, on=["pair", "tf", "strategy"])
    ok = ok[ok.robust >= 0.5]
    ok["score"] = np.minimum(ok.is_pf, ok.oos_pf) * ok.robust
    ok = ok.sort_values(["score", "oos_pnl"], ascending=False)
    ok = ok.drop_duplicates(["pair", "strategy"])  # one config per pair+strategy
    return ok.head(MAX_SELECTED)


def portfolio(selected: pd.DataFrame, start: str) -> dict:
    """Replay all selected strategies together with 100 EUR, 10 EUR per trade
    and MAX_OPEN_POSITIONS; returns equity stats from `start`."""
    events = []
    for _, r in selected.iterrows():
        df = load_bars(r.pair, config.TIMEFRAMES[r.tf])
        df = df[df.index >= pd.Timestamp(start, tz="UTC")]
        sig = STRATEGIES[r.strategy](df, **json.loads(r.params))
        for t in simulate(df, sig, r.pair):
            events.append((t.entry_time, t.exit_time, t.pnl_eur))
    events.sort()
    eq, open_until, taken, curve = config.HARD_START_CAPITAL_EUR, [], [], []
    for et, xt, pnl in events:
        open_until = [x for x in open_until if x > et]
        if len(open_until) >= config.MAX_OPEN_POSITIONS:
            continue
        open_until.append(xt)
        taken.append((xt, pnl))
    for xt, pnl in sorted(taken):
        eq += pnl
        curve.append(eq)
    c = np.r_[config.HARD_START_CAPITAL_EUR, curve]
    dd = float(np.max((np.maximum.accumulate(c) - c) / np.maximum.accumulate(c)))
    return dict(start=start, trades=len(taken), final_equity=round(float(c[-1]), 2),
                return_pct=round(float(c[-1] / c[0] - 1) * 100, 2), max_drawdown_pct=round(dd * 100, 2))


def main():
    res = run_grid()
    res.to_csv(config.ROOT / "backtest_results.csv", index=False)
    sel = choose(res)
    print(f"\n{len(res)} Konfigurationen getestet, {len(sel)} ausgewählt\n")
    out = {"generated": datetime.now(timezone.utc).isoformat(),
           "fees": {"maker": config.MAKER_FEE, "taker": config.TAKER_FEE},
           "split": {"in_sample": [config.BACKTEST_START, config.IS_END], "out_of_sample_from": config.IS_END},
           "strategies": [], "portfolio": {}}
    if not sel.empty:
        cols = ["pair", "tf", "strategy", "params", "is_n", "is_pnl", "is_pf", "oos_n", "oos_pnl", "oos_pf", "oos_mdd", "robust"]
        print(sel[cols].to_string(index=False))
        for _, r in sel.iterrows():
            out["strategies"].append({
                "pair": r.pair, "tf": r.tf, "strategy": r.strategy, "params": json.loads(r.params),
                "why": (f"{r.strategy} {r.pair} {r.tf}: profitabel nach Gebühren in-sample "
                        f"({r.is_n} Trades, PF {r.is_pf}, {r.is_pnl:+.2f} EUR je 10-EUR-Trades) und "
                        f"out-of-sample ({r.oos_n} Trades, PF {r.oos_pf}, {r.oos_pnl:+.2f} EUR); "
                        f"{r.robust:.0%} des Parametergitters OOS profitabel"),
                "backtest": {k: r[k] for k in ["is_n", "is_pnl", "is_pf", "is_win", "oos_n", "oos_pnl", "oos_pf", "oos_win", "oos_mdd"]},
            })
        out["portfolio"] = {"full": portfolio(sel, config.BACKTEST_START), "oos": portfolio(sel, config.IS_END)}
        print("\nPortfolio:", json.dumps(out["portfolio"], indent=2))
    else:
        print("Keine Strategie hat beide Perioden nach Gebühren bestanden -> Bot handelt nicht.")
    config.SELECTION_FILE.write_text(json.dumps(out, indent=2, default=float))


if __name__ == "__main__":
    main()

"""Print bot status: equity, positions, pending orders, recent decisions.

    uv run python -m kraken_bot.status [--live] [-n 20]
"""
from __future__ import annotations

import argparse
import json

from . import config


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true")
    ap.add_argument("-n", type=int, default=20)
    args = ap.parse_args()
    mode = "live" if args.live else "paper"
    st_path = config.STATE_DIR / f"state_{mode}.json"
    if config.KILL_FILE.exists():
        print("!!! KILL-SWITCH AUSGELÖST:", config.KILL_FILE.read_text().strip())
    if st_path.exists():
        st = json.loads(st_path.read_text())
        print(f"Modus {mode} | Start-Equity {st['start_equity']:.2f} EUR | realisiert {st['realized_pnl']:+.4f} EUR "
              f"| {st['closed_trades']} abgeschlossene Trades | Kill bei "
              f"{st['start_equity'] * (1 - config.HARD_KILL_SWITCH_DRAWDOWN):.2f} EUR")
        for p in st["positions"]:
            print(f"  POS  {p['key']:32s} {p['qty']:.8g} @ {p['entry']:.6g}  TP {p['tp']:.6g}  SL {p['sl']:.6g}")
        for o in st["pending"]:
            print(f"  BUY  {o['key']:32s} {o['qty']:.8g} @ {o['limit']:.6g} (offen)")
    else:
        print(f"Noch kein Zustand für Modus {mode}.")
    log = config.LOG_DIR / ("decisions.jsonl" if args.live else "decisions_paper.jsonl")
    if log.exists():
        print("\nLetzte Entscheidungen:")
        for line in log.read_text().splitlines()[-args.n:]:
            r = json.loads(line)
            print(f"  {r['ts']} {r['event']:15s} {r.get('strategy', '')} :: {r['reason']}")


if __name__ == "__main__":
    main()

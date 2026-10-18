"""Configuration. The HARD_* values are safety limits enforced in code paths
that cannot be overridden by config files, CLI flags or environment variables."""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------- hard limits
HARD_MAX_TRADE_EUR = 10.0        # max notional (incl. fee) per single order
HARD_KILL_SWITCH_DRAWDOWN = 0.30  # stop everything at -30 % vs. start equity
HARD_START_CAPITAL_EUR = 100.0   # bot never manages more than this
HARD_AGENT_DAILY_USD = 3.0       # Claude supervisor API spend per UTC day
# Margin and withdrawals are prevented structurally in kraken_client.py
# (endpoint whitelist, forbidden order params).

# ---------------------------------------------------------------- universe
# Most liquid non-stablecoin EUR pairs on Kraken (24h volume, Oct 2026).
PAIRS = ["XBTEUR", "ETHEUR", "SOLEUR", "XRPEUR", "ADAEUR", "LTCEUR"]
TIMEFRAMES = {"5m": "5min", "15m": "15min", "1h": "1h"}

# ---------------------------------------------------------------- fees
# Kraken Pro, lowest volume tier. The live bot refreshes these from
# the TradeVolume endpoint at startup.
MAKER_FEE = 0.0025
TAKER_FEE = 0.0040

# ---------------------------------------------------------------- trading
TRADE_EUR = 10.0                 # target notional per entry (<= HARD_MAX)
MAX_OPEN_POSITIONS = 8           # 8 x 10 EUR, leaves a cash buffer for fees
ENTRY_ORDER_TTL_BARS = 2         # cancel unfilled entry limit after N bars
LOOP_SECONDS = 20

# ---------------------------------------------------------------- backtest
BACKTEST_START = "2026-01-01"
IS_END = "2026-07-01"            # in-sample: Jan-Jun, out-of-sample: Jul-now
MIN_TRADES_IS = 30
MIN_TRADES_OOS = 15

# ---------------------------------------------------------------- claude agent
AGENT_INTERVAL_MIN = 60          # regular review; extra reviews after stop-losses

STATE_DIR = ROOT / "state"
LOG_DIR = ROOT / "logs"
SELECTION_FILE = ROOT / "selected_strategies.json"
KILL_FILE = STATE_DIR / "KILLED"

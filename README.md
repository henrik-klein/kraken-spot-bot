# Kraken Spot Trading Bot

Autonomer Bot für Kraken (Spot, EUR-Paare, 100 € Kapital). Er fährt mehrere kurzfristige
Strategien (5m–1h) parallel, nutzt Post-only-Limit-Orders (Maker) für Einstieg und Take-Profit
und loggt jede Entscheidung mit Begründung. Optional überwacht ein Claude-Agent die Strategien.

## Harte Grenzen (im Code, nicht konfigurierbar)

| Grenze | Wo erzwungen |
|---|---|
| max. 10 € pro Order (inkl. Gebühr) | `KrakenClient.check_order` vor jedem Versand, auch im Paper-Modus |
| kein Margin | `leverage`/`reduce_only`/`margin`-Parameter werfen `SafetyViolation`; nur `limit`/`market`/`stop-loss` |
| keine Withdrawals | Private Endpoints nur über Whitelist (`ALLOWED_PRIVATE`); `Withdraw*`, `WalletTransfer` usw. sind nicht aufrufbar |
| Kill-Switch bei −30 % | Bot-Equity ≤ 70 € → alle Bot-Orders storniert, alle Bot-Positionen verkauft, `state/KILLED` geschrieben; Neustart erst nach manuellem Löschen |
| Claude-Agent ≤ 3 $/Tag | Worst-Case-Prüfung vor jedem API-Call, Buchung der echten Kosten aus `usage` (persistiert in `state/agent_budget.json`) |

Eine `SafetyViolation` löst ebenfalls den Kill-Switch aus.
**Zusätzlich empfohlen:** API-Key bei Kraken nur mit folgenden Rechten anlegen:
*Query Funds*, *Query Open Orders & Trades*, *Query Closed Orders & Trades*,
*Create & Modify Orders*, *Cancel/Close Orders*. **Kein** *Withdraw Funds*, **kein** *Deposit*.

## Ablauf

```bash
uv sync

# 1. Historische Daten ab 01.01.2026 (Kraken-Trades -> 1m-Kerzen, fortsetzbar)
uv run python -m kraken_bot.data XBTEUR ETHEUR SOLEUR XRPEUR ADAEUR LTCEUR

# 2. Backtest + Auswahl -> selected_strategies.json, backtest_results.csv
uv run python -m kraken_bot.selection

# 3. Paper-Trading (Standard; echte Marktdaten, simulierte Fills)
uv run python -m kraken_bot.bot

# 4. Live
export KRAKEN_API_KEY=... KRAKEN_API_SECRET=...
uv run python -m kraken_bot.bot --live

# optional: Claude-Supervisor dazuschalten
export ANTHROPIC_API_KEY=...
uv run python -m kraken_bot.bot --live --agent

# Status, Positionen, letzte Entscheidungen
uv run python -m kraken_bot.status --live
```

Tests: `uv run pytest`

## Strategien (long-only, Spot)

| Strategie | Idee |
|---|---|
| `rsi_reversion` | RSI überverkauft + Close unter unterem Bollinger-Band → Rebound |
| `bb_breakout` | Close über oberem Band mit Volumenspitze → Retest des Bands kaufen |
| `ema_momentum` | EMA-Cross nach oben über langem Trend-EMA |
| `donchian_breakout` | Ausbruch über N-Bar-Hoch → Retest kaufen |
| `dip_in_uptrend` | Scharfer Rücksetzer (in ATR) in steigendem Trend |

Ausstiege: Take-Profit als Maker-Limit, Stop-Loss und Zeitstopp als Market-Order (Software-Stop,
geprüft alle 20 s). Kursziele sind mindestens `min_tp` (0,6–2 %) vom Einstieg entfernt, weil
auf 5m die typische Kerzenbewegung (~0,2 %) unter den Round-Trip-Gebühren (~0,5–0,65 %) liegt.

## Strategie-Auswahl (Backtest)

* Daten: Kraken-Trades ab 2026-01-01, eigene Aggregation zu 5m/15m/1h.
* Gebühren: Maker 0,25 %, Taker 0,40 % (niedrigste Kraken-Pro-Stufe), Slippage bei Market-Orders je Paar.
* Fill-Modell konservativ: Limit füllt nur, wenn der Preis **durch** das Limit handelt; SL vor TP im selben Bar; Gaps füllen zum Open.
* In-Sample Jan–Jun 2026, Out-of-Sample ab Jul 2026. Ausgewählt wird nur, was in **beiden** Perioden nach Gebühren
  profitabel ist (PF > 1,1, Mindestanzahl Trades) und dessen Parameterraum überwiegend OOS-profitabel ist.
* Besteht keine Strategie, handelt der Bot nicht.

## Claude-Supervisor (`--agent`)

Claude Opus 5.5 (`claude-opus-5-5`) prüft stündlich, zusätzlich nach jedem Stop-Loss (höchstens alle 15 min), einen Snapshot:
Equity, Live- vs. Backtest-Performance je Strategie, offene Positionen, Tagesbewegung/Spread je Paar,
die letzten Entscheidungen und die eigenen Notizen der letzten Prüfungen.
Er kann ausschließlich:

* Strategien pausieren und fortsetzen (nur aus der Backtest-Auswahl)
* Risk-off: alle neuen Einstiege blockieren
* Positionsgröße einer Strategie auf 25–100 % verkleinern
* eine offene Position vorzeitig schließen

Es gibt kein Werkzeug für Limits, Konfiguration, Keys oder Orders. Aktionen werden validiert,
vom Bot im Hauptthread angewendet und mit Claudes Begründung geloggt (`agent_action`, `agent_review`).
Bei Ablehnungen durch Sicherheitsfilter springt serverseitig ein Fallback-Modell ein (`fallbacks: "default"`).
Die Kosten werden deshalb konservativ zu 5 $/25 $ pro MTok verbucht.
**Abschalten:** ohne `--agent` starten oder zur Laufzeit `touch state/AGENT_OFF`.

## Betrieb

Stop-Loss und Zeitstopp sind Software-Stops: Sie greifen nur, solange der Bot läuft. Auf dem Mac deshalb
mit `caffeinate -i uv run python -m kraken_bot.bot --live` starten (verhindert Ruhezustand), besser
dauerhaft auf einem kleinen Server. Nach Neustarts übernimmt der Bot Positionen und Orders aus `state/`.

## Logs

* `logs/decisions.jsonl` (live) / `logs/decisions_paper.jsonl`: eine JSON-Zeile pro Entscheidung mit `event` und `reason`
* `state/state_live.json`: Positionen, offene Orders, realisierter PnL, Agent-Steuerung (Neustart-fest)

## Risiken

Kurzfristiger Handel mit 10-€-Orders ist gebührenintensiv; ein Backtest ist keine Garantie.
Mit 100 € Kapital und −30 % Kill-Switch beträgt der realistisch erwartbare Maximalverlust etwa 30 €
plus Slippage beim Notverkauf.

## Ergebnis des Backtests (Stand 19.10.2026)

3.888 Kombinationen (5 Strategien × 6 Paare × 3 Zeitrahmen × Parameter) auf Kraken-Daten 01.01.–18.10.2026:
**keine besteht** (in- und out-of-sample nach Gebühren profitabel mit ≥ 30/15 Trades).
5m und 15m sind praktisch durchgehend negativ (Gebühren > Edge), 1h knapp um null.
31 Kombinationen sind in beiden Perioden positiv, alle mit nur 6–14 Trades pro Periode (statistisch nicht belastbar).
`selected_strategies.json` ist daher leer, der Bot startet, loggt das und handelt nicht.

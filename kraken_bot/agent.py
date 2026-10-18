"""Optional Claude supervisor for the trading bot.

Runs in a background thread. Periodically it sends Claude a snapshot of the
bot (equity, per-strategy live vs. backtest performance, open positions,
market moves, recent decisions) and lets it steer through a small set of
tools. Its powers are deliberately narrow:

  * pause / resume a strategy from the backtest selection (cannot add new ones)
  * risk-off switch: block all new entries
  * shrink a strategy's trade size (factor 0.25-1.0, never above TRADE_EUR)
  * close a strategy's open position early

There is no tool that touches config, hard limits, the kill switch, API keys
or the order path; actions are queued and applied by the bot, whose
KrakenClient.check_order and kill switch stay in force regardless.

Cost: HARD_AGENT_DAILY_USD is checked against a worst-case estimate BEFORE
every request and the real cost from `usage` is booked after it.

Switch off: start the bot without --agent, or create state/AGENT_OFF at runtime.
"""
from __future__ import annotations

import json
import queue
import threading
import time
from datetime import datetime, timezone

import anthropic

from . import config

MODEL = "claude-opus-5-5"
MAX_TOKENS = 4000
MAX_CALLS_PER_REVIEW = 4
# Conservative accounting: priced at the most expensive model a server-side
# refusal fallback could route to ($5 / $25 per MTok; cache write 1.25x).
USD_IN, USD_OUT = 5e-6, 25e-6
USD_CACHE_WRITE, USD_CACHE_READ = 6.25e-6, 0.5e-6

SYSTEM = """Du überwachst einen autonomen Krypto-Trading-Bot auf Kraken (Spot, EUR-Paare, 100 EUR Kapital,
10 EUR pro Trade, Gebühren Maker ~0,25 % / Taker ~0,40 %). Die Strategien wurden per Backtest
(Jan-Jun 2026 in-sample, ab Jul 2026 out-of-sample, inkl. Gebühren) ausgewählt.

Deine Aufgabe: Bei jeder Überprüfung den Snapshot lesen und entscheiden, ob du eingreifen musst.
Typische Gründe zum Eingreifen:
- Eine Strategie weicht live deutlich und über genügend Trades negativ von ihrem Backtest ab -> pausieren.
- Das Marktregime passt offensichtlich nicht zur Strategie (z. B. Mean-Reversion im Crash) -> pausieren
  oder Größe reduzieren; später wieder fortsetzen, wenn sich das Regime normalisiert.
- Equity nähert sich dem Kill-Switch -> Risiko herunterfahren (risk-off oder kleinere Größe).
- Eine offene Position hat ihre These klar verloren und der Stop ist weit weg -> vorzeitig schließen.

Prinzipien: Wenige Trades sind statistisch wenig aussagekräftig, also nicht auf Rauschen reagieren.
Jeder Eingriff kostet Gebühren oder entgangene Chancen. Nichts zu tun ist oft richtig.
Harte Limits (max. 10 EUR pro Trade, kein Margin, Kill-Switch bei -30 %, keine Withdrawals) kannst
und sollst du nicht ändern; dafür gibt es keine Werkzeuge. Jede Aktion braucht eine konkrete,
datenbasierte Begründung auf Deutsch. Schließe mit einer kurzen Zusammenfassung (2-4 Sätze) deiner
Einschätzung ab; sie wird geloggt und dir beim nächsten Mal wieder gezeigt."""


def _tools(keys: list[str]) -> list[dict]:
    key = {"type": "string", "enum": keys, "description": "Strategie-Schlüssel strategy|pair|tf"}
    reason = {"type": "string", "description": "Datenbasierte Begründung (Deutsch)"}

    def tool(name, desc, props):
        return {"name": name, "description": desc, "strict": True,
                "input_schema": {"type": "object", "properties": {**props, "reason": reason},
                                 "required": [*props, "reason"], "additionalProperties": False}}

    return [
        tool("pause_strategy", "Keine neuen Einstiege mehr für diese Strategie. Offene Position läuft regulär weiter.", {"key": key}),
        tool("resume_strategy", "Pausierte Strategie wieder aktivieren.", {"key": key}),
        tool("set_risk_off", "Globaler Schalter: true blockiert alle neuen Einstiege, false hebt das auf.",
             {"enabled": {"type": "boolean"}}),
        tool("set_size_factor", "Positionsgröße einer Strategie skalieren: 0.25-1.0 x 10 EUR (nur verkleinern).",
             {"key": key, "factor": {"type": "number"}}),
        tool("close_position", "Offene Position dieser Strategie sofort per Market-Order schließen (Taker-Gebühr!).", {"key": key}),
    ]


def validate_action(name: str, args: dict, keys: list[str]) -> str | None:
    """Returns an error message, or None if the action is allowed."""
    if name not in {"pause_strategy", "resume_strategy", "set_risk_off", "set_size_factor", "close_position"}:
        return f"unbekanntes Werkzeug {name}"
    if name != "set_risk_off" and args.get("key") not in keys:
        return f"unbekannte Strategie {args.get('key')}"
    if name == "set_risk_off" and not isinstance(args.get("enabled"), bool):
        return "enabled muss true/false sein"
    if name == "set_size_factor":
        f = args.get("factor")
        if not isinstance(f, (int, float)) or not 0.25 <= f <= 1.0:
            return "factor muss zwischen 0.25 und 1.0 liegen (Größe kann nur verkleinert werden)"
    if not str(args.get("reason", "")).strip():
        return "Begründung fehlt"
    return None


class Budget:
    """Daily USD ledger, persisted so restarts cannot reset it."""

    def __init__(self, path=None):
        self.path = path or config.STATE_DIR / "agent_budget.json"
        self.lock = threading.Lock()

    def _load(self) -> dict:
        return json.loads(self.path.read_text()) if self.path.exists() else {}

    @staticmethod
    def today() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def spent(self) -> float:
        return self._load().get(self.today(), 0.0)

    def allows(self, worst_case_usd: float) -> bool:
        return self.spent() + worst_case_usd <= config.HARD_AGENT_DAILY_USD

    def book(self, usd: float):
        with self.lock:
            d = self._load()
            d[self.today()] = d.get(self.today(), 0.0) + usd
            self.path.parent.mkdir(exist_ok=True)
            self.path.write_text(json.dumps(d, indent=1))


def usage_cost(u) -> float:
    return ((u.input_tokens or 0) * USD_IN + (u.output_tokens or 0) * USD_OUT
            + (getattr(u, "cache_creation_input_tokens", 0) or 0) * USD_CACHE_WRITE
            + (getattr(u, "cache_read_input_tokens", 0) or 0) * USD_CACHE_READ)


def worst_case_cost(request: dict) -> float:
    # ~2.5 chars per token over-estimates for German/JSON text; every input
    # token priced as a cache write, plus the full output allowance.
    est_in = len(json.dumps(request, default=str, ensure_ascii=False)) / 2.5
    return est_in * USD_CACHE_WRITE + MAX_TOKENS * USD_OUT


class Supervisor:
    def __init__(self, keys: list[str], snapshot_fn, log, interval_min: float = config.AGENT_INTERVAL_MIN,
                 client: anthropic.Anthropic | None = None, budget: Budget | None = None):
        self.keys = keys
        self.snapshot_fn = snapshot_fn      # thread-safe callable -> dict
        self.log = log
        self.interval = interval_min * 60
        self.client = client or anthropic.Anthropic(max_retries=0)  # retries could bill twice
        self.budget = budget or Budget()
        self.actions: queue.Queue = queue.Queue()
        self.tools = _tools(keys)
        self.notes: list[str] = []
        self.wake = threading.Event()
        self.stopped = threading.Event()
        self.last_run = 0.0

    # -------------------------------------------------------------- thread
    def start(self):
        threading.Thread(target=self._loop, name="claude-supervisor", daemon=True).start()

    def trigger(self, why: str):
        """Ask for an early review (e.g. after a stop-loss); at most every 15 min."""
        if time.time() - self.last_run > 15 * 60:
            self.wake_reason = why
            self.wake.set()

    def _loop(self):
        self.wake_reason = "Start"
        while not self.stopped.is_set():
            if (config.STATE_DIR / "AGENT_OFF").exists():
                self.stopped.wait(60)
                continue
            reason, self.wake_reason = self.wake_reason or "Intervall", None
            try:
                self.review(reason)
            except Exception as e:
                self.log("agent_error", f"Claude-Überprüfung fehlgeschlagen: {e}")
            self.wake.clear()
            self.wake.wait(self.interval)

    # -------------------------------------------------------------- review
    def review(self, trigger: str = "manuell"):
        self.last_run = time.time()
        snap = self.snapshot_fn()
        snap["agent_budget"] = {"spent_today_usd": round(self.budget.spent(), 4),
                                "daily_cap_usd": config.HARD_AGENT_DAILY_USD}
        snap["your_previous_notes"] = self.notes[-3:]
        messages = [{"role": "user", "content": f"Anlass: {trigger}\n\nSnapshot:\n"
                     + json.dumps(snap, ensure_ascii=False, default=str, indent=1)}]
        cost_total, applied = 0.0, []
        for _ in range(MAX_CALLS_PER_REVIEW):
            request = dict(model=MODEL, max_tokens=MAX_TOKENS, system=SYSTEM, tools=self.tools,
                           messages=messages, output_config={"effort": "low"},
                           cache_control={"type": "ephemeral"})
            worst = worst_case_cost(request)
            if not self.budget.allows(worst):
                self.log("agent_budget", f"Tagesbudget {config.HARD_AGENT_DAILY_USD} $ würde überschritten "
                         f"(heute {self.budget.spent():.3f} $, nächster Call bis {worst:.3f} $) -> Überprüfung übersprungen")
                break
            resp = self.client.beta.messages.create(
                **request, betas=["server-side-fallback-2026-07-01"], fallbacks="default")
            cost = usage_cost(resp.usage)
            self.budget.book(cost)
            cost_total += cost
            if resp.stop_reason == "refusal":
                self.log("agent_error", "Claude hat die Überprüfung abgelehnt; keine Aktionen")
                break
            messages.append({"role": "assistant", "content": resp.content})
            calls = [b for b in resp.content if b.type == "tool_use"]
            if not calls or resp.stop_reason != "tool_use":
                text = " ".join(b.text for b in resp.content if b.type == "text").strip()
                if text:
                    self.notes.append(f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M} {text}")
                    self.notes = self.notes[-10:]
                self.log("agent_review", text or "(keine Zusammenfassung)", trigger=trigger,
                         actions=applied, cost_usd=round(cost_total, 4),
                         spent_today_usd=round(self.budget.spent(), 4))
                return
            results = []
            for b in calls:
                err = validate_action(b.name, b.input, self.keys)
                if err:
                    results.append({"type": "tool_result", "tool_use_id": b.id, "content": f"Abgelehnt: {err}",
                                    "is_error": True})
                    continue
                self.actions.put({"action": b.name, **b.input})
                applied.append(b.name)
                results.append({"type": "tool_result", "tool_use_id": b.id,
                                "content": "Angenommen; der Bot wendet die Aktion im nächsten Zyklus an."})
            messages.append({"role": "user", "content": results})
        self.log("agent_review", "Überprüfung ohne Abschlusszusammenfassung beendet", trigger=trigger,
                 actions=applied, cost_usd=round(cost_total, 4), spent_today_usd=round(self.budget.spent(), 4))

    def drain(self) -> list[dict]:
        out = []
        while True:
            try:
                out.append(self.actions.get_nowait())
            except queue.Empty:
                return out

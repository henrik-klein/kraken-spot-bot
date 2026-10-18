"""Live / paper trading loop.

    uv run python -m kraken_bot.bot            # paper trading (default)
    uv run python -m kraken_bot.bot --live     # real money, needs API keys

Every decision (signal, skip, order, fill, exit, kill switch) is appended to
logs/decisions.jsonl with a German `reason` field.
"""
from __future__ import annotations

import argparse
import copy
import json
import math
import signal
import sys
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from . import config
from .kraken_client import KrakenClient, KrakenError, SafetyViolation
from .strategies import STRATEGIES

TF_MINUTES = {"5m": 5, "15m": 15, "1h": 60}
USERREF_BASE = 7_310_000  # tags all bot orders; userref = base + strategy index


def now() -> datetime:
    return datetime.now(timezone.utc)


# ====================================================================== logging
class DecisionLog:
    def __init__(self, mode: str):
        config.LOG_DIR.mkdir(exist_ok=True)
        self.path = config.LOG_DIR / ("decisions.jsonl" if mode == "live" else "decisions_paper.jsonl")
        self.mode = mode

    def __call__(self, event: str, reason: str, **data):
        rec = {"ts": now().isoformat(timespec="seconds"), "mode": self.mode, "event": event, "reason": reason, **data}
        with self.path.open("a") as f:
            f.write(json.dumps(rec, default=str, ensure_ascii=False) + "\n")
        extra = " ".join(f"{k}={v}" for k, v in data.items() if k not in ("why",))
        print(f"{rec['ts']} [{event}] {reason} {extra}", flush=True)


# ====================================================================== paper exchange
class PaperExchange:
    """Simulates fills against real Kraken 1m candles with the same
    trade-through rule as the backtest. Balances start at 100 EUR."""

    def __init__(self, client: KrakenClient):
        self.client = client
        self.orders: dict[str, dict] = {}
        self.n = 0

    def add_order(self, pair, side, ordertype, volume, ref_price, price=None, post_only=False, userref=None):
        KrakenClient.check_order({"pair": pair, "type": side, "ordertype": ordertype,
                                  "volume": f"{volume:.8f}"}, ref_price)
        if post_only and price is not None:
            t = self.client.ticker([pair])
            tk = next(iter(t.values()))
            ask, bid = float(tk["a"][0]), float(tk["b"][0])
            if (side == "buy" and price >= ask) or (side == "sell" and price <= bid):
                self.n += 1
                txid = f"PAPER-{self.n}"
                self.orders[txid] = dict(status="canceled", reason="Post only order", vol_exec="0", cost="0",
                                         fee="0", descr={"pair": pair, "type": side}, vol=volume, price=price)
                return {"txid": [txid]}
        self.n += 1
        txid = f"PAPER-{self.n}"
        o = dict(status="open", vol=volume, vol_exec="0", cost="0", fee="0", price=price, side=side, pair=pair,
                 ordertype=ordertype, opentm=time.time(), userref=userref, descr={"pair": pair, "type": side})
        self.orders[txid] = o
        if ordertype == "market":
            tk = next(iter(self.client.ticker([pair]).values()))
            px = float(tk["b"][0]) if side == "sell" else float(tk["a"][0])
            self._fill(o, px, config.TAKER_FEE)
        return {"txid": [txid]}

    def _fill(self, o, px, fee):
        o.update(status="closed", vol_exec=str(o["vol"]), cost=str(o["vol"] * px), fee=str(o["vol"] * px * fee), price=px)

    def query_orders(self, txids):
        for txid in txids:
            o = self.orders.get(txid)
            if not o or o["status"] != "open":
                continue
            bars = self.client.ohlc_since(o["pair"], 1, int(o["opentm"]) - 60)
            for b in bars:
                if float(b[0]) < o["opentm"]:
                    continue  # only minutes that started after the order was placed
                lo, hi = float(b[3]), float(b[2])
                if (o["side"] == "buy" and lo < o["price"]) or (o["side"] == "sell" and hi > o["price"]):
                    self._fill(o, o["price"], config.MAKER_FEE)
                    break
        return {t: self.orders[t] for t in txids if t in self.orders}

    def cancel_order(self, txid):
        o = self.orders.get(txid)
        if o and o["status"] == "open":
            o["status"] = "canceled"
        return {"count": 1}

    def balance(self):
        return None  # paper mode: no real balances


# ====================================================================== bot
class Bot:
    def __init__(self, live: bool, selection=config.SELECTION_FILE, agent: bool = False):
        self.live = live
        self.mode = "live" if live else "paper"
        self.client = KrakenClient()
        self.ex = self.client if live else PaperExchange(self.client)
        self.log = DecisionLog(self.mode)
        config.STATE_DIR.mkdir(exist_ok=True)
        self.state_path = config.STATE_DIR / f"state_{self.mode}.json"
        self.state = self._load_state()
        sel = json.loads(Path(selection).read_text())
        self.combos = sel["strategies"]
        for i, c in enumerate(self.combos):
            c["key"] = f"{c['strategy']}|{c['pair']}|{c['tf']}"
            c["userref"] = USERREF_BASE + i
        self.pairs = sorted({c["pair"] for c in self.combos})
        self.pair_info = {}
        self.stop = False
        self.state.setdefault("controls", {"paused": {}, "risk_off": None, "size": {}})
        self.state.setdefault("stats", {})
        self._snap, self._snap_lock = {}, threading.Lock()
        self.supervisor = None
        if agent:
            from .agent import Supervisor
            self.supervisor = Supervisor([c["key"] for c in self.combos], self.snapshot, self.log)

    # ------------------------------------------------------------ state
    def _load_state(self) -> dict:
        if self.state_path.exists():
            return json.loads(self.state_path.read_text())
        return {"start_equity": config.HARD_START_CAPITAL_EUR, "realized_pnl": 0.0, "pending": [],
                "positions": [], "last_bar": {}, "closed_trades": 0, "started": now().isoformat()}

    def save(self):
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=1, default=str))
        tmp.replace(self.state_path)

    # ------------------------------------------------------------ setup
    def setup(self):
        if config.KILL_FILE.exists():
            sys.exit(f"Kill-Switch war ausgelöst ({config.KILL_FILE.read_text().strip()}). "
                     f"Datei {config.KILL_FILE} manuell löschen, um neu zu starten.")
        if not self.combos:
            self.log("idle", "Keine Strategie hat den Backtest nach Gebühren bestanden; Bot handelt nicht.")
            sys.exit(0)
        info = self.client.asset_pairs(self.pairs)
        for k, v in info.items():
            alt = v["altname"]
            self.pair_info[alt] = dict(key=k, base=v["base"], lot=int(v["lot_decimals"]), dec=int(v["pair_decimals"]),
                                       ordermin=float(v["ordermin"]), costmin=float(v.get("costmin", 0) or 0))
        if self.live:
            vol = self.client.trade_volume(self.pairs[0])
            fees = vol.get("fees", {})
            maker = vol.get("fees_maker", {})
            if fees:
                config.TAKER_FEE = float(next(iter(fees.values()))["fee"]) / 100
            if maker:
                config.MAKER_FEE = float(next(iter(maker.values()))["fee"]) / 100
            bal = self.client.balance()
            eur = bal.get("ZEUR", 0.0)
            self.log("startup", "Live-Modus: Kontostand geprüft, Gebühren von Kraken geladen",
                     eur_balance=round(eur, 2), maker=config.MAKER_FEE, taker=config.TAKER_FEE)
            if eur < config.TRADE_EUR and not self.state["positions"]:
                sys.exit(f"Zu wenig EUR auf dem Konto ({eur:.2f}).")
        self.log("startup", f"{len(self.combos)} Strategien aktiv, Kill-Switch bei Equity <= "
                 f"{self.kill_level():.2f} EUR", strategies=[c["key"] for c in self.combos])

    def kill_level(self) -> float:
        return self.state["start_equity"] * (1 - config.HARD_KILL_SWITCH_DRAWDOWN)

    # ------------------------------------------------------------ helpers
    def round_qty(self, pair, q):
        d = self.pair_info[pair]["lot"]
        return math.floor(q * 10 ** d) / 10 ** d

    def round_px(self, pair, p, up=False):
        d = self.pair_info[pair]["dec"]
        f = math.ceil if up else math.floor
        return f(p * 10 ** d) / 10 ** d

    def tickers(self) -> dict:
        t = self.client.ticker(self.pairs)
        out = {}
        for k, v in t.items():
            alt = next(a for a, i in self.pair_info.items() if i["key"] == k or a == k)
            out[alt] = dict(bid=float(v["b"][0]), ask=float(v["a"][0]), last=float(v["c"][0]), open=float(v["o"]))
        return out

    def bars(self, pair, tf) -> pd.DataFrame:
        raw = self.client.ohlc(pair, TF_MINUTES[tf])
        df = pd.DataFrame(raw, columns=["time", "open", "high", "low", "close", "vwap", "volume", "count"])
        df.index = pd.to_datetime(df["time"].astype(int), unit="s", utc=True)
        df = df[["open", "high", "low", "close", "volume"]].astype(float)
        return df.iloc[:-1]  # last candle is still forming

    def equity(self, tk) -> float:
        unreal = sum(p["qty"] * tk[p["pair"]]["bid"] * (1 - config.TAKER_FEE) - p["cost"] for p in self.state["positions"])
        return self.state["start_equity"] + self.state["realized_pnl"] + unreal

    def committed(self) -> float:
        return sum(p["cost"] for p in self.state["positions"]) + sum(o["notional"] for o in self.state["pending"])

    # ------------------------------------------------------------ main loop
    def run(self):
        self.setup()
        if self.supervisor:
            self.tick_snapshot_only()
            self.supervisor.start()
            self.log("startup", f"Claude-Supervisor aktiv (alle {config.AGENT_INTERVAL_MIN} min, "
                     f"max. {config.HARD_AGENT_DAILY_USD} $/Tag; abschalten: Datei state/AGENT_OFF anlegen)")
        signal.signal(signal.SIGTERM, lambda *a: setattr(self, "stop", True))
        while not self.stop:
            try:
                self.tick()
            except SafetyViolation as e:
                self.log("safety_violation", f"Harte Grenze hätte verletzt werden können: {e}. Bot stoppt.")
                self.kill(f"SafetyViolation: {e}")
                raise
            except KeyboardInterrupt:
                break
            except Exception as e:
                self.log("error", f"Fehler im Loop, nächster Versuch in {config.LOOP_SECONDS}s: {e}",
                         trace=traceback.format_exc(limit=3))
            time.sleep(config.LOOP_SECONDS)
        self.log("shutdown", "Bot beendet (offene Positionen und Orders bleiben bestehen, werden beim Neustart übernommen)")

    def tick(self):
        tk = self.tickers()
        eq = self.equity(tk)
        if eq <= self.kill_level():
            self.kill(f"Equity {eq:.2f} EUR <= {self.kill_level():.2f} EUR (-30 %)", tk)
            return
        if self.supervisor:
            self.apply_agent_actions(tk)
        self.manage_pending()
        self.manage_positions(tk)
        self.scan_signals(tk, eq)
        self.update_snapshot(tk)
        self.save()

    # ------------------------------------------------------------ entries
    def scan_signals(self, tk, eq):
        busy = {p["key"] for p in self.state["positions"]} | {o["key"] for o in self.state["pending"]}
        cache = {}
        for c in self.combos:
            pair, tf = c["pair"], c["tf"]
            step = TF_MINUTES[tf] * 60
            expected = datetime.fromtimestamp((time.time() // step - 1) * step, timezone.utc).isoformat()
            if self.state["last_bar"].get(c["key"]) == expected:
                continue  # no new closed bar since last evaluation
            if (pair, tf) not in cache:
                cache[(pair, tf)] = self.bars(pair, tf)
            df = cache[(pair, tf)]
            bar_t = df.index[-1].isoformat()
            if self.state["last_bar"].get(c["key"]) == bar_t:
                continue
            self.state["last_bar"][c["key"]] = bar_t
            sig = STRATEGIES[c["strategy"]](df, **c["params"])
            if not bool(sig.entry.iloc[-1]):
                continue
            why = sig.reason.iloc[-1]
            base = dict(strategy=c["key"], bar=bar_t)
            ctl = self.state["controls"]
            if c["key"] in ctl["paused"] or ctl["risk_off"]:
                self.log("skip", f"Signal ignoriert: {'Strategie pausiert' if c['key'] in ctl['paused'] else 'Risk-off aktiv'} "
                         f"({ctl['paused'].get(c['key']) or ctl['risk_off']}). Signal: {why}", **base)
                continue
            if c["key"] in busy:
                self.log("skip", f"Signal ignoriert: Strategie hat bereits Position/Order. Signal: {why}", **base)
                continue
            n_open = len(self.state["positions"]) + len(self.state["pending"])
            if n_open >= config.MAX_OPEN_POSITIONS:
                self.log("skip", f"Signal ignoriert: max. {config.MAX_OPEN_POSITIONS} gleichzeitige Positionen erreicht. Signal: {why}", **base)
                continue
            free = eq - self.committed()
            factor = min(1.0, max(0.25, float(self.state["controls"]["size"].get(c["key"], 1.0))))
            notional = min(config.TRADE_EUR, config.HARD_MAX_TRADE_EUR) * factor / (1 + config.TAKER_FEE)
            if free < notional * (1 + config.MAKER_FEE):
                self.log("skip", f"Signal ignoriert: freies Budget {free:.2f} EUR reicht nicht. Signal: {why}", **base)
                continue
            limit = float(sig.limit.iloc[-1])
            tp, sl = float(sig.tp.iloc[-1]), float(sig.sl.iloc[-1])
            ask, bid = tk[pair]["ask"], tk[pair]["bid"]
            note = ""
            if limit >= ask:
                shift = limit - bid
                limit, tp, sl = bid, tp - shift, sl - shift
                note = " Limit lag über dem Ask -> auf Bid gesetzt (bleibt Maker)."
            limit = self.round_px(pair, limit)
            qty = self.round_qty(pair, notional / limit)
            pi = self.pair_info[pair]
            if qty < pi["ordermin"] or qty * limit < pi["costmin"]:
                self.log("skip", f"Ordergröße {qty} unter Kraken-Minimum {pi['ordermin']}", **base)
                continue
            if self.live:
                eur = self.client.balance().get("ZEUR", 0.0)
                if eur < qty * limit * (1 + config.MAKER_FEE):
                    self.log("skip", f"Signal ignoriert: nur {eur:.2f} EUR auf dem Konto", **base)
                    continue
            try:
                res = self.ex.add_order(pair, "buy", "limit", qty, ref_price=limit, price=limit, post_only=True,
                                        userref=c["userref"])
            except KrakenError as e:
                self.log("skip", f"Kauforder von Kraken abgelehnt: {e}. Signal: {why}", **base)
                continue
            txid = res["txid"][0]
            ttl = config.ENTRY_ORDER_TTL_BARS * TF_MINUTES[tf] * 60
            self.state["pending"].append(dict(txid=txid, key=c["key"], pair=pair, qty=qty, limit=limit,
                                              notional=qty * limit, tp=tp, sl=sl, max_bars=sig.max_bars, tf=tf,
                                              expires=time.time() + ttl, why=why))
            busy.add(c["key"])
            self.log("entry_order", f"KAUF-Limit (post-only) platziert. Begründung: {why}.{note} "
                     f"Ziel {tp:.6g}, Stop {sl:.6g}, Zeitstopp {sig.max_bars} Bars",
                     txid=txid, pair=pair, qty=qty, limit=limit, eur=round(qty * limit, 2), **base)

    def manage_pending(self):
        if not self.state["pending"]:
            return
        info = self.ex.query_orders([o["txid"] for o in self.state["pending"]])
        keep = []
        for o in self.state["pending"]:
            q = info.get(o["txid"], {})
            status, filled = q.get("status"), float(q.get("vol_exec", 0) or 0)
            if status == "closed" or (filled > 0 and status == "canceled"):
                self.open_position(o, filled, float(q["cost"]), float(q["fee"]))
            elif status in ("canceled", "expired"):
                self.log("entry_canceled", f"Kauforder nicht ausgeführt ({q.get('reason') or status})",
                         txid=o["txid"], strategy=o["key"])
            elif time.time() > o["expires"]:
                self.ex.cancel_order(o["txid"])
                q = self.ex.query_orders([o["txid"]]).get(o["txid"], {})
                filled = float(q.get("vol_exec", 0) or 0)
                if filled > 0:
                    self.open_position(o, filled, float(q["cost"]), float(q["fee"]))
                else:
                    self.log("entry_expired", f"Kauf-Limit nach {config.ENTRY_ORDER_TTL_BARS} Bars nicht gefüllt -> storniert "
                             "(Preis kam nicht zurück, Setup verfallen)", txid=o["txid"], strategy=o["key"])
            else:
                keep.append(o)
        self.state["pending"] = keep

    def open_position(self, o, qty, cost, fee):
        pair = o["pair"]
        pos = dict(key=o["key"], pair=pair, qty=qty, entry=cost / qty, cost=cost + fee, tp=o["tp"], sl=o["sl"],
                   opened=time.time(), deadline=time.time() + o["max_bars"] * TF_MINUTES[o["tf"]] * 60, tp_txid=None)
        self.state["positions"].append(pos)
        self.log("entry_filled", f"Kauf ausgeführt zu {pos['entry']:.6g} (Gebühr {fee:.4f} EUR)",
                 strategy=o["key"], qty=qty, cost=round(cost + fee, 4))
        self.place_tp(pos)

    def place_tp(self, pos):
        pair = pos["pair"]
        px = self.round_px(pair, pos["tp"], up=True)
        try:
            res = self.ex.add_order(pair, "sell", "limit", pos["qty"], ref_price=px, price=px, post_only=True)
            pos["tp_txid"] = res["txid"][0]
            self.log("tp_order", f"Take-Profit-Limit (Maker) bei {px:.6g} platziert", strategy=pos["key"], txid=pos["tp_txid"])
        except SafetyViolation:
            raise
        except Exception as e:
            self.log("tp_order_failed", f"TP-Order fehlgeschlagen, Position wird per Software überwacht: {e}", strategy=pos["key"])

    # ------------------------------------------------------------ exits
    def manage_positions(self, tk):
        tp_info = self.ex.query_orders([p["tp_txid"] for p in self.state["positions"] if p["tp_txid"]])
        for p in self.state["positions"]:
            q = tp_info.get(p["tp_txid"], {}) if p["tp_txid"] else {}
            if q.get("status") == "closed":
                self.close(p, float(q["cost"]) - float(q["fee"]), "tp", f"Take-Profit gefüllt bei {float(q['price']):.6g}")
                continue
            if p["tp_txid"] and q.get("status") == "canceled":
                p["tp_txid"] = None  # e.g. post-only reject -> retry below
            bid = tk[p["pair"]]["bid"]
            if bid <= p["sl"]:
                self.market_exit(p, "sl", f"Stop-Loss: Bid {bid:.6g} <= Stop {p['sl']:.6g}", tk)
            elif time.time() >= p["deadline"]:
                self.market_exit(p, "time", f"Zeitstopp erreicht, Setup hat sich nicht entwickelt "
                                 f"(Bid {bid:.6g}, Einstieg {p['entry']:.6g})", tk)
            elif not p["tp_txid"]:
                self.place_tp(p)
        self.state["positions"] = [p for p in self.state["positions"] if not p.get("closed")]

    def market_exit(self, p, kind, why, tk=None):
        filled_tp = 0.0
        if p["tp_txid"]:
            self.ex.cancel_order(p["tp_txid"])
            q = self.ex.query_orders([p["tp_txid"]]).get(p["tp_txid"], {})
            filled_tp = float(q.get("vol_exec", 0) or 0)
            if q.get("status") == "closed":
                self.close(p, float(q["cost"]) - float(q["fee"]), "tp", "Take-Profit wurde vor dem Storno gefüllt")
                return
        proceeds = 0.0
        if filled_tp > 0:
            proceeds += float(q["cost"]) - float(q["fee"])
        qty = self.round_qty(p["pair"], p["qty"] - filled_tp)
        if self.live:
            held = self.client.balance().get(self.pair_info[p["pair"]]["base"], 0.0)
            qty = self.round_qty(p["pair"], min(qty, held))
        bid = (tk or self.tickers())[p["pair"]]["bid"]
        # split into chunks below the hard per-order cap
        max_chunk = self.round_qty(p["pair"], config.HARD_MAX_TRADE_EUR / (bid * (1 + config.TAKER_FEE)))
        while qty >= self.pair_info[p["pair"]]["ordermin"]:
            chunk = min(qty, max_chunk)
            res = self.ex.add_order(p["pair"], "sell", "market", chunk, ref_price=bid)
            txid = res["txid"][0]
            q = {}
            for _ in range(10):
                q = self.ex.query_orders([txid]).get(txid, {})
                if q.get("status") == "closed":
                    break
                time.sleep(1)
            proceeds += float(q.get("cost", 0) or 0) - float(q.get("fee", 0) or 0)
            qty = self.round_qty(p["pair"], qty - float(q.get("vol_exec", chunk) or chunk))
        self.close(p, proceeds, kind, why)

    def close(self, p, proceeds, kind, why):
        pnl = proceeds - p["cost"]
        self.state["realized_pnl"] += pnl
        self.state["closed_trades"] += 1
        st = self.state["stats"].setdefault(p["key"], {"n": 0, "pnl": 0.0, "wins": 0})
        st["n"] += 1
        st["pnl"] = round(st["pnl"] + pnl, 4)
        st["wins"] += pnl > 0
        if kind == "sl" and self.supervisor:
            self.supervisor.trigger(f"Stop-Loss bei {p['key']} ({pnl:+.3f} EUR)")
        p["closed"] = True
        self.log("exit", f"{why}. Ergebnis {pnl:+.4f} EUR", strategy=p["key"], kind=kind,
                 pnl=round(pnl, 4), realized_total=round(self.state["realized_pnl"], 4))

    # ------------------------------------------------------------ claude supervisor
    def snapshot(self) -> dict:
        with self._snap_lock:
            return copy.deepcopy(self._snap)

    def tick_snapshot_only(self):
        self.update_snapshot(self.tickers())

    def update_snapshot(self, tk):
        eq = self.equity(tk)
        bt = {c["key"]: c.get("backtest", {}) for c in self.combos}
        pos = {p["key"]: p for p in self.state["positions"]}
        strategies = []
        for c in self.combos:
            k = c["key"]
            row = {"key": k, "params": c["params"], "backtest": bt[k],
                   "live": self.state["stats"].get(k, {"n": 0, "pnl": 0.0, "wins": 0}),
                   "paused": self.state["controls"]["paused"].get(k), "size_factor": self.state["controls"]["size"].get(k, 1.0)}
            if k in pos:
                p, bid = pos[k], tk[pos[k]["pair"]]["bid"]
                row["open_position"] = {"entry": p["entry"], "bid": bid, "tp": p["tp"], "sl": p["sl"],
                                        "unrealized_eur": round(p["qty"] * bid * (1 - config.TAKER_FEE) - p["cost"], 4),
                                        "age_min": round((time.time() - p["opened"]) / 60),
                                        "time_stop_in_min": round((p["deadline"] - time.time()) / 60)}
            strategies.append(row)
        market = {pair: {"last": t["last"], "change_today_pct": round((t["last"] / t["open"] - 1) * 100, 2),
                         "spread_bp": round((t["ask"] - t["bid"]) / t["bid"] * 1e4, 1)} for pair, t in tk.items()}
        recent = []
        if self.log.path.exists():
            for line in self.log.path.read_text().splitlines()[-25:]:
                r = json.loads(line)
                if r["event"] not in ("agent_review",):
                    recent.append({k: r.get(k) for k in ("ts", "event", "strategy", "reason", "pnl") if r.get(k) is not None})
        snap = {"time_utc": now().isoformat(timespec="seconds"), "mode": self.mode,
                "equity_eur": round(eq, 2), "start_equity_eur": self.state["start_equity"],
                "kill_switch_at_eur": round(self.kill_level(), 2), "realized_pnl_eur": round(self.state["realized_pnl"], 4),
                "closed_trades": self.state["closed_trades"], "risk_off": self.state["controls"]["risk_off"],
                "pending_entries": len(self.state["pending"]), "strategies": strategies, "market": market,
                "recent_decisions": recent}
        with self._snap_lock:
            self._snap = snap

    def apply_agent_actions(self, tk):
        ctl = self.state["controls"]
        for a in self.supervisor.drain():
            name, key, why = a["action"], a.get("key"), a["reason"]
            if name == "pause_strategy":
                ctl["paused"][key] = why
            elif name == "resume_strategy":
                ctl["paused"].pop(key, None)
            elif name == "set_risk_off":
                ctl["risk_off"] = why if a["enabled"] else None
            elif name == "set_size_factor":
                ctl["size"][key] = min(1.0, max(0.25, float(a["factor"])))
            elif name == "close_position":
                pos = next((p for p in self.state["positions"] if p["key"] == key), None)
                if pos is None:
                    self.log("agent_action", f"Claude wollte {key} schließen, aber keine offene Position", strategy=key)
                    continue
                self.market_exit(pos, "agent", f"Vorzeitig geschlossen auf Anweisung von Claude: {why}", tk)
                self.state["positions"] = [p for p in self.state["positions"] if not p.get("closed")]
                continue
            self.log("agent_action", f"Claude: {name} – {why}", strategy=key,
                     **({"enabled": a["enabled"]} if "enabled" in a else {}), **({"factor": a["factor"]} if "factor" in a else {}))

    # ------------------------------------------------------------ kill switch
    def kill(self, why, tk=None):
        self.log("kill_switch", f"KILL-SWITCH: {why}. Storniere alle Bot-Orders und verkaufe alle Bot-Positionen.")
        for o in self.state["pending"]:
            try:
                self.ex.cancel_order(o["txid"])
            except Exception as e:
                self.log("error", f"Storno fehlgeschlagen {o['txid']}: {e}")
        self.state["pending"] = []
        for p in list(self.state["positions"]):
            try:
                self.market_exit(p, "kill", "Zwangsverkauf durch Kill-Switch", tk)
            except Exception as e:
                self.log("error", f"Zwangsverkauf fehlgeschlagen {p['key']}: {e} -> bitte manuell prüfen!")
        self.state["positions"] = [p for p in self.state["positions"] if not p.get("closed")]
        self.save()
        config.KILL_FILE.parent.mkdir(exist_ok=True)
        config.KILL_FILE.write_text(f"{now().isoformat()} {why}\n")
        self.stop = True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="mit echtem Geld handeln")
    ap.add_argument("--selection", default=config.SELECTION_FILE, help="Strategie-Auswahl (JSON)")
    ap.add_argument("--agent", action="store_true", help="Claude-Supervisor aktivieren (ANTHROPIC_API_KEY nötig)")
    args = ap.parse_args()
    Bot(live=args.live, selection=args.selection, agent=args.agent).run()


if __name__ == "__main__":
    main()

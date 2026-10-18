from types import SimpleNamespace as NS

import pytest

from kraken_bot import agent, config

KEYS = ["rsi_reversion|SOLEUR|5m", "bb_breakout|XBTEUR|15m"]


def usage(i=3000, o=500):
    return NS(input_tokens=i, output_tokens=o, cache_creation_input_tokens=0, cache_read_input_tokens=0)


def tool_use(name, **inp):
    return NS(type="tool_use", id=f"tu_{name}", name=name, input=inp)


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.beta = NS(messages=NS(create=self.create))

    def create(self, **kw):
        self.calls.append(kw)
        return self.responses.pop(0)


def make(tmp_path, responses):
    logs = []
    sup = agent.Supervisor(KEYS, lambda: {"equity_eur": 99.0}, lambda *a, **k: logs.append((a, k)),
                           client=FakeClient(responses), budget=agent.Budget(tmp_path / "b.json"))
    return sup, logs


def test_tools_cannot_touch_limits():
    names = {t["name"] for t in agent._tools(KEYS)}
    assert names == {"pause_strategy", "resume_strategy", "set_risk_off", "set_size_factor", "close_position"}
    assert not any("limit" in n or "kill" in n or "config" in n or "withdraw" in n for n in names)


@pytest.mark.parametrize("name,args", [
    ("set_size_factor", {"key": KEYS[0], "factor": 2.0, "reason": "x"}),
    ("set_size_factor", {"key": KEYS[0], "factor": 0.1, "reason": "x"}),
    ("pause_strategy", {"key": "evil|XBTEUR|1m", "reason": "x"}),
    ("set_max_trade", {"value": 1000, "reason": "x"}),
    ("pause_strategy", {"key": KEYS[0], "reason": " "}),
])
def test_invalid_actions_rejected(name, args):
    assert agent.validate_action(name, args, KEYS) is not None


def test_review_queues_valid_actions_and_books_cost(tmp_path):
    r1 = NS(stop_reason="tool_use", usage=usage(), content=[
        tool_use("pause_strategy", key=KEYS[0], reason="live PF 0.4 nach 25 Trades vs. Backtest 1.3"),
        tool_use("set_size_factor", key=KEYS[1], factor=5.0, reason="mehr Risiko")])
    r2 = NS(stop_reason="end_turn", usage=usage(), content=[NS(type="text", text="Eine Strategie pausiert.")])
    sup, logs = make(tmp_path, [r1, r2])
    sup.review("test")
    assert sup.drain() == [{"action": "pause_strategy", "key": KEYS[0], "reason": "live PF 0.4 nach 25 Trades vs. Backtest 1.3"}]
    # the rejected action is reported back as an error tool_result
    results = sup.client.calls[1]["messages"][2]["content"]  # list is shared, later turns appended
    assert results[1]["is_error"] and "0.25 und 1.0" in results[1]["content"]
    assert sup.budget.spent() == pytest.approx(2 * (3000 * agent.USD_IN + 500 * agent.USD_OUT))
    assert sup.client.calls[0]["model"] == "claude-opus-5-5"


def test_budget_blocks_call(tmp_path):
    sup, logs = make(tmp_path, [])
    sup.budget.book(config.HARD_AGENT_DAILY_USD - 0.01)
    sup.review("test")
    assert sup.client.calls == []
    assert any(a[0] == "agent_budget" for a, _ in logs)


def test_review_stops_after_max_calls(tmp_path):
    loop = NS(stop_reason="tool_use", usage=usage(), content=[tool_use("set_risk_off", enabled=True, reason="Crash")])
    sup, _ = make(tmp_path, [loop] * 10)
    sup.review("test")
    assert len(sup.client.calls) == agent.MAX_CALLS_PER_REVIEW

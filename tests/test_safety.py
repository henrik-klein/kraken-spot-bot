import pytest

from kraken_bot import config
from kraken_bot.kraken_client import KrakenClient, SafetyViolation


def order(**kw):
    p = {"pair": "XBTEUR", "type": "buy", "ordertype": "limit", "volume": "0.0001"}
    p.update(kw)
    return p


def test_order_within_cap_passes():
    KrakenClient.check_order(order(volume="0.0001"), ref_price=70_000)  # ~7 EUR


def test_order_above_10_eur_rejected():
    with pytest.raises(SafetyViolation):
        KrakenClient.check_order(order(volume="0.0002"), ref_price=70_000)  # ~14 EUR


@pytest.mark.parametrize("key", ["leverage", "reduce_only", "margin"])
def test_margin_params_rejected(key):
    with pytest.raises(SafetyViolation):
        KrakenClient.check_order(order(**{key: "2"}), ref_price=70_000)


def test_non_eur_pair_rejected():
    with pytest.raises(SafetyViolation):
        KrakenClient.check_order(order(pair="XBTUSD"), ref_price=70_000)


@pytest.mark.parametrize("endpoint", ["Withdraw", "WithdrawInfo", "WalletTransfer", "WithdrawAddresses",
                                      "OpenPositions", "DepositAddresses"])
def test_non_whitelisted_endpoints_blocked(endpoint):
    c = KrakenClient(key="x", secret="eA==")
    with pytest.raises(SafetyViolation):
        c._private(endpoint)


def test_add_order_checks_before_sending(monkeypatch):
    c = KrakenClient(key="x", secret="eA==")
    monkeypatch.setattr(c, "_private", lambda *a, **k: pytest.fail("order must not be sent"))
    with pytest.raises(SafetyViolation):
        c.add_order("XBTEUR", "buy", "limit", 0.001, ref_price=70_000, price=70_000)


def test_hard_limits():
    assert config.HARD_MAX_TRADE_EUR == 10.0
    assert config.HARD_KILL_SWITCH_DRAWDOWN == 0.30
    assert config.TRADE_EUR <= config.HARD_MAX_TRADE_EUR

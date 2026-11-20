"""Broker account type observations must not infer DEMO from missing data.

These tests use an injected fake only; no MetaTrader5 package or terminal is used.
"""
from types import SimpleNamespace

import pytest

from axiom_forex.brokers.mt5.terminal import MT5Terminal
from tests.fake_mt5 import FakeMT5
from tests.test_mt5_adapter import mt5_settings


@pytest.mark.parametrize(
    "value, expected, confirmed",
    [(0, "DEMO", True), (2, "REAL", True), (1, "CONTEST", True),
     (None, "UNCONFIRMED", False), (False, "UNCONFIRMED", False),
     (True, "UNCONFIRMED", False), ("0", "UNCONFIRMED", False),
     (0.0, "UNCONFIRMED", False), (99, "UNCONFIRMED", False)],
)
def test_account_type_comes_from_broker_integer(value, expected, confirmed):
    fake = FakeMT5(trade_mode=value)
    terminal = MT5Terminal(mt5_settings(), module=fake)
    status = terminal.connect()
    assert status.account_mode == expected
    assert status.account_mode_confirmed is confirmed
    assert fake.login_calls == []
    assert fake.send_requests == []
    assert len(fake.initialize_calls) == 1


def test_missing_account_type_is_unconfirmed():
    fake = FakeMT5()
    original = fake.account_info

    def without_type():
        fields = vars(original())
        fields.pop("trade_mode")
        return SimpleNamespace(**fields)

    fake.account_info = without_type
    status = MT5Terminal(mt5_settings(), module=fake).connect()
    assert status.account_mode == "UNCONFIRMED"
    assert not status.account_mode_confirmed
    assert fake.login_calls == []
    assert fake.send_requests == []

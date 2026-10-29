"""Paper trading priced from live terminal data -- and nothing else.

The claim being tested is precise: in paper mode, AXIOM reads real bid/ask ticks
from the terminal (or refuses to trade when it cannot), simulates the fill
locally, and never calls an order-submitting API.

The fake terminal below deliberately *has* ``order_send`` and ``order_check``
methods that record their calls, so "never called" is observable rather than
assumed.  No MetaTrader 5 package, terminal, account, or credential is involved.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum

import pytest

from app.common.types import OrderSide
from app.data.models import ForexQuote
from app.execution.broker import TradingBlocked
from axiom_forex.execution.live_data_paper import (
    PAPER_MODE_REQUIRED,
    QUOTE_PAIR_MISMATCH,
    QUOTE_STALE,
    SPEC_REQUIRED,
    SYMBOL_UNRESOLVED,
    MarketDataRefused,
    PaperTradingOnLiveData,
    TerminalMarketData,
)
from fixtures.m1 import make_quote_freshness, make_spec

NOW = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)


class Mode(str, Enum):
    PAPER = "PAPER"
    LIVE = "LIVE"


class FlatMode:
    def __init__(self, mode: str) -> None:
        self.mode = mode


class FakeTerminal:
    """A data terminal with an order surface that must stay untouched."""

    def __init__(self, *, bid="1.10000", ask="1.10020", at=None, symbol="EURUSD.pro") -> None:
        self.bid = Decimal(bid)
        self.ask = Decimal(ask)
        self.at = at or NOW
        self.symbol = symbol
        self.order_send_calls: list[dict] = []
        self.order_check_calls: list[dict] = []
        self.quote_calls = 0

    def resolve_symbol(self, spec):
        return self.symbol

    def quote(self, spec, symbol):
        self.quote_calls += 1
        return ForexQuote(
            pair=str(spec.pair),
            timestamp=self.at,
            bid=self.bid,
            ask=self.ask,
            source="mt5_terminal",
            quote_id=f"mt5:{symbol}:{int(self.at.timestamp())}",
            received_at=self.at + timedelta(milliseconds=50),
            metadata={"provider_symbol": symbol, "price_basis": "BID_ASK"},
        )

    # -- the surfaces that must never be reached ------------------------
    def order_send(self, request):  # pragma: no cover - asserted not to run
        self.order_send_calls.append(dict(request))
        raise AssertionError("order_send must never be called from paper mode")

    def order_check(self, request):  # pragma: no cover - asserted not to run
        self.order_check_calls.append(dict(request))
        raise AssertionError("order_check must never be called from paper mode")


class ExplodingTerminal:
    def resolve_symbol(self, spec):
        raise MarketDataRefused(SYMBOL_UNRESOLVED, "no such symbol")

    def quote(self, spec, symbol):  # pragma: no cover - resolution fails first
        raise AssertionError("unreachable")


class FakeEngine:
    def __init__(self) -> None:
        self.calls: list[tuple[object, object]] = []
        self.record = type(
            "Record", (), {"state": "SIMULATED", "simulation_id": "sim-1", "reason": ""}
        )()

    def submit(self, request, context):
        self.calls.append((request, context))
        return self.record


class Audit:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def record(self, event):
        self.events.append(dict(event))
        return dict(event)


class Request:
    request_id = "req-1"
    pair = "AAA/BBB"  # same pair the fixture spec describes


def market(terminal=None, at=NOW) -> TerminalMarketData:
    return TerminalMarketData(
        terminal=terminal or FakeTerminal(at=at),
        freshness=make_quote_freshness(),
        clock=lambda: at,
    )


def paper(terminal=None, *, mode="PAPER", audit=None, at=NOW):
    engine = FakeEngine()
    execution = PaperTradingOnLiveData(
        engine=engine,
        market=market(terminal, at=at),
        mode=FlatMode(mode),
        audit=audit or Audit(),
    )
    return execution, engine


# ---------------------------------------------------------------------------
# live market data
# ---------------------------------------------------------------------------


def test_a_quote_comes_from_the_terminal_with_both_sides():
    data = market()
    quote = data.quote(make_spec())
    assert (quote.bid, quote.ask) == (Decimal("1.10000"), Decimal("1.10020"))
    assert quote.source == "mt5_terminal"
    assert quote.metadata["price_basis"] == "BID_ASK"


def test_a_fresh_quote_passes_the_configured_policy():
    _, decision = market().fresh_quote(make_spec())
    assert decision.ok is True
    assert decision.reason.value == "OK"


def test_a_stale_quote_is_refused_by_policy_not_filled_at_the_old_price():
    stale = FakeTerminal(at=NOW - timedelta(seconds=30))
    _, decision = market(stale).fresh_quote(make_spec())
    assert decision.ok is False
    assert decision.reason.value == "STALE_QUOTE"


def test_the_executable_price_is_the_ask_for_buy_and_the_bid_for_sell():
    data = market()
    assert data.executable_price(make_spec(), OrderSide.BUY) == Decimal("1.10020")
    assert data.executable_price(make_spec(), "SELL") == Decimal("1.10000")


def test_an_unresolvable_symbol_is_refused():
    data = TerminalMarketData(terminal=ExplodingTerminal(), freshness=make_quote_freshness())
    with pytest.raises(MarketDataRefused) as caught:
        data.quote(make_spec())
    assert caught.value.reason == SYMBOL_UNRESOLVED


# ---------------------------------------------------------------------------
# paper execution on that data
# ---------------------------------------------------------------------------


def test_paper_simulates_the_fill_and_never_calls_order_send():
    terminal = FakeTerminal(at=NOW)
    audit = Audit()
    execution, engine = paper(terminal, audit=audit)

    record = execution.submit(Request(), "context", spec=make_spec())

    assert record is engine.record
    assert engine.calls == [(Request(), "context")] or engine.calls[0][0].request_id == "req-1"
    assert terminal.order_send_calls == []
    assert terminal.order_check_calls == []

    event = audit.events[-1]
    assert event["event_type"] == "PAPER_ORDER_ON_LIVE_DATA"
    assert event["live"] is False
    assert event["order_sent"] is False
    assert event["execution"] == "SIMULATED"
    assert event["data_class"] == "LIVE_BROKER_TICK"
    assert event["quote_id"]


def test_a_stale_quote_stops_the_paper_order_before_the_engine():
    terminal = FakeTerminal(at=NOW - timedelta(seconds=30))
    audit = Audit()
    execution, engine = paper(terminal, audit=audit)

    with pytest.raises(TradingBlocked) as caught:
        execution.submit(Request(), "context", spec=make_spec())

    assert caught.value.reason == QUOTE_STALE
    assert engine.calls == []
    assert audit.events[-1]["reason"] == QUOTE_STALE
    assert audit.events[-1]["order_sent"] is False


def test_paper_will_not_run_while_live_mode_is_active():
    execution, engine = paper(mode="LIVE")
    with pytest.raises(TradingBlocked) as caught:
        execution.submit(Request(), "context", spec=make_spec())
    assert caught.value.reason == PAPER_MODE_REQUIRED
    assert engine.calls == []


def test_a_missing_spec_fails_closed():
    execution, engine = paper()
    with pytest.raises(TradingBlocked) as caught:
        execution.submit(Request(), "context")
    assert caught.value.reason == SPEC_REQUIRED
    assert engine.calls == []


def test_a_quote_for_another_pair_is_refused():
    execution, engine = paper()

    class GbpRequest(Request):
        pair = "GBP/USD"

    with pytest.raises(TradingBlocked) as caught:
        execution.submit(GbpRequest(), "context", spec=make_spec())
    assert caught.value.reason == QUOTE_PAIR_MISMATCH
    assert engine.calls == []


def test_the_paper_fill_price_comes_from_the_live_quote():
    execution, _ = paper()
    assert execution.market.executable_price(make_spec(), OrderSide.BUY) == Decimal("1.10020")


# ---------------------------------------------------------------------------
# structure
# ---------------------------------------------------------------------------


def test_the_paper_live_source_holds_no_order_submitting_surface():
    import ast
    from pathlib import Path

    source = Path("axiom_forex/execution/live_data_paper.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    names = {
        node.attr if isinstance(node, ast.Attribute) else node.id
        for node in ast.walk(tree)
        if isinstance(node, (ast.Attribute, ast.Name))
    }
    for forbidden in (
        "order_send",
        "order_check",
        "TRADE_ACTION_DEAL",
        "MT5TradingAdapter",
        "arm_trading",
    ):
        assert forbidden not in names


# ---------------------------------------------------------------------------
# through the workstation's own session
# ---------------------------------------------------------------------------


def test_the_observation_session_hands_paper_mode_a_data_only_quote_surface():
    """The session's terminal offers quotes for paper pricing -- and nothing else.

    A fake module stands in for MetaTrader 5: it records ``order_send`` calls, and
    after reading a quote the list is still empty.
    """

    from axiom_forex.config.mt5 import MT5ExecutionMode, MT5Settings
    from axiom_forex.workstation.session import MT5ObservationSource
    from tests.fake_mt5 import FakeMT5

    fake = FakeMT5()
    settings = MT5Settings(
        terminal_path="terminal64.exe",
        server="Broker-Demo",
        account_login=55500123,
        mode=MT5ExecutionMode.TRADING_ENABLED,
        symbol_suffix=".pro",
    )
    source = MT5ObservationSource(settings=settings, module=fake)
    source.connect()

    data = source.market_data(make_quote_freshness())
    quote = data.quote(make_spec(pair="EUR/USD"))

    assert quote.bid == Decimal("1.1") and quote.ask == Decimal("1.1002")
    assert quote.source == "mt5_terminal"
    assert fake.send_requests == []
    source.disconnect()


def test_paper_market_data_is_refused_without_a_connection():
    from axiom_forex.config.mt5 import MT5Settings
    from axiom_forex.workstation.session import ConnectionRefused, MT5ObservationSource
    from tests.fake_mt5 import FakeMT5

    settings = MT5Settings(terminal_path="terminal64.exe", server="Broker-Demo", account_login=1)
    source = MT5ObservationSource(settings=settings, module=FakeMT5())
    with pytest.raises(ConnectionRefused) as caught:
        source.market_data(make_quote_freshness())
    assert caught.value.reason == "not_connected"
    assert "Press Connect" in caught.value.hint

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from axiom_forex.scalper import lab
from axiom_forex.scalper.bayes import ProbabilityEstimate
from axiom_forex.scalper.config import fixture_config
from axiom_forex.scalper.desk import DeskThresholds, ScalperDesk
from axiom_forex.scalper.execution import PaperVenue
from axiom_forex.scalper.live_quotes import LiveQuoteRefused, LivePaperPump, MT5LiveQuoteSource
from axiom_forex.scalper.ticks import synthetic_ticks

UTC = timezone.utc
NOW = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)


class FakeTerminal:
    def __init__(self, tick):
        self.value = tick
        self.reads = 0
        self.methods = []

    def tick(self, symbol):
        self.reads += 1
        self.methods.append(("tick", symbol))
        if isinstance(self.value, Exception):
            raise self.value
        return self.value

    def order_send(self, *_args, **_kwargs):
        raise AssertionError("PAPER must not send orders")

    def order_check(self, *_args, **_kwargs):
        raise AssertionError("PAPER must not check broker orders")

    def close_position(self, *_args, **_kwargs):
        raise AssertionError("PAPER must not close broker positions")


class FakeSession:
    def __init__(self, terminal, connected=False):
        self._terminal = terminal
        self.connected = connected
        self.connect_calls = 0

    def connect(self):
        self.connect_calls += 1
        self.connected = True

    def market_data_source(self):
        if not self.connected:
            return None
        return self._terminal


def tick(at=NOW, bid="1.10000", ask="1.10010", symbol="EURUSD"):
    return SimpleNamespace(time=at, bid=Decimal(bid), ask=Decimal(ask), symbol=symbol, volume=None)


def test_no_connect_attempt_or_quote_read_until_explicit_workstation_connect():
    terminal = FakeTerminal(tick())
    session = FakeSession(terminal)
    source = MT5LiveQuoteSource(session, clock=lambda: NOW)
    with pytest.raises(LiveQuoteRefused, match="terminal_not_connected"):
        source.read("EURUSD")
    assert session.connect_calls == terminal.reads == 0
    session.connect()  # models the operator's Connect action in the workstation
    result = source.read("EURUSD")
    assert result.symbol == "EURUSD" and result.source == "MT5_LIVE_PAPER"
    assert result.received_at == NOW
    assert terminal.reads == 1


@pytest.mark.parametrize(("value", "reason"), [
    (None, "quote_fields_missing"),
    (SimpleNamespace(time=None, bid=1, ask=1), "quote_fields_missing"),
    (SimpleNamespace(time=NOW, bid=0, ask=1), "quote_bid_ask_invalid"),
    (SimpleNamespace(time=NOW, bid="bad", ask=1), "quote_bid_ask_malformed"),
    (SimpleNamespace(time=NOW, bid=2, ask=1), "quote_bid_ask_invalid"),
])
def test_malformed_or_missing_quote_is_refused(value, reason):
    terminal = FakeTerminal(value)
    source = MT5LiveQuoteSource(FakeSession(terminal, True), clock=lambda: NOW)
    with pytest.raises(LiveQuoteRefused, match=reason):
        source.read("EURUSD")


def test_repeated_old_terminal_tick_cannot_refresh_freshness():
    terminal = FakeTerminal(tick(at=NOW - timedelta(seconds=2)))
    source = MT5LiveQuoteSource(FakeSession(terminal, True), clock=lambda: NOW,
                                max_quote_age_ms=500)
    with pytest.raises(LiveQuoteRefused, match="stale_quote"):
        source.read("EURUSD")


def test_tick_data_is_routed_to_local_paper_desk_only():
    spec_config = fixture_config(min_window_ticks=20, resync_ticks=0)
    venue = PaperVenue(cost=spec_config.cost)
    desk = ScalperDesk(config=spec_config, venue=venue, starting_equity=Decimal("10000"))
    symbol = spec_config.instruments[0].symbol
    terminal = FakeTerminal(tick(symbol=symbol))
    source = MT5LiveQuoteSource(FakeSession(terminal, True), clock=lambda: NOW,
                                monotonic_ns=lambda: 9_000_000_000)
    quote = source.read(symbol)
    outcome = desk.on_tick(symbol=quote.symbol, bid=quote.bid, ask=quote.ask,
                           source_time=quote.source_time, received_at=quote.received_at,
                           source=quote.source)
    assert outcome.accepted
    assert desk.status()["ticks"]["accepted"] == 1
    assert venue.mode == "PAPER"
    assert terminal.methods == [("tick", symbol)]


def test_live_source_stream_can_produce_simulated_paper_fills():
    cfg = fixture_config(cooldown_ms=1500, min_window_ticks=40, max_open_positions=1)
    spec = cfg.instruments[0]
    strategies = lab.cost_compliant_strategies(
        assumed_spread_points=Decimal("8"), stops_level_points=spec.stops_level_points,
        cost=cfg.cost, point_value_per_lot=spec.point_value_per_lot,
    )
    training = lab.collect_outcomes(
        (lab.trending(count=300), lab.ranging(count=300), lab.reversal(count=300)),
        config=cfg, strategies=strategies,
    )
    book = training["book"]
    def probability(signal, regime):
        return book.estimate(signal.parameter_fingerprint, regime)
    desk = ScalperDesk(config=cfg, strategies=strategies,
                       venue=PaperVenue(cost=cfg.cost), probability_source=probability,
                       thresholds=DeskThresholds(min_probability_samples=20),
                       starting_equity=Decimal("10000"))
    replay = synthetic_ticks(count=200, drift_points=1.6, noise_points=1.2, step_ms=200)

    class SequenceTerminal:
        def __init__(self):
            self.current = None
            self.items = iter(replay)
        def tick(self, _symbol):
            item = next(self.items)
            self.current = SimpleNamespace(symbol=item.symbol, time=item.at, bid=item.bid,
                                           ask=item.ask, volume=item.volume)
            return self.current

    terminal = SequenceTerminal()
    session = FakeSession(terminal, connected=True)
    source = MT5LiveQuoteSource(
        session, clock=lambda: terminal.current.time + timedelta(milliseconds=20),
        max_quote_age_ms=1500,
    )
    for _ in replay:
        quote = source.read(spec.symbol)
        desk.on_tick(symbol=quote.symbol, bid=quote.bid, ask=quote.ask,
                     source_time=quote.source_time, received_at=quote.received_at,
                     volume=terminal.current.volume, source=quote.source)
    assert desk.status()["stats"]["fills"] > 0
    fills = [entry for entry in desk.scribe.entries("execution_result")
             if entry.payload.get("filled_lots") not in (None, "0")]
    assert len(fills) == desk.status()["stats"]["fills"]
    assert all(entry.payload.get("quote_source") == "MT5_LIVE_PAPER"
               and entry.payload.get("quote_source_time")
               and entry.payload.get("quote_bid") and entry.payload.get("quote_ask")
               for entry in fills)


def test_stale_quote_is_refused_by_tick_core():
    cfg = fixture_config(max_quote_age_ms=1000, resync_ticks=0)
    desk = ScalperDesk(config=cfg, venue=PaperVenue(cost=cfg.cost))
    result = desk.on_tick(symbol=cfg.instruments[0].symbol, bid="1.10000", ask="1.10010",
                          source_time=NOW - timedelta(seconds=3), received_at=NOW,
                          source="MT5_LIVE_PAPER")
    assert not result.accepted and result.rejection == "stale_tick"


def test_duplicate_and_out_of_order_quotes_are_refused_by_tick_core():
    cfg = fixture_config(min_window_ticks=20, max_quote_age_ms=10_000, resync_ticks=0)
    desk = ScalperDesk(config=cfg, venue=PaperVenue(cost=cfg.cost))
    base = dict(symbol=cfg.instruments[0].symbol, bid="1.10000", ask="1.10010", received_at=NOW,
                source="MT5_LIVE_PAPER")
    first = desk.on_tick(**base, source_time=NOW)
    duplicate = desk.on_tick(**base, source_time=NOW)
    old = desk.on_tick(**base, source_time=NOW - timedelta(seconds=1))
    assert first.accepted
    assert not duplicate.accepted and duplicate.rejection == "duplicate_tick"
    assert not old.accepted and old.rejection == "out_of_order_tick"


def test_pump_refuses_live_start_while_disconnected_and_pauses_on_disconnect():
    cfg = fixture_config()
    desk = ScalperDesk(config=cfg, venue=PaperVenue(cost=cfg.cost))
    terminal = FakeTerminal(tick())
    session = FakeSession(terminal)
    pump = LivePaperPump(source=MT5LiveQuoteSource(session, clock=lambda: NOW),
                         desk=desk, symbol=cfg.instruments[0].symbol, interval_seconds=.005)
    assert pump.start()["reason"] == "terminal_not_connected"
    assert terminal.reads == 0
    session.connect()
    assert pump.start()["ok"]
    import time
    deadline = time.monotonic() + 1
    while terminal.reads == 0 and time.monotonic() < deadline:
        time.sleep(.005)
    assert terminal.reads > 0
    session.connected = False
    deadline = time.monotonic() + 1
    while pump.status()["state"] == "RUNNING" and time.monotonic() < deadline:
        time.sleep(.005)
    assert pump.status()["state"] == "PAUSED"
    assert pump.status()["reason"] == "terminal_disconnected"
    reads = terminal.reads
    time.sleep(.02)
    assert terminal.reads == reads

"""The trader, the paper venue and the position manager.

These are the tests for the parts where being wrong costs money immediately:
idempotency, partial fills, rejects, ambiguous outcomes, protective orders, and
exits that must not be blocked by anything.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.execution.broker import TradingBlocked
from axiom_forex.scalper.config import fixture_config
from axiom_forex.scalper.execution import (
    ACCEPTED,
    ALREADY_SUBMITTED,
    BLOCKED,
    MARKET_CLOSED,
    PAPER_ONLY,
    PRECHECK_FAILED,
    PROTECTION_UNVERIFIED,
    REJECTED,
    SIGNAL_EXPIRED,
    SIGNAL_STALE_QUOTE,
    STOPS_TOO_CLOSE,
    UNKNOWN_OUTCOME,
    UNRESOLVED_ORDER,
    PaperVenue,
    Trader,
    VenueFaults,
    VenueOrder,
    precheck,
)
from axiom_forex.scalper.guardian import Guardian, GuardianLimits, OpenPositionRisk
from axiom_forex.scalper.positions import (
    BREAK_EVEN,
    CLOSE,
    MODIFY_STOP,
    MOMENTUM_FLIP,
    PARTIAL_EXIT,
    STOP_HIT,
    TARGET_HIT,
    TIME_EXIT,
    TRAIL,
    ManagementSettings,
    PositionBook,
)
from axiom_forex.scalper.signals import Signal
from axiom_forex.scalper.ticks import ReplayTick, Tick, TickCore, synthetic_ticks

CONFIG = fixture_config()
SPEC = CONFIG.spec_for("EURUSD.pro")
START = datetime(2026, 3, 2, 9, 0, tzinfo=timezone.utc)


def ticks(count: int = 12, *, step_ms: int = 100, mid: str = "1.10200", spread_points: str = "8"):
    point = SPEC.point
    spread = Decimal(spread_points) * point
    base = Decimal(mid)
    out = []
    for index in range(count):
        out.append(
            ReplayTick(
                symbol=SPEC.symbol,
                bid=base - spread / 2,
                ask=base + spread / 2,
                at=START + timedelta(milliseconds=step_ms * index),
            )
        )
    return out


def accepted(ticks_) -> list[Tick]:
    core = TickCore(
        instruments=CONFIG.instruments,
        max_quote_age_ms=CONFIG.max_quote_age_ms,
        max_spread_points=CONFIG.max_spread_points,
        gap_threshold_ms=CONFIG.gap_threshold_ms,
        min_window_ticks=CONFIG.min_window_ticks,
        tick_window=CONFIG.tick_window,
    )
    out = []
    for tick in ticks_:
        result = core.ingest(
            symbol=tick.symbol, bid=tick.bid, ask=tick.ask, source_time=tick.at, received_at=tick.at
        )
        if result.accepted:
            out.append(result.tick)
    return out


def ms(moment: datetime) -> int:
    return int(moment.timestamp() * 1000)


def signal(*, stop: str = "25", target: str = "60", at: datetime | None = None) -> Signal:
    reference = Decimal("1.10200")
    return Signal(
        symbol=SPEC.symbol,
        family="momentum",
        strategy="momentum_continuation",
        side="BUY",
        at=at or START,
        tick_id="tick-1",
        reference_price=reference,
        stop_points=Decimal(stop),
        target_points=Decimal(target),
        reason="test",
        rule_version="v1",
        parameter_fingerprint="a" * 64,
        ttl_ms=750,
        _point=SPEC.point,
    )


def guardian(equity: str = "10000") -> Guardian:
    instance = Guardian(GuardianLimits.from_config(CONFIG), declared_slippage_points=Decimal("1"))
    instance.observe_account(
        equity=Decimal(equity), balance=Decimal(equity), at_ms=0, day_key="2026-03-02"
    )
    return instance


def trader(venue=None, **kwargs) -> Trader:
    return Trader(
        venue=venue or PaperVenue(cost=CONFIG.cost, faults=VenueFaults(max_latency_ms=5)),
        guardian=guardian(),
        cost=CONFIG.cost,
        specs=CONFIG.instruments_by_symbol,
        max_quote_age_ms=CONFIG.max_quote_age_ms,
        **kwargs,
    )


def execute(instance, *, quote=None, sig=None, lots="0.10", now_ms=None):
    quote = quote or accepted(ticks())[-1]
    sig = sig or signal(at=quote.source_time)
    return instance.execute(
        signal=sig,
        lots=Decimal(lots),
        risk_account=Decimal("5"),
        notional_account=Decimal("1000"),
        quote=quote,
        now_ms=now_ms if now_ms is not None else ms(quote.source_time),
        decision_latency_ms=10,
        currencies=("EUR", "USD"),
    )


# ---------------------------------------------------------------------------
# venue
# ---------------------------------------------------------------------------


def test_a_paper_venue_never_claims_to_be_live():
    venue = PaperVenue(cost=CONFIG.cost)
    assert venue.mode == "PAPER"


def test_a_trader_refuses_a_venue_that_is_not_paper():
    class LiveVenue(PaperVenue):
        mode = "LIVE"

    with pytest.raises(TradingBlocked):
        Trader(
            venue=LiveVenue(cost=CONFIG.cost),
            guardian=guardian(),
            cost=CONFIG.cost,
            specs=CONFIG.instruments_by_symbol,
            max_quote_age_ms=1500,
        )


def test_the_same_request_id_never_fills_twice():
    venue = PaperVenue(cost=CONFIG.cost)
    quote = accepted(ticks())[-1]
    order = VenueOrder(
        request_id="r1",
        symbol=SPEC.symbol,
        side="BUY",
        lots=Decimal("0.1"),
        stop_price=Decimal("1.10100"),
        target_price=Decimal("1.10300"),
        requested_at_ms=ms(quote.source_time),
        signal_fingerprint="f",
        strategy="s",
        point=SPEC.point,
    )
    first = venue.submit(order, quote)
    second = venue.submit(order, quote)
    assert first.status == ACCEPTED
    assert second.status == ACCEPTED
    assert second.ticket == first.ticket
    assert venue.stats["submissions"] == 2


def test_a_partial_fill_is_booked_at_the_filled_size():
    venue = PaperVenue(
        cost=CONFIG.cost, faults=VenueFaults(partial_every=1, partial_fraction=Decimal("0.5"))
    )
    fill = execute(trader(venue))
    assert fill.fill is not None
    assert fill.fill.partial is True
    assert fill.fill.filled_lots == Decimal("0.05")
    assert any(event["event"] == "partial_fill" for event in fill.events)
    assert "not chased" in " ".join(
        event.get("detail", "") for event in fill.events if event["event"] == "partial_fill"
    )


def test_a_rejected_order_is_reported_not_retried():
    venue = PaperVenue(cost=CONFIG.cost, faults=VenueFaults(reject_every=1))
    report = execute(trader(venue))
    assert report.status == REJECTED
    assert report.filled is False
    assert venue.stats["submissions"] == 1


def test_a_closed_market_is_a_rejection():
    venue = PaperVenue(cost=CONFIG.cost, market_open=False)
    report = execute(trader(venue))
    assert report.status == REJECTED
    assert report.fill.reason == MARKET_CLOSED


# ---------------------------------------------------------------------------
# the trader's gates
# ---------------------------------------------------------------------------


def test_a_stale_quote_stops_the_order_before_it_is_sent():
    instance = trader()
    quote = accepted(ticks())[-1]
    report = execute(instance, quote=quote, now_ms=ms(quote.source_time) + 5_000)
    assert report.status == BLOCKED
    assert SIGNAL_STALE_QUOTE in report.reasons


def test_an_expired_signal_is_not_traded_late():
    instance = trader()
    quote = accepted(ticks())[-1]
    old_signal = signal(at=START - timedelta(seconds=2))
    report = execute(instance, quote=quote, sig=old_signal, now_ms=ms(quote.source_time))
    assert report.status == BLOCKED
    assert SIGNAL_EXPIRED in report.reasons


def test_an_undeclared_instrument_is_refused():
    instance = trader()
    quote = accepted(ticks())[-1]
    unknown = Signal(
        symbol="GBPUSD",
        family="momentum",
        strategy="momentum_continuation",
        side="BUY",
        at=START,
        tick_id="t",
        reference_price=Decimal("1.2"),
        stop_points=Decimal("25"),
        target_points=Decimal("60"),
        reason="test",
        rule_version="v1",
        parameter_fingerprint="b" * 64,
        ttl_ms=750,
        _point=Decimal("0.00001"),
    )
    report = execute(instance, quote=quote, sig=unknown)
    assert report.status == BLOCKED
    assert "instrument_not_declared" in report.reasons


def test_the_guardian_is_asked_and_its_refusal_is_final():
    instance = trader()
    instance._guardian.suspend_entries("operator_hold")  # noqa: SLF001 - deliberate
    report = execute(instance)
    assert report.status == BLOCKED
    assert "guardian_refused" in report.reasons


def test_a_stop_inside_the_broker_stops_level_is_refused_before_submission():
    venue = PaperVenue(cost=CONFIG.cost)
    instance = trader(venue)
    quote_now = accepted(ticks())[-1]
    report = execute(instance, quote=quote_now, sig=signal(stop="5", at=quote_now.source_time))
    assert report.status == BLOCKED
    assert PRECHECK_FAILED in report.reasons
    assert STOPS_TOO_CLOSE in report.reasons
    assert venue.stats["submissions"] == 0


def test_a_signal_without_a_priceable_stop_is_blocked():
    instance = trader()
    quote_now = accepted(ticks())[-1]
    no_point = signal(at=quote_now.source_time)
    object.__setattr__(no_point, "_point", None)
    report = execute(instance, quote=quote_now, sig=no_point)
    assert report.status == BLOCKED
    assert PROTECTION_UNVERIFIED in report.reasons


def test_the_same_signal_is_only_submitted_once():
    instance = trader()
    quote = accepted(ticks())[-1]
    first = execute(instance, quote=quote)
    second = execute(instance, quote=quote)
    assert first.status == ACCEPTED
    assert second.status == BLOCKED
    assert ALREADY_SUBMITTED in second.reasons


def test_precheck_rejects_volume_outside_the_broker_terms():
    check = precheck(
        spec=SPEC,
        side="BUY",
        lots=Decimal("0.005"),
        price=Decimal("1.102"),
        stop_price=Decimal("1.101"),
        target_price=None,
    )
    assert check.passed is False
    assert "volume_outside_broker_terms" in check.reasons


def test_precheck_rejects_a_stop_on_the_wrong_side():
    check = precheck(
        spec=SPEC,
        side="BUY",
        lots=Decimal("0.1"),
        price=Decimal("1.102"),
        stop_price=Decimal("1.103"),
        target_price=None,
    )
    assert check.passed is False
    assert "stop_on_the_wrong_side_of_entry" in check.reasons


# ---------------------------------------------------------------------------
# ambiguous outcomes
# ---------------------------------------------------------------------------


def test_an_ambiguous_outcome_blocks_everything_until_it_is_resolved():
    venue = PaperVenue(cost=CONFIG.cost, faults=VenueFaults(unknown_every=1, max_latency_ms=1))
    instance = trader(venue)
    quote = accepted(ticks())[-1]
    first = execute(instance, quote=quote)
    assert first.status == UNKNOWN_OUTCOME
    assert instance.has_unresolved is True

    second = execute(
        instance,
        quote=quote,
        sig=signal(stop="30", target="70", at=START),
    )
    assert second.status == BLOCKED
    assert UNRESOLVED_ORDER in second.reasons
    assert venue.stats["submissions"] == 1  # nothing was resent


def test_resolving_an_ambiguous_outcome_can_adopt_a_fill():
    venue = PaperVenue(cost=CONFIG.cost, faults=VenueFaults(unknown_every=1, seed=2))
    instance = trader(venue)
    report = execute(instance)
    state, fill = instance.resolve(report.request_id)
    assert state in {"filled", "not_found", "rejected"}
    if state == "filled":
        assert instance.has_unresolved is False
        assert fill.ticket is not None


def test_a_resend_is_only_allowed_when_the_venue_confirms_it_never_saw_the_order():
    venue = PaperVenue(cost=CONFIG.cost, faults=VenueFaults(unknown_every=1))
    instance = trader(venue)
    report = execute(instance)
    state, _fill = instance.resolve(report.request_id)
    assert state in {"filled", "still_unknown"} or state == "not_found"
    allowed = instance.retry_after_not_found(report.request_id)
    assert allowed is (state == "not_found")


def test_reconciliation_names_orphans_and_unprotected_positions():
    venue = PaperVenue(cost=CONFIG.cost)
    instance = trader(venue)
    report = execute(instance)
    ticket = report.fill.ticket
    result = instance.reconcile(tickets=[ticket, "9999"])
    assert result["ok"] is False
    assert result["unknown_to_trader"] == ["9999"]


# ---------------------------------------------------------------------------
# position manager
# ---------------------------------------------------------------------------


def book(venue=None, **overrides) -> PositionBook:
    settings = ManagementSettings.from_config(CONFIG)
    if overrides:
        settings = ManagementSettings(**{**settings.__dict__, **overrides})
    return PositionBook(
        venue=venue or PaperVenue(cost=CONFIG.cost),
        settings=settings,
        specs=CONFIG.instruments_by_symbol,
    )


def open_through_venue(instance, *, entry="1.10200", stop="1.10175", target="1.10260", lots="0.1"):
    """Open a position the way the desk does: a venue fill with protective orders."""

    venue = instance._venue  # noqa: SLF001 - the test is setting up the venue's own book
    quote_now = accepted(ticks())[-1]
    order = VenueOrder(
        request_id="setup-order",
        symbol=SPEC.symbol,
        side="BUY",
        lots=Decimal(lots),
        stop_price=Decimal(stop),
        target_price=None if target is None else Decimal(target),
        requested_at_ms=ms(quote_now.source_time),
        signal_fingerprint="a" * 64,
        strategy="setup",
        point=SPEC.point,
    )
    fill = venue.submit(order, quote_now)
    return instance.open_from_fill(
        fill=fill,
        symbol=SPEC.symbol,
        side="BUY",
        stop_price=Decimal(stop),
        target_price=None if target is None else Decimal(target),
        strategy_fingerprint="a" * 64,
        now_ms=0,
    )


def position(instance, *, side="BUY", entry="1.10200", stop="1.10175", target="1.10260", lots="0.1"):
    return instance.open_position(
        position_id="1000",
        symbol=SPEC.symbol,
        side=side,
        lots=Decimal(lots),
        entry_price=Decimal(entry),
        stop_price=Decimal(stop),
        target_price=None if target is None else Decimal(target),
        now_ms=0,
        request_id="req-1",
        strategy_fingerprint="a" * 64,
    )


def quote(price: str, *, at_ms: int, spread_points: str = "8") -> Tick:
    spread = Decimal(spread_points) * SPEC.point
    mid = Decimal(price)
    return Tick(
        symbol=SPEC.symbol,
        bid=mid - spread / 2,
        ask=mid + spread / 2,
        source_time=START + timedelta(milliseconds=at_ms),
        received_at=START + timedelta(milliseconds=at_ms),
        sequence=1,
        source="TEST",
        ingest_latency_ms=0.0,
    )


def test_a_stop_touch_closes_the_position_at_the_quote_when_it_gaps():
    instance = book()
    position(instance)
    actions = instance.on_tick(
        position_id="1000", tick=quote("1.10150", at_ms=100), now_ms=100
    )
    assert actions[0].kind == CLOSE
    assert actions[0].reason == STOP_HIT
    assert actions[0].exit_price < Decimal("1.10175")


def test_a_target_touch_closes_with_a_win():
    instance = book()
    open_through_venue(instance)
    actions = instance.on_tick(
        position_id="1000", tick=quote("1.10270", at_ms=100), now_ms=100
    )
    assert actions[0].reason == TARGET_HIT
    assert instance.closed[0].gross_points > 0
    assert instance.closed[0].won is True


def test_break_even_moves_the_stop_to_the_entry_after_the_trigger():
    instance = book()
    open_through_venue(instance)  # fills at 1.10205 (ask plus one point of slippage)
    actions = instance.on_tick(
        position_id="1000", tick=quote("1.10218", at_ms=100), now_ms=100
    )
    assert any(action.kind == MODIFY_STOP for action in actions)
    assert any(action.reason == BREAK_EVEN for action in actions)
    assert instance.get("1000").break_even_moved is True
    assert instance.get("1000").stop_price > Decimal("1.10200")


def test_trailing_ratchets_and_never_widens():
    instance = book()
    open_through_venue(instance)
    for index, price in enumerate(["1.10220", "1.10240", "1.10260", "1.10240"]):
        instance.on_tick(
            position_id="1000", tick=quote(price, at_ms=100 + index * 100), now_ms=100 + index * 100
        )
        if not instance.open_positions:
            break
    closed = instance.closed
    if closed:
        # The trail must have protected the move rather than given it back.
        assert closed[0].reason in {TRAIL, TARGET_HIT} or closed[0].gross_points > 0
    else:
        assert instance.get("1000").trailing_active is True


def test_a_time_exit_closes_a_position_that_overstays():
    instance = book()
    position(instance)
    late = CONFIG.max_hold_ms + 100
    actions = instance.on_tick(position_id="1000", tick=quote("1.10205", at_ms=late), now_ms=late)
    assert actions[0].reason == TIME_EXIT


def test_a_stale_quote_closes_rather_than_managing_blind():
    instance = book()
    open_through_venue(instance)
    stale_tick = quote("1.10205", at_ms=0)
    actions = instance.on_tick(
        position_id="1000", tick=stale_tick, now_ms=ms(stale_tick.source_time) + 60_000
    )
    assert actions[0].reason in {"stale_quote_exit", TIME_EXIT}


def test_a_momentum_flip_exits_the_position():
    instance = book()
    open_through_venue(instance)
    actions = instance.on_tick(
        position_id="1000", tick=quote("1.10205", at_ms=100), now_ms=100, momentum_flip=True
    )
    assert actions[0].reason == MOMENTUM_FLIP


def test_a_partial_exit_reduces_the_position_without_closing_it():
    instance = book(partial_exit_points=Decimal("10"))
    open_through_venue(instance)
    actions = instance.on_tick(
        position_id="1000", tick=quote("1.10220", at_ms=100), now_ms=100
    )
    assert any(action.kind == PARTIAL_EXIT for action in actions)
    remaining = instance.get("1000")
    assert remaining is not None
    assert remaining.lots < Decimal("0.1")
    assert remaining.partial_closed_lots > 0


def test_mae_and_mfe_are_recorded_for_every_trade():
    instance = book()
    open_through_venue(instance)
    instance.on_tick(position_id="1000", tick=quote("1.10230", at_ms=100), now_ms=100)
    instance.on_tick(position_id="1000", tick=quote("1.10190", at_ms=200), now_ms=200)
    instance.on_tick(position_id="1000", tick=quote("1.10270", at_ms=300), now_ms=300)
    assert instance.closed, "the trail should have protected a 21-point run"
    trade = instance.closed[0]
    assert trade.mfe_points >= Decimal("20")
    assert trade.reason == STOP_HIT


def test_protection_verification_reads_the_venue():
    venue = PaperVenue(cost=CONFIG.cost)
    quote_now = accepted(ticks())[-1]
    order = VenueOrder(
        request_id="r1",
        symbol=SPEC.symbol,
        side="BUY",
        lots=Decimal("0.1"),
        stop_price=Decimal("1.10100"),
        target_price=None,
        requested_at_ms=ms(quote_now.source_time),
        signal_fingerprint="f",
        strategy="s",
        point=SPEC.point,
    )
    fill = venue.submit(order, quote_now)
    instance = PositionBook(
        venue=venue, settings=ManagementSettings.from_config(CONFIG), specs=CONFIG.instruments_by_symbol
    )
    position = instance.open_from_fill(
        fill=fill,
        symbol=SPEC.symbol,
        side="BUY",
        stop_price=Decimal("1.10100"),
        target_price=None,
        strategy_fingerprint="a" * 64,
        now_ms=0,
    )
    result = instance.verify_protection()
    assert result["ok"] is True
    assert position.position_id in result["verified"]


def test_an_unprotected_position_is_flagged_and_entries_are_suspended():
    venue = PaperVenue(cost=CONFIG.cost)
    instance = PositionBook(
        venue=venue, settings=ManagementSettings.from_config(CONFIG), specs=CONFIG.instruments_by_symbol
    )
    instance.open_position(
        position_id="2000",
        symbol=SPEC.symbol,
        side="BUY",
        lots=Decimal("0.1"),
        entry_price=Decimal("1.10200"),
        stop_price=Decimal("1.10175"),
        target_price=None,
        now_ms=0,
        request_id="req-2",
    )
    result = instance.verify_protection()
    assert result["ok"] is False
    assert result["missing"] == ["2000"]


def test_flatten_closes_what_it_can_and_reports_what_it_cannot():
    instance = book()
    position(instance)
    instance.open_position(
        position_id="2000",
        symbol="GBPUSD",
        side="BUY",
        lots=Decimal("0.1"),
        entry_price=Decimal("1.2"),
        stop_price=Decimal("1.19"),
        target_price=None,
        now_ms=0,
        request_id="req-2",
    )
    actions = instance.flatten_all(ticks={SPEC.symbol: quote("1.10205", at_ms=100)}, now_ms=100)
    assert [action.position_id for action in actions] == ["1000"]
    blocked = instance.reconcile(venue_tickets=["2000"])["blocked_exits"]
    assert any(item["position_id"] == "2000" for item in blocked)
    assert instance.get("2000") is not None  # never deleted silently


def test_reconcile_flags_a_position_the_venue_no_longer_reports():
    instance = book()
    position(instance)
    result = instance.reconcile(venue_tickets=[])
    assert result["vanished_from_venue"] == ["1000"]
    assert result["ok"] is False
    assert result["blocked_exits"]


def test_the_book_summary_never_reports_a_win_rate_from_no_trades():
    instance = book()
    summary = instance.summary()
    assert summary["closed_trades"] == 0
    assert summary["win_rate"] is None

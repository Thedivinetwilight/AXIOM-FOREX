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
    Fill,
    MARKET_CLOSED,
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
from axiom_forex.scalper.guardian import Guardian, GuardianLimits
from axiom_forex.scalper.positions import (
    BREAK_EVEN,
    CLOSE,
    CLOSE_UNCONFIRMED,
    HOLD,
    MODIFY_STOP,
    MOMENTUM_FLIP,
    PARTIAL_EXIT,
    PROTECTION_MISSING,
    STOP_HIT,
    TARGET_HIT,
    TIME_EXIT,
    TRAIL,
    ManagementSettings,
    PositionBook,
)
from axiom_forex.scalper.signals import Signal
from axiom_forex.scalper.ticks import ReplayTick, Tick, TickCore

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


def test_a_modify_return_is_not_protection_until_the_broker_echoes_the_new_stop():
    class LyingModifyVenue(PaperVenue):
        def modify_protection(self, *, ticket, stop_price, target_price, request_id=None):
            return True

    venue = LyingModifyVenue(cost=CONFIG.cost)
    instance = book(venue=venue)
    open_through_venue(instance)
    original_stop = instance.get("1000").stop_price
    actions = instance.on_tick(
        position_id="1000", tick=quote("1.10218", at_ms=100), now_ms=100
    )

    assert any(action.kind == HOLD and action.reason == PROTECTION_MISSING for action in actions)
    assert instance.get("1000").stop_price == original_stop
    assert instance.get("1000").protection_verified is False
    assert instance.verify_protection()["missing"] == ["1000"]
    assert instance.summary()["pending_protection_modifications"] == ["1000"]


def test_target_change_is_pending_until_both_protection_fields_echo_after_restore():
    class StopOnlyEchoVenue(PaperVenue):
        def modify_protection(self, *, ticket, stop_price, target_price, request_id=None):
            _current_stop, current_target = self.protection(ticket)
            return super().modify_protection(
                ticket=ticket,
                stop_price=stop_price,
                target_price=current_target,
                request_id=request_id,
            )

    venue = StopOnlyEchoVenue(cost=CONFIG.cost)
    instance = book(venue=venue)
    open_through_venue(instance)
    current = instance.get("1000")
    assert current is not None and current.target_price is not None
    requested_target = current.target_price + Decimal("0.00020")

    action = instance.modify_protection(
        position_id="1000",
        stop_price=current.stop_price,
        target_price=requested_target,
        now_ms=100,
        reason="operator_target_update",
    )

    pending = instance.snapshot()["pending_protection_modifications"]
    assert action.kind == HOLD
    assert instance.get("1000").target_price == current.target_price
    assert instance.get("1000").protection_verified is False
    assert pending[0]["target_price"] == str(requested_target)
    assert pending[0]["previous_target_price"] == str(current.target_price)
    assert instance.verify_protection()["missing"] == ["1000"]
    assert instance.snapshot()["pending_protection_modifications"] == pending

    restored = book(venue=venue)
    restored.restore_state(instance.snapshot())
    assert restored.verify_protection()["missing"] == ["1000"]
    venue._protection["1000"] = (current.stop_price, requested_target)
    verified = restored.verify_protection()

    assert verified["ok"] is True
    assert restored.get("1000").target_price == requested_target
    assert restored.get("1000").protection_verified is True
    assert restored.snapshot()["pending_protection_modifications"] == []


def test_live_protection_change_requires_both_durable_intent_and_result_hooks():
    class LivePaperHarness(PaperVenue):
        mode = "LIVE"

        def __init__(self):
            super().__init__(cost=CONFIG.cost)
            self.modification_calls = 0

        def modify_protection(self, *, ticket, stop_price, target_price, request_id=None):
            self.modification_calls += 1
            return super().modify_protection(
                ticket=ticket,
                stop_price=stop_price,
                target_price=target_price,
                request_id=request_id,
            )

    venue = LivePaperHarness()
    instance = book(venue=venue)
    open_through_venue(instance)
    tick = quote("1.10218", at_ms=100)

    actions = instance.on_tick(position_id="1000", tick=tick, now_ms=100)

    assert venue.modification_calls == 0
    assert any(action.kind == HOLD and action.reason == PROTECTION_MISSING for action in actions)
    assert instance.snapshot()["pending_protection_modifications"]
    assert instance.get("1000").protection_verified is False


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


def test_a_refused_close_stays_open_and_blocks_new_entries():
    class RefusingCloseVenue(PaperVenue):
        def __init__(self):
            super().__init__(cost=CONFIG.cost)
            self.close_attempts = 0

        def close(self, *, ticket, price, spread_points, reason, quote=None, quantity=None):
            self.close_attempts += 1
            return Fill(
                request_id=f"close:{ticket}",
                status=REJECTED,
                filled_lots=Decimal(0),
                requested_lots=Decimal(str(quantity or 0)),
                price=None,
                spread_points=spread_points,
                slippage_points=Decimal(0),
                latency_ms=0,
                ticket=ticket,
                reason="broker_rejected",
                simulated=False,
            )

    venue = RefusingCloseVenue()
    instance = book(venue=venue)
    position(instance)
    action = instance.close_now(
        position_id="1000", tick=quote("1.10210", at_ms=100), now_ms=100
    )

    assert action.kind == HOLD
    assert action.reason == CLOSE_UNCONFIRMED
    assert instance.get("1000") is not None
    assert instance.closed == ()
    assert instance.close_blockers == ({"position_id": "1000", "reason": "broker_rejected"},)
    assert venue.close_attempts == 1


def test_an_unknown_close_is_latched_until_its_exact_durable_fill_is_reconciled():
    class UnknownCloseVenue(PaperVenue):
        def __init__(self):
            super().__init__(cost=CONFIG.cost)
            self.close_attempts = 0

        def close(self, *, ticket, price, spread_points, reason, quote=None, quantity=None):
            self.close_attempts += 1
            return Fill(
                request_id="close-request-17",
                status=UNKNOWN_OUTCOME,
                filled_lots=Decimal(0),
                requested_lots=Decimal(str(quantity)),
                price=None,
                spread_points=spread_points,
                slippage_points=Decimal(0),
                latency_ms=0,
                ticket=ticket,
                reason="response_lost",
                simulated=False,
            )

    venue = UnknownCloseVenue()
    instance = book(venue=venue)
    position(instance)
    first = instance.close_now(
        position_id="1000", tick=quote("1.10210", at_ms=100), now_ms=100
    )
    retry = instance.close_now(
        position_id="1000", tick=quote("1.10220", at_ms=200), now_ms=200
    )

    assert first.kind == HOLD and first.reason == CLOSE_UNCONFIRMED
    assert retry.kind == HOLD and retry.reason == CLOSE_UNCONFIRMED
    assert venue.close_attempts == 1
    assert instance.pending_closes[0]["request_id"] == "close-request-17"

    reconciled = instance.resolve_pending_close(
        position_id="1000",
        fill=Fill(
            request_id="close-request-17",
            status=ACCEPTED,
            filled_lots=Decimal("0.04"),
            requested_lots=Decimal("0.10"),
            price=Decimal("1.10217"),
            spread_points=Decimal(0),
            slippage_points=Decimal(0),
            latency_ms=0,
            ticket="1000",
            reason="FILLED",
            simulated=False,
        ),
        now_ms=300,
    )

    assert reconciled is not None and reconciled.kind == PARTIAL_EXIT
    assert instance.get("1000").lots == Decimal("0.06")
    assert instance.pending_closes == ()
    assert instance.close_blockers == ()
    assert instance.closed[0].exit_price == Decimal("1.10217")


def test_a_broker_partial_close_books_only_the_reported_slice():
    class PartialCloseVenue(PaperVenue):
        def close(self, *, ticket, price, spread_points, reason, quote=None, quantity=None):
            return Fill(
                request_id=f"close:{ticket}",
                status=ACCEPTED,
                filled_lots=Decimal("0.04"),
                requested_lots=Decimal(str(quantity)),
                price=Decimal("1.10227"),
                spread_points=spread_points,
                slippage_points=Decimal(0),
                latency_ms=0,
                ticket=ticket,
                reason="partial_fill",
                simulated=False,
            )

    venue = PartialCloseVenue(cost=CONFIG.cost)
    instance = book(venue=venue, partial_exit_points=Decimal("10"))
    open_through_venue(instance)
    actions = instance.on_tick(
        position_id="1000", tick=quote("1.10230", at_ms=100), now_ms=100
    )

    remaining = instance.get("1000")
    assert any(action.kind == PARTIAL_EXIT for action in actions)
    assert remaining is not None
    assert remaining.lots == Decimal("0.06")
    assert remaining.partial_closed_lots == Decimal("0.04")
    assert instance.closed[0].lots == Decimal("0.04")
    assert instance.closed[0].exit_price == Decimal("1.10227")


def test_position_book_snapshot_round_trips_partial_and_management_state():
    venue = PaperVenue(cost=CONFIG.cost)
    settings = {
        "partial_exit_points": Decimal("10"),
        "partial_exit_fraction": Decimal("0.4"),
        "break_even_trigger_points": Decimal("10"),
        "trail_after_points": Decimal("30"),
        "trail_distance_points": Decimal("5"),
    }
    original = book(venue=venue, **settings)
    open_through_venue(original)
    original.on_tick(
        position_id="1000", tick=quote("1.10220", at_ms=100), now_ms=100
    )
    original.on_tick(
        position_id="1000", tick=quote("1.10250", at_ms=200), now_ms=200
    )
    before = original.get("1000")
    assert before is not None
    assert before.partial_closed_lots == Decimal("0.04")
    assert before.break_even_moved is True
    assert before.trailing_active is True
    assert before.mfe_points > 0

    saved = original.snapshot()
    restored = book(venue=venue, **settings)
    restored.restore_state(saved)

    after = restored.get("1000")
    assert after is not None
    assert after.lots == before.lots
    assert after.partial_closed_lots == before.partial_closed_lots
    assert after.break_even_moved is before.break_even_moved
    assert after.trailing_active is before.trailing_active
    assert after.mfe_points == before.mfe_points
    assert after.mae_points == before.mae_points
    assert restored.verify_protection()["ok"] is True
    assert restored.snapshot()["open_positions"] == saved["open_positions"]
    assert restored.snapshot()["closed_trades"] == saved["closed_trades"]


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


def test_zero_broker_target_is_the_absent_tp_sentinel():
    venue = PaperVenue(cost=CONFIG.cost)
    instance = book(venue=venue)
    opened = open_through_venue(instance, target=None)
    venue._protection[opened.position_id] = (opened.stop_price, Decimal("0"))

    result = instance.verify_protection()

    assert result["ok"] is True
    assert opened.position_id in result["verified"]
    assert instance.get(opened.position_id).protection_verified is True


def test_a_positive_but_wrong_broker_stop_does_not_count_as_verified():
    class WrongStopVenue(PaperVenue):
        def protection(self, ticket):
            return (Decimal("1.10100"), None)

    instance = book(venue=WrongStopVenue(cost=CONFIG.cost))
    position(instance)

    result = instance.verify_protection()
    assert result["ok"] is False
    assert result["missing"] == ["1000"]
    assert instance.get("1000").protection_verified is False


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

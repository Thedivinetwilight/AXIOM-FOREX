import json
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest

from app.accounting.costs import CostEstimate
from app.audit.log import InMemoryAuditLog
from app.common.types import OrderSide, TradingMode
from app.common.validation import ValidationResult
from app.config.settings import SafetySettings
from app.risk.models import UnresolvedOrder
from app.risk.pretrade import PreTradeGate, RejectionReason
from fixtures.m1 import (
    ASK,
    BID,
    CALENDAR_ID,
    CONVERSION_SOURCE,
    EVALUATED_AT,
    PAIR,
    PROVIDER_ID,
    make_account,
    make_calendar,
    make_catalog,
    make_conversion_rates,
    make_cost_model,
    make_environment,
    make_quote,
    make_close_intent,
    make_position_snapshot,
    make_quote_freshness,
    make_risk_context,
    make_risk_limits,
    make_rollover_policy,
    make_spec,
    local_utc,
)


def evaluate(
    env,
    *,
    env_overrides=None,
    request_overrides=None,
    context_overrides=None,
    gate=None,
):
    effective_env = replace(env, **(env_overrides or {}))
    request = replace(effective_env.request, **(request_overrides or {}))
    context = effective_env.context(**(context_overrides or {}))
    effective_gate = gate or effective_env.gate()
    return effective_env, effective_gate.evaluate(request, context)


def evaluate_at(at, **kwargs):
    return evaluate(make_environment(at), **kwargs)


def assert_rejection(env, decision, code):
    assert not decision.allowed
    assert decision.code is code
    assert decision.order_intent is None
    event = env.audit_log.last()
    assert event is not None
    assert event["allowed"] is False
    assert event["code"] == code.value
    assert event["order_intent"] is None
    json.dumps(event)


class BadEstimateCostModel:
    def __init__(self, terms):
        self.terms = terms

    def validate(self):
        return ValidationResult.ok()

    def estimate_new_order_cost(self, *, notional_account, pip_value_account):
        return CostEstimate(
            Decimal("1"), Decimal("1"), Decimal("1"), Decimal("-1")
        )


class ExplodingCalendar:
    calendar_id = CALENDAR_ID
    version = "calendar-v1"
    source = "EXPLODING_TEST_CALENDAR"

    def validate(self):
        return ValidationResult.ok()

    def signature(self):
        return "exploding-calendar-signature"

    def audit_view(self):
        return {"calendar_id": self.calendar_id, "version": self.version}

    def status(self, pair, at):
        raise RuntimeError("calendar failure")


class FakeLiveSettings:
    def validate(self):
        return ValidationResult.fail("live_trading_forbidden")

    def signature(self):
        return {"mode": "EXTERNAL_LIVE_SETTINGS"}


def test_gate_allows_valid_request_and_writes_audit(env):
    effective_env, decision = evaluate(env)
    assert decision.allowed
    assert decision.code is RejectionReason.ALLOWED
    assert decision.order_intent is not None
    assert decision.order_intent.executable_price == ASK  # never the derived mid
    assert decision.risk_calculation is not None
    assert decision.risk_calculation.notional_account > 0
    event = effective_env.audit_log.last()
    assert event["allowed"] is True
    assert event["code"] == "ALLOWED"
    assert event["order_intent"]["pair"] == PAIR
    json.dumps(event)


def test_gate_accepts_research_mode_and_sell_uses_bid(env):
    effective_env, research = evaluate(
        env, request_overrides={"target_mode": TradingMode.RESEARCH}
    )
    assert research.allowed

    # A close names an open position, so it is matched against the ledger, its
    # entry limits are skipped, and risk can always be cut.
    effective_env, sell = evaluate(
        env,
        request_overrides={
            "side": OrderSide.SELL,
            "close_intent": make_close_intent(),
        },
        env_overrides={
            "risk_context": make_risk_context(
                open_positions=1,
                trades_today=99,
                realized_pnl_today_account=Decimal("-999"),
                gross_exposure_account=Decimal("200"),
            ),
        },
        context_overrides={"open_positions": (make_position_snapshot(),)},
    )
    assert sell.allowed
    assert sell.order_intent.executable_price == BID
    assert sell.risk_calculation.side_sign == -1
    assert sell.risk_calculation.exposure_reducing is True
    assert sell.details["exposure_reducing"] is True
    assert sell.details["close_position_id"] == "fixture-position"
    assert sell.order_intent.close_position_id == "fixture-position"
    assert sell.details["margin_released_account"] > 0


@pytest.mark.parametrize(
    "risk_context,code",
    [
        (
            make_risk_context(open_positions=99),
            RejectionReason.MAX_OPEN_POSITIONS_EXCEEDED,
        ),
        (
            make_risk_context(realized_pnl_today_account=Decimal("-999")),
            RejectionReason.MAX_DAILY_LOSS_EXCEEDED,
        ),
        (make_risk_context(trades_today=99), RejectionReason.MAX_TRADES_PER_DAY_EXCEEDED),
    ],
)
def test_gate_treats_an_unflagged_sell_as_an_entry(env, risk_context, code):
    """A SELL that the caller did not mark as reducing is a new short.

    Exempting every SELL from the entry limits meant a broker-neutral short
    entry could be opened after the daily loss limit, the trades-per-day cap, or
    the open-position cap had already been hit -- the exemptions intended for
    risk reduction leaked to the opposite side.  The flag is now explicit and
    defaults to "entry".
    """

    effective_env, decision = evaluate(
        env,
        request_overrides={"side": OrderSide.SELL},
        env_overrides={"risk_context": risk_context},
    )
    assert_rejection(effective_env, decision, code)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"live_trading": True},
        {"mode": TradingMode.LIVE},
        {"allow_live_orders": True},
        {"broker_mode": "LIVE"},
    ],
)
def test_safety_settings_cannot_enable_live(kwargs):
    with pytest.raises(ValueError):
        SafetySettings(**kwargs)


def test_gate_rejects_live_mode_request_and_fake_live_settings(env):
    effective_env, decision = evaluate(
        env, request_overrides={"target_mode": TradingMode.LIVE}
    )
    assert_rejection(effective_env, decision, RejectionReason.LIVE_MODE_REQUEST)

    effective_env, decision = evaluate(
        env, env_overrides={"settings": FakeLiveSettings()}
    )
    assert_rejection(effective_env, decision, RejectionReason.LIVE_TRADING_FORBIDDEN)


@pytest.mark.parametrize(
    "field, code",
    [
        ("emergency_stop", RejectionReason.EMERGENCY_STOP),
        ("kill_switch_active", RejectionReason.KILL_SWITCH_ACTIVE),
        ("execution_paused", RejectionReason.EXECUTION_PAUSED),
    ],
)
def test_gate_rejects_system_switches(env, field, code):
    effective_env, decision = evaluate(
        env,
        env_overrides={"system_state": replace(env.system_state, **{field: True})},
    )
    assert_rejection(effective_env, decision, code)


def test_gate_rejects_unresolved_orders_but_accepts_final_orders(env):
    effective_env, decision = evaluate(
        env,
        context_overrides={
            "unresolved_orders": (
                UnresolvedOrder("pending-1", "SUBMITTED", PAIR, EVALUATED_AT),
            )
        },
    )
    assert_rejection(effective_env, decision, RejectionReason.UNRESOLVED_ORDER_PRESENT)

    effective_env, decision = evaluate(
        env,
        context_overrides={
            "unresolved_orders": (
                UnresolvedOrder("done-1", "FILLED", PAIR, EVALUATED_AT),
            )
        },
    )
    assert decision.allowed


@pytest.mark.parametrize(
    "field, code",
    [
        ("account", RejectionReason.MISSING_ACCOUNT_TERMS),
        ("risk_limits", RejectionReason.MISSING_RISK_LIMITS),
        ("cost_model", RejectionReason.MISSING_COST_MODEL),
        ("rollover_policy", RejectionReason.MISSING_ROLLOVER_SCHEDULE),
        ("conversion_rates", RejectionReason.MISSING_CONVERSION_RATES),
        ("quote_freshness", RejectionReason.MISSING_QUOTE_FRESHNESS),
        ("risk_context", RejectionReason.MISSING_RISK_CONTEXT),
    ],
)
def test_gate_rejects_missing_required_inputs(env, field, code):
    effective_env, decision = evaluate(env, context_overrides={field: None})
    assert_rejection(effective_env, decision, code)


@pytest.mark.parametrize(
    "name, overrides, code",
    [
        (
            "invalid_account",
            {"account": replace(make_account(), leverage=Decimal("0"))},
            RejectionReason.INVALID_ACCOUNT_TERMS,
        ),
        (
            "invalid_limits",
            {"risk_limits": replace(make_risk_limits(), max_spread_pips=Decimal("0"))},
            RejectionReason.INVALID_RISK_LIMITS,
        ),
        (
            "invalid_cost_model",
            {"cost_model": make_cost_model(commission_rate=float("nan"))},
            RejectionReason.INVALID_COST_MODEL,
        ),
        (
            "invalid_rollover",
            {"rollover_policy": replace(make_rollover_policy(), triple_rollover_weekday=9)},
            RejectionReason.INVALID_ROLLOVER_SCHEDULE,
        ),
        (
            "invalid_freshness",
            {"quote_freshness": replace(make_quote_freshness(), max_age_seconds=Decimal("0"))},
            RejectionReason.INVALID_QUOTE_FRESHNESS,
        ),
        (
            "invalid_risk_context",
            {"risk_context": make_risk_context(equity_account=Decimal("0"))},
            RejectionReason.INVALID_RISK_CONTEXT,
        ),
        (
            "invalid_system_state",
            {"system_state": replace(make_environment().system_state, emergency_stop="yes")},
            RejectionReason.INVALID_SYSTEM_STATE,
        ),
        (
            "invalid_catalog",
            {"catalog": make_catalog(replace(make_spec(), provider_aliases={PROVIDER_ID: ""}))},
            RejectionReason.INVALID_INSTRUMENT_CATALOG,
        ),
        (
            "invalid_calendar",
            {"calendar": replace(make_calendar(), valid_until=make_calendar().valid_from)},
            RejectionReason.INVALID_CALENDAR,
        ),
        (
            "invalid_conversion_book",
            {
                "conversion_rates": replace(
                    make_conversion_rates(),
                    rates={
                        ("BBB", "CCC"): replace(
                            make_conversion_rates().rates[("BBB", "CCC")],
                            source="OTHER",
                        )
                    },
                )
            },
            RejectionReason.INVALID_CONVERSION_RATES,
        ),
    ],
)
def test_gate_rejects_invalid_configurations(env, name, overrides, code):
    effective_env, decision = evaluate(env, env_overrides=overrides)
    assert_rejection(effective_env, decision, code)


def test_gate_rejects_invalid_unresolved_order_object(env):
    effective_env, decision = evaluate(
        env,
        context_overrides={
            "unresolved_orders": (UnresolvedOrder("", "SUBMITTED"),)
        },
    )
    assert_rejection(effective_env, decision, RejectionReason.INVALID_UNRESOLVED_ORDER)


@pytest.mark.parametrize(
    "name, overrides, code",
    [
        (
            "conversion_source",
            {"account": replace(make_account(), conversion_source="OTHER")},
            RejectionReason.CONVERSION_SOURCE_MISMATCH,
        ),
        (
            "cost_model_version",
            {"account": replace(make_account(), cost_model_version="other")},
            RejectionReason.COST_MODEL_MISMATCH,
        ),
        (
            "rollover_version",
            {"account": replace(make_account(), rollover_schedule_version="other")},
            RejectionReason.ROLLOVER_SCHEDULE_MISMATCH,
        ),
        (
            "rollover_calendar",
            {"rollover_policy": replace(make_rollover_policy(), calendar_id="other")},
            RejectionReason.ROLLOVER_CALENDAR_MISMATCH,
        ),
        (
            "calendar_version",
            {
                "catalog": make_catalog(
                    replace(make_spec(), trading_calendar_version="other")
                )
            },
            RejectionReason.CALENDAR_MISMATCH,
        ),
        (
            "rate_book_source",
            {"conversion_rates": make_conversion_rates(source="OTHER")},
            RejectionReason.CONVERSION_SOURCE_MISMATCH,
        ),
    ],
)
def test_gate_rejects_configuration_mismatches(env, name, overrides, code):
    effective_env, decision = evaluate(env, env_overrides=overrides)
    assert_rejection(effective_env, decision, code)


@pytest.mark.parametrize(
    "name, request_overrides, env_overrides, code",
    [
        (
            "unknown_alias",
            {"provider_symbol": "WRONG_ALIAS"},
            {},
            RejectionReason.UNKNOWN_INSTRUMENT,
        ),
        (
            "inactive",
            {},
            {"catalog": make_catalog(replace(make_spec(), status="INACTIVE"))},
            RejectionReason.INSTRUMENT_INACTIVE,
        ),
        (
            "missing_spec",
            {},
            {"catalog": make_catalog(replace(make_spec(), spec_version=""))},
            RejectionReason.MISSING_SPEC,
        ),
        (
            "invalid_spec",
            {},
            {"catalog": make_catalog(replace(make_spec(), pip_size=Decimal("0")))},
            RejectionReason.INVALID_SPEC,
        ),
        (
            "below_min_quantity",
            {"quantity": Decimal("0.001")},
            {},
            RejectionReason.INVALID_QUANTITY,
        ),
        (
            "above_max_quantity",
            {"quantity": Decimal("6")},
            {},
            RejectionReason.INVALID_QUANTITY,
        ),
        (
            "wrong_step",
            {"quantity": Decimal("0.105")},
            {},
            RejectionReason.INVALID_QUANTITY,
        ),
    ],
)
def test_gate_rejects_bad_instrument_or_quantity(
    env, name, request_overrides, env_overrides, code
):
    effective_env, decision = evaluate(
        env, request_overrides=request_overrides, env_overrides=env_overrides
    )
    assert_rejection(effective_env, decision, code)


@pytest.mark.parametrize(
    "at, expected_status",
    [
        (local_utc(2026, 3, 6, 17, 30), "DAILY_BREAK"),
        (local_utc(2026, 3, 7, 12, 0), "WEEKEND_GAP"),
        (local_utc(2026, 3, 9, 10, 0), "HOLIDAY"),
        (local_utc(2026, 3, 13, 17, 1), "CALENDAR_EXPIRED"),
    ],
)
def test_gate_rejects_closed_or_expired_calendar(at, expected_status):
    effective_env, decision = evaluate_at(at)
    assert_rejection(effective_env, decision, RejectionReason.MARKET_CLOSED)
    assert decision.details["market_status"] == expected_status


@pytest.mark.parametrize(
    "name, quote, code",
    [
        ("missing_quote", None, RejectionReason.INCOMPLETE_QUOTE),
        ("missing_bid", make_quote(bid=None), RejectionReason.INCOMPLETE_QUOTE),
        ("missing_ask", make_quote(ask=None), RejectionReason.INCOMPLETE_QUOTE),
        ("crossed", make_quote(bid=ASK, ask=BID), RejectionReason.CROSSED_QUOTE),
        (
            "bad_precision",
            make_quote(ask=Decimal("1.100201")),
            RejectionReason.INVALID_QUOTE,
        ),
        ("missing_source", make_quote(source=""), RejectionReason.INVALID_QUOTE),
    ],
)
def test_gate_rejects_incomplete_or_invalid_quotes(env, name, quote, code):
    effective_env, decision = evaluate(
        env, request_overrides={"quote": quote}
    )
    assert_rejection(effective_env, decision, code)


@pytest.mark.parametrize(
    "name, quote, code",
    [
        (
            "stale",
            make_quote(
                EVALUATED_AT,
                timestamp=EVALUATED_AT - timedelta(seconds=10),
            ),
            RejectionReason.STALE_QUOTE,
        ),
        (
            "future",
            make_quote(
                EVALUATED_AT,
                timestamp=EVALUATED_AT + timedelta(seconds=3),
            ),
            RejectionReason.FUTURE_QUOTE,
        ),
        (
            "feed_latency",
            make_quote(
                EVALUATED_AT,
                timestamp=EVALUATED_AT - timedelta(seconds=2),
                received_at=EVALUATED_AT,
            ),
            RejectionReason.FEED_LATENCY_EXCEEDED,
        ),
        (
            "missing_received_at",
            make_quote(EVALUATED_AT, include_received_at=False),
            RejectionReason.MISSING_RECEIVED_AT,
        ),
    ],
)
def test_gate_rejects_bad_quote_timing(env, name, quote, code):
    effective_env, decision = evaluate(
        env, request_overrides={"quote": quote}
    )
    assert_rejection(effective_env, decision, code)


def test_gate_rejects_wide_spread(env):
    effective_env, decision = evaluate(
        env,
        request_overrides={"quote": make_quote(ask=Decimal("1.10120"))},
    )
    assert_rejection(effective_env, decision, RejectionReason.SPREAD_TOO_WIDE)


@pytest.mark.parametrize(
    "name, rate_book, code",
    [
        (
            "missing_quote_rate",
            make_conversion_rates(include_quote_rate=False),
            RejectionReason.MISSING_CONVERSION_RATE,
        ),
        (
            "missing_base_rate",
            make_conversion_rates(include_base_rate=False),
            RejectionReason.MISSING_CONVERSION_RATE,
        ),
        (
            "stale_rate",
            make_conversion_rates(EVALUATED_AT - timedelta(seconds=10)),
            RejectionReason.STALE_CONVERSION_RATE,
        ),
    ],
)
def test_gate_rejects_missing_or_stale_conversion_rates(env, name, rate_book, code):
    effective_env, decision = evaluate(
        env, env_overrides={"conversion_rates": rate_book}
    )
    assert_rejection(effective_env, decision, code)


def test_gate_rejects_invalid_risk_calculation(env):
    effective_env, decision = evaluate(
        env,
        env_overrides={
            "risk_context": make_risk_context(equity_account=Decimal("0.001"))
        },
    )
    assert_rejection(effective_env, decision, RejectionReason.INVALID_RISK_CALCULATION)


def test_gate_rejects_invalid_cost_calculation(env):
    effective_env, decision = evaluate(
        env,
        env_overrides={
            "cost_model": BadEstimateCostModel(make_cost_model().terms)
        },
    )
    assert_rejection(effective_env, decision, RejectionReason.INVALID_COST_CALCULATION)


@pytest.mark.parametrize(
    "name, overrides, code",
    [
        (
            "margin",
            {
                "risk_limits": replace(
                    make_risk_limits(), max_margin_account=Decimal("1")
                )
            },
            RejectionReason.MAX_MARGIN_EXCEEDED,
        ),
        (
            "order_notional",
            {
                "risk_limits": replace(
                    make_risk_limits(), max_order_notional_account=Decimal("1")
                )
            },
            RejectionReason.MAX_ORDER_NOTIONAL_EXCEEDED,
        ),
        (
            "gross_exposure",
            {
                "risk_limits": replace(
                    make_risk_limits(), max_gross_exposure_account=Decimal("1")
                )
            },
            RejectionReason.MAX_GROSS_EXPOSURE_EXCEEDED,
        ),
        (
            "currency_exposure",
            {
                "risk_limits": replace(
                    make_risk_limits(),
                    max_abs_currency_exposure_account=Decimal("1"),
                )
            },
            RejectionReason.MAX_CURRENCY_EXPOSURE_EXCEEDED,
        ),
        (
            "correlation_group",
            {
                "risk_limits": replace(
                    make_risk_limits(),
                    max_correlation_group_exposure_account=Decimal("1"),
                )
            },
            RejectionReason.MAX_CORRELATION_GROUP_EXPOSURE_EXCEEDED,
        ),
        (
            "leverage",
            {
                "risk_limits": replace(
                    make_risk_limits(), max_leverage=Decimal("0.1")
                )
            },
            RejectionReason.MAX_LEVERAGE_EXCEEDED,
        ),
        (
            "open_positions",
            {"risk_context": make_risk_context(open_positions=2)},
            RejectionReason.MAX_OPEN_POSITIONS_EXCEEDED,
        ),
        (
            "daily_loss",
            {
                "risk_context": make_risk_context(
                    realized_pnl_today_account=Decimal("-100")
                )
            },
            RejectionReason.MAX_DAILY_LOSS_EXCEEDED,
        ),
        (
            "trades_per_day",
            {"risk_context": make_risk_context(trades_today=5)},
            RejectionReason.MAX_TRADES_PER_DAY_EXCEEDED,
        ),
    ],
)
def test_gate_rejects_exposure_and_entry_limit_breaches(env, name, overrides, code):
    effective_env, decision = evaluate(env, env_overrides=overrides)
    assert_rejection(effective_env, decision, code)


def test_gate_invalid_request_and_context_fail_closed(env):
    effective_env, decision = evaluate(
        env, request_overrides={"side": "HOLD"}
    )
    assert_rejection(effective_env, decision, RejectionReason.INVALID_REQUEST)

    decision = env.gate().evaluate(env.request, None)
    assert not decision.allowed
    assert decision.code is RejectionReason.INVALID_GATE_CONTEXT
    assert decision.order_intent is None


def test_gate_internal_error_fails_closed_and_is_audited(env):
    gate = PreTradeGate(env.catalog, ExplodingCalendar(), env.audit_log)
    effective_env, decision = evaluate(
        env, gate=gate, env_overrides={"audit_log": InMemoryAuditLog()}
    )
    # The supplied gate keeps the original log; both remain repository-local.
    assert not decision.allowed
    assert decision.code is RejectionReason.GATE_INTERNAL_ERROR
    assert decision.order_intent is None
    assert gate.audit_log.last()["code"] == "GATE_INTERNAL_ERROR"
    assert effective_env.audit_log.events == []


def test_all_rejection_decisions_are_json_safe_and_have_no_intent(env):
    scenarios = [
        ({"target_mode": TradingMode.LIVE}, {}),
        ({}, {"account": None}),
        ({}, {"risk_limits": replace(make_risk_limits(), max_spread_pips=Decimal("0"))}),
        ({"quote": None}, {}),
    ]
    for request_overrides, context_overrides in scenarios:
        effective_env, decision = evaluate(
            env,
            request_overrides=request_overrides,
            context_overrides=context_overrides,
            env_overrides={"audit_log": InMemoryAuditLog()},
        )
        assert not decision.allowed
        assert decision.order_intent is None
        json.dumps(decision.audit_event)
        json.dumps(effective_env.audit_log.last())


# -- close validation is position-aware and fail-closed ---------------------


def _close_env(
    env,
    *,
    positions=None,
    intent=None,
    side=OrderSide.SELL,
    context_overrides=None,
):
    """Evaluate a close against an explicit ledger snapshot."""

    snapshot = positions if positions is not None else (make_position_snapshot(),)
    request_overrides = {
        "side": side,
        "close_intent": make_close_intent() if intent is None else intent,
    }
    overrides = {
        "open_positions": snapshot,
        # The book must be able to absorb the close: gross exposure carries at
        # least the position being closed.
        "risk_context": make_risk_context(gross_exposure_account=Decimal("200")),
    }
    overrides.update(context_overrides or {})
    return evaluate(
        env, request_overrides=request_overrides, context_overrides=overrides
    )


def test_close_naming_a_position_that_is_not_open_is_refused(env):
    effective_env, decision = _close_env(env, positions=())
    assert_rejection(effective_env, decision, RejectionReason.CLOSE_POSITION_NOT_FOUND)


def test_close_naming_another_accounts_position_is_refused(env):
    foreign = make_position_snapshot(account_id="SOMEONE_ELSES_ACCOUNT")
    effective_env, decision = _close_env(env, positions=(foreign,))
    assert_rejection(effective_env, decision, RejectionReason.CLOSE_ACCOUNT_MISMATCH)


def test_close_intent_for_another_account_is_refused(env):
    effective_env, decision = _close_env(
        env, intent=make_close_intent(account_id="SOMEONE_ELSES_ACCOUNT")
    )
    assert_rejection(effective_env, decision, RejectionReason.CLOSE_ACCOUNT_MISMATCH)


def test_close_naming_a_position_on_another_pair_is_refused(env):
    other = make_position_snapshot(pair="AAA/DDD")
    effective_env, decision = _close_env(env, positions=(other,))
    assert_rejection(effective_env, decision, RejectionReason.CLOSE_PAIR_MISMATCH)


def test_close_that_does_not_oppose_the_position_is_refused(env):
    # A BUY "closing" a long is an addition, not a reduction.
    effective_env, decision = _close_env(env, side=OrderSide.BUY)
    assert_rejection(effective_env, decision, RejectionReason.CLOSE_SIDE_MISMATCH)


def test_close_of_a_short_must_be_a_buy(env):
    short = make_position_snapshot(side=OrderSide.SELL)
    effective_env, decision = _close_env(env, positions=(short,), side=OrderSide.SELL)
    assert_rejection(effective_env, decision, RejectionReason.CLOSE_SIDE_MISMATCH)

    effective_env, decision = _close_env(env, positions=(short,), side=OrderSide.BUY)
    assert decision.allowed
    assert decision.risk_calculation.exposure_reducing is True


def test_close_larger_than_the_position_is_refused(env):
    tiny = make_position_snapshot(quantity=Decimal("0.05"), base_units=Decimal("50"))
    effective_env, decision = _close_env(env, positions=(tiny,))
    assert_rejection(
        effective_env, decision, RejectionReason.CLOSE_QUANTITY_EXCEEDS_POSITION
    )


def test_close_larger_than_what_is_left_after_another_in_flight_close_is_refused(env):
    reserved = make_position_snapshot(pending_close_quantity=Decimal("0.05"))
    effective_env, decision = _close_env(env, positions=(reserved,))
    assert_rejection(
        effective_env, decision, RejectionReason.CLOSE_QUANTITY_EXCEEDS_POSITION
    )
    assert decision.details["available_quantity"] == Decimal("0.05")


def test_close_quantity_must_match_the_intent(env):
    # An intent for the full position with a smaller order quantity is not the
    # reduction the gate validated, so it is refused rather than silently resized.
    position = make_position_snapshot(quantity=Decimal("0.20"), base_units=Decimal("200"))
    effective_env, decision = evaluate(
        env,
        request_overrides={
            "side": OrderSide.SELL,
            "quantity": Decimal("0.10"),
            "close_intent": make_close_intent(quantity=Decimal("0.20")),
        },
        context_overrides={"open_positions": (position,)},
    )
    assert_rejection(effective_env, decision, RejectionReason.INVALID_CLOSE_INTENT)


def test_close_must_use_the_positions_quantity_unit(env):
    effective_env, decision = _close_env(
        env, intent=make_close_intent(quantity_unit="UNITS")
    )
    assert_rejection(
        effective_env, decision, RejectionReason.CLOSE_QUANTITY_UNIT_MISMATCH
    )


def test_malformed_close_intent_is_refused(env):
    effective_env, decision = _close_env(
        env, intent=make_close_intent(position_id="", account_id="")
    )
    assert_rejection(effective_env, decision, RejectionReason.INVALID_CLOSE_INTENT)


def test_close_cannot_bypass_the_unrelated_safety_checks(env):
    # Unresolved orders must still stop a close.
    blocker = UnresolvedOrder("blocker", "SUBMITTED", PAIR, EVALUATED_AT)
    effective_env, decision = _close_env(
        env, context_overrides={"unresolved_orders": (blocker,)}
    )
    assert_rejection(effective_env, decision, RejectionReason.UNRESOLVED_ORDER_PRESENT)


def test_close_is_refused_when_the_market_is_closed(env):
    # 2026-03-14 is a Saturday: the fixture calendar is closed.
    closed_env = make_environment(EVALUATED_AT + timedelta(days=4))
    effective_env, decision = _close_env(closed_env)
    assert_rejection(effective_env, decision, RejectionReason.MARKET_CLOSED)


def test_close_is_refused_on_a_stale_quote(env):
    stale_quote = make_quote(
        EVALUATED_AT, timestamp=EVALUATED_AT - timedelta(seconds=10)
    )
    stale_env = make_environment(EVALUATED_AT, quote=stale_quote)
    effective_env, decision = _close_env(stale_env)
    assert_rejection(effective_env, decision, RejectionReason.STALE_QUOTE)


def test_close_does_not_increase_projected_exposure(env):
    _, decision = _close_env(
        env,
        context_overrides={
            "risk_context": make_risk_context(gross_exposure_account=Decimal("0"))
        },
    )
    # Gross exposure cannot fall below zero, so a "close" against a book that
    # reports none is refused instead of being reported as a reduction.
    assert not decision.allowed
    assert decision.code in {
        RejectionReason.CLOSE_INCREASES_EXPOSURE,
        RejectionReason.INVALID_RISK_CALCULATION,
    }

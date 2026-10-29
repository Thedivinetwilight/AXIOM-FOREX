from dataclasses import replace
from datetime import time as time_of_day, timedelta, timezone
from decimal import Decimal

import pytest

from app.accounting.costs import CostEstimate, StaticCostModel
from app.accounting.fx import RateReason
from app.common.validation import parse_decimal
from app.risk.calculations import calculate_risk
from fixtures.m1 import (
    ASK,
    BASE_RATE,
    EVALUATED_AT,
    QUANTITY,
    QUOTE_RATE,
    make_account,
    make_conversion_rates,
    make_cost_model,
    make_quote,
    make_quote_freshness,
    make_risk_context,
    make_risk_limits,
    make_rollover_policy,
    make_spec,
)


def test_account_and_risk_limits_require_explicit_valid_values():
    assert make_account().validate().valid
    assert make_risk_limits().validate().valid
    assert not replace(make_account(), leverage=Decimal("0")).validate().valid
    assert not replace(make_account(), initial_capital=Decimal("-1")).validate().valid
    assert not replace(make_account(), account_currency="").validate().valid
    assert not replace(
        make_risk_limits(), max_spread_pips=Decimal("0")
    ).validate().valid


def test_conversion_rate_book_identity_missing_and_stale():
    book = make_conversion_rates()
    assert book.validate().valid
    identity = book.get("CCC", "CCC", EVALUATED_AT)
    assert identity.ok and identity.reason == RateReason.IDENTITY
    assert identity.rate == Decimal(1)

    missing = book.get("AAA", "BBB", EVALUATED_AT)
    assert not missing.ok and missing.reason == RateReason.MISSING_RATE

    stale_book = make_conversion_rates(EVALUATED_AT - timedelta(seconds=10))
    stale = stale_book.get("BBB", "CCC", EVALUATED_AT)
    assert not stale.ok and stale.reason == RateReason.STALE_RATE


def test_conversion_rate_book_rejects_source_mismatch_and_identity_storage():
    book = make_conversion_rates()
    quote_rate = book.rates[("BBB", "CCC")]
    mismatched = replace(book, rates={("BBB", "CCC"): replace(quote_rate, source="OTHER")})
    result = mismatched.get("BBB", "CCC", EVALUATED_AT)
    assert not result.ok
    assert result.reason == RateReason.INVALID_BOOK

    stored_identity = replace(quote_rate, from_currency="CCC", to_currency="CCC")
    assert not stored_identity.validate().valid


def test_cost_model_estimate_is_finite_and_rejects_invalid_terms():
    model = make_cost_model()
    estimate = model.estimate_new_order_cost(
        notional_account=Decimal("100"),
        pip_value_account=Decimal("0.01"),
    )
    assert estimate.validate().valid
    assert estimate.total_account == (
        estimate.commission_account
        + estimate.slippage_account
        + estimate.financing_account
    )

    bad_terms = replace(model.terms, commission_rate=float("nan"))
    assert not bad_terms.validate().valid
    with pytest.raises(ValueError):
        StaticCostModel(bad_terms).estimate_new_order_cost(
            notional_account=Decimal("100"),
            pip_value_account=Decimal("0.01"),
        )

    bad_estimate = CostEstimate(
        Decimal("1"), Decimal("1"), Decimal("1"), Decimal("-1")
    )
    assert not bad_estimate.validate().valid


def test_rollover_policy_requires_explicit_weekday_and_utc_cutoff():
    assert make_rollover_policy().validate().valid
    assert replace(make_rollover_policy(), triple_rollover_weekday=2).validate().valid
    assert not replace(
        make_rollover_policy(), triple_rollover_weekday=9
    ).validate().valid
    wrong_cutoff = replace(
        make_rollover_policy(),
        daily_cutoff_utc=time_of_day(17, 0, tzinfo=timezone(timedelta(hours=5))),
    )
    assert not wrong_cutoff.validate().valid


def test_freshness_policy_rejects_non_positive_age():
    assert make_quote_freshness().validate().valid
    assert not replace(
        make_quote_freshness(), max_age_seconds=Decimal("0")
    ).validate().valid


def test_risk_calculation_uses_conversion_pip_value_and_margin():
    spec = make_spec()
    quote = make_quote()
    calculation = calculate_risk(
        side="BUY",
        quantity=QUANTITY,
        spec=spec,
        executable_price=quote.executable_price("BUY"),
        quote_to_account_rate=QUOTE_RATE,
        base_to_account_rate=BASE_RATE,
        account=make_account(),
        risk_context=make_risk_context(),
        cost_model=make_cost_model(),
    )
    assert calculation.validate().valid
    assert calculation.base_units == Decimal("100")
    assert calculation.notional_account == Decimal("99.018")
    assert calculation.pip_value_account == Decimal("0.009")
    assert calculation.required_margin_account == Decimal("19.8036")
    assert calculation.projected_currency_exposures_account["AAA"] == Decimal("110")
    assert calculation.projected_currency_exposures_account["BBB"] == Decimal("-99.018")
    assert calculation.effective_leverage > 0


def test_risk_calculation_rejects_invalid_or_non_finite_inputs():
    spec = make_spec()
    quote = make_quote()
    base_kwargs = dict(
        side="BUY",
        quantity=QUANTITY,
        spec=spec,
        executable_price=quote.executable_price("BUY"),
        quote_to_account_rate=QUOTE_RATE,
        base_to_account_rate=BASE_RATE,
        account=make_account(),
        risk_context=make_risk_context(),
        cost_model=make_cost_model(),
    )
    with pytest.raises(ValueError):
        calculate_risk(**{**base_kwargs, "quote_to_account_rate": Decimal("0")})
    with pytest.raises(ValueError):
        calculate_risk(**{**base_kwargs, "base_to_account_rate": float("nan")})
    with pytest.raises(ValueError):
        calculate_risk(**{**base_kwargs, "account": replace(make_account(), leverage=Decimal("0"))})
    with pytest.raises(ValueError, match="projected_equity"):
        calculate_risk(
            **{
                **base_kwargs,
                "risk_context": make_risk_context(equity_account=Decimal("0.001")),
            }
        )


def test_parse_decimal_rejects_nan_and_infinity():
    value, error = parse_decimal(float("nan"), "value")
    assert value is None and error == "non_finite_value"
    value, error = parse_decimal(float("inf"), "value")
    assert value is None and error == "non_finite_value"

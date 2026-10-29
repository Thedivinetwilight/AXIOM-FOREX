"""Synthetic multi-pair instrument/calendar fixtures for research evaluation.

These are *instrument and calendar* fixtures only: three pairs that share the
AAA, BBB, and CCC currencies so portfolio exposure genuinely overlaps, with
deliberately asynchronous sessions.  No market data lives here; the evaluation
tests import deterministic user-supplied CSV data for these specs.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from app.accounting.fx import ConversionRate, ConversionRateBook
from app.accounting.rollover import RolloverPolicy
from app.calendar.trading import CalendarWindow, ExplicitTradingCalendar
from app.config.settings import AccountProfile, RiskLimits
from app.data.models import QuoteFreshness
from app.universe.specs import (
    CurrencyPairSpec,
    InstrumentCatalog,
    InstrumentStatus,
    QuantityUnit,
)

from axiom_forex.risk.aggregate import AggregateRiskLimits, CorrelationMatrix

UTC = timezone.utc
NEW_YORK = ZoneInfo("America/New_York")

PAIR_A = "AAA/BBB"
PAIR_B = "AAA/CCC"
PAIR_C = "BBB/CCC"
MULTI_PAIRS = (PAIR_A, PAIR_B, PAIR_C)
#: A fourth currency is the account currency so that no pair quotes in it.  A
#: pair whose quote currency *is* the account currency would need a CCC/CCC
#: identity rate in the profile, which is not a real conversion.
ACCOUNT_CURRENCY = "DDD"
CALENDAR_ID = "synthetic-multi-pair-24x5-v1"
CALENDAR_VERSION = "calendar-multi-v1"
CONVERSION_SOURCE = "SYNTHETIC_MULTI_FX_SOURCE"
CORRELATION_SOURCE = "SYNTHETIC_MULTI_CORRELATION_SOURCE"
PROVIDER_ID = "SYNTHETIC_FEED"


def local_utc(
    year: int, month: int, day: int, hour: int = 0, minute: int = 0
) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=NEW_YORK).astimezone(UTC)


def _windows(
    *,
    close_hour: int,
    first_day: int = 6,
    last_day: int = 13,
) -> tuple[CalendarWindow, ...]:
    """Weekday sessions with an explicit per-pair break and close hour."""

    windows: list[CalendarWindow] = []
    day = first_day
    while day <= last_day:
        weekday = datetime(2026, 3, day).weekday()
        if weekday < 5:
            windows.append(
                CalendarWindow(
                    start=local_utc(2026, 3, day, 0, 0),
                    end=local_utc(2026, 3, day, close_hour, 0),
                    label=f"d{day}_single",
                )
            )
        day += 1
    return tuple(windows)


def _windows_with_midnight(
    break_start: int, break_end: int
) -> tuple[CalendarWindow, ...]:
    """Split weekday session: 00:00-break, break_end-next midnight."""

    windows: list[CalendarWindow] = []
    day = 6
    while day <= 13:
        if datetime(2026, 3, day).weekday() < 5:
            windows.append(
                CalendarWindow(
                    start=local_utc(2026, 3, day, 0, 0),
                    end=local_utc(2026, 3, day, break_start, 0),
                    label=f"d{day}_core",
                )
            )
            windows.append(
                CalendarWindow(
                    start=local_utc(2026, 3, day, break_end, 0),
                    end=local_utc(2026, 3, day + 1, 0, 0),
                    label=f"d{day}_late",
                )
            )
        day += 1
    return tuple(windows)


def make_multi_pair_calendar(
    *, version: str = CALENDAR_VERSION, holidays: dict[str, frozenset[date]] | None = None
) -> ExplicitTradingCalendar:
    """Asynchronous sessions: each pair closes at a different local hour."""

    windows_by_pair = {
        # PAIR_A trades 00:00-17:00 and 18:00-24:00 local.
        PAIR_A: _windows_with_midnight(17, 18),
        # PAIR_B has an earlier daily break (15:00-16:00 local).
        PAIR_B: _windows_with_midnight(15, 16),
        # PAIR_C stops for the day at 21:00 local and has no evening session.
        PAIR_C: _windows(close_hour=21),
    }
    holidays_by_pair = holidays or {pair: frozenset() for pair in MULTI_PAIRS}
    return ExplicitTradingCalendar(
        calendar_id=CALENDAR_ID,
        version=version,
        source="LOCAL_FIXTURE_NOT_A_BROKER_CALENDAR",
        timezone_name="America/New_York",
        valid_from=local_utc(2026, 3, 6, 0, 0),
        valid_until=local_utc(2026, 3, 14, 0, 0),
        windows_by_pair=windows_by_pair,
        holidays_by_pair=holidays_by_pair,
    )


def make_pair_spec(pair: str, **overrides: Any) -> CurrencyPairSpec:
    values: dict[str, Any] = {
        "pair": pair,
        "pip_size": Decimal("0.0001"),
        "price_tick_size": Decimal("0.00001"),
        "quote_precision": 5,
        "contract_size": Decimal("1000"),
        "min_quantity": Decimal("0.01"),
        "quantity_step": Decimal("0.01"),
        "max_quantity": Decimal("50"),
        "quantity_unit": QuantityUnit.LOTS,
        "margin_multiplier": Decimal("1"),
        "correlation_group": f"GROUP_{pair.split('/')[0]}",
        "trading_calendar_id": CALENDAR_ID,
        "trading_calendar_version": CALENDAR_VERSION,
        "status": InstrumentStatus.ACTIVE,
        "spec_version": "spec-multi-v1",
        "source": "SYNTHETIC_INSTRUMENT_SOURCE",
        "provider_aliases": {PROVIDER_ID: pair.replace("/", "_")},
    }
    values.update(overrides)
    return CurrencyPairSpec(**values)


def make_multi_pair_catalog() -> InstrumentCatalog:
    return InstrumentCatalog(tuple(make_pair_spec(pair) for pair in MULTI_PAIRS))


def make_multi_pair_account(**overrides: Any) -> AccountProfile:
    values: dict[str, Any] = {
        "account_id": "SYNTHETIC_RESEARCH_ACCOUNT",
        "account_currency": ACCOUNT_CURRENCY,
        "leverage": Decimal("10"),
        "initial_capital": Decimal("100000"),
        "conversion_source": CONVERSION_SOURCE,
        "cost_model_id": "cost-model-v1",
        "cost_model_version": "cost-v1",
        "rollover_schedule_id": "multi-pair-rollover-schedule-v1",
        "rollover_schedule_version": "rollover-policy-v1",
    }
    values.update(overrides)
    return AccountProfile(**values)


def make_multi_pair_conversion_rates(
    *, at: datetime | None = None
) -> ConversionRateBook:
    """Explicit conversions covering every currency in the two pairs' terms."""

    timestamp = at or datetime(2026, 3, 6, 0, 0, tzinfo=UTC)
    freshness = QuoteFreshness(
        policy_id="multi-pair-rate-freshness",
        version="rate-freshness-v1",
        max_age_seconds=Decimal("864000"),
        max_future_skew_seconds=Decimal("1"),
        max_feed_latency_seconds=Decimal("1"),
    )
    # Triangular consistency is deliberate:
    # AAA/BBB = (AAA/DDD)/(BBB/DDD) and AAA/CCC = (AAA/DDD)/(CCC/DDD).
    # An inconsistent rate set would create valuation drift that looks like P&L.
    rates = {
        ("AAA", ACCOUNT_CURRENCY): ConversionRate(
            "AAA",
            ACCOUNT_CURRENCY,
            Decimal("2.4"),
            timestamp,
            CONVERSION_SOURCE,
            "aaa-account",
        ),
        ("BBB", ACCOUNT_CURRENCY): ConversionRate(
            "BBB",
            ACCOUNT_CURRENCY,
            Decimal("2.0"),
            timestamp,
            CONVERSION_SOURCE,
            "bbb-account",
        ),
        ("CCC", ACCOUNT_CURRENCY): ConversionRate(
            "CCC",
            ACCOUNT_CURRENCY,
            Decimal("1.0"),
            timestamp,
            CONVERSION_SOURCE,
            "ccc-account",
        ),
    }
    return ConversionRateBook(
        source=CONVERSION_SOURCE, freshness=freshness, rates=rates
    )


def make_multi_pair_rollover_policy() -> RolloverPolicy:
    return RolloverPolicy(
        schedule_id="multi-pair-rollover-schedule-v1",
        version="rollover-policy-v1",
        daily_cutoff_utc=datetime.min.time().replace(hour=21),
        triple_rollover_weekday=None,
        calendar_id=CALENDAR_ID,
        source="SYNTHETIC_ROLLOVER_SOURCE",
    )


def make_multi_pair_correlation_matrix() -> CorrelationMatrix:
    values: dict[tuple[str, str], Decimal] = {}
    for pair in MULTI_PAIRS:
        values[(pair, pair)] = Decimal(1)
    values[(PAIR_A, PAIR_B)] = Decimal("0.7")
    values[(PAIR_B, PAIR_A)] = Decimal("0.7")
    values[(PAIR_A, PAIR_C)] = Decimal("0.5")
    values[(PAIR_C, PAIR_A)] = Decimal("0.5")
    values[(PAIR_B, PAIR_C)] = Decimal("0.6")
    values[(PAIR_C, PAIR_B)] = Decimal("0.6")
    return CorrelationMatrix(
        pairs=frozenset(MULTI_PAIRS),
        values=values,
        source=CORRELATION_SOURCE,
        version="correlation-multi-v1",
    )


def make_multi_pair_aggregate_limits(**overrides: Any) -> AggregateRiskLimits:
    values: dict[str, Any] = {
        "max_gross_exposure_account": Decimal("400000"),
        "max_adjusted_exposure_account": Decimal("400000"),
        "max_abs_currency_exposure_account": Decimal("250000"),
        "version": "aggregate-limits-multi-v1",
    }
    values.update(overrides)
    return AggregateRiskLimits(**values)


def make_multi_pair_risk_limits() -> RiskLimits:
    from fixtures.m1 import make_risk_limits

    return make_risk_limits()


def pair_reference_prices() -> dict[str, Decimal]:
    """Reference mid levels consistent with the conversion-rate triangle."""

    return {
        PAIR_A: Decimal("1.20000"),
        PAIR_B: Decimal("2.40000"),
        PAIR_C: Decimal("2.00000"),
    }


def make_multi_pair_cost_terms():
    from fixtures.m1 import make_cost_model

    return make_cost_model().terms


def make_multi_pair_swap_cards() -> dict[str, Any]:
    from axiom_forex.portfolio.rollover import SwapRateCard

    return {
        pair: SwapRateCard(
            pair=pair,
            long_rate=Decimal("0.00001"),
            short_rate=Decimal("-0.00002"),
            source="SYNTHETIC_SWAP_SOURCE",
            version="swap-multi-v1",
        )
        for pair in MULTI_PAIRS
    }


def session_specs():
    """Explicit research session buckets used by evaluation reports."""

    from axiom_forex.evaluation import SessionSpec

    return (
        SessionSpec("ASIA", start_seconds=0, end_seconds=7 * 3600),
        SessionSpec("LONDON", start_seconds=7 * 3600, end_seconds=12 * 3600),
        SessionSpec("NEW_YORK", start_seconds=12 * 3600, end_seconds=21 * 3600),
        SessionSpec("LATE", start_seconds=21 * 3600, end_seconds=0),
    )


def window_bounds() -> tuple[datetime, datetime]:
    return local_utc(2026, 3, 6, 0, 0), local_utc(2026, 3, 11, 0, 0)


def make_multi_pair_import_profile(pair: str):
    """Synthetic FIXTURE provider profile compatible with the multi-pair calendar.

    A profile declares exactly one instrument spec, so the research workflow
    validates each pair's import against its own explicit profile.  The class is
    FIXTURE and the import data class is user-supplied: nothing here claims a
    real provider, and production data is refused against this profile.

    Only pair-specific and calendar-specific terms are overridden; every other
    term stays exactly as the reviewed offline fixture profile declares it.
    """

    from axiom_forex.profiles.fixtures import _json_safe, build_fixture_profile_payload
    from axiom_forex.profiles.loader import load_provider_profile

    payload = build_fixture_profile_payload()
    terms = payload["terms"]
    spec = make_pair_spec(pair)
    calendar = make_multi_pair_calendar()
    provider_symbol = spec.alias_for(PROVIDER_ID) or pair
    account_currency = ACCOUNT_CURRENCY

    calendar_terms = {
        "calendar_id": calendar.calendar_id,
        "version": calendar.version,
        "signature": calendar.signature(),
        "timezone": calendar.timezone_name,
        "valid_from": _json_safe(calendar.valid_from),
        "valid_until": _json_safe(calendar.valid_until),
        "holidays": {
            item: sorted(day.isoformat() for day in days)
            for item, days in calendar.holidays_by_pair.items()
        },
        "windows": {
            item: [
                {
                    "start": _json_safe(window.start),
                    "end": _json_safe(window.end),
                    "label": window.label,
                }
                for window in windows
            ]
            for item, windows in calendar.windows_by_pair.items()
        },
    }

    market_calendar = dict(terms["market.calendar"]["value"])
    market_calendar.update(calendar_terms)
    market_calendar.update(
        {
            "daily_break_policy": "EXPLICIT_DAILY_BREAK_WINDOWS",
            "dst_transition_policy": "UTC_WINDOWS_FROM_DST_AWARE_CALENDAR",
            "holiday_source": calendar.source,
            "weekend_gap_policy": "EXPLICIT_WEEKEND_GAP",
            "instrument_availability": {item: "ACTIVE" for item in MULTI_PAIRS},
        }
    )

    trading_hours = dict(terms["broker.trading_hours"]["value"])
    trading_hours.update(calendar_terms)
    trading_hours.update(
        {
            "calendar_version": calendar.version,
            "calendar_signature": calendar.signature(),
            "daily_break_policy": market_calendar["daily_break_policy"],
            "dst_transition_policy": market_calendar["dst_transition_policy"],
            "holiday_source": market_calendar["holiday_source"],
            "weekend_gap_policy": market_calendar["weekend_gap_policy"],
            "dst_transitions": market_calendar["dst_transitions"],
        }
    )

    sizing = dict(terms["broker.sizing"]["value"])
    sizing.update(
        {
            "pair": pair,
            "pip_size": str(spec.pip_size_decimal()),
            "tick_size": str(spec.price_tick_size_decimal()),
            "quote_precision": spec.quote_precision,
            "contract_size": str(spec.contract_size_decimal()),
            "min_quantity": str(spec.min_quantity_decimal()),
            "quantity_step": str(spec.quantity_step_decimal()),
            "max_quantity": str(spec.max_quantity_decimal()),
            "spec_version": spec.spec_version,
            "quantity_unit": spec.quantity_unit_value,
            "margin_multiplier": str(spec.margin_multiplier_decimal()),
            "supported_pairs": [pair],
            "provider_symbols": {pair: provider_symbol},
            "status": spec.status_value,
        }
    )

    account_terms = dict(terms["broker.account_currency"]["value"])
    account_terms["account_currency"] = account_currency
    required = sorted(
        {
            (currency, account_currency)
            for pair_name in MULTI_PAIRS
            for currency in pair_name.split("/")
            if currency != account_currency
        }
    )
    existing = [tuple(str(code) for code in item) for item in account_terms["conversion_rate_pairs"]]
    for item in required:
        if item not in existing:
            existing.append(item)
    account_terms["conversion_rate_pairs"] = [list(item) for item in existing]
    timestamps = dict(account_terms.get("rate_timestamps", {}))
    sources = dict(account_terms.get("rate_sources", {}))
    stamp = _json_safe(calendar.valid_from)
    for from_currency, to_currency in required:
        key = f"{from_currency}/{to_currency}"
        timestamps.setdefault(key, stamp)
        sources.setdefault(key, CONVERSION_SOURCE)
    account_terms["rate_timestamps"] = timestamps
    account_terms["rate_sources"] = sources

    overrides = {
        "market.supported_pairs": [pair],
        "market.provider_symbols": {pair: provider_symbol},
        "market.supported_timeframes": ["1m"],
        "market.instrument_spec": _json_safe(spec.audit_view()),
        "market.calendar": _json_safe(market_calendar),
        "market.history_bounds": {
            "start": _json_safe(calendar.valid_from),
            "end": _json_safe(calendar.valid_until),
        },
        "broker.trading_hours": _json_safe(trading_hours),
        "broker.sizing": _json_safe(sizing),
        "broker.account_currency": _json_safe(account_terms),
    }
    provenance = {
        "source": "LOCAL_MULTI_PAIR_FIXTURE",
        "source_version": "multi-pair-fixture-v1",
        "effective_date": _json_safe(calendar.valid_from),
        "value_class": "FIXTURE",
        "expires_at": _json_safe(calendar.valid_until),
        "documented_at": _json_safe(calendar.valid_from),
        "review_by": _json_safe(calendar.valid_until),
    }
    for key, value in overrides.items():
        if key not in terms:
            raise RuntimeError(f"fixture_profile_missing_term:{key}")
        terms[key]["value"] = value
        terms[key]["provenance"] = dict(provenance)
    payload["profile"]["profile_id"] = f"multi-pair-fixture-{pair.replace('/', '-')}"
    return load_provider_profile(payload)


def make_multi_pair_import_environment():
    """Catalog manager, calendar, freshness, and profiles for offline imports."""

    from axiom_forex.profiles.loader import load_provider_profile
    from axiom_forex.profiles.paths import DEFAULT_FIXTURE_PROFILE_PATH
    from axiom_forex.universe.manager import InstrumentCatalogManager

    from app.data.models import QuoteFreshness
    from decimal import Decimal as _Decimal

    manager = InstrumentCatalogManager.from_specs(
        list(make_multi_pair_catalog().specs),
        source="SYNTHETIC_INSTRUMENT_SOURCE",
        version="catalog-multi-v1",
    )
    base_profile = load_provider_profile(DEFAULT_FIXTURE_PROFILE_PATH)
    freshness = QuoteFreshness(
        policy_id="multi-pair-freshness",
        version="freshness-multi-v1",
        max_age_seconds=_Decimal("864000"),
        max_future_skew_seconds=_Decimal("1"),
        max_feed_latency_seconds=_Decimal("1"),
    )
    return {
        "catalog_manager": manager,
        "calendar": make_multi_pair_calendar(),
        "quote_freshness": freshness,
        "base_profile": base_profile,
        "profiles": {pair: make_multi_pair_import_profile(pair) for pair in MULTI_PAIRS},
    }


def import_pair_csv(text: str, *, pair: str, data_class: str = "USER_SUPPLIED_DEMO", timeframe: str = "1m", valid_from=None, valid_until=None, now=None):
    """Run the real canonical importer for one pair; returns its ImportResult."""

    from axiom_forex.ingest import ForexDataImporter, ImportRequest
    from axiom_forex.profiles.validation import validate_provider_profile

    environment = make_multi_pair_import_environment()
    profile = environment["profiles"][pair]
    calendar = environment["calendar"]
    importer = ForexDataImporter(
        catalog_manager=environment["catalog_manager"],
        calendar=calendar,
        quote_freshness=environment["quote_freshness"],
        profile=profile,
        profile_passed=bool(
            validate_provider_profile(profile, evaluated_at=now or datetime.now(timezone.utc)).passed
        ),
        now=now,
    )
    request = ImportRequest(
        pair=pair,
        timeframe=timeframe,
        data_class=data_class,
        source="SYNTHETIC_TEST_CSV",
        source_version="v1",
        valid_from=valid_from or calendar.valid_from,
        valid_until=valid_until or local_utc(2026, 3, 11, 0, 0),
    )
    return importer.import_text(text, request)


def make_multi_pair_evaluation_config(**overrides):
    """Evaluation config wired to the multi-pair fixture terms."""

    from axiom_forex.evaluation import EvaluationConfig
    from axiom_forex.strategies.examples import SmaCrossStrategy

    from fixtures.m1 import make_cost_model

    cost_model = make_cost_model()
    values: dict[str, Any] = {
        "strategy_id": "example_sma_cross",
        "strategy_factory": lambda pair: SmaCrossStrategy(fast_period=2, slow_period=4),
        "account": make_multi_pair_account(),
        "conversion_rates": make_multi_pair_conversion_rates(),
        "cost_terms": cost_model.terms,
        "rollover_policy": make_multi_pair_rollover_policy(),
        "swap_cards": make_multi_pair_swap_cards(),
        "correlation_matrix": make_multi_pair_correlation_matrix(),
        "aggregate_limits": make_multi_pair_aggregate_limits(),
        "grid_seconds": 60,
        "quantity_by_pair": {pair: Decimal("1") for pair in MULTI_PAIRS},
        "max_open_positions": 3,
        "min_bars_per_pair": 30,
        "walk_forward_train": 40,
        "walk_forward_validation": 10,
        "walk_forward_test": 10,
        "min_folds": 1,
        "min_trades": 1,
        "sessions": session_specs(),
    }
    values.update(overrides)
    return EvaluationConfig(**values)

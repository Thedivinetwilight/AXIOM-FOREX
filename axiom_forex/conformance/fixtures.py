"""Deterministic offline conformance fixtures and environment assembly.

The environment is built from either an existing platform's explicit fixture
values or the M1 synthetic fixtures.  It never reads credentials, environment
variables, runtime settings, network responses, or account state.
"""

from __future__ import annotations

from dataclasses import dataclass, fields, replace
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from app.accounting.costs import CostModelTerms
from app.accounting.fx import ConversionRateBook
from app.accounting.rollover import RolloverPolicy
from app.calendar.trading import TradingCalendar
from app.common.types import ExecutionMode
from app.config.settings import AccountProfile, RiskLimits
from app.data.models import ForexQuote, ForexQuoteBar, QuoteFreshness
from app.execution.broker import BrokerCapabilities
from app.universe.specs import CurrencyPairSpec, InstrumentCatalog

from axiom_forex.data.fixtures import (
    QUOTE_SOURCE,
    build_synthetic_bars,
    synthetic_bar_payloads,
    synthetic_quote_payloads,
)
from axiom_forex.data.normalization import DataNormalizer

from fixtures.m1 import (
    ASK,
    BID,
    EVALUATED_AT,
    PAIR,
    PROVIDER_ID,
    PROVIDER_SYMBOL,
    local_utc,
    make_account,
    make_calendar,
    make_capabilities,
    make_catalog,
    make_conversion_rates,
    make_cost_model,
    make_quote,
    make_quote_freshness,
    make_risk_limits,
    make_rollover_policy,
    make_spec,
)

from .adapters import (
    DeterministicBrokerAdapter,
    DeterministicMarketDataAdapter,
    MarketDataScenario,
)
from .contracts import (
    LOCAL_SIMULATION_MODE,
    NO_RESUBMIT_POLICY,
    REQUIRED_PROVIDER_VALUES,
    BrokerConformanceCapabilities,
    ConformanceContracts,
    MarketDataCapabilities,
)

CONFORMANCE_BROKER_ID = "SYNTHETIC_CONFORMANCE_BROKER"
CONFORMANCE_PROVIDER_SOURCE = "SYNTHETIC_CONFORMANCE_FEED"
SWAP_SOURCE = "SYNTHETIC_CONFORMANCE_SWAP_SOURCE"
SWAP_VERSION = "swap-fixture-v1"


def _required_value(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, time):
        return value.isoformat()
    if isinstance(value, frozenset):
        return sorted(str(item) for item in value)
    if isinstance(value, tuple):
        return [_required_value(item) for item in value]
    if isinstance(value, Mapping):
        return {str(key): _required_value(item) for key, item in value.items()}
    return value


def _calendar_view(calendar: TradingCalendar, pair: str, name: str, at: datetime) -> dict[str, Any]:
    state = calendar.status(pair, at)
    return {
        "scenario": name,
        "at": at,
        "pair": pair,
        "status": state.status.value,
        "is_open": state.is_open,
        "reason": state.reason,
        "calendar_id": state.calendar_id,
        "version": state.version,
        "signature": state.signature,
    }


def _platform_value(platform: Any | None, name: str, fallback: Any) -> Any:
    return getattr(platform, name, fallback) if platform is not None else fallback


@dataclass(frozen=True)
class ConformanceEnvironment:
    contracts: ConformanceContracts
    market_adapter: DeterministicMarketDataAdapter
    broker_adapter: DeterministicBrokerAdapter
    m1_broker_capabilities: BrokerCapabilities
    catalog: InstrumentCatalog
    calendar: TradingCalendar
    quote_freshness: QuoteFreshness
    account: AccountProfile
    risk_limits: RiskLimits
    cost_terms: CostModelTerms
    rollover_policy: RolloverPolicy
    conversion_rates: ConversionRateBook
    spec: CurrencyPairSpec
    bars: tuple[ForexQuoteBar, ...]
    quotes: tuple[ForexQuote, ...]
    raw_bar_payloads: tuple[Mapping[str, Any], ...]
    raw_quote_payloads: tuple[Mapping[str, Any], ...]
    normalization_errors: tuple[str, ...]
    evaluated_at: datetime
    provider_symbol: str
    required_values: Mapping[str, Any]

    @property
    def market_contract(self) -> MarketDataCapabilities:
        return self.contracts.market

    @property
    def broker_contract(self) -> BrokerConformanceCapabilities:
        return self.contracts.broker

    def scenario_view(self) -> dict[str, Any]:
        return {
            "market": self.market_adapter.scenarios.scenario_view(
                spec=self.spec,
                freshness=self.quote_freshness,
                calendar=self.calendar,
            ),
            "broker": self.broker_adapter.scenario_view(),
            "normalization": {
                "bar_payload_count": len(self.raw_bar_payloads),
                "quote_payload_count": len(self.raw_quote_payloads),
                "normalized_bar_count": len(self.bars),
                "normalized_quote_count": len(self.quotes),
                "errors": list(self.normalization_errors),
            },
        }


def _resolve_platform_parts(
    platform: Any | None,
) -> tuple[
    InstrumentCatalog,
    TradingCalendar,
    QuoteFreshness,
    AccountProfile,
    RiskLimits,
    CostModelTerms,
    RolloverPolicy,
    ConversionRateBook,
    tuple[ForexQuoteBar, ...],
    str,
    str,
]:
    spec = make_spec()
    catalog = _platform_value(platform, "catalog", make_catalog(spec))
    calendar = _platform_value(platform, "calendar", make_calendar())
    quote_freshness = _platform_value(platform, "quote_freshness", make_quote_freshness())
    account = _platform_value(platform, "account", make_account())
    risk_limits = _platform_value(platform, "risk_limits", make_risk_limits())
    cost_model = _platform_value(platform, "forex_cost_model", make_cost_model())
    cost_terms = getattr(cost_model, "terms", make_cost_model().terms)
    rollover_policy = _platform_value(
        platform, "rollover_policy", make_rollover_policy(triple_rollover_weekday=2)
    )
    platform_bars = tuple(_platform_value(platform, "bars", ()))
    if not platform_bars:
        platform_bars = build_synthetic_bars(
            spec=catalog.get(PAIR) or spec,
            calendar=calendar,
            start_at=local_utc(2026, 3, 10, 10, 0),
            count=12,
        )
    fallback_rate_timestamp = (
        platform_bars[-1].end_at - timedelta(seconds=1)
        if platform_bars
        else EVALUATED_AT - timedelta(seconds=1)
    )
    conversion_rates = _platform_value(
        platform,
        "conversion_rates",
        make_conversion_rates(fallback_rate_timestamp),
    )
    execution_config = _platform_value(platform, "execution_config", None)
    provider_id = str(getattr(execution_config, "provider_id", PROVIDER_ID) or PROVIDER_ID)
    provider_symbol = str(
        getattr(execution_config, "provider_symbol", PROVIDER_SYMBOL) or PROVIDER_SYMBOL
    )
    return (
        catalog,
        calendar,
        quote_freshness,
        account,
        risk_limits,
        cost_terms,
        rollover_policy,
        conversion_rates,
        platform_bars,
        provider_id,
        provider_symbol,
    )


def build_conformance_environment(platform: Any | None = None) -> ConformanceEnvironment:
    (
        catalog,
        calendar,
        quote_freshness,
        account,
        risk_limits,
        cost_terms,
        rollover_policy,
        conversion_rates,
        source_bars,
        provider_id,
        provider_symbol,
    ) = _resolve_platform_parts(platform)
    spec = catalog.get(PAIR) or make_spec()
    pair = spec.canonical_pair or PAIR
    evaluated_at = source_bars[-1].end_at if source_bars else EVALUATED_AT

    raw_bar_payloads = synthetic_bar_payloads(source_bars, provider_symbol=provider_symbol)
    raw_quote_payloads = synthetic_quote_payloads(source_bars, provider_symbol=provider_symbol)
    normalizer = DataNormalizer(catalog, provider_id)
    bar_results = normalizer.normalize_bars(raw_bar_payloads)
    quote_results = normalizer.normalize_quotes(raw_quote_payloads)
    bars = tuple(
        result.value
        for result in bar_results
        if isinstance(result.value, ForexQuoteBar)
    )
    quotes = tuple(
        result.value for result in quote_results if isinstance(result.value, ForexQuote)
    )
    normalization_errors = tuple(
        f"bar:{index}:{','.join(result.errors)}"
        for index, result in enumerate(bar_results)
        if not result.ok
    ) + tuple(
        f"quote:{index}:{','.join(result.errors)}"
        for index, result in enumerate(quote_results)
        if not result.ok
    )

    scenario_bars = build_synthetic_bars(
        spec=spec,
        calendar=calendar,
        start_at=local_utc(2026, 3, 10, 10, 0),
        count=8,
    )
    gap_bars = scenario_bars[:4] + scenario_bars[5:]
    incomplete_bar = replace(scenario_bars[-1], complete=False)

    fresh_timestamp = evaluated_at - timedelta(seconds=1)
    fresh_received_at = fresh_timestamp + timedelta(milliseconds=100)
    stale_age_seconds = max(
        Decimal(30),
        Decimal(str(quote_freshness.max_age_seconds)) + Decimal(5),
    )
    stale_timestamp = evaluated_at - timedelta(seconds=float(stale_age_seconds))
    stale_received_at = stale_timestamp + timedelta(milliseconds=100)
    bid_ask_quote = make_quote(
        evaluated_at,
        timestamp=fresh_timestamp,
        received_at=fresh_received_at,
        bid=BID,
        ask=ASK,
        source=QUOTE_SOURCE,
    )
    stale_quote = make_quote(
        evaluated_at,
        timestamp=stale_timestamp,
        received_at=stale_received_at,
        bid=BID,
        ask=ASK,
        source=QUOTE_SOURCE,
    )
    crossed_quote = make_quote(
        evaluated_at,
        timestamp=fresh_timestamp,
        received_at=fresh_received_at,
        bid=ASK,
        ask=BID,
        source=QUOTE_SOURCE,
    )
    corrected_quote = replace(
        bid_ask_quote,
        quote_id="conformance-corrected-quote",
        bid=BID + spec.price_tick_size_decimal(),
        ask=ASK + spec.price_tick_size_decimal(),
    )

    try:
        zone = ZoneInfo(calendar.timezone_name)
    except Exception:
        zone = timezone.utc
    dst_before_local = datetime(2026, 3, 8, 1, 30, tzinfo=zone)
    dst_after_local = datetime(2026, 3, 8, 3, 0, tzinfo=zone)
    before_offset = dst_before_local.utcoffset()
    after_offset = dst_after_local.utcoffset()
    calendar_states = (
        _calendar_view(calendar, pair, "open", local_utc(2026, 3, 10, 10, 0)),
        _calendar_view(calendar, pair, "holiday", local_utc(2026, 3, 9, 10, 0)),
        _calendar_view(calendar, pair, "daily_break", local_utc(2026, 3, 10, 17, 30)),
        _calendar_view(calendar, pair, "weekend_gap", local_utc(2026, 3, 8, 10, 0)),
        _calendar_view(calendar, pair, "dst_transition", dst_before_local.astimezone(timezone.utc)),
        _calendar_view(
            calendar,
            pair,
            "validity_before",
            calendar.valid_from - timedelta(seconds=1),
        ),
        _calendar_view(calendar, pair, "validity_after", calendar.valid_until),
        _calendar_view(
            calendar, "ZZZ/YYY", "instrument_unavailable", local_utc(2026, 3, 10, 10, 0)
        ),
    )

    market_contract = MarketDataCapabilities(
        provider_id=provider_id,
        capability_version="market-conformance-v1",
        source=CONFORMANCE_PROVIDER_SOURCE,
        supported_pairs=(pair,),
        provider_symbols={pair: spec.alias_for(provider_id) or ""},
        supported_timeframes=frozenset({"1m"}),
        quote_timestamp_basis="UTC_SOURCE_TIMESTAMP",
        clock_source="SYNTHETIC_UTC_CLOCK",
        quote_timezone="UTC",
        bid_ask_semantics="SEPARATE_BID_ASK",
        quote_id_required=True,
        received_at_required=True,
        quote_correction_policy="EXPLICIT_REPLACEMENT_BY_QUOTE_ID",
        bar_timestamp_basis="UTC_BAR_START",
        bar_completion_rule="COMPLETE_ONLY",
        bar_ohlc_semantics="SEPARATE_BID_ASK_OHLC",
        gap_policy="REPORT_NO_INTERPOLATION",
        history_start_utc=calendar.valid_from,
        history_end_utc=calendar.valid_until,
        freshness_policy_id=quote_freshness.policy_id,
        freshness_policy_version=quote_freshness.version,
        max_age_seconds=Decimal(str(quote_freshness.max_age_seconds)),
        max_future_skew_seconds=Decimal(str(quote_freshness.max_future_skew_seconds)),
        max_feed_latency_seconds=Decimal(str(quote_freshness.max_feed_latency_seconds)),
        calendar_id=calendar.calendar_id,
        calendar_version=calendar.version,
        calendar_timezone=calendar.timezone_name,
        spec_version=spec.spec_version,
        pip_size=spec.pip_size_decimal(),
        tick_size=spec.price_tick_size_decimal(),
        quote_precision=spec.quote_precision,
        contract_size=spec.contract_size_decimal(),
        min_quantity=spec.min_quantity_decimal(),
        quantity_step=spec.quantity_step_decimal(),
        max_quantity=spec.max_quantity_decimal(),
        max_spread_pips=Decimal(str(risk_limits.max_spread_pips)),
        corrections_supported=True,
    )
    broker_contract = BrokerConformanceCapabilities(
        broker_id=CONFORMANCE_BROKER_ID,
        capability_version="broker-conformance-v1",
        source=LOCAL_SIMULATION_MODE,
        execution_mode=ExecutionMode.LOCAL_PAPER,
        external_orders_supported=False,
        supports_live_orders=False,
        supported_pairs=(pair,),
        order_types=frozenset({"SIMULATED_INTENT", "MARKET", "LIMIT"}),
        supports_partial_fills=True,
        supports_cancellation=True,
        supports_reconciliation=True,
        supports_unknown_outcome=True,
        unknown_outcome_policy=NO_RESUBMIT_POLICY,
        idempotency_key="CLIENT_ORDER_ID",
        order_id_source="LOCAL_DETERMINISTIC_BROKER_ORDER_ID",
        status_mapping_source="SYNTHETIC_CONFORMANCE_STATUS_MAP",
        reconciliation_retention="LOCAL_FIXTURE_SNAPSHOT",
        rejection_codes=(
            "BROKER_REJECTED",
            "UNKNOWN_OUTCOME",
            "IDEMPOTENCY_CONFLICT",
            "CANCEL_CONFIRMED",
        ),
        pip_size=spec.pip_size_decimal(),
        tick_size=spec.price_tick_size_decimal(),
        quote_precision=spec.quote_precision,
        contract_size=spec.contract_size_decimal(),
        min_quantity=spec.min_quantity_decimal(),
        quantity_step=spec.quantity_step_decimal(),
        max_quantity=spec.max_quantity_decimal(),
        calendar_id=calendar.calendar_id,
        calendar_version=calendar.version,
        calendar_timezone=calendar.timezone_name,
        daily_break_policy="EXPLICIT_DAILY_BREAK_WINDOWS",
        dst_transition_policy="UTC_WINDOWS_FROM_DST_AWARE_CALENDAR",
        holiday_source=calendar.source,
        weekend_gap_policy="EXPLICIT_WEEKEND_GAP",
        calendar_valid_from=calendar.valid_from,
        calendar_valid_until=calendar.valid_until,
        account_currency=account.account_currency_code,
        conversion_source=conversion_rates.source,
        conversion_rate_source_version=f"{account.conversion_source}:{conversion_rates.source}",
        conversion_rate_pairs=tuple(
            sorted(tuple(key) for key in conversion_rates.rates.keys())
        ),
        conversion_max_age_seconds=Decimal(
            str(conversion_rates.freshness.max_age_seconds)
        ),
        leverage=account.leverage_decimal,
        max_leverage=Decimal(str(risk_limits.max_leverage)),
        margin_multiplier=spec.margin_multiplier_decimal(),
        margin_policy="EXPLICIT_ACCOUNT_LEVERAGE_AND_INSTRUMENT_MULTIPLIER",
        negative_equity_policy="NO_NEGATIVE_EQUITY_FIXTURE",
        commission_rate=Decimal(str(cost_terms.commission_rate)),
        slippage_pips=Decimal(str(cost_terms.slippage_pips)),
        financing_rate_daily=Decimal(str(cost_terms.financing_rate_daily)),
        rollover_cutoff_utc=rollover_policy.daily_cutoff_utc,
        triple_rollover_weekday=rollover_policy.triple_rollover_weekday,
        swap_rate_source=SWAP_SOURCE,
        swap_rate_version=SWAP_VERSION,
        long_swap_rate=Decimal(str(cost_terms.financing_rate_daily)),
        short_swap_rate=-Decimal(str(cost_terms.financing_rate_daily)),
        cost_model_id=cost_terms.model_id,
        cost_model_version=cost_terms.version,
    )
    contracts = ConformanceContracts(market_contract, broker_contract)
    m1_capabilities = make_capabilities(
        broker_id=broker_contract.broker_id,
        capability_version=broker_contract.capability_version,
        supports_partial_fills=broker_contract.supports_partial_fills,
        order_types=broker_contract.order_types,
        calendar_id=broker_contract.calendar_id,
    )
    scenario = MarketDataScenario(
        provider_symbol=provider_symbol,
        pair=pair,
        evaluated_at=evaluated_at,
        bid_ask_quote=bid_ask_quote,
        stale_quote=stale_quote,
        crossed_quote=crossed_quote,
        incomplete_bar=incomplete_bar,
        gap_bars=gap_bars,
        quote_correction={
            "original_quote_id": bid_ask_quote.quote_id,
            "corrected_quote_id": corrected_quote.quote_id,
            "timestamp": bid_ask_quote.timestamp,
            "original_bid": bid_ask_quote.bid,
            "corrected_bid": corrected_quote.bid,
            "policy": market_contract.quote_correction_policy,
            "corrected_valid": corrected_quote.validate(spec).valid,
        },
        calendar_states=calendar_states,
        dst_timezone=calendar.timezone_name,
        dst_before_local=dst_before_local,
        dst_after_local=dst_after_local,
        dst_before_utc_offset_seconds=int(before_offset.total_seconds())
        if before_offset
        else 0,
        dst_after_utc_offset_seconds=int(after_offset.total_seconds())
        if after_offset
        else 0,
    )
    market_adapter = DeterministicMarketDataAdapter(
        provider_id=provider_id,
        supported_timeframes=market_contract.supported_timeframes,
        bars=bars,
        quotes=quotes,
        raw_bar_payloads=raw_bar_payloads,
        raw_quote_payloads=raw_quote_payloads,
        scenarios=scenario,
    )
    broker_adapter = DeterministicBrokerAdapter(
        capabilities=broker_contract,
        m1_capabilities=m1_capabilities,
    )

    required_values: dict[str, Any] = {}
    for prefix, contract in (("market", market_contract), ("broker", broker_contract)):
        for item in fields(contract):
            required_values[f"{prefix}.{item.name}"] = _required_value(
                getattr(contract, item.name)
            )
    required_values.update(
        {
            "market.history_bounds": [
                market_contract.history_start_utc,
                market_contract.history_end_utc,
            ],
            "market.freshness": quote_freshness.audit_view(),
            "market.calendar": {
                "calendar_id": calendar.calendar_id,
                "version": calendar.version,
                "signature": calendar.signature(),
                "timezone": calendar.timezone_name,
                "valid_from": calendar.valid_from,
                "valid_until": calendar.valid_until,
                "holidays": {
                    item: [day.isoformat() for day in days]
                    for item, days in calendar.holidays_by_pair.items()
                },
                "windows": {
                    item: [
                        {
                            "start": window.start,
                            "end": window.end,
                            "label": window.label,
                        }
                        for window in windows
                    ]
                    for item, windows in calendar.windows_by_pair.items()
                },
            },
            "market.instrument_spec": spec.audit_view(),
            "broker.partial_fills": broker_contract.supports_partial_fills,
            "broker.cancellation": broker_contract.supports_cancellation,
            "broker.reconciliation": {
                "supported": broker_contract.supports_reconciliation,
                "source": broker_contract.status_mapping_source,
                "retention": broker_contract.reconciliation_retention,
                "unknown_outcome_policy": broker_contract.unknown_outcome_policy,
            },
            "broker.idempotency": {
                "key": broker_contract.idempotency_key,
                "order_id_source": broker_contract.order_id_source,
                "no_resubmit_policy": broker_contract.unknown_outcome_policy,
            },
            "broker.sizing": {
                "pip_size": broker_contract.pip_size,
                "tick_size": broker_contract.tick_size,
                "quote_precision": broker_contract.quote_precision,
                "contract_size": broker_contract.contract_size,
                "min_quantity": broker_contract.min_quantity,
                "quantity_step": broker_contract.quantity_step,
                "max_quantity": broker_contract.max_quantity,
                "spec_version": spec.spec_version,
                "quantity_unit": spec.quantity_unit_value,
            },
            "broker.trading_hours": {
                "calendar_id": calendar.calendar_id,
                "calendar_version": calendar.version,
                "calendar_signature": calendar.signature(),
                "timezone": calendar.timezone_name,
                "valid_from": calendar.valid_from,
                "valid_until": calendar.valid_until,
                "daily_break_policy": broker_contract.daily_break_policy,
                "dst_transition_policy": broker_contract.dst_transition_policy,
                "holiday_source": broker_contract.holiday_source,
                "weekend_gap_policy": broker_contract.weekend_gap_policy,
            },
            "broker.costs": {
                "cost_model_id": cost_terms.model_id,
                "cost_model_version": cost_terms.version,
                "commission_rate": cost_terms.commission_rate,
                "slippage_pips": cost_terms.slippage_pips,
                "financing_rate_daily": cost_terms.financing_rate_daily,
                "rollover_cutoff_utc": _required_value(
                    rollover_policy.daily_cutoff_utc
                ),
                "triple_rollover_weekday": rollover_policy.triple_rollover_weekday,
                "swap_rate_source": SWAP_SOURCE,
                "swap_rate_version": SWAP_VERSION,
                "long_swap_rate": broker_contract.long_swap_rate,
                "short_swap_rate": broker_contract.short_swap_rate,
                "rollover_policy": rollover_policy.audit_view(),
            },
            "broker.margin": {
                "leverage": broker_contract.leverage,
                "max_leverage": broker_contract.max_leverage,
                "margin_multiplier": broker_contract.margin_multiplier,
                "margin_policy": broker_contract.margin_policy,
                "negative_equity_policy": broker_contract.negative_equity_policy,
                "initial_capital": account.initial_capital,
            },
            "broker.recovery": {
                "persistence": "SQLite snapshots",
                "no_resubmit_policy": NO_RESUBMIT_POLICY,
                "restart_fixture": True,
                "unresolved_order_policy": "PRESERVE_AND_BLOCK_NEW_ORDERS",
            },
        }
    )
    missing_required_keys = tuple(
        item.key for item in REQUIRED_PROVIDER_VALUES if item.key not in required_values
    )
    if missing_required_keys:
        # This is a programming error in the fixture builder, not a provider value.
        raise RuntimeError("missing_required_conformance_values:" + ",".join(missing_required_keys))

    return ConformanceEnvironment(
        contracts=contracts,
        market_adapter=market_adapter,
        broker_adapter=broker_adapter,
        m1_broker_capabilities=m1_capabilities,
        catalog=catalog,
        calendar=calendar,
        quote_freshness=quote_freshness,
        account=account,
        risk_limits=risk_limits,
        cost_terms=cost_terms,
        rollover_policy=rollover_policy,
        conversion_rates=conversion_rates,
        spec=spec,
        bars=bars,
        quotes=quotes,
        raw_bar_payloads=raw_bar_payloads,
        raw_quote_payloads=raw_quote_payloads,
        normalization_errors=normalization_errors,
        evaluated_at=evaluated_at,
        provider_symbol=provider_symbol,
        required_values=required_values,
    )

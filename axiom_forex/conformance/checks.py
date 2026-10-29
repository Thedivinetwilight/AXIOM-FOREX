"""Fail-closed offline conformance checks for market-data and broker adapters."""

from __future__ import annotations

from datetime import datetime, time
from decimal import Decimal
from typing import Any, Mapping

from app.audit.log import sanitize_audit_value
from app.calendar.trading import TradingCalendar
from app.common.validation import as_utc, parse_decimal
from app.data.models import ForexQuote, ForexQuoteBar

from axiom_forex.data.normalization import DataNormalizer
from axiom_forex.data.quality import assess_bars

from .contracts import (
    CONFORMANCE_SUITE_ID,
    NO_RESUBMIT_POLICY,
    REQUIRED_PROVIDER_VALUES,
    ConformanceCheck,
    ConformanceFinding,
    ConformanceReport,
    ConformanceSeverity,
)
from .fixtures import ConformanceEnvironment


def _is_supplied(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (tuple, list, dict, set, frozenset)):
        return bool(value)
    if isinstance(value, bool):
        return True
    return True


def _decimal(value: Any, name: str) -> Decimal | None:
    parsed, error = parse_decimal(value, name)
    return parsed if not error else None


def _status_map(view: Mapping[str, Any]) -> dict[str, str]:
    return {
        str(item.get("scenario", "")): str(item.get("status", ""))
        for item in view.get("calendar_states", [])
        if isinstance(item, Mapping)
    }


def _calendar_state(calendar: TradingCalendar, pair: str, at: datetime) -> str:
    return calendar.status(pair, at).status.value


def _safe_scenarios(environment: ConformanceEnvironment) -> tuple[dict[str, Any], str | None]:
    try:
        view = environment.scenario_view()
        return view if isinstance(view, dict) else {}, None
    except Exception as exc:
        return {
            "market": {},
            "broker": {},
            "normalization": {},
        }, f"{type(exc).__name__}:{exc}"


def run_conformance_checks(
    environment: ConformanceEnvironment,
) -> ConformanceReport:
    """Validate explicit contracts and deterministic fixture behavior."""
    findings: list[ConformanceFinding] = []

    def check(code: str, condition: Any, detail: str) -> bool:
        ok = bool(condition)
        findings.append(
            ConformanceFinding(
                code=code,
                severity=ConformanceSeverity.INFO if ok else ConformanceSeverity.ERROR,
                detail=detail,
            )
        )
        return ok

    market = environment.market_contract
    broker = environment.broker_contract
    catalog = environment.catalog
    calendar = environment.calendar
    spec = environment.spec
    freshness = environment.quote_freshness
    account = environment.account
    risk_limits = environment.risk_limits
    cost_terms = environment.cost_terms
    rollover = environment.rollover_policy
    conversion_rates = environment.conversion_rates
    pair = spec.canonical_pair or ""

    contract_result = environment.contracts.validate()
    check("contracts_valid", contract_result.valid, contract_result.reason)

    missing_values = [
        item.key
        for item in REQUIRED_PROVIDER_VALUES
        if not _is_supplied(environment.required_values.get(item.key))
    ]
    check(
        "required_values_supplied",
        not missing_values,
        "missing:" + ",".join(missing_values) if missing_values else "all explicit",
    )
    check(
        "required_values_declared",
        all(item.key in environment.required_values for item in REQUIRED_PROVIDER_VALUES),
        f"declared={len(REQUIRED_PROVIDER_VALUES)}",
    )

    catalog_result = catalog.validate()
    spec_result = spec.validate()
    resolved = catalog.resolve(market.provider_id, environment.provider_symbol)
    check(
        "provider_identity_consistent",
        market.provider_id == environment.market_adapter.provider_id
        and resolved is not None
        and resolved.canonical_pair == pair
        and market.provider_symbols.get(pair) == environment.provider_symbol
        and spec.alias_for(market.provider_id) == environment.provider_symbol
        and pair in market.supported_pairs,
        f"provider={market.provider_id},symbol={environment.provider_symbol},pair={pair}",
    )
    check(
        "instrument_catalog_valid",
        catalog_result.valid and spec_result.valid and spec.status_value == "ACTIVE",
        f"catalog={catalog_result.reason},spec={spec_result.reason},status={spec.status_value}",
    )
    check(
        "instrument_terms_consistent",
        spec.trading_calendar_id == market.calendar_id
        and spec.trading_calendar_version == market.calendar_version
        and spec.spec_version == market.spec_version
        and spec.pip_size_decimal() == market.pip_size
        and spec.price_tick_size_decimal() == market.tick_size
        and spec.contract_size_decimal() == market.contract_size
        and spec.min_quantity_decimal() == market.min_quantity
        and spec.quantity_step_decimal() == market.quantity_step
        and spec.max_quantity_decimal() == market.max_quantity
        and spec.quote_precision == market.quote_precision,
        f"pair={pair},spec_version={spec.spec_version}",
    )
    check(
        "quote_freshness_terms_consistent",
        freshness.validate().valid
        and freshness.policy_id == market.freshness_policy_id
        and freshness.version == market.freshness_policy_version
        and _decimal(freshness.max_age_seconds, "max_age_seconds") == market.max_age_seconds
        and _decimal(freshness.max_future_skew_seconds, "future_skew")
        == market.max_future_skew_seconds
        and _decimal(freshness.max_feed_latency_seconds, "feed_latency")
        == market.max_feed_latency_seconds,
        f"policy={freshness.policy_id}:{freshness.version}",
    )

    calendar_result = calendar.validate()
    check(
        "calendar_valid",
        calendar_result.valid
        and calendar.calendar_id == market.calendar_id == broker.calendar_id
        and calendar.version == market.calendar_version == broker.calendar_version
        and calendar.timezone_name == market.calendar_timezone == broker.calendar_timezone
        and calendar.valid_from == market.history_start_utc == broker.calendar_valid_from
        and calendar.valid_until == market.history_end_utc == broker.calendar_valid_until
        and pair in calendar.instruments
        and bool(calendar.signature()),
        f"calendar={calendar_result.reason},signature={calendar.signature()}",
    )
    account_result = account.validate()
    risk_result = risk_limits.validate()
    leverage = account.leverage_decimal
    max_leverage = _decimal(risk_limits.max_leverage, "max_leverage")
    check(
        "account_risk_terms_consistent",
        account_result.valid
        and risk_result.valid
        and account.account_currency_code == broker.account_currency
        and leverage == broker.leverage
        and max_leverage == broker.max_leverage
        and leverage <= (max_leverage or Decimal(0))
        and spec.margin_multiplier_decimal() == broker.margin_multiplier
        and bool(broker.margin_policy)
        and bool(broker.negative_equity_policy)
        and _decimal(risk_limits.max_spread_pips, "max_spread_pips") == market.max_spread_pips,
        f"account={account_result.reason},risk={risk_result.reason}",
    )
    cost_result = cost_terms.validate()
    rollover_result = rollover.validate()
    long_swap = _decimal(broker.long_swap_rate, "long_swap")
    short_swap = _decimal(broker.short_swap_rate, "short_swap")
    check(
        "cost_financing_terms_consistent",
        cost_result.valid
        and rollover_result.valid
        and account.cost_model_id == cost_terms.model_id == broker.cost_model_id
        and account.cost_model_version == cost_terms.version == broker.cost_model_version
        and account.rollover_schedule_id == rollover.schedule_id
        and account.rollover_schedule_version == rollover.version
        and rollover.calendar_id == broker.calendar_id
        and rollover.daily_cutoff_utc == broker.rollover_cutoff_utc
        and isinstance(broker.rollover_cutoff_utc, time)
        and broker.rollover_cutoff_utc.tzinfo is None
        and rollover.triple_rollover_weekday == broker.triple_rollover_weekday
        and _decimal(cost_terms.commission_rate, "commission") == broker.commission_rate
        and _decimal(cost_terms.slippage_pips, "slippage") == broker.slippage_pips
        and _decimal(cost_terms.financing_rate_daily, "financing")
        == broker.financing_rate_daily
        and bool(broker.swap_rate_source)
        and bool(broker.swap_rate_version)
        and long_swap is not None
        and short_swap is not None,
        f"cost={cost_result.reason},rollover={rollover_result.reason}",
    )
    rate_book_result = conversion_rates.validate()
    base_rate = conversion_rates.get(spec.base_currency, broker.account_currency, environment.evaluated_at)
    quote_rate = conversion_rates.get(spec.quote_currency, broker.account_currency, environment.evaluated_at)
    rate_pairs = tuple(sorted(tuple(key) for key in conversion_rates.rates.keys()))
    required_rate_pairs = {
        (spec.base_currency, broker.account_currency),
        (spec.quote_currency, broker.account_currency),
    }
    check(
        "account_conversion_terms_consistent",
        rate_book_result.valid
        and conversion_rates.source == account.conversion_source == broker.conversion_source
        and rate_pairs == broker.conversion_rate_pairs
        and required_rate_pairs.issubset(set(rate_pairs))
        and base_rate.ok
        and quote_rate.ok
        and _decimal(conversion_rates.freshness.max_age_seconds, "rate_max_age")
        == broker.conversion_max_age_seconds
        and bool(broker.conversion_rate_source_version),
        f"book={rate_book_result.reason},base={base_rate.reason.value},quote={quote_rate.reason.value}",
    )

    normalizer = DataNormalizer(catalog, market.provider_id)
    try:
        raw_bar_results = normalizer.normalize_bars(environment.market_adapter.raw_bar_payloads)
        raw_quote_results = normalizer.normalize_quotes(
            environment.market_adapter.raw_quote_payloads
        )
        normalization_ok = all(result.ok for result in raw_bar_results) and all(
            result.ok for result in raw_quote_results
        )
        normalization_detail = (
            f"bars={len(raw_bar_results)},quotes={len(raw_quote_results)}"
        )
    except Exception as exc:
        normalization_ok = False
        normalization_detail = f"{type(exc).__name__}:{exc}"
    check(
        "strict_payload_normalization",
        normalization_ok
        and not environment.normalization_errors
        and len(environment.bars) == len(environment.market_adapter.raw_bar_payloads)
        and len(environment.quotes) == len(environment.market_adapter.raw_quote_payloads),
        normalization_detail
        + ",errors="
        + ",".join(environment.normalization_errors),
    )

    bars_valid = all(isinstance(item, ForexQuoteBar) and item.validate(spec).valid for item in environment.bars)
    quote_ids = [
        str(getattr(item, "quote_id", "") or "")
        for item in environment.quotes
        if isinstance(item, ForexQuote)
    ]
    try:
        quotes_valid = all(
            isinstance(item, ForexQuote)
            and item.validate(spec).valid
            and item.received_at is not None
            and item.spread > 0
            and item.source.strip()
            for item in environment.quotes
        )
    except Exception:
        quotes_valid = False
    quote_ids_valid = bool(quote_ids) and all(quote_ids) and len(quote_ids) == len(set(quote_ids))
    history_start, _ = as_utc(market.history_start_utc, "history_start")
    history_end, _ = as_utc(market.history_end_utc, "history_end")
    bars_in_bounds = True
    try:
        for bar in environment.bars:
            bar_start, error = as_utc(bar.timestamp, "bar_timestamp")
            if error or bar_start is None or history_start is None or history_end is None:
                bars_in_bounds = False
                break
            if not (history_start <= bar_start < history_end):
                bars_in_bounds = False
                break
    except Exception:
        bars_in_bounds = False
    try:
        quality = assess_bars(
            environment.bars,
            spec=spec,
            calendar=calendar,
            freshness=freshness,
            evaluated_at=environment.evaluated_at,
        )
        quality_ok = quality.usable and quality.classification in {"VALID", "WARNING"}
        quality_detail = f"{quality.classification}:{len(quality.findings)}"
    except Exception as exc:
        quality_ok = False
        quality_detail = f"{type(exc).__name__}:{exc}"
    check(
        "complete_bid_ask_history_no_fabrication",
        bars_valid
        and quotes_valid
        and quote_ids_valid
        and bars_in_bounds
        and quality_ok
        and all(bar.complete for bar in environment.bars)
        and all(bar.timeframe in market.supported_timeframes for bar in environment.bars),
        f"bars_valid={bars_valid},quotes_valid={quotes_valid},ids={quote_ids_valid},bounds={bars_in_bounds},quality={quality_detail}",
    )

    adapter = environment.market_adapter
    latest = adapter.latest_quote(environment.provider_symbol)
    unsupported_latest = adapter.latest_quote("UNSUPPORTED_SYMBOL")
    start = environment.evaluated_at
    end = environment.evaluated_at
    if len(environment.bars) >= 4:
        try:
            start = environment.bars[1].timestamp
            end = environment.bars[3].timestamp
            selected = adapter.historical_bars(
                environment.provider_symbol, "1m", start, end
            )
            half_open_ok = (
                len(selected) == 2
                and selected[0].timestamp == environment.bars[1].timestamp
            )
        except Exception:
            half_open_ok = False
    else:
        half_open_ok = False
    check(
        "provider_read_only_half_open_history",
        latest is not None
        and latest.validate(spec).valid
        and unsupported_latest is None
        and half_open_ok
        and not adapter.historical_bars("UNSUPPORTED", "1m", start, end)
        and not adapter.historical_bars(environment.provider_symbol, "UNSUPPORTED", start, end),
        f"latest={latest is not None},unsupported={unsupported_latest is None},half_open={half_open_ok}",
    )

    scenarios, scenario_error = _safe_scenarios(environment)
    check(
        "scenario_replay_available",
        scenario_error is None,
        scenario_error or "deterministic replay completed",
    )
    market_view = scenarios.get("market", {})
    broker_view = scenarios.get("broker", {})
    normalization_view = scenarios.get("normalization", {})
    check(
        "scenario_normalization_counts",
        normalization_view.get("errors") == []
        and normalization_view.get("normalized_bar_count")
        == normalization_view.get("bar_payload_count")
        and normalization_view.get("normalized_quote_count")
        == normalization_view.get("quote_payload_count"),
        f"normalization={normalization_view}",
    )

    bid_ask_view = market_view.get("bid_ask", {})
    stale_view = market_view.get("stale_quote", {})
    gap_view = market_view.get("gap", {})
    correction_view = market_view.get("quote_correction", {})
    dst_view = market_view.get("dst", {})
    status_map = _status_map(market_view)
    spread_pips = _decimal(bid_ask_view.get("spread_pips"), "spread_pips")
    max_spread = _decimal(market.max_spread_pips, "max_spread")
    check(
        "bid_ask_stale_crossed_incomplete_fail_closed",
        bid_ask_view.get("valid") is True
        and bid_ask_view.get("fresh") is True
        and spread_pips is not None
        and max_spread is not None
        and spread_pips > 0
        and spread_pips <= max_spread
        and stale_view.get("ok") is False
        and stale_view.get("reason") == "STALE_QUOTE"
        and market_view.get("crossed_quote_valid") is False
        and market_view.get("incomplete_bar_valid") is False,
        f"spread={bid_ask_view.get('spread_pips')},stale={stale_view},crossed={market_view.get('crossed_quote_valid')},incomplete={market_view.get('incomplete_bar_valid')}",
    )
    try:
        dst_closed = (
            not calendar.status(
                pair, environment.market_adapter.scenarios.dst_before_local
            ).is_open
            and not calendar.status(
                pair, environment.market_adapter.scenarios.dst_after_local
            ).is_open
        )
    except Exception:
        dst_closed = False
    check(
        "gap_daily_break_dst_holiday_weekend_validity",
        gap_view.get("classification") == "MISSING"
        and gap_view.get("usable") is False
        and "MISSING_BAR" in gap_view.get("finding_codes", [])
        and gap_view.get("interpolation_used") is False
        and dst_view.get("offset_changed") is True
        and dst_view.get("timezone") == market.calendar_timezone
        and status_map == {
            "open": "OPEN",
            "holiday": "HOLIDAY",
            "daily_break": "DAILY_BREAK",
            "weekend_gap": "WEEKEND_GAP",
            "dst_transition": status_map.get("dst_transition", ""),
            "validity_before": "NOT_YET_VALID",
            "validity_after": "CALENDAR_EXPIRED",
            "instrument_unavailable": "INSTRUMENT_UNAVAILABLE",
        }
        and status_map.get("dst_transition") not in {"", "OPEN"}
        and dst_closed,
        f"gap={gap_view},statuses={status_map},dst={dst_view}",
    )
    check(
        "quote_correction_explicit",
        correction_view.get("price_changed") is True
        and correction_view.get("same_timestamp") is True
        and correction_view.get("corrected_valid") is True
        and correction_view.get("original_quote_id") != correction_view.get("corrected_quote_id")
        and correction_view.get("policy") == market.quote_correction_policy
        and market.corrections_supported
        and market.quote_id_required
        and market.received_at_required,
        f"correction={correction_view}",
    )

    m1 = environment.m1_broker_capabilities
    check(
        "broker_external_orders_disabled",
        m1.validate().valid
        and m1.execution_mode_value == "LOCAL_PAPER"
        and m1.supports_live_orders is False
        and broker.execution_mode_value == "LOCAL_PAPER"
        and broker.external_orders_supported is False
        and broker.supports_live_orders is False
        and broker_view.get("all_external_orders_disabled") is True
        and broker_view.get("external_calls") == 0,
        f"mode={broker.execution_mode_value},external={broker.external_orders_supported},calls={broker_view.get('external_calls')}",
    )
    check(
        "broker_capability_terms_consistent",
        m1.broker_id == broker.broker_id
        and m1.capability_version == broker.capability_version
        and m1.calendar_id == broker.calendar_id
        and m1.order_types == broker.order_types
        and m1.supports_partial_fills == broker.supports_partial_fills
        and "SIMULATED_INTENT" in broker.order_types
        and "MARKET" in broker.order_types
        and "LIMIT" in broker.order_types
        and broker.idempotency_key == "CLIENT_ORDER_ID"
        and broker.unknown_outcome_policy == NO_RESUBMIT_POLICY
        and bool(broker.status_mapping_source)
        and bool(broker.reconciliation_retention)
        and bool(broker.order_id_source)
        and bool(broker.rejection_codes),
        f"order_types={sorted(broker.order_types)},idempotency={broker.idempotency_key}",
    )

    rejected = broker_view.get("rejected", {})
    partial = broker_view.get("partial", {})
    cancelled_partial = broker_view.get("cancelled_partial", {})
    unknown = broker_view.get("unknown", {})
    reconciled = broker_view.get("unknown_reconciled", {})
    partial_filled = _decimal(partial.get("filled_quantity"), "partial_filled")
    partial_remaining = _decimal(partial.get("remaining_quantity"), "partial_remaining")
    partial_quantity = _decimal(partial.get("quantity"), "partial_quantity")
    partial_average = _decimal(partial.get("average_price"), "partial_average")
    cancelled_filled = _decimal(
        cancelled_partial.get("filled_quantity"), "cancelled_filled"
    )
    rejected_filled = _decimal(rejected.get("filled_quantity"), "rejected_filled")
    sequence = list(broker_view.get("reconcile_status_sequence", []))
    check(
        "order_lifecycle_partial_reject_unknown_reconcile",
        broker.supports_partial_fills
        and broker.supports_cancellation
        and broker.supports_reconciliation
        and broker.supports_unknown_outcome
        and broker_view.get("idempotency_duplicate_same_id") is True
        and broker_view.get("idempotency_transition_count_unchanged") is True
        and broker_view.get("idempotency_conflict_rejected") is True
        and broker_view.get("no_resubmit_while_unresolved") is True
        and broker_view.get("resubmit_blocked_count", 0) >= 2
        and rejected.get("status") == "REJECTED"
        and rejected.get("reason") in broker.rejection_codes
        and rejected_filled == Decimal(0)
        and partial.get("status") == "PARTIALLY_FILLED"
        and partial_filled is not None
        and partial_remaining is not None
        and partial_quantity is not None
        and partial_filled > 0
        and partial_remaining > 0
        and partial_filled + partial_remaining == partial_quantity
        and partial_average is not None
        and partial_average > 0
        and broker_view.get("partial_duplicate_fill_noop") is True
        and cancelled_partial.get("status") == "CANCELLED"
        and cancelled_partial.get("reason") in broker.rejection_codes
        and cancelled_filled == partial_filled
        and unknown.get("status") == "UNKNOWN"
        and unknown.get("reason") in broker.rejection_codes
        and reconciled.get("status") == "FILLED"
        and "UNKNOWN" in sequence
        and "FILLED" in sequence
        and sequence.index("UNKNOWN") < sequence.index("FILLED")
        and broker_view.get("all_transitions_unique") is True,
        f"rejected={rejected.get('status')},partial={partial.get('status')},cancel={cancelled_partial.get('status')},unknown={unknown.get('status')},reconciled={reconciled.get('status')}",
    )
    check(
        "restart_recovery_no_resubmit",
        broker_view.get("restart_recovery_equal") is True
        and broker_view.get("restart_unresolved_equal") is True
        and broker_view.get("restart_no_resubmit_unresolved") is True
        and broker_view.get("snapshot_order_count", 0) > 0
        and bool(broker_view.get("unresolved_order_ids"))
        and broker_view.get("unknown_outcome_policy") == NO_RESUBMIT_POLICY,
        f"equal={broker_view.get('restart_recovery_equal')},unresolved={broker_view.get('unresolved_order_ids')}",
    )
    check(
        "contract_versions_explicit",
        all(
            bool(value)
            for value in (
                market.capability_version,
                market.source,
                market.freshness_policy_version,
                market.calendar_version,
                market.spec_version,
                broker.capability_version,
                broker.source,
                broker.status_mapping_source,
                broker.swap_rate_version,
                broker.cost_model_version,
                rollover.version,
                risk_limits.limits_version,
            )
        ),
        "all versioned terms explicit",
    )

    required_value_rows: list[dict[str, Any]] = []
    for item in REQUIRED_PROVIDER_VALUES:
        value = environment.required_values.get(item.key)
        row = item.audit_view()
        row["supplied"] = _is_supplied(value)
        row["value"] = value
        required_value_rows.append(row)
    checks = tuple(
        ConformanceCheck(item.code, item.ok, item.detail) for item in findings
    )
    report = ConformanceReport(
        suite_id=CONFORMANCE_SUITE_ID,
        passed=all(item.ok for item in findings),
        findings=tuple(findings),
        checks=checks,
        required_values=tuple(required_value_rows),
        provider_contract=market.audit_view(),
        broker_contract=broker.audit_view(),
        scenarios=scenarios,
    )
    # Ensure the report itself is safe to persist in an audit log.
    sanitize_audit_value(report.audit_view())
    return report

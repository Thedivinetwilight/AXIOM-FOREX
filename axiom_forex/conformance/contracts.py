"""Explicit provider/broker capability contracts for offline conformance.

These objects describe what a future adapter must supply.  They do not read
configuration, select a vendor, connect anywhere, or place orders.  Every
required term is explicit so conformance can fail closed on a missing or
inconsistent answer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time
from decimal import Decimal
from enum import Enum
from typing import Any, Mapping

from app.audit.log import sanitize_audit_value
from app.common.types import ExecutionMode
from app.common.validation import (
    ValidationResult,
    canonical_pair,
    parse_decimal,
    validate_currency_code,
)

CONFORMANCE_SUITE_ID = "provider-broker-conformance-v1"
NO_RESUBMIT_POLICY = "NO_RESUBMIT_UNTIL_RECONCILED"
LOCAL_SIMULATION_MODE = "LOCAL_OFFLINE_FIXTURE"


class ConformanceSeverity(str, Enum):
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"


class ConformanceError(Exception):
    """Raised when a required conformance contract cannot be trusted."""


@dataclass(frozen=True)
class ConformanceFinding:
    code: str
    severity: ConformanceSeverity | str
    detail: str

    @property
    def severity_value(self) -> str:
        return (
            self.severity.value
            if isinstance(self.severity, ConformanceSeverity)
            else str(self.severity).upper()
        )

    @property
    def ok(self) -> bool:
        return self.severity_value != ConformanceSeverity.ERROR.value

    def audit_view(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity_value,
            "detail": self.detail,
            "ok": self.ok,
        }


@dataclass(frozen=True)
class ConformanceCheck:
    name: str
    passed: bool
    detail: str

    def audit_view(self) -> dict[str, Any]:
        return {"name": self.name, "passed": self.passed, "detail": self.detail}


@dataclass(frozen=True)
class ConformanceReport:
    suite_id: str
    passed: bool
    findings: tuple[ConformanceFinding, ...] = ()
    checks: tuple[ConformanceCheck, ...] = ()
    required_values: tuple[dict[str, Any], ...] = ()
    provider_contract: dict[str, Any] = field(default_factory=dict)
    broker_contract: dict[str, Any] = field(default_factory=dict)
    scenarios: dict[str, Any] = field(default_factory=dict)

    @property
    def status(self) -> str:
        return "PASS" if self.passed else "FAIL"

    @property
    def check_count(self) -> int:
        return len(self.checks)

    @property
    def error_count(self) -> int:
        return sum(1 for item in self.findings if not item.ok)

    def raise_for_failure(self) -> None:
        if not self.passed:
            codes = ",".join(item.code for item in self.findings if not item.ok)
            raise ConformanceError(f"conformance_failed:{codes}")

    def audit_view(self) -> dict[str, Any]:
        payload = {
            "suite_id": self.suite_id,
            "status": self.status,
            "passed": self.passed,
            "check_count": len(self.checks),
            "error_count": self.error_count,
            "findings": [item.audit_view() for item in self.findings],
            "checks": [item.audit_view() for item in self.checks],
            "required_values": [dict(item) for item in self.required_values],
            "provider_contract": dict(self.provider_contract),
            "broker_contract": dict(self.broker_contract),
            "scenarios": dict(self.scenarios),
        }
        clean = sanitize_audit_value(payload)
        return clean if isinstance(clean, dict) else {"passed": self.passed}


@dataclass(frozen=True)
class RequiredValue:
    area: str
    key: str
    description: str
    fixture_example: str

    def audit_view(self) -> dict[str, Any]:
        return {
            "area": self.area,
            "key": self.key,
            "description": self.description,
            "fixture_example": self.fixture_example,
        }


REQUIRED_PROVIDER_VALUES: tuple[RequiredValue, ...] = (
    RequiredValue("market", "market.provider_id", "Canonical data-provider identity used for alias resolution.", "SYNTHETIC_FEED"),
    RequiredValue("market", "market.capability_version", "Version of the market-data capability declaration.", "market-conformance-v1"),
    RequiredValue("market", "market.source", "Provenance of recorded quotes and bars.", "SYNTHETIC_CONFORMANCE_FEED"),
    RequiredValue("market", "market.supported_pairs", "Canonical currency pairs the adapter is allowed to publish.", "AAA/BBB"),
    RequiredValue("market", "market.provider_symbols", "Canonical pair to provider symbol mapping.", "AAA_BBB"),
    RequiredValue("market", "market.supported_timeframes", "Completed-bar timeframes with defined boundaries.", "1m"),
    RequiredValue("market", "market.quote_timestamp_basis", "Meaning and timezone basis of quote timestamps.", "UTC_SOURCE_TIMESTAMP"),
    RequiredValue("market", "market.clock_source", "Clock source and synchronization assumption.", "SYNTHETIC_UTC_CLOCK"),
    RequiredValue("market", "market.bid_ask_semantics", "Whether bid/ask are separate executable prices.", "SEPARATE_BID_ASK"),
    RequiredValue("market", "market.quote_id_required", "Whether every quote has a stable provider identifier.", "true"),
    RequiredValue("market", "market.received_at_required", "Whether local receive time is mandatory for freshness.", "true"),
    RequiredValue("market", "market.quote_correction_policy", "How corrected quotes replace earlier quotes.", "EXPLICIT_REPLACEMENT_BY_QUOTE_ID"),
    RequiredValue("market", "market.bar_timestamp_basis", "Whether bar timestamps are start, end, or another documented instant.", "UTC_BAR_START"),
    RequiredValue("market", "market.bar_completion_rule", "Rule that guarantees only completed bars are returned.", "COMPLETE_ONLY"),
    RequiredValue("market", "market.bar_ohlc_semantics", "Bid/ask OHLC aggregation semantics and volume definition.", "SEPARATE_BID_ASK_OHLC"),
    RequiredValue("market", "market.gap_policy", "How session gaps and missing bars are represented.", "REPORT_NO_INTERPOLATION"),
    RequiredValue("market", "market.history_bounds", "Explicit history coverage validity bounds in UTC.", "2026-03-06..2026-03-13 UTC"),
    RequiredValue("market", "market.freshness", "Quote age, future skew, and feed latency tolerances.", "5s / 1s / 1s"),
    RequiredValue("market", "market.calendar", "Broker calendar identity, version, timezone, windows, holidays, breaks, DST, validity.", "synthetic-24x5-v1 / calendar-v1 / America/New_York"),
    RequiredValue("market", "market.instrument_spec", "Pip, tick, precision, contract size, min/step/max quantity, status, spec version.", "spec-v1"),
    RequiredValue("market", "market.max_spread_pips", "Maximum executable spread accepted by the gate.", "5 pips"),
    RequiredValue("broker", "broker.broker_id", "Canonical broker identity used in audit records.", "SYNTHETIC_CONFORMANCE_BROKER"),
    RequiredValue("broker", "broker.capability_version", "Version of the broker capability declaration.", "broker-conformance-v1"),
    RequiredValue("broker", "broker.execution_mode", "Execution mode permitted by this repository.", "LOCAL_PAPER"),
    RequiredValue("broker", "broker.external_orders_supported", "Must remain false for this offline milestone.", "false"),
    RequiredValue("broker", "broker.order_types", "Supported order types and simulated-intent capability.", "SIMULATED_INTENT, MARKET, LIMIT"),
    RequiredValue("broker", "broker.partial_fills", "Whether partial fills and incremental fill quantities are supported.", "true"),
    RequiredValue("broker", "broker.cancellation", "Cancellation semantics and confirmation requirements.", "CONFIRMED_TRANSITION"),
    RequiredValue("broker", "broker.reconciliation", "Order-history source, status map, retention, and unknown-outcome policy.", "SYNTHETIC_CONFORMANCE_STATUS_MAP / LOCAL_FIXTURE_SNAPSHOT"),
    RequiredValue("broker", "broker.idempotency", "Client-order idempotency key and broker order identity source.", "CLIENT_ORDER_ID / LOCAL_DETERMINISTIC_BROKER_ORDER_ID"),
    RequiredValue("broker", "broker.rejection_codes", "Provider rejection/error codes that must be mapped deterministically.", "BROKER_REJECTED, UNKNOWN_OUTCOME, IDEMPOTENCY_CONFLICT, CANCEL_CONFIRMED"),
    RequiredValue("broker", "broker.sizing", "Pip, tick, precision, contract size, min/step/max quantity.", "spec-v1 / LOTS / 0.01 step"),
    RequiredValue("broker", "broker.trading_hours", "Calendar identity, timezone, DST, holidays, daily breaks, weekend gaps, validity.", "synthetic-24x5-v1 / calendar-v1 / America/New_York"),
    RequiredValue("broker", "broker.costs", "Commission, spread/slippage, financing, swaps, triple-rollover day, cutoff, source versions.", "cost-v1 / rollover-policy-v1 / swap-fixture-v1"),
    RequiredValue("broker", "broker.account_currency", "Account currency and timestamped conversion-rate source/pairs/freshness.", "CCC / SYNTHETIC_FX_SOURCE / BBB-CCC, AAA-CCC"),
    RequiredValue("broker", "broker.margin", "Leverage, max leverage, margin multiplier, margin-call and negative-equity policy.", "leverage 5 / max 10 / multiplier 1"),
    RequiredValue("broker", "broker.recovery", "Durable order/fill/position snapshot and unresolved-order no-resubmit behavior.", "SQLite snapshots / NO_RESUBMIT_UNTIL_RECONCILED"),
)


def _require_text(result: list[str], value: Any, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        result.append(f"missing_{name}")


def _require_positive(result: list[str], value: Any, name: str) -> None:
    parsed, error = parse_decimal(value, name)
    if error or parsed is None or parsed <= 0:
        result.append(error or f"invalid_{name}")


def _require_non_negative(result: list[str], value: Any, name: str) -> None:
    parsed, error = parse_decimal(value, name)
    if error or parsed is None or parsed < 0:
        result.append(error or f"invalid_{name}")


def _require_bool(result: list[str], value: Any, name: str) -> None:
    if not isinstance(value, bool):
        result.append(f"invalid_{name}")


def _require_pairs(result: list[str], pairs: Any, name: str) -> None:
    if not isinstance(pairs, (tuple, list, frozenset)) or not pairs:
        result.append(f"missing_{name}")
        return
    for pair in pairs:
        canonical, error = canonical_pair(pair)
        if error or canonical is None or canonical != pair:
            result.append(f"invalid_{name}:{pair}")


def _clean(view: dict[str, Any]) -> dict[str, Any]:
    result = sanitize_audit_value(view)
    return result if isinstance(result, dict) else {}


@dataclass(frozen=True)
class MarketDataCapabilities:
    provider_id: str
    capability_version: str
    source: str
    supported_pairs: tuple[str, ...]
    provider_symbols: Mapping[str, str]
    supported_timeframes: frozenset[str]
    quote_timestamp_basis: str
    clock_source: str
    quote_timezone: str
    bid_ask_semantics: str
    quote_id_required: bool
    received_at_required: bool
    quote_correction_policy: str
    bar_timestamp_basis: str
    bar_completion_rule: str
    bar_ohlc_semantics: str
    gap_policy: str
    history_start_utc: datetime
    history_end_utc: datetime
    freshness_policy_id: str
    freshness_policy_version: str
    max_age_seconds: Decimal
    max_future_skew_seconds: Decimal
    max_feed_latency_seconds: Decimal
    calendar_id: str
    calendar_version: str
    calendar_timezone: str
    spec_version: str
    pip_size: Decimal
    tick_size: Decimal
    quote_precision: int
    contract_size: Decimal
    min_quantity: Decimal
    quantity_step: Decimal
    max_quantity: Decimal | None
    max_spread_pips: Decimal
    corrections_supported: bool

    def validate(self) -> ValidationResult:
        errors: list[str] = []
        for name in (
            "provider_id",
            "capability_version",
            "source",
            "quote_timestamp_basis",
            "clock_source",
            "quote_timezone",
            "bid_ask_semantics",
            "quote_correction_policy",
            "bar_timestamp_basis",
            "bar_completion_rule",
            "bar_ohlc_semantics",
            "gap_policy",
            "freshness_policy_id",
            "freshness_policy_version",
            "calendar_id",
            "calendar_version",
            "calendar_timezone",
            "spec_version",
        ):
            _require_text(errors, getattr(self, name), name)
        _require_pairs(errors, self.supported_pairs, "supported_pairs")
        if not isinstance(self.provider_symbols, Mapping) or not self.provider_symbols:
            errors.append("missing_provider_symbols")
        else:
            for pair in self.supported_pairs:
                symbol = self.provider_symbols.get(pair)
                if not isinstance(symbol, str) or not symbol.strip():
                    errors.append(f"missing_provider_symbol:{pair}")
            for pair in self.provider_symbols:
                if pair not in self.supported_pairs:
                    errors.append(f"unmapped_provider_symbol:{pair}")
        if not isinstance(self.supported_timeframes, frozenset) or not self.supported_timeframes:
            errors.append("missing_supported_timeframes")
        elif any(not isinstance(item, str) or not item.strip() for item in self.supported_timeframes):
            errors.append("invalid_supported_timeframes")
        _require_bool(errors, self.quote_id_required, "quote_id_required")
        _require_bool(errors, self.received_at_required, "received_at_required")
        _require_bool(errors, self.corrections_supported, "corrections_supported")
        if not isinstance(self.history_start_utc, datetime) or not isinstance(self.history_end_utc, datetime):
            errors.append("invalid_history_bounds")
        elif self.history_start_utc.tzinfo is None or self.history_end_utc.tzinfo is None:
            errors.append("naive_history_bounds")
        elif self.history_end_utc <= self.history_start_utc:
            errors.append("empty_history_bounds")
        _require_positive(errors, self.max_age_seconds, "max_age_seconds")
        _require_non_negative(errors, self.max_future_skew_seconds, "max_future_skew_seconds")
        _require_non_negative(errors, self.max_feed_latency_seconds, "max_feed_latency_seconds")
        for name in (
            "pip_size",
            "tick_size",
            "contract_size",
            "min_quantity",
            "quantity_step",
            "max_spread_pips",
        ):
            _require_positive(errors, getattr(self, name), name)
        if not isinstance(self.quote_precision, int) or isinstance(self.quote_precision, bool) or self.quote_precision < 0:
            errors.append("invalid_quote_precision")
        if self.max_quantity is not None:
            maximum, error = parse_decimal(self.max_quantity, "max_quantity")
            minimum, min_error = parse_decimal(self.min_quantity, "min_quantity")
            if error or maximum is None or maximum <= 0:
                errors.append(error or "invalid_max_quantity")
            elif not min_error and minimum is not None and maximum < minimum:
                errors.append("max_quantity_below_minimum")
        return ValidationResult.fail(*errors) if errors else ValidationResult.ok()

    def audit_view(self) -> dict[str, Any]:
        return _clean(
            {
                "provider_id": self.provider_id,
                "capability_version": self.capability_version,
                "source": self.source,
                "supported_pairs": list(self.supported_pairs),
                "provider_symbols": dict(self.provider_symbols),
                "supported_timeframes": sorted(self.supported_timeframes),
                "quote_timestamp_basis": self.quote_timestamp_basis,
                "clock_source": self.clock_source,
                "quote_timezone": self.quote_timezone,
                "bid_ask_semantics": self.bid_ask_semantics,
                "quote_id_required": self.quote_id_required,
                "received_at_required": self.received_at_required,
                "quote_correction_policy": self.quote_correction_policy,
                "bar_timestamp_basis": self.bar_timestamp_basis,
                "bar_completion_rule": self.bar_completion_rule,
                "bar_ohlc_semantics": self.bar_ohlc_semantics,
                "gap_policy": self.gap_policy,
                "history_start_utc": self.history_start_utc,
                "history_end_utc": self.history_end_utc,
                "freshness": {
                    "policy_id": self.freshness_policy_id,
                    "version": self.freshness_policy_version,
                    "max_age_seconds": self.max_age_seconds,
                    "max_future_skew_seconds": self.max_future_skew_seconds,
                    "max_feed_latency_seconds": self.max_feed_latency_seconds,
                },
                "calendar": {
                    "calendar_id": self.calendar_id,
                    "version": self.calendar_version,
                    "timezone": self.calendar_timezone,
                },
                "instrument": {
                    "spec_version": self.spec_version,
                    "pip_size": self.pip_size,
                    "tick_size": self.tick_size,
                    "quote_precision": self.quote_precision,
                    "contract_size": self.contract_size,
                    "min_quantity": self.min_quantity,
                    "quantity_step": self.quantity_step,
                    "max_quantity": self.max_quantity,
                    "max_spread_pips": self.max_spread_pips,
                },
                "corrections_supported": self.corrections_supported,
            }
        )


@dataclass(frozen=True)
class BrokerConformanceCapabilities:
    broker_id: str
    capability_version: str
    source: str
    execution_mode: ExecutionMode | str
    external_orders_supported: bool
    supports_live_orders: bool
    supported_pairs: tuple[str, ...]
    order_types: frozenset[str]
    supports_partial_fills: bool
    supports_cancellation: bool
    supports_reconciliation: bool
    supports_unknown_outcome: bool
    unknown_outcome_policy: str
    idempotency_key: str
    order_id_source: str
    status_mapping_source: str
    reconciliation_retention: str
    rejection_codes: tuple[str, ...]
    pip_size: Decimal
    tick_size: Decimal
    quote_precision: int
    contract_size: Decimal
    min_quantity: Decimal
    quantity_step: Decimal
    max_quantity: Decimal | None
    calendar_id: str
    calendar_version: str
    calendar_timezone: str
    daily_break_policy: str
    dst_transition_policy: str
    holiday_source: str
    weekend_gap_policy: str
    calendar_valid_from: datetime
    calendar_valid_until: datetime
    account_currency: str
    conversion_source: str
    conversion_rate_source_version: str
    conversion_rate_pairs: tuple[tuple[str, str], ...]
    conversion_max_age_seconds: Decimal
    leverage: Decimal
    max_leverage: Decimal
    margin_multiplier: Decimal
    margin_policy: str
    negative_equity_policy: str
    commission_rate: Decimal
    slippage_pips: Decimal
    financing_rate_daily: Decimal
    rollover_cutoff_utc: time
    triple_rollover_weekday: int | None
    swap_rate_source: str
    swap_rate_version: str
    long_swap_rate: Decimal
    short_swap_rate: Decimal
    cost_model_id: str
    cost_model_version: str

    @property
    def execution_mode_value(self) -> str:
        return (
            self.execution_mode.value
            if isinstance(self.execution_mode, ExecutionMode)
            else str(self.execution_mode).upper()
        )

    def validate(self) -> ValidationResult:
        errors: list[str] = []
        for name in (
            "broker_id",
            "capability_version",
            "source",
            "unknown_outcome_policy",
            "idempotency_key",
            "order_id_source",
            "status_mapping_source",
            "reconciliation_retention",
            "calendar_id",
            "calendar_version",
            "calendar_timezone",
            "daily_break_policy",
            "dst_transition_policy",
            "holiday_source",
            "weekend_gap_policy",
            "account_currency",
            "conversion_source",
            "conversion_rate_source_version",
            "margin_policy",
            "negative_equity_policy",
            "swap_rate_source",
            "swap_rate_version",
            "cost_model_id",
            "cost_model_version",
        ):
            _require_text(errors, getattr(self, name), name)
        try:
            if ExecutionMode(self.execution_mode_value) != ExecutionMode.LOCAL_PAPER:
                errors.append("unsupported_execution_mode")
        except ValueError:
            errors.append("unsupported_execution_mode")
        _require_bool(errors, self.external_orders_supported, "external_orders_supported")
        if self.external_orders_supported:
            errors.append("external_orders_must_be_disabled")
        _require_bool(errors, self.supports_live_orders, "supports_live_orders")
        if self.supports_live_orders:
            errors.append("live_orders_must_be_disabled")
        _require_bool(errors, self.supports_partial_fills, "supports_partial_fills")
        _require_bool(errors, self.supports_cancellation, "supports_cancellation")
        _require_bool(errors, self.supports_reconciliation, "supports_reconciliation")
        _require_bool(errors, self.supports_unknown_outcome, "supports_unknown_outcome")
        if self.unknown_outcome_policy != NO_RESUBMIT_POLICY:
            errors.append("invalid_unknown_outcome_policy")
        if self.idempotency_key != "CLIENT_ORDER_ID":
            errors.append("invalid_idempotency_key")
        _require_pairs(errors, self.supported_pairs, "supported_pairs")
        if not isinstance(self.order_types, frozenset) or not self.order_types:
            errors.append("missing_order_types")
        elif "SIMULATED_INTENT" not in self.order_types or "MARKET" not in self.order_types:
            errors.append("required_order_types_missing")
        if not isinstance(self.rejection_codes, tuple) or not self.rejection_codes:
            errors.append("missing_rejection_codes")
        elif any(not isinstance(item, str) or not item.strip() for item in self.rejection_codes):
            errors.append("invalid_rejection_codes")
        for name in (
            "pip_size",
            "tick_size",
            "contract_size",
            "min_quantity",
            "quantity_step",
            "max_leverage",
            "margin_multiplier",
            "conversion_max_age_seconds",
        ):
            _require_positive(errors, getattr(self, name), name)
        leverage, leverage_error = parse_decimal(self.leverage, "leverage")
        max_leverage, max_leverage_error = parse_decimal(self.max_leverage, "max_leverage")
        _require_positive(errors, self.leverage, "leverage")
        _require_non_negative(errors, self.commission_rate, "commission_rate")
        _require_non_negative(errors, self.slippage_pips, "slippage_pips")
        _require_non_negative(errors, self.financing_rate_daily, "financing_rate_daily")
        if not leverage_error and not max_leverage_error and leverage is not None and max_leverage is not None and leverage > max_leverage:
            errors.append("leverage_above_max_leverage")
        if not isinstance(self.quote_precision, int) or isinstance(self.quote_precision, bool) or self.quote_precision < 0:
            errors.append("invalid_quote_precision")
        if self.max_quantity is not None:
            maximum, error = parse_decimal(self.max_quantity, "max_quantity")
            minimum, min_error = parse_decimal(self.min_quantity, "min_quantity")
            if error or maximum is None or maximum <= 0:
                errors.append(error or "invalid_max_quantity")
            elif not min_error and minimum is not None and maximum < minimum:
                errors.append("max_quantity_below_minimum")
        _, currency_error = validate_currency_code(self.account_currency, "account_currency")
        if currency_error:
            errors.append(currency_error)
        if not isinstance(self.conversion_rate_pairs, tuple) or not self.conversion_rate_pairs:
            errors.append("missing_conversion_rate_pairs")
        else:
            for pair in self.conversion_rate_pairs:
                if not isinstance(pair, tuple) or len(pair) != 2:
                    errors.append("invalid_conversion_rate_pair")
                    continue
                for code in pair:
                    _, code_error = validate_currency_code(code, "conversion_currency")
                    if code_error:
                        errors.append(code_error)
        if not isinstance(self.rollover_cutoff_utc, time):
            errors.append("invalid_rollover_cutoff")
        elif self.rollover_cutoff_utc.tzinfo is not None:
            errors.append("rollover_cutoff_must_be_naive_utc_time")
        weekday = self.triple_rollover_weekday
        if weekday is not None:
            if isinstance(weekday, bool) or not isinstance(weekday, int) or not 0 <= weekday <= 6:
                errors.append("invalid_triple_rollover_weekday")
        if not isinstance(self.calendar_valid_from, datetime) or not isinstance(self.calendar_valid_until, datetime):
            errors.append("invalid_calendar_validity")
        elif self.calendar_valid_from.tzinfo is None or self.calendar_valid_until.tzinfo is None:
            errors.append("naive_calendar_validity")
        elif self.calendar_valid_until <= self.calendar_valid_from:
            errors.append("empty_calendar_validity")
        return ValidationResult.fail(*errors) if errors else ValidationResult.ok()

    def audit_view(self) -> dict[str, Any]:
        return _clean(
            {
                "broker_id": self.broker_id,
                "capability_version": self.capability_version,
                "source": self.source,
                "execution_mode": self.execution_mode_value,
                "external_orders_supported": self.external_orders_supported,
                "supports_live_orders": self.supports_live_orders,
                "supported_pairs": list(self.supported_pairs),
                "order_types": sorted(self.order_types),
                "partial_fills": self.supports_partial_fills,
                "cancellation": self.supports_cancellation,
                "reconciliation": self.supports_reconciliation,
                "unknown_outcome": self.supports_unknown_outcome,
                "unknown_outcome_policy": self.unknown_outcome_policy,
                "idempotency_key": self.idempotency_key,
                "order_id_source": self.order_id_source,
                "status_mapping_source": self.status_mapping_source,
                "reconciliation_retention": self.reconciliation_retention,
                "rejection_codes": list(self.rejection_codes),
                "instrument": {
                    "pip_size": self.pip_size,
                    "tick_size": self.tick_size,
                    "quote_precision": self.quote_precision,
                    "contract_size": self.contract_size,
                    "min_quantity": self.min_quantity,
                    "quantity_step": self.quantity_step,
                    "max_quantity": self.max_quantity,
                },
                "calendar": {
                    "calendar_id": self.calendar_id,
                    "version": self.calendar_version,
                    "timezone": self.calendar_timezone,
                    "valid_from": self.calendar_valid_from,
                    "valid_until": self.calendar_valid_until,
                    "daily_break_policy": self.daily_break_policy,
                    "dst_transition_policy": self.dst_transition_policy,
                    "holiday_source": self.holiday_source,
                    "weekend_gap_policy": self.weekend_gap_policy,
                },
                "account": {
                    "account_currency": self.account_currency,
                    "conversion_source": self.conversion_source,
                    "conversion_rate_source_version": self.conversion_rate_source_version,
                    "conversion_rate_pairs": [
                        list(item) for item in self.conversion_rate_pairs
                    ],
                    "conversion_max_age_seconds": self.conversion_max_age_seconds,
                    "leverage": self.leverage,
                    "max_leverage": self.max_leverage,
                    "margin_multiplier": self.margin_multiplier,
                    "margin_policy": self.margin_policy,
                    "negative_equity_policy": self.negative_equity_policy,
                },
                "costs": {
                    "commission_rate": self.commission_rate,
                    "slippage_pips": self.slippage_pips,
                    "financing_rate_daily": self.financing_rate_daily,
                    "rollover_cutoff_utc": self.rollover_cutoff_utc.isoformat()
                    if isinstance(self.rollover_cutoff_utc, time)
                    else self.rollover_cutoff_utc,
                    "triple_rollover_weekday": self.triple_rollover_weekday,
                    "swap_rate_source": self.swap_rate_source,
                    "swap_rate_version": self.swap_rate_version,
                    "long_swap_rate": self.long_swap_rate,
                    "short_swap_rate": self.short_swap_rate,
                    "cost_model_id": self.cost_model_id,
                    "cost_model_version": self.cost_model_version,
                },
            }
        )


@dataclass(frozen=True)
class ConformanceContracts:
    market: MarketDataCapabilities
    broker: BrokerConformanceCapabilities

    def validate(self) -> ValidationResult:
        errors: list[str] = []
        errors.extend(self.market.validate().errors)
        errors.extend(self.broker.validate().errors)
        if tuple(sorted(self.market.supported_pairs)) != tuple(sorted(self.broker.supported_pairs)):
            errors.append("market_broker_pair_mismatch")
        for name in ("calendar_id", "calendar_version", "calendar_timezone"):
            if getattr(self.market, name) != getattr(self.broker, name):
                errors.append(f"calendar_{name}_mismatch")
        for name in (
            "pip_size",
            "tick_size",
            "quote_precision",
            "contract_size",
            "min_quantity",
            "quantity_step",
            "max_quantity",
        ):
            if getattr(self.market, name) != getattr(self.broker, name):
                errors.append(f"instrument_{name}_mismatch")
        return ValidationResult.fail(*errors) if errors else ValidationResult.ok()

    def audit_view(self) -> dict[str, Any]:
        return {
            "market": self.market.audit_view(),
            "broker": self.broker.audit_view(),
        }

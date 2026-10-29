"""Deterministic fixture and empty-template provider profile payloads.

The fixture profile is explicitly synthetic and reproduces the local workflow's
provider-neutral terms.  The template contains every required key with empty
values and empty provenance; it intentionally fails validation until a human
fills it from official provider documentation.
"""

from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal
from enum import Enum
from typing import Any, Mapping

from axiom_forex.conformance.contracts import REQUIRED_PROVIDER_VALUES
from axiom_forex.conformance.fixtures import build_conformance_environment

from .contracts import (
    DEFAULT_SECTION_KEYS,
    LOCAL_PAPER_ACTIVATION_MODE,
    NO_RESUBMIT_POLICY,
    PROFILE_FORMAT,
    PROFILE_SCHEMA_VERSION,
)

FIXTURE_PROFILE_ID = "offline-fixture-v1"
FIXTURE_PROFILE_VERSION = "fixture-profile-v1"
FIXTURE_SOURCE = "LOCAL_SYNTHETIC_WORKFLOW_FIXTURE"
TEMPLATE_PROFILE_ID = "provider-onboarding-template"
TEMPLATE_PROFILE_VERSION = "template-v1"


def _json_safe(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, time):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_safe(item) for item in value]
    return value


def _calendar_windows(calendar: Any) -> dict[str, Any]:
    return {
        str(pair): [
            {
                "start": _json_safe(window.start),
                "end": _json_safe(window.end),
                "label": window.label,
            }
            for window in windows
        ]
        for pair, windows in calendar.windows_by_pair.items()
    }


def _calendar_holidays(calendar: Any) -> dict[str, Any]:
    return {
        str(pair): sorted(day.isoformat() if isinstance(day, date) else str(day) for day in days)
        for pair, days in calendar.holidays_by_pair.items()
    }


def _dst_transitions(environment: Any) -> list[dict[str, Any]]:
    scenario = environment.market_adapter.scenarios
    return [
        {
            "timezone": scenario.dst_timezone,
            "before_local": _json_safe(scenario.dst_before_local),
            "after_local": _json_safe(scenario.dst_after_local),
            "before_utc_offset_seconds": scenario.dst_before_utc_offset_seconds,
            "after_utc_offset_seconds": scenario.dst_after_utc_offset_seconds,
            "offset_changed": scenario.dst_before_utc_offset_seconds
            != scenario.dst_after_utc_offset_seconds,
        }
    ]


def _fixture_provenance(
    *,
    effective_date: str,
    effective_from: str,
    expires_at: str,
    review_by: str,
) -> dict[str, str]:
    return {
        "source": FIXTURE_SOURCE,
        "source_version": FIXTURE_PROFILE_VERSION,
        "effective_date": effective_date,
        "value_class": "FIXTURE",
        "expires_at": expires_at,
        "documented_at": effective_from,
        "review_by": review_by,
    }


def build_fixture_profile_payload() -> dict[str, Any]:
    """Return an offline synthetic profile with all required values supplied."""
    environment = build_conformance_environment()
    market = environment.market_contract
    broker = environment.broker_contract
    calendar = environment.calendar
    spec = environment.spec
    scenario = environment.market_adapter.scenarios
    pair = spec.canonical_pair or ""
    provider_id = market.provider_id
    provider_symbol = environment.provider_symbol
    account_currency = environment.account.account_currency_code

    effective_from = _json_safe(calendar.valid_from)
    expires_at = _json_safe(calendar.valid_until)
    effective_date = calendar.valid_from.date().isoformat()
    reviewed_by = expires_at
    evaluated_at = _json_safe(environment.evaluated_at)
    provenance = _fixture_provenance(
        effective_date=effective_date,
        effective_from=effective_from,
        expires_at=expires_at,
        review_by=reviewed_by,
    )

    calendar_terms = {
        "calendar_id": calendar.calendar_id,
        "version": calendar.version,
        "signature": calendar.signature(),
        "timezone": calendar.timezone_name,
        "valid_from": effective_from,
        "valid_until": expires_at,
        "holidays": _calendar_holidays(calendar),
        "windows": _calendar_windows(calendar),
        "daily_break_policy": broker.daily_break_policy,
        "dst_transition_policy": broker.dst_transition_policy,
        "holiday_source": broker.holiday_source,
        "weekend_gap_policy": broker.weekend_gap_policy,
        "dst_transitions": _dst_transitions(environment),
        "instrument_availability": {pair: spec.status_value},
    }
    trading_hours = {
        **calendar_terms,
        "calendar_version": calendar.version,
        "calendar_signature": calendar.signature(),
        "supported_timeframes": sorted(market.supported_timeframes),
    }
    instrument_spec = _json_safe(spec.audit_view())
    freshness = _json_safe(environment.quote_freshness.audit_view())
    sizing = {
        "pair": pair,
        "pip_size": _json_safe(spec.pip_size_decimal()),
        "tick_size": _json_safe(spec.price_tick_size_decimal()),
        "quote_precision": spec.quote_precision,
        "contract_size": _json_safe(spec.contract_size_decimal()),
        "min_quantity": _json_safe(spec.min_quantity_decimal()),
        "quantity_step": _json_safe(spec.quantity_step_decimal()),
        "max_quantity": _json_safe(spec.max_quantity_decimal()),
        "spec_version": spec.spec_version,
        "quantity_unit": spec.quantity_unit_value,
        "margin_multiplier": _json_safe(spec.margin_multiplier_decimal()),
        "supported_pairs": [pair],
        "provider_symbols": {pair: provider_symbol},
        "timeframes": sorted(market.supported_timeframes),
        "status": spec.status_value,
    }
    rate_pairs = []
    rate_timestamps: dict[str, Any] = {}
    rate_sources: dict[str, Any] = {}
    for (from_currency, to_currency), rate in sorted(environment.conversion_rates.rates.items()):
        rate_pairs.append([from_currency, to_currency])
        key = f"{from_currency}/{to_currency}"
        rate_timestamps[key] = _json_safe(rate.timestamp)
        rate_sources[key] = rate.source
    account_terms = {
        "account_currency": account_currency,
        "conversion_source": environment.conversion_rates.source,
        "conversion_rate_source_version": f"{environment.account.conversion_source}:{FIXTURE_PROFILE_VERSION}",
        "conversion_rate_pairs": rate_pairs,
        "conversion_max_age_seconds": _json_safe(
            environment.conversion_rates.freshness.max_age_seconds
        ),
        "rate_freshness_policy": freshness,
        "rate_timestamps": rate_timestamps,
        "rate_sources": rate_sources,
        "source": environment.conversion_rates.source,
    }
    rollover_terms = _json_safe(environment.rollover_policy.audit_view())
    # Explicit fixture choice: the workflow assumes no triple-rollover rule.
    rollover_terms["triple_rollover_weekday"] = None
    cost_terms = environment.cost_terms
    costs = {
        "cost_model_id": cost_terms.model_id,
        "cost_model_version": cost_terms.version,
        "commission_rate": _json_safe(cost_terms.commission_rate),
        "slippage_pips": _json_safe(cost_terms.slippage_pips),
        "financing_rate_daily": _json_safe(cost_terms.financing_rate_daily),
        "spread_limit_pips": _json_safe(market.max_spread_pips),
        "rollover_cutoff_utc": _json_safe(environment.rollover_policy.daily_cutoff_utc),
        "triple_rollover_weekday": None,
        "swap_rate_source": "SYNTHETIC_CONFORMANCE_SWAP_SOURCE",
        "swap_rate_version": "swap-fixture-v1",
        "long_swap_rate": _json_safe(cost_terms.financing_rate_daily),
        "short_swap_rate": _json_safe(-cost_terms.financing_rate_daily),
        "rollover_policy": rollover_terms,
        "source": cost_terms.source,
    }
    margin = {
        "leverage": _json_safe(environment.account.leverage_decimal),
        "max_leverage": _json_safe(environment.risk_limits.max_leverage),
        "margin_multiplier": _json_safe(spec.margin_multiplier_decimal()),
        "margin_policy": broker.margin_policy,
        "negative_equity_policy": broker.negative_equity_policy,
        "initial_capital": _json_safe(environment.account.initial_capital),
        "margin_call_policy": "NO_NEGATIVE_EQUITY_FIXTURE",
    }

    values: dict[str, Any] = {
        "market.provider_id": provider_id,
        "market.capability_version": market.capability_version,
        "market.source": market.source,
        "market.supported_pairs": [pair],
        "market.provider_symbols": {pair: provider_symbol},
        "market.supported_timeframes": sorted(market.supported_timeframes),
        "market.quote_timestamp_basis": market.quote_timestamp_basis,
        "market.clock_source": market.clock_source,
        "market.bid_ask_semantics": market.bid_ask_semantics,
        "market.quote_id_required": market.quote_id_required,
        "market.received_at_required": market.received_at_required,
        "market.quote_correction_policy": market.quote_correction_policy,
        "market.bar_timestamp_basis": market.bar_timestamp_basis,
        "market.bar_completion_rule": market.bar_completion_rule,
        "market.bar_ohlc_semantics": market.bar_ohlc_semantics,
        "market.gap_policy": market.gap_policy,
        "market.history_bounds": {"start": effective_from, "end": expires_at},
        "market.freshness": freshness,
        "market.calendar": calendar_terms,
        "market.instrument_spec": instrument_spec,
        "market.max_spread_pips": _json_safe(market.max_spread_pips),
        "broker.broker_id": broker.broker_id,
        "broker.capability_version": broker.capability_version,
        "broker.execution_mode": broker.execution_mode_value,
        "broker.external_orders_supported": broker.external_orders_supported,
        "broker.order_types": sorted(broker.order_types),
        "broker.partial_fills": {
            "supported": broker.supports_partial_fills,
            "incremental_quantities": True,
            "fill_id_source": "LOCAL_DETERMINISTIC_FIXTURE_EVENT_ID",
        },
        "broker.cancellation": {
            "supported": broker.supports_cancellation,
            "confirmation_required": True,
            "confirmation_source": "LOCAL_FIXTURE_CANCELLATION",
        },
        "broker.reconciliation": {
            "supported": broker.supports_reconciliation,
            "source": broker.status_mapping_source,
            "retention": broker.reconciliation_retention,
            "unknown_outcome_policy": broker.unknown_outcome_policy,
            "status_mapping_source": broker.status_mapping_source,
            "order_history_source": "LOCAL_FIXTURE_ORDER_TRANSITIONS",
        },
        "broker.idempotency": {
            "key": broker.idempotency_key,
            "order_id_source": broker.order_id_source,
            "no_resubmit_policy": broker.unknown_outcome_policy,
            "client_order_id_scope": "LOCAL_PAPER_WORKFLOW",
        },
        "broker.rejection_codes": list(broker.rejection_codes),
        "broker.sizing": sizing,
        "broker.trading_hours": trading_hours,
        "broker.costs": costs,
        "broker.account_currency": account_terms,
        "broker.margin": margin,
        "broker.recovery": {
            "persistence": "SQLite snapshots",
            "no_resubmit_policy": NO_RESUBMIT_POLICY,
            "unresolved_order_policy": "PRESERVE_AND_BLOCK_NEW_ORDERS",
            "snapshot_source": "LOCAL_FIXTURE_SNAPSHOT",
            "order_state_source": "DURABLE_AUDIT_LOG",
            "fill_state_source": "DURABLE_AUDIT_LOG",
            "restart_fixture": True,
        },
    }
    missing = sorted({item.key for item in REQUIRED_PROVIDER_VALUES} - set(values))
    if missing:
        raise RuntimeError("fixture_profile_missing_values:" + ",".join(missing))

    return {
        "format": PROFILE_FORMAT,
        "schema_version": PROFILE_SCHEMA_VERSION,
        "profile": {
            "profile_id": FIXTURE_PROFILE_ID,
            "profile_version": FIXTURE_PROFILE_VERSION,
            "environment": "FIXTURE",
            "label": "SYNTHETIC_OFFLINE_FIXTURE",
            "synthetic": True,
            "external_execution_enabled": False,
            "activation_mode": LOCAL_PAPER_ACTIVATION_MODE,
            "evaluated_at": evaluated_at,
            "effective_from": effective_from,
            "expires_at": expires_at,
            "review_by": reviewed_by,
        },
        "sections": {
            str(name): list(keys) for name, keys in DEFAULT_SECTION_KEYS.items()
        },
        "terms": {
            str(key): {
                "value": _json_safe(values[key]),
                "provenance": dict(provenance),
            }
            for key in values
        },
    }


def build_template_profile_payload() -> dict[str, Any]:
    """Return an empty onboarding template with no invented provider values."""
    empty_provenance = {
        "source": "",
        "source_version": "",
        "effective_date": "",
        "value_class": "",
        "expires_at": "",
        "documented_at": "",
        "review_by": "",
    }
    return {
        "format": PROFILE_FORMAT,
        "schema_version": PROFILE_SCHEMA_VERSION,
        "profile": {
            "profile_id": TEMPLATE_PROFILE_ID,
            "profile_version": TEMPLATE_PROFILE_VERSION,
            "environment": "UNSPECIFIED",
            "label": "EMPTY_PROVIDER_ONBOARDING_TEMPLATE",
            "synthetic": False,
            "external_execution_enabled": False,
            "activation_mode": LOCAL_PAPER_ACTIVATION_MODE,
            "evaluated_at": "",
            "effective_from": "",
            "expires_at": "",
            "review_by": "",
        },
        "sections": {
            str(name): list(keys) for name, keys in DEFAULT_SECTION_KEYS.items()
        },
        "terms": {
            item.key: {"value": None, "provenance": dict(empty_provenance)}
            for item in REQUIRED_PROVIDER_VALUES
        },
    }

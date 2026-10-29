"""Fail-closed validation and compatibility checks for provider profiles."""

from __future__ import annotations

from datetime import datetime, time, timezone
from decimal import Decimal
from typing import Any, Mapping

from app.common.validation import canonical_pair, parse_decimal, validate_currency_code
from axiom_forex.conformance.contracts import (
    NO_RESUBMIT_POLICY,
    REQUIRED_PROVIDER_VALUES,
)

from .contracts import (
    LOCAL_PAPER_ACTIVATION_MODE,
    PROFILE_ENVIRONMENTS,
    PROFILE_FORMAT,
    PROFILE_VALUE_CLASSES,
    REQUIRED_SECTIONS,
    SUPPORTED_SCHEMA_VERSIONS,
    ProfileCheck,
    ProfileFinding,
    ProviderProfile,
    ProviderProfileReport,
)

EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
PLACEHOLDER_TEXTS = frozenset(
    {"", "TBD", "TODO", "UNKNOWN", "N/A", "NA", "NONE", "REPLACE_ME", "PLACEHOLDER", "FIXME", "XXX"}
)
FORBIDDEN_SOURCE_PARTS = (
    "://",
    "token",
    "secret",
    "password",
    "credential",
    "private_key",
    "api_key",
    "access_key",
    "account_id",
    "balance",
    "equity",
)

Issue = tuple[str, str, str, str]  # field, code, category, detail


def _is_supplied(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip()) and value.strip().upper() not in PLACEHOLDER_TEXTS
    if isinstance(value, (tuple, list, dict, set, frozenset)):
        return bool(value)
    if isinstance(value, bool):
        return True
    return True


def _parse_instant(value: Any, name: str) -> tuple[datetime | None, str | None]:
    if not isinstance(value, str) or not value.strip():
        return None, f"missing_{name}"
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None, f"invalid_{name}"
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    utc = parsed.astimezone(timezone.utc)
    if utc.tzinfo is None or utc.utcoffset() is None:
        return None, f"invalid_{name}"
    return utc, None


def _parse_time(value: Any, name: str) -> tuple[time | None, str | None]:
    if not isinstance(value, str) or not value.strip():
        return None, f"missing_{name}"
    try:
        parsed = time.fromisoformat(value.strip())
    except ValueError:
        return None, f"invalid_{name}"
    if parsed.tzinfo is not None:
        return None, f"timezone_aware_{name}"
    return parsed, None


def _decimal(value: Any, name: str) -> tuple[Decimal | None, str | None]:
    parsed, error = parse_decimal(value, name)
    if error or parsed is None:
        return None, error or f"invalid_{name}"
    return parsed, None


def _term_key(field: str) -> str | None:
    parts = field.split(".")
    if len(parts) >= 2 and parts[0] in {"market", "broker"}:
        return f"{parts[0]}.{parts[1]}"
    return None


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _sequence(value: Any) -> list[Any]:
    return list(value) if isinstance(value, (list, tuple)) else []


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _issue(field: str, code: str, category: str, detail: str) -> Issue:
    return field, code, category, detail


def _validate_mapping(
    key: str,
    value: Any,
    *,
    required: tuple[str, ...] = (),
    nullable: frozenset[str] = frozenset(),
    dates: tuple[str, ...] = (),
    times: tuple[str, ...] = (),
    decimals: tuple[str, ...] = (),
    integers: tuple[str, ...] = (),
    booleans: tuple[str, ...] = (),
    mappings: tuple[str, ...] = (),
    sequences: tuple[str, ...] = (),
    datetime_maps: tuple[str, ...] = (),
) -> list[Issue]:
    issues: list[Issue] = []
    if not isinstance(value, Mapping):
        return [_issue(key, "invalid_mapping", "invalid", "expected a mapping")]
    for name in required:
        field = f"{key}.{name}"
        if name not in value:
            issues.append(_issue(field, "missing_subfield", "invalid", "required subfield is absent"))
        elif name not in nullable and not _is_supplied(value.get(name)):
            issues.append(_issue(field, "empty_subfield", "invalid", "subfield must be explicit"))
    for name in dates:
        if name in value and _is_supplied(value.get(name)):
            _, error = _parse_instant(value.get(name), name)
            if error:
                issues.append(_issue(f"{key}.{name}", "invalid_timestamp", "invalid", "timestamp must be ISO 8601"))
    for name in times:
        if name in value and _is_supplied(value.get(name)):
            _, error = _parse_time(value.get(name), name)
            if error:
                issues.append(_issue(f"{key}.{name}", "invalid_time", "invalid", "time must be a naive ISO time"))
    for name in decimals:
        if name in value and _is_supplied(value.get(name)):
            _, error = _decimal(value.get(name), name)
            if error:
                issues.append(_issue(f"{key}.{name}", "invalid_decimal", "invalid", "numeric value is invalid"))
    for name in integers:
        if name in value and _is_supplied(value.get(name)):
            raw = value.get(name)
            if isinstance(raw, bool) or not isinstance(raw, int):
                issues.append(_issue(f"{key}.{name}", "invalid_integer", "invalid", "integer value is invalid"))
    for name in booleans:
        if name in value and not isinstance(value.get(name), bool):
            issues.append(_issue(f"{key}.{name}", "invalid_boolean", "invalid", "boolean value is invalid"))
    for name in mappings:
        if name in value and not isinstance(value.get(name), Mapping):
            issues.append(_issue(f"{key}.{name}", "invalid_mapping", "invalid", "expected a mapping"))
    for name in sequences:
        if name in value and not isinstance(value.get(name), (list, tuple)):
            issues.append(_issue(f"{key}.{name}", "invalid_sequence", "invalid", "expected a list"))
    for name in datetime_maps:
        raw = value.get(name)
        if not isinstance(raw, Mapping):
            issues.append(_issue(f"{key}.{name}", "invalid_timestamp_map", "invalid", "expected a timestamp mapping"))
            continue
        for item in raw.values():
            _, error = _parse_instant(item, name)
            if error:
                issues.append(_issue(f"{key}.{name}", "invalid_timestamp_value", "invalid", "timestamp value must be ISO 8601"))
                break
    return issues


def _validate_dst_transitions(key: str, value: Any) -> list[Issue]:
    issues: list[Issue] = []
    transitions = _sequence(value)
    if not transitions:
        return [_issue(f"{key}.dst_transitions", "missing_dst_transitions", "invalid", "explicit DST transitions are required")]
    for index, raw in enumerate(transitions):
        item = _mapping(raw)
        offset_before = item.get("before_utc_offset_seconds")
        offset_after = item.get("after_utc_offset_seconds")
        if not isinstance(offset_before, int) or isinstance(offset_before, bool):
            issues.append(_issue(f"{key}.dst_transitions[{index}].before_utc_offset_seconds", "invalid_dst_offset", "invalid", "explicit UTC offset required"))
        if not isinstance(offset_after, int) or isinstance(offset_after, bool):
            issues.append(_issue(f"{key}.dst_transitions[{index}].after_utc_offset_seconds", "invalid_dst_offset", "invalid", "explicit UTC offset required"))
        if isinstance(offset_before, int) and isinstance(offset_after, int) and offset_before == offset_after:
            issues.append(_issue(f"{key}.dst_transitions[{index}]", "unchanged_dst_offset", "invalid", "DST transition must record an offset change"))
        for name in ("before_local", "after_local"):
            if _is_supplied(item.get(name)):
                _, error = _parse_instant(item.get(name), name)
                if error:
                    issues.append(_issue(f"{key}.dst_transitions[{index}].{name}", "invalid_dst_timestamp", "invalid", "DST timestamp must be ISO 8601"))
            else:
                issues.append(_issue(f"{key}.dst_transitions[{index}].{name}", "missing_dst_timestamp", "invalid", "DST timestamp is required"))
    return issues


def _validate_weekday(key: str, value: Any, *, nullable: bool = True) -> list[Issue]:
    if value is None and nullable:
        return []
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 6:
        return [_issue(key, "invalid_triple_rollover_weekday", "invalid", "weekday must be null or 0..6")]
    return []


FIELD_TYPES: dict[str, str] = {
    "market.provider_id": "text",
    "market.capability_version": "text",
    "market.source": "text",
    "market.supported_pairs": "sequence",
    "market.provider_symbols": "mapping",
    "market.supported_timeframes": "sequence",
    "market.quote_timestamp_basis": "text",
    "market.clock_source": "text",
    "market.bid_ask_semantics": "text",
    "market.quote_id_required": "boolean",
    "market.received_at_required": "boolean",
    "market.quote_correction_policy": "text",
    "market.bar_timestamp_basis": "text",
    "market.bar_completion_rule": "text",
    "market.bar_ohlc_semantics": "text",
    "market.gap_policy": "text",
    "market.history_bounds": "mapping",
    "market.freshness": "mapping",
    "market.calendar": "mapping",
    "market.instrument_spec": "mapping",
    "market.max_spread_pips": "decimal",
    "broker.broker_id": "text",
    "broker.capability_version": "text",
    "broker.execution_mode": "text",
    "broker.external_orders_supported": "boolean",
    "broker.order_types": "sequence",
    "broker.partial_fills": "mapping",
    "broker.cancellation": "mapping",
    "broker.reconciliation": "mapping",
    "broker.idempotency": "mapping",
    "broker.rejection_codes": "sequence",
    "broker.sizing": "mapping",
    "broker.trading_hours": "mapping",
    "broker.costs": "mapping",
    "broker.account_currency": "mapping",
    "broker.margin": "mapping",
    "broker.recovery": "mapping",
}


def _generic_type_issue(key: str, value: Any) -> Issue | None:
    expected = FIELD_TYPES.get(key)
    if expected == "text" and (not isinstance(value, str) or not _is_supplied(value)):
        return _issue(key, "invalid_text", "invalid", "non-empty text is required")
    if expected == "sequence" and (not isinstance(value, (list, tuple)) or not value):
        return _issue(key, "invalid_sequence", "invalid", "non-empty list is required")
    if expected == "mapping" and not isinstance(value, Mapping):
        return _issue(key, "invalid_mapping", "invalid", "mapping is required")
    if expected == "boolean" and not isinstance(value, bool):
        return _issue(key, "invalid_boolean", "invalid", "boolean is required")
    if expected == "decimal":
        _, error = _decimal(value, key)
        if error or not isinstance(value, (str, int, float, Decimal)):
            return _issue(key, "invalid_decimal", "invalid", "numeric value is required")
    return None


def validate_field_value(key: str, value: Any) -> list[Issue]:
    """Return field-level issues without including raw values in diagnostics."""
    issues: list[Issue] = []
    generic = _generic_type_issue(key, value)
    if generic is not None:
        return [generic]

    if key == "market.history_bounds":
        issues.extend(
            _validate_mapping(
                key,
                value,
                required=("start", "end"),
                dates=("start", "end"),
            )
        )
    elif key == "market.freshness":
        issues.extend(
            _validate_mapping(
                key,
                value,
                required=(
                    "policy_id",
                    "version",
                    "max_age_seconds",
                    "max_future_skew_seconds",
                    "max_feed_latency_seconds",
                ),
                decimals=("max_age_seconds", "max_future_skew_seconds", "max_feed_latency_seconds"),
            )
        )
    elif key == "market.calendar":
        issues.extend(
            _validate_mapping(
                key,
                value,
                required=(
                    "calendar_id",
                    "version",
                    "signature",
                    "timezone",
                    "valid_from",
                    "valid_until",
                    "holidays",
                    "windows",
                    "daily_break_policy",
                    "dst_transition_policy",
                    "holiday_source",
                    "weekend_gap_policy",
                    "dst_transitions",
                    "instrument_availability",
                ),
                dates=("valid_from", "valid_until"),
                mappings=("holidays", "windows", "instrument_availability"),
                sequences=("dst_transitions",),
            )
        )
        issues.extend(_validate_dst_transitions(key, _mapping(value).get("dst_transitions")))
    elif key == "market.instrument_spec":
        issues.extend(
            _validate_mapping(
                key,
                value,
                required=(
                    "pair",
                    "pip_size",
                    "price_tick_size",
                    "quote_precision",
                    "contract_size",
                    "min_quantity",
                    "quantity_step",
                    "max_quantity",
                    "quantity_unit",
                    "status",
                    "spec_version",
                    "source",
                    "provider_aliases",
                    "trading_calendar_id",
                    "trading_calendar_version",
                    "margin_multiplier",
                ),
                decimals=(
                    "pip_size",
                    "price_tick_size",
                    "contract_size",
                    "min_quantity",
                    "quantity_step",
                    "max_quantity",
                    "margin_multiplier",
                ),
                integers=("quote_precision",),
                mappings=("provider_aliases",),
            )
        )
    elif key == "broker.partial_fills":
        issues.extend(
            _validate_mapping(
                key,
                value,
                required=("supported", "incremental_quantities", "fill_id_source"),
                booleans=("supported", "incremental_quantities"),
            )
        )
    elif key == "broker.cancellation":
        issues.extend(
            _validate_mapping(
                key,
                value,
                required=("supported", "confirmation_required", "confirmation_source"),
                booleans=("supported", "confirmation_required"),
            )
        )
    elif key == "broker.reconciliation":
        issues.extend(
            _validate_mapping(
                key,
                value,
                required=(
                    "supported",
                    "source",
                    "retention",
                    "unknown_outcome_policy",
                    "status_mapping_source",
                    "order_history_source",
                ),
                booleans=("supported",),
            )
        )
    elif key == "broker.idempotency":
        issues.extend(
            _validate_mapping(
                key,
                value,
                required=("key", "order_id_source", "no_resubmit_policy", "client_order_id_scope"),
            )
        )
    elif key == "broker.sizing":
        issues.extend(
            _validate_mapping(
                key,
                value,
                required=(
                    "pair",
                    "pip_size",
                    "tick_size",
                    "quote_precision",
                    "contract_size",
                    "min_quantity",
                    "quantity_step",
                    "max_quantity",
                    "spec_version",
                    "quantity_unit",
                    "margin_multiplier",
                    "supported_pairs",
                    "provider_symbols",
                    "timeframes",
                    "status",
                ),
                decimals=(
                    "pip_size",
                    "tick_size",
                    "contract_size",
                    "min_quantity",
                    "quantity_step",
                    "max_quantity",
                    "margin_multiplier",
                ),
                integers=("quote_precision",),
                mappings=("provider_symbols",),
                sequences=("supported_pairs", "timeframes"),
            )
        )
    elif key == "broker.trading_hours":
        issues.extend(
            _validate_mapping(
                key,
                value,
                required=(
                    "calendar_id",
                    "calendar_version",
                    "calendar_signature",
                    "timezone",
                    "valid_from",
                    "valid_until",
                    "daily_break_policy",
                    "dst_transition_policy",
                    "holiday_source",
                    "weekend_gap_policy",
                    "dst_transitions",
                    "windows",
                    "holidays",
                    "instrument_availability",
                    "supported_timeframes",
                ),
                dates=("valid_from", "valid_until"),
                mappings=("windows", "holidays", "instrument_availability"),
                sequences=("dst_transitions", "supported_timeframes"),
            )
        )
        issues.extend(_validate_dst_transitions(key, _mapping(value).get("dst_transitions")))
    elif key == "broker.costs":
        issues.extend(
            _validate_mapping(
                key,
                value,
                required=(
                    "cost_model_id",
                    "cost_model_version",
                    "commission_rate",
                    "slippage_pips",
                    "financing_rate_daily",
                    "spread_limit_pips",
                    "rollover_cutoff_utc",
                    "triple_rollover_weekday",
                    "swap_rate_source",
                    "swap_rate_version",
                    "long_swap_rate",
                    "short_swap_rate",
                    "rollover_policy",
                    "source",
                ),
                nullable=frozenset({"triple_rollover_weekday"}),
                times=("rollover_cutoff_utc",),
                decimals=(
                    "commission_rate",
                    "slippage_pips",
                    "financing_rate_daily",
                    "spread_limit_pips",
                    "long_swap_rate",
                    "short_swap_rate",
                ),
                mappings=("rollover_policy",),
            )
        )
        data = _mapping(value)
        issues.extend(_validate_weekday(f"{key}.triple_rollover_weekday", data.get("triple_rollover_weekday")))
        rollover = _mapping(data.get("rollover_policy"))
        issues.extend(
            _validate_mapping(
                f"{key}.rollover_policy",
                rollover,
                required=(
                    "schedule_id",
                    "version",
                    "daily_cutoff_utc",
                    "triple_rollover_weekday",
                    "calendar_id",
                    "source",
                ),
                nullable=frozenset({"triple_rollover_weekday"}),
                times=("daily_cutoff_utc",),
            )
        )
        issues.extend(
            _validate_weekday(
                f"{key}.rollover_policy.triple_rollover_weekday",
                rollover.get("triple_rollover_weekday"),
            )
        )
    elif key == "broker.account_currency":
        issues.extend(
            _validate_mapping(
                key,
                value,
                required=(
                    "account_currency",
                    "conversion_source",
                    "conversion_rate_source_version",
                    "conversion_rate_pairs",
                    "conversion_max_age_seconds",
                    "rate_freshness_policy",
                    "rate_timestamps",
                    "rate_sources",
                    "source",
                ),
                decimals=("conversion_max_age_seconds",),
                mappings=("rate_freshness_policy", "rate_timestamps", "rate_sources"),
                sequences=("conversion_rate_pairs",),
                datetime_maps=("rate_timestamps",),
            )
        )
    elif key == "broker.margin":
        issues.extend(
            _validate_mapping(
                key,
                value,
                required=(
                    "leverage",
                    "max_leverage",
                    "margin_multiplier",
                    "margin_policy",
                    "negative_equity_policy",
                    "initial_capital",
                    "margin_call_policy",
                ),
                decimals=("leverage", "max_leverage", "margin_multiplier", "initial_capital"),
            )
        )
    elif key == "broker.recovery":
        issues.extend(
            _validate_mapping(
                key,
                value,
                required=(
                    "persistence",
                    "no_resubmit_policy",
                    "unresolved_order_policy",
                    "snapshot_source",
                    "order_state_source",
                    "fill_state_source",
                    "restart_fixture",
                ),
                booleans=("restart_fixture",),
            )
        )

    if key == "market.supported_pairs":
        for item in _sequence(value):
            canonical, error = canonical_pair(item)
            if error or canonical is None or canonical != item:
                issues.append(_issue(key, "invalid_canonical_pair", "invalid", "pair must be canonical"))
    elif key == "market.provider_symbols":
        for pair, symbol in _mapping(value).items():
            canonical, error = canonical_pair(pair)
            if error or canonical is None or canonical != pair:
                issues.append(_issue(key, "invalid_provider_symbol_pair", "invalid", "alias pair must be canonical"))
            if not _is_supplied(symbol):
                issues.append(_issue(key, "empty_provider_symbol", "invalid", "provider symbol must be explicit"))
    elif key in {"market.supported_timeframes", "broker.order_types", "broker.rejection_codes"}:
        for item in _sequence(value):
            if not _is_supplied(item) or not isinstance(item, str):
                issues.append(_issue(key, "invalid_string_item", "invalid", "list items must be non-empty strings"))
    elif key == "broker.account_currency":
        for item in _sequence(_mapping(value).get("conversion_rate_pairs")):
            pair = _sequence(item)
            if len(pair) != 2:
                issues.append(_issue(f"{key}.conversion_rate_pairs", "invalid_currency_pair", "invalid", "currency pair must have two codes"))
                continue
            for code in pair:
                _, error = validate_currency_code(code, "currency")
                if error:
                    issues.append(_issue(f"{key}.conversion_rate_pairs", "invalid_currency_code", "invalid", "currency code is invalid"))
    return issues


def validate_provider_profile(
    profile: ProviderProfile,
    *,
    evaluated_at: datetime | None = None,
) -> ProviderProfileReport:
    """Validate a profile and return a safe, activation-blocking report."""
    findings: list[ProfileFinding] = []
    checks: list[ProfileCheck] = []
    missing: set[str] = set()
    invalid: set[str] = set()
    stale: set[str] = set()
    contradictory: set[str] = set()
    compatibility: set[str] = set()

    def add(category: str, field: str, code: str, detail: str) -> None:
        findings.append(ProfileFinding(field, code, category, detail))
        target = {
            "missing": missing,
            "invalid": invalid,
            "stale": stale,
            "contradictory": contradictory,
            "compatibility": invalid,
        }.get(category)
        if target is not None:
            target.add(field)
            key = _term_key(field)
            if key:
                target.add(key)
            if category == "compatibility":
                compatibility.add(code)

    def check(name: str, ok: bool, detail: str) -> bool:
        checks.append(ProfileCheck(name, ok, detail))
        if not ok:
            add("compatibility", f"compatibility.{name}", name, detail)
        return ok

    profile_evaluated = evaluated_at
    if profile_evaluated is None:
        parsed, error = _parse_instant(profile.evaluated_at, "evaluated_at")
        if error or parsed is None:
            add("invalid", "profile.evaluated_at", "invalid_evaluation_time", "explicit ISO evaluation time is required")
            profile_evaluated = EPOCH
        else:
            profile_evaluated = parsed
    assert profile_evaluated is not None
    evaluated_utc = profile_evaluated.astimezone(timezone.utc)

    for issue in profile.parse_issues:
        add("invalid", issue.field, issue.code, issue.detail)

    if profile.format != PROFILE_FORMAT:
        add("invalid", "format", "unsupported_profile_format", "profile format identifier is unsupported")
    if profile.schema_version not in SUPPORTED_SCHEMA_VERSIONS:
        add("invalid", "schema_version", "unsupported_schema_version", "schema version is not supported")
    if not _is_supplied(profile.profile_id):
        add("missing", "profile.profile_id", "missing_profile_id", "profile id is required")
    if not _is_supplied(profile.profile_version):
        add("missing", "profile.profile_version", "missing_profile_version", "profile version is required")
    if not _is_supplied(profile.label):
        add("missing", "profile.label", "missing_profile_label", "profile label is required")
    if profile.environment not in PROFILE_ENVIRONMENTS:
        add("invalid", "profile.environment", "invalid_environment", "environment must be fixture, demo, production, or unspecified")
    elif profile.environment == "UNSPECIFIED":
        add("invalid", "profile.environment", "unspecified_environment", "environment must be selected before validation can pass")
    if profile.environment == "FIXTURE" and not profile.synthetic:
        add("contradictory", "profile.synthetic", "fixture_not_marked_synthetic", "fixture environment requires synthetic=true")
    if profile.environment in {"DEMO", "PRODUCTION"} and profile.synthetic:
        add("contradictory", "profile.synthetic", "non_fixture_marked_synthetic", "demo/production profiles must not be marked synthetic")
    if profile.external_execution_enabled:
        add("contradictory", "profile.external_execution_enabled", "external_execution_forbidden", "profiles must not enable external execution")
    if profile.activation_mode != LOCAL_PAPER_ACTIVATION_MODE:
        add("invalid", "profile.activation_mode", "invalid_activation_mode", "only local paper activation is permitted")

    effective_from, from_error = _parse_instant(profile.effective_from, "effective_from")
    expires_at, expires_error = _parse_instant(profile.expires_at, "expires_at")
    review_by, review_error = _parse_instant(profile.review_by, "review_by")
    if from_error or effective_from is None:
        add("invalid", "profile.effective_from", "invalid_effective_from", "effective_from must be an ISO timestamp")
    if expires_error or expires_at is None:
        add("invalid", "profile.expires_at", "invalid_expires_at", "expires_at must be an ISO timestamp")
    if review_error or review_by is None:
        add("invalid", "profile.review_by", "invalid_review_by", "review_by must be an ISO timestamp")
    if effective_from is not None and expires_at is not None:
        if expires_at <= effective_from:
            add("invalid", "profile.expires_at", "empty_validity_window", "expires_at must be after effective_from")
        if effective_from > evaluated_utc:
            add("stale", "profile.effective_from", "not_yet_effective", "profile is not effective at evaluation time")
        if expires_at <= evaluated_utc:
            add("stale", "profile.expires_at", "profile_expired", "profile validity has expired")
    if review_by is not None and review_by <= evaluated_utc:
        add("stale", "profile.review_by", "review_due", "profile review date has passed")

    source_of_truth = {item.key for item in REQUIRED_PROVIDER_VALUES}
    for required in REQUIRED_PROVIDER_VALUES:
        key = required.key
        term = profile.terms.get(key)
        if term is None or not _is_supplied(term.value):
            add("missing", key, "missing_required_value", "required value is missing")
            continue
        for issue in validate_field_value(key, term.value):
            add(issue[2], issue[0], issue[1], issue[3])
        provenance = term.provenance
        if not _is_supplied(provenance.source):
            add("missing", f"{key}.provenance.source", "missing_provenance_source", "provider documentation/source is required")
        elif any(part in provenance.source.lower() for part in FORBIDDEN_SOURCE_PARTS):
            add("invalid", f"{key}.provenance.source", "unsafe_provenance_source", "source must be a documentation label, not a sensitive locator")
        if not _is_supplied(provenance.source_version) and not _is_supplied(provenance.effective_date):
            add("missing", f"{key}.provenance", "missing_effective_date_or_version", "effective date or source version is required")
        if provenance.value_class not in PROFILE_VALUE_CLASSES:
            add("invalid", f"{key}.provenance.value_class", "invalid_value_class", "value class must be fixture, demo, or production")
        elif profile.environment in PROFILE_VALUE_CLASSES and provenance.value_class != profile.environment:
            add("contradictory", f"{key}.provenance.value_class", "value_class_environment_mismatch", "term value class conflicts with profile environment")
        if _is_supplied(provenance.effective_date):
            effective_date, effective_error = _parse_instant(provenance.effective_date, "effective_date")
            if effective_error or effective_date is None:
                add("invalid", f"{key}.provenance.effective_date", "invalid_effective_date", "effective date must be ISO 8601")
            elif effective_date > evaluated_utc:
                add("stale", f"{key}.provenance.effective_date", "term_not_yet_effective", "term is not effective at evaluation time")
        if _is_supplied(provenance.expires_at):
            term_expires, term_expires_error = _parse_instant(provenance.expires_at, "expires_at")
            if term_expires_error or term_expires is None:
                add("invalid", f"{key}.provenance.expires_at", "invalid_term_expiry", "term expiry must be ISO 8601")
            elif term_expires <= evaluated_utc:
                add("stale", f"{key}.provenance.expires_at", "term_expired", "term provenance has expired")
        if _is_supplied(provenance.review_by):
            term_review, review_term_error = _parse_instant(provenance.review_by, "review_by")
            if review_term_error or term_review is None:
                add("invalid", f"{key}.provenance.review_by", "invalid_term_review_date", "term review date must be ISO 8601")
            elif term_review <= evaluated_utc:
                add("stale", f"{key}.provenance.review_by", "term_review_due", "term review date has passed")
        if _is_supplied(provenance.documented_at):
            documented, documented_error = _parse_instant(provenance.documented_at, "documented_at")
            if documented_error or documented is None:
                add("invalid", f"{key}.provenance.documented_at", "invalid_documented_at", "documented date must be ISO 8601")

    for section in REQUIRED_SECTIONS:
        if section not in profile.sections or not profile.sections.get(section):
            add("invalid", f"sections.{section}", "missing_section_terms", "required section has no terms")
    section_keys = {item for keys in profile.sections.values() for item in keys}
    missing_coverage = sorted(source_of_truth - section_keys)
    if missing_coverage:
        add("invalid", "sections.coverage", "incomplete_section_coverage", "required terms are not assigned to a section")
    for section, keys in profile.sections.items():
        for key in keys:
            if key not in profile.terms:
                add("invalid", f"sections.{section}", "unknown_section_term", "section references an unknown term")

    values = {key: term.value for key, term in profile.terms.items()}

    def value(key: str) -> Any:
        return values.get(key)

    def text_value(key: str) -> str:
        return _text(value(key))

    def map_value(key: str) -> dict[str, Any]:
        return _mapping(value(key))

    def list_value(key: str) -> list[Any]:
        return _sequence(value(key))

    def dec_value(key: str, field: str) -> Decimal | None:
        parsed, _ = _decimal(value(key), field)
        return parsed

    def nested_dec(key: str, name: str) -> Decimal | None:
        parsed, _ = _decimal(map_value(key).get(name), name)
        return parsed

    market_pairs = [str(item) for item in list_value("market.supported_pairs")]
    market_symbols = _mapping(value("market.provider_symbols"))
    broker_sizing = map_value("broker.sizing")
    broker_hours = map_value("broker.trading_hours")
    market_calendar = map_value("market.calendar")
    instrument = map_value("market.instrument_spec")
    account = map_value("broker.account_currency")
    costs = map_value("broker.costs")
    margin = map_value("broker.margin")
    recovery = map_value("broker.recovery")
    history = map_value("market.history_bounds")

    check(
        "profile_identity_and_mode",
        bool(text_value("market.provider_id"))
        and bool(text_value("broker.broker_id"))
        and bool(text_value("market.capability_version"))
        and bool(text_value("broker.capability_version"))
        and text_value("broker.execution_mode") == "LOCAL_PAPER"
        and value("broker.external_orders_supported") is False
        and profile.activation_mode == LOCAL_PAPER_ACTIVATION_MODE
        and profile.external_execution_enabled is False,
        "provider/broker identity and local-paper mode must be explicit",
    )

    pair_checks = bool(market_pairs) and set(market_pairs) == set(market_symbols)
    for pair in market_pairs:
        canonical, error = canonical_pair(pair)
        if error or canonical is None or canonical != pair:
            pair_checks = False
            break
    pair_checks = pair_checks and set(market_pairs) == set(str(item) for item in _sequence(broker_sizing.get("supported_pairs")))
    pair_checks = pair_checks and market_symbols == _mapping(broker_sizing.get("provider_symbols"))
    pair_checks = pair_checks and instrument.get("pair") in market_pairs
    aliases = _mapping(instrument.get("provider_aliases"))
    pair_checks = pair_checks and any(symbol in aliases.values() for symbol in market_symbols.values())
    numeric_pairs = (
        ("pip_size", "pip_size"),
        ("price_tick_size", "tick_size"),
        ("contract_size", "contract_size"),
        ("min_quantity", "min_quantity"),
        ("quantity_step", "quantity_step"),
        ("max_quantity", "max_quantity"),
        ("margin_multiplier", "margin_multiplier"),
    )
    for market_name, broker_name in numeric_pairs:
        market_number, _ = _decimal(instrument.get(market_name), market_name)
        broker_number, _ = _decimal(broker_sizing.get(broker_name), broker_name)
        if market_number is None or broker_number is None or market_number != broker_number:
            pair_checks = False
            break
    pair_checks = (
        pair_checks
        and instrument.get("spec_version") == broker_sizing.get("spec_version")
        and instrument.get("quantity_unit") == broker_sizing.get("quantity_unit")
        and instrument.get("status") == broker_sizing.get("status")
        and instrument.get("quote_precision") == broker_sizing.get("quote_precision")
    )
    check("pair_specs_and_aliases", pair_checks, "market and broker pair, alias, and sizing terms must agree")

    timeframes = [str(item) for item in list_value("market.supported_timeframes")]
    timeframe_checks = (
        bool(timeframes)
        and timeframes == [str(item) for item in _sequence(broker_sizing.get("timeframes"))]
        and timeframes == [str(item) for item in _sequence(broker_hours.get("supported_timeframes"))]
        and "UTC" in text_value("market.bar_timestamp_basis").upper()
        and text_value("market.bar_completion_rule") == "COMPLETE_ONLY"
        and text_value("market.bar_ohlc_semantics") == "SEPARATE_BID_ASK_OHLC"
        and text_value("market.gap_policy") == "REPORT_NO_INTERPOLATION"
    )
    check("timeframes_and_bar_semantics", timeframe_checks, "timeframes and UTC completed-bar semantics must agree")

    quote_checks = (
        text_value("market.bid_ask_semantics") == "SEPARATE_BID_ASK"
        and value("market.quote_id_required") is True
        and value("market.received_at_required") is True
        and text_value("market.quote_correction_policy") == "EXPLICIT_REPLACEMENT_BY_QUOTE_ID"
        and "UTC" in text_value("market.quote_timestamp_basis").upper()
        and bool(text_value("market.clock_source"))
    )
    check("quote_semantics", quote_checks, "quote IDs, receive times, corrections, and bid/ask semantics must be explicit")

    account_currency = _text(account.get("account_currency"))
    _, currency_error = validate_currency_code(account_currency, "account_currency")
    rate_pairs = {tuple(str(code) for code in _sequence(item)) for item in _sequence(account.get("conversion_rate_pairs"))}
    rate_timestamps = _mapping(account.get("rate_timestamps"))
    required_rate_pairs = {
        (pair.split("/")[0], account_currency) for pair in market_pairs if "/" in pair
    } | {
        (pair.split("/")[1], account_currency) for pair in market_pairs if "/" in pair
    }
    required_rate_keys = {f"{item[0]}/{item[1]}" for item in required_rate_pairs}
    max_rate_age, _ = _decimal(account.get("conversion_max_age_seconds"), "conversion_max_age_seconds")
    conversion_checks = (
        not currency_error
        and bool(_text(account.get("conversion_source")))
        and bool(_text(account.get("conversion_rate_source_version")))
        and bool(rate_pairs)
        and required_rate_pairs.issubset(rate_pairs)
        and required_rate_keys.issubset(set(rate_timestamps))
        and max_rate_age is not None
        and max_rate_age > 0
        and isinstance(account.get("rate_freshness_policy"), Mapping)
    )
    check("account_currency_and_conversion", conversion_checks, "account currency and timestamped conversion rates must cover every pair")

    calendar_checks = (
        market_calendar.get("calendar_id") == broker_hours.get("calendar_id")
        and market_calendar.get("version") == broker_hours.get("calendar_version")
        and market_calendar.get("signature") == broker_hours.get("calendar_signature")
        and market_calendar.get("timezone") == broker_hours.get("timezone")
        and market_calendar.get("valid_from") == broker_hours.get("valid_from")
        and market_calendar.get("valid_until") == broker_hours.get("valid_until")
        and market_calendar.get("daily_break_policy") == broker_hours.get("daily_break_policy")
        and market_calendar.get("dst_transition_policy") == broker_hours.get("dst_transition_policy")
        and market_calendar.get("holiday_source") == broker_hours.get("holiday_source")
        and market_calendar.get("weekend_gap_policy") == broker_hours.get("weekend_gap_policy")
        and history.get("start") == market_calendar.get("valid_from")
        and history.get("end") == market_calendar.get("valid_until")
        and bool(market_calendar.get("windows"))
        and isinstance(market_calendar.get("holidays"), Mapping)
        and isinstance(market_calendar.get("instrument_availability"), Mapping)
        and bool(_sequence(market_calendar.get("dst_transitions")))
        and bool(_sequence(broker_hours.get("dst_transitions")))
    )
    check("calendar_compatibility", calendar_checks, "market and broker calendars, validity bounds, DST, windows, and availability must agree")

    spread_limit, _ = _decimal(value("market.max_spread_pips"), "max_spread_pips")
    rollover = _mapping(costs.get("rollover_policy"))
    leverage, _ = _decimal(margin.get("leverage"), "leverage")
    max_leverage, _ = _decimal(margin.get("max_leverage"), "max_leverage")
    margin_multiplier, _ = _decimal(margin.get("margin_multiplier"), "margin_multiplier")
    instrument_multiplier, _ = _decimal(instrument.get("margin_multiplier"), "margin_multiplier")
    cost_spread_limit, _ = _decimal(costs.get("spread_limit_pips"), "spread_limit_pips")
    commission, _ = _decimal(costs.get("commission_rate"), "commission_rate")
    slippage, _ = _decimal(costs.get("slippage_pips"), "slippage_pips")
    financing, _ = _decimal(costs.get("financing_rate_daily"), "financing_rate_daily")
    long_swap, _ = _decimal(costs.get("long_swap_rate"), "long_swap_rate")
    short_swap, _ = _decimal(costs.get("short_swap_rate"), "short_swap_rate")
    _, cutoff_error = _parse_time(costs.get("rollover_cutoff_utc"), "rollover_cutoff_utc")
    _, rollover_cutoff_error = _parse_time(rollover.get("daily_cutoff_utc"), "daily_cutoff_utc")
    cost_checks = (
        spread_limit is not None
        and spread_limit > 0
        and cost_spread_limit == spread_limit
        and commission is not None
        and commission >= 0
        and slippage is not None
        and slippage >= 0
        and financing is not None
        and financing >= 0
        and long_swap is not None
        and short_swap is not None
        and not cutoff_error
        and not rollover_cutoff_error
        and "triple_rollover_weekday" in costs
        and "triple_rollover_weekday" in rollover
        and bool(_text(costs.get("swap_rate_source")))
        and bool(_text(costs.get("swap_rate_version")))
        and bool(_text(costs.get("cost_model_id")))
        and bool(_text(costs.get("cost_model_version")))
        and leverage is not None
        and max_leverage is not None
        and leverage <= max_leverage
        and margin_multiplier is not None
        and margin_multiplier == instrument_multiplier
        and bool(_text(margin.get("margin_policy")))
        and bool(_text(margin.get("negative_equity_policy")))
        and bool(_text(margin.get("margin_call_policy")))
    )
    check("costs_and_margin", cost_checks, "costs, financing, spread limits, and margin terms must be consistent")

    order_types = {str(item) for item in list_value("broker.order_types")}
    partials = map_value("broker.partial_fills")
    cancellation = map_value("broker.cancellation")
    reconciliation = map_value("broker.reconciliation")
    idempotency = map_value("broker.idempotency")
    recovery_check = (
        {"SIMULATED_INTENT", "MARKET", "LIMIT"}.issubset(order_types)
        and partials.get("supported") is True
        and partials.get("incremental_quantities") is True
        and cancellation.get("supported") is True
        and cancellation.get("confirmation_required") is True
        and reconciliation.get("supported") is True
        and reconciliation.get("unknown_outcome_policy") == NO_RESUBMIT_POLICY
        and idempotency.get("key") == "CLIENT_ORDER_ID"
        and bool(list_value("broker.rejection_codes"))
        and bool(_text(recovery.get("persistence")))
        and recovery.get("no_resubmit_policy") == NO_RESUBMIT_POLICY
        and bool(_text(recovery.get("unresolved_order_policy")))
        and bool(_text(recovery.get("snapshot_source")))
    )
    check("order_capabilities_and_recovery", recovery_check, "order lifecycle, idempotency, reconciliation, and no-resubmit recovery must agree")

    checks.append(
        ProfileCheck(
            "required_values_covered",
            not missing,
            "all 37 required values must be supplied with provenance",
        )
    )
    if missing:
        add("compatibility", "compatibility.required_values_covered", "required_values_covered", "required values are missing")

    passed = not findings
    status = "PASS" if passed else "FAIL"
    activation_allowed = bool(
        passed
        and profile.environment == "FIXTURE"
        and profile.synthetic is True
        and profile.external_execution_enabled is False
        and profile.activation_mode == LOCAL_PAPER_ACTIVATION_MODE
        and value("broker.execution_mode") == "LOCAL_PAPER"
        and value("broker.external_orders_supported") is False
    )
    if not passed:
        activation_reason = "validation_failed"
    elif profile.environment != "FIXTURE":
        activation_reason = "only_synthetic_fixture_profiles_can_activate"
    elif not profile.synthetic:
        activation_reason = "profile_not_marked_synthetic"
    elif profile.external_execution_enabled:
        activation_reason = "external_execution_forbidden"
    else:
        activation_reason = "eligible_for_local_paper"
    activation_status = "ACTIVATED_FOR_LOCAL_PAPER" if activation_allowed else "BLOCKED"

    term_rows: list[dict[str, Any]] = []
    for required in REQUIRED_PROVIDER_VALUES:
        key = required.key
        term = profile.terms.get(key)
        if key in missing:
            term_status = "MISSING"
        elif key in invalid:
            term_status = "INVALID"
        elif key in stale:
            term_status = "STALE"
        elif key in contradictory:
            term_status = "CONTRADICTORY"
        else:
            term_status = "PRESENT"
        provenance = term.provenance if term is not None else None
        term_rows.append(
            {
                "key": key,
                "area": required.area,
                "status": term_status,
                "value_present": bool(term is not None and _is_supplied(term.value)),
                "value_included": False,
                "provenance": provenance.safe_view() if provenance is not None else {},
            }
        )

    return ProviderProfileReport(
        profile=profile,
        status=status,
        passed=passed,
        activation_allowed=activation_allowed,
        activation_status=activation_status,
        activation_reason=activation_reason,
        evaluated_at=evaluated_utc,
        missing_fields=tuple(sorted(missing)),
        invalid_fields=tuple(sorted(invalid)),
        stale_fields=tuple(sorted(stale)),
        contradictory_fields=tuple(sorted(contradictory)),
        compatibility_failures=tuple(sorted(compatibility)),
        findings=tuple(findings),
        checks=tuple(checks),
        term_status=tuple(term_rows),
    )

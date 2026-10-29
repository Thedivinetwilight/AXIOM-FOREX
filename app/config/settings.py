"""Required configuration objects.

M1 deliberately has no production default for account currency, leverage,
strategy, pair, provider, broker, calendar, or cost model.  The only defaults
are safety settings that cannot authorize live trading.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from app.common.types import ExecutionMode, TradingMode
from app.common.validation import ValidationResult, parse_decimal, validate_currency_code


def _enum_value(value: Any) -> str:
    return value.value if hasattr(value, "value") else str(value)


def _is_secret_like(value: str) -> bool:
    marker = value.lower()
    return any(
        term in marker
        for term in ("token", "secret", "password", "credential", "private key", "api key")
    )


@dataclass(frozen=True)
class SafetySettings:
    """Immutable paper/research boundary. LIVE cannot be constructed here."""

    mode: TradingMode | str = TradingMode.PAPER
    live_trading: bool = False
    broker_mode: ExecutionMode | str = ExecutionMode.LOCAL_PAPER
    allow_live_orders: bool = False
    settings_version: str = "m1-paper-only-v1"

    def __post_init__(self) -> None:
        result = self.validate()
        if not result.valid:
            raise ValueError("Invalid safety settings: " + result.reason)

    def validate(self) -> ValidationResult:
        errors: list[str] = []
        try:
            mode = TradingMode(_enum_value(self.mode))
        except ValueError:
            mode = None
            errors.append("invalid_mode")
        if mode not in {TradingMode.RESEARCH, TradingMode.PAPER}:
            errors.append("live_or_unknown_mode_forbidden")
        if not isinstance(self.live_trading, bool) or self.live_trading:
            errors.append("live_trading_forbidden")
        try:
            execution_mode = ExecutionMode(_enum_value(self.broker_mode))
        except ValueError:
            execution_mode = None
            errors.append("unknown_broker_mode")
        if execution_mode != ExecutionMode.LOCAL_PAPER:
            errors.append("only_local_paper_adapter_permitted")
        if not isinstance(self.allow_live_orders, bool) or self.allow_live_orders:
            errors.append("live_orders_forbidden")
        if not self.settings_version.strip():
            errors.append("missing_settings_version")
        return ValidationResult.fail(*errors) if errors else ValidationResult.ok()

    def signature(self) -> dict[str, Any]:
        return {
            "mode": _enum_value(self.mode),
            "live_trading": self.live_trading,
            "broker_mode": _enum_value(self.broker_mode),
            "allow_live_orders": self.allow_live_orders,
            "version": self.settings_version,
        }


@dataclass(frozen=True)
class AccountProfile:
    """Explicit account terms. All fields must come from trusted configuration."""

    account_id: str
    account_currency: str
    leverage: Decimal
    initial_capital: Decimal
    conversion_source: str
    cost_model_id: str
    cost_model_version: str
    rollover_schedule_id: str
    rollover_schedule_version: str

    def validate(self) -> ValidationResult:
        errors: list[str] = []
        if not isinstance(self.account_id, str) or not self.account_id.strip():
            errors.append("missing_account_id")
        elif _is_secret_like(self.account_id):
            errors.append("credential_like_account_id")
        currency, currency_error = validate_currency_code(self.account_currency, "account_currency")
        if currency_error:
            errors.append(currency_error)
        leverage, leverage_error = parse_decimal(self.leverage, "leverage")
        if leverage_error or leverage is None or leverage <= 0:
            errors.append(leverage_error or "invalid_leverage")
        capital, capital_error = parse_decimal(self.initial_capital, "initial_capital")
        if capital_error or capital is None or capital <= 0:
            errors.append(capital_error or "invalid_initial_capital")
        for field_name in (
            "conversion_source",
            "cost_model_id",
            "cost_model_version",
            "rollover_schedule_id",
            "rollover_schedule_version",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                errors.append(f"missing_{field_name}")
            elif _is_secret_like(value):
                errors.append(f"credential_like_{field_name}")
        return ValidationResult.fail(*errors) if errors else ValidationResult.ok()

    @property
    def account_currency_code(self) -> str:
        code, _ = validate_currency_code(self.account_currency, "account_currency")
        return code or ""

    @property
    def leverage_decimal(self) -> Decimal:
        value, _ = parse_decimal(self.leverage, "leverage")
        if value is None:
            raise ValueError("Invalid leverage")
        return value

    @property
    def initial_capital_decimal(self) -> Decimal:
        value, _ = parse_decimal(self.initial_capital, "initial_capital")
        if value is None:
            raise ValueError("Invalid initial capital")
        return value

    def audit_view(self) -> dict[str, Any]:
        return {
            "account_id": self.account_id,
            "account_currency": self.account_currency_code,
            "leverage": self.leverage_decimal,
            "initial_capital": self.initial_capital_decimal,
            "conversion_source": self.conversion_source,
            "cost_model_id": self.cost_model_id,
            "cost_model_version": self.cost_model_version,
            "rollover_schedule_id": self.rollover_schedule_id,
            "rollover_schedule_version": self.rollover_schedule_version,
        }


@dataclass(frozen=True)
class RiskLimits:
    """Explicit hard limits. Zero never means unlimited in M1."""

    max_order_notional_account: Decimal
    max_gross_exposure_account: Decimal
    max_margin_account: Decimal
    max_abs_currency_exposure_account: Decimal
    max_correlation_group_exposure_account: Decimal
    max_open_positions: int
    max_daily_loss_account: Decimal
    max_trades_per_day: int
    max_spread_pips: Decimal
    max_leverage: Decimal
    limits_version: str

    def validate(self) -> ValidationResult:
        errors: list[str] = []
        positive_decimals = (
            "max_order_notional_account",
            "max_gross_exposure_account",
            "max_margin_account",
            "max_abs_currency_exposure_account",
            "max_correlation_group_exposure_account",
            "max_daily_loss_account",
            "max_spread_pips",
            "max_leverage",
        )
        for field_name in positive_decimals:
            value, error = parse_decimal(getattr(self, field_name), field_name)
            if error or value is None or value <= 0:
                errors.append(error or f"invalid_{field_name}")
        for field_name in ("max_open_positions", "max_trades_per_day"):
            value = getattr(self, field_name)
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                errors.append(f"invalid_{field_name}")
        if not isinstance(self.limits_version, str) or not self.limits_version.strip():
            errors.append("missing_limits_version")
        return ValidationResult.fail(*errors) if errors else ValidationResult.ok()

    def decimal(self, field_name: str) -> Decimal:
        value, error = parse_decimal(getattr(self, field_name), field_name)
        if error or value is None:
            raise ValueError(f"Invalid {field_name}")
        return value

    def audit_view(self) -> dict[str, Any]:
        return {
            "max_order_notional_account": self.decimal("max_order_notional_account"),
            "max_gross_exposure_account": self.decimal("max_gross_exposure_account"),
            "max_margin_account": self.decimal("max_margin_account"),
            "max_abs_currency_exposure_account": self.decimal(
                "max_abs_currency_exposure_account"
            ),
            "max_correlation_group_exposure_account": self.decimal(
                "max_correlation_group_exposure_account"
            ),
            "max_open_positions": self.max_open_positions,
            "max_daily_loss_account": self.decimal("max_daily_loss_account"),
            "max_trades_per_day": self.max_trades_per_day,
            "max_spread_pips": self.decimal("max_spread_pips"),
            "max_leverage": self.decimal("max_leverage"),
            "version": self.limits_version,
        }


@dataclass(frozen=True)
class SystemState:
    """Current operational switches; all are explicit inputs."""

    emergency_stop: bool
    kill_switch_active: bool
    execution_paused: bool

    def validate(self) -> ValidationResult:
        errors = []
        for field_name in ("emergency_stop", "kill_switch_active", "execution_paused"):
            if not isinstance(getattr(self, field_name), bool):
                errors.append(f"invalid_{field_name}")
        return ValidationResult.fail(*errors) if errors else ValidationResult.ok()

    def audit_view(self) -> dict[str, bool]:
        return {
            "emergency_stop": self.emergency_stop,
            "kill_switch_active": self.kill_switch_active,
            "execution_paused": self.execution_paused,
        }

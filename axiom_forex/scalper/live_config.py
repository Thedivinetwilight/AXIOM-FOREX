"""Strict, local configuration for a workstation-connected scalper PAPER desk.

No defaults in this module can silently turn an incomplete live setup into a
trading configuration. Broker instrument facts come from the already-connected
observation terminal; PAPER capital comes from AXIOM's local paper-capital file;
cost and scalper risk terms are operator-declared in a separate local JSON file.
"""
from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping

from axiom_forex.paper_runner.accounting import read_capital

from .config import CostTerms, InstrumentSpec, ScalperConfig, build_config

SCHEMA = "axiom-scalper-paper-v1"
RISK_KEYS = (
    "risk_fraction", "max_loss_per_trade_account", "max_aggregate_risk_fraction",
    "max_leverage", "max_currency_exposure_fraction", "max_correlated_positions",
    "max_daily_loss_fraction", "max_drawdown_fraction", "max_loss_streak",
    "max_spread_points_hard", "max_slippage_points", "max_fill_latency_ms",
    "max_open_positions", "max_quote_age_ms", "max_spread_points", "max_hold_ms",
    "break_even_trigger_points", "trail_after_points", "trail_distance_points",
)


class LiveScalperConfigError(ValueError):
    def __init__(self, reason: str, remediation: str):
        self.reason, self.remediation = reason, remediation
        super().__init__(f"{reason}: {remediation}")


def _decimal(value: Any, key: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise LiveScalperConfigError(f"invalid_{key}", f"Set {key} to a finite decimal in the local scalper PAPER settings.") from None
    if not result.is_finite():
        raise LiveScalperConfigError(f"invalid_{key}", f"Set {key} to a finite decimal in the local scalper PAPER settings.")
    return result


def load_live_paper_config(*, session: Any, settings_path: str | Path,
                           paper_root: str | Path) -> tuple[ScalperConfig, Decimal, str]:
    """Build from fresh terminal facts and explicit local terms; never connects.

    The JSON document contains ``schema``, exact broker ``symbol``, ``costs``,
    ``risk`` and an explicitly declared per-lot ``margin_rate``. It must not carry
    account balance or starting capital: capital is read only from AXIOM's existing
    local paper-capital document.
    """
    path = Path(settings_path)
    if not bool(getattr(session, "connected", False)):
        raise LiveScalperConfigError("terminal_not_connected", "Press Connect on Setup before configuring or starting AX-SCALPER.")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise LiveScalperConfigError("scalper_settings_missing", f"Create the local settings file at {path}; declare costs and every required risk limit. No values are assumed.") from None
    except (OSError, ValueError) as exc:
        raise LiveScalperConfigError("scalper_settings_unreadable", f"Repair {path} ({type(exc).__name__}); no settings were changed.") from None
    if not isinstance(payload, Mapping) or payload.get("schema") != SCHEMA:
        raise LiveScalperConfigError("scalper_settings_schema_invalid", f"Use schema {SCHEMA!r} in {path}.")
    unknown_top = set(payload) - {"schema", "symbol", "margin_rate", "costs", "risk"}
    if unknown_top:
        raise LiveScalperConfigError("unknown_scalper_setting", "Remove unsupported setting(s): " + ", ".join(sorted(map(str, unknown_top))))
    symbol = payload.get("symbol")
    if not isinstance(symbol, str) or not symbol:
        raise LiveScalperConfigError("scalper_symbol_missing", "Choose an exact broker symbol from the connected terminal's verified tradable Forex symbols.")
    from axiom_forex.workstation.symbols import build_catalogue
    records = tuple(item for item in session.symbols() if isinstance(item, Mapping))
    catalogue = build_catalogue(records, specs_reported=True)
    choice = next((item for item in catalogue.pairs if item.broker_symbol == symbol), None)
    if choice is None or not choice.usable or not choice.tradable:
        raise LiveScalperConfigError("scalper_symbol_not_verified", f"{symbol!r} is not an exact verified tradable Forex symbol in the connected terminal. Select one shown by AXIOM; do not substitute a suffix.")
    terminal = session.market_data_source()
    if terminal is None:
        raise LiveScalperConfigError("market_data_unavailable", "Reconnect explicitly on Setup; no background connection is attempted.")
    try:
        broker = terminal.symbol_info(symbol)
    except Exception as exc:
        raise LiveScalperConfigError("symbol_spec_unavailable", f"Refresh the connected terminal symbol facts ({type(exc).__name__}).") from None
    if str(getattr(broker, "name", "")) != symbol:
        raise LiveScalperConfigError("symbol_identity_mismatch", "The broker returned a different symbol name. Refresh the terminal symbol list and use the exact byte-for-byte name.")
    if not getattr(broker, "tradable", False):
        raise LiveScalperConfigError("symbol_not_tradable", f"{symbol!r} is not currently verified as fully tradable by the terminal.")

    account = session.account()
    account_currency = str(account.get("currency") or "").strip().upper()
    quote_currency = str(getattr(broker, "currency_profit", "") or "").strip().upper()
    if not account_currency or len(account_currency) != 3:
        raise LiveScalperConfigError("account_currency_unavailable", "Connect to an account whose currency is reported by the observation session.")
    if quote_currency != account_currency:
        raise LiveScalperConfigError("conversion_rate_missing", f"The symbol quote currency ({quote_currency or 'unknown'}) differs from paper capital currency ({account_currency}); provide a separately verified conversion feed before using this pair.")
    base_currency = str(getattr(broker, "currency_base", "") or "").strip().upper()
    if len(base_currency) != 3 or len(quote_currency) != 3:
        raise LiveScalperConfigError("symbol_currency_facts_missing", "Use a terminal-verified Forex symbol with both currency legs reported.")

    capital = read_capital(paper_root)
    if not capital.configured:
        raise LiveScalperConfigError("paper_capital_missing", "Set local simulated capital on the existing Performance page. Broker balance is never used.")
    if capital.currency != account_currency:
        raise LiveScalperConfigError("paper_capital_currency_mismatch", f"Set local paper capital in {account_currency}; it must not be copied from broker balance.")

    margin_rate = _decimal(payload.get("margin_rate"), "margin_rate")
    if not (Decimal(0) < margin_rate <= Decimal(1)):
        raise LiveScalperConfigError("margin_rate_out_of_range", "Declare margin_rate as a fraction greater than zero and no greater than one.")
    instrument = InstrumentSpec(
        symbol=symbol, pair=f"{base_currency}/{quote_currency}",
        point=broker.point, digits=broker.digits,
        contract_size=broker.trade_contract_size,
        volume_min=broker.volume_min, volume_step=broker.volume_step,
        volume_max=broker.volume_max, stops_level_points=Decimal(broker.stops_level),
        margin_rate=margin_rate, quote_currency=quote_currency,
        account_currency=account_currency, source="CONNECTED_MT5_SYMBOL_INFO",
    )
    if not instrument.complete:
        raise LiveScalperConfigError("symbol_terms_incomplete", "Refresh the broker's symbol specification; contract, point, volume and currency facts must all be present.")

    costs = payload.get("costs")
    if not isinstance(costs, Mapping):
        raise LiveScalperConfigError("cost_terms_missing", "Declare commission, slippage and financing state in the local scalper PAPER settings.")
    extra_costs = set(costs) - {"commission_per_lot", "commission_state", "slippage_points", "slippage_state", "swap_state"}
    if extra_costs:
        raise LiveScalperConfigError("unknown_cost_setting", "Remove unsupported cost field(s): " + ", ".join(sorted(map(str, extra_costs))))
    try:
        cost = CostTerms(
            commission_per_lot=_decimal(costs["commission_per_lot"], "commission_per_lot"),
            commission_state=str(costs["commission_state"]),
            slippage_points=_decimal(costs["slippage_points"], "slippage_points"),
            slippage_state=str(costs["slippage_state"]),
            swap_state=str(costs["swap_state"]),
        )
    except KeyError as exc:
        raise LiveScalperConfigError("cost_term_missing", f"Declare {exc.args[0]} explicitly; unknown costs cannot be zero-filled.") from None
    cost_errors = cost.validate()
    if (cost_errors or cost.commission_state not in {"OBSERVED", "ESTIMATE"}
            or cost.slippage_state not in {"OBSERVED", "ESTIMATE"}
            or cost.swap_state not in {"OBSERVED", "ESTIMATE"}):
        raise LiveScalperConfigError("cost_terms_unusable", "Replace unknown or invalid cost states with operator-verified/declared values; do not label unknown costs as zero.")
    risk = payload.get("risk")
    if not isinstance(risk, Mapping):
        raise LiveScalperConfigError("scalper_risk_limits_missing", "Declare every required scalper risk limit with its documented units in the local settings.")
    extra_risk = set(risk) - set(RISK_KEYS)
    if extra_risk:
        raise LiveScalperConfigError("unknown_scalper_risk_setting", "Remove unsupported risk field(s): " + ", ".join(sorted(map(str, extra_risk))))
    missing = [key for key in RISK_KEYS if key not in risk]
    if missing:
        raise LiveScalperConfigError("scalper_risk_limit_missing", "Declare required risk setting(s): " + ", ".join(missing))
    parsed: dict[str, Any] = {}
    integer_keys = {"max_correlated_positions", "max_loss_streak", "max_fill_latency_ms", "max_open_positions", "max_quote_age_ms", "max_hold_ms"}
    for key in RISK_KEYS:
        try:
            if key in integer_keys:
                integer_value = _decimal(risk[key], key)
                if integer_value != integer_value.to_integral_value():
                    raise ValueError("not an integer")
                parsed[key] = int(integer_value)
            else:
                parsed[key] = _decimal(risk[key], key)
        except (TypeError, ValueError):
            raise LiveScalperConfigError(f"invalid_{key}", f"Set {key} to a valid integer or finite decimal with the documented units.") from None
    for key in ("max_loss_per_trade_account", "max_leverage"):
        if parsed[key] <= 0:
            raise LiveScalperConfigError(f"{key}_not_positive", f"Declare a positive {key}; values are not inferred from the broker balance.")
    if not (0 < parsed["max_currency_exposure_fraction"] <= 1):
        raise LiveScalperConfigError("max_currency_exposure_fraction_out_of_range", "Declare a fraction greater than zero and no greater than 1.")
    for key in ("max_correlated_positions", "max_loss_streak", "max_fill_latency_ms"):
        if parsed[key] <= 0:
            raise LiveScalperConfigError(f"{key}_not_positive", f"Declare a positive integer for {key}.")
    if parsed["max_spread_points_hard"] < parsed["max_spread_points"]:
        raise LiveScalperConfigError("hard_spread_below_soft_spread", "The hard spread limit must be at least the soft spread limit, both in broker points.")
    if parsed["max_slippage_points"] < 0:
        raise LiveScalperConfigError("max_slippage_points_negative", "Declare a non-negative slippage limit in broker points.")
    for key in ("break_even_trigger_points", "trail_after_points", "trail_distance_points"):
        if parsed[key] < 0:
            raise LiveScalperConfigError(f"{key}_negative", f"Declare a non-negative {key} in broker points.")
    try:
        config = build_config(instruments=(instrument,), cost=cost, **parsed)
    except (ValueError, TypeError) as exc:
        reason = str(getattr(exc, "reason", "scalper_config_invalid"))
        raise LiveScalperConfigError(reason, "Correct the local scalper settings using the validation reason; no defaults were substituted.") from None
    return config, Decimal(capital.amount), account_currency

"""Account currency, leverage, margin, and margin-mode handling.

AXIOM's gate needs the account currency (for FX conversion) and a margin
multiplier.  Both are broker facts, so this module *reads* them from the
terminal and compares them with what the catalog/operator declared.  It never
derives a currency from a pair name, never assumes a leverage, and never
converts a margin-mode name into a number.

Netting vs hedging matters for more than reporting: on a netting account one
symbol has at most one position and an opposite order *reduces* it, while on a
hedging account an opposite order opens a second position.  An AXIOM close that
expects hedging semantics on a netting account (or the reverse) is refused
rather than reinterpreted.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from .terminal import (
    ACCOUNT_MARGIN_MODE_RETAIL_HEDGING,
    ACCOUNT_MARGIN_MODE_RETAIL_NETTING,
    MT5Error,
    MT5Terminal,
    MT5TerminalStatus,
    _decimal,
)


class MT5AccountMismatch(MT5Error):
    """Declared account terms and broker-reported terms disagree."""


def position_accounting(margin_mode: int) -> str:
    if margin_mode == ACCOUNT_MARGIN_MODE_RETAIL_NETTING:
        return "NETTING"
    if margin_mode == ACCOUNT_MARGIN_MODE_RETAIL_HEDGING:
        return "HEDGING"
    return f"UNSUPPORTED_{margin_mode}"


@dataclass(frozen=True)
class AccountTerms:
    """What AXIOM needs to know about the account, all broker-reported."""

    login: int
    server: str
    currency: str
    currency_digits: int
    leverage: int
    trade_mode: str
    margin_mode: str
    position_accounting: str
    fifo_close: bool
    trade_allowed: bool
    margin_call_level: Decimal | None
    stop_out_level: Decimal | None
    balance: Decimal | None
    equity: Decimal | None
    margin: Decimal | None
    margin_free: Decimal | None
    margin_level: Decimal | None
    source: str = "mt5_account_info"

    @property
    def hedging_supported(self) -> bool:
        return self.position_accounting == "HEDGING"

    def audit_view(self) -> dict[str, Any]:
        return {
            "login": self.login,
            "server": self.server,
            "currency": self.currency,
            "currency_digits": self.currency_digits,
            "leverage": self.leverage,
            "trade_mode": self.trade_mode,
            "margin_mode": self.margin_mode,
            "position_accounting": self.position_accounting,
            "fifo_close": self.fifo_close,
            "trade_allowed": self.trade_allowed,
            "margin_call_level": self.margin_call_level,
            "stop_out_level": self.stop_out_level,
            "balance": self.balance,
            "equity": self.equity,
            "margin": self.margin,
            "margin_free": self.margin_free,
            "margin_level": self.margin_level,
            "source": self.source,
        }


def account_terms(status: MT5TerminalStatus) -> AccountTerms:
    accounting = position_accounting(status.margin_mode)
    if accounting.startswith("UNSUPPORTED"):
        # Exchange-margin accounts price forex differently; refusing keeps us
        # from applying retail margin maths to them.
        raise MT5AccountMismatch("mt5_margin_mode_unsupported", accounting)
    if not status.account_currency or not status.account_currency.strip():
        raise MT5AccountMismatch("mt5_account_currency_missing")
    if status.leverage <= 0:
        raise MT5AccountMismatch("mt5_account_leverage_missing")
    return AccountTerms(
        login=status.account_login,
        server=status.account_server,
        currency=status.account_currency.strip().upper(),
        currency_digits=status.currency_digits,
        leverage=status.leverage,
        trade_mode=status.trade_mode_name,
        margin_mode=status.margin_mode_name,
        position_accounting=accounting,
        fifo_close=status.fifo_close,
        trade_allowed=status.trade_allowed,
        margin_call_level=status.margin_so_call,
        stop_out_level=status.margin_so_so,
        balance=status.balance,
        equity=status.equity,
        margin=status.margin,
        margin_free=status.margin_free,
        margin_level=status.margin_level,
    )


def require_declared_currency(
    terms: AccountTerms, declared_currency: str | None
) -> AccountTerms:
    """The catalog's declared account currency must be the broker's."""

    if not declared_currency:
        raise MT5AccountMismatch("account_currency_not_declared")
    if terms.currency != str(declared_currency).strip().upper():
        raise MT5AccountMismatch(
            "mt5_account_currency_mismatch",
            f"broker={terms.currency} declared={declared_currency}",
        )
    return terms


def require_declared_leverage(terms: AccountTerms, declared_leverage: Any) -> AccountTerms:
    """The declared leverage must equal the account's, or margins are fiction."""

    try:
        leverage = int(declared_leverage)
    except (TypeError, ValueError):
        raise MT5AccountMismatch("leverage_not_declared") from None
    if leverage != terms.leverage:
        raise MT5AccountMismatch(
            "mt5_account_leverage_mismatch",
            f"broker={terms.leverage} declared={leverage}",
        )
    return terms


def require_margin_mode(terms: AccountTerms, expected: str) -> AccountTerms:
    if terms.position_accounting != str(expected).strip().upper():
        raise MT5AccountMismatch(
            "mt5_position_accounting_mismatch",
            f"broker={terms.position_accounting} declared={expected}",
        )
    return terms


def margin_for_order(
    terminal: MT5Terminal,
    *,
    side: str,
    symbol: str,
    volume: Decimal,
    price: Decimal,
) -> Decimal:
    """Broker-computed margin for a hypothetical order.

    ``order_calc_margin`` is the only honest source: AXIOM must not recompute it
    from a leverage rule the broker may not use.  ``None`` means the terminal
    could not compute it and the order is refused upstream.
    """

    action = _order_type_value(terminal, side)
    try:
        value = terminal.module.order_calc_margin(action, symbol, float(volume), float(price))
    except Exception as exc:
        raise MT5Error("mt5_margin_calc_failed", type(exc).__name__) from exc
    margin = _decimal(value)
    if margin is None or margin < 0:
        raise MT5Error("mt5_margin_calc_unavailable", symbol)
    return margin


def profit_for_order(
    terminal: MT5Terminal,
    *,
    side: str,
    symbol: str,
    volume: Decimal,
    price_open: Decimal,
    price_close: Decimal,
) -> Decimal | None:
    action = _order_type_value(terminal, side)
    try:
        value = terminal.module.order_calc_profit(
            action, symbol, float(volume), float(price_open), float(price_close)
        )
    except Exception:
        return None
    return _decimal(value)


def _order_type_value(terminal: MT5Terminal, side: str) -> int:
    name = "ORDER_TYPE_BUY" if str(side).upper() == "BUY" else "ORDER_TYPE_SELL"
    value = getattr(terminal.module, name, None)
    if value is None:
        # Official constants: buy=0, sell=1.  Use them only when the module does
        # not expose the constant at all (a hand-built fake in tests).
        value = 0 if name.endswith("BUY") else 1
    return int(value)

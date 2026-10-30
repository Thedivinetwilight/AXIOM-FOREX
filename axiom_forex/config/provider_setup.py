"""The operator's provider setup document: broker terms, read strictly.

A real symbol cannot be traded from a fixture.  The broker publishes *prices and
contract terms* (through the terminal), but it does not publish, in any machine
readable form this build can trust, the account terms, cost schedule, rollover
cutoff, risk limits, session calendar, or the pip-size convention.  Those are the
operator's to state.  This module reads them from one local JSON file and refuses
anything incomplete, rather than filling a gap with a default that would quietly
change size, cost, or risk.

Schema ``axiom-provider-setup-v1``::

    {
      "schema": "axiom-provider-setup-v1",
      "account":  {"account_id": "...", "currency": "USD", "leverage": "100",
                   "initial_capital": "10000", "conversion_source": "..."},
      "costs":    {"model_id": "...", "version": "...", "commission_rate": "0.00002",
                   "slippage_pips": "0.2", "financing_rate_daily": "0.00001",
                   "source": "..."},
      "rollover": {"schedule_id": "...", "version": "...", "daily_cutoff_utc": "21:00",
                   "triple_rollover_weekday": 2, "source": "..."},
      "risk":     {"max_order_notional_account": "2000", ...},
      "calendar": {"calendar_id": "...", "version": "...", "source": "...",
                   "timezone_name": "America/New_York",
                   "valid_from": "2026-01-01T00:00:00-05:00",
                   "valid_until": "2027-01-01T00:00:00-05:00",
                   "windows": {"EUR/USD": [["...", "...", "label"]]},
                   "holidays": {"EUR/USD": ["2026-12-25"]}},
      "symbols":  [{"pair": "EUR/USD", "broker_symbol": "EURUSD.pro", "suffix": ".pro",
                    "pip_size": "0.0001", "calendar_id": "...", "calendar_version": "...",
                    "correlation_group": "USD_MAJORS", "margin_multiplier": "1",
                    "spec_version": "..."}],
      "execution": {"quantity": "0.10", "max_open_positions": 1, "allow_short": true},
      "swap":     [{"pair": "EUR/USD", "long_rate": "-0.0000074",
                    "short_rate": "0.0000012", "source": "...", "version": "..."}],
      "conversion_rates": [{"from": "USD", "to": "EUR", "rate": "0.92",
                            "timestamp": "..."}]
    }

Notes that matter:

* ``symbols[].broker_symbol`` is the *exact* terminal symbol for this account.  If
  it is omitted, ``pair`` plus ``suffix`` must match exactly; if both are omitted,
  only the bare six-letter pair is accepted.  Nothing is guessed.
* ``calendar`` windows are the broker's session windows for that pair, in the
  stated timezone.  A pair with no windows is refused.
* ``swap`` carries the broker's overnight financing points per lot.  Without a
  card for a pair the stack refuses: financing that is not modelled would
  understate the cost of holding a position overnight.
* ``conversion_rates`` are only needed when a pair's quote currency differs from
  the account currency; identity conversions are implied, never invented.
* The document holds no credential, and this loader rejects anything that looks
  like one.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping, Sequence

from app.accounting.costs import CostModelTerms
from app.accounting.fx import ConversionRate, ConversionRateBook
from app.accounting.rollover import RolloverPolicy
from app.calendar.trading import CalendarWindow, ExplicitTradingCalendar
from app.common.validation import as_utc
from app.config.settings import AccountProfile, RiskLimits
from app.data.models import QuoteFreshness

from axiom_forex.risk.entry_limits import EntryExposureLimit

SCHEMA = "axiom-provider-setup-v1"

#: Field names that would mean a credential is being handed to us.
CREDENTIAL_KEYS = frozenset(
    {
        "password",
        "passwd",
        "pwd",
        "token",
        "access_token",
        "secret",
        "api_key",
        "investor_password",
        "market_access_token",
    }
)

REQUIRED_SECTIONS = ("account", "costs", "rollover", "risk", "calendar", "execution")
#: ``symbols`` is a list, checked separately below.
REQUIRED_KEYS = REQUIRED_SECTIONS + ("symbols",)


class ProviderSetupError(Exception):
    """The document cannot be used at all."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(reason)
        self.reason = reason
        self.detail = detail

    def audit_view(self) -> dict[str, Any]:
        return {"reason": self.reason, "detail": self.detail}


@dataclass(frozen=True)
class SymbolTerms:
    """The per-pair terms the terminal cannot tell us, plus the exact symbol."""

    pair: str
    broker_symbol: str
    suffix: str
    pip_size: Decimal
    calendar_id: str
    calendar_version: str
    correlation_group: str
    margin_multiplier: Decimal
    spec_version: str
    source: str

    def missing(self) -> tuple[str, ...]:
        missing: list[str] = []
        if not str(self.pair).strip():
            missing.append("pair")
        for name in (
            "calendar_id",
            "calendar_version",
            "correlation_group",
            "spec_version",
            "source",
        ):
            if not str(getattr(self, name)).strip():
                missing.append(name)
        try:
            if Decimal(self.pip_size) <= 0:
                missing.append("pip_size")
        except (InvalidOperation, ValueError):
            missing.append("pip_size")
        return tuple(missing)


@dataclass(frozen=True)
class SwapTerms:
    """Broker swap points per lot per night; operator-supplied, never assumed."""

    pair: str
    long_rate: Decimal
    short_rate: Decimal
    source: str
    version: str


@dataclass(frozen=True)
class ExecutionTerms:
    quantity: Decimal
    max_open_positions: int = 1
    allow_short: bool = True


@dataclass(frozen=True)
class ConversionRateTerms:
    from_currency: str
    to_currency: str
    rate: Decimal
    timestamp: datetime


@dataclass(frozen=True)
class ProviderSetup:
    """Every operator-supplied term, as validated objects."""

    path: str
    account: AccountProfile
    costs: CostModelTerms
    rollover: RolloverPolicy
    risk: RiskLimits
    calendar: ExplicitTradingCalendar
    symbols: tuple[SymbolTerms, ...]
    execution: ExecutionTerms
    swap: tuple[SwapTerms, ...] = ()
    conversion_rates: tuple[ConversionRateTerms, ...] = ()
    quote_freshness: QuoteFreshness | None = None
    #: The operator-declared hard cap on in-flight entry notional.  Without it
    #: the reservation book refuses new entries (closes still work).
    entry_limit: EntryExposureLimit | None = None

    def pairs(self) -> tuple[str, ...]:
        return tuple(item.pair for item in self.symbols)

    def terms_for(self, pair: str) -> SymbolTerms | None:
        canonical = _canonical(pair)
        for item in self.symbols:
            if _canonical(item.pair) == canonical:
                return item
        return None

    def calendar_pairs(self) -> tuple[str, ...]:
        return self.calendar.instruments

    def swap_for(self, pair: str) -> SwapTerms | None:
        canonical = _canonical(pair)
        for item in self.swap:
            if _canonical(item.pair) == canonical:
                return item
        return None

    def rate_book(self, *, source: str) -> ConversionRateBook:
        """The conversion book; identity conversions need no entry."""

        rates = {
            (item.from_currency, item.to_currency): ConversionRate(
                from_currency=item.from_currency,
                to_currency=item.to_currency,
                rate=item.rate,
                timestamp=item.timestamp,
                source=source,
            )
            for item in self.conversion_rates
        }
        freshness = self.quote_freshness or QuoteFreshness(
            policy_id="provider-setup-rates",
            version="v1",
            max_age_seconds=Decimal("86400"),
            max_future_skew_seconds=Decimal("60"),
            max_feed_latency_seconds=Decimal("86400"),
        )
        return ConversionRateBook(source=source, freshness=freshness, rates=rates)

    def audit_view(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "path": self.path,
            "account_id": self.account.account_id,
            "account_currency": self.account.account_currency,
            "leverage": str(self.account.leverage),
            "pairs": list(self.pairs()),
            "symbols": [
                {
                    "pair": item.pair,
                    "broker_symbol": item.broker_symbol,
                    "suffix": item.suffix,
                    "pip_size": str(item.pip_size),
                    "spec_version": item.spec_version,
                }
                for item in self.symbols
            ],
            "calendar_id": self.calendar.calendar_id,
            "calendar_version": self.calendar.version,
            "calendar_pairs": list(self.calendar_pairs()),
            "cost_model": f"{self.costs.model_id}/{self.costs.version}",
            "quantity": str(self.execution.quantity),
        }


def _canonical(pair: str) -> str:
    return (
        str(pair or "").replace("/", "").replace("-", "").replace("_", "").strip().upper()
    )


def _decimal(value: Any, name: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise ProviderSetupError(f"invalid_{name}", repr(value))
    try:
        parsed = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise ProviderSetupError(f"invalid_{name}", str(value)) from exc
    if not parsed.is_finite():
        raise ProviderSetupError(f"invalid_{name}", str(value))
    return parsed


def _section(payload: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    value = payload.get(name)
    if not isinstance(value, Mapping):
        raise ProviderSetupError(f"missing_section:{name}")
    return value


def _text(section: Mapping[str, Any], name: str, *, owner: str) -> str:
    value = section.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ProviderSetupError(f"missing_{owner}.{name}")
    return value.strip()


def load_provider_setup(path: str | Path) -> ProviderSetup:
    """Read and validate one provider setup document.  No network, no secrets."""

    location = Path(path)
    try:
        raw = location.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ProviderSetupError("provider_setup_missing", str(location)) from exc
    except OSError as exc:
        raise ProviderSetupError("provider_setup_unreadable", type(exc).__name__) from exc
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ProviderSetupError("provider_setup_malformed", exc.msg) from exc
    if not isinstance(payload, Mapping):
        raise ProviderSetupError("provider_setup_not_an_object")

    lowered = json.dumps(payload).lower()
    for key in CREDENTIAL_KEYS:
        if f'"{key}"' in lowered:
            # A credential in a plain local document is a hard stop, not a warning.
            raise ProviderSetupError("provider_setup_contains_credential_field", key)

    if str(payload.get("schema", "")) != SCHEMA:
        raise ProviderSetupError("provider_setup_wrong_schema", str(payload.get("schema", "")))
    for name in REQUIRED_KEYS:
        if name not in payload:
            raise ProviderSetupError(f"missing_section:{name}")
    for name in REQUIRED_SECTIONS:
        _section(payload, name)

    account_section = _section(payload, "account")
    cost_section = _section(payload, "costs")
    rollover_section = _section(payload, "rollover")
    risk_section = _section(payload, "risk")
    calendar_section = _section(payload, "calendar")

    account = AccountProfile(
        account_id=_text(account_section, "account_id", owner="account"),
        account_currency=_text(account_section, "currency", owner="account"),
        leverage=_decimal(account_section.get("leverage"), "account.leverage"),
        initial_capital=_decimal(
            account_section.get("initial_capital"), "account.initial_capital"
        ),
        conversion_source=_text(account_section, "conversion_source", owner="account"),
        cost_model_id=_text(cost_section, "model_id", owner="costs"),
        cost_model_version=_text(cost_section, "version", owner="costs"),
        rollover_schedule_id=_text(rollover_section, "schedule_id", owner="rollover"),
        rollover_schedule_version=_text(rollover_section, "version", owner="rollover"),
    )
    _require(account.validate(), "account")

    costs = CostModelTerms(
        model_id=_text(cost_section, "model_id", owner="costs"),
        version=_text(cost_section, "version", owner="costs"),
        commission_rate=_decimal(
            cost_section.get("commission_rate"), "costs.commission_rate"
        ),
        slippage_pips=_decimal(cost_section.get("slippage_pips"), "costs.slippage_pips"),
        financing_rate_daily=_decimal(
            cost_section.get("financing_rate_daily"), "costs.financing_rate_daily"
        ),
        source=_text(cost_section, "source", owner="costs"),
    )
    _require(costs.validate(), "costs")

    cutoff = rollover_section.get("daily_cutoff_utc")
    if not isinstance(cutoff, str) or not cutoff.strip():
        raise ProviderSetupError("missing_rollover.daily_cutoff_utc")
    try:
        hours, minutes = (int(part) for part in cutoff.strip().split(":", 1))
        cutoff_time = time(hour=hours, minute=minutes)
    except (ValueError, TypeError) as exc:
        raise ProviderSetupError("invalid_rollover.daily_cutoff_utc", str(cutoff)) from exc
    weekday = rollover_section.get("triple_rollover_weekday")
    rollover = RolloverPolicy(
        schedule_id=_text(rollover_section, "schedule_id", owner="rollover"),
        version=_text(rollover_section, "version", owner="rollover"),
        daily_cutoff_utc=cutoff_time,
        triple_rollover_weekday=None if weekday is None else int(weekday),
        calendar_id=_text(calendar_section, "calendar_id", owner="calendar"),
        source=_text(rollover_section, "source", owner="rollover"),
    )
    _require(rollover.validate(), "rollover")

    risk = RiskLimits(
        max_order_notional_account=_decimal(
            risk_section.get("max_order_notional_account"), "risk.max_order_notional_account"
        ),
        max_gross_exposure_account=_decimal(
            risk_section.get("max_gross_exposure_account"), "risk.max_gross_exposure_account"
        ),
        max_margin_account=_decimal(
            risk_section.get("max_margin_account"), "risk.max_margin_account"
        ),
        max_abs_currency_exposure_account=_decimal(
            risk_section.get("max_abs_currency_exposure_account"),
            "risk.max_abs_currency_exposure_account",
        ),
        max_correlation_group_exposure_account=_decimal(
            risk_section.get("max_correlation_group_exposure_account"),
            "risk.max_correlation_group_exposure_account",
        ),
        max_open_positions=int(risk_section.get("max_open_positions", 0) or 0),
        max_daily_loss_account=_decimal(
            risk_section.get("max_daily_loss_account"), "risk.max_daily_loss_account"
        ),
        max_trades_per_day=int(risk_section.get("max_trades_per_day", 0) or 0),
        max_spread_pips=_decimal(risk_section.get("max_spread_pips"), "risk.max_spread_pips"),
        max_leverage=_decimal(risk_section.get("max_leverage"), "risk.max_leverage"),
        limits_version=_text(risk_section, "limits_version", owner="risk"),
    )
    _require(risk.validate(), "risk")

    calendar = _calendar(calendar_section)
    _require(calendar.validate(), "calendar")

    symbols_raw = payload.get("symbols")
    if not isinstance(symbols_raw, Sequence) or isinstance(symbols_raw, (str, bytes)):
        raise ProviderSetupError("invalid_symbols")
    if not symbols_raw:
        raise ProviderSetupError("missing_symbols")
    symbols: list[SymbolTerms] = []
    for index, entry in enumerate(symbols_raw):
        if not isinstance(entry, Mapping):
            raise ProviderSetupError(f"invalid_symbols[{index}]")
        terms = SymbolTerms(
            pair=_text(entry, "pair", owner="symbol"),
            broker_symbol=str(entry.get("broker_symbol") or "").strip(),
            suffix=str(entry.get("suffix") or ""),
            pip_size=_decimal(entry.get("pip_size"), f"symbols[{index}].pip_size"),
            calendar_id=str(entry.get("calendar_id") or calendar.calendar_id).strip(),
            calendar_version=str(
                entry.get("calendar_version") or calendar.version
            ).strip(),
            correlation_group=_text(entry, "correlation_group", owner="symbols"),
            margin_multiplier=_decimal(
                entry.get("margin_multiplier", "1"), f"symbols[{index}].margin_multiplier"
            ),
            spec_version=_text(entry, "spec_version", owner="symbols"),
            source=str(entry.get("source") or "OPERATOR_PROVIDER_SETUP").strip(),
        )
        missing = terms.missing()
        if missing:
            raise ProviderSetupError(
                f"missing_symbol_terms[{index}]:" + ",".join(missing)
            )
        if terms.calendar_id != calendar.calendar_id:
            raise ProviderSetupError(
                "symbol_calendar_mismatch", f"{terms.pair}:{terms.calendar_id}"
            )
        if terms.calendar_version != calendar.version:
            raise ProviderSetupError(
                "symbol_calendar_version_mismatch", f"{terms.pair}:{terms.calendar_version}"
            )
        symbols.append(terms)
    pairs = {_canonical(item.pair) for item in symbols}
    if len(pairs) != len(symbols):
        raise ProviderSetupError("duplicate_symbol_pair")

    execution_raw = _section(payload, "execution")
    execution = ExecutionTerms(
        quantity=_decimal(execution_raw.get("quantity"), "execution.quantity"),
        max_open_positions=int(execution_raw.get("max_open_positions", 1) or 1),
        allow_short=bool(execution_raw.get("allow_short", True)),
    )
    if execution.quantity <= 0:
        raise ProviderSetupError("invalid_execution.quantity")

    rates: list[ConversionRateTerms] = []
    for index, entry in enumerate(payload.get("conversion_rates") or ()):
        if not isinstance(entry, Mapping):
            raise ProviderSetupError(f"invalid_conversion_rates[{index}]")
        stamp, error = as_utc(_parse_instant(entry.get("timestamp")), "timestamp")
        if error or stamp is None:
            raise ProviderSetupError(f"invalid_conversion_rates[{index}].timestamp")
        rates.append(
            ConversionRateTerms(
                from_currency=str(entry.get("from") or "").strip().upper(),
                to_currency=str(entry.get("to") or "").strip().upper(),
                rate=_decimal(entry.get("rate"), f"conversion_rates[{index}].rate"),
                timestamp=stamp,
            )
        )

    entry_limit = None
    entry_raw = account_section.get("entry_exposure_limit")
    if entry_raw is not None:
        if not isinstance(entry_raw, Mapping):
            raise ProviderSetupError("invalid_account.entry_exposure_limit")
        entry_limit = EntryExposureLimit(
            limit_account=_decimal(
                entry_raw.get("amount"), "account.entry_exposure_limit.amount"
            ),
            account_currency=str(entry_raw.get("currency") or account.account_currency).strip(),
            version=_text(entry_raw, "version", owner="entry_limit"),
            source=_text(entry_raw, "source", owner="entry_limit"),
        )
        _require(entry_limit.validate(), "entry_limit")

    swap: list[SwapTerms] = []
    for index, entry in enumerate(payload.get("swap") or ()):
        if not isinstance(entry, Mapping):
            raise ProviderSetupError(f"invalid_swap[{index}]")
        swap.append(
            SwapTerms(
                pair=_text(entry, "pair", owner="swap"),
                long_rate=_decimal(entry.get("long_rate"), f"swap[{index}].long_rate"),
                short_rate=_decimal(entry.get("short_rate"), f"swap[{index}].short_rate"),
                source=_text(entry, "source", owner="swap"),
                version=_text(entry, "version", owner="swap"),
            )
        )

    return ProviderSetup(
        path=str(location),
        account=account,
        costs=costs,
        rollover=rollover,
        risk=risk,
        calendar=calendar,
        symbols=tuple(symbols),
        execution=execution,
        swap=tuple(swap),
        entry_limit=entry_limit,
        conversion_rates=tuple(rates),
    )


def _parse_instant(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _calendar(section: Mapping[str, Any]) -> ExplicitTradingCalendar:
    valid_from, from_error = as_utc(_parse_instant(section.get("valid_from")), "valid_from")
    valid_until, until_error = as_utc(
        _parse_instant(section.get("valid_until")), "valid_until"
    )
    if from_error or valid_from is None:
        raise ProviderSetupError("invalid_calendar.valid_from")
    if until_error or valid_until is None:
        raise ProviderSetupError("invalid_calendar.valid_until")
    windows_raw = section.get("windows")
    if not isinstance(windows_raw, Mapping):
        raise ProviderSetupError("missing_calendar.windows")
    windows: dict[str, tuple[CalendarWindow, ...]] = {}
    for pair, entries in windows_raw.items():
        if not isinstance(entries, Sequence) or isinstance(entries, (str, bytes)):
            raise ProviderSetupError(f"invalid_calendar.windows[{pair}]")
        built: list[CalendarWindow] = []
        for entry in entries:
            if isinstance(entry, Mapping):
                start_raw, end_raw, label = (
                    entry.get("start"),
                    entry.get("end"),
                    str(entry.get("label") or ""),
                )
            elif isinstance(entry, Sequence) and len(entry) >= 2:
                start_raw, end_raw = entry[0], entry[1]
                label = str(entry[2]) if len(entry) > 2 else ""
            else:
                raise ProviderSetupError(f"invalid_calendar.window[{pair}]")
            start, start_error = as_utc(_parse_instant(start_raw), "start")
            end, end_error = as_utc(_parse_instant(end_raw), "end")
            if start_error or end_error or start is None or end is None:
                raise ProviderSetupError(f"invalid_calendar.window_bounds[{pair}]")
            built.append(CalendarWindow(start=start, end=end, label=label))
        windows[str(pair)] = tuple(built)
    holidays_raw = section.get("holidays") or {}
    holidays: dict[str, frozenset[date]] = {}
    if isinstance(holidays_raw, Mapping):
        for pair, values in holidays_raw.items():
            parsed: set[date] = set()
            for value in values or ():
                try:
                    parsed.add(date.fromisoformat(str(value)))
                except ValueError as exc:
                    raise ProviderSetupError(
                        f"invalid_calendar.holiday[{pair}]", str(value)
                    ) from exc
            holidays[str(pair)] = frozenset(parsed)
    return ExplicitTradingCalendar(
        calendar_id=_text(section, "calendar_id", owner="calendar"),
        version=_text(section, "version", owner="calendar"),
        source=_text(section, "source", owner="calendar"),
        timezone_name=_text(section, "timezone_name", owner="calendar"),
        valid_from=valid_from,
        valid_until=valid_until,
        windows_by_pair=windows,
        holidays_by_pair=holidays,
    )


def _require(result: Any, section: str) -> None:
    if not getattr(result, "valid", False):
        raise ProviderSetupError(f"invalid_{section}", getattr(result, "reason", ""))


__all__ = [
    "CREDENTIAL_KEYS",
    "ConversionRateTerms",
    "ExecutionTerms",
    "ProviderSetup",
    "ProviderSetupError",
    "REQUIRED_SECTIONS",
    "SCHEMA",
    "SymbolTerms",
    "SwapTerms",
    "load_provider_setup",
]

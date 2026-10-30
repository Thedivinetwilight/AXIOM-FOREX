"""The guided provider-setup wizard: build, check, and write one local document.

The runner cannot start without ``provider_setup.local.json``: it holds the terms
AXIOM will not invent — commissions, slippage, financing, swap, the trading
calendar, conversion rates, risk limits, and the execution quantity.  Typing that
document by hand is where a setup fails, so this module does three narrow things:

1. **Read what the terminal can prove.**  The account currency and leverage, and
   each symbol's exact broker name, suffix, and tick size come from the connected
   MetaTrader 5 terminal.  They are marked ``TERMINAL`` and are never typed.
2. **Ask for what only the operator knows.**  Every other term is marked
   ``OPERATOR``, is required, and has no default: a missing commission is a
   missing commission, not zero.
3. **Validate before writing.**  The payload is checked by the *same* loader the
   runner uses (:func:`axiom_forex.config.provider_setup.load_provider_setup`),
   so a saved file is one the runner would accept.  A refusal names the exact
   field.

What this module never does
---------------------------
* it never reads, writes, or touches ``mt5_settings.local.json``;
* it never stores a password, token, or any credential — the loader refuses a
  document that contains a credential field, and so does this;
* it never guesses a term, and it never invents a symbol: a symbol the terminal
  does not quote is refused by name;
* it never runs anything: writing the file is the whole effect.
"""

from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .provider_setup import (
    CREDENTIAL_KEYS,
    SCHEMA,
    ProviderSetupError,
    load_provider_setup,
)

#: The file the runner is started against, in the project root.
SETUP_FILENAME = "provider_setup.local.json"

#: The safe example shipped with the repository.  Deliberately not runnable.
EXAMPLE_PATH = Path("profiles") / "provider_setup.example.json"

#: How a field is known.
TERMINAL = "TERMINAL"
OPERATOR = "OPERATOR"


@dataclass(frozen=True)
class SetupField:
    """One input on the form, and where its value came from."""

    path: str
    label: str
    kind: str
    help: str
    placeholder: str = ""
    optional: bool = False

    def audit_view(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "label": self.label,
            "kind": self.kind,
            "help": self.help,
            "placeholder": self.placeholder,
            "optional": self.optional,
        }


#: The scalar fields, in the order the page shows them.  The list-shaped
#: sections (symbols, swap, calendar windows, conversion rates) have their own
#: one-line-per-entry text areas, documented in :data:`LIST_FORMATS`.
FIELDS: tuple[SetupField, ...] = (
    SetupField("account.account_id", "Account label", OPERATOR,
               "A name for this account in AXIOM. It is a label, not your login.",
               "vantage-live-1"),
    SetupField("account.currency", "Account currency", TERMINAL,
               "Read from the terminal's account_info(). Never typed."),
    SetupField("account.leverage", "Leverage", TERMINAL,
               "Read from the terminal's account_info(). Never typed."),
    SetupField("account.initial_capital", "Initial capital", OPERATOR,
               "The balance the paper ledger starts from, in the account currency.",
               "10000"),
    SetupField("account.conversion_source", "Conversion source label", OPERATOR,
               "Where your declared conversion rates come from, for the audit trail.",
               "OPERATOR_DECLARED_RATES"),
    SetupField("account.entry_exposure_limit.amount", "Entry cap (amount)", OPERATOR,
               "Hard cap on in-flight entry notional. Without it the book refuses new entries.",
               "1000"),
    SetupField("account.entry_exposure_limit.currency", "Entry cap currency", OPERATOR,
               "Usually the account currency. Typed, never assumed.", "USD"),
    SetupField("account.entry_exposure_limit.version", "Entry cap version", OPERATOR,
               "A label for this cap, so a change is visible in the audit trail.", "cap-v1"),
    SetupField("account.entry_exposure_limit.source", "Entry cap source", OPERATOR,
               "Who set this cap.", "OPERATOR_PROVIDER_SETUP"),
    SetupField("costs.model_id", "Cost model id", OPERATOR,
               "A name for your broker's cost model.", "vantage-standard"),
    SetupField("costs.version", "Cost model version", OPERATOR, "A version label.", "v1"),
    SetupField("costs.commission_rate", "Commission per side", OPERATOR,
               "Fraction of notional charged per side, or 0 if your account is spread-only. "
               "0 is a decision you make, not a default.", "0"),
    SetupField("costs.slippage_pips", "Slippage (pips)", OPERATOR,
               "Assumed slippage per fill, in pips.", "0.2"),
    SetupField("costs.financing_rate_daily", "Financing (daily)", OPERATOR,
               "Daily financing fraction, if your broker charges it outside swap.", "0"),
    SetupField("costs.source", "Cost source", OPERATOR,
               "Where these cost figures come from (your broker's schedule).",
               "BROKER_SCHEDULE"),
    SetupField("rollover.schedule_id", "Rollover schedule id", OPERATOR,
               "A name for the swap/rollover schedule.", "vantage-swap"),
    SetupField("rollover.version", "Rollover version", OPERATOR, "A version label.", "v1"),
    SetupField("rollover.daily_cutoff_utc", "Rollover cutoff (UTC, HH:MM)", OPERATOR,
               "The broker's daily rollover time, in UTC.", "21:00"),
    SetupField("rollover.triple_rollover_weekday", "Triple-swap weekday (0=Mon)", OPERATOR,
               "The weekday that carries triple swap. Leave blank if your broker has none.",
               "2", optional=True),
    SetupField("rollover.source", "Rollover source", OPERATOR,
               "Where the swap schedule comes from.", "BROKER_SCHEDULE"),
    SetupField("risk.max_order_notional_account", "Max order notional", OPERATOR,
               "Largest single order, in account currency.", "1000"),
    SetupField("risk.max_gross_exposure_account", "Max gross exposure", OPERATOR,
               "Largest total exposure, in account currency.", "2000"),
    SetupField("risk.max_margin_account", "Max margin in use", OPERATOR,
               "Largest margin the account may have committed.", "1000"),
    SetupField("risk.max_abs_currency_exposure_account", "Max single-currency exposure", OPERATOR,
               "Largest exposure to any one currency.", "1500"),
    SetupField("risk.max_correlation_group_exposure_account",
               "Max correlation-group exposure", OPERATOR,
               "Largest exposure to one correlated group.", "1500"),
    SetupField("risk.max_open_positions", "Max open positions", OPERATOR, "Whole number.", "1"),
    SetupField("risk.max_daily_loss_account", "Max daily loss", OPERATOR,
               "Loss after which new entries stop, in account currency.", "200"),
    SetupField("risk.max_trades_per_day", "Max trades per day", OPERATOR, "Whole number.", "3"),
    SetupField("risk.max_spread_pips", "Max spread (pips)", OPERATOR,
               "Widest spread at which an order may be sent.", "1.5"),
    SetupField("risk.max_leverage", "Max leverage", OPERATOR,
               "Leverage ceiling for risk checks; never above your account's.", "30"),
    SetupField("risk.limits_version", "Risk limits version", OPERATOR,
               "A label for this set of limits.", "limits-v1"),
    SetupField("calendar.calendar_id", "Calendar id", OPERATOR,
               "A name for the broker's trading calendar.", "vantage-forex"),
    SetupField("calendar.version", "Calendar version", OPERATOR, "A version label.", "v1"),
    SetupField("calendar.source", "Calendar source", OPERATOR,
               "Where the session windows come from (your broker's spec page).",
               "BROKER_CONTRACT_SPEC"),
    SetupField("calendar.timezone_name", "Calendar timezone", OPERATOR,
               "The zone the exchange day is defined in.", "UTC"),
    SetupField("calendar.valid_from", "Windows valid from (ISO, UTC)", OPERATOR,
               "First instant these windows apply. Not extrapolated either way.",
               "2026-01-01T00:00:00Z"),
    SetupField("calendar.valid_until", "Windows valid until (ISO, UTC)", OPERATOR,
               "Last instant these windows apply.", "2026-12-31T23:59:59Z"),
    SetupField("execution.quantity", "Order quantity (lots)", OPERATOR,
               "Volume per entry, in lots.", "0.10"),
    SetupField("execution.max_open_positions", "Runner max open positions", OPERATOR,
               "Whole number; 1 means one position at a time.", "1"),
    SetupField("execution.allow_short", "Allow short", OPERATOR,
               "yes or no. A short is a decision you make.", "no"),
)

#: The list-shaped sections, one entry per line.
LIST_FORMATS: dict[str, str] = {
    "symbols": "PAIR | broker symbol (blank = from terminal) | pip size (blank = from terminal) "
    "| correlation group | margin multiplier | spec version",
    "swap": "PAIR | long rate | short rate | source | version",
    "calendar.windows": "PAIR | start (ISO, UTC) | end (ISO, UTC) | label",
    "calendar.holidays": "PAIR | YYYY-MM-DD",
    "conversion_rates": "FROM | TO | rate | timestamp (ISO, UTC)",
}

#: Human explanations for the loader's reason codes.  The page shows these; the
#: raw code is kept beside them so a support conversation stays precise.
ERROR_HELP: dict[str, str] = {
    "provider_setup_missing": "The file was not found where the runner looks for it.",
    "provider_setup_unreadable": "The file could not be read. Check its permissions.",
    "provider_setup_malformed": "The file is not valid JSON.",
    "provider_setup_not_an_object": "The file must be a JSON object.",
    "provider_setup_contains_credential_field": (
        "The document contains a credential field. AXIOM never stores a password, "
        "token, or secret in a setup file. Remove it and sign in inside MetaTrader 5."
    ),
    "provider_setup_wrong_schema": f'The "schema" field must be exactly "{SCHEMA}".',
}

MISSING = "missing"
INVALID = "invalid"


class SetupIncomplete(Exception):
    """Something the operator must supply is absent or unusable."""

    def __init__(self, problems: Sequence[Any]) -> None:
        super().__init__("provider_setup_incomplete")
        self.problems = [
            item.audit_view() if hasattr(item, "audit_view") else dict(item)
            for item in problems
        ]

    def audit_view(self) -> dict[str, Any]:
        return {"reason": "provider_setup_incomplete", "problems": self.problems}


@dataclass(frozen=True)
class Problem:
    path: str
    kind: str
    message: str
    kind_of_field: str = OPERATOR

    def audit_view(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "kind": self.kind,
            "message": self.message,
            "field_kind": self.kind_of_field,
        }


@dataclass(frozen=True)
class TerminalFacts:
    """What the connected terminal can prove, read-only."""

    connected: bool
    currency: str = ""
    leverage: str = ""
    server: str = ""
    margin_mode: str = ""
    symbols: tuple[str, ...] = ()
    reason: str = ""

    def audit_view(self) -> dict[str, Any]:
        return {
            "connected": self.connected,
            "currency": self.currency,
            "leverage": self.leverage,
            "server": self.server,
            "margin_mode": self.margin_mode,
            "symbols": list(self.symbols),
            "reason": self.reason,
        }


def terminal_facts(session: Any) -> TerminalFacts:
    """Read the account facts the terminal proves.  Never guesses."""

    if session is None or not getattr(session, "connected", False):
        return TerminalFacts(connected=False, reason="terminal_not_connected")
    account = dict(session.account() or {})
    symbols = tuple(
        str(item.get("name"))
        for item in (session.symbols() or ())
        if isinstance(item, Mapping) and item.get("name")
    )
    return TerminalFacts(
        connected=True,
        currency=str(account.get("currency") or ""),
        leverage=str(account.get("leverage") or ""),
        server=str(account.get("server") or ""),
        margin_mode=str(account.get("margin_mode") or ""),
        symbols=symbols,
    )


def symbol_facts(session: Any, symbol: str) -> dict[str, Any]:
    """The broker's own terms for one symbol, or an empty dict if unreadable."""

    terminal = getattr(session, "market_data_source", lambda: None)()
    if terminal is None:
        return {}
    try:
        info = terminal.symbol_info(symbol)
    except Exception:
        return {}
    return {
        "broker_symbol": str(info.name),
        "pip_size": str(info.point),
        "digits": int(info.digits),
        "base": str(info.currency_base),
        "quote": str(info.currency_profit),
        "volume_min": str(info.volume_min),
        "volume_step": str(info.volume_step),
    }


# ---------------------------------------------------------------------------
# building the payload from the form
# ---------------------------------------------------------------------------


def _clean(value: Any) -> str:
    return str(value if value is not None else "").strip()


def _lines(text: Any) -> list[list[str]]:
    rows: list[list[str]] = []
    for raw in _clean(text).splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        rows.append([cell.strip() for cell in line.split("|")])
    return rows


def _number(text: str, path: str, problems: list[Problem]) -> str | None:
    if not text:
        return None
    try:
        value = Decimal(text)
    except (InvalidOperation, ValueError):
        problems.append(Problem(path, INVALID, "This must be a number."))
        return None
    if not value.is_finite():
        problems.append(Problem(path, INVALID, "This must be a finite number."))
        return None
    return str(text)


def _instant(text: str, path: str, problems: list[Problem]) -> str | None:
    if not text:
        # An empty value is already recorded as missing; do not report it twice.
        return None
    candidate = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        problems.append(
            Problem(path, INVALID, "Use an ISO timestamp, e.g. 2026-01-01T00:00:00Z.")
        )
        return None
    if parsed.tzinfo is None:
        problems.append(
            Problem(path, INVALID, "Include a timezone offset (Z for UTC).")
        )
        return None
    return text


def build_payload(
    form: Mapping[str, Any],
    *,
    session: Any = None,
    facts: TerminalFacts | None = None,
) -> tuple[dict[str, Any], list[Problem]]:
    """Turn submitted values into the schema's document, or say what is missing.

    Terminal-verifiable values are read here, not trusted from the form: if the
    terminal's currency differs from what was submitted, the terminal wins and the
    difference is reported.
    """

    problems: list[Problem] = []
    facts = facts or terminal_facts(session)

    def field_value(path: str, field_kind: str, *, optional: bool = False) -> str:
        raw = _clean(form.get(path))
        if field_kind == TERMINAL:
            if path == "account.currency":
                if facts.connected and facts.currency:
                    if raw and raw.upper() != facts.currency.upper():
                        problems.append(
                            Problem(path, INVALID,
                                    f"The terminal reports {facts.currency}; the terminal wins.")
                        )
                    return facts.currency
            if path == "account.leverage":
                if facts.connected and facts.leverage:
                    return facts.leverage
            if not facts.connected:
                problems.append(
                    Problem(path, MISSING, "Connect to your terminal so AXIOM can read this.",
                            field_kind)
                )
                return ""
        if not raw:
            if not optional:
                problems.append(Problem(path, MISSING, "This term is required.", field_kind))
            return ""
        return raw

    payload: dict[str, Any] = {"schema": SCHEMA}

    account: dict[str, Any] = {
        "account_id": field_value("account.account_id", OPERATOR),
        "currency": field_value("account.currency", TERMINAL),
        "leverage": field_value("account.leverage", TERMINAL),
        "initial_capital": _number(
            field_value("account.initial_capital", OPERATOR), "account.initial_capital", problems
        ),
        "conversion_source": field_value("account.conversion_source", OPERATOR),
    }
    entry_amount = field_value("account.entry_exposure_limit.amount", OPERATOR)
    if entry_amount:
        parsed = _number(entry_amount, "account.entry_exposure_limit.amount", problems)
        if parsed is not None:
            account["entry_exposure_limit"] = {
                "amount": parsed,
                "currency": field_value("account.entry_exposure_limit.currency", OPERATOR),
                "version": field_value("account.entry_exposure_limit.version", OPERATOR),
                "source": field_value("account.entry_exposure_limit.source", OPERATOR),
            }
    payload["account"] = account

    payload["costs"] = {
        "model_id": field_value("costs.model_id", OPERATOR),
        "version": field_value("costs.version", OPERATOR),
        "commission_rate": _number(
            field_value("costs.commission_rate", OPERATOR), "costs.commission_rate", problems
        ),
        "slippage_pips": _number(
            field_value("costs.slippage_pips", OPERATOR), "costs.slippage_pips", problems
        ),
        "financing_rate_daily": _number(
            field_value("costs.financing_rate_daily", OPERATOR),
            "costs.financing_rate_daily",
            problems,
        ),
        "source": field_value("costs.source", OPERATOR),
    }

    rollover: dict[str, Any] = {
        "schedule_id": field_value("rollover.schedule_id", OPERATOR),
        "version": field_value("rollover.version", OPERATOR),
        "daily_cutoff_utc": field_value("rollover.daily_cutoff_utc", OPERATOR),
        "source": field_value("rollover.source", OPERATOR),
    }
    weekday = _clean(form.get("rollover.triple_rollover_weekday"))
    if weekday:
        weekdays = {
            "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
            "friday": 4, "saturday": 5, "sunday": 6,
        }
        lowered = weekday.lower()
        if lowered in weekdays:
            rollover["triple_rollover_weekday"] = weekdays[lowered]
        elif weekday.isdigit() and 0 <= int(weekday) <= 6:
            rollover["triple_rollover_weekday"] = int(weekday)
        else:
            problems.append(
                Problem("rollover.triple_rollover_weekday", INVALID,
                        "Use 0-6 (0 = Monday), a weekday name, or leave it blank.")
            )
    payload["rollover"] = rollover

    risk: dict[str, Any] = {}
    for name in (
        "max_order_notional_account",
        "max_gross_exposure_account",
        "max_margin_account",
        "max_abs_currency_exposure_account",
        "max_correlation_group_exposure_account",
        "max_daily_loss_account",
        "max_spread_pips",
        "max_leverage",
    ):
        risk[name] = _number(field_value(f"risk.{name}", OPERATOR), f"risk.{name}", problems)
    for name in ("max_open_positions", "max_trades_per_day"):
        risk[name] = field_value(f"risk.{name}", OPERATOR)
    risk["limits_version"] = field_value("risk.limits_version", OPERATOR)
    payload["risk"] = risk

    calendar: dict[str, Any] = {
        "calendar_id": field_value("calendar.calendar_id", OPERATOR),
        "version": field_value("calendar.version", OPERATOR),
        "source": field_value("calendar.source", OPERATOR),
        "timezone_name": field_value("calendar.timezone_name", OPERATOR),
        "valid_from": _instant(
            field_value("calendar.valid_from", OPERATOR), "calendar.valid_from", problems
        ),
        "valid_until": _instant(
            field_value("calendar.valid_until", OPERATOR), "calendar.valid_until", problems
        ),
    }
    windows: dict[str, list[dict[str, str]]] = {}
    for index, row in enumerate(_lines(form.get("calendar.windows"))):
        if len(row) < 3:
            problems.append(
                Problem(f"calendar.windows[{index}]", INVALID,
                        "Expected: PAIR | start | end | label")
            )
            continue
        start = _instant(row[1], f"calendar.windows[{index}].start", problems)
        end = _instant(row[2], f"calendar.windows[{index}].end", problems)
        if start and end:
            windows.setdefault(row[0], []).append(
                {"start": start, "end": end, "label": row[3] if len(row) > 3 else ""}
            )
    if not windows:
        problems.append(
            Problem("calendar.windows", MISSING,
                    "At least one trading window is required: PAIR | start | end | label")
        )
    calendar["windows"] = windows
    holidays: dict[str, list[str]] = {}
    for index, row in enumerate(_lines(form.get("calendar.holidays"))):
        if len(row) < 2:
            problems.append(
                Problem(f"calendar.holidays[{index}]", INVALID, "Expected: PAIR | YYYY-MM-DD")
            )
            continue
        holidays.setdefault(row[0], []).append(row[1])
    if holidays:
        calendar["holidays"] = holidays
    payload["calendar"] = calendar

    symbols: list[dict[str, Any]] = []
    for index, row in enumerate(_lines(form.get("symbols"))):
        if len(row) < 1 or not row[0]:
            problems.append(
                Problem(f"symbols[{index}]", INVALID,
                        "Each line starts with the pair, e.g. EUR/USD.")
            )
            continue
        pair = row[0]
        broker_symbol = row[1] if len(row) > 1 else ""
        pip_size = row[2] if len(row) > 2 else ""
        facts_for_symbol = symbol_facts(session, broker_symbol or pair) if session else {}
        if not broker_symbol:
            if facts_for_symbol.get("broker_symbol"):
                broker_symbol = facts_for_symbol["broker_symbol"]
            else:
                problems.append(
                    Problem(f"symbols[{index}].broker_symbol", MISSING,
                            f"The terminal did not resolve {pair}. Name its exact broker "
                            "symbol, or connect the terminal that quotes it.")
                )
                continue
        if not pip_size:
            if facts_for_symbol.get("pip_size"):
                pip_size = facts_for_symbol["pip_size"]
            else:
                problems.append(
                    Problem(f"symbols[{index}].pip_size", MISSING,
                            "The terminal did not report this symbol's tick size.")
                )
                continue
        suffix = ""
        if "." in broker_symbol:
            suffix = "." + broker_symbol.split(".", 1)[1]
        symbols.append(
            {
                "pair": pair,
                "broker_symbol": broker_symbol,
                "suffix": suffix,
                "pip_size": pip_size,
                "calendar_id": _clean(form.get("calendar.calendar_id")),
                "calendar_version": _clean(form.get("calendar.version")),
                "correlation_group": row[3] if len(row) > 3 else "",
                "margin_multiplier": row[4] if len(row) > 4 and row[4] else "1",
                "spec_version": row[5] if len(row) > 5 else "",
                "source": "OPERATOR_PROVIDER_SETUP",
            }
        )
    if not symbols:
        problems.append(
            Problem("symbols", MISSING,
                    "At least one symbol is required: PAIR | broker symbol | pip size | "
                    "correlation group | margin multiplier | spec version")
        )
    payload["symbols"] = symbols

    for index, row in enumerate(_lines(form.get("symbols"))):
        if len(row) >= 4 and row[0] and not (row[3] if len(row) > 3 else ""):
            problems.append(
                Problem(f"symbols[{index}].correlation_group", MISSING,
                        "Group correlated pairs together, or use a unique label per pair.")
            )
        if len(row) >= 6 and row[0] and not (row[5] if len(row) > 5 else ""):
            problems.append(
                Problem(f"symbols[{index}].spec_version", MISSING,
                        "Name the version of the broker's contract specification you used.")
            )

    payload["execution"] = {
        "quantity": _number(
            field_value("execution.quantity", OPERATOR), "execution.quantity", problems
        ),
        "max_open_positions": field_value("execution.max_open_positions", OPERATOR),
        "allow_short": _clean(form.get("execution.allow_short")).lower()
        in {"yes", "true", "1", "y"},
    }

    swap: list[dict[str, str]] = []
    for index, row in enumerate(_lines(form.get("swap"))):
        if len(row) < 5:
            problems.append(
                Problem(f"swap[{index}]", INVALID,
                        "Expected: PAIR | long rate | short rate | source | version")
            )
            continue
        long_rate = _number(row[1], f"swap[{index}].long_rate", problems)
        short_rate = _number(row[2], f"swap[{index}].short_rate", problems)
        swap.append(
            {
                "pair": row[0],
                "long_rate": long_rate,
                "short_rate": short_rate,
                "source": row[3],
                "version": row[4],
            }
        )
    payload["swap"] = swap

    rates: list[dict[str, str]] = []
    for index, row in enumerate(_lines(form.get("conversion_rates"))):
        if len(row) < 4:
            problems.append(
                Problem(f"conversion_rates[{index}]", INVALID,
                        "Expected: FROM | TO | rate | timestamp")
            )
            continue
        rate = _number(row[2], f"conversion_rates[{index}].rate", problems)
        stamp = _instant(row[3], f"conversion_rates[{index}].timestamp", problems)
        rates.append({"from": row[0], "to": row[1], "rate": rate, "timestamp": stamp})
    payload["conversion_rates"] = rates

    for name in form:
        if str(name).strip().lower() in CREDENTIAL_KEYS:
            problems.append(
                Problem("credentials", INVALID,
                        "A field name looks like a credential. AXIOM never stores one.")
            )
            break

    return payload, problems


# ---------------------------------------------------------------------------
# validating with the runner's own loader
# ---------------------------------------------------------------------------


def check_payload(payload: Mapping[str, Any]) -> list[Problem]:
    """Validate a document exactly as the runner will.  Writes only to a temp file."""

    handle = tempfile.NamedTemporaryFile(
        mode="w", prefix="axiom-provider-setup-", suffix=".json", delete=False, encoding="utf-8"
    )
    try:
        handle.write(json.dumps(payload, indent=2))
        handle.close()
        path = Path(handle.name)
        load_provider_setup(path)
    except ProviderSetupError as exc:
        return [
            Problem(
                exc.reason.replace("missing_section:", "section:").replace(":", "."),
                MISSING if "missing" in exc.reason else INVALID,
                ERROR_HELP.get(exc.reason, "")
                or f"The runner refused this document: {exc.reason}"
                + (f" ({exc.detail})" if exc.detail else ""),
            )
        ]
    except OSError as exc:
        return [Problem("file", INVALID, f"The temporary file could not be written ({type(exc).__name__}).")]
    finally:
        try:
            path.unlink()
        except OSError:
            pass
    return []


def validate(
    form: Mapping[str, Any],
    *,
    session: Any = None,
    facts: TerminalFacts | None = None,
) -> tuple[dict[str, Any], list[Problem]]:
    """Build the document and check it.  Returns the payload and every problem."""

    payload, problems = build_payload(form, session=session, facts=facts)
    if problems:
        return payload, problems
    return payload, check_payload(payload)


# ---------------------------------------------------------------------------
# writing it, locally and atomically
# ---------------------------------------------------------------------------


def setup_path(root: Path | str) -> Path:
    return Path(root) / SETUP_FILENAME


def write_payload(
    payload: Mapping[str, Any],
    path: Path | str,
    *,
    overwrite: bool = False,
) -> Path:
    """Write the document atomically.  Refuses an existing file unless told to."""

    target = Path(path)
    lowered = json.dumps(payload).lower()
    for key in CREDENTIAL_KEYS:
        if f'"{key}"' in lowered:
            raise SetupIncomplete(
                [
                    Problem(
                        "credentials",
                        INVALID,
                        "The document contains a credential field, so it was not written.",
                    )
                ]
            )
    if target.exists() and not overwrite:
        raise FileExistsError(str(target))
    target.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    # Write beside the target and rename over it, so a reader never sees a
    # half-written document.  The temporary file is removed on any failure.
    handle = tempfile.NamedTemporaryFile(
        mode="w", prefix=".provider-setup-", dir=str(target.parent),
        delete=False, encoding="utf-8",
    )
    try:
        handle.write(text)
        handle.close()
        temp = Path(handle.name)
        temp.replace(target)
    finally:
        leftover = Path(handle.name)
        if leftover.exists():
            leftover.unlink()
    return target


def describe_problems(problems: Iterable[Problem]) -> str:
    """One sentence the page can show, naming the first thing to fix."""

    items = list(problems)
    if not items:
        return ""
    first = items[0]
    more = f" (+{len(items) - 1} more)" if len(items) > 1 else ""
    return f"{first.path}: {first.message}{more}"


__all__ = [
    "EXAMPLE_PATH",
    "ERROR_HELP",
    "FIELDS",
    "INVALID",
    "LIST_FORMATS",
    "MISSING",
    "OPERATOR",
    "Problem",
    "SETUP_FILENAME",
    "SetupField",
    "SetupIncomplete",
    "TERMINAL",
    "TerminalFacts",
    "build_payload",
    "check_payload",
    "describe_problems",
    "setup_path",
    "symbol_facts",
    "terminal_facts",
    "validate",
    "write_payload",
]

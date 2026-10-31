"""The short setup: read the terminal, ask five things, record both.

The old flow asked for the whole broker-terms document before anything could be
used, including values the terminal already knows.  This module builds the *short*
route:

1. read the terminal (account currency, leverage, account mode, exact symbols,
   their specifications, and whatever swap the broker publishes);
2. ask only what the terminal cannot answer and AXIOM genuinely needs: **which
   symbols**, **what size**, and **hard risk limits** -- plus the research window;
3. record everything in ``quick_setup.local.json`` with per-field provenance, so
   every screen can say *where* a number came from.

Two readiness states come out of it, and they are deliberately different:

``research``   needs observed symbols/specs and the operator's own limits.  A
               missing commission or calendar does **not** block it: those are
               labelled ``ESTIMATE``/``UNKNOWN`` and the results carry the label.
``live``       still needs the broker-terms document (commission, session
               windows, rollover cutoff, conversion) through the existing strict
               loader.  This module never invents one, and never claims a value it
               could not read.

Nothing here writes a credential, and there is no field that could carry one.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .terminal_facts import OBSERVED, SIMULATED, UNKNOWN, SymbolFacts, TerminalFacts

SCHEMA = "axiom-quick-setup-v1"
FILENAME = "quick_setup.local.json"

ESTIMATE = "ESTIMATE"
OPERATOR = "OPERATOR"
ASSUMED = "ASSUMED_ZERO"

#: The research window this build offers: the most recent year of broker history.
DEFAULT_WINDOW_DAYS = 365


class QuickSetupError(Exception):
    """The short setup cannot be used.  Carries a stable reason code."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(reason if not detail else f"{reason}: {detail}")
        self.reason = str(reason)
        self.detail = str(detail)

    def audit_view(self) -> dict[str, str]:
        return {"reason": self.reason, "detail": self.detail}


def _utc() -> datetime:
    return datetime.now(timezone.utc)


def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _positive(value: Any, name: str) -> Decimal:
    parsed = _decimal(value)
    if parsed is None or parsed <= 0:
        raise QuickSetupError(f"invalid_{name}", str(value))
    return parsed


@dataclass(frozen=True)
class QuickChoices:
    """What the operator decided.  Five answers, and they are all required."""

    symbols: tuple[str, ...] = ()
    lot_size: Decimal | None = None
    max_open_positions: int = 1
    max_daily_loss_account: Decimal | None = None
    max_order_notional_account: Decimal | None = None
    max_spread_pips: Decimal | None = None
    window_days: int = DEFAULT_WINDOW_DAYS

    @classmethod
    def from_form(cls, form: Mapping[str, Any] | None) -> "QuickChoices":
        payload = form or {}

        def one(name: str) -> str:
            value = payload.get(name)
            if isinstance(value, (str, bytes)):
                return value.strip()
            if isinstance(value, (list, tuple)):
                return str(value[0]).strip() if value else ""
            return "" if value is None else str(value).strip()

        raw_symbols = payload.get("symbols")
        if isinstance(raw_symbols, (str, bytes)):
            symbols = [raw_symbols]
        elif raw_symbols is None:
            symbols = []
        else:
            symbols = [str(item) for item in raw_symbols]
        cleaned = tuple(dict.fromkeys(item.strip() for item in symbols if item.strip()))

        positions = one("max_open_positions")
        try:
            max_positions = max(1, int(positions)) if positions else 1
        except ValueError:
            max_positions = 1
        window = one("window_days")
        try:
            days = int(window) if window else DEFAULT_WINDOW_DAYS
        except ValueError:
            days = DEFAULT_WINDOW_DAYS
        return cls(
            symbols=cleaned,
            lot_size=_decimal(one("lot_size")),
            max_open_positions=max_positions,
            max_daily_loss_account=_decimal(one("max_daily_loss_account")),
            max_order_notional_account=_decimal(one("max_order_notional_account")),
            max_spread_pips=_decimal(one("max_spread_pips")),
            window_days=max(30, min(days, 1095)),
        )

    def problems(self) -> tuple[str, ...]:
        problems: list[str] = []
        if not self.symbols:
            problems.append("choose at least one symbol to trade")
        if self.lot_size is None or self.lot_size <= 0:
            problems.append("a positive lot size is required")
        if self.max_daily_loss_account is None or self.max_daily_loss_account <= 0:
            problems.append("a positive daily loss limit is required")
        if self.max_order_notional_account is None or self.max_order_notional_account <= 0:
            problems.append("a positive maximum order notional is required")
        if self.max_spread_pips is not None and self.max_spread_pips <= 0:
            problems.append("the spread cap must be positive")
        return tuple(problems)

    def audit_view(self) -> dict[str, Any]:
        return {
            "symbols": list(self.symbols),
            "lot_size": str(self.lot_size) if self.lot_size is not None else "",
            "max_open_positions": self.max_open_positions,
            "max_daily_loss_account": (
                str(self.max_daily_loss_account)
                if self.max_daily_loss_account is not None
                else ""
            ),
            "max_order_notional_account": (
                str(self.max_order_notional_account)
                if self.max_order_notional_account is not None
                else ""
            ),
            "max_spread_pips": (
                str(self.max_spread_pips) if self.max_spread_pips is not None else ""
            ),
            "window_days": self.window_days,
        }


@dataclass(frozen=True)
class CostAssumptions:
    """The costs the terminal cannot report.  Every one is labelled."""

    commission_per_lot: Decimal = Decimal("0")
    slippage_pips: Decimal = Decimal("0.2")
    #: A research-only sensitivity multiplier applied to the whole cost stack.
    sensitivity_multipliers: tuple[Decimal, ...] = (Decimal("1"), Decimal("2"))

    @property
    def commission_state(self) -> str:
        return ASSUMED if self.commission_per_lot == 0 else ESTIMATE

    def audit_view(self) -> dict[str, Any]:
        return {
            "commission_per_lot": {
                "value": str(self.commission_per_lot),
                "state": self.commission_state,
                "effect": (
                    "Treated as zero inside research. If your broker charges a "
                    "commission, every research result here is optimistic by that "
                    "amount per lot per side."
                ),
            },
            "slippage_pips": {
                "value": str(self.slippage_pips),
                "state": ESTIMATE,
                "effect": (
                    "A fixed per-fill price concession. It is a stated assumption, not "
                    "a measurement."
                ),
            },
            "sensitivity_multipliers": [str(value) for value in self.sensitivity_multipliers],
        }


@dataclass(frozen=True)
class QuickSetup:
    """One recorded short setup: observed facts, operator choices, provenance."""

    facts: TerminalFacts
    choices: QuickChoices
    costs: CostAssumptions = field(default_factory=CostAssumptions)
    created_at: str = ""
    path: str = ""

    # -- selected symbols -------------------------------------------------

    def selected(self) -> tuple[SymbolFacts, ...]:
        wanted = set(self.choices.symbols)
        return tuple(item for item in self.facts.symbols if item.broker_symbol in wanted)

    def symbol(self, broker_symbol: str) -> SymbolFacts | None:
        return self.facts.symbol(broker_symbol)

    # -- readiness --------------------------------------------------------

    def research_blockers(self) -> tuple[str, ...]:
        blockers = list(self.choices.problems())
        if not self.facts.connected and not self.facts.simulated:
            blockers.append("connect to your terminal so symbols and specs can be read")
        if not self.selected():
            blockers.append("none of the chosen symbols was reported by the terminal")
        for item in self.selected():
            if not item.pair:
                blockers.append(
                    f"{item.broker_symbol}: the terminal did not report both legs, so "
                    "the pair cannot be named"
                )
            if item.volume_min is None or item.volume_step is None:
                blockers.append(f"{item.broker_symbol}: the volume grid is unknown")
            if self.choices.lot_size is not None and item.volume_min is not None:
                if self.choices.lot_size < item.volume_min:
                    blockers.append(
                        f"{item.broker_symbol}: {self.choices.lot_size} is below the "
                        f"broker minimum {item.volume_min}"
                    )
        return tuple(dict.fromkeys(blockers))

    def live_blockers(self) -> tuple[str, ...]:
        blockers = list(self.research_blockers())
        blockers.append(
            "commission, session windows, rollover cutoff and conversion are not read "
            "from the terminal; supply them in the broker-terms page before live"
        )
        for item in self.selected():
            if item.swap_state not in {"POINTS", "CURRENCY", "DISABLED"}:
                blockers.append(
                    f"{item.broker_symbol}: swap is {item.swap_state.lower()}, so the "
                    "cost of holding overnight is unknown"
                )
        return tuple(dict.fromkeys(blockers))

    def readiness(self) -> dict[str, Any]:
        research = self.research_blockers()
        live = self.live_blockers()
        return {
            "research": {
                "ready": not research,
                "blockers": list(research),
                "note": (
                    "One-year research needs observed symbols, specs, your limits and "
                    "costs that are labelled. It does not claim live readiness."
                ),
            },
            "live": {
                "ready": not live,
                "blockers": list(live),
                "note": (
                    "Live execution still passes the existing broker-terms document, "
                    "the pre-trade gate, the risk limits and the protective-stop "
                    "requirement. Nothing here shortens that."
                ),
            },
        }

    # -- serialisation ----------------------------------------------------

    def fingerprint(self) -> str:
        payload = {
            "facts": self.facts.audit_view(),
            "choices": self.choices.audit_view(),
            "costs": self.costs.audit_view(),
        }
        encoded = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()[:16]

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "created_at": self.created_at or _utc().isoformat(),
            "fingerprint": self.fingerprint(),
            "source": {
                "kind": self.facts.source_kind,
                "state": self.facts.state,
                "connected": self.facts.connected,
                "simulated": self.facts.simulated,
                "company": self.facts.company,
                "server": self.facts.server,
                "account_fingerprint": self.facts.account_fingerprint,
                "read_at": self.facts.read_at,
            },
            "account": {
                "currency": self.facts.currency.audit_view(),
                "leverage": self.facts.leverage.audit_view(),
                "mode": self.facts.account_mode.audit_view(),
            },
            "symbols": [item.audit_view() for item in self.facts.symbols],
            "choices": self.choices.audit_view(),
            "costs": self.costs.audit_view(),
            "readiness": self.readiness(),
            "notes": list(self.facts.notes),
        }

    def save(self, path: str | Path) -> dict[str, Any]:
        """Write the record atomically.  It contains no credential."""

        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = self.to_payload()
        handle = tempfile.NamedTemporaryFile(
            mode="w",
            prefix=".quick-setup-",
            dir=str(target.parent),
            delete=False,
            encoding="utf-8",
        )
        try:
            handle.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
            handle.close()
            Path(handle.name).replace(target)
        finally:
            leftover = Path(handle.name)
            if leftover.exists():
                leftover.unlink()
        return {"path": str(target), "fingerprint": payload["fingerprint"]}

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "QuickSetup":
        """Rebuild from the saved record, preserving every state label."""

        if str(payload.get("schema", "")) != SCHEMA:
            raise QuickSetupError("wrong_schema", str(payload.get("schema", "")))
        server = dict(payload.get("source") or {})
        account = dict(payload.get("account") or {})
        facts = TerminalFacts(
            source_kind=str(server.get("kind", "")),
            connected=bool(server.get("connected", True)),
            simulated=bool(server.get("simulated")),
            company=str(server.get("company", "")),
            server=str(server.get("server", "")),
            account_fingerprint=str(server.get("account_fingerprint", "")),
            read_at=str(server.get("read_at", "")),
            symbols=tuple(
                _symbol_from_payload(item)
                for item in payload.get("symbols") or []
                if isinstance(item, Mapping)
            ),
            currency=_fact_from_payload(account.get("currency"), "account_currency"),
            leverage=_fact_from_payload(account.get("leverage"), "account_leverage"),
            account_mode=_fact_from_payload(account.get("mode"), "account_mode"),
        )
        choices_payload = dict(payload.get("choices") or {})
        choices = QuickChoices(
            symbols=tuple(str(item) for item in choices_payload.get("symbols") or []),
            lot_size=_decimal(choices_payload.get("lot_size")),
            max_open_positions=int(choices_payload.get("max_open_positions") or 1),
            max_daily_loss_account=_decimal(choices_payload.get("max_daily_loss_account")),
            max_order_notional_account=_decimal(
                choices_payload.get("max_order_notional_account")
            ),
            max_spread_pips=_decimal(choices_payload.get("max_spread_pips")),
            window_days=int(choices_payload.get("window_days") or DEFAULT_WINDOW_DAYS),
        )
        costs_payload = dict(payload.get("costs") or {})
        commission = dict(costs_payload.get("commission_per_lot") or {})
        slippage = dict(costs_payload.get("slippage_pips") or {})
        costs = CostAssumptions(
            commission_per_lot=_decimal(commission.get("value")) or Decimal("0"),
            slippage_pips=_decimal(slippage.get("value")) or Decimal("0"),
            sensitivity_multipliers=tuple(
                _decimal(value) or Decimal("1")
                for value in costs_payload.get("sensitivity_multipliers") or ["1", "2"]
            ),
        )
        return cls(
            facts=facts,
            choices=choices,
            costs=costs,
            created_at=str(payload.get("created_at", "")),
        )


def _fact_from_payload(payload: Any, name: str):
    from .terminal_facts import Fact

    if not isinstance(payload, Mapping):
        return Fact(name)
    return Fact(
        name=str(payload.get("name") or name),
        value=str(payload.get("value") or ""),
        state=str(payload.get("state") or UNKNOWN),
        source=str(payload.get("source") or ""),
        detail=str(payload.get("detail") or ""),
    )


def _symbol_from_payload(payload: Mapping[str, Any]) -> SymbolFacts:
    return SymbolFacts(
        broker_symbol=str(payload.get("broker_symbol", "")),
        description=str(payload.get("description", "")),
        pair=str(payload.get("pair", "")),
        base_currency=str(payload.get("base_currency", "")),
        profit_currency=str(payload.get("profit_currency", "")),
        margin_currency=str(payload.get("margin_currency", "")),
        digits=int(payload.get("digits") or 0),
        point=_decimal(payload.get("point")),
        contract_size=_decimal(payload.get("contract_size")),
        volume_min=_decimal(payload.get("volume_min")),
        volume_max=_decimal(payload.get("volume_max")),
        volume_step=_decimal(payload.get("volume_step")),
        stops_level=int(payload.get("stops_level") or 0),
        spread_points=int(payload.get("spread_points") or 0),
        tradable=bool(payload.get("tradable")),
        swap_long=_decimal(payload.get("swap_long")),
        swap_short=_decimal(payload.get("swap_short")),
        swap_mode=(
            None if payload.get("swap_mode") in (None, "") else int(payload["swap_mode"])
        ),
        state=str(payload.get("state") or OBSERVED),
        unknown=tuple(str(item) for item in payload.get("unknown") or []),
        source=str(payload.get("source") or "quick_setup"),
    )


def load_quick_setup(path: str | Path) -> QuickSetup:
    """Read the recorded short setup, refusing anything malformed."""

    location = Path(path)
    try:
        payload = json.loads(location.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise QuickSetupError("quick_setup_missing", str(location)) from exc
    except OSError as exc:
        raise QuickSetupError("quick_setup_unreadable", type(exc).__name__) from exc
    except json.JSONDecodeError as exc:
        raise QuickSetupError("quick_setup_malformed", exc.msg) from exc
    if not isinstance(payload, Mapping):
        raise QuickSetupError("quick_setup_not_an_object")
    setup = QuickSetup.from_payload(payload)
    return QuickSetup(
        facts=setup.facts,
        choices=setup.choices,
        costs=setup.costs,
        created_at=setup.created_at,
        path=str(location),
    )


def compose(
    facts: TerminalFacts,
    choices: QuickChoices,
    costs: CostAssumptions | None = None,
) -> QuickSetup:
    """Build the record without saving it."""

    return QuickSetup(
        facts=facts,
        choices=choices,
        costs=costs or CostAssumptions(),
        created_at=_utc().isoformat(),
    )


def summarise(setup: QuickSetup) -> list[tuple[str, str, str, str]]:
    """Rows for the page: label, value, provenance, what it means."""

    rows: list[tuple[str, str, str, str]] = []
    for label, fact in (
        ("Account currency", setup.facts.currency),
        ("Leverage", setup.facts.leverage),
        ("Account mode", setup.facts.account_mode),
    ):
        rows.append(
            (
                label,
                fact.value or "unknown",
                fact.state,
                fact.detail or "read from the connected terminal",
            )
        )
    return rows


__all__ = [
    "ASSUMED",
    "CostAssumptions",
    "DEFAULT_WINDOW_DAYS",
    "ESTIMATE",
    "FILENAME",
    "OPERATOR",
    "QuickChoices",
    "QuickSetup",
    "QuickSetupError",
    "SCHEMA",
    "compose",
    "load_quick_setup",
    "summarise",
]

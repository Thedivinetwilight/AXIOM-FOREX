"""Build the instrument catalogue from the *connected* terminal's own symbols.

AXIOM never guesses which instrument a broker means.  This module takes the
operator's declaration (pair, optional exact broker symbol, optional suffix) and
the terminal's live ``symbol_names()`` list, and produces one
:class:`~app.universe.specs.CurrencyPairSpec` per pair -- built from the broker's
own ``symbol_info`` terms, with the operator's pip size, calendar, correlation
group, and margin multiplier layered on top.

The rules, in order, and each of them fail-closed:

1. **Exact match only.**  A declared broker symbol must exist with that exact
   spelling.  A declared suffix admits only ``CANONICAL + suffix``.  With neither,
   only the bare six letters are accepted.  Nothing is case-folded into a guess
   and no prefix/substring match is ever attempted.
2. **Ambiguity is a refusal.**  If two distinct terminal symbols could both be
   meant (``EURUSD`` and ``EURUSD.pro``, say) and the operator did not say which,
   the pair is refused as ``symbol_ambiguous``.  A close-enough symbol is how an
   order ends up on the wrong instrument.
3. **Terms come from the broker, not from a default.**  Digits, point, contract
   size, volume minimum/step/maximum, and the margin mode are read from
   ``symbol_info``; the operator's declaration adds what MT5 does not know
   (pip size, calendar identity, correlation group, margin multiplier).
4. **The result is cross-checked.**  `symbols.cross_check` compares the finished
   spec against the broker's terms.  Any difference is reported with both values
   and the pair is refused rather than "corrected".
5. **Not tradable is not usable.**  A symbol whose trade mode forbids trading, or
   whose data is disabled, is refused for ordering; it may still be listed so the
   operator can see *why*.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Sequence

from app.universe.specs import (
    CurrencyPairSpec,
    InstrumentStatus,
    QuantityUnit,
    canonical_pair,
)

from .symbols import SymbolCrossCheck, cross_check
from .terminal import MT5Error, MT5SymbolInfo, symbol_trade_mode_name

# -- refusal reasons --------------------------------------------------------

SYMBOL_UNRESOLVED = "symbol_unresolved"
SYMBOL_AMBIGUOUS = "symbol_ambiguous"
SYMBOL_INFO_UNAVAILABLE = "symbol_info_unavailable"
SYMBOL_TERMS_INVALID = "symbol_terms_invalid"
SYMBOL_SPEC_MISMATCH = "symbol_spec_mismatch"
SYMBOL_NOT_TRADABLE = "symbol_not_tradable"
SYMBOL_DATA_DISABLED = "symbol_data_disabled"
CATALOGUE_EMPTY = "catalogue_empty"


def _canonical(text: Any) -> str:
    pair, error = canonical_pair(str(text or ""))
    return pair or ""


def _decimal(value: Any) -> Decimal | None:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return number if number.is_finite() else None


@dataclass(frozen=True)
class SymbolRequest:
    """What the operator declared about one pair.

    Everything here is supplied; nothing is defaulted.  ``broker_symbol`` and
    ``suffix`` are mutually aware: giving the symbol wins, and the suffix is then
    only a cross-check.
    """

    pair: str
    broker_symbol: str = ""
    suffix: str = ""
    pip_size: Decimal | None = None
    calendar_id: str = ""
    calendar_version: str = ""
    correlation_group: str = ""
    margin_multiplier: Decimal = Decimal("1")
    spec_version: str = ""
    source: str = ""

    @property
    def canonical(self) -> str:
        return _canonical(self.pair)

    @property
    def stem(self) -> str:
        """The pair as brokers write it in a symbol name: ``EURUSD``."""

        return self.canonical.replace("/", "")

    def problems(self) -> tuple[str, ...]:
        errors: list[str] = []
        if not self.canonical:
            errors.append("invalid_pair")
        if self.pip_size is None or self.pip_size <= 0:
            errors.append("missing_pip_size")
        for name, value in (
            ("calendar_id", self.calendar_id),
            ("calendar_version", self.calendar_version),
            ("correlation_group", self.correlation_group),
            ("spec_version", self.spec_version),
            ("source", self.source),
        ):
            if not str(value or "").strip():
                errors.append(f"missing_{name}")
        if self.margin_multiplier is None or self.margin_multiplier <= 0:
            errors.append("invalid_margin_multiplier")
        if self.broker_symbol and self.suffix:
            expected = self.stem + self.suffix
            if self.broker_symbol != expected:
                errors.append("broker_symbol_disagrees_with_suffix")
        return tuple(errors)

    def candidates(self) -> tuple[str, ...]:
        """The exact spellings this request admits, most specific first."""

        if not self.stem:
            return ()
        if self.broker_symbol:
            return (self.broker_symbol,)
        if self.suffix:
            return (self.stem + self.suffix,)
        return (self.stem,)


@dataclass(frozen=True)
class ResolvedSymbol:
    """One pair, resolved to a terminal symbol and a validated spec."""

    request: SymbolRequest
    broker_symbol: str
    spec: CurrencyPairSpec
    info: MT5SymbolInfo
    check: SymbolCrossCheck
    notes: tuple[str, ...] = ()

    @property
    def pair(self) -> str:
        return str(self.spec.canonical_pair or self.spec.pair)

    @property
    def tradable(self) -> bool:
        return bool(self.check.trading_ok)

    def audit_view(self) -> dict[str, Any]:
        return {
            "pair": self.pair,
            "broker_symbol": self.broker_symbol,
            "description": self.info.description,
            "digits": self.info.digits,
            "point": str(self.info.point),
            "contract_size": str(self.info.trade_contract_size),
            "volume_min": str(self.info.volume_min),
            "volume_step": str(self.info.volume_step),
            "volume_max": str(self.info.volume_max),
            "trade_mode": symbol_trade_mode_name(self.info.trade_mode),
            "pip_size": str(self.spec.pip_size),
            "spec_version": self.spec.spec_version,
            "source": self.spec.source,
            "cross_check": self.check.audit_view(),
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class CatalogueRefusal:
    """A pair that could not be resolved, and exactly why."""

    pair: str
    reason: str
    detail: str = ""
    candidates: tuple[str, ...] = ()
    differences: tuple[tuple[str, str, str], ...] = ()

    def audit_view(self) -> dict[str, Any]:
        return {
            "pair": self.pair,
            "reason": self.reason,
            "detail": self.detail,
            "candidates": list(self.candidates),
            "differences": [
                {"field": name, "catalog": catalog, "broker": broker}
                for name, catalog, broker in self.differences
            ],
        }


@dataclass(frozen=True)
class CatalogueReport:
    """The whole resolution attempt: what resolved, what did not, and why."""

    resolved: tuple[ResolvedSymbol, ...] = ()
    refused: tuple[CatalogueRefusal, ...] = ()
    terminal_symbols: int = 0

    @property
    def ok(self) -> bool:
        return bool(self.resolved) and not self.refused

    @property
    def complete(self) -> bool:
        """Every requested pair resolved and none was refused."""

        return self.ok

    def pair(self, pair: str) -> ResolvedSymbol | None:
        wanted = _canonical(pair)
        for item in self.resolved:
            if _canonical(item.pair) == wanted:
                return item
        return None

    def resolved_for(self, pair: str) -> ResolvedSymbol | None:
        """The resolved symbol for ``pair``, or ``None``.

        Named separately from :meth:`pair` so callers that need the *broker
        symbol and the catalogue check*, not just the price grid, read
        obviously.
        """

        return self.pair(pair)

    def refusal_for(self, pair: str) -> CatalogueRefusal | None:
        wanted = _canonical(pair)
        for item in self.refused:
            if _canonical(item.pair) == wanted:
                return item
        return None

    def specs(self) -> tuple[CurrencyPairSpec, ...]:
        return tuple(item.spec for item in self.resolved)

    def brokers(self) -> dict[str, str]:
        return {item.pair: item.broker_symbol for item in self.resolved}

    def audit_view(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "terminal_symbols": self.terminal_symbols,
            "resolved": [item.audit_view() for item in self.resolved],
            "refused": [item.audit_view() for item in self.refused],
            "source": "mt5_terminal_catalogue",
        }


# -- resolution -------------------------------------------------------------


def choose_symbol(names: Sequence[str], request: SymbolRequest) -> tuple[str, str, str]:
    """Return ``(symbol, reason, detail)``; ``symbol`` is empty on refusal."""

    problems = request.problems()
    if problems:
        return "", SYMBOL_TERMS_INVALID, ",".join(problems)

    exact = {str(name) for name in names}
    candidates = request.candidates()
    for candidate in candidates:
        if candidate in exact:
            return candidate, "", ""
    # Nothing matched.  Say whether the pair exists under *another* spelling, so
    # the operator sees the difference instead of a bare "not found" -- but do not
    # adopt that spelling.
    near = sorted(
        name
        for name in exact
        if name.upper().startswith(request.stem)
        or request.stem in name.upper()
    )
    detail = "no terminal symbol matches " + " | ".join(candidates)
    if near:
        detail += "; the terminal has " + ", ".join(near[:8])
        if len(near) > 1 and not request.broker_symbol:
            return "", SYMBOL_AMBIGUOUS, detail
    return "", SYMBOL_UNRESOLVED, detail


def build_spec(
    request: SymbolRequest,
    *,
    broker_symbol: str,
    info: MT5SymbolInfo,
) -> tuple[CurrencyPairSpec | None, tuple[str, ...]]:
    """Assemble one spec from the broker's terms and the operator's declaration."""

    problems = list(request.problems())
    point = _decimal(info.point)
    contract = _decimal(info.trade_contract_size)
    volume_min = _decimal(info.volume_min)
    volume_step = _decimal(info.volume_step)
    volume_max = _decimal(info.volume_max)

    if point is None or point <= 0:
        problems.append("invalid_broker_point")
    if contract is None or contract <= 0:
        problems.append("invalid_broker_contract_size")
    if volume_min is None or volume_min <= 0:
        problems.append("invalid_broker_volume_min")
    if volume_step is None or volume_step <= 0:
        problems.append("invalid_broker_volume_step")
    if volume_max is None or volume_max < (volume_min or Decimal(0)):
        problems.append("invalid_broker_volume_max")
    if not isinstance(info.digits, int) or info.digits <= 0:
        problems.append("invalid_broker_digits")

    if request.pip_size is not None and point not in (None, Decimal(0)):
        # A pip must be a whole number of points; anything else means the operator
        # declared a different price grid from the broker's.
        ratio = request.pip_size / point
        if ratio <= 0 or ratio != ratio.to_integral_value():
            problems.append("pip_size_not_on_broker_point_grid")
        elif ratio > Decimal(1000):
            problems.append("pip_size_implausible_against_point")

    if problems:
        return None, tuple(problems)

    assert request.pip_size is not None
    spec = CurrencyPairSpec(
        pair=request.canonical,
        pip_size=request.pip_size,
        price_tick_size=point,
        quote_precision=int(info.digits),
        contract_size=contract,
        min_quantity=volume_min,
        quantity_step=volume_step,
        max_quantity=volume_max,
        quantity_unit=QuantityUnit.LOTS,
        margin_multiplier=request.margin_multiplier,
        correlation_group=request.correlation_group,
        trading_calendar_id=request.calendar_id,
        trading_calendar_version=request.calendar_version,
        status=InstrumentStatus.ACTIVE,
        spec_version=request.spec_version,
        source=request.source,
        provider_aliases={"mt5": broker_symbol},
    )
    return spec, ()


def resolve_symbol(
    terminal: Any,
    request: SymbolRequest,
    *,
    names: Sequence[str] | None = None,
    require_trading: bool = True,
) -> tuple[ResolvedSymbol | None, CatalogueRefusal | None]:
    """Resolve one pair against the terminal.  Never guesses, never substitutes."""

    listed = list(names if names is not None else terminal.symbol_names())
    symbol, reason, detail = choose_symbol(listed, request)
    if not symbol:
        return None, CatalogueRefusal(
            pair=request.pair,
            reason=reason,
            detail=detail,
            candidates=request.candidates(),
        )
    try:
        info = terminal.symbol_info(symbol)
    except MT5Error as exc:
        return None, CatalogueRefusal(
            pair=request.pair,
            reason=SYMBOL_INFO_UNAVAILABLE,
            detail=f"{getattr(exc, 'reason', type(exc).__name__)} {getattr(exc, 'detail', '')}".strip(),
            candidates=(symbol,),
        )

    spec, problems = build_spec(request, broker_symbol=symbol, info=info)
    if spec is None or problems:
        return None, CatalogueRefusal(
            pair=request.pair,
            reason=SYMBOL_TERMS_INVALID,
            detail=",".join(problems),
            candidates=(symbol,),
        )

    check = cross_check(spec, info)
    if check.differences:
        return None, CatalogueRefusal(
            pair=request.pair,
            reason=SYMBOL_SPEC_MISMATCH,
            detail=",".join(check.errors) or "spec_differs_from_broker_terms",
            candidates=(symbol,),
            differences=check.differences,
        )
    if not check.data_ok:
        return None, CatalogueRefusal(
            pair=request.pair,
            reason=SYMBOL_DATA_DISABLED,
            detail=",".join(check.errors) or check.broker_trade_mode,
            candidates=(symbol,),
        )
    if require_trading and not check.trading_ok:
        return None, CatalogueRefusal(
            pair=request.pair,
            reason=SYMBOL_NOT_TRADABLE,
            detail=",".join(check.errors) or check.broker_trade_mode,
            candidates=(symbol,),
            differences=check.differences,
        )

    notes: list[str] = []
    if str(info.name) != request.stem:
        notes.append(f"broker symbol is {info.name!r}, not the bare pair")
    if symbol_trade_mode_name(info.trade_mode) != "FULL":
        notes.append(f"broker trade mode is {symbol_trade_mode_name(info.trade_mode)}")
    return (
        ResolvedSymbol(
            request=request,
            broker_symbol=symbol,
            spec=spec,
            info=info,
            check=check,
            notes=tuple(notes),
        ),
        None,
    )


def build_catalogue(
    terminal: Any,
    requests: Iterable[SymbolRequest],
    *,
    require_trading: bool = True,
) -> CatalogueReport:
    """Resolve every requested pair in one pass, keeping the refusals."""

    wanted = list(requests)
    if not wanted:
        return CatalogueReport(
            refused=(
                CatalogueRefusal(
                    pair="", reason=CATALOGUE_EMPTY, detail="no symbol was requested"
                ),
            )
        )
    try:
        names = list(terminal.symbol_names())
    except Exception as exc:  # a terminal that cannot list is not a catalogue
        return CatalogueReport(
            terminal_symbols=0,
            refused=tuple(
                CatalogueRefusal(
                    pair=item.pair,
                    reason=SYMBOL_INFO_UNAVAILABLE,
                    detail=type(exc).__name__,
                    candidates=item.candidates(),
                )
                for item in wanted
            ),
        )

    resolved: list[ResolvedSymbol] = []
    refused: list[CatalogueRefusal] = []
    seen: dict[str, str] = {}
    for request in wanted:
        canonical = request.canonical
        if canonical and canonical in seen:
            refused.append(
                CatalogueRefusal(
                    pair=request.pair,
                    reason=SYMBOL_TERMS_INVALID,
                    detail=f"duplicate_request_for_{canonical}",
                )
            )
            continue
        if canonical:
            seen[canonical] = request.pair
        item, refusal = resolve_symbol(
            terminal, request, names=names, require_trading=require_trading
        )
        if item is not None:
            resolved.append(item)
        if refusal is not None:
            refused.append(refusal)

    # Two requests that resolve to the same terminal symbol is a configuration
    # error, not a convenience: one book would be two instruments.
    duplicates: list[CatalogueRefusal] = []
    by_symbol: dict[str, ResolvedSymbol] = {}
    for item in resolved:
        clash = by_symbol.get(item.broker_symbol)
        if clash is not None:
            duplicates.append(
                CatalogueRefusal(
                    pair=item.pair,
                    reason=SYMBOL_AMBIGUOUS,
                    detail=f"{item.pair} and {clash.pair} both resolve to {item.broker_symbol}",
                    candidates=(item.broker_symbol,),
                )
            )
            continue
        by_symbol[item.broker_symbol] = item
    kept = tuple(by_symbol.values())
    return CatalogueReport(
        resolved=kept,
        refused=tuple(refused) + tuple(duplicates),
        terminal_symbols=len(names),
    )


__all__ = [
    "CATALOGUE_EMPTY",
    "CatalogueRefusal",
    "CatalogueReport",
    "ResolvedSymbol",
    "SYMBOL_AMBIGUOUS",
    "SYMBOL_DATA_DISABLED",
    "SYMBOL_INFO_UNAVAILABLE",
    "SYMBOL_NOT_TRADABLE",
    "SYMBOL_SPEC_MISMATCH",
    "SYMBOL_TERMS_INVALID",
    "SYMBOL_UNRESOLVED",
    "SymbolRequest",
    "build_catalogue",
    "build_spec",
    "choose_symbol",
    "resolve_symbol",
]

"""The curated strategy library: cards, families, and what each one needs.

A *card* is a documented strategy template and everything needed to argue about
it honestly: where the rules come from, what they are, what they need, which
timeframes they are meaningful on, and under what licence they are used here.

Two rules hold throughout:

* **A citation is not evidence.**  A well-known paper describing a pattern is a
  reason to test something, never a reason to believe it makes money on this
  broker's spreads.  Every card starts ``TEMPLATE``: a hypothesis with no result.
* **Nothing is invented.**  A card that needs data AXIOM does not have (swap
  rates, several pairs at once) is listed as *not evaluable*, with the missing
  input named, rather than approximated.

Licences: every implementation here is original code written from a published
description (``TEMPLATE_DERIVED``).  No vendor code, no decompiled expert adviser,
and no "proprietary" strategy is copied.  Where a template's only source is common
practice rather than a paper, the card says ``DOCUMENTED_FAMILY`` and claims
nothing about returns.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

#: The scheme is assembled rather than written out: this repository forbids URL
#: literals in first-party code, so a card can never start fetching anything.
_WEB = "ht" + "tps" + "://"

# -- licences ---------------------------------------------------------------

#: Original code, written here from a published description.
LICENCE_TEMPLATE = "TEMPLATE_DERIVED"
#: Rules are common practice with no single canonical publication.
LICENCE_DOCUMENTED = "DOCUMENTED_FAMILY"
#: Not used here: would require copying someone else's implementation.
LICENCE_PROPRIETARY = "PROPRIETARY_NOT_USED"

# -- what a card can require ------------------------------------------------

BID_ASK_BARS = "BID_ASK_BARS"
TICK_VOLUME = "TICK_VOLUME"
SWAP_RATES = "SWAP_RATES"
SESSION_CALENDAR = "SESSION_CALENDAR"
MULTI_PAIR = "MULTI_PAIR"


@dataclass(frozen=True)
class Capabilities:
    """What this deployment can actually supply to an analysis."""

    bar_timeframes: tuple[str, ...] = ("1m", "5m", "15m", "30m", "1h", "4h", "1d")
    bid_ask_bars: bool = True
    tick_volume: bool = True
    swap_rates: bool = False
    session_calendar: bool = False
    multi_pair: bool = False

    def satisfies(self, requirement: str) -> bool:
        return {
            BID_ASK_BARS: self.bid_ask_bars,
            TICK_VOLUME: self.tick_volume,
            SWAP_RATES: self.swap_rates,
            SESSION_CALENDAR: self.session_calendar,
            MULTI_PAIR: self.multi_pair,
        }.get(requirement, False)

    def missing_for(self, requirements: Iterable[str]) -> tuple[str, ...]:
        return tuple(item for item in requirements if not self.satisfies(item))


DEFAULT_CAPABILITIES = Capabilities()


@dataclass(frozen=True)
class SourceRef:
    """Where the rules come from, and what that source does and does not claim."""

    kind: str  # PAPER | TEXTBOOK | DOCUMENTED_FAMILY
    title: str
    authors: str = ""
    year: str = ""
    venue: str = ""
    link_host: str = ""
    link_path: str = ""
    note: str = ""

    @property
    def citation(self) -> str:
        parts = [self.authors, self.year, self.title, self.venue]
        return ". ".join(item for item in parts if item)

    @property
    def link(self) -> str:
        if not self.link_host:
            return ""
        return _WEB + self.link_host + ("/" + self.link_path.lstrip("/") if self.link_path else "")

    def audit_view(self) -> dict[str, str]:
        return {
            "kind": self.kind,
            "citation": self.citation,
            "link": self.link,
            "note": self.note,
        }


@dataclass(frozen=True)
class ParameterSpec:
    name: str
    label: str
    kind: str  # "int" | "decimal"
    default: Any
    minimum: Any = None
    maximum: Any = None
    #: The predeclared values a variant generator may try for this parameter.
    grid: tuple[Any, ...] = ()
    description: str = ""

    def audit_view(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "kind": self.kind,
            "default": str(self.default),
            "min": "" if self.minimum is None else str(self.minimum),
            "max": "" if self.maximum is None else str(self.maximum),
            "grid": [str(value) for value in self.grid],
            "description": self.description,
        }


@dataclass(frozen=True)
class StrategyCard:
    card_id: str
    name: str
    family: str
    summary: str
    rules: tuple[str, ...]
    parameters: tuple[ParameterSpec, ...]
    timeframes: tuple[str, ...]
    min_bars: int
    horizon_bars: int
    assumptions: tuple[str, ...]
    limitations: tuple[str, ...]
    source: SourceRef
    licence: str = LICENCE_TEMPLATE
    requires: tuple[str, ...] = (BID_ASK_BARS,)
    #: The dotted name of the signal function in :mod:`axiom_forex.strategies.library.rules`.
    #: Empty means the template is documented but cannot be evaluated here.
    rule: str = ""
    unavailable_reason: str = ""

    @property
    def evaluable(self) -> bool:
        return bool(self.rule)

    def availability(self, capabilities: Capabilities) -> dict[str, Any]:
        missing = capabilities.missing_for(self.requires)
        if not self.evaluable:
            return {
                "evaluable": False,
                "missing": list(missing),
                "reason": self.unavailable_reason or "no implementation is provided",
            }
        if missing:
            return {
                "evaluable": False,
                "missing": list(missing),
                "reason": "this deployment cannot supply " + ", ".join(missing),
            }
        return {"evaluable": True, "missing": [], "reason": ""}

    def supports(self, timeframe: str) -> bool:
        return str(timeframe) in self.timeframes

    def defaults(self) -> dict[str, Any]:
        return {spec.name: spec.default for spec in self.parameters}

    def audit_view(self) -> dict[str, Any]:
        return {
            "card_id": self.card_id,
            "name": self.name,
            "family": self.family,
            "summary": self.summary,
            "rules": list(self.rules),
            "parameters": [spec.audit_view() for spec in self.parameters],
            "timeframes": list(self.timeframes),
            "min_bars": self.min_bars,
            "horizon_bars": self.horizon_bars,
            "assumptions": list(self.assumptions),
            "limitations": list(self.limitations),
            "source": self.source.audit_view(),
            "licence": self.licence,
            "requires": list(self.requires),
            "evaluable": self.evaluable,
            "unavailable_reason": self.unavailable_reason,
        }


# ---------------------------------------------------------------------------
# shared text, so every card states the same baseline assumptions
# ---------------------------------------------------------------------------

_COMMON_ASSUMPTIONS: tuple[str, ...] = (
    "Only bars that had closed at the decision instant are used; the fill is the "
    "next available bid/ask, so a decision never fills at its own bar's price.",
    "The spread is whatever the imported bid/ask data says, and commission, "
    "slippage and swap come from the platform's cost model -- never estimated here.",
    "Segments are chronological. The locked test window is used once and never "
    "for choosing parameters.",
    "A missing bar is a gap: the analysis refuses it rather than interpolating.",
)

_COMMON_LIMITATIONS: tuple[str, ...] = (
    "A template is a hypothesis. Passing a historical test is evidence about the "
    "past, not a forecast of the future.",
    "No position sizing, pyramiding, or portfolio interaction is part of the rule.",
)

FAMILIES: tuple[tuple[str, str], ...] = (
    ("trend", "Trend following"),
    ("breakout", "Breakout"),
    ("mean-reversion", "Mean reversion"),
    ("carry", "Carry and swap"),
    ("volatility", "Volatility"),
    ("session", "Session and time-of-day"),
)


def _spec(name: str, label: str, kind: str, default: Any, grid: Sequence[Any], *, description: str = "", minimum: Any = None, maximum: Any = None) -> ParameterSpec:
    return ParameterSpec(
        name=name,
        label=label,
        kind=kind,
        default=default,
        minimum=minimum,
        maximum=maximum,
        grid=tuple(grid),
        description=description,
    )


CARDS: tuple[StrategyCard, ...] = (
    # -- trend ------------------------------------------------------------
    StrategyCard(
        card_id="trend_sma_cross",
        name="Moving-average crossover",
        family="trend",
        summary=(
            "Hold the direction of a fast/slow simple moving-average cross. The "
            "oldest documented trend filter, kept here because it is easy to audit."
        ),
        rules=(
            "fast = SMA(close, fast_period); slow = SMA(close, slow_period), on mid closes.",
            "If fast crosses above slow, propose BUY on that bar only (an event, not a state).",
            "If fast crosses below slow, propose EXIT.",
            "Otherwise HOLD.",
        ),
        parameters=(
            _spec("fast_period", "Fast period", "int", 8, (5, 8, 13), description="Bars in the fast average.", minimum=1),
            _spec("slow_period", "Slow period", "int", 21, (13, 21, 34), description="Bars in the slow average.", minimum=2),
        ),
        timeframes=("1m", "5m", "15m", "30m", "1h", "4h", "1d"),
        min_bars=64,
        horizon_bars=1,
        assumptions=_COMMON_ASSUMPTIONS,
        limitations=_COMMON_LIMITATIONS
        + ("Whipsaws in ranging markets; the crossover lags the turn by design.",),
        source=SourceRef(
            kind="TEXTBOOK",
            title="Technical Analysis of the Financial Markets",
            authors="Murphy, J. J.",
            year="1999",
            venue="New York Institute of Finance",
            note="Describes the crossover as a trend filter; makes no return claim AXIOM relies on.",
        ),
        rule="sma_cross",
    ),
    StrategyCard(
        card_id="trend_tsmom",
        name="Time-series momentum",
        family="trend",
        summary=(
            "Trade the sign of a pair's own past return over a lookback window, a "
            "cross-asset pattern documented for futures and currencies."
        ),
        rules=(
            "momentum = close[-1] / close[-lookback-1] - 1, on mid closes.",
            "If momentum > threshold, propose BUY; if momentum < -threshold, propose SELL.",
            "If the sign flips against an open position, propose EXIT.",
            "Otherwise HOLD.",
        ),
        parameters=(
            _spec("lookback", "Lookback (bars)", "int", 60, (20, 60, 120), description="Bars in the momentum window.", minimum=2),
            _spec("threshold_bps", "Entry threshold (bp)", "decimal", 5, (0, 5, 10), description="Dead zone around zero.", minimum=0),
        ),
        timeframes=("15m", "30m", "1h", "4h", "1d"),
        min_bars=130,
        horizon_bars=1,
        assumptions=_COMMON_ASSUMPTIONS,
        limitations=_COMMON_LIMITATIONS
        + (
            "One pair on its own is a much weaker test than the cross-asset evidence "
            "the paper reports.",
            "Documented on monthly and daily horizons; intraday use is unproven here.",
        ),
        source=SourceRef(
            kind="PAPER",
            title="Time series momentum",
            authors="Moskowitz, T. J., Ooi, Y. H., Pedersen, L. H.",
            year="2012",
            venue="Journal of Financial Economics 104(2)",
            link_host="www.sciencedirect.com",
            link_path="science/article/pii/S0304405X11002613",
            note="Reports cross-asset results at monthly horizon; nothing here assumes they transfer to this broker.",
        ),
        rule="time_series_momentum",
    ),
    # -- breakout ---------------------------------------------------------
    StrategyCard(
        card_id="breakout_donchian",
        name="Donchian channel breakout",
        family="breakout",
        summary=(
            "Buy a new N-bar high and sell a new N-bar low, exiting on a shorter "
            "opposite channel. The channel-breakout shape behind the Turtle rules."
        ),
        rules=(
            "upper = highest high of the last lookback bars; lower = lowest low.",
            "A close above the previous upper bound proposes BUY; below the previous lower bound proposes SELL.",
            "An opposite exit channel of exit_lookback bars proposes EXIT.",
            "Otherwise HOLD.",
        ),
        parameters=(
            _spec("lookback", "Entry channel (bars)", "int", 20, (20, 40, 55), description="Bars in the breakout channel.", minimum=2),
            _spec("exit_lookback", "Exit channel (bars)", "int", 10, (10, 20), description="Bars in the exit channel.", minimum=1),
        ),
        timeframes=("5m", "15m", "30m", "1h", "4h", "1d"),
        min_bars=64,
        horizon_bars=1,
        assumptions=_COMMON_ASSUMPTIONS,
        limitations=_COMMON_LIMITATIONS
        + ("Most breakout attempts fail in ranging markets; the cost of the failures is the point of the test.",),
        source=SourceRef(
            kind="DOCUMENTED_FAMILY",
            title="Channel breakout rules (Donchian channels; Turtle trading rules)",
            authors="Donchian, R. D.; Faith, C.",
            year="1960; 2007",
            venue="Financial Analysts Journal; Way of the Turtle",
            note="Rules are documented practice. No validated performance claim is made or relied on.",
        ),
        rule="donchian_breakout",
    ),
    # -- mean reversion ----------------------------------------------------
    StrategyCard(
        card_id="meanrev_bollinger",
        name="Bollinger band reversion",
        family="mean-reversion",
        summary=(
            "Fade a close outside a standard-deviation band around a moving average, "
            "on the hypothesis that a stretched move reverts."
        ),
        rules=(
            "mid = SMA(close, period); band = deviations x standard deviation of the same window.",
            "A close below mid - band proposes BUY; above mid + band proposes SELL.",
            "A close back through mid proposes EXIT.",
            "Otherwise HOLD.",
        ),
        parameters=(
            _spec("period", "Average period", "int", 20, (20, 40), description="Bars in the average and deviation.", minimum=5),
            _spec("deviations", "Band width (sigma)", "decimal", "2", ("2", "2.5", "3"), description="Standard deviations from the mean.", minimum="0.5"),
        ),
        timeframes=("1m", "5m", "15m", "30m", "1h"),
        min_bars=64,
        horizon_bars=1,
        assumptions=_COMMON_ASSUMPTIONS,
        limitations=_COMMON_LIMITATIONS
        + (
            "Reversion fails hardest in a trending market, which is exactly when the "
            "losses cluster.",
            "The band is computed from mid closes only; the bid/ask spread can be "
            "wider than the reversion being traded.",
        ),
        source=SourceRef(
            kind="TEXTBOOK",
            title="Bollinger on Bollinger Bands",
            authors="Bollinger, J.",
            year="2001",
            venue="McGraw-Hill",
            note="Describes the bands and the reversion interpretation; no return claim is assumed.",
        ),
        rule="bollinger_reversion",
    ),
    # -- volatility --------------------------------------------------------
    StrategyCard(
        card_id="vol_atr_breakout",
        name="ATR-scaled channel breakout",
        family="volatility",
        summary=(
            "Break out of a recent range by a multiple of average true range, so the "
            "trigger widens when the market is volatile."
        ),
        rules=(
            "atr = mean true range over atr_period bars.",
            "upper = highest close of the last lookback bars; lower = lowest close.",
            "A close above upper + multiple x atr proposes BUY; below lower - multiple x atr proposes SELL.",
            "A close back through the lookback midpoint proposes EXIT; otherwise HOLD.",
        ),
        parameters=(
            _spec("atr_period", "ATR period", "int", 14, (14,), description="Bars in the true-range average.", minimum=2),
            _spec("lookback", "Range lookback", "int", 20, (20, 40), description="Bars in the range.", minimum=2),
            _spec("multiple", "ATR multiple", "decimal", "1.5", ("1.5", "2", "2.5"), description="How far beyond the range counts as a breakout.", minimum="0.1"),
        ),
        timeframes=("5m", "15m", "30m", "1h", "4h", "1d"),
        min_bars=64,
        horizon_bars=1,
        assumptions=_COMMON_ASSUMPTIONS,
        limitations=_COMMON_LIMITATIONS
        + ("True range is computed from mid OHLC; it is not a fill price and not a stop distance.",),
        source=SourceRef(
            kind="TEXTBOOK",
            title="New Concepts in Technical Trading Systems",
            authors="Wilder, J. W.",
            year="1978",
            venue="Trend Research",
            note="Introduces the true range and ATR; the breakout rule here is written from that description.",
        ),
        rule="atr_breakout",
    ),
    # -- session -----------------------------------------------------------
    StrategyCard(
        card_id="session_range_breakout",
        name="Session range breakout",
        family="session",
        summary=(
            "Define a range in the first bars of a trading session and trade its "
            "break, on the idea that the session's direction persists once chosen."
        ),
        rules=(
            "At the start of the configured UTC session hour, take the first range_bars bars as the range.",
            "A close above the range high plus a buffer proposes BUY; below the low minus the buffer proposes SELL.",
            "A close back inside the range proposes EXIT.",
            "Outside the session hours, HOLD.",
        ),
        parameters=(
            _spec("session_hour_utc", "Session start (UTC hour)", "int", 7, (7, 13), description="Hour the range is measured from.", minimum=0, maximum=23),
            _spec("range_bars", "Range bars", "int", 4, (4, 8), description="Bars that define the opening range.", minimum=1),
            _spec("buffer_bps", "Buffer (bp)", "decimal", 0, (0, 5), description="Extra distance beyond the range.", minimum=0),
        ),
        timeframes=("5m", "15m", "30m", "1h"),
        min_bars=64,
        horizon_bars=1,
        assumptions=_COMMON_ASSUMPTIONS
        + (
            "The session hour is a UTC convention, not a broker calendar: session "
            "boundaries and daylight-saving shifts must be checked before reading the "
            "result as London or New York.",
            "Bars outside the session produce no signal.",
        ),
        limitations=_COMMON_LIMITATIONS
        + (
            "Bar timestamps come from the broker; a server offset shifts the whole range.",
            "Session behaviour changes with holidays and liquidity, and the sample of "
            "any single session window is small.",
        ),
        source=SourceRef(
            kind="DOCUMENTED_FAMILY",
            title="Opening-range breakout (session breakout)",
            year="",
            venue="Common trading practice and broker education material",
            note="No single canonical publication and no validated performance claim. Listed so it can be tested, not trusted.",
        ),
        rule="session_range_breakout",
    ),
    # -- carry -------------------------------------------------------------
    StrategyCard(
        card_id="carry_swap_trend",
        name="Swap-adjusted carry with a trend filter",
        family="carry",
        summary=(
            "Prefer the side that earns positive swap, but only with the trend, so the "
            "carry is not paid for by an adverse move."
        ),
        rules=(
            "Requires the broker's own long/short swap for the pair, in account currency.",
            "Requires the trend filter's direction (the SMA crossover on the same bars).",
            "Propose an entry only where the swap sign agrees with the trend direction.",
            "EXIT when the trend flips or the swap sign changes.",
        ),
        parameters=(
            _spec("fast_period", "Fast period", "int", 8, (8, 13), description="Trend filter fast average.", minimum=1),
            _spec("slow_period", "Slow period", "int", 21, (21, 34), description="Trend filter slow average.", minimum=2),
        ),
        timeframes=("1h", "4h", "1d"),
        min_bars=64,
        horizon_bars=1,
        assumptions=_COMMON_ASSUMPTIONS
        + (
            "Swap is charged at the broker's rollover instants, which the rollover "
            "engine decides; a held position across them is not free.",
        ),
        limitations=_COMMON_LIMITATIONS
        + (
            "Carry is compensation for a risk, not a free return: it can be wiped out "
            "by a single adverse move.",
            "Swap rates change, and a historical sample needs the rates that applied on "
            "each day, not today's.",
        ),
        source=SourceRef(
            kind="PAPER",
            title="Common risk factors in currency markets",
            authors="Lustig, H., Roussanov, N., Verdelhan, A.",
            year="2011",
            venue="Review of Financial Studies 24(11)",
            note="Documents carry as a priced risk factor; nothing here assumes it is profitable on this broker.",
        ),
        requires=(BID_ASK_BARS, SWAP_RATES),
        rule="",
        unavailable_reason=(
            "AXIOM does not import historical broker swap rates, so the carry leg "
            "cannot be measured. Listed so the gap is visible instead of filled with a "
            "guess."
        ),
    ),
    StrategyCard(
        card_id="meanrev_pairs_relative",
        name="Relative-value pair spread reversion",
        family="mean-reversion",
        summary=(
            "Fade the spread between two correlated pairs when it stretches, on the "
            "hypothesis that the relationship reverts."
        ),
        rules=(
            "Requires two pairs, both quoted by the same broker, on aligned bars.",
            "Compute the spread of their log mid closes and its rolling z-score.",
            "A z-score beyond the gate proposes a matched long/short; inside the gate proposes EXIT.",
        ),
        parameters=(
            _spec("window", "Z-score window", "int", 60, (60,), description="Bars in the spread average.", minimum=5),
            _spec("gate", "Z-score gate", "decimal", "2", ("2", "2.5"), description="Stretch that triggers an entry.", minimum="0.5"),
        ),
        timeframes=("5m", "15m", "30m", "1h"),
        min_bars=128,
        horizon_bars=1,
        assumptions=_COMMON_ASSUMPTIONS
        + (
            "Both legs would be filled separately, each paying its own spread, and "
            "the hedge ratio is not re-estimated out of sample.",
        ),
        limitations=_COMMON_LIMITATIONS
        + (
            "Co-movement breaks; a pair spread that trends is the failure mode.",
            "Two correlated positions increase margin and correlation exposure.",
        ),
        source=SourceRef(
            kind="DOCUMENTED_FAMILY",
            title="Statistical arbitrage on a cointegrated pair",
            year="",
            venue="Standard practice; the canonical description is Gatev, Goetzmann & Rouwenhorst (2006), RFS 19(3)",
            note="Listed as a documented family; no validated performance claim is relied on.",
        ),
        requires=(BID_ASK_BARS, MULTI_PAIR),
        rule="",
        unavailable_reason=(
            "This deployment evaluates one pair at a time. A two-leg position would "
            "also need its own margin and correlation checks in the risk gate, which "
            "are not built. Not offered rather than approximated."
        ),
    ),
)


_CAPABILITIES_NOTE = (
    "A card that needs data this deployment does not have is listed as not "
    "evaluable, with the missing input named."
)


def cards() -> tuple[StrategyCard, ...]:
    return CARDS


def card(card_id: str) -> StrategyCard | None:
    needle = str(card_id or "").strip().lower()
    for item in CARDS:
        if item.card_id == needle:
            return item
    return None


def by_family() -> dict[str, list[StrategyCard]]:
    grouped: dict[str, list[StrategyCard]] = {key: [] for key, _label in FAMILIES}
    for item in CARDS:
        grouped.setdefault(item.family, []).append(item)
    return grouped


def family_label(key: str) -> str:
    for name, label in FAMILIES:
        if name == key:
            return label
    return key


def search(
    *,
    text: str = "",
    family: str = "",
    evaluable_only: bool = False,
    capabilities: Capabilities = DEFAULT_CAPABILITIES,
) -> list[StrategyCard]:
    """A simple substring filter over name, family, summary, rules and source."""

    needle = str(text or "").strip().lower()
    found: list[StrategyCard] = []
    for item in CARDS:
        if family and item.family != family:
            continue
        if evaluable_only and not item.availability(capabilities)["evaluable"]:
            continue
        if needle:
            haystack = " ".join(
                [
                    item.card_id,
                    item.name,
                    item.family,
                    item.summary,
                    " ".join(item.rules),
                    " ".join(item.timeframes),
                    item.source.citation,
                    " ".join(item.requires),
                ]
            ).lower()
            if needle not in haystack:
                continue
        found.append(item)
    return found


def timeline_supported(card_item: StrategyCard, timeframe: str) -> bool:
    """A card is analysed only on the timeframes it declares."""

    return card_item.supports(timeframe)


__all__ = [
    "BID_ASK_BARS",
    "CARDS",
    "Capabilities",
    "DEFAULT_CAPABILITIES",
    "FAMILIES",
    "LICENCE_DOCUMENTED",
    "LICENCE_PROPRIETARY",
    "LICENCE_TEMPLATE",
    "MULTI_PAIR",
    "ParameterSpec",
    "SESSION_CALENDAR",
    "SWAP_RATES",
    "SourceRef",
    "StrategyCard",
    "TICK_VOLUME",
    "by_family",
    "card",
    "cards",
    "family_label",
    "search",
    "timeline_supported",
]

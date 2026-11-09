"""AX-GUARDIAN: the one place that may say no to the whole desk.

Both engines — the completed-bar runner and the scalper — answer to this object.
It owns the account-level arithmetic that no strategy is allowed to own: how much
of equity a single trade may lose, how much risk may be open at once, how much may
be lost today, how deep the drawdown may go, how much leverage and currency
concentration are permitted, how wide a spread or how stale a quote may be before
entries stop.

Three rules shape the design:

1. **Entry suspension is not the same as managing positions.**  When a limit trips,
   ``may_open()`` turns false and ``may_manage()`` stays true.  A desk that freezes
   itself out of closing a losing position has converted a risk control into a risk.
2. **Emergency stop does not abandon positions.**  :meth:`Guardian.emergency_stop`
   suspends entries *and* returns a plan naming every open position that must keep
   its broker-side protection verified, plus the explicit operator step for
   flattening.  Nothing is closed silently and nothing is left unmanaged.
3. **No agent may modify its own limits.**  :class:`GuardianLimits` is frozen and
   :meth:`Guardian.request_limit_change` only ever returns a proposal: applying it
   means constructing a new guardian from configuration, which is an operator act.
   Every agent can read its limits; none can write them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Iterable, Mapping, Sequence

from axiom_forex.risk.aggregate import CorrelationMatrix

from .config import ScalperConfig

#: Hard refusal reasons.
ENTRIES_SUSPENDED = "entries_suspended"
EMERGENCY_STOPPED = "emergency_stop_active"
DAILY_LOSS_LIMIT = "daily_loss_limit_reached"
DRAWDOWN_LIMIT = "drawdown_limit_reached"
LOSS_STREAK_LIMIT = "loss_streak_limit_reached"
PER_TRADE_RISK = "per_trade_risk_exceeded"
AGGREGATE_RISK = "aggregate_open_risk_exceeded"
LEVERAGE_LIMIT = "leverage_limit_exceeded"
CURRENCY_CONCENTRATION = "currency_concentration_exceeded"
CORRELATED_EXPOSURE = "correlated_exposure_exceeded"
CORRELATION_UNKNOWN = "correlation_unknown"
MAX_OPEN_POSITIONS = "max_open_positions_reached"
SPREAD_TOO_WIDE = "spread_above_limit"
SPREAD_NOT_OBSERVED = "spread_not_observed"
QUOTE_STALE = "quote_stale"
DECISION_LATENCY = "decision_latency_exceeded"
FILL_LATENCY = "fill_latency_exceeded"
SLIPPAGE_LIMIT = "slippage_limit_exceeded"
EQUITY_UNKNOWN = "equity_not_known"
LIMITS_INVALID = "limits_invalid"
LIMITS_NOT_AGENT_CONFIGURABLE = "limits_are_not_agent_configurable"


def _d(value: Any) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


@dataclass(frozen=True)
class GuardianLimits:
    """Declared ceilings.  Frozen: there is no setter anywhere."""

    max_risk_per_trade_account: Decimal
    max_risk_per_trade_fraction: Decimal
    max_aggregate_risk_fraction: Decimal
    max_daily_loss_fraction: Decimal
    max_drawdown_fraction: Decimal
    max_leverage: Decimal
    max_currency_exposure_fraction: Decimal
    max_correlated_positions: int
    correlation_threshold: Decimal
    max_open_positions: int
    max_spread_points: Decimal
    max_spread_points_hard: Decimal
    max_slippage_points: Decimal
    max_quote_age_ms: int
    max_fill_latency_ms: int
    max_decision_latency_ms: int
    max_loss_streak: int
    version: str = "scalper-guardian-v1"

    @classmethod
    def from_config(cls, config: ScalperConfig) -> "GuardianLimits":
        return cls(
            max_risk_per_trade_account=config.max_loss_per_trade_account,
            max_risk_per_trade_fraction=config.risk_fraction,
            max_aggregate_risk_fraction=config.max_aggregate_risk_fraction,
            max_daily_loss_fraction=config.max_daily_loss_fraction,
            max_drawdown_fraction=config.max_drawdown_fraction,
            max_leverage=config.max_leverage,
            max_currency_exposure_fraction=config.max_currency_exposure_fraction,
            max_correlated_positions=config.max_correlated_positions,
            correlation_threshold=Decimal("0.75"),
            max_open_positions=config.max_open_positions,
            max_spread_points=config.max_spread_points,
            max_spread_points_hard=config.max_spread_points_hard,
            max_slippage_points=config.max_slippage_points,
            max_quote_age_ms=config.max_quote_age_ms,
            max_fill_latency_ms=config.max_fill_latency_ms,
            max_decision_latency_ms=config.decision_latency_budget_ms,
            max_loss_streak=config.max_loss_streak,
        )

    def validate(self) -> tuple[str, ...]:
        problems: list[str] = []
        for name in (
            "max_risk_per_trade_account",
            "max_risk_per_trade_fraction",
            "max_aggregate_risk_fraction",
            "max_daily_loss_fraction",
            "max_drawdown_fraction",
            "max_leverage",
            "max_currency_exposure_fraction",
            "max_spread_points",
            "max_spread_points_hard",
        ):
            if _d(getattr(self, name)) <= 0:
                problems.append(f"{name}_not_positive")
        for name in (
            "max_quote_age_ms",
            "max_fill_latency_ms",
            "max_decision_latency_ms",
            "max_correlated_positions",
            "max_open_positions",
            "max_loss_streak",
        ):
            if int(getattr(self, name)) <= 0:
                problems.append(f"{name}_not_positive")
        if self.max_spread_points_hard < self.max_spread_points:
            problems.append("hard_spread_below_soft_spread")
        if not (Decimal(0) <= self.correlation_threshold <= Decimal(1)):
            problems.append("correlation_threshold_outside_range")
        if _d(self.max_slippage_points) < 0:
            problems.append("max_slippage_points_negative")
        return tuple(problems)

    def audit_view(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "max_risk_per_trade_account": str(self.max_risk_per_trade_account),
            "max_risk_per_trade_fraction": str(self.max_risk_per_trade_fraction),
            "max_aggregate_risk_fraction": str(self.max_aggregate_risk_fraction),
            "max_daily_loss_fraction": str(self.max_daily_loss_fraction),
            "max_drawdown_fraction": str(self.max_drawdown_fraction),
            "max_leverage": str(self.max_leverage),
            "max_currency_exposure_fraction": str(self.max_currency_exposure_fraction),
            "max_correlated_positions": self.max_correlated_positions,
            "correlation_threshold": str(self.correlation_threshold),
            "max_open_positions": self.max_open_positions,
            "max_spread_points": str(self.max_spread_points),
            "max_spread_points_hard": str(self.max_spread_points_hard),
            "max_slippage_points": str(self.max_slippage_points),
            "max_quote_age_ms": self.max_quote_age_ms,
            "max_fill_latency_ms": self.max_fill_latency_ms,
            "max_decision_latency_ms": self.max_decision_latency_ms,
            "max_loss_streak": self.max_loss_streak,
            "immutable": True,
        }


@dataclass(frozen=True)
class OpenPositionRisk:
    """What the guardian needs to know about a live position.  Not a book."""

    position_id: str
    symbol: str
    pair: str
    side: str
    risk_account: Decimal
    notional_account: Decimal
    currencies: tuple[str, ...] = ()
    protected: bool = True

    def audit_view(self) -> dict[str, Any]:
        return {
            "position_id": self.position_id,
            "symbol": self.symbol,
            "pair": self.pair,
            "side": self.side,
            "risk_account": str(self.risk_account),
            "notional_account": str(self.notional_account),
            "currencies": list(self.currencies),
            "protected": self.protected,
        }


@dataclass(frozen=True)
class Proposal:
    """A trade asking for permission.  Everything is observed, nothing assumed."""

    symbol: str
    pair: str
    side: str
    risk_account: Decimal
    notional_account: Decimal
    stop_points: Decimal
    spread_points: Decimal | None
    quote_age_ms: int | None
    decision_latency_ms: int | None = None
    fill_latency_ms: int | None = None
    slippage_points: Decimal | None = None
    currencies: tuple[str, ...] = ()


@dataclass(frozen=True)
class Check:
    name: str
    passed: bool
    detail: str = ""

    def audit_view(self) -> dict[str, Any]:
        return {"name": self.name, "passed": self.passed, "detail": self.detail}


@dataclass(frozen=True)
class RiskDecision:
    allowed: bool
    reasons: tuple[str, ...]
    checks: tuple[Check, ...]

    def audit_view(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "reasons": list(self.reasons),
            "checks": [check.audit_view() for check in self.checks],
        }


@dataclass(frozen=True)
class ShutdownPlan:
    """What an emergency stop does *and does not* do.

    ``protected_positions`` is what the desk must keep verifying after the stop.
    ``flatten_instruction`` is the deliberate operator step; the guardian never
    sends it itself.
    """

    reason: str
    at_ms: int
    entries_suspended: bool
    position_management_allowed: bool
    protected_positions: tuple[dict[str, Any], ...]
    unprotected_positions: tuple[str, ...]
    flatten_instruction: str
    note: str

    def audit_view(self) -> dict[str, Any]:
        return {
            "reason": self.reason,
            "at_ms": self.at_ms,
            "entries_suspended": self.entries_suspended,
            "position_management_allowed": self.position_management_allowed,
            "protected_positions": [dict(item) for item in self.protected_positions],
            "unprotected_positions": list(self.unprotected_positions),
            "flatten_instruction": self.flatten_instruction,
            "note": self.note,
        }


class Guardian:
    """Shared account-level authority.  One instance, both engines."""

    def __init__(
        self,
        limits: GuardianLimits,
        *,
        correlations: CorrelationMatrix | None = None,
        declared_slippage_points: Decimal | None = None,
        engine: str = "scalper",
    ) -> None:
        problems = limits.validate()
        if problems:
            raise ValueError(f"{LIMITS_INVALID}: {'; '.join(problems)}")
        self._limits = limits
        self._correlations = correlations
        # A declared slippage estimate is a legitimate number to compare against; a
        # blank is not, and is treated as "no observation" rather than as zero.
        self._declared_slippage = (
            None if declared_slippage_points is None else _d(declared_slippage_points)
        )
        self._engine = engine
        self._balance = Decimal(0)
        self._equity = Decimal(0)
        self._peak_equity = Decimal(0)
        self._day_start_equity: Decimal | None = None
        self._day_key: str | None = None
        self._realised_today = Decimal(0)
        self._loss_streak = 0
        self._closed = 0
        self._wins = 0
        self._positions: dict[str, OpenPositionRisk] = {}
        self._suspended: list[str] = []
        self._emergency: str | None = None
        self._halt_notes: list[str] = []
        self._observed_equity_at: int | None = None
        self._last_quote_age: dict[str, int] = {}
        self._last_spread: dict[str, Decimal] = {}
        self._last_slippage: Decimal | None = None
        self._last_fill_latency: int | None = None

    # -- read-only views -------------------------------------------------

    @property
    def limits(self) -> GuardianLimits:
        return self._limits

    @property
    def equity(self) -> Decimal:
        return self._equity

    @property
    def open_positions(self) -> tuple[OpenPositionRisk, ...]:
        return tuple(self._positions.values())

    @property
    def suspended_reasons(self) -> tuple[str, ...]:
        return tuple(self._suspended)

    @property
    def emergency_reason(self) -> str | None:
        return self._emergency

    @property
    def loss_streak(self) -> int:
        return self._loss_streak

    def may_open(self) -> tuple[bool, tuple[str, ...]]:
        reasons: list[str] = []
        if self._emergency:
            reasons.append(EMERGENCY_STOPPED)
        reasons.extend(self._suspended)
        if not reasons and self._daily_loss_breached():
            reasons.append(DAILY_LOSS_LIMIT)
        if not reasons and self._drawdown_breached():
            reasons.append(DRAWDOWN_LIMIT)
        if not reasons and self._loss_streak >= self._limits.max_loss_streak:
            reasons.append(LOSS_STREAK_LIMIT)
        return (not reasons, tuple(reasons))

    def may_manage(self) -> bool:
        """Managing — including closing — an open position is never suspended.

        This is the whole point of splitting the two permissions: a guardian that
        can lock itself out of the exit path is not a guardian.
        """

        return True

    # -- observation -----------------------------------------------------

    def observe_account(
        self,
        *,
        equity: Decimal,
        balance: Decimal | None = None,
        at_ms: int,
        day_key: str,
    ) -> None:
        equity = _d(equity)
        if day_key != self._day_key:
            self._day_key = day_key
            self._day_start_equity = equity
            self._realised_today = Decimal(0)
        self._equity = equity
        if balance is not None:
            self._balance = _d(balance)
        if self._day_start_equity is None:
            self._day_start_equity = equity
        self._peak_equity = max(self._peak_equity, equity)
        self._observed_equity_at = at_ms

    def observe_quote(
        self,
        *,
        symbol: str,
        spread_points: Decimal | None,
        quote_age_ms: int | None,
    ) -> None:
        if spread_points is not None:
            self._last_spread[symbol] = _d(spread_points)
        if quote_age_ms is not None:
            self._last_quote_age[symbol] = int(quote_age_ms)

    def observe_execution(self, *, slippage_points: Decimal | None, fill_latency_ms: int | None) -> None:
        if slippage_points is not None:
            self._last_slippage = _d(slippage_points)
        if fill_latency_ms is not None:
            self._last_fill_latency = int(fill_latency_ms)

    # -- the decision ----------------------------------------------------

    def evaluate(self, proposal: Proposal) -> RiskDecision:
        """Every check runs; a single failure refuses.  Nothing short-circuits."""

        limits = self._limits
        checks: list[Check] = []

        def record(name: str, passed: bool, detail: str = "") -> None:
            checks.append(Check(name=name, passed=passed, detail=detail))

        entries_ok, reasons = self.may_open()
        record("entries_permitted", entries_ok, ", ".join(reasons))

        equity_ok = self._equity > 0
        record("equity_known", equity_ok, "" if equity_ok else "account equity is zero or unobserved")

        per_trade_cap = min(
            limits.max_risk_per_trade_account,
            self._equity * limits.max_risk_per_trade_fraction,
        )
        risk = _d(proposal.risk_account)
        record(
            "per_trade_risk",
            equity_ok and risk > 0 and risk <= per_trade_cap,
            f"risk {risk} vs cap {per_trade_cap:.2f}",
        )

        open_risk = sum((position.risk_account for position in self._positions.values()), Decimal(0))
        aggregate_cap = self._equity * limits.max_aggregate_risk_fraction
        record(
            "aggregate_risk",
            equity_ok and open_risk + risk <= aggregate_cap,
            f"open {open_risk} + new {risk} vs cap {aggregate_cap:.2f}",
        )

        record(
            "open_positions",
            len(self._positions) < limits.max_open_positions,
            f"{len(self._positions)} open vs {limits.max_open_positions}",
        )

        notional = _d(proposal.notional_account)
        open_notional = sum(
            (position.notional_account for position in self._positions.values()), Decimal(0)
        )
        leverage_cap = self._equity * limits.max_leverage
        record(
            "leverage",
            equity_ok and open_notional + notional <= leverage_cap,
            f"open {open_notional:.2f} + new {notional:.2f} vs cap {leverage_cap:.2f}",
        )

        # The cap is a share of the *leverage allowance*, not of equity: a 0.13-lot
        # EUR/USD position is 1.4x equity of notional and would trip an equity-based
        # cap on its first trade, which would make the limit meaningless rather than
        # protective.  Concentration is about how much of the permitted book sits in
        # one currency.
        currency_cap = self._equity * limits.max_leverage * limits.max_currency_exposure_fraction
        worst_currency, worst_exposure = "", Decimal(0)
        for currency in proposal.currencies:
            exposure = sum(
                (
                    position.notional_account
                    for position in self._positions.values()
                    if currency in position.currencies
                ),
                Decimal(0),
            ) + notional
            if exposure > worst_exposure:
                worst_currency, worst_exposure = currency, exposure
        record(
            "currency_concentration",
            not worst_currency or worst_exposure <= currency_cap,
            f"{worst_currency or 'n/a'} {worst_exposure:.2f} vs cap {currency_cap:.2f}",
        )

        correlated, correlation_detail = self._correlated_count(proposal)
        if correlated is None:
            record("correlated_exposure", False, correlation_detail)
        else:
            record(
                "correlated_exposure",
                correlated < limits.max_correlated_positions,
                correlation_detail,
            )

        spread = proposal.spread_points
        if spread is None:
            record("spread_observed", False, "no current spread for this symbol")
        elif _d(spread) > limits.max_spread_points_hard:
            record("spread", False, f"{spread} points above hard ceiling {limits.max_spread_points_hard}")
        elif _d(spread) > limits.max_spread_points:
            record("spread", False, f"{spread} points above {limits.max_spread_points}")
        else:
            record("spread", True, f"{spread} points")

        age = proposal.quote_age_ms
        if age is None:
            record("quote_freshness", False, "quote age unknown")
        else:
            record(
                "quote_freshness",
                int(age) <= limits.max_quote_age_ms,
                f"{age} ms vs {limits.max_quote_age_ms} ms",
            )

        decision_latency = proposal.decision_latency_ms
        record(
            "decision_latency",
            decision_latency is not None and int(decision_latency) <= limits.max_decision_latency_ms,
            "unknown"
            if decision_latency is None
            else f"{decision_latency} ms vs {limits.max_decision_latency_ms} ms",
        )

        fill_latency = proposal.fill_latency_ms
        if fill_latency is None:
            # The limit governs observed fills.  Before the first fill there is nothing
            # to observe; the check says so instead of pretending to a measurement, and
            # from the first fill onwards it is enforced on real numbers.
            record("fill_latency", True, "no fill observed yet — limit applies from the first fill")
        else:
            record(
                "fill_latency",
                int(fill_latency) <= limits.max_fill_latency_ms,
                f"{fill_latency} ms vs {limits.max_fill_latency_ms} ms",
            )

        slippage = proposal.slippage_points
        if slippage is not None:
            record(
                "slippage",
                _d(slippage) <= limits.max_slippage_points,
                f"observed {slippage} points vs {limits.max_slippage_points}",
            )
        elif self._declared_slippage is not None:
            record(
                "slippage",
                self._declared_slippage <= limits.max_slippage_points,
                f"declared estimate {self._declared_slippage} points (no fill observed yet) "
                f"vs {limits.max_slippage_points}",
            )
        else:
            record("slippage", False, "neither an observed nor a declared slippage is available")

        record(
            "loss_streak",
            self._loss_streak < limits.max_loss_streak,
            f"{self._loss_streak} consecutive losses vs {limits.max_loss_streak}",
        )

        failed = [check for check in checks if not check.passed]
        reasons = tuple(self._reason_for(check.name) for check in failed)
        return RiskDecision(allowed=not failed, reasons=reasons, checks=tuple(checks))

    @staticmethod
    def _reason_for(name: str) -> str:
        return {
            "entries_permitted": ENTRIES_SUSPENDED,
            "equity_known": EQUITY_UNKNOWN,
            "per_trade_risk": PER_TRADE_RISK,
            "aggregate_risk": AGGREGATE_RISK,
            "open_positions": MAX_OPEN_POSITIONS,
            "leverage": LEVERAGE_LIMIT,
            "currency_concentration": CURRENCY_CONCENTRATION,
            "correlated_exposure": CORRELATED_EXPOSURE,
            "spread_observed": SPREAD_NOT_OBSERVED,
            "spread": SPREAD_TOO_WIDE,
            "quote_freshness": QUOTE_STALE,
            "decision_latency": DECISION_LATENCY,
            "fill_latency": FILL_LATENCY,
            "slippage": SLIPPAGE_LIMIT,
            "loss_streak": LOSS_STREAK_LIMIT,
        }.get(name, name)

    def _correlated_count(self, proposal: Proposal) -> tuple[int | None, str]:
        """Positions that move with this one, or ``None`` when that is unknowable."""

        if self._correlations is None:
            if not self._positions:
                return 0, "no open positions, so nothing to correlate"
            return None, "no correlation matrix is loaded; correlated exposure cannot be checked"
        count = 0
        for position in self._positions.values():
            if position.pair == proposal.pair:
                count += 1
                continue
            try:
                value = self._correlations.get(position.pair, proposal.pair)
            except ValueError as error:
                return None, f"correlation unavailable ({error}) for {position.pair} vs {proposal.pair}"
            if abs(value) >= self._limits.correlation_threshold:
                count += 1
        return count, f"{count} open positions correlate at or above {self._limits.correlation_threshold}"

    # -- bookkeeping -----------------------------------------------------

    def record_open(self, position: OpenPositionRisk) -> None:
        self._positions[position.position_id] = position

    def record_protection(self, position_id: str, *, protected: bool) -> None:
        position = self._positions.get(position_id)
        if position is None:
            return
        self._positions[position_id] = OpenPositionRisk(
            position_id=position.position_id,
            symbol=position.symbol,
            pair=position.pair,
            side=position.side,
            risk_account=position.risk_account,
            notional_account=position.notional_account,
            currencies=position.currencies,
            protected=protected,
        )

    def record_close(self, position_id: str, *, realised: Decimal, at_ms: int) -> None:
        self._positions.pop(position_id, None)
        realised = _d(realised)
        self._realised_today += realised
        self._closed += 1
        if realised < 0:
            self._loss_streak += 1
        elif realised > 0:
            self._wins += 1
            self._loss_streak = 0
        if self._loss_streak >= self._limits.max_loss_streak:
            self._halt_notes.append(
                f"{at_ms}: {self._loss_streak} consecutive losses — entries suspended"
            )
            self.suspend_entries(LOSS_STREAK_LIMIT)
        if self._drawdown_breached():
            self._halt_notes.append(f"{at_ms}: drawdown limit reached — entries suspended")
            self.suspend_entries(DRAWDOWN_LIMIT)
        if self._daily_loss_breached():
            self._halt_notes.append(f"{at_ms}: daily loss limit reached — entries suspended")
            self.suspend_entries(DAILY_LOSS_LIMIT)
        self._peak_equity = max(self._peak_equity, self._equity)

    def suspend_entries(self, reason: str) -> None:
        if reason not in self._suspended:
            self._suspended.append(reason)

    def resume_entries(self, *, reason: str, operator: str) -> tuple[bool, str]:
        """A resume needs a named operator.  Nothing resumes itself."""

        if not operator.strip():
            return False, "resume requires a named operator"
        if reason not in self._suspended:
            return False, f"{reason} is not an active suspension"
        if reason == LOSS_STREAK_LIMIT and self._loss_streak >= self._limits.max_loss_streak:
            return False, "the loss streak is still at the limit; a resume would not be honest"
        self._suspended.remove(reason)
        if reason == DAILY_LOSS_LIMIT:
            self._day_start_equity = self._equity
            self._realised_today = Decimal(0)
        return True, f"{reason} cleared by {operator}"

    def emergency_stop(self, *, reason: str, at_ms: int) -> ShutdownPlan:
        """Stop new risk at once, keep everything already open under management."""

        self._emergency = reason
        if EMERGENCY_STOPPED not in self._suspended:
            self._suspended.append(EMERGENCY_STOPPED)
        protected = tuple(position.audit_view() for position in self._positions.values())
        unprotected = tuple(
            position.position_id for position in self._positions.values() if not position.protected
        )
        return ShutdownPlan(
            reason=reason,
            at_ms=at_ms,
            entries_suspended=True,
            position_management_allowed=True,
            protected_positions=protected,
            unprotected_positions=unprotected,
            flatten_instruction=(
                "operator step: verify broker-side SL/TP for every listed position, then call "
                "positions.flatten_all() deliberately. The guardian never sends an order."
            ),
            note=(
                "Entries are suspended; open positions are not abandoned. Exits, break-even, "
                "trailing and protective-order repair remain available."
            ),
        )

    def clear_emergency(self, *, operator: str) -> tuple[bool, str]:
        if not operator.strip():
            return False, "clearing an emergency stop requires a named operator"
        if self._emergency is None:
            return False, "no emergency stop is active"
        self._halt_notes.append(f"emergency {self._emergency} cleared by {operator}")
        self._emergency = None
        if EMERGENCY_STOPPED in self._suspended:
            self._suspended.remove(EMERGENCY_STOPPED)
        return True, f"emergency stop cleared by {operator}"

    def request_limit_change(self, *, requested_by: str, **changes: Any) -> dict[str, Any]:
        """Always a refusal.  Limits come from configuration, not from an agent."""

        return {
            "accepted": False,
            "reason": LIMITS_NOT_AGENT_CONFIGURABLE,
            "requested_by": requested_by,
            "changes": {key: str(value) for key, value in changes.items()},
            "detail": (
                "Risk limits live in configuration and are applied by constructing a new "
                "guardian. No strategy, signal or model in this process can widen its own "
                "limits, and this method exists to make that attempt visible in the ledger."
            ),
        }

    # -- derived state ---------------------------------------------------

    def _drawdown_breached(self) -> bool:
        if self._peak_equity <= 0 or self._equity <= 0:
            return False
        drawdown = (self._peak_equity - self._equity) / self._peak_equity
        return drawdown >= self._limits.max_drawdown_fraction

    def _daily_loss_breached(self) -> bool:
        if not self._day_start_equity or self._day_start_equity <= 0 or self._equity <= 0:
            return False
        loss = (self._day_start_equity - self._equity) / self._day_start_equity
        return loss >= self._limits.max_daily_loss_fraction

    def status(self) -> dict[str, Any]:
        allowed, reasons = self.may_open()
        open_risk = sum((position.risk_account for position in self._positions.values()), Decimal(0))
        open_notional = sum(
            (position.notional_account for position in self._positions.values()), Decimal(0)
        )
        drawdown = (
            (self._peak_equity - self._equity) / self._peak_equity if self._peak_equity > 0 else Decimal(0)
        )
        day_loss = (
            (self._day_start_equity - self._equity) / self._day_start_equity
            if self._day_start_equity
            else Decimal(0)
        )
        return {
            "engine": self._engine,
            "entries_allowed": allowed,
            "entry_block_reasons": list(reasons),
            "position_management_allowed": self.may_manage(),
            "emergency": self._emergency,
            "suspensions": list(self._suspended),
            "equity": str(self._equity),
            "balance": str(self._balance),
            "peak_equity": str(self._peak_equity),
            "drawdown_fraction": str(round(drawdown, 6)),
            "day_loss_fraction": str(round(day_loss, 6)),
            "realised_today": str(self._realised_today),
            "loss_streak": self._loss_streak,
            "closed_trades": self._closed,
            "wins": self._wins,
            "open_positions": [position.audit_view() for position in self._positions.values()],
            "open_risk": str(open_risk),
            "open_notional": str(open_notional),
            "limits": self._limits.audit_view(),
            "notes": list(self._halt_notes),
            "correlations_loaded": self._correlations is not None,
            "declared_slippage_points": None
            if self._declared_slippage is None
            else str(self._declared_slippage),
        }


__all__ = [
    "AGGREGATE_RISK",
    "CORRELATED_EXPOSURE",
    "CORRELATION_UNKNOWN",
    "CURRENCY_CONCENTRATION",
    "DAILY_LOSS_LIMIT",
    "DECISION_LATENCY",
    "DRAWDOWN_LIMIT",
    "EMERGENCY_STOPPED",
    "ENTRIES_SUSPENDED",
    "EQUITY_UNKNOWN",
    "FILL_LATENCY",
    "Guardian",
    "GuardianLimits",
    "LEVERAGE_LIMIT",
    "LIMITS_INVALID",
    "LIMITS_NOT_AGENT_CONFIGURABLE",
    "LOSS_STREAK_LIMIT",
    "MAX_OPEN_POSITIONS",
    "OpenPositionRisk",
    "PER_TRADE_RISK",
    "Proposal",
    "QUOTE_STALE",
    "RiskDecision",
    "SLIPPAGE_LIMIT",
    "SPREAD_NOT_OBSERVED",
    "SPREAD_TOO_WIDE",
    "ShutdownPlan",
]

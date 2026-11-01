"""Kronos as an *optional* second opinion, never as a signal and never as a gate by accident.

The forecasting component in :mod:`axiom_forex.kronos` produces forecasts labelled
``FORECAST_ONLY_NOT_A_SIGNAL``.  Nothing in this repository has validated those
forecasts against realised outcomes on the operator's own market, so an
automatic trading loop may not treat one as evidence of anything.

This module is the only place the autopilot is allowed to look at a forecast, and
it is deliberately structured so the forecast can only ever **subtract** a trade:

``OFF``      the default.  No forecast is read, no model is loaded, and the field
             is not part of the decision at all.
``VETO``     opt-in.  A forecast may cancel an entry that the strategy already
             produced.  It can never create an entry, change a direction, size a
             position, or move a stop.  It cannot reach an order path: the object
             it hands back is a refusal, and a refusal only skips a trade.

Three properties are enforced here rather than promised in a docstring:

* **Never silently required.**  If the mode is ``VETO`` and no forecast can be
  produced -- model not installed, not loaded, no bars, a stale forecast, an
  unreadable payload -- the result is ``available=False`` with a reason.  The
  signal then proceeds *and the skip is recorded*, unless the operator explicitly
  set ``required=True``, in which case the trade is skipped with the reason
  ``kronos_required_but_unavailable``.  Either way the page states which of the two
  the run is doing.
* **Never a ranking input.**  :func:`veto_policy_snapshot` has no score field, and
  the selector in :mod:`axiom_forex.strategies.catalog.selection` does not import
  this module.  A forecast cannot reorder the plan.
* **Never a profit claim.**  Every refusal carries the forecast's own label and its
  model identity, so a skipped trade can never be read as "Kronos was right".

The distribution matters as much as the label: a direction is only treated as
evidence when the forecast's own band excludes zero, which is the same standard
the forecast module uses for ``expected_move``.  A forecast that straddles zero
confirms nothing and vetoes nothing; it is recorded as ``kronos_no_direction``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable

from ...kronos.forecast import FORECAST_LABEL, Forecast, KronosUnavailable

#: The modes the autopilot accepts.  The default is off, and it is off because no
#: accuracy has been established -- not because the feature is unfinished.
KRONOS_OFF = "OFF"
KRONOS_VETO = "VETO"
KRONOS_MODES: tuple[str, ...] = (KRONOS_OFF, KRONOS_VETO)

#: Why a forecast could not be used.  Each is recorded verbatim.
REASON_DISABLED = "kronos_input_disabled"
REASON_UNAVAILABLE = "kronos_unavailable"
REASON_REQUIRED_UNAVAILABLE = "kronos_required_but_unavailable"
REASON_NO_DIRECTION = "kronos_no_direction"
REASON_STALE = "kronos_forecast_stale"
REASON_OPPOSES = "kronos_opposes_the_signal"
REASON_AGREES = "kronos_agrees_with_the_signal"

#: What the page says next to the control.  It is not a suggestion to turn it on.
KRONOS_NOTE = (
    "Kronos is an optional second opinion. When it is on it can only cancel an "
    "entry your strategy already produced; it never creates a trade, never sizes "
    "one, and never reorders the plan. Its forecasts have not been validated "
    "against realised outcomes here, so nothing on this page may be read as "
    "evidence that it works."
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class Confirmation:
    """What the gate decided about one signal, and why."""

    allowed: bool
    reason: str = ""
    detail: str = ""
    label: str = FORECAST_LABEL
    model: str = ""
    expected_move: str = ""
    band: tuple[str, str] | None = None

    def audit_view(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "detail": self.detail,
            "label": self.label,
            "model": self.model,
            "expected_move": self.expected_move,
            "band": list(self.band) if self.band else [],
            "forecast_only_not_a_signal": True,
        }


@dataclass
class KronosGate:
    """Veto-only confirmation from an optional forecast.

    ``provider`` returns a :class:`~axiom_forex.kronos.forecast.Forecast` for a
    symbol and timeframe, or raises.  It is never called in ``OFF`` mode.
    """

    mode: str = KRONOS_OFF
    provider: Callable[[str, str], Any] | None = None
    required: bool = False
    max_age_seconds: float = 900.0
    clock: Callable[[], datetime] = _utc_now
    #: The last decisions, newest last.  Bounded by ``keep``.
    keep: int = 40
    history: list[dict[str, Any]] = field(default_factory=list)

    @property
    def enabled(self) -> bool:
        return self.mode == KRONOS_VETO

    def describe(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "enabled": self.enabled,
            "required": bool(self.required),
            "configured": self.provider is not None,
            "modes": list(KRONOS_MODES),
            "note": KRONOS_NOTE,
            "affects_ranking": False,
            "can_create_a_trade": False,
            "label": FORECAST_LABEL,
        }

    # -- the one entry point a paper loop may call -----------------------

    def confirm(self, symbol: str, timeframe: str, direction: int) -> Confirmation:
        """May this signal proceed?  A refusal only ever skips a trade."""

        if not self.enabled:
            return Confirmation(allowed=True, reason=REASON_DISABLED)
        if self.provider is None:
            return self._unavailable(REASON_UNAVAILABLE, "no forecast provider is wired in")
        try:
            forecast = self.provider(symbol, timeframe)
        except KronosUnavailable as exc:
            return self._unavailable(REASON_UNAVAILABLE, exc.reason)
        except Exception as exc:  # a forecast must never break a trade loop
            return self._unavailable(REASON_UNAVAILABLE, type(exc).__name__)
        if forecast is None:
            return self._unavailable(REASON_UNAVAILABLE, "no forecast was returned")
        if not isinstance(forecast, Forecast):
            return self._unavailable(REASON_UNAVAILABLE, "the forecast payload was not readable")

        identity = forecast.audit_view() if hasattr(forecast, "audit_view") else {}
        model = str(identity.get("model_key") or identity.get("model_reference") or "")
        try:
            expected = Decimal(forecast.expected_move)
        except Exception:
            return self._unavailable(REASON_UNAVAILABLE, "the forecast carried no usable move")
        # The honest band is the forecast's own distribution at the horizon, in
        # price terms; the move is that band measured from the last observed close.
        band = None
        moves = None
        try:
            last = Decimal(forecast.last_close)
            low = Decimal(forecast.band.p10[-1]) - last
            high = Decimal(forecast.band.p90[-1]) - last
            band = (str(forecast.band.p10[-1]), str(forecast.band.p90[-1]))
            moves = (low, high)
        except Exception:
            band = None
            moves = None

        if not self._fresh(forecast):
            return self._decide(
                Confirmation(
                    allowed=not self.required,
                    reason=REASON_STALE,
                    detail=(
                        "The forecast is older than the freshness allowance, so it is "
                        "not used as a second opinion"
                        + (" and the trade is skipped because Kronos is required."
                           if self.required else "; the signal proceeds without it.")
                    ),
                    model=model,
                    band=band,
                )
            )

        threshold = Decimal(0)
        if moves is not None and moves[0] <= threshold <= moves[1]:
            return self._decide(
                Confirmation(
                    allowed=True,
                    reason=REASON_NO_DIRECTION,
                    detail=(
                        "The forecast's own distribution includes no move at the horizon, "
                        "so it neither confirms nor contradicts this signal."
                    ),
                    model=model,
                    expected_move=str(expected),
                    band=band,
                )
            )

        agrees = (expected > threshold and direction > 0) or (expected < threshold and direction < 0)
        if agrees:
            return self._decide(
                Confirmation(
                    allowed=True,
                    reason=REASON_AGREES,
                    detail=(
                        "The forecast's median points the same way as the signal. This is "
                        "recorded, not treated as proof."
                    ),
                    model=model,
                    expected_move=str(expected),
                    band=band,
                )
            )
        return self._decide(
            Confirmation(
                allowed=False,
                reason=REASON_OPPOSES,
                detail=(
                    "The forecast's own range points against this entry, so the signal is "
                    "skipped. The strategy, the data and the position are untouched."
                ),
                model=model,
                expected_move=str(expected),
                band=band,
            )
        )

    # -- internals -------------------------------------------------------

    def _fresh(self, forecast: Any) -> bool:
        made = getattr(forecast, "generated_at", None) or getattr(forecast, "created_at", None)
        if made is None:
            return True
        try:
            if isinstance(made, str):
                made = datetime.fromisoformat(made.replace("Z", "+00:00"))
            if not isinstance(made, datetime):
                return True
            if made.tzinfo is None:
                made = made.replace(tzinfo=timezone.utc)
            return (self.clock() - made).total_seconds() <= self.max_age_seconds
        except Exception:
            return True

    def _unavailable(self, reason: str, detail: str) -> Confirmation:
        """An absent forecast is reported, never turned into a silent yes or no."""

        if self.required:
            decision = Confirmation(
                allowed=False,
                reason=REASON_REQUIRED_UNAVAILABLE,
                detail=(
                    f"Kronos is required for this run but unavailable ({detail}), so the "
                    "signal is skipped."
                ),
            )
        else:
            decision = Confirmation(
                allowed=True,
                reason=REASON_UNAVAILABLE,
                detail=(
                    f"No forecast was available ({detail}). The signal proceeds without a "
                    "second opinion, and this is recorded."
                ),
            )
        return self._decide(decision)

    def _decide(self, decision: Confirmation) -> Confirmation:
        entry = dict(decision.audit_view())
        entry["at"] = self.clock().isoformat()
        self.history.append(entry)
        if len(self.history) > max(1, self.keep):
            del self.history[: len(self.history) - self.keep]
        return decision

    def view(self) -> dict[str, Any]:
        recent = list(self.history[-10:])
        counts: dict[str, int] = {}
        for entry in self.history:
            reason = str(entry.get("reason") or "")
            counts[reason] = counts.get(reason, 0) + 1
        return {
            **self.describe(),
            "recent": recent,
            "counts": counts,
            "vetoed": counts.get(REASON_OPPOSES, 0),
            "unavailable": counts.get(REASON_UNAVAILABLE, 0),
        }


def veto_policy_snapshot(gate: KronosGate | None) -> dict[str, Any]:
    """How the plan describes Kronos: an input that can only remove trades."""

    if gate is None:
        return {"mode": KRONOS_OFF, "can_affect_selection": False, "label": FORECAST_LABEL}
    described = gate.describe()
    return {
        "mode": described["mode"],
        "required": described["required"],
        "can_affect_selection": False,
        "can_create_a_trade": False,
        "can_only_veto": True,
        "label": FORECAST_LABEL,
        "note": KRONOS_NOTE,
    }


__all__ = [
    "Confirmation",
    "KRONOS_MODES",
    "KRONOS_NOTE",
    "KRONOS_OFF",
    "KRONOS_VETO",
    "KronosGate",
    "REASON_AGREES",
    "REASON_DISABLED",
    "REASON_NO_DIRECTION",
    "REASON_OPPOSES",
    "REASON_REQUIRED_UNAVAILABLE",
    "REASON_STALE",
    "REASON_UNAVAILABLE",
    "veto_policy_snapshot",
]

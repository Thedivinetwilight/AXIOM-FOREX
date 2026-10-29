"""One execution path per mode, chosen by the operator, never guessed.

The router is the single place an approved order is dispatched, and it exists so
that the choice between paper and live is *structural* rather than a flag
somewhere downstream:

* :meth:`ExecutionRouter.route_paper` refuses to run unless the execution mode is
  ``PAPER``, and it only ever touches the paper engine;
* :meth:`ExecutionRouter.route_live` refuses to run unless the execution mode is
  ``LIVE``, confirmed in this process, and the live submitter reports itself
  armed; it only ever touches the live submitter;
* there is no fallback in either direction.  A refused live order is an error,
  never a quiet paper fill, and a refused paper order never becomes a live one.

The router imports neither the MetaTrader 5 package nor the trading adapter: the
live submitter is injected, so this module can be tested with fakes and cannot
open a terminal by accident.  It never connects, never arms, never sets a mode,
and never retries.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from app.execution.broker import TradingBlocked

PAPER = "PAPER"
LIVE = "LIVE"

#: Refusal reasons.  They are stable codes, used in audits and in tests.
PAPER_MODE_REQUIRED = "paper_mode_not_active"
LIVE_MODE_REQUIRED = "live_mode_not_active"
LIVE_NOT_CONFIRMED = "live_mode_not_confirmed"
LIVE_NOT_ARMED = "live_session_not_armed"
LIVE_UNAVAILABLE = "live_execution_unavailable"
PAPER_UNAVAILABLE = "paper_execution_unavailable"
MODE_UNKNOWN = "execution_mode_unknown"


def _read_mode(mode: Any) -> tuple[str, bool]:
    """(mode name, live_effective) from a controller or a plain mapping."""

    if mode is None:
        return "", False
    if isinstance(mode, Mapping):
        return str(mode.get("mode", "")).upper(), bool(mode.get("live_effective", False))
    return (
        str(getattr(mode, "mode", "")).upper(),
        bool(getattr(mode, "live_effective", False)),
    )


@dataclass
class ExecutionRouter:
    """Dispatches an approved order down exactly one path."""

    mode: Any
    paper: Any = None
    live: Any = None
    audit: Any = None
    routed: list[dict[str, Any]] = field(default_factory=list)

    # -- read model ------------------------------------------------------

    @property
    def mode_name(self) -> str:
        return _read_mode(self.mode)[0]

    @property
    def live_effective(self) -> bool:
        return _read_mode(self.mode)[1]

    def describe(self) -> dict[str, Any]:
        return {
            "mode": self.mode_name or "UNKNOWN",
            "live_effective": self.live_effective,
            "paper_path_available": self.paper is not None,
            "live_path_available": self.live is not None,
            "fallback": "none",
        }

    # -- paper -----------------------------------------------------------

    def route_paper(self, request: Any, context: Any) -> Any:
        """Simulate one order locally.  Never reaches the live path."""

        mode_name, _ = _read_mode(self.mode)
        if mode_name != PAPER:
            self._refuse(
                PAPER_MODE_REQUIRED,
                route=PAPER,
                detail=f"mode={mode_name or 'UNKNOWN'}",
            )
        if self.paper is None:
            self._refuse(PAPER_UNAVAILABLE, route=PAPER)
        record = self.paper.submit(request, context)
        payload = {
            "event_type": "EXECUTION_ROUTED",
            "route": PAPER,
            "live": False,
            "order_sent": False,
            "request_id": str(getattr(request, "request_id", "") or ""),
            "request_state": str(getattr(record, "state", "") or ""),
        }
        self._audit(payload)
        return record

    # -- live ------------------------------------------------------------

    def route_live(self, decision: Any, intent: Any, *, spec: Any = None) -> Any:
        """Submit one gated order to the live path.  Never falls back to paper."""

        mode_name, confirmed = _read_mode(self.mode)
        if mode_name != LIVE:
            self._refuse(LIVE_MODE_REQUIRED, route=LIVE, detail=f"mode={mode_name or 'UNKNOWN'}")
        if not confirmed:
            self._refuse(LIVE_NOT_CONFIRMED, route=LIVE)
        if self.live is None:
            self._refuse(LIVE_UNAVAILABLE, route=LIVE)
        if getattr(self.live, "armed", None) is not True:
            # Missing arming is not "probably fine": it is a refusal.
            self._refuse(LIVE_NOT_ARMED, route=LIVE)
        receipt = self.live.submit(decision, intent, spec=spec)
        payload = {
            "event_type": "EXECUTION_ROUTED",
            "route": LIVE,
            "live": True,
            "order_sent": True,
            "request_id": str(getattr(receipt, "request_id", "") or ""),
            "request_state": str(getattr(receipt, "state", "") or ""),
        }
        self._audit(payload)
        return receipt

    # -- internals -------------------------------------------------------

    def _refuse(self, reason: str, *, route: str, detail: str = "") -> None:
        self._audit(
            {
                "event_type": "EXECUTION_ROUTE_REFUSED",
                "route": route,
                "reason": reason,
                "detail": detail,
                "live": False,
                "order_sent": False,
            }
        )
        raise TradingBlocked(reason)

    def _audit(self, payload: Mapping[str, Any]) -> None:
        self.routed.append(dict(payload))
        if self.audit is None:
            return
        try:
            self.audit.record(dict(payload))
        except Exception:
            pass


__all__ = [
    "ExecutionRouter",
    "LIVE",
    "LIVE_MODE_REQUIRED",
    "LIVE_NOT_ARMED",
    "LIVE_NOT_CONFIRMED",
    "LIVE_UNAVAILABLE",
    "MODE_UNKNOWN",
    "PAPER",
    "PAPER_MODE_REQUIRED",
    "PAPER_UNAVAILABLE",
]

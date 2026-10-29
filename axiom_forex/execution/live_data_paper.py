"""Paper trading on real market data: live prices in, simulated fills out.

Paper mode in AXIOM is not a toy feed.  It reads ticks from the same terminal the
live path would use and simulates the *fill* locally:

* prices come from the selected MetaTrader 5 terminal (:class:`TerminalMarketData`),
  so a paper decision sees the broker's actual bid/ask;
* every price is checked against the configured :class:`QuoteFreshness` policy
  before it is used -- a stale, crossed, incomplete, or future quote is refused
  rather than quietly filled at a made-up price;
* the order itself goes to the paper engine, which re-runs the pre-trade gate and
  writes a simulated receipt with ``live=False``.

The object graph makes the promise structural: :class:`PaperTradingOnLiveData`
holds a *data* terminal and a paper engine.  It has no order-submitting surface
to call, so ``order_send`` cannot happen here even by mistake -- and the audit
event it writes says ``order_sent: False`` for every fill.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable, Mapping

from app.common.types import OrderSide
from app.data.models import ForexQuote, QuoteFreshness
from app.execution.broker import TradingBlocked

PAPER = "PAPER"

#: Refusal reasons.
MARKET_DATA_UNAVAILABLE = "market_data_unavailable"
SYMBOL_UNRESOLVED = "symbol_unresolved"
SPEC_REQUIRED = "spec_required"
QUOTE_STALE = "stale_quote"
QUOTE_PAIR_MISMATCH = "quote_pair_mismatch"
PAPER_MODE_REQUIRED = "paper_requires_paper_mode"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class MarketDataRefused(Exception):
    """A price could not be read, or could not be trusted."""

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(reason if not detail else f"{reason}: {detail}")

    def audit_view(self) -> dict[str, Any]:
        return {"reason": self.reason, "detail": self.detail}


def _mode_name(mode: Any) -> str:
    if mode is None:
        return ""
    if isinstance(mode, Mapping):
        return str(mode.get("mode", "")).upper()
    return str(getattr(mode, "mode", "")).upper()


@dataclass
class TerminalMarketData:
    """Live quotes from a terminal's *data* surface, with a freshness policy."""

    terminal: Any
    freshness: QuoteFreshness
    clock: Callable[[], datetime] | None = None
    data_class: str = "LIVE_BROKER_TICK"

    def symbol_for(self, spec: Any) -> str:
        resolve = getattr(self.terminal, "resolve_symbol", None)
        if not callable(resolve):
            raise MarketDataRefused(
                SYMBOL_UNRESOLVED, "the terminal cannot resolve broker symbols"
            )
        try:
            return str(resolve(spec))
        except Exception as exc:  # terminal refusals are already worded
            raise MarketDataRefused(
                getattr(exc, "reason", SYMBOL_UNRESOLVED) or SYMBOL_UNRESOLVED,
                getattr(exc, "detail", "") or type(exc).__name__,
            ) from exc

    def quote(self, spec: Any) -> ForexQuote:
        symbol = self.symbol_for(spec)
        try:
            quote = self.terminal.quote(spec, symbol)
        except MarketDataRefused:
            raise
        except Exception as exc:
            raise MarketDataRefused(
                getattr(exc, "reason", MARKET_DATA_UNAVAILABLE) or MARKET_DATA_UNAVAILABLE,
                getattr(exc, "detail", "") or type(exc).__name__,
            ) from exc
        if not isinstance(quote, ForexQuote):
            raise MarketDataRefused(MARKET_DATA_UNAVAILABLE, "quote_type_unexpected")
        return quote

    def fresh_quote(
        self, spec: Any, *, evaluated_at: datetime | None = None
    ) -> tuple[ForexQuote, Any]:
        quote = self.quote(spec)
        moment = evaluated_at or (self.clock or _utc_now)()
        return quote, self.freshness.check(quote, moment)

    def executable_price(self, spec: Any, side: OrderSide | str) -> Decimal:
        quote, decision = self.fresh_quote(spec)
        if not decision.ok:
            raise MarketDataRefused(QUOTE_STALE, decision.reason.value)
        return quote.executable_price(side)


@dataclass
class PaperTradingOnLiveData:
    """Paper execution priced from live terminal quotes."""

    engine: Any
    market: TerminalMarketData
    mode: Any
    audit: Any = None

    def _record(self, payload: Mapping[str, Any]) -> None:
        if self.audit is None:
            return
        try:
            self.audit.record(dict(payload))
        except Exception:
            pass

    def submit(self, request: Any, context: Any, *, spec: Any = None) -> Any:
        """Gate, price, and simulate one order.  Sends nothing, ever."""

        mode_name = _mode_name(self.mode)
        if mode_name != PAPER:
            self._record(
                {
                    "event_type": "PAPER_ON_LIVE_DATA_REFUSED",
                    "reason": PAPER_MODE_REQUIRED,
                    "mode": mode_name or "UNKNOWN",
                    "live": False,
                    "order_sent": False,
                }
            )
            raise TradingBlocked(PAPER_MODE_REQUIRED)
        if spec is None:
            self._record(
                {
                    "event_type": "PAPER_ON_LIVE_DATA_REFUSED",
                    "reason": SPEC_REQUIRED,
                    "live": False,
                    "order_sent": False,
                }
            )
            raise TradingBlocked(SPEC_REQUIRED)
        try:
            quote, decision = self.market.fresh_quote(spec)
        except MarketDataRefused as exc:
            self._record(
                {
                    "event_type": "PAPER_ON_LIVE_DATA_REFUSED",
                    "reason": exc.reason,
                    "detail": exc.detail,
                    "live": False,
                    "order_sent": False,
                }
            )
            raise TradingBlocked(exc.reason) from exc
        if not decision.ok:
            self._record(
                {
                    "event_type": "PAPER_ON_LIVE_DATA_REFUSED",
                    "reason": QUOTE_STALE,
                    "detail": decision.reason.value,
                    "age_seconds": str(decision.age_seconds),
                    "live": False,
                    "order_sent": False,
                }
            )
            raise TradingBlocked(QUOTE_STALE)
        request_pair = str(getattr(request, "pair", "") or "").replace("/", "").upper()
        quote_pair = str(quote.pair or "").replace("/", "").upper()
        if request_pair and quote_pair and request_pair != quote_pair:
            self._record(
                {
                    "event_type": "PAPER_ON_LIVE_DATA_REFUSED",
                    "reason": QUOTE_PAIR_MISMATCH,
                    "live": False,
                    "order_sent": False,
                }
            )
            raise TradingBlocked(QUOTE_PAIR_MISMATCH)

        record = self.engine.submit(request, context)
        self._record(
            {
                "event_type": "PAPER_ORDER_ON_LIVE_DATA",
                "request_id": str(getattr(request, "request_id", "") or ""),
                "simulation_id": str(getattr(record, "simulation_id", "") or ""),
                "state": str(getattr(record, "state", "") or ""),
                "quote_id": str(getattr(quote, "quote_id", "") or ""),
                "quote_timestamp": str(getattr(quote, "timestamp", "") or ""),
                "quote_age_seconds": str(decision.age_seconds),
                "price_basis": "BID_ASK",
                "data_class": self.market.data_class,
                "execution": "SIMULATED",
                "live": False,
                "order_sent": False,
            }
        )
        return record


__all__ = [
    "MARKET_DATA_UNAVAILABLE",
    "MarketDataRefused",
    "PAPER_MODE_REQUIRED",
    "PaperTradingOnLiveData",
    "QUOTE_PAIR_MISMATCH",
    "QUOTE_STALE",
    "SPEC_REQUIRED",
    "SYMBOL_UNRESOLVED",
    "TerminalMarketData",
]

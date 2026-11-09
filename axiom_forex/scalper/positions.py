"""POSITION MANAGER: everything that happens after the fill.

Opening a trade is one decision; keeping it honest is a loop.  This module owns the
loop: break-even, trailing, time exits, partial exits, stop and target detection,
broker-side protection verification, and reconciliation against the venue.

Two rules cannot be overridden by any strategy:

* **Exits are never blocked.**  The guardian may suspend entries; it may not
  suspend this class.  Closing is always available, and every exit records why.
* **An unverified position is reported as unprotected.**  If the venue cannot
  confirm a stop, the position is flagged and the trader suspends entries.  The
  desk does not print "protected" because it asked for protection.

P&L here is *realised position P&L in points and in account currency before
commission*; costs are applied by the accounting layer so the two never get
double-counted.  MAE and MFE are recorded in points against the entry, because a
strategy's average loss and average run are part of what has to be reported.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Any, Iterable, Mapping

from .config import InstrumentSpec
from .execution import BUY, SELL, Fill, ScalperVenue
from .ticks import Tick

#: Action kinds.
MODIFY_STOP = "modify_stop"
CLOSE = "close"
HOLD = "hold"

#: Exit and management reasons.
STOP_HIT = "stop_hit"
TARGET_HIT = "target_hit"
TIME_EXIT = "max_hold_reached"
BREAK_EVEN = "break_even"
TRAIL = "trailing_stop"
MOMENTUM_FLIP = "momentum_flip"
MANUAL_CLOSE = "manual_close"
EMERGENCY_CLOSE = "emergency_close"
PROTECTION_MISSING = "broker_side_protection_missing"
PARTIAL_EXIT = "partial_exit"
STALE_QUOTE_EXIT = "stale_quote_exit"


def _d(value: Any) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


@dataclass(frozen=True)
class ManagedPosition:
    position_id: str
    symbol: str
    side: str
    lots: Decimal
    entry_price: Decimal
    stop_price: Decimal
    target_price: Decimal | None
    opened_at_ms: int
    entry_request_id: str
    point: Decimal
    point_value_per_lot: Decimal | None
    initial_stop_points: Decimal
    initial_target_points: Decimal | None
    break_even_moved: bool = False
    trailing_active: bool = False
    mfe_points: Decimal = Decimal(0)
    mae_points: Decimal = Decimal(0)
    protection_verified: bool = False
    partial_closed_lots: Decimal = Decimal(0)
    last_update_ms: int = 0
    notes: tuple[str, ...] = ()

    @property
    def sign(self) -> int:
        return 1 if self.side == BUY else -1

    def points_at(self, price: Decimal) -> Decimal:
        if self.point <= 0:
            return Decimal(0)
        return (price - self.entry_price) / self.point * self.sign

    def unrealised_points(self, tick: Tick) -> Decimal:
        price = tick.bid if self.side == BUY else tick.ask
        return self.points_at(price)

    def unrealised_account(self, tick: Tick) -> Decimal | None:
        if self.point_value_per_lot is None:
            return None
        return self.unrealised_points(tick) * self.point_value_per_lot * self.lots

    def audit_view(self) -> dict[str, Any]:
        return {
            "position_id": self.position_id,
            "symbol": self.symbol,
            "side": self.side,
            "lots": str(self.lots),
            "entry_price": str(self.entry_price),
            "stop_price": str(self.stop_price),
            "target_price": None if self.target_price is None else str(self.target_price),
            "opened_at_ms": self.opened_at_ms,
            "entry_request_id": self.entry_request_id,
            "initial_stop_points": str(self.initial_stop_points),
            "break_even_moved": self.break_even_moved,
            "trailing_active": self.trailing_active,
            "mfe_points": str(round(self.mfe_points, 2)),
            "mae_points": str(round(self.mae_points, 2)),
            "protection_verified": self.protection_verified,
            "partial_closed_lots": str(self.partial_closed_lots),
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class ClosedTrade:
    position_id: str
    symbol: str
    side: str
    lots: Decimal
    entry_price: Decimal
    exit_price: Decimal
    opened_at_ms: int
    closed_at_ms: int
    reason: str
    gross_points: Decimal
    gross_account: Decimal | None
    mfe_points: Decimal
    mae_points: Decimal
    holding_ms: int
    entry_request_id: str
    strategy_fingerprint: str = ""

    @property
    def won(self) -> bool:
        return self.gross_points > 0

    def audit_view(self) -> dict[str, Any]:
        return {
            "position_id": self.position_id,
            "symbol": self.symbol,
            "side": self.side,
            "lots": str(self.lots),
            "entry_price": str(self.entry_price),
            "exit_price": str(self.exit_price),
            "opened_at_ms": self.opened_at_ms,
            "closed_at_ms": self.closed_at_ms,
            "reason": self.reason,
            "gross_points": str(round(self.gross_points, 3)),
            "gross_account": None if self.gross_account is None else str(round(self.gross_account, 4)),
            "mfe_points": str(round(self.mfe_points, 2)),
            "mae_points": str(round(self.mae_points, 2)),
            "holding_ms": self.holding_ms,
            "entry_request_id": self.entry_request_id,
            "won": self.won,
            "costs_applied_by_accounting": True,
        }


@dataclass(frozen=True)
class ManagementAction:
    kind: str
    position_id: str
    reason: str
    detail: str = ""
    stop_price: Decimal | None = None
    target_price: Decimal | None = None
    exit_price: Decimal | None = None

    def audit_view(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "position_id": self.position_id,
            "reason": self.reason,
            "detail": self.detail,
            "stop_price": None if self.stop_price is None else str(self.stop_price),
            "target_price": None if self.target_price is None else str(self.target_price),
            "exit_price": None if self.exit_price is None else str(self.exit_price),
        }


@dataclass(frozen=True)
class ManagementSettings:
    max_hold_ms: int
    break_even_trigger_points: Decimal
    trail_after_points: Decimal
    trail_distance_points: Decimal
    break_even_buffer_points: Decimal = Decimal("1")
    partial_exit_points: Decimal | None = None
    partial_exit_fraction: Decimal = Decimal("0.5")
    stale_quote_exit_ms: int = 5_000
    #: A trailing stop is only rewritten when it improves by at least this much.
    #: Rewriting it on every tick is a burst of modify requests the broker will not
    #: thank us for, and it makes the journal unreadable.
    trail_step_points: Decimal = Decimal("1")

    @classmethod
    def from_config(cls, config: Any) -> "ManagementSettings":
        return cls(
            max_hold_ms=config.max_hold_ms,
            break_even_trigger_points=config.break_even_trigger_points,
            trail_after_points=config.trail_after_points,
            trail_distance_points=config.trail_distance_points,
            stale_quote_exit_ms=max(config.max_quote_age_ms * 3, 1_000),
        )


class PositionBook:
    """Open positions, their protective orders, and their exits."""

    def __init__(
        self,
        *,
        venue: ScalperVenue,
        settings: ManagementSettings,
        specs: Mapping[str, InstrumentSpec],
    ) -> None:
        self._venue = venue
        self._settings = settings
        self._specs = dict(specs)
        self._open: dict[str, ManagedPosition] = {}
        self._closed: list[ClosedTrade] = []
        self._blocked_exits: list[dict[str, Any]] = []

    # -- views -----------------------------------------------------------

    @property
    def open_positions(self) -> tuple[ManagedPosition, ...]:
        return tuple(self._open.values())

    @property
    def closed(self) -> tuple[ClosedTrade, ...]:
        return tuple(self._closed)

    def get(self, position_id: str) -> ManagedPosition | None:
        return self._open.get(position_id)

    def unrealised(self, ticks: Mapping[str, Tick]) -> Decimal:
        total = Decimal(0)
        for position in self._open.values():
            tick = ticks.get(position.symbol)
            if tick is None:
                continue
            value = position.unrealised_account(tick)
            if value is not None:
                total += value
        return total

    def open_risk(self) -> Decimal:
        total = Decimal(0)
        for position in self._open.values():
            if position.point_value_per_lot is None:
                continue
            distance = abs(position.entry_price - position.stop_price) / position.point
            total += distance * position.point_value_per_lot * position.lots
        return total

    def open_notional(self, spec_contract: Mapping[str, Decimal] | None = None) -> Decimal:
        total = Decimal(0)
        for position in self._open.values():
            spec = self._specs.get(position.symbol)
            if spec is None:
                continue
            conversion = spec.conversion_rate if spec.conversion_rate is not None else Decimal(1)
            total += spec.contract_size * position.entry_price * conversion * position.lots
        return total

    # -- opening ---------------------------------------------------------

    def open_from_fill(
        self,
        *,
        fill: Fill,
        symbol: str,
        side: str,
        stop_price: Decimal,
        target_price: Decimal | None,
        strategy_fingerprint: str,
        now_ms: int,
    ) -> ManagedPosition:
        """Book a filled order.  The symbol is passed in, never inferred from a price."""

        if fill.ticket is None or fill.price is None or fill.filled_lots <= 0:
            raise ValueError("a position needs a ticket, a price and a volume")
        return self._open_position(
            position_id=fill.ticket,
            symbol=symbol,
            side=side,
            lots=fill.filled_lots,
            entry_price=fill.price,
            stop_price=stop_price,
            target_price=target_price,
            now_ms=now_ms,
            request_id=fill.request_id,
            spec=self._specs.get(symbol),
            strategy_fingerprint=strategy_fingerprint,
        )

    def open_position(
        self,
        *,
        position_id: str,
        symbol: str,
        side: str,
        lots: Decimal,
        entry_price: Decimal,
        stop_price: Decimal,
        target_price: Decimal | None,
        now_ms: int,
        request_id: str,
        strategy_fingerprint: str = "",
    ) -> ManagedPosition:
        """Open a position from facts the caller already holds.

        The desk uses this after a fill; tests and the lab use it directly.
        """

        return self._open_position(
            position_id=position_id,
            symbol=symbol,
            side=side,
            lots=lots,
            entry_price=entry_price,
            stop_price=stop_price,
            target_price=target_price,
            now_ms=now_ms,
            request_id=request_id,
            spec=self._specs.get(symbol),
            strategy_fingerprint=strategy_fingerprint,
        )

    def _open_position(
        self,
        *,
        position_id: str,
        symbol: str,
        side: str,
        lots: Decimal,
        entry_price: Decimal,
        stop_price: Decimal,
        target_price: Decimal | None,
        now_ms: int,
        request_id: str,
        spec: InstrumentSpec | None,
        strategy_fingerprint: str,
    ) -> ManagedPosition:
        if side not in {BUY, SELL}:
            raise ValueError(f"invalid side {side!r}")
        if lots <= 0 or entry_price <= 0:
            raise ValueError("a position needs a positive volume and price")
        if (side == BUY and stop_price >= entry_price) or (side == SELL and stop_price <= entry_price):
            raise ValueError("the stop is on the wrong side of the entry")
        point = spec.point if spec is not None else Decimal("0.00001")
        point_value = spec.point_value_per_lot if spec is not None else None
        stop_points = abs(entry_price - stop_price) / point
        target_points = (
            abs(target_price - entry_price) / point if target_price is not None else None
        )
        stop_now, target_now = self._venue.protection(position_id)
        position = ManagedPosition(
            position_id=position_id,
            symbol=symbol,
            side=side,
            lots=lots,
            entry_price=entry_price,
            stop_price=stop_price,
            target_price=target_price,
            opened_at_ms=now_ms,
            entry_request_id=request_id,
            point=point,
            point_value_per_lot=point_value,
            initial_stop_points=stop_points,
            initial_target_points=target_points,
            protection_verified=stop_now is not None,
            last_update_ms=now_ms,
            notes=(
                ()
                if strategy_fingerprint == ""
                else (f"strategy_fingerprint={strategy_fingerprint}",)
            ),
        )
        self._open[position_id] = position
        return position

    # -- the loop --------------------------------------------------------

    def on_tick(
        self,
        *,
        position_id: str,
        tick: Tick,
        now_ms: int,
        momentum_flip: bool = False,
    ) -> tuple[ManagementAction, ...]:
        """Decide what this tick does to one position.  Returns actions, applies them."""

        position = self._open.get(position_id)
        if position is None:
            return ()
        spec = self._specs.get(position.symbol)
        if spec is not None and position.point_value_per_lot is None:
            position = replace(position, point_value_per_lot=spec.point_value_per_lot)
            self._open[position_id] = position

        progress = position.points_at(tick.bid if position.side == BUY else tick.ask)
        favour = max(position.mfe_points, progress)
        adverse = min(position.mae_points, progress)
        position = replace(
            position, mfe_points=favour, mae_points=adverse, last_update_ms=now_ms
        )
        self._open[position_id] = position

        actions: list[ManagementAction] = []

        exit_price = self._stop_touch(position, tick)
        if exit_price is not None:
            actions.append(
                self._close(position, exit_price, now_ms, STOP_HIT, "the market traded through the stop")
            )
            return tuple(actions)

        exit_price = self._target_touch(position, tick)
        if exit_price is not None:
            actions.append(
                self._close(position, exit_price, now_ms, TARGET_HIT, "the target was reached")
            )
            return tuple(actions)

        if now_ms - position.opened_at_ms >= self._settings.max_hold_ms:
            actions.append(
                self._close(
                    position,
                    tick.bid if position.side == BUY else tick.ask,
                    now_ms,
                    TIME_EXIT,
                    f"held {now_ms - position.opened_at_ms} ms of {self._settings.max_hold_ms}",
                )
            )
            return tuple(actions)

        if now_ms - _ms(tick.source_time) > self._settings.stale_quote_exit_ms:
            actions.append(
                self._close(
                    position,
                    tick.bid if position.side == BUY else tick.ask,
                    now_ms,
                    STALE_QUOTE_EXIT,
                    "no fresh quote; the position is closed rather than managed blind",
                )
            )
            return tuple(actions)

        if momentum_flip:
            actions.append(
                self._close(
                    position,
                    tick.bid if position.side == BUY else tick.ask,
                    now_ms,
                    MOMENTUM_FLIP,
                    "the entry condition reversed",
                )
            )
            return tuple(actions)

        if (
            self._settings.partial_exit_points is not None
            and position.partial_closed_lots == 0
            and position.lots > Decimal(0)
            and progress >= self._settings.partial_exit_points
        ):
            fraction = self._settings.partial_exit_fraction
            portion = (position.lots * fraction).quantize(Decimal("0.01"))
            if 0 < portion < position.lots:
                position = replace(
                    position,
                    lots=position.lots - portion,
                    partial_closed_lots=portion,
                )
                self._open[position_id] = position
                actions.append(
                    ManagementAction(
                        kind=PARTIAL_EXIT,
                        position_id=position_id,
                        reason=PARTIAL_EXIT,
                        detail=f"closed {portion} lots at {progress:.1f} points",
                        exit_price=tick.bid if position.side == BUY else tick.ask,
                    )
                )

        stop, reason = self._desired_stop(position, progress)
        if stop is not None and stop != position.stop_price:
            applied = self._venue.modify_protection(
                ticket=position.position_id,
                stop_price=stop,
                target_price=position.target_price,
            )
            if applied:
                self._open[position_id] = replace(
                    position,
                    stop_price=stop,
                    break_even_moved=position.break_even_moved or reason == BREAK_EVEN,
                    trailing_active=position.trailing_active or reason == TRAIL,
                    protection_verified=True,
                )
                actions.append(
                    ManagementAction(
                        kind=MODIFY_STOP,
                        position_id=position_id,
                        reason=reason,
                        detail=f"stop moved to {stop} at {progress:.1f} points of progress",
                        stop_price=stop,
                    )
                )
            else:
                self._open[position_id] = replace(position, protection_verified=False)
                actions.append(
                    ManagementAction(
                        kind=HOLD,
                        position_id=position_id,
                        reason=PROTECTION_MISSING,
                        detail="the venue did not accept the stop modification",
                    )
                )

        if not position.protection_verified:
            actions.append(
                ManagementAction(
                    kind=HOLD,
                    position_id=position_id,
                    reason=PROTECTION_MISSING,
                    detail="the venue has not confirmed a stop order for this position",
                )
            )
        return tuple(actions)

    def _desired_stop(
        self, position: ManagedPosition, progress: Decimal
    ) -> tuple[Decimal | None, str]:
        """Break-even, then a ratcheting trail.  Never widens an existing stop."""

        settings = self._settings
        candidate: Decimal | None = None
        reason = ""
        if progress >= settings.trail_after_points:
            offset = settings.trail_distance_points * position.point
            candidate = (
                position.entry_price + progress * position.point - offset
                if position.side == BUY
                else position.entry_price + progress * position.point + offset
            )
            reason = TRAIL
        elif progress >= settings.break_even_trigger_points and not position.break_even_moved:
            buffer = settings.break_even_buffer_points * position.point
            candidate = (
                position.entry_price + buffer if position.side == BUY else position.entry_price - buffer
            )
            reason = BREAK_EVEN
        if candidate is None:
            return None, ""
        improvement = (
            candidate - position.stop_price
            if position.side == BUY
            else position.stop_price - candidate
        )
        if improvement <= 0:
            return None, ""
        if reason == BREAK_EVEN:
            # The break-even step is taken once and is always worth taking.
            return candidate, reason
        if improvement < settings.trail_step_points * position.point:
            return None, ""
        return candidate, reason

    def _stop_touch(self, position: ManagedPosition, tick: Tick) -> Decimal | None:
        if position.side == BUY:
            if tick.bid <= position.stop_price:
                # Gap through the stop fills at the quote, not at the stop.
                return min(tick.bid, position.stop_price)
        elif tick.ask >= position.stop_price:
            return max(tick.ask, position.stop_price)
        return None

    def _target_touch(self, position: ManagedPosition, tick: Tick) -> Decimal | None:
        if position.target_price is None:
            return None
        if position.side == BUY and tick.bid >= position.target_price:
            return max(tick.bid, position.target_price) if tick.bid > position.target_price else position.target_price
        if position.side == SELL and tick.ask <= position.target_price:
            return min(tick.ask, position.target_price) if tick.ask < position.target_price else position.target_price
        return None

    def _close(
        self,
        position: ManagedPosition,
        exit_price: Decimal,
        now_ms: int,
        reason: str,
        detail: str,
    ) -> ManagementAction:
        self._venue.close(
            ticket=position.position_id,
            price=exit_price,
            spread_points=Decimal(0),
            reason=reason,
        )
        points = (exit_price - position.entry_price) / position.point * position.sign
        gross_account = (
            None
            if position.point_value_per_lot is None
            else points * position.point_value_per_lot * position.lots
        )
        fingerprint = ""
        for note in position.notes:
            if note.startswith("strategy_fingerprint="):
                fingerprint = note.split("=", 1)[1]
        trade = ClosedTrade(
            position_id=position.position_id,
            symbol=position.symbol,
            side=position.side,
            lots=position.lots,
            entry_price=position.entry_price,
            exit_price=exit_price,
            opened_at_ms=position.opened_at_ms,
            closed_at_ms=now_ms,
            reason=reason,
            gross_points=points,
            gross_account=gross_account,
            mfe_points=position.mfe_points,
            mae_points=position.mae_points,
            holding_ms=now_ms - position.opened_at_ms,
            entry_request_id=position.entry_request_id,
            strategy_fingerprint=fingerprint,
        )
        self._closed.append(trade)
        self._open.pop(position.position_id, None)
        return ManagementAction(
            kind=CLOSE,
            position_id=position.position_id,
            reason=reason,
            detail=detail,
            exit_price=exit_price,
        )

    def close_now(
        self, *, position_id: str, tick: Tick, now_ms: int, reason: str = MANUAL_CLOSE
    ) -> ManagementAction | None:
        position = self._open.get(position_id)
        if position is None:
            return None
        price = tick.bid if position.side == BUY else tick.ask
        return self._close(position, price, now_ms, reason, "closed on request")

    def flatten_all(self, *, ticks: Mapping[str, Tick], now_ms: int, reason: str = EMERGENCY_CLOSE) -> list[ManagementAction]:
        """Deliberate operator step.  Positions with no quote are *not* skipped silently."""

        actions: list[ManagementAction] = []
        for position_id, position in list(self._open.items()):
            tick = ticks.get(position.symbol)
            if tick is None:
                self._blocked_exits.append(
                    {
                        "position_id": position_id,
                        "reason": "no_quote_available",
                        "at_ms": now_ms,
                        "detail": "the position stays open and visible; nothing is invented to close it",
                    }
                )
                continue
            action = self.close_now(position_id=position_id, tick=tick, now_ms=now_ms, reason=reason)
            if action is not None:
                actions.append(action)
        return actions

    # -- verification and reconciliation ---------------------------------

    def verify_protection(self) -> dict[str, Any]:
        verified: list[str] = []
        missing: list[str] = []
        for position_id, position in list(self._open.items()):
            stop, target = self._venue.protection(position_id)
            ok = stop is not None
            self._open[position_id] = replace(position, protection_verified=ok)
            (verified if ok else missing).append(position_id)
        return {
            "verified": verified,
            "missing": missing,
            "ok": not missing,
            "note": "verification reads the venue's own protective orders, not the desk's intent",
        }

    def reconcile(self, *, venue_tickets: Iterable[str]) -> dict[str, Any]:
        tickets = set(venue_tickets)
        ours = set(self._open)
        orphan = sorted(tickets - ours)
        vanished = sorted(ours - tickets)
        for position_id in vanished:
            position = self._open.get(position_id)
            if position is None:
                continue
            self._blocked_exits.append(
                {
                    "position_id": position_id,
                    "reason": "vanished_from_venue",
                    "at_ms": 0,
                    "detail": "the venue no longer reports this position; it is flagged, not deleted",
                }
            )
        return {
            "matched": len(ours & tickets),
            "orphan_at_venue": orphan,
            "vanished_from_venue": vanished,
            "ok": not orphan and not vanished,
            "blocked_exits": list(self._blocked_exits),
        }

    def summary(self) -> dict[str, Any]:
        wins = [trade for trade in self._closed if trade.won]
        losses = [trade for trade in self._closed if not trade.won]
        gross = sum((trade.gross_points for trade in self._closed), Decimal(0))
        return {
            "open_positions": len(self._open),
            "closed_trades": len(self._closed),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round(len(wins) / len(self._closed), 4) if self._closed else None,
            "gross_points": str(round(gross, 3)),
            "mean_win_points": (
                str(round(sum((t.gross_points for t in wins), Decimal(0)) / len(wins), 3))
                if wins
                else None
            ),
            "mean_loss_points": (
                str(round(sum((t.gross_points for t in losses), Decimal(0)) / len(losses), 3))
                if losses
                else None
            ),
            "mean_hold_ms": (
                round(sum(trade.holding_ms for trade in self._closed) / len(self._closed), 1)
                if self._closed
                else None
            ),
            "open_risk": str(round(self.open_risk(), 4)),
            "note": "gross: before commission and slippage, which accounting applies once",
        }


def _ms(moment: Any) -> int:
    return int(moment.timestamp() * 1000)


__all__ = [
    "BREAK_EVEN",
    "CLOSE",
    "ClosedTrade",
    "EMERGENCY_CLOSE",
    "HOLD",
    "MODIFY_STOP",
    "MOMENTUM_FLIP",
    "MANUAL_CLOSE",
    "ManagementAction",
    "ManagementSettings",
    "ManagedPosition",
    "PARTIAL_EXIT",
    "PROTECTION_MISSING",
    "PositionBook",
    "STALE_QUOTE_EXIT",
    "STOP_HIT",
    "TARGET_HIT",
    "TIME_EXIT",
    "TRAIL",
]

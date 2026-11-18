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

from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Any, Callable, Iterable, Mapping
from uuid import uuid4

from .config import InstrumentSpec
from .execution import ACCEPTED, BUY, SELL, Fill, ScalperVenue
from .ticks import Tick

#: Action kinds.
MODIFY_STOP = "modify_stop"
MODIFY_PROTECTION = "modify_protection"
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
CLOSE_UNCONFIRMED = "close_outcome_unconfirmed"
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
            "point": str(self.point),
            "point_value_per_lot": (
                None if self.point_value_per_lot is None else str(self.point_value_per_lot)
            ),
            "initial_stop_points": str(self.initial_stop_points),
            "initial_target_points": (
                None if self.initial_target_points is None else str(self.initial_target_points)
            ),
            "break_even_moved": self.break_even_moved,
            "trailing_active": self.trailing_active,
            "mfe_points": str(round(self.mfe_points, 2)),
            "mae_points": str(round(self.mae_points, 2)),
            "protection_verified": self.protection_verified,
            "partial_closed_lots": str(self.partial_closed_lots),
            "last_update_ms": self.last_update_ms,
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
            "strategy_fingerprint": self.strategy_fingerprint,
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
        self._close_blockers: dict[str, str] = {}
        self._pending_closes: dict[str, dict[str, Any]] = {}
        self._protection_pending: dict[str, Decimal] = {}
        self._protection_target_pending: dict[str, Decimal | None] = {}
        self._protection_request_ids: dict[str, str] = {}
        self._close_intent_hook: Callable[[Mapping[str, Any]], bool] | None = None
        self._protection_intent_hook: Callable[[Mapping[str, Any]], bool] | None = None
        self._protection_result_hook: Callable[[Mapping[str, Any]], bool] | None = None

    # -- views -----------------------------------------------------------

    @property
    def open_positions(self) -> tuple[ManagedPosition, ...]:
        return tuple(self._open.values())

    @property
    def closed(self) -> tuple[ClosedTrade, ...]:
        return tuple(self._closed)

    @property
    def close_blockers(self) -> tuple[dict[str, str], ...]:
        return tuple(
            {"position_id": position_id, "reason": reason}
            for position_id, reason in sorted(self._close_blockers.items())
        )

    @property
    def pending_closes(self) -> tuple[dict[str, Any], ...]:
        return tuple(
            {"position_id": position_id, **dict(details)}
            for position_id, details in sorted(self._pending_closes.items())
        )

    def get(self, position_id: str) -> ManagedPosition | None:
        return self._open.get(position_id)

    def set_close_intent_hook(
        self, callback: Callable[[Mapping[str, Any]], bool] | None
    ) -> None:
        """Install a durable pre-submit barrier for live close attempts."""

        self._close_intent_hook = callback

    def set_protection_intent_hook(
        self, callback: Callable[[Mapping[str, Any]], bool] | None
    ) -> None:
        """Persist an exact SL/TP intent before a live venue action can run."""

        self._protection_intent_hook = callback

    def set_protection_result_hook(
        self, callback: Callable[[Mapping[str, Any]], bool] | None
    ) -> None:
        """Persist a read-back-confirmed protection state after broker action."""

        self._protection_result_hook = callback

    def snapshot(self) -> dict[str, Any]:
        """Serialize all state needed to continue managing the same exposure."""

        settings = self._settings
        return {
            "schema": "axiom-scalper-position-book-v1",
            "management_settings": {
                "max_hold_ms": settings.max_hold_ms,
                "break_even_trigger_points": str(settings.break_even_trigger_points),
                "trail_after_points": str(settings.trail_after_points),
                "trail_distance_points": str(settings.trail_distance_points),
                "break_even_buffer_points": str(settings.break_even_buffer_points),
                "partial_exit_points": (
                    None if settings.partial_exit_points is None else str(settings.partial_exit_points)
                ),
                "partial_exit_fraction": str(settings.partial_exit_fraction),
                "stale_quote_exit_ms": settings.stale_quote_exit_ms,
                "trail_step_points": str(settings.trail_step_points),
            },
            "open_positions": [
                {
                    **item.audit_view(),
                    "mfe_points": str(item.mfe_points),
                    "mae_points": str(item.mae_points),
                }
                for item in self._open.values()
            ],
            "closed_trades": [
                {
                    **item.audit_view(),
                    "gross_points": str(item.gross_points),
                    "gross_account": (
                        None if item.gross_account is None else str(item.gross_account)
                    ),
                    "mfe_points": str(item.mfe_points),
                    "mae_points": str(item.mae_points),
                }
                for item in self._closed
            ],
            "close_blockers": list(self.close_blockers),
            "pending_closes": list(self.pending_closes),
            "pending_protection_modifications": [
                {
                    "position_id": position_id,
                    "request_id": self._protection_request_ids.get(position_id, ""),
                    "stop_price": str(stop),
                    "target_price": (
                        None
                        if self._protection_target_pending.get(position_id) is None
                        else str(self._protection_target_pending[position_id])
                    ),
                    "previous_stop_price": str(self._open[position_id].stop_price),
                    "previous_target_price": (
                        None
                        if self._open[position_id].target_price is None
                        else str(self._open[position_id].target_price)
                    ),
                }
                for position_id, stop in sorted(self._protection_pending.items())
            ],
            "blocked_exits": [dict(item) for item in self._blocked_exits],
        }

    def validate_state(self, state: Mapping[str, Any]) -> None:
        """Validate a snapshot against this book's instrument and management terms."""

        probe = PositionBook(
            venue=self._venue,
            settings=self._settings,
            specs=self._specs,
        )
        probe.restore_state(state)

    def restore_state(self, state: Mapping[str, Any]) -> None:
        """Atomically restore a validated full management state.

        Broker verification is deliberately a separate step: restored positions
        start with ``protection_verified=False`` and must pass read-back before
        any new entry can be considered.
        """

        if str(state.get("schema") or "") != "axiom-scalper-position-book-v1":
            raise ValueError("unsupported_position_book_schema")
        settings = self._settings
        expected_settings = {
            "max_hold_ms": settings.max_hold_ms,
            "break_even_trigger_points": str(settings.break_even_trigger_points),
            "trail_after_points": str(settings.trail_after_points),
            "trail_distance_points": str(settings.trail_distance_points),
            "break_even_buffer_points": str(settings.break_even_buffer_points),
            "partial_exit_points": (
                None if settings.partial_exit_points is None else str(settings.partial_exit_points)
            ),
            "partial_exit_fraction": str(settings.partial_exit_fraction),
            "stale_quote_exit_ms": settings.stale_quote_exit_ms,
            "trail_step_points": str(settings.trail_step_points),
        }
        if dict(state.get("management_settings") or {}) != expected_settings:
            raise ValueError("position_management_settings_differ")

        def decimal_value(value: Any, field_name: str, *, optional: bool = False) -> Decimal | None:
            if value is None and optional:
                return None
            try:
                parsed = Decimal(str(value))
            except Exception as exc:
                raise ValueError(f"invalid_{field_name}") from exc
            if not parsed.is_finite():
                raise ValueError(f"invalid_{field_name}")
            return parsed

        def integer_value(value: Any, field_name: str, *, minimum: int = 0) -> int:
            if isinstance(value, bool):
                raise ValueError(f"invalid_{field_name}")
            try:
                parsed = int(value)
            except Exception as exc:
                raise ValueError(f"invalid_{field_name}") from exc
            if parsed < minimum or str(parsed) != str(value):
                raise ValueError(f"invalid_{field_name}")
            return parsed

        open_rows = state.get("open_positions")
        closed_rows = state.get("closed_trades")
        if not isinstance(open_rows, list) or not isinstance(closed_rows, list):
            raise ValueError("position_book_rows_missing")

        restored_open: dict[str, ManagedPosition] = {}
        for row in open_rows:
            if not isinstance(row, Mapping):
                raise ValueError("invalid_open_position_row")
            position_id = str(row.get("position_id") or "")
            symbol = str(row.get("symbol") or "")
            side = str(row.get("side") or "").upper()
            spec = self._specs.get(symbol)
            if not position_id or position_id in restored_open:
                raise ValueError("invalid_or_duplicate_position_id")
            if spec is None or side not in {BUY, SELL}:
                raise ValueError(f"position_instrument_or_side_invalid:{position_id}")
            lots = decimal_value(row.get("lots"), "lots")
            entry_price = decimal_value(row.get("entry_price"), "entry_price")
            stop_price = decimal_value(row.get("stop_price"), "stop_price")
            target_price = decimal_value(row.get("target_price"), "target_price", optional=True)
            point = decimal_value(row.get("point"), "point")
            saved_point_value = decimal_value(
                row.get("point_value_per_lot"), "point_value_per_lot", optional=True
            )
            initial_stop = decimal_value(row.get("initial_stop_points"), "initial_stop_points")
            initial_target = decimal_value(
                row.get("initial_target_points"), "initial_target_points", optional=True
            )
            partial_closed = decimal_value(row.get("partial_closed_lots", "0"), "partial_closed_lots")
            mfe = decimal_value(row.get("mfe_points", "0"), "mfe_points")
            mae = decimal_value(row.get("mae_points", "0"), "mae_points")
            opened_at = integer_value(row.get("opened_at_ms"), "opened_at_ms")
            updated_at = integer_value(row.get("last_update_ms", row.get("opened_at_ms")), "last_update_ms")
            if (
                lots <= 0
                or entry_price <= 0
                or stop_price <= 0
                or point != spec.point
                or saved_point_value != spec.point_value_per_lot
                or initial_stop <= 0
                or partial_closed < 0
                or mfe < 0
                or mae > 0
                or (initial_target is not None and initial_target <= 0)
                or (target_price is not None and target_price <= 0)
            ):
                raise ValueError(f"position_values_invalid:{position_id}")
            if (side == BUY and target_price is not None and target_price <= entry_price) or (
                side == SELL and target_price is not None and target_price >= entry_price
            ):
                raise ValueError(f"position_target_side_invalid:{position_id}")
            request_id = str(row.get("entry_request_id") or "")
            if not request_id:
                raise ValueError(f"position_request_identity_missing:{position_id}")
            break_even_moved = row.get("break_even_moved")
            trailing_active = row.get("trailing_active")
            if not isinstance(break_even_moved, bool) or not isinstance(trailing_active, bool):
                raise ValueError(f"position_management_flags_invalid:{position_id}")
            if lots < spec.volume_min or lots > spec.volume_max or lots % spec.volume_step != 0:
                raise ValueError(f"position_volume_off_broker_grid:{position_id}")
            notes = row.get("notes", [])
            if not isinstance(notes, list) or any(not isinstance(note, str) for note in notes):
                raise ValueError(f"position_notes_invalid:{position_id}")
            restored_open[position_id] = ManagedPosition(
                position_id=position_id,
                symbol=symbol,
                side=side,
                lots=lots,
                entry_price=entry_price,
                stop_price=stop_price,
                target_price=target_price,
                opened_at_ms=opened_at,
                entry_request_id=request_id,
                point=point,
                point_value_per_lot=saved_point_value,
                initial_stop_points=initial_stop,
                initial_target_points=initial_target,
                break_even_moved=break_even_moved,
                trailing_active=trailing_active,
                mfe_points=mfe,
                mae_points=mae,
                protection_verified=False,
                partial_closed_lots=partial_closed,
                last_update_ms=updated_at,
                notes=tuple(notes),
            )

        restored_closed: list[ClosedTrade] = []
        for row in closed_rows:
            if not isinstance(row, Mapping):
                raise ValueError("invalid_closed_trade_row")
            position_id = str(row.get("position_id") or "")
            symbol = str(row.get("symbol") or "")
            side = str(row.get("side") or "").upper()
            if not position_id or not symbol or side not in {BUY, SELL}:
                raise ValueError("closed_trade_identity_invalid")
            lots = decimal_value(row.get("lots"), "closed_lots")
            entry = decimal_value(row.get("entry_price"), "closed_entry_price")
            exit_price = decimal_value(row.get("exit_price"), "closed_exit_price")
            gross_points = decimal_value(row.get("gross_points"), "gross_points")
            gross_account = decimal_value(row.get("gross_account"), "gross_account", optional=True)
            mfe_points = decimal_value(row.get("mfe_points", "0"), "closed_mfe_points")
            mae_points = decimal_value(row.get("mae_points", "0"), "closed_mae_points")
            opened_at = integer_value(row.get("opened_at_ms"), "closed_opened_at_ms")
            closed_at = integer_value(row.get("closed_at_ms"), "closed_at_ms")
            holding = integer_value(row.get("holding_ms"), "holding_ms")
            if lots <= 0 or entry <= 0 or exit_price <= 0 or closed_at < opened_at:
                raise ValueError("closed_trade_values_invalid")
            restored_closed.append(
                ClosedTrade(
                    position_id=position_id,
                    symbol=symbol,
                    side=side,
                    lots=lots,
                    entry_price=entry,
                    exit_price=exit_price,
                    opened_at_ms=opened_at,
                    closed_at_ms=closed_at,
                    reason=str(row.get("reason") or ""),
                    gross_points=gross_points,
                    gross_account=gross_account,
                    mfe_points=mfe_points,
                    mae_points=mae_points,
                    holding_ms=holding,
                    entry_request_id=str(row.get("entry_request_id") or ""),
                    strategy_fingerprint=str(row.get("strategy_fingerprint") or ""),
                )
            )

        close_blockers: dict[str, str] = {}
        blocker_rows = state.get("close_blockers")
        if not isinstance(blocker_rows, list):
            raise ValueError("close_blockers_missing")
        for row in blocker_rows:
            if not isinstance(row, Mapping):
                raise ValueError("invalid_close_blocker")
            position_id = str(row.get("position_id") or "")
            reason = str(row.get("reason") or "")
            if position_id not in restored_open or not reason or position_id in close_blockers:
                raise ValueError("close_blocker_identity_invalid")
            close_blockers[position_id] = reason

        pending_closes: dict[str, dict[str, Any]] = {}
        pending_rows = state.get("pending_closes")
        if not isinstance(pending_rows, list):
            raise ValueError("pending_closes_missing")
        for row in pending_rows:
            if not isinstance(row, Mapping):
                raise ValueError("invalid_pending_close")
            position_id = str(row.get("position_id") or "")
            request_id = str(row.get("request_id") or "")
            if (position_id not in restored_open or position_id in pending_closes
                    or not request_id or len(request_id) > 128):
                raise ValueError("pending_close_identity_invalid")
            requested = decimal_value(row.get("requested_lots"), "pending_close_quantity")
            if requested <= 0 or requested > restored_open[position_id].lots:
                raise ValueError("pending_close_quantity_invalid")
            pending_closes[position_id] = {
                key: value for key, value in row.items() if key != "position_id"
            }
            pending_closes[position_id]["request_id"] = request_id

        pending_protection: dict[str, Decimal] = {}
        pending_protection_targets: dict[str, Decimal | None] = {}
        pending_protection_requests: dict[str, str] = {}
        protection_rows = state.get("pending_protection_modifications")
        if not isinstance(protection_rows, list):
            raise ValueError("pending_protection_modifications_missing")
        for row in protection_rows:
            if not isinstance(row, Mapping):
                raise ValueError("invalid_pending_protection")
            position_id = str(row.get("position_id") or "")
            request_id = str(row.get("request_id") or "")
            position = restored_open.get(position_id)
            if not {"stop_price", "target_price", "previous_stop_price", "previous_target_price"}.issubset(row):
                raise ValueError("pending_protection_terms_incomplete")
            stop = decimal_value(row.get("stop_price"), "pending_protection_stop")
            target_raw = (
                row.get("target_price")
                if "target_price" in row
                else (None if position is None or position.target_price is None else str(position.target_price))
            )
            target = decimal_value(
                target_raw, "pending_protection_target", optional=True
            )
            previous_stop = decimal_value(
                row.get("previous_stop_price", None if position is None else position.stop_price),
                "previous_protection_stop",
            )
            previous_target_raw = (
                row.get("previous_target_price")
                if "previous_target_price" in row
                else (None if position is None or position.target_price is None else str(position.target_price))
            )
            previous_target = decimal_value(
                previous_target_raw, "previous_protection_target", optional=True
            )
            if (
                position is None
                or not request_id
                or len(request_id) > 128
                or stop <= 0
                or (target is not None and target <= 0)
                or (
                    target is not None
                    and (
                        (position.side == BUY and target <= position.entry_price)
                        or (position.side == SELL and target >= position.entry_price)
                    )
                )
                or previous_stop != position.stop_price
                or previous_target != position.target_price
                or position_id in pending_protection
            ):
                raise ValueError("pending_protection_identity_invalid")
            pending_protection[position_id] = stop
            pending_protection_targets[position_id] = target
            pending_protection_requests[position_id] = request_id

        blocked = state.get("blocked_exits")
        if not isinstance(blocked, list) or any(not isinstance(item, Mapping) for item in blocked):
            raise ValueError("blocked_exits_invalid")
        restored_blocked = [dict(item) for item in blocked]

        # Commit only after every row has passed validation; a failed restore
        # cannot leave half of a run adopted in memory.
        self._open = restored_open
        self._closed = restored_closed
        self._close_blockers = close_blockers
        self._pending_closes = pending_closes
        self._protection_pending = pending_protection
        self._protection_target_pending = pending_protection_targets
        self._protection_request_ids = pending_protection_requests
        self._blocked_exits = restored_blocked

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

    @staticmethod
    def _stop_matches(
        actual: Any, expected: Decimal, point: Decimal, side: str | None = None
    ) -> bool:
        """Require a matching broker stop that is not looser than requested.

        A one-point read-back tolerance allows broker price normalization, but
        the tolerance must never bless a stop that increases risk: for a BUY the
        observed stop may not be below the requested price; for a SELL it may
        not be above it.
        """

        if actual is None or not point.is_finite() or point <= 0:
            return False
        try:
            observed = Decimal(str(actual))
        except Exception:
            return False
        if not observed.is_finite() or observed <= 0 or not expected.is_finite() or expected <= 0:
            return False
        if abs(observed - expected) > point:
            return False
        if side == BUY and observed < expected:
            return False
        if side == SELL and observed > expected:
            return False
        return True

    @staticmethod
    def _target_matches(
        actual: Any, expected: Decimal | None, point: Decimal
    ) -> bool:
        if not point.is_finite() or point <= 0:
            return False
        if actual is None:
            return expected is None
        try:
            observed = Decimal(str(actual))
        except Exception:
            return False
        if not observed.is_finite():
            return False
        if observed == 0:
            return expected is None
        if observed < 0:
            return False
        return (
            expected is not None
            and expected.is_finite()
            and expected > 0
            and abs(observed - expected) <= point
        )

    @classmethod
    def _protection_matches(
        cls,
        actual_stop: Any,
        actual_target: Any,
        *,
        expected_stop: Decimal,
        expected_target: Decimal | None,
        point: Decimal,
        side: str,
    ) -> bool:
        return cls._stop_matches(
            actual_stop, expected_stop, point, side
        ) and cls._target_matches(actual_target, expected_target, point)

    def modify_protection(
        self,
        *,
        position_id: str,
        stop_price: Decimal,
        target_price: Decimal | None,
        now_ms: int,
        reason: str = "protection_update",
        request_id: str | None = None,
    ) -> ManagementAction:
        """Request one durable SL/TP change and confirm only by venue read-back.

        A pending identity is never resent from a later tick.  Restart recovery
        compares this exact stop/target pair against the same broker ticket.
        """

        position = self._open.get(str(position_id))
        if position is None:
            return ManagementAction(
                kind=HOLD,
                position_id=str(position_id),
                reason=PROTECTION_MISSING,
                detail="the managed position does not exist",
            )
        original_position = position
        position_id = position.position_id
        if position_id in self._protection_pending:
            return ManagementAction(
                kind=HOLD,
                position_id=position_id,
                reason=PROTECTION_MISSING,
                detail="an earlier protection request is pending reconciliation; nothing was resent",
                stop_price=self._protection_pending[position_id],
                target_price=self._protection_target_pending.get(position_id),
            )
        if position_id in self._pending_closes:
            return ManagementAction(
                kind=HOLD,
                position_id=position_id,
                reason=CLOSE_UNCONFIRMED,
                detail="a close request is pending; protection was not modified",
            )

        try:
            requested_stop = _d(stop_price)
            requested_target = None if target_price is None else _d(target_price)
        except Exception:
            return ManagementAction(
                kind=HOLD,
                position_id=position_id,
                reason=PROTECTION_MISSING,
                detail="the requested stop or target is not a valid decimal price",
            )
        if (
            not requested_stop.is_finite()
            or requested_stop <= 0
            or (requested_target is not None and (
                not requested_target.is_finite() or requested_target <= 0
            ))
        ):
            return ManagementAction(
                kind=HOLD,
                position_id=position_id,
                reason=PROTECTION_MISSING,
                detail="the requested stop or target must be a positive finite price",
            )
        if (
            (
                position.side == BUY
                and requested_stop < position.stop_price
            )
            or (
                position.side == SELL
                and requested_stop > position.stop_price
            )
            or (
                requested_target is not None
                and (
                    (position.side == BUY and requested_target <= position.entry_price)
                    or (position.side == SELL and requested_target >= position.entry_price)
                )
            )
        ):
            return ManagementAction(
                kind=HOLD,
                position_id=position_id,
                reason=PROTECTION_MISSING,
                detail="the requested stop would widen risk or the target is on the wrong side of entry",
                stop_price=requested_stop,
                target_price=requested_target,
            )
        if (
            requested_stop == position.stop_price
            and requested_target == position.target_price
            and position.protection_verified
        ):
            return ManagementAction(
                kind=HOLD,
                position_id=position_id,
                reason="protection_unchanged",
                detail="the managed stop and target are already confirmed",
                stop_price=requested_stop,
                target_price=requested_target,
            )

        request_id = str(request_id or f"protection-{uuid4().hex}").strip()
        if not request_id or len(request_id) > 128:
            return ManagementAction(
                kind=HOLD,
                position_id=position_id,
                reason=PROTECTION_MISSING,
                detail="the protection request identity is invalid",
            )
        self._protection_pending[position_id] = requested_stop
        self._protection_target_pending[position_id] = requested_target
        self._protection_request_ids[position_id] = request_id
        position = replace(
            position, protection_verified=False, last_update_ms=int(now_ms)
        )
        self._open[position_id] = position
        intent = {
            "position_id": position_id,
            "request_id": request_id,
            "stop_price": str(requested_stop),
            "target_price": (
                None if requested_target is None else str(requested_target)
            ),
            "previous_stop_price": str(position.stop_price),
            "previous_target_price": (
                None if position.target_price is None else str(position.target_price)
            ),
            "requested_at_ms": int(now_ms),
            "reason": str(reason or "protection_update"),
        }
        durable = self._protection_intent_hook
        result_hook = self._protection_result_hook
        live_venue = str(getattr(self._venue, "mode", "")).upper() == "LIVE"
        persisted = True
        if callable(durable):
            try:
                persisted = bool(durable(intent))
            except Exception:
                persisted = False
        if live_venue and (not callable(durable) or not callable(result_hook)):
            # A live broker action requires both a durable pre-send intent and
            # a durable post-readback result. Without either barrier it is not
            # allowed to cross the venue boundary.
            persisted = False

        if persisted:
            try:
                self._venue.modify_protection(
                    ticket=position_id,
                    stop_price=requested_stop,
                    target_price=requested_target,
                    request_id=request_id,
                )
            except Exception:
                pass

        try:
            observed_stop, observed_target = self._venue.protection(position_id)
        except Exception:
            observed_stop, observed_target = None, None
        verified = persisted and self._protection_matches(
            observed_stop,
            observed_target,
            expected_stop=requested_stop,
            expected_target=requested_target,
            point=position.point,
            side=position.side,
        )
        if verified:
            position = replace(
                position,
                stop_price=requested_stop,
                target_price=requested_target,
                break_even_moved=position.break_even_moved or reason == BREAK_EVEN,
                trailing_active=position.trailing_active or reason == TRAIL,
                protection_verified=True,
                last_update_ms=int(now_ms),
            )
            self._open[position_id] = position
            self._protection_pending.pop(position_id, None)
            self._protection_target_pending.pop(position_id, None)
            self._protection_request_ids.pop(position_id, None)
            if callable(result_hook):
                result_record = {
                    **intent,
                    "state": "CONFIRMED",
                    "verified": True,
                    "broker_stop_price": str(observed_stop),
                    "broker_target_price": (
                        None if requested_target is None else str(observed_target)
                    ),
                }
                try:
                    result_persisted = bool(result_hook(result_record))
                except Exception:
                    result_persisted = False
                if not result_persisted:
                    self._open[position_id] = replace(
                        original_position,
                        protection_verified=False,
                        last_update_ms=int(now_ms),
                    )
                    self._protection_pending[position_id] = requested_stop
                    self._protection_target_pending[position_id] = requested_target
                    self._protection_request_ids[position_id] = request_id
                    return ManagementAction(
                        kind=HOLD,
                        position_id=position_id,
                        reason=PROTECTION_MISSING,
                        detail=(
                            "broker read-back matched, but the confirmed protection result "
                            "could not be durably committed; the exact intent remains pending"
                        ),
                        stop_price=requested_stop,
                        target_price=requested_target,
                    )
            return ManagementAction(
                kind=(MODIFY_STOP if reason in {BREAK_EVEN, TRAIL} else MODIFY_PROTECTION),
                position_id=position_id,
                reason=str(reason or "protection_update"),
                detail=(
                    f"broker read-back confirmed stop {requested_stop} and target "
                    f"{requested_target}"
                ),
                stop_price=requested_stop,
                target_price=requested_target,
            )

        self._open[position_id] = replace(
            position, protection_verified=False, last_update_ms=int(now_ms)
        )
        return ManagementAction(
            kind=HOLD,
            position_id=position_id,
            reason=PROTECTION_MISSING,
            detail=(
                "the requested stop/target change was not durably prepared or was not "
                "confirmed by broker read-back; the position stays open and new entries "
                "must remain blocked"
            ),
            stop_price=requested_stop,
            target_price=requested_target,
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
        try:
            stop_now, target_now = self._venue.protection(position_id)
        except Exception:
            stop_now, target_now = None, None
        protection_verified = self._protection_matches(
            stop_now,
            target_now,
            expected_stop=stop_price,
            expected_target=target_price,
            point=point,
            side=side,
        )
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
            protection_verified=protection_verified,
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
        if position_id in self._pending_closes:
            pending = self._pending_closes[position_id]
            return (
                ManagementAction(
                    kind=HOLD,
                    position_id=position_id,
                    reason=CLOSE_UNCONFIRMED,
                    detail=(
                        "a previous close may have reached the broker; no second close is sent "
                        f"until request {pending.get('request_id') or 'unknown'} is reconciled"
                    ),
                ),
            )
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
                self._close(
                    position, exit_price, now_ms, STOP_HIT,
                    "the market traded through the stop", quote=tick,
                )
            )
            return tuple(actions)

        exit_price = self._target_touch(position, tick)
        if exit_price is not None:
            actions.append(
                self._close(
                    position, exit_price, now_ms, TARGET_HIT,
                    "the target was reached", quote=tick,
                )
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
                    quote=tick,
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
                    "no fresh quote; the manager requests an exit rather than managing blind",
                    quote=tick,
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
                    quote=tick,
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
                exit_price = tick.bid if position.side == BUY else tick.ask
                action = self._close(
                    position,
                    exit_price,
                    now_ms,
                    PARTIAL_EXIT,
                    f"partial exit requested for {portion} lots at {progress:.1f} points",
                    quote=tick,
                    quantity=portion,
                )
                actions.append(action)
                position = self._open.get(position_id)
                if position is None or action.kind != PARTIAL_EXIT:
                    return tuple(actions)

        stop, reason = (
            (None, "")
            if position_id in self._protection_pending
            else self._desired_stop(position, progress)
        )
        if stop is not None and stop != position.stop_price:
            actions.append(
                self.modify_protection(
                    position_id=position_id,
                    stop_price=stop,
                    target_price=position.target_price,
                    now_ms=now_ms,
                    reason=reason,
                )
            )
            position = self._open.get(position_id)
            if position is None:
                return tuple(actions)

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

    @staticmethod
    def _close_attempt(
        *,
        position: ManagedPosition,
        requested: Decimal,
        reason: str,
        now_ms: int,
        price: Decimal,
        quote: Tick | None,
    ) -> dict[str, Any]:
        return {
            "request_id": "",
            "requested_lots": str(requested),
            "reason": reason,
            "requested_at_ms": int(now_ms),
            "requested_price": str(price),
            "quote_id": "" if quote is None else str(quote.tick_id),
            "quote_source_time": "" if quote is None else quote.source_time.isoformat(),
            "quote_bid": "" if quote is None else str(quote.bid),
            "quote_ask": "" if quote is None else str(quote.ask),
        }

    def _close(
        self,
        position: ManagedPosition,
        exit_price: Decimal,
        now_ms: int,
        reason: str,
        detail: str,
        *,
        quote: Tick | None = None,
        quantity: Decimal | None = None,
    ) -> ManagementAction:
        pending = self._pending_closes.get(position.position_id)
        if pending is not None:
            return ManagementAction(
                kind=HOLD,
                position_id=position.position_id,
                reason=CLOSE_UNCONFIRMED,
                detail=(
                    "a prior close is still unresolved; refusing to submit another close "
                    f"for request {pending.get('request_id') or 'unknown'}"
                ),
            )
        requested = position.lots if quantity is None else _d(quantity)
        if not requested.is_finite() or requested <= 0 or requested > position.lots:
            return self._close_refused(
                position, now_ms, "invalid_close_quantity",
                f"requested close quantity {requested} is outside the open position",
            )
        request_id_factory = getattr(self._venue, "close_request_id", None)
        request_id = ""
        if callable(request_id_factory):
            try:
                request_id = str(request_id_factory(position.position_id) or "")
            except Exception as exc:
                return self._close_refused(
                    position,
                    now_ms,
                    "close_request_identity_unavailable",
                    f"a durable close request id could not be prepared ({type(exc).__name__}); nothing was sent",
                )
            if not request_id:
                return self._close_refused(
                    position,
                    now_ms,
                    "close_request_identity_unavailable",
                    "a durable close request id could not be prepared; nothing was sent",
                )
        attempt = self._close_attempt(
            position=position,
            requested=requested,
            reason=reason,
            now_ms=now_ms,
            price=exit_price,
            quote=quote,
        )
        if request_id:
            attempt["request_id"] = request_id
            self._pending_closes[position.position_id] = dict(attempt)
            if self._close_intent_hook is not None:
                try:
                    intent_saved = bool(self._close_intent_hook(dict(attempt)))
                except Exception:
                    intent_saved = False
                if not intent_saved:
                    self._pending_closes.pop(position.position_id, None)
                    return self._close_refused(
                        position,
                        now_ms,
                        "close_intent_not_durable",
                        "the exact close intent could not be durably saved; no broker request was sent",
                    )
        close_args = {
            "ticket": position.position_id,
            "price": exit_price,
            "spread_points": Decimal(0),
            "reason": reason,
            "quote": quote,
            "quantity": requested,
        }
        if request_id:
            close_args["request_id"] = request_id
        try:
            fill = self._venue.close(**close_args)
        except Exception as exc:
            return self._close_refused(
                position, now_ms, "close_execution_uncertain",
                f"the venue close raised {type(exc).__name__}; the position remains open pending reconciliation",
                pending_close=attempt,
            )

        if not isinstance(fill, Fill):
            return self._close_refused(
                position, now_ms, "invalid_close_result",
                "the venue returned an unreadable close result; the position remains open pending reconciliation",
                pending_close=attempt,
            )
        observed_request_id = str(fill.request_id or "")
        if (
            request_id
            and observed_request_id
            and observed_request_id != request_id
            and fill.status not in {"rejected", "blocked"}
        ):
            pending_identity = {
                **attempt,
                "reported_request_id": observed_request_id,
            }
            return self._close_refused(
                position,
                now_ms,
                "close_request_identity_mismatch",
                "the close result named a different request id than the durably prepared intent",
                pending_close=pending_identity,
            )
        if fill.status != ACCEPTED:
            fill_reason = str(getattr(fill, "reason", "") or getattr(fill, "status", "") or "invalid_close_result")
            fill_detail = str(getattr(fill, "detail", "") or "")
            pending_close = (
                {**attempt, "request_id": observed_request_id or request_id}
                if fill.status == "unknown_outcome"
                else None
            )
            if pending_close is None:
                self._pending_closes.pop(position.position_id, None)
            return self._close_refused(
                position, now_ms, fill_reason,
                f"the venue did not confirm the close ({fill_reason}); the position remains open. {fill_detail}".strip(),
                pending_close=pending_close,
            )
        if str(fill.ticket or "") != position.position_id:
            return self._close_refused(
                position, now_ms, "close_position_identity_mismatch",
                "the venue's accepted close does not name the managed broker ticket; the position remains open",
                pending_close={**attempt, "request_id": str(fill.request_id or "")},
            )

        if fill.simulated:
            simulated_lots = _d(fill.filled_lots)
            closed_lots = (
                simulated_lots
                if simulated_lots.is_finite() and simulated_lots > 0
                else requested
            )
            actual_exit = _d(fill.price) if fill.price is not None else _d(exit_price)
        else:
            closed_lots = _d(fill.filled_lots)
            reported_request = _d(fill.requested_lots)
            actual_exit = None if fill.price is None else _d(fill.price)
            if (
                not closed_lots.is_finite()
                or closed_lots <= 0
                or closed_lots > requested
                or not reported_request.is_finite()
                or reported_request != requested
                or closed_lots > reported_request
                or actual_exit is None
            ):
                return self._close_refused(
                    position, now_ms, "close_fill_incomplete",
                    "the live close lacked a matching broker request amount, fill quantity, or price; the position remains open",
                    pending_close={**attempt, "request_id": str(fill.request_id or "")},
                )
        if actual_exit is None or not actual_exit.is_finite() or actual_exit <= 0:
            return self._close_refused(
                position, now_ms, "close_fill_price_invalid",
                "the venue did not provide a positive close price; the position remains open",
                pending_close={**attempt, "request_id": str(fill.request_id or "")},
            )
        if closed_lots > position.lots:
            return self._close_refused(
                position, now_ms, "close_fill_exceeds_position",
                "the broker-reported close quantity exceeds the locally managed position",
                pending_close={**attempt, "request_id": str(fill.request_id or "")},
            )

        return self._record_confirmed_close(
            position=position,
            closed_lots=closed_lots,
            actual_exit=actual_exit,
            now_ms=now_ms,
            reason=reason,
            detail=detail,
        )

    def _record_confirmed_close(
        self,
        *,
        position: ManagedPosition,
        closed_lots: Decimal,
        actual_exit: Decimal,
        now_ms: int,
        reason: str,
        detail: str,
    ) -> ManagementAction:
        points = (actual_exit - position.entry_price) / position.point * position.sign
        gross_account = (
            None
            if position.point_value_per_lot is None
            else points * position.point_value_per_lot * closed_lots
        )
        fingerprint = ""
        for note in position.notes:
            if note.startswith("strategy_fingerprint="):
                fingerprint = note.split("=", 1)[1]
        trade = ClosedTrade(
            position_id=position.position_id,
            symbol=position.symbol,
            side=position.side,
            lots=closed_lots,
            entry_price=position.entry_price,
            exit_price=actual_exit,
            opened_at_ms=position.opened_at_ms,
            closed_at_ms=now_ms,
            reason=reason,
            gross_points=points,
            gross_account=gross_account,
            mfe_points=position.mfe_points,
            mae_points=position.mae_points,
            holding_ms=max(0, now_ms - position.opened_at_ms),
            entry_request_id=position.entry_request_id,
            strategy_fingerprint=fingerprint,
        )
        self._closed.append(trade)
        remaining = position.lots - closed_lots
        self._close_blockers.pop(position.position_id, None)
        self._pending_closes.pop(position.position_id, None)
        if remaining > 0:
            self._open[position.position_id] = replace(
                position,
                lots=remaining,
                partial_closed_lots=position.partial_closed_lots + closed_lots,
                last_update_ms=now_ms,
            )
            return ManagementAction(
                kind=PARTIAL_EXIT,
                position_id=position.position_id,
                reason=reason,
                detail=f"broker confirmed {closed_lots} of {position.lots} lots closed at {actual_exit}; {remaining} remain",
                exit_price=actual_exit,
            )

        self._open.pop(position.position_id, None)
        self._protection_pending.pop(position.position_id, None)
        self._protection_target_pending.pop(position.position_id, None)
        self._protection_request_ids.pop(position.position_id, None)
        return ManagementAction(
            kind=CLOSE,
            position_id=position.position_id,
            reason=reason,
            detail=detail,
            exit_price=actual_exit,
        )

    def _close_refused(
        self,
        position: ManagedPosition,
        now_ms: int,
        reason: str,
        detail: str,
        *,
        pending_close: Mapping[str, Any] | None = None,
    ) -> ManagementAction:
        # A blocker stays visible until a broker-confirmed close or explicit
        # reconciliation.  Ambiguous outcomes also latch the exact request so a
        # later tick cannot send a second close for the same exposure.
        self._close_blockers[position.position_id] = reason
        if pending_close is not None:
            self._pending_closes[position.position_id] = dict(pending_close)
        self._blocked_exits.append(
            {
                "kind": "close",
                "position_id": position.position_id,
                "reason": reason,
                "at_ms": now_ms,
                "detail": detail,
            }
        )
        return ManagementAction(
            kind=HOLD,
            position_id=position.position_id,
            reason=CLOSE_UNCONFIRMED,
            detail=detail,
        )

    def resolve_pending_close(
        self, *, position_id: str, fill: Fill, now_ms: int
    ) -> ManagementAction | None:
        """Apply a queried close only when its durable outcome is exact.

        Position presence/absence alone is not enough to book P&L or a partial
        exit.  The fill must name the original request and position, include the
        original requested size, a positive broker fill, a broker price, and a
        durable booking acknowledgement from the live venue.
        """

        position = self._open.get(position_id)
        pending = self._pending_closes.get(position_id)
        if position is None or pending is None:
            return None
        request_id = str(pending.get("request_id") or "")
        if not request_id or str(fill.request_id or "") != request_id:
            return ManagementAction(
                kind=HOLD,
                position_id=position_id,
                reason=CLOSE_UNCONFIRMED,
                detail="the queried outcome does not match the exact unresolved close request",
            )
        if fill.status == "unknown_outcome":
            return ManagementAction(
                kind=HOLD,
                position_id=position_id,
                reason=CLOSE_UNCONFIRMED,
                detail="the broker outcome is still unknown; the position remains latched",
            )
        if fill.status == "rejected" and not fill.simulated:
            self._pending_closes.pop(position_id, None)
            self._close_blockers[position_id] = "close_resolved_without_fill"
            self._blocked_exits.append(
                {
                    "kind": "close_reconciliation",
                    "position_id": position_id,
                    "request_id": request_id,
                    "reason": "close_resolved_without_fill",
                    "at_ms": now_ms,
                    "detail": "broker confirms no fill; the position remains open and requires a new quoted close",
                }
            )
            return ManagementAction(
                kind=HOLD,
                position_id=position_id,
                reason=CLOSE_UNCONFIRMED,
                detail="the original close is confirmed not filled; use a fresh quote for any new attempt",
            )
        if fill.status != ACCEPTED or fill.simulated:
            return ManagementAction(
                kind=HOLD,
                position_id=position_id,
                reason=CLOSE_UNCONFIRMED,
                detail="the queried result is not a broker-confirmed close fill",
            )
        try:
            requested = _d(pending.get("requested_lots"))
            reported_request = _d(fill.requested_lots)
            closed_lots = _d(fill.filled_lots)
            actual_exit = None if fill.price is None else _d(fill.price)
        except Exception:
            requested = reported_request = closed_lots = Decimal(0)
            actual_exit = None
        if (
            str(fill.ticket or "") != position_id
            or not requested.is_finite()
            or requested <= 0
            or reported_request != requested
            or not closed_lots.is_finite()
            or closed_lots <= 0
            or closed_lots > requested
            or closed_lots > position.lots
            or actual_exit is None
            or not actual_exit.is_finite()
            or actual_exit <= 0
        ):
            return ManagementAction(
                kind=HOLD,
                position_id=position_id,
                reason=CLOSE_UNCONFIRMED,
                detail="the queried close lacks matching identity, quantity, or broker price; reconciliation remains required",
            )
        reason = str(pending.get("reason") or "reconciled_close")
        return self._record_confirmed_close(
            position=position,
            closed_lots=closed_lots,
            actual_exit=actual_exit,
            now_ms=now_ms,
            reason=reason,
            detail="the exact broker close was reconciled and booked",
        )

    def close_now(
        self, *, position_id: str, tick: Tick, now_ms: int, reason: str = MANUAL_CLOSE
    ) -> ManagementAction | None:
        position = self._open.get(position_id)
        if position is None:
            return None
        price = tick.bid if position.side == BUY else tick.ask
        return self._close(position, price, now_ms, reason, "closed on request", quote=tick)

    def flatten_all(self, *, ticks: Mapping[str, Tick], now_ms: int, reason: str = EMERGENCY_CLOSE) -> list[ManagementAction]:
        """Deliberate operator step.  Positions with no quote are *not* skipped silently."""

        actions: list[ManagementAction] = []
        for position_id, position in list(self._open.items()):
            tick = ticks.get(position.symbol)
            if tick is None:
                self._close_blockers[position_id] = "no_quote_available"
                self._blocked_exits.append(
                    {
                        "kind": "close",
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

    def verify_protection(self, *, update: bool = True) -> dict[str, Any]:
        verified: list[str] = []
        missing: list[str] = []
        for position_id, position in list(self._open.items()):
            try:
                stop, target = self._venue.protection(position_id)
            except Exception:
                stop, target = None, None
            has_pending = position_id in self._protection_pending
            expected_stop = self._protection_pending.get(
                position_id, position.stop_price
            )
            expected_target = (
                self._protection_target_pending[position_id]
                if position_id in self._protection_target_pending
                else position.target_price
            )
            ok = self._protection_matches(
                stop,
                target,
                expected_stop=expected_stop,
                expected_target=expected_target,
                point=position.point,
                side=position.side,
            )
            if update and ok and has_pending:
                position = replace(
                    position,
                    stop_price=expected_stop,
                    target_price=expected_target,
                )
                self._protection_pending.pop(position_id, None)
                self._protection_target_pending.pop(position_id, None)
                self._protection_request_ids.pop(position_id, None)
            if update:
                self._open[position_id] = replace(position, protection_verified=ok)
            (verified if ok else missing).append(position_id)
        return {
            "verified": verified,
            "missing": missing,
            "pending_modification": sorted(self._protection_pending),
            "ok": not missing,
            "note": "broker read-back must match the managed stop and target within one broker point",
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
                    "kind": "reconcile",
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
            "ok": not orphan and not vanished and not self._close_blockers,
            "close_blockers": list(self.close_blockers),
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
            "close_blockers": list(self.close_blockers),
            "pending_closes": list(self.pending_closes),
            "pending_protection_modifications": sorted(self._protection_pending),
            "note": "gross: before commission and slippage, which accounting applies once",
        }


def _ms(moment: Any) -> int:
    return int(moment.timestamp() * 1000)


__all__ = [
    "BREAK_EVEN",
    "CLOSE",
    "CLOSE_UNCONFIRMED",
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

"""The one-shot DEMO execution test: one order, a demo account, then stop.

This module exists to answer one question and no other: *does an order AXIOM
builds actually reach the MetaTrader 5 account I am signed into?*  It is an
execution smoke test.  It is **not** a strategy, it is **not** AX-SCALPER, it is
**not** the continuous LIVE Autopilot, and nothing it produces says anything
about whether a strategy makes money.

Why it is a separate object instead of another strategy
------------------------------------------------------
The production live lane refuses to send unless an *approved strategy* armed a
plan.  That rule is exactly right, and it is the rule this module must not bend:
a smoke test has no strategy and no plan, so pretending one is approved would be
the one thing that could turn a test into a bypass.  Instead:

* the request is built honestly as a *demo test* request (``strategy_id`` is the
  test's own identity, never a strategy id, and it can never carry a close
  intent);
* :meth:`~axiom_forex.paper_runner.live.LiveLeg.submit_plan` is called with
  ``demo_test=True``, which **replaces** the strategy-approval requirement with
  strictly *more*: the terminal must positively identify the account as DEMO,
  and every other live guard (mode LIVE and confirmed, adapter connected and
  armed, armed account == current account, trading enabled, symbol spec still
  matching the broker, no unresolved order, ledger == terminal, protective stop
  configured) stays in force, unchanged;
* the order still leaves through the same ``ExecutionRouter`` ->
  ``MT5TradingAdapter.submit`` path, so it still gets the adapter's own
  write-ahead record, duplicate check, volume-grid check, price-drift check,
  margin check and quote-age check.  **There is no second way to reach a
  broker in this codebase, and this module does not add one.**

One request, ever
-----------------
Every submission is wrapped in a durable record that is written *before* the
leg is called and keyed by one idempotency id.  That id is the broker request id
the adapter writes ahead of ``order_send``, so "did this order happen?" is
answered by reading one durable row, on this machine, by the same id:

``IDLE`` -> ``SUBMITTING`` -> ``FILLED`` / ``PARTIAL`` / ``REJECTED`` /
``REFUSED`` / ``UNRESOLVED``

A second submit is refused, not retried.  An ambiguous outcome (the adapter
could not say, or the record is in doubt) is **never re-sent**: :meth:`reconcile`
re-reads the same id from the durable order ledger, the broker's own order list
and the terminal's positions, and reports what it can prove.  The adapter is
disarmed after every outcome, including a refusal, so a test never leaves
trading armed.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Mapping, Sequence

from app.common.types import OrderSide, TradingMode

# -- identities -------------------------------------------------------------

#: The durable snapshot kind every record is stored under.
KIND = "axiom-demo-execution-test"

#: The request's ``strategy_id``.  It names the smoke test, deliberately, so no
#: audit row or report can mistake this order for a strategy's.
STRATEGY_ID = "axiom-demo-execution-test"

#: What the operator must type to send one.  Not a phrase that could be reused
#: from the LIVE arming step.
CONFIRMATION = "SEND ONE DEMO ORDER"

# -- record states ----------------------------------------------------------

IDLE = "IDLE"
SUBMITTING = "SUBMITTING"
FILLED = "FILLED"
PARTIAL = "PARTIAL"
REJECTED = "REJECTED"
REFUSED = "REFUSED"
UNRESOLVED = "UNRESOLVED"
RECONCILED = "RECONCILED"

#: States that end a test.  A record in any of these means "already used".
FINAL_STATES = frozenset({FILLED, PARTIAL, REJECTED, REFUSED, RECONCILED})

# -- refusal reasons --------------------------------------------------------

UNAVAILABLE = "demo_test_unavailable"
ACCOUNT_NOT_DEMO = "demo_account_required"
ACCOUNT_UNKNOWN = "demo_account_mode_unknown"
ACCOUNT_CONFIRMATION_REQUIRED = "demo_account_confirmation_required"
ACCOUNT_CONFIRMATION_MISMATCH = "demo_account_confirmation_mismatch"
SYMBOL_REQUIRED = "demo_symbol_required"
SYMBOL_NOT_VERIFIED = "demo_symbol_not_verified"
SIDE_REQUIRED = "demo_side_required"
VOLUME_REQUIRED = "demo_volume_required"
VOLUME_INVALID = "demo_volume_invalid"
STOP_REQUIRED = "demo_stop_required"
STOP_INVALID = "demo_stop_invalid"
STOP_MISMATCH = "demo_stop_mismatch"
CONFIRMATION_REQUIRED = "demo_confirmation_required"
CONFIRMATION_MISMATCH = "demo_confirmation_mismatch"
QUOTE_UNAVAILABLE = "demo_quote_unavailable"
ALREADY_USED = "demo_test_already_used"
IN_DOUBT = "demo_submission_in_doubt"
STACK_UNAVAILABLE = "demo_platform_unavailable"
NOTHING_TO_RECONCILE = "demo_nothing_to_reconcile"

#: ``LiveLeg`` reasons that mean the broker may already have an order.
_DOUBT_REASONS = frozenset(
    {"unresolved_order_present", "duplicate_request", "demo_submission_in_doubt"}
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class DemoBlocker:
    """One reason this test cannot be sent, and what clears it."""

    reason: str
    message: str
    hint: str = ""

    def audit_view(self) -> dict[str, str]:
        return {"reason": self.reason, "message": self.message, "hint": self.hint}

    def as_dict(self) -> dict[str, str]:
        return self.audit_view()


@dataclass
class DemoPlan:
    """What the live leg is handed: one request, the context it was judged in.

    Deliberately not ``ExecutionPlan.extend``-able: a smoke test never closes a
    position, so ``action`` is fixed and ``position_id`` is empty.
    """

    request: Any
    context: Any
    action: str = "OPEN"
    position_id: str = ""
    quantity: Any = None
    values: dict[str, Any] = field(default_factory=dict)


def _text(value: Any) -> str:
    return str(value if value is not None else "").strip()


def _decimal(value: Any) -> Decimal | None:
    try:
        parsed = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, TypeError):
        return None
    return parsed if parsed.is_finite() else None


def _iso(value: Any) -> str:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    return _text(value)


class DemoExecutionTest:
    """One demo order, built from the broker's own terms, sent at most once."""

    def __init__(
        self,
        *,
        adapter: Any = None,
        mode: Any = None,
        platform_for: Callable[[str], Any] | None = None,
        pairs: Sequence[str] | Callable[[], Sequence[str]] = (),
        repository: Any = None,
        audit_log: Any = None,
        clock: Callable[[], datetime] | None = None,
        ledger_positions: Callable[[], Sequence[Mapping[str, Any]]] | None = None,
    ) -> None:
        self.adapter = adapter
        self.mode = mode
        self.platform_for = platform_for
        self._pairs = pairs
        self.repository = repository
        self.audit_log = audit_log
        self.clock = clock or _utc_now
        self.ledger_positions = ledger_positions
        #: Resolution is not free (it builds a provider platform), and every page
        #: render asks for the terms of every pair.  The platforms are cached per
        #: pair and dropped when the pair list changes, so a saved provider setup
        #: is picked up on the next render rather than served from a stale build.
        self._platforms: dict[str, Any] = {}

    # -- read model ------------------------------------------------------

    @property
    def pairs(self) -> tuple[str, ...]:
        """The symbols this test may name, read fresh each time.

        A callable is accepted because the provider setup can be saved after the
        workstation started; the list is never cached from a stale document.
        """

        source = self._pairs
        try:
            items = source() if callable(source) else source
        except Exception:
            return ()
        return tuple(_text(item) for item in (items or ()) if _text(item))

    @property
    def available(self) -> bool:
        """True only when there is a terminal to ask and a store to write to."""

        return self.adapter is not None and self.platform_for is not None

    def _account(self) -> dict[str, Any]:
        """What the terminal says about the account, in its own words."""

        if self.adapter is None:
            return {
                "identified": False,
                "mode": "UNCONFIRMED",
                "confirmed": False,
                "is_demo": False,
                "fingerprint": "",
                "server": "",
            }
        try:
            status = dict(self.adapter.status())
        except Exception:
            return {
                "identified": False,
                "mode": "UNCONFIRMED",
                "confirmed": False,
                "is_demo": False,
                "fingerprint": "",
                "server": "",
            }
        redacted = dict(status.get("redacted_account") or {})
        return {
            "identified": bool(redacted.get("identified")),
            "mode": _text(status.get("account_mode")) or "UNCONFIRMED",
            "confirmed": bool(status.get("account_mode_confirmed")),
            "is_demo": bool(status.get("account_is_demo")) and bool(
                status.get("account_mode_confirmed")
            ),
            "fingerprint": _text(status.get("account_fingerprint")),
            "server": _text(redacted.get("server")),
            "currency": _text(redacted.get("currency")),
            "connected": bool(status.get("connected")),
            "armed": bool(status.get("armed")),
            "unresolved": list(status.get("unresolved_orders") or []),
            "trading_enabled": bool(status.get("trading_enabled_setting")),
        }

    def _mode_facts(self) -> dict[str, Any]:
        return {
            "name": _text(getattr(self.mode, "mode", "")) or "UNKNOWN",
            "live_effective": bool(getattr(self.mode, "live_effective", False)),
        }

    def _platform(self, pair: str) -> tuple[Any, str]:
        if self.platform_for is None:
            return None, STACK_UNAVAILABLE
        if pair in self._platforms:
            return self._platforms[pair], ""
        try:
            platform = self.platform_for(pair)
        except Exception as exc:
            return None, f"{STACK_UNAVAILABLE}:{type(exc).__name__}"
        if platform is None:
            return None, STACK_UNAVAILABLE
        # A different pair list means a different provider setup: drop everything
        # that was resolved against the old one.
        allowed = set(self.pairs)
        for stale in [key for key in self._platforms if key not in allowed]:
            self._platforms.pop(stale, None)
        self._platforms[pair] = platform
        return platform, ""

    def symbol_options(self) -> list[dict[str, Any]]:
        """Every symbol this test may name, with the broker's own terms.

        The list is the provider setup's pairs, and each one is kept only when
        the terminal publishes a specification for it *and* the broker symbol
        the setup resolved is the one the terminal reports.  A symbol that
        cannot be verified is listed with its reason rather than dropped
        silently, so the operator can see why it is not selectable.
        """

        options: list[dict[str, Any]] = []
        for pair in self.pairs:
            option: dict[str, Any] = {"pair": pair, "verified": False, "reason": ""}
            platform, reason = self._platform(pair)
            if platform is None:
                option["reason"] = reason
                options.append(option)
                continue
            spec = getattr(platform, "spec", None)
            broker_symbol = _text(getattr(platform, "broker_symbol", ""))
            option.update(
                {
                    "broker_symbol": broker_symbol,
                    "digits": int(getattr(spec, "quote_precision", 0) or 0),
                    "pip_size": str(getattr(spec, "pip_size", "") or ""),
                }
            )
            info = None
            try:
                _, info = self.adapter.symbol_info(spec)
            except Exception as exc:
                option["reason"] = f"symbol_info_unavailable:{type(exc).__name__}"
            if info is not None:
                if _text(getattr(info, "name", "")) != broker_symbol:
                    option["reason"] = "broker_symbol_mismatch"
                elif not getattr(info, "tradable", True):
                    option["reason"] = "symbol_not_tradable"
                else:
                    option["verified"] = True
                option.update(
                    {
                        "digits": int(getattr(info, "digits", option["digits"]) or 0),
                        "point": str(getattr(info, "point", "") or ""),
                        "volume_min": str(getattr(info, "volume_min", "") or ""),
                        "volume_max": str(getattr(info, "volume_max", "") or ""),
                        "volume_step": str(getattr(info, "volume_step", "") or ""),
                        "stops_level": int(getattr(info, "stops_level", 0) or 0),
                        "bid": str(getattr(info, "bid", "") or ""),
                        "ask": str(getattr(info, "ask", "") or ""),
                    }
                )
            stop = None
            try:
                stop = dict(self.adapter.stop_requirement(spec))
            except Exception:
                stop = None
            if stop is not None:
                option["stop"] = {
                    "points": int(stop.get("distance_points") or 0),
                    "declared": _text(stop.get("declared")),
                    "valid": bool(stop.get("valid")),
                    "reason": _text(stop.get("reason")),
                }
            options.append(option)
        return options

    def _record_key(self, test_id: str) -> str:
        return f"{KIND}:{test_id}"

    def _load(self, test_id: str) -> dict[str, Any] | None:
        if self.repository is None or not test_id:
            return None
        try:
            state = self.repository.get_snapshot(self._record_key(test_id))
        except Exception:
            return None
        if not isinstance(state, dict) or state.get("kind") != KIND:
            return None
        return state

    def latest(self) -> dict[str, Any]:
        """The most recent record, or an empty dict.  Reads only."""

        if self.repository is None:
            return {}
        try:
            rows = self.repository.list_snapshots(kind=KIND)
        except Exception:
            return {}
        records = [dict(row) for row in rows if isinstance(row, Mapping)]
        if not records:
            return {}
        def _stamp(record: Mapping[str, Any]) -> str:
            payload = record.get("payload")
            if isinstance(payload, Mapping):
                return _text(payload.get("updated_at"))
            return _text(record.get("updated_at"))

        records.sort(key=_stamp)
        newest = records[-1]
        payload = newest.get("payload")
        if isinstance(payload, Mapping):
            return dict(payload)
        if isinstance(newest.get("record"), Mapping):
            return dict(newest["record"])
        return newest

    def _write(self, record: Mapping[str, Any], event_type: str, **fields: Any) -> dict[str, Any]:
        test_id = _text(record.get("test_id"))
        now = self.clock().astimezone(timezone.utc).isoformat()
        updated = dict(record)
        updated["kind"] = KIND
        updated["updated_at"] = now
        event = {"event_type": event_type, "at": now, "test_id": test_id, **fields}
        updated["events"] = (list(updated.get("events") or []) + [event])[-60:]
        if self.repository is None:
            return updated
        with self.repository.transaction():
            self.repository.save_snapshot(self._record_key(test_id), KIND, updated)
            self.repository.record_audit(
                {
                    "event_type": event_type,
                    "at": now,
                    "test_id": test_id,
                    "request_id": _text(updated.get("request_id")),
                    "demo_test": True,
                    **fields,
                }
            )
        if self.audit_log is not None:
            try:
                self.audit_log.record(
                    {"event_type": event_type, "at": now, "test_id": test_id, **fields}
                )
            except Exception:
                pass
        return updated

    def view(self) -> dict[str, Any]:
        """Everything the page draws.  Reads only; sends nothing."""

        account = self._account()
        execution = self._mode_facts()
        record = self.latest()
        blocking: list[DemoBlocker] = list(self._account_blockers(account, execution))
        status = _text(record.get("status")) or IDLE
        if status in FINAL_STATES:
            blocking.append(
                DemoBlocker(
                    ALREADY_USED,
                    "A demo execution test has already run on this machine "
                    f"({status}, request {_text(record.get('request_id')) or 'unknown'}).",
                    "One test per record. Reconcile it, or remove the record "
                    "deliberately if you mean to run a new one.",
                )
            )
        elif status == SUBMITTING or status == UNRESOLVED:
            blocking.append(
                DemoBlocker(
                    IN_DOUBT,
                    "A demo test request is already in flight or in doubt. It is never "
                    "re-sent.",
                    "Reconcile the same request id; nothing new can be sent until the "
                    "durable record and the broker agree.",
                )
            )
        return {
            "available": self.available,
            "account": account,
            "execution": execution,
            "symbols": self.symbol_options(),
            "confirmation": CONFIRMATION,
            "record": {
                "test_id": _text(record.get("test_id")),
                "request_id": _text(record.get("request_id")),
                "status": status,
                "values": dict(record.get("values") or {}),
                "broker": dict(record.get("broker") or {}),
                "reconciliation": dict(record.get("reconciliation") or {}),
                "submitted_at": _text(record.get("submitted_at")),
                "events": list(record.get("events") or [])[-10:],
            },
            "blockers": [item.audit_view() for item in blocking],
            "can_send": self.available and not blocking and account.get("is_demo") is True,
        }

    # -- validation ------------------------------------------------------

    def _stop_preview(
        self, spec: Any, info: Any, side: str, points: int
    ) -> dict[str, Any]:
        """The exact price the broker would hold, or why it cannot be held."""

        from ..brokers.mt5.orders import protective_stop_price

        tick = None
        try:
            tick = self.adapter.quote(spec)
        except Exception:
            tick = None
        reference = None
        if tick is not None:
            reference = getattr(tick, "ask" if side == "BUY" else "bid", None)
        if reference is None:
            reference = getattr(info, "ask" if side == "BUY" else "bid", None)
        if reference is None:
            return {"price": "", "reason": QUOTE_UNAVAILABLE}
        price, reason = protective_stop_price(
            side=side,
            reference=Decimal(str(reference)),
            point=getattr(info, "point", None),
            digits=int(getattr(info, "digits", 0) or 0),
            points=int(points),
            stops_level=int(getattr(info, "stops_level", 0) or 0),
        )
        return {
            "price": "" if price is None else str(price),
            "reference": str(reference),
            "reason": reason,
        }

    def _account_blockers(
        self, account: Mapping[str, Any], execution: Mapping[str, Any]
    ) -> list[DemoBlocker]:
        """The terminal, the mode and the arm — checked before anything is typed."""

        blockers: list[DemoBlocker] = []
        if not self.available:
            return [
                DemoBlocker(
                    UNAVAILABLE,
                    "There is no terminal adapter or provider stack in this window.",
                    "Start the workstation from your settings file.",
                )
            ]
        if not account.get("connected"):
            blockers.append(
                DemoBlocker(
                    "mt5_not_connected",
                    "The MetaTrader 5 terminal is not connected.",
                    "Press Connect and sign in to the terminal that holds your demo account.",
                )
            )
        elif not account.get("confirmed"):
            blockers.append(
                DemoBlocker(
                    ACCOUNT_UNKNOWN,
                    "The terminal did not confirm the account type "
                    f"(reported: {account.get('mode') or 'nothing'}).",
                    "A demo test needs MT5 to say DEMO. Sign in again and reconnect.",
                )
            )
        elif not account.get("is_demo"):
            blockers.append(
                DemoBlocker(
                    ACCOUNT_NOT_DEMO,
                    f"The connected account is {account.get('mode')}, not DEMO. This test "
                    "refuses to send to anything else.",
                    "Sign the terminal in to your demo account. Nothing on this page can "
                    "override that check.",
                )
            )
        if execution.get("name") != "LIVE" or not execution.get("live_effective"):
            blockers.append(
                DemoBlocker(
                    "mode_not_live",
                    f"Axiom's execution mode is {execution.get('name') or 'UNKNOWN'}"
                    + ("" if execution.get("live_effective") else ", and not confirmed")
                    + ". The live order path only runs in confirmed LIVE mode.",
                    "Switch to LIVE on the Trading mode screen and complete its preflight. "
                    "Only your demo account can be chosen here.",
                )
            )
        if not account.get("armed"):
            blockers.append(
                DemoBlocker(
                    "live_not_armed",
                    "Live trading is not armed, so the existing live path would refuse the "
                    "order anyway.",
                    "Arm live trading on the Trading mode screen. It is a separate, "
                    "explicit step after the mode switch, and this page cannot do it.",
                )
            )
        return blockers

    def check(self, values: Mapping[str, Any]) -> dict[str, Any]:
        """Judge the form against the broker's current terms.  Sends nothing."""

        blockers: list[DemoBlocker] = []
        account = self._account()
        execution = self._mode_facts()
        pair = _text(values.get("symbol"))
        side = _text(values.get("side")).upper()
        volume_text = _text(values.get("volume"))
        stop_text = _text(values.get("stop_points"))
        phrase = _text(values.get("confirmation"))
        account_confirmation = _text(values.get("account_confirmation"))

        # 1. The account, the mode and the arm: MT5's own answer, never a guess
        #    and never this workstation's opinion of what it ought to be.
        blockers.extend(self._account_blockers(account, execution))

        # 2. The symbol, from the setup's pairs, cross-checked with the terminal.
        spec = None
        info = None
        option = None
        if not pair:
            blockers.append(
                DemoBlocker(
                    SYMBOL_REQUIRED,
                    "Choose the broker symbol the test order is for; none is guessed.",
                    "The list holds every symbol your provider setup resolved.",
                )
            )
        else:
            option = next(
                (item for item in self.symbol_options() if item.get("pair") == pair), None
            )
            if option is None:
                blockers.append(
                    DemoBlocker(
                        SYMBOL_NOT_VERIFIED,
                        f"{pair} is not one of the symbols this workstation resolved.",
                        "Choose a symbol from the list; re-run provider setup to add one.",
                    )
                )
            elif not option.get("verified"):
                blockers.append(
                    DemoBlocker(
                        SYMBOL_NOT_VERIFIED,
                        f"{pair} could not be verified with the terminal "
                        f"({option.get('reason') or 'no specification'}).",
                        "Check the symbol exists in Market Watch and that the terminal is "
                        "connected.",
                    )
                )
            else:
                platform, reason = self._platform(pair)
                if platform is None:
                    blockers.append(DemoBlocker(STACK_UNAVAILABLE, reason))
                else:
                    spec = getattr(platform, "spec", None)
                    try:
                        _, info = self.adapter.symbol_info(spec)
                    except Exception as exc:
                        blockers.append(
                            DemoBlocker(
                                SYMBOL_NOT_VERIFIED,
                                f"The terminal stopped answering for {pair} "
                                f"({type(exc).__name__}).",
                                "Reconnect and try again.",
                            )
                        )
                        info = None

        # 3. Side.
        if side not in {"BUY", "SELL"}:
            blockers.append(
                DemoBlocker(
                    SIDE_REQUIRED,
                    "Choose BUY or SELL; the direction is never defaulted.",
                    "",
                )
            )

        # 4. Volume, on the broker's own grid.
        lots = None
        if not volume_text:
            blockers.append(
                DemoBlocker(
                    VOLUME_REQUIRED,
                    "Enter a volume in lots; nothing is prefilled.",
                    "The broker's minimum, step and maximum are shown beside the field.",
                )
            )
        else:
            if info is None:
                blockers.append(
                    DemoBlocker(
                        VOLUME_INVALID,
                        "The volume cannot be checked until the symbol is verified.",
                    )
                )
            else:
                lots = _decimal(volume_text)
                if lots is None or lots <= 0:
                    blockers.append(
                        DemoBlocker(
                            VOLUME_INVALID,
                            "The volume must be a positive number of lots.",
                        )
                    )
                    lots = None
                else:
                    from ..brokers.mt5.symbols import to_broker_volume

                    quantity, quantity_problem = _demo_quantity(spec, lots)
                    if quantity is None:
                        blockers.append(
                            DemoBlocker(VOLUME_INVALID, quantity_problem)
                        )
                    else:
                        broker_lots, lot_result = to_broker_volume(spec, quantity, info)
                        if broker_lots is None:
                            errors = tuple(getattr(lot_result, "errors", ()) or ())
                            blockers.append(
                                DemoBlocker(
                                    VOLUME_INVALID,
                                    "The broker would refuse this size ("
                                    + ", ".join(errors or ("invalid_quantity",))
                                    + f"): minimum {info.volume_min} lots, step "
                                    f"{info.volume_step}, maximum {info.volume_max}.",
                                )
                            )

        # 5. The protective stop, in the broker's own points.
        points = None
        if not stop_text:
            blockers.append(
                DemoBlocker(
                    STOP_REQUIRED,
                    "Enter the protective stop distance in points; an unprotected order "
                    "is never sent.",
                    "The broker's own minimum is shown beside the field.",
                )
            )
        else:
            points_value = _decimal(stop_text)
            if points_value is None or points_value <= 0 or points_value != points_value.to_integral_value():
                blockers.append(
                    DemoBlocker(
                        STOP_INVALID,
                        "The protective stop must be a whole number of points, above zero.",
                    )
                )
            else:
                points = int(points_value)
                if info is None or spec is None:
                    blockers.append(
                        DemoBlocker(
                            STOP_INVALID,
                            "The stop cannot be checked until the symbol is verified.",
                        )
                    )
                else:
                    stops_level = int(getattr(info, "stops_level", 0) or 0)
                    if stops_level and points < stops_level:
                        blockers.append(
                            DemoBlocker(
                                STOP_INVALID,
                                f"The broker's minimum stop for {option.get('broker_symbol')} "
                                f"is {stops_level} points; {points} would be refused.",
                            )
                        )
                    else:
                        preview = self._stop_preview(spec, info, side or "BUY", points)
                        if not preview.get("price"):
                            blockers.append(
                                DemoBlocker(
                                    STOP_INVALID,
                                    "No stop price can be computed from the current quote "
                                    f"({preview.get('reason')}).",
                                    "Wait for a quote, or reconnect the terminal.",
                                )
                            )
                        # The stop that is actually attached comes from the local
                        # broker-side stop setting; the typed distance must be that
                        # same distance, or the test would be describing one order
                        # and sending another.
                        requirement = None
                        try:
                            requirement = dict(self.adapter.stop_requirement(spec))
                        except Exception:
                            requirement = None
                        if requirement is None or not requirement.get("valid"):
                            blockers.append(
                                DemoBlocker(
                                    "live_protective_stop_unconfigured",
                                    "The broker-side stop distance for this symbol is not "
                                    "configured, so no protected order can be sent.",
                                    "Set it under Broker-side stop on this page, reconnect, "
                                    "then run the test.",
                                )
                            )
                        else:
                            effective = int(requirement.get("distance_points") or 0)
                            if effective != points:
                                blockers.append(
                                    DemoBlocker(
                                        STOP_MISMATCH,
                                        f"The broker-side stop in force for "
                                        f"{option.get('broker_symbol')} is {effective} points "
                                        f"({requirement.get('declared')}); you selected "
                                        f"{points}. The order would carry a different stop "
                                        "than the one you chose.",
                                        f"Set the distance to {points} points under "
                                        "Broker-side stop, reconnect, then send. Nothing is "
                                        "sent with a stop you did not select.",
                                    )
                                )

        # 6. The two confirmations: the phrase, and the account.
        if not phrase:
            blockers.append(
                DemoBlocker(
                    CONFIRMATION_REQUIRED,
                    f"Type {CONFIRMATION} to confirm you are sending one real order to "
                    "your demo account.",
                )
            )
        elif phrase.strip().upper() != CONFIRMATION:
            blockers.append(
                DemoBlocker(
                    CONFIRMATION_MISMATCH,
                    f"The confirmation phrase must be exactly: {CONFIRMATION}",
                )
            )
        if not account_confirmation:
            blockers.append(
                DemoBlocker(
                    ACCOUNT_CONFIRMATION_REQUIRED,
                    "Repeat the account signature shown on this page, so the test names "
                    "one account.",
                    "The signature is the short fingerprint in the panel above.",
                )
            )
        elif account_confirmation != account.get("fingerprint"):
            blockers.append(
                DemoBlocker(
                    ACCOUNT_CONFIRMATION_MISMATCH,
                    "The account signature you typed is not the connected account's.",
                    "Copy the signature from the panel above; it changes with the account.",
                )
            )

        # 7. One request, ever.
        record = self.latest()
        status = _text(record.get("status"))
        if status in FINAL_STATES:
            blockers.append(
                DemoBlocker(
                    ALREADY_USED,
                    f"A demo test already ran ({status}, request "
                    f"{_text(record.get('request_id')) or 'unknown'}).",
                    "Reconcile it; a second submit is refused, never retried.",
                )
            )
        elif status in {SUBMITTING, UNRESOLVED}:
            blockers.append(
                DemoBlocker(
                    IN_DOUBT,
                    "A demo test request is in doubt. It will not be re-sent.",
                    "Reconcile the same request id first.",
                )
            )

        return {
            "ok": not blockers,
            "blockers": [item.audit_view() for item in blockers],
            "values": {
                "symbol": pair,
                "broker_symbol": _text((option or {}).get("broker_symbol")),
                "side": side,
                "volume": volume_text,
                "stop_points": stop_text,
                "quotes": {
                    "bid": _text((option or {}).get("bid")),
                    "ask": _text((option or {}).get("ask")),
                },
            },
            "terms": dict(option or {}),
            "stop": (
                self._stop_preview(spec, info, side or "BUY", points)
                if spec is not None and info is not None and points
                else {}
            ),
            "account": account,
            "execution": execution,
        }

    # -- the one submission ----------------------------------------------

    def _new_request_id(self) -> str:
        """One durable, comment-safe idempotency id.

        MT5 truncates an order comment at 31 characters, so the id is built to
        fit: the id in the durable ledger and the comment on the broker's own
        order are then the same string, and no reconciliation has to guess which
        truncated prefix belongs to which request.
        """

        stamp = self.clock().astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        return f"demo-{stamp}-{secrets.token_hex(4)}"

    def submit(self, values: Mapping[str, Any]) -> dict[str, Any]:
        """Send at most one demo order through the existing live leg."""

        checked = self.check(values)
        if not checked["ok"]:
            return {
                "ok": False,
                "reason": checked["blockers"][0]["reason"],
                "message": checked["blockers"][0]["message"],
                "blockers": checked["blockers"],
                "sent": False,
                "status": IDLE,
            }
        pair = checked["values"]["symbol"]
        platform, reason = self._platform(pair)
        if platform is None:
            return {
                "ok": False,
                "reason": reason,
                "message": "The provider stack for that symbol could not be built.",
                "blockers": [{"reason": reason, "message": reason, "hint": ""}],
                "sent": False,
                "status": IDLE,
            }
        spec = getattr(platform, "spec", None)
        quote = None
        try:
            quote = self.adapter.quote(spec)
        except Exception:
            quote = None
        if quote is None:
            return {
                "ok": False,
                "reason": QUOTE_UNAVAILABLE,
                "message": "The terminal returned no quote, so no order can be priced.",
                "blockers": [{"reason": QUOTE_UNAVAILABLE, "message": "", "hint": ""}],
                "sent": False,
                "status": IDLE,
            }

        side = checked["values"]["side"]
        lots = _decimal(checked["values"]["volume"]) or Decimal(0)
        quantity, quantity_reason = _demo_quantity(spec, lots)
        if quantity is None:
            return {
                "ok": False,
                "reason": VOLUME_INVALID,
                "message": quantity_reason,
                "blockers": [{"reason": VOLUME_INVALID, "message": quantity_reason, "hint": ""}],
                "sent": False,
                "status": IDLE,
            }

        request_id = self._new_request_id()
        now = self.clock().astimezone(timezone.utc)
        record: dict[str, Any] = {
            "kind": KIND,
            "test_id": request_id,
            "request_id": request_id,
            "status": SUBMITTING,
            "created_at": now.isoformat(),
            "submitted_at": "",
            "values": {
                "symbol": pair,
                "broker_symbol": checked["values"]["broker_symbol"],
                "side": side,
                "volume_lots": str(lots),
                "quantity": str(quantity),
                "stop_points": int(Decimal(checked["values"]["stop_points"])),
                "stop_preview": dict(checked.get("stop") or {}),
                "terms": {
                    key: checked["terms"].get(key)
                    for key in (
                        "point",
                        "digits",
                        "volume_min",
                        "volume_step",
                        "volume_max",
                        "stops_level",
                        "bid",
                        "ask",
                    )
                },
                "quote_at": _iso(getattr(quote, "timestamp", "")),
            },
            "account": {
                "mode": checked["account"].get("mode"),
                "confirmed": checked["account"].get("confirmed"),
                "fingerprint": checked["account"].get("fingerprint"),
                "server": checked["account"].get("server"),
            },
            "kind_word": "DEMO_EXECUTION_TEST",
            "not_a_strategy": True,
            "events": [],
        }
        # Write-ahead: the record and the audit row exist before the leg is
        # called, and the id they carry is the id the broker request will carry.
        record = self._write(
            record,
            "DEMO_TEST_SUBMISSION_STARTED",
            request_id=request_id,
            symbol=pair,
            broker_symbol=checked["values"]["broker_symbol"],
            side=side,
            volume_lots=str(lots),
            stop_points=int(Decimal(checked["values"]["stop_points"])),
            account_mode=checked["account"].get("mode"),
        )
        record["submitted_at"] = now.isoformat()

        leg, leg_reason = self._leg(platform, spec)
        if leg is None:
            record["status"] = REFUSED
            record["refusal"] = {"reason": leg_reason, "message": ""}
            self._write(record, "DEMO_TEST_REFUSED", reason=leg_reason)
            self._disarm("demo_test_stack_unavailable")
            return {
                "ok": False,
                "reason": leg_reason,
                "message": "The guarded live leg could not be built, so nothing was sent.",
                "blockers": [{"reason": leg_reason, "message": "", "hint": ""}],
                "sent": False,
                "status": REFUSED,
                "request_id": request_id,
            }

        plan = self._plan(platform, request_id, quantity, quote, side)
        if plan is None:
            record["status"] = REFUSED
            record["refusal"] = {"reason": STACK_UNAVAILABLE, "message": ""}
            self._write(record, "DEMO_TEST_REFUSED", reason=STACK_UNAVAILABLE)
            self._disarm("demo_test_context_unavailable")
            return {
                "ok": False,
                "reason": STACK_UNAVAILABLE,
                "message": "The gate context for this quote could not be built; nothing "
                "was sent.",
                "blockers": [{"reason": STACK_UNAVAILABLE, "message": "", "hint": ""}],
                "sent": False,
                "status": REFUSED,
                "request_id": request_id,
            }

        try:
            outcome = dict(leg.submit_plan(plan, demo_test=True) or {})
        except Exception as exc:
            record["status"] = UNRESOLVED
            record["refusal"] = {"reason": type(exc).__name__, "message": str(exc)}
            self._write(record, "DEMO_TEST_SUBMISSION_UNCERTAIN", reason=type(exc).__name__)
            self._disarm("demo_test_submission_uncertain")
            return {
                "ok": False,
                "reason": "demo_submission_outcome_unreadable",
                "message": "The live leg raised while submitting. The request is in doubt "
                "and will not be re-sent; reconcile it by the same id.",
                "blockers": [],
                "sent": True,
                "status": UNRESOLVED,
                "request_id": request_id,
            }

        final, summary = _classify(outcome)
        record["status"] = final
        record["broker"] = {key: summary.get(key) for key in (
            "submitted",
            "state",
            "retcode",
            "retcode_name",
            "classification",
            "volume_requested",
            "volume_filled",
            "booked",
            "unknown",
            "permits_resend",
            "reasons",
        )}
        record["transport"] = {
            "sent": bool(outcome.get("submitted")),
            "unknown": bool(outcome.get("unknown")),
        }
        if not outcome.get("submitted"):
            record["refusal"] = {
                "reason": ",".join(str(item) for item in outcome.get("reasons") or ()),
                "message": _text(outcome.get("detail")),
            }
        record = self._write(
            record,
            "DEMO_TEST_BROKER_RESPONSE",
            request_id=request_id,
            broker_state=final,
            retcode=summary.get("retcode"),
            volume_filled=str(summary.get("volume_filled") or ""),
        )
        # Whatever happened, trading is disarmed: a smoke test never leaves the
        # adapter armed behind it.
        record = self._disarm(f"demo_test_{final.lower()}")
        if final == UNRESOLVED:
            record = self.reconcile()
        return {
            "ok": final in {FILLED, PARTIAL},
            "reason": final,
            "message": _message_for(final, summary),
            "blockers": [],
            "sent": bool(outcome.get("submitted")),
            "status": final,
            "request_id": request_id,
            "broker": dict(summary),
            "record": record.get("values") if isinstance(record, Mapping) else {},
        }

    # -- reconciliation --------------------------------------------------

    def reconcile(self) -> dict[str, Any]:
        """Read the same identity again.  Never sends and never re-sends."""

        record = self.latest()
        if not record or not _text(record.get("request_id")):
            return {
                "ok": False,
                "reason": NOTHING_TO_RECONCILE,
                "message": "No demo test record exists on this machine, so there is "
                "nothing to reconcile.",
                "status": IDLE,
            }
        request_id = _text(record.get("request_id"))
        order = None
        if self.repository is not None:
            try:
                order = self.repository.get_order(request_id)
            except Exception:
                order = None
        broker_order = None
        if order is None and self.adapter is not None:
            try:
                broker_order = next(
                    (
                        item
                        for item in self.adapter.account_orders()
                        if _text(item.get("request_id")) == request_id
                    ),
                    None,
                )
            except Exception:
                broker_order = None
        durable = dict(order or broker_order or {})
        state = _text(durable.get("state")).upper()
        if _text(record.get("status")) in FINAL_STATES and state in FINAL_STATES:
            final = state
        elif state in FINAL_STATES:
            final = RECONCILED
        else:
            final = UNRESOLVED
        record["status"] = final
        record["reconciliation"] = {
            "at": self.clock().astimezone(timezone.utc).isoformat(),
            "request_id": request_id,
            "durable_state": state,
            "durable_volume_filled": _text(durable.get("volume_filled")),
            "broker_orders_consulted": bool(broker_order is not None),
            "resent": False,
        }
        record = self._write(
            record,
            "DEMO_TEST_RECONCILED",
            request_id=request_id,
            durable_state=state,
            resent=False,
        )
        return {
            "ok": final in {FILLED, PARTIAL, RECONCILED, REJECTED, REFUSED},
            "reason": final,
            "message": (
                "The same request id was read back from the durable order ledger and the "
                "broker's own order list. Nothing was re-sent."
                if final != UNRESOLVED
                else "The broker still cannot say what happened to this request. It stays "
                "in doubt: nothing is re-sent and no new test can be sent until it is "
                "explained."
            ),
            "status": final,
            "request_id": request_id,
            "durable": {
                "state": state,
                "volume_filled": _text(durable.get("volume_filled")),
                "retcode": _text(durable.get("retcode")),
            },
        }

    # -- internals -------------------------------------------------------

    def _disarm(self, reason: str) -> dict[str, Any]:
        try:
            self.adapter.disarm(reason=reason)
        except Exception:
            pass
        record = self.latest()
        if not record:
            return {}
        return self._write(record, "DEMO_TEST_DISARMED", reason=reason)

    def _leg(self, platform: Any, spec: Any) -> tuple[Any, str]:
        """The *existing* live leg, wired to the same gate, router and adapter."""

        if self.adapter is None:
            return None, "live_adapter_missing"
        from ..execution.mode_router import ExecutionRouter
        from .live import LiveLeg

        try:
            router = ExecutionRouter(
                mode=self.mode,
                paper=None,
                live=self.adapter,
                audit=getattr(platform, "audit_log", None),
            )
            leg = LiveLeg(
                router=router,
                adapter=self.adapter,
                gate=getattr(platform, "gate", None),
                mode=self.mode,
                spec=spec,
                strategy_id=STRATEGY_ID,
                repository=getattr(platform, "repository", None),
                audit_log=getattr(platform, "audit_log", None),
                portfolio=getattr(platform, "portfolio", None),
                ledger_positions=self.ledger_positions,
                clock=self.clock,
            )
        except Exception as exc:
            return None, f"demo_leg_unavailable:{type(exc).__name__}"
        return leg, ""

    def _plan(
        self,
        platform: Any,
        request_id: str,
        quantity: Decimal,
        quote: Any,
        side: str,
    ) -> Any:
        """One ``OrderRequest`` and the ``GateContext`` it is judged in."""

        from app.data.models import ForexQuoteBar, QuoteSideBar
        from app.risk.models import OrderRequest

        spec = getattr(platform, "spec", None)
        factory = getattr(platform, "context_factory", None)
        if spec is None or factory is None or quote is None:
            return None
        try:
            at = quote.timestamp_utc()
        except Exception:
            return None
        seconds = int(getattr(platform, "execution_config", None).__dict__.get("timeframe_seconds", 0) or 0) or 60
        pair = _text(getattr(spec, "canonical_pair", "") or getattr(spec, "pair", ""))
        bid = getattr(quote.bid, "price", quote.bid) if hasattr(quote, "bid") else None
        ask = getattr(quote.ask, "price", quote.ask) if hasattr(quote, "ask") else None
        if bid is None or ask is None:
            return None
        bid_bar = QuoteSideBar(open=bid, high=bid, low=bid, close=bid)
        ask_bar = QuoteSideBar(open=ask, high=ask, low=ask, close=ask)
        bar = ForexQuoteBar(
            pair=_text(quote.pair) or pair,
            timeframe="M1",
            timestamp=at.replace(microsecond=0),
            duration_seconds=seconds,
            bid=bid_bar,
            ask=ask_bar,
            source="AXIOM_DEMO_EXECUTION_TEST_SINGLE_QUOTE",
            quote_count=1,
            last_quote_at=at,
            complete=True,
        )
        try:
            validation = bar.validate(spec)
        except Exception:
            return None
        if not getattr(validation, "valid", False):
            return None
        config = getattr(platform, "execution_config", None)
        provider_id = _text(getattr(config, "provider_id", ""))
        provider_symbol = _text(getattr(platform, "broker_symbol", ""))
        if not provider_id or not provider_symbol:
            return None
        request = OrderRequest(
            request_id=request_id,
            provider_id=provider_id,
            provider_symbol=provider_symbol,
            side=OrderSide(side),
            quantity=quantity,
            # The gate refuses a LIVE-target request by design (the mode switch
            # is the router's business, not the gate's), so the request carries
            # the mode the gate judges.  The adapter still only runs in LIVE
            # mode: ``route_live`` refuses anything else.
            target_mode=TradingMode.PAPER,
            evaluated_at=at,
            quote=quote,
            strategy_id=STRATEGY_ID,
            close_intent=None,
        )
        try:
            context = factory.build(at=at, bar=bar)
        except Exception:
            return None
        return DemoPlan(request=request, context=context, quantity=quantity)


def _demo_quantity(spec: Any, lots: Decimal) -> tuple[Decimal | None, str]:
    """Lots the operator typed -> the quantity the request must carry."""

    unit = _text(getattr(spec, "quantity_unit_value", "") or getattr(spec, "quantity_unit", ""))
    if unit.upper() == "LOTS":
        return lots, ""
    contract = _decimal(getattr(spec, "contract_size", ""))
    if contract is None or contract <= 0:
        return None, "the instrument's contract size is not readable, so lots cannot be converted"
    return contract * lots, ""


def _classify(outcome: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    """One broker answer -> one record state, in the operator's words."""

    summary = {
        "submitted": bool(outcome.get("submitted")),
        "state": _text(outcome.get("state")),
        "retcode": int(outcome.get("retcode") or 0),
        "retcode_name": _text(outcome.get("retcode_name")),
        "classification": _text(outcome.get("classification")),
        "volume_requested": _text(outcome.get("volume_requested")),
        "volume_filled": _text(outcome.get("volume_filled")),
        "booked": bool(outcome.get("booked")),
        "unknown": bool(outcome.get("unknown")),
        "permits_resend": bool(outcome.get("permits_resend")),
        "reasons": list(outcome.get("reasons") or []),
    }
    filled = _decimal(summary["volume_filled"]) or Decimal(0)
    requested = _decimal(summary["volume_requested"]) or Decimal(0)
    if not summary["submitted"]:
        return REFUSED, summary
    if summary["unknown"] or summary["state"].upper() == "UNRESOLVED":
        return UNRESOLVED, summary
    if summary["state"].upper() == "REJECTED" or "REJECT" in summary["classification"].upper():
        return REJECTED, summary
    if filled <= 0:
        return REJECTED, summary
    if requested > 0 and filled < requested:
        return PARTIAL, summary
    return FILLED, summary


def _message_for(final: str, summary: Mapping[str, Any]) -> str:
    if final == FILLED:
        return (
            f"The broker accepted one order: {summary.get('volume_filled')} lots filled"
            + (
                f" (retcode {summary.get('retcode')} {summary.get('retcode_name')})."
                if summary.get("retcode")
                else "."
            )
            + " Trading was disarmed afterwards."
        )
    if final == PARTIAL:
        return (
            f"The broker filled part of the request: {summary.get('volume_filled')} of "
            f"{summary.get('volume_requested')} lots. Trading was disarmed afterwards."
        )
    if final == REJECTED:
        return (
            "The broker refused the order"
            + (
                f" (retcode {summary.get('retcode')} {summary.get('retcode_name')})."
                if summary.get("retcode")
                else "."
            )
            + " No position was opened; trading was disarmed afterwards."
        )
    if final == REFUSED:
        return (
            "Nothing was sent: the guarded live path refused before reaching the broker. "
            "The refusal reasons are recorded with the request id."
        )
    return (
        "The broker's answer is in doubt. The request is recorded and will never be "
        "re-sent; reconcile it by the same id. Trading was disarmed."
    )


__all__ = [
    "CONFIRMATION",
    "DemoBlocker",
    "DemoExecutionTest",
    "DemoPlan",
    "FINAL_STATES",
    "KIND",
    "STRATEGY_ID",
]

"""Observation-only terminal session.

The workstation looks at a terminal; it never trades through one.  That is
enforced structurally rather than by convention:

* this module builds :class:`~axiom_forex.brokers.mt5.terminal.MT5Terminal`
  directly -- the *data* half of the MT5 integration -- and never imports
  :class:`MT5TradingAdapter`.  There is no arming flag, no ``submit``, no
  ``order_send`` and no ``order_check`` anywhere in the object graph the UI holds;
* connections happen only when :meth:`ObservationSession.connect` is called from
  an explicit operator action; nothing reconnects on its own, and nothing
  disconnects on its own;
* the snapshot is redacted: account login becomes a fingerprint, and balances,
  equity, margin and profit are never copied into it.

``SimulatedObservationSource`` exists so the interface can be explored on a
machine without MT5.  It is always labelled ``SIMULATED`` in every surface, it
never contacts anything, and it is only ever selected by an explicit flag.

The object *here* is still data-only.  Paper trading reads prices from the same
terminal session through :meth:`MT5ObservationSource.market_data`, which wraps
the terminal's quote/ticks surface -- never an order surface -- so paper mode
can be priced from real broker data without any execution capability existing in
this module.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Protocol, Sequence, runtime_checkable

from ..brokers.mt5.identity import account_fingerprint
from ..brokers.mt5.terminal import (
    MT5DataRefused,
    MT5Error,
    MT5ModuleUnavailable,
    MT5Terminal,
    MT5TerminalUnavailable,
)
from ..config.mt5 import MT5Settings, MT5SettingsError

#: States a session can be in.  The UI renders each one differently.
DISCONNECTED = "DISCONNECTED"
CONNECTING = "CONNECTING"
CONNECTED = "CONNECTED"
ERROR = "ERROR"

#: How many symbols the snapshot carries to the browser.
MAX_SYMBOLS = 120


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class ConnectionRefused(Exception):
    """Connect was attempted and refused, with an operator-readable reason."""

    def __init__(self, reason: str, detail: str = "", hint: str = ""):
        self.reason = reason
        self.detail = detail
        self.hint = hint
        super().__init__(reason if not detail else f"{reason}: {detail}")

    def audit_view(self) -> dict[str, Any]:
        return {"reason": self.reason, "detail": self.detail, "hint": self.hint}


#: Reasons a Connect attempt can fail where the fix is in the terminal, not in
#: this app.  Each hint names the exact next step; none of them suggests
#: relaxing a check, and none echoes a value from the settings document.
CONNECT_HINTS: dict[str, str] = {
    "mt5_account_mismatch": (
        "MetaTrader 5 is signed in to a different account than the one "
        "mt5_settings.local.json names. Sign in to the configured account inside "
        "MetaTrader 5, then press Connect again. AXIOM will not accept a different "
        "account, and it never switches accounts for you."
    ),
    "mt5_server_mismatch": (
        "The terminal is connected to a different broker server than the one "
        "mt5_settings.local.json names. Switch to an account on the configured "
        "server inside MetaTrader 5, then press Connect again -- or correct the "
        "settings document if that server is the one you meant."
    ),
    "mt5_terminal_not_connected": (
        "The terminal answered but is not connected to a broker. Reconnect inside "
        "MetaTrader 5, then press Connect here again."
    ),
}

DEFAULT_CONNECT_HINT = (
    "Open MetaTrader 5, sign in to the account you want, and leave it running; "
    "then press Connect again."
)


@runtime_checkable
class ObservationSource(Protocol):
    """The narrow read-only surface the workstation may use."""

    source_id: str
    kind: str  # "LIVE_TERMINAL" | "SIMULATED"
    label: str

    def connect(self) -> None: ...

    def disconnect(self) -> None: ...

    def snapshot(self) -> dict[str, Any]: ...


@dataclass
class MT5ObservationSource:
    """Read-only access to a real terminal through the official package."""

    settings: MT5Settings
    module: Any | None = None
    clock: Callable[[], datetime] | None = None
    source_id: str = "mt5-terminal"
    kind: str = "LIVE_TERMINAL"
    label: str = "MetaTrader 5 terminal"
    _terminal: MT5Terminal | None = field(default=None, init=False, repr=False)

    def _module(self) -> Any:
        if self.module is None:
            from ..brokers.mt5.terminal import import_mt5

            self.module = import_mt5()
        return self.module

    @property
    def connected(self) -> bool:
        return self._terminal is not None and self._terminal.connected

    def connect(self) -> None:
        validation = self.settings.validate()
        if not validation.valid:
            raise ConnectionRefused(
                "mt5_settings_invalid",
                validation.reason,
                "Fix the local settings document, then try again.",
            )
        try:
            module = self._module()
        except MT5ModuleUnavailable as exc:
            raise ConnectionRefused(
                "metatrader5_package_missing",
                exc.detail,
                "Install the official package: py -m pip install MetaTrader5",
            ) from exc
        terminal = MT5Terminal(self.settings, module=module, clock=self.clock)
        try:
            terminal.connect()
        except MT5TerminalUnavailable as exc:
            raise ConnectionRefused(
                exc.reason,
                exc.detail,
                CONNECT_HINTS.get(exc.reason, DEFAULT_CONNECT_HINT),
            ) from exc
        except MT5Error as exc:
            raise ConnectionRefused(
                exc.reason, exc.detail, CONNECT_HINTS.get(exc.reason, DEFAULT_CONNECT_HINT)
            ) from exc
        self._terminal = terminal

    def market_data(self, freshness: Any) -> Any:
        """The terminal's quote surface, wrapped for paper execution.

        This is the *data* half of the terminal: ``quote``, ``ticks``, and
        ``resolve_symbol``.  There is no order surface in the object returned, so
        a paper decision can be priced from real broker ticks without gaining any
        ability to send anything.
        """

        if self._terminal is None or not self._terminal.connected:
            raise ConnectionRefused(
                "not_connected",
                "no terminal session is open",
                "Press Connect first. Paper trading reads prices from the terminal "
                "you connected; without it there is no price to use.",
            )
        from ..execution.live_data_paper import TerminalMarketData

        return TerminalMarketData(
            terminal=self._terminal, freshness=freshness, clock=self.clock
        )

    def disconnect(self) -> None:
        if self._terminal is not None:
            self._terminal.disconnect()
        self._terminal = None

    def snapshot(self) -> dict[str, Any]:
        if self._terminal is None or not self._terminal.connected:
            return {"connected": False}
        try:
            status = self._terminal.status()
        except MT5Error as exc:
            return {"connected": False, "error": exc.reason}
        symbols: list[dict[str, Any]] = []
        symbol_error = ""
        try:
            for name in self._terminal.symbol_names()[:MAX_SYMBOLS]:
                symbols.append({"name": name, "tradable": None, "digits": None})
        except MT5DataRefused as exc:
            symbol_error = exc.reason
        return {
            "connected": True,
            "source_kind": self.kind,
            #: Where a paper fill would be priced from.  Real ticks, or none.
            "market_data": "TERMINAL_TICKS",
            "terminal": {
                "name": status.terminal_name,
                "build": status.build,
                "company": status.company,
                "connected": status.terminal_connected,
                "trade_allowed": status.trade_allowed,
                "tradeapi_disabled": status.tradeapi_disabled,
            },
            "account": {
                "login": "<redacted>",
                "fingerprint": account_fingerprint(status.account_login),
                "server": status.account_server,
                "company": status.company,
                "currency": status.account_currency,
                "currency_digits": status.currency_digits,
                "leverage": status.leverage,
                "mode": status.account_mode,
                "mode_confirmed": status.account_mode_confirmed,
                "trade_mode": status.trade_mode,
                "margin_mode": status.margin_mode_name,
                "fifo_close": status.fifo_close,
                "trade_allowed": status.trade_allowed,
                "balance_reporting": "withheld",
            },
            "symbols": symbols,
            "symbol_error": symbol_error,
            "observed_at": status.time if hasattr(status, "time") else None,
        }


#: The simulator's account is deliberately unlike a real one: an obviously fake
#: server name, a demo mode, and round numbers.  It is never presented as real.
SIMULATED_ACCOUNT = {
    "login": "<redacted>",
    "fingerprint": account_fingerprint(90000001),
    "server": "SIMULATOR-DEMO",
    "company": "AXIOM offline simulator",
    "currency": "USD",
    "currency_digits": 2,
    "leverage": 100,
    "mode": "DEMO",
    "mode_confirmed": True,
    "trade_mode": 0,
    "margin_mode": "RETAIL_HEDGING",
    "fifo_close": False,
    "trade_allowed": False,
    "balance_reporting": "withheld",
    #: Marks every value above as an illustration. Nothing here was read from a
    #: broker, and the interface must say so wherever it shows them.
    "sample": True,
}

SIMULATED_SYMBOLS: tuple[dict[str, Any], ...] = (
    {"name": "SAMPLE_EURUSD", "description": "Sample major pair", "digits": 5, "tradable": True},
    {"name": "SAMPLE_GBPUSD", "description": "Sample major pair", "digits": 5, "tradable": True},
    {"name": "SAMPLE_USDJPY", "description": "Sample major pair", "digits": 3, "tradable": True},
    {"name": "SAMPLE_XAUUSD", "description": "Sample metal", "digits": 2, "tradable": False},
)


@dataclass
class SimulatedObservationSource:
    """A labelled stand-in so the interface can be explored without MT5."""

    clock: Callable[[], datetime] | None = None
    source_id: str = "offline-simulator"
    kind: str = "SIMULATED"
    label: str = "Offline simulator (no terminal attached)"
    _connected: bool = field(default=False, init=False)

    @property
    def connected(self) -> bool:
        return self._connected

    def connect(self) -> None:
        self._connected = True

    def disconnect(self) -> None:
        self._connected = False

    def snapshot(self) -> dict[str, Any]:
        clock = self.clock or _utc_now
        if not self._connected:
            return {"connected": False, "source_kind": self.kind}
        return {
            "connected": True,
            "source_kind": self.kind,
            "market_data": "SAMPLE",
            "simulated": True,
            "terminal": {
                "name": "AXIOM offline simulator",
                "build": 0,
                "company": "AXIOM offline simulator",
                "connected": True,
                "trade_allowed": False,
                "tradeapi_disabled": True,
            },
            "account": dict(SIMULATED_ACCOUNT),
            "sample": True,
            "symbols": [dict(item) for item in SIMULATED_SYMBOLS],
            "symbol_error": "",
            "observed_at": clock(),
        }


@dataclass
class ObservationSession:
    """Owns the connection lifecycle; the UI only calls these four methods."""

    source: ObservationSource
    clock: Callable[[], datetime] | None = None
    state: str = DISCONNECTED
    last_error: ConnectionRefused | None = None
    connected_at: datetime | None = None
    disconnected_at: datetime | None = None
    connect_count: int = 0
    snapshot_data: dict[str, Any] = field(default_factory=dict)

    # -- the only mutating operations, both explicit --------------------

    def connect(self) -> dict[str, Any]:
        """Connect because the operator asked.  Never called implicitly."""

        if self.state == CONNECTED:
            # Idempotent: pressing Connect twice must not re-open a session or
            # switch anything behind the operator's back.
            return self.view()
        self.state = CONNECTING
        try:
            self.source.connect()
        except ConnectionRefused as exc:
            self.state = ERROR
            self.last_error = exc
            self.snapshot_data = {}
            return self.view()
        except Exception as exc:  # defensive: no traceback reaches the page
            self.state = ERROR
            self.last_error = ConnectionRefused(
                "connection_failed", type(exc).__name__
            )
            self.snapshot_data = {}
            return self.view()
        self.state = CONNECTED
        self.last_error = None
        self.connected_at = (self.clock or _utc_now)()
        self.connect_count += 1
        self.snapshot_data = self.source.snapshot()
        return self.view()

    def disconnect(self) -> dict[str, Any]:
        """Disconnect because the operator asked.  Never called implicitly."""

        try:
            self.source.disconnect()
        except Exception:
            pass
        self.state = DISCONNECTED
        self.snapshot_data = {}
        self.disconnected_at = (self.clock or _utc_now)()
        return self.view()

    def refresh(self) -> dict[str, Any]:
        """Re-read the current session.  Never connects."""

        if self.state == CONNECTED:
            try:
                fresh = self.source.snapshot()
            except Exception as exc:
                self.state = ERROR
                self.last_error = ConnectionRefused(
                    "snapshot_failed", type(exc).__name__
                )
                return self.view()
            if not fresh.get("connected", False):
                # The terminal closed the session; report it and stop.  Do not
                # reconnect -- that needs a fresh operator click.
                self.state = ERROR
                self.last_error = ConnectionRefused(
                    "terminal_disconnected",
                    str(fresh.get("error", "")),
                    "The terminal is no longer connected. Reopen MetaTrader 5 and "
                    "press Connect when you are ready.",
                )
                self.snapshot_data = {}
                return self.view()
            self.snapshot_data = fresh
        return self.view()

    # -- read model -----------------------------------------------------

    def should_poll(self) -> bool:
        return False

    def market_data_class(self) -> str:
        """Where prices would come from right now: terminal ticks, or nothing."""

        if not self.connected:
            return "NONE"
        kind = getattr(self.source, "kind", "")
        if kind == "SIMULATED":
            return "SAMPLE"
        if kind == "LIVE_TERMINAL":
            return "TERMINAL_TICKS"
        return "NONE"

    @property
    def connected(self) -> bool:
        return self.state == CONNECTED

    def account(self) -> Mapping[str, Any]:
        return self.snapshot_data.get("account", {}) if self.connected else {}

    def symbols(self) -> Sequence[Mapping[str, Any]]:
        return self.snapshot_data.get("symbols", ()) if self.connected else ()

    def view(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "connected": self.connected,
            "source_kind": getattr(self.source, "kind", "UNKNOWN"),
            "source_id": getattr(self.source, "source_id", ""),
            "source_label": getattr(self.source, "label", ""),
            "connected_at": self.connected_at,
            "disconnected_at": self.disconnected_at,
            "connect_count": self.connect_count,
            "error": self.last_error.audit_view() if self.last_error else None,
            "snapshot": dict(self.snapshot_data),
            "auto_reconnect": False,
            "auto_disconnect": False,
            "polling": False,
            "market_data": self.market_data_class(),
            #: This object holds a data terminal only; execution mode decides
            #: whether a strategy's order is simulated or routed elsewhere.
            "trading_capability": "none",
        }


def session_from_settings(
    settings: MT5Settings,
    *,
    module: Any | None = None,
    simulate: bool = False,
    clock: Callable[[], datetime] | None = None,
) -> ObservationSession:
    """Build the session the workstation uses.

    ``simulate=True`` is only passed by the launcher's explicit offline flag.
    """

    if simulate:
        source: ObservationSource = SimulatedObservationSource(clock=clock)
    else:
        source = MT5ObservationSource(settings=settings, module=module, clock=clock)
    return ObservationSession(source=source, clock=clock)


def locked_session(
    reason: str = "no_settings_document",
    clock=None,
    *,
    detail: str = "",
    hint: str = "",
    label: str = "No usable local MT5 settings",
) -> ObservationSession:
    """A session that cannot connect because configuration is missing.

    Keeping this as a normal session -- rather than a special case in the view --
    means the Connect button is disabled for an explainable reason everywhere.
    ``detail`` and ``hint`` say why this particular document was refused; both
    are operator text and never contain a value from the document.
    """

    resolved_detail = detail or (
        "No mt5_settings.local.json was found next to the launcher."
    )
    resolved_hint = hint or (
        "Create the settings document from the template on the setup screen."
    )

    class _Unavailable:
        source_id = "unconfigured"
        kind = "UNCONFIGURED"

        @property
        def label(self) -> str:
            return label

        def connect(self) -> None:
            raise ConnectionRefused(reason, resolved_detail, resolved_hint)

        def disconnect(self) -> None:
            return None

        def snapshot(self) -> dict[str, Any]:
            return {"connected": False}

    return ObservationSession(source=_Unavailable(), clock=clock)


__all__ = [
    "CONNECTED",
    "CONNECTING",
    "ConnectionRefused",
    "DISCONNECTED",
    "ERROR",
    "MAX_SYMBOLS",
    "MT5ObservationSource",
    "ObservationSession",
    "ObservationSource",
    "SIMULATED_ACCOUNT",
    "SIMULATED_SYMBOLS",
    "SimulatedObservationSource",
    "locked_session",
    "session_from_settings",
]

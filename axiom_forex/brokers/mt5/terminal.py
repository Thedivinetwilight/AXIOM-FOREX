"""Terminal/session handling for the official MetaTrader 5 Python integration.

The ``MetaTrader5`` package is an *optional* dependency and is never imported at
module load: the adapter receives the module object (production passes
:func:`import_mt5`; tests pass a fake).  Every call into it is wrapped so that a
missing function, a wrong-shaped result, or a terminal error becomes a typed
refusal instead of an exception escaping the trading path.

Nothing here sends or modifies an order.  Terminal status, symbol discovery,
and market data only.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Iterable, Mapping, Sequence

from app.data.models import ForexQuote
from app.universe.specs import CurrencyPairSpec

from ...config.mt5 import MT5Settings

# -- constants from the official integration --------------------------------
# Declared locally so a fake module (tests) never has to reproduce them, and so
# a newer module cannot silently change their meaning.

ACCOUNT_TRADE_MODE_DEMO = 0
ACCOUNT_TRADE_MODE_CONTEST = 1
ACCOUNT_TRADE_MODE_REAL = 2

#: Account modes AXIOM can name.  Anything else is ``UNCONFIRMED``: a terminal
#: answer we cannot interpret is never assumed to be demo (or real).
ACCOUNT_MODE_NAMES = {
    ACCOUNT_TRADE_MODE_DEMO: "DEMO",
    ACCOUNT_TRADE_MODE_CONTEST: "CONTEST",
    ACCOUNT_TRADE_MODE_REAL: "REAL",
}
ACCOUNT_MODE_UNCONFIRMED = "UNCONFIRMED"


def account_mode_name(trade_mode: int) -> str:
    """Map the broker's ``trade_mode`` to a name, or say it is unconfirmed."""

    try:
        return ACCOUNT_MODE_NAMES.get(int(trade_mode), ACCOUNT_MODE_UNCONFIRMED)
    except (TypeError, ValueError):
        return ACCOUNT_MODE_UNCONFIRMED

ACCOUNT_MARGIN_MODE_RETAIL_NETTING = 0
ACCOUNT_MARGIN_MODE_EXCHANGE = 1
ACCOUNT_MARGIN_MODE_RETAIL_HEDGING = 2

SYMBOL_TRADE_MODE_DISABLED = 0
SYMBOL_TRADE_MODE_LONGONLY = 1
SYMBOL_TRADE_MODE_SHORTONLY = 2
SYMBOL_TRADE_MODE_CLOSEONLY = 3
SYMBOL_TRADE_MODE_FULL = 4

COPY_TICKS_ALL = 0

#: AXIOM timeframe label -> MT5 ``TIMEFRAME_*`` value and its duration.
TIMEFRAMES: dict[str, tuple[int, int]] = {
    "1m": (1, 60),
    "5m": (5, 300),
    "15m": (15, 900),
    "30m": (30, 1800),
    "1h": (16385, 3600),
    "4h": (16388, 14400),
    "1d": (16408, 86400),
    "1w": (32769, 604800),
    "1mn": (49153, 2592000),
}


class MT5Error(Exception):
    """Base class: a typed refusal from the MT5 integration."""

    def __init__(self, reason: str, detail: str = ""):
        self.reason = reason
        self.detail = detail
        super().__init__(reason if not detail else f"{reason}: {detail}")


class MT5ModuleUnavailable(MT5Error):
    """The ``MetaTrader5`` package is not importable in this interpreter."""


class MT5TerminalUnavailable(MT5Error):
    """The terminal could not be initialised (missing, wrong build, refused)."""


class MT5NotConnected(MT5Error):
    """A call needed a live terminal session, and there is none."""


class MT5DataRefused(MT5Error):
    """Market data was missing, malformed, or from an unconfigured symbol."""


def import_mt5() -> Any:
    """Import the official package, or say precisely why it is missing."""

    try:  # optional dependency
        import MetaTrader5 as mt5  # type: ignore[import-not-found]
    except Exception as exc:  # ImportError, or a broken install
        raise MT5ModuleUnavailable(
            "metatrader5_package_not_installed", type(exc).__name__
        ) from exc
    return mt5


def _field(value: Any, name: str, default: Any = None) -> Any:
    """Read a named field from a namedtuple, dict, or attribute object."""

    if value is None:
        return default
    if isinstance(value, Mapping):
        return value.get(name, default)
    if hasattr(value, name):
        return getattr(value, name)
    try:
        return value[name]
    except Exception:
        return default


def _sequence(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (str, bytes)):
        return []
    try:
        return list(value)
    except TypeError:
        return []


def _utc(value: Any) -> datetime | None:
    """MT5 times are epoch seconds in the *server* clock, treated as UTC.

    The terminal reports broker-server time.  AXIOM records what the broker
    said plus the declared offset, and never silently shifts it: callers get
    naive-UTC semantics and the offset is carried in the manifest/metadata.
    """

    if isinstance(value, datetime):
        return value.astimezone(timezone.utc) if value.tzinfo else value.replace(
            tzinfo=timezone.utc
        )
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    if seconds <= 0:
        return None
    try:
        return datetime.fromtimestamp(seconds, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _decimal(value: Any, default: Decimal | None = None) -> Decimal | None:
    if value is None:
        return default
    try:
        result = Decimal(str(value))
    except Exception:
        return default
    return result if result.is_finite() else default


@dataclass(frozen=True)
class MT5SymbolInfo:
    """The broker's own symbol specification, exactly as reported."""

    name: str
    description: str
    digits: int
    point: Decimal
    trade_contract_size: Decimal
    volume_min: Decimal
    volume_max: Decimal
    volume_step: Decimal
    currency_base: str
    currency_profit: str
    currency_margin: str | None
    trade_mode: int
    filling_mask: int
    spread: int
    stops_level: int
    freeze_level: int
    bid: Decimal | None
    ask: Decimal | None
    time: datetime | None
    raw_keys: tuple[str, ...] = ()

    @property
    def tradable(self) -> bool:
        return self.trade_mode == SYMBOL_TRADE_MODE_FULL

    def audit_view(self) -> dict[str, Any]:
        return {
            "symbol": self.name,
            "description": self.description,
            "digits": self.digits,
            "point": self.point,
            "trade_contract_size": self.trade_contract_size,
            "volume_min": self.volume_min,
            "volume_max": self.volume_max,
            "volume_step": self.volume_step,
            "currency_base": self.currency_base,
            "currency_profit": self.currency_profit,
            "currency_margin": self.currency_margin,
            "trade_mode": self.trade_mode,
            "trade_mode_name": symbol_trade_mode_name(self.trade_mode),
            "filling_mask": self.filling_mask,
            "stops_level": self.stops_level,
            "freeze_level": self.freeze_level,
            "source": "mt5_symbol_info",
        }


def symbol_trade_mode_name(mode: int) -> str:
    return {
        SYMBOL_TRADE_MODE_DISABLED: "DISABLED",
        SYMBOL_TRADE_MODE_LONGONLY: "LONG_ONLY",
        SYMBOL_TRADE_MODE_SHORTONLY: "SHORT_ONLY",
        SYMBOL_TRADE_MODE_CLOSEONLY: "CLOSE_ONLY",
        SYMBOL_TRADE_MODE_FULL: "FULL",
    }.get(int(mode), f"UNKNOWN_{mode}")


@dataclass(frozen=True)
class MT5Tick:
    symbol: str
    time: datetime
    bid: Decimal
    ask: Decimal
    last: Decimal | None
    volume: Decimal | None


@dataclass(frozen=True)
class MT5TerminalStatus:
    """Terminal + account snapshot.  No secret and no balance guesswork."""

    connected: bool
    terminal_connected: bool
    trade_allowed: bool
    tradeapi_disabled: bool
    build: int
    company: str
    terminal_name: str
    path_configured: bool
    server: str
    account_login: int
    account_server: str
    account_name: str
    account_currency: str
    currency_digits: int
    trade_mode: int
    trade_mode_name: str
    account_mode: str
    account_mode_confirmed: bool
    margin_mode: int
    margin_mode_name: str
    leverage: int
    fifo_close: bool
    balance: Decimal | None
    equity: Decimal | None
    margin: Decimal | None
    margin_free: Decimal | None
    margin_level: Decimal | None
    margin_so_call: Decimal | None
    margin_so_so: Decimal | None
    error_code: int
    error_text: str
    module_version: str

    def audit_view(self) -> dict[str, Any]:
        return {
            "connected": self.connected,
            "terminal_connected": self.terminal_connected,
            "trade_allowed": self.trade_allowed,
            "tradeapi_disabled": self.tradeapi_disabled,
            "build": self.build,
            "company": self.company,
            "terminal_name": self.terminal_name,
            "path_configured": self.path_configured,
            "server": self.server,
            "account_login": self.account_login,
            "account_server": self.account_server,
            "account_name": self.account_name,
            "account_currency": self.account_currency,
            "currency_digits": self.currency_digits,
            "trade_mode": self.trade_mode,
            "trade_mode_name": self.trade_mode_name,
            "account_mode": self.account_mode,
            "account_mode_confirmed": self.account_mode_confirmed,
            "margin_mode": self.margin_mode,
            "margin_mode_name": self.margin_mode_name,
            "leverage": self.leverage,
            "fifo_close": self.fifo_close,
            "balance": self.balance,
            "equity": self.equity,
            "margin": self.margin,
            "margin_free": self.margin_free,
            "margin_level": self.margin_level,
            "margin_so_call": self.margin_so_call,
            "margin_so_so": self.margin_so_so,
            "error_code": self.error_code,
            "error_text": self.error_text,
            "module_version": self.module_version,
            "source": "mt5_terminal_info",
        }


class MT5Terminal:
    """One terminal session, opened explicitly and never at import time."""

    def __init__(
        self,
        settings: MT5Settings,
        *,
        module: Any,
        clock: Any | None = None,
    ):
        if module is None:
            raise MT5ModuleUnavailable("metatrader5_module_not_supplied")
        self.settings = settings
        self.module = module
        self.clock = clock
        self._connected = False

    # -- lifecycle ------------------------------------------------------

    @property
    def connected(self) -> bool:
        return self._connected

    def connect(self, *, password: str | None = None) -> MT5TerminalStatus:
        """Open the terminal session.  Sends no order of any kind."""

        result = self.settings.validate()
        if not result.valid:
            raise MT5TerminalUnavailable("mt5_settings_invalid", result.reason)
        kwargs: dict[str, Any] = {"timeout": int(self.settings.timeout_ms)}
        if password:
            # Only passed when the terminal is not already logged in; the value
            # is used in this call and never retained.
            kwargs["login"] = int(self.settings.account_login)
            kwargs["password"] = password
            kwargs["server"] = self.settings.server
        try:
            initialized = self.module.initialize(self.settings.terminal_path, **kwargs)
        except Exception as exc:
            raise MT5TerminalUnavailable(
                "mt5_initialize_failed", type(exc).__name__
            ) from exc
        if not initialized:
            code, text = self._last_error()
            raise MT5TerminalUnavailable("mt5_initialize_refused", f"{code}:{text}")
        self._connected = True
        status = self.status()
        if not status.terminal_connected:
            self.disconnect()
            raise MT5TerminalUnavailable("mt5_terminal_not_connected")
        if status.account_login != int(self.settings.account_login):
            self.disconnect()
            raise MT5TerminalUnavailable(
                "mt5_account_mismatch",
                f"terminal={status.account_login} configured={self.settings.account_login}",
            )
        if status.account_server.strip().lower() != self.settings.server.strip().lower():
            self.disconnect()
            raise MT5TerminalUnavailable(
                "mt5_server_mismatch",
                f"terminal={status.account_server} configured={self.settings.server}",
            )
        return status

    def disconnect(self) -> None:
        if not self._connected:
            return
        try:
            self.module.shutdown()
        except Exception:
            pass
        self._connected = False

    def _require_connection(self) -> None:
        if not self._connected:
            raise MT5NotConnected("mt5_not_connected")

    def _last_error(self) -> tuple[int, str]:
        try:
            error = self.module.last_error()
        except Exception:
            return (0, "")
        code = _field(error, "code", 0) if not isinstance(error, tuple) else (
            error[0] if error else 0
        )
        text = ""
        if isinstance(error, tuple) and len(error) > 1:
            text = str(error[1])
        else:
            text = str(_field(error, "description", "") or "")
        try:
            return (int(code), text[:200])
        except (TypeError, ValueError):
            return (0, text[:200])

    # -- status ---------------------------------------------------------

    def status(self) -> MT5TerminalStatus:
        self._require_connection()
        code, text = 0, ""
        try:
            terminal = self.module.terminal_info()
            account = self.module.account_info()
        except Exception as exc:
            raise MT5Error("mt5_status_unavailable", type(exc).__name__) from exc
        if terminal is None:
            code, text = self._last_error()
        trade_mode = int(_field(account, "trade_mode", -1) or 0)
        margin_mode = int(_field(account, "margin_mode", -1) or 0)
        return MT5TerminalStatus(
            connected=terminal is not None and account is not None,
            terminal_connected=bool(_field(terminal, "connected", False)),
            trade_allowed=bool(_field(terminal, "trade_allowed", False))
            and bool(_field(account, "trade_allowed", False)),
            tradeapi_disabled=bool(_field(terminal, "tradeapi_disabled", True)),
            build=int(_field(terminal, "build", 0) or 0),
            company=str(_field(account, "company", "") or _field(terminal, "company", "") or ""),
            terminal_name=str(_field(terminal, "name", "") or ""),
            path_configured=bool(str(self.settings.terminal_path).strip()),
            server=str(self.settings.server),
            account_login=int(_field(account, "login", 0) or 0),
            account_server=str(_field(account, "server", "") or ""),
            account_name=str(_field(account, "name", "") or ""),
            account_currency=str(_field(account, "currency", "") or ""),
            currency_digits=int(_field(account, "currency_digits", 0) or 0),
            trade_mode=trade_mode,
            trade_mode_name=account_mode_name(trade_mode),
            account_mode=account_mode_name(trade_mode),
            account_mode_confirmed=account_mode_name(trade_mode)
            != ACCOUNT_MODE_UNCONFIRMED,
            margin_mode=margin_mode,
            margin_mode_name={
                ACCOUNT_MARGIN_MODE_RETAIL_NETTING: "RETAIL_NETTING",
                ACCOUNT_MARGIN_MODE_EXCHANGE: "EXCHANGE",
                ACCOUNT_MARGIN_MODE_RETAIL_HEDGING: "RETAIL_HEDGING",
            }.get(margin_mode, f"UNKNOWN_{margin_mode}"),
            leverage=int(_field(account, "leverage", 0) or 0),
            fifo_close=bool(_field(account, "fifo_close", False)),
            balance=_decimal(_field(account, "balance")),
            equity=_decimal(_field(account, "equity")),
            margin=_decimal(_field(account, "margin")),
            margin_free=_decimal(_field(account, "margin_free")),
            margin_level=_decimal(_field(account, "margin_level")),
            margin_so_call=_decimal(_field(account, "margin_so_call")),
            margin_so_so=_decimal(_field(account, "margin_so_so")),
            error_code=code,
            error_text=text,
            module_version=str(getattr(self.module, "__version__", "") or ""),
        )

    # -- symbols --------------------------------------------------------

    def symbol_names(self, group: str | None = None) -> list[str]:
        self._require_connection()
        try:
            result = (
                self.module.symbols_get(group) if group else self.module.symbols_get()
            )
        except Exception as exc:
            raise MT5DataRefused("mt5_symbols_unavailable", type(exc).__name__) from exc
        names: list[str] = []
        for entry in _sequence(result):
            name = entry if isinstance(entry, str) else _field(entry, "name")
            if isinstance(name, str) and name.strip():
                names.append(name.strip())
        return sorted(set(names))

    def symbol_info(self, symbol: str) -> MT5SymbolInfo:
        self._require_connection()
        try:
            info = self.module.symbol_info(symbol)
        except Exception as exc:
            raise MT5DataRefused("mt5_symbol_info_failed", type(exc).__name__) from exc
        if info is None:
            raise MT5DataRefused("mt5_symbol_not_found", symbol)
        # Some brokers hide a symbol until it is selected in Market Watch.
        try:
            self.module.symbol_select(symbol, True)
        except Exception:
            pass
        point = _decimal(_field(info, "point"))
        contract = _decimal(_field(info, "trade_contract_size"))
        volume_min = _decimal(_field(info, "volume_min"))
        volume_max = _decimal(_field(info, "volume_max"))
        volume_step = _decimal(_field(info, "volume_step"))
        if None in (point, contract, volume_min, volume_max, volume_step):
            raise MT5DataRefused("mt5_symbol_spec_incomplete", symbol)
        raw_keys = ()
        if hasattr(info, "_fields"):
            raw_keys = tuple(str(name) for name in info._fields)
        elif isinstance(info, Mapping):
            raw_keys = tuple(sorted(str(key) for key in info))
        return MT5SymbolInfo(
            name=str(_field(info, "name", symbol) or symbol),
            description=str(_field(info, "description", "") or ""),
            digits=int(_field(info, "digits", 0) or 0),
            point=point,
            trade_contract_size=contract,
            volume_min=volume_min,
            volume_max=volume_max,
            volume_step=volume_step,
            currency_base=str(_field(info, "currency_base", "") or ""),
            currency_profit=str(_field(info, "currency_profit", "") or ""),
            currency_margin=(
                str(_field(info, "currency_margin")) if _field(info, "currency_margin") else None
            ),
            trade_mode=int(_field(info, "trade_mode", 0) or 0),
            filling_mask=int(_field(info, "filling_mode", 0) or 0),
            spread=int(_field(info, "spread", 0) or 0),
            stops_level=int(_field(info, "trade_stops_level", 0) or 0),
            freeze_level=int(_field(info, "trade_freeze_level", 0) or 0),
            bid=_decimal(_field(info, "bid")),
            ask=_decimal(_field(info, "ask")),
            time=_utc(_field(info, "time")),
            raw_keys=raw_keys,
        )

    def resolve_symbol(self, spec: CurrencyPairSpec) -> str:
        """Canonical pair -> this broker's symbol, or fail closed.

        Resolution order: the catalog's explicit MT5 alias; the canonical pair
        if the broker lists it; the canonical pair plus the configured suffix.
        A missing symbol is never guessed (no fuzzy matching, no suffix search).
        """

        alias = None
        aliases = getattr(spec, "provider_aliases", None) or {}
        for key in ("mt5", "MT5", "metaquotes", "MetaQuotes"):
            if key in aliases:
                alias = str(aliases[key]).strip()
                break
        names = set(self.symbol_names())
        candidates: list[str] = []
        if alias:
            candidates.append(alias)
        canonical = str(
            getattr(spec, "canonical_pair", None) or spec.pair
        ).replace("/", "").replace("-", "").replace("_", "").strip().upper()
        candidates.append(canonical)
        if self.settings.symbol_suffix:
            candidates.append(canonical + self.settings.symbol_suffix)
        for candidate in candidates:
            if candidate in names:
                return candidate
        raise MT5DataRefused(
            "mt5_symbol_unresolved",
            f"pair={canonical} tried={candidates}",
        )

    # -- market data ----------------------------------------------------

    def tick(self, symbol: str) -> MT5Tick:
        self._require_connection()
        try:
            raw = self.module.symbol_info_tick(symbol)
        except Exception as exc:
            raise MT5DataRefused("mt5_tick_failed", type(exc).__name__) from exc
        if raw is None:
            raise MT5DataRefused("mt5_tick_unavailable", symbol)
        bid = _decimal(_field(raw, "bid"))
        ask = _decimal(_field(raw, "ask"))
        when = _utc(_field(raw, "time"))
        if bid is None or ask is None or bid <= 0 or ask <= 0 or ask < bid:
            raise MT5DataRefused("mt5_tick_invalid", symbol)
        if when is None:
            raise MT5DataRefused("mt5_tick_missing_time", symbol)
        return MT5Tick(
            symbol=symbol,
            time=when,
            bid=bid,
            ask=ask,
            last=_decimal(_field(raw, "last")),
            volume=_decimal(_field(raw, "volume_real")) or _decimal(_field(raw, "volume")),
        )

    def quote(self, spec: CurrencyPairSpec, symbol: str) -> ForexQuote:
        tick = self.tick(symbol)
        return ForexQuote(
            pair=str(spec.pair),
            timestamp=tick.time,
            bid=tick.bid,
            ask=tick.ask,
            source="mt5_terminal",
            quote_id=f"mt5:{symbol}:{int(tick.time.timestamp())}",
            received_at=tick.time,
            bid_size=tick.volume,
            ask_size=tick.volume,
            metadata={
                "provider_symbol": symbol,
                "price_basis": "BID_ASK",
                "time_basis": "broker_server_utc_label",
                "data_class": "LIVE_BROKER_TICK",
            },
        )

    def ticks(
        self, symbol: str, start: datetime, end: datetime
    ) -> list[MT5Tick]:
        self._require_connection()
        try:
            raw = self.module.copy_ticks_range(
                symbol, start, end, COPY_TICKS_ALL
            )
        except Exception as exc:
            raise MT5DataRefused("mt5_tick_history_failed", type(exc).__name__) from exc
        ticks: list[MT5Tick] = []
        for entry in _sequence(raw):
            bid = _decimal(_field(entry, "bid"))
            ask = _decimal(_field(entry, "ask"))
            when = _utc(_field(entry, "time"))
            if bid is None or ask is None or when is None:
                continue
            if bid <= 0 or ask <= 0 or ask < bid:
                continue
            ticks.append(
                MT5Tick(
                    symbol=symbol,
                    time=when,
                    bid=bid,
                    ask=ask,
                    last=_decimal(_field(entry, "last")),
                    volume=_decimal(_field(entry, "volume_real"))
                    or _decimal(_field(entry, "volume")),
                )
            )
        ticks.sort(key=lambda item: item.time)
        return ticks

    def rates(
        self,
        symbol: str,
        timeframe: str,
        *,
        start: datetime,
        end: datetime,
    ):
        """Broker OHLC rates.  Returned as raw records, deliberately.

        MT5 rate bars carry *one* price series (bid).  AXIOM's
        :class:`ForexQuoteBar` requires separate bid and ask OHLC, so these rows
        are never reinterpreted as a two-sided bar.  They are exposed for
        research/diagnostics with an explicit ``price_basis='BID_ONLY'`` label;
        executable decision data comes from ticks.
        """

        self._require_connection()
        if timeframe not in TIMEFRAMES:
            raise MT5DataRefused("unsupported_timeframe", timeframe)
        mt5_timeframe = TIMEFRAMES[timeframe][0]
        try:
            raw = self.module.copy_rates_range(
                symbol, mt5_timeframe, start, end
            )
        except Exception as exc:
            raise MT5DataRefused("mt5_rates_failed", type(exc).__name__) from exc
        rows: list[dict[str, Any]] = []
        for entry in _sequence(raw):
            when = _utc(_field(entry, "time"))
            if when is None:
                continue
            rows.append(
                {
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "timestamp": when,
                    "duration_seconds": TIMEFRAMES[timeframe][1],
                    "open": _decimal(_field(entry, "open")),
                    "high": _decimal(_field(entry, "high")),
                    "low": _decimal(_field(entry, "low")),
                    "close": _decimal(_field(entry, "close")),
                    "tick_volume": _field(entry, "tick_volume"),
                    "real_volume": _field(entry, "real_volume"),
                    "spread_points": _field(entry, "spread"),
                    "price_basis": "BID_ONLY",
                    "source": "mt5_copy_rates_range",
                    "complete": _utc(_field(entry, "time")) is not None,
                }
            )
        rows.sort(key=lambda item: item["timestamp"])
        return rows


def bar_boundaries(
    start: datetime, end: datetime, duration_seconds: int
) -> Iterable[tuple[datetime, datetime]]:
    """Half-open ``[bucket_start, bucket_end)`` boundaries aligned to ``start``."""

    if duration_seconds <= 0:
        raise ValueError("duration_seconds must be positive")
    cursor = start
    delta = timedelta(seconds=duration_seconds)
    while cursor < end:
        nxt = cursor + delta
        yield cursor, min(nxt, end) if nxt > end else nxt
        cursor = nxt


def aggregate_ticks_to_bars(
    spec: CurrencyPairSpec,
    symbol: str,
    timeframe: str,
    ticks: Sequence[MT5Tick],
    *,
    start: datetime,
    end: datetime,
):
    """Build two-sided OHLC bars from ticks; never invent a missing side.

    A bucket is ``complete`` only when it is fully inside the requested window
    *and* at least one tick arrived after the bucket closed (proving the feed
    reached past its boundary).  Buckets without ticks are omitted rather than
    synthesised -- a gap must stay visible to the data quality layer.
    """

    from app.data.models import ForexQuoteBar, QuoteSideBar  # local: avoid cycle

    if timeframe not in TIMEFRAMES:
        raise MT5DataRefused("unsupported_timeframe", timeframe)
    duration = TIMEFRAMES[timeframe][1]
    bars = []
    ordered = sorted(ticks, key=lambda item: item.time)
    last_tick_at = ordered[-1].time if ordered else None
    for bucket_start, bucket_end in bar_boundaries(start, end, duration):
        bucket = [
            tick
            for tick in ordered
            if bucket_start <= tick.time < bucket_end
        ]
        if not bucket:
            continue
        def side(values):
            return QuoteSideBar(
                open=values[0],
                high=max(values),
                low=min(values),
                close=values[-1],
            )

        complete = (
            bucket_end <= end
            and last_tick_at is not None
            and last_tick_at >= bucket_end
        )
        bars.append(
            ForexQuoteBar(
                pair=str(spec.pair),
                timeframe=timeframe,
                timestamp=bucket_start,
                duration_seconds=duration,
                bid=side([tick.bid for tick in bucket]),
                ask=side([tick.ask for tick in bucket]),
                source="mt5_terminal_ticks",
                quote_count=len(bucket),
                last_quote_at=bucket[-1].time,
                complete=complete,
                volume=sum(
                    (tick.volume for tick in bucket if tick.volume is not None),
                    Decimal(0),
                ),
                volume_kind="tick_volume",
            )
        )
    return bars

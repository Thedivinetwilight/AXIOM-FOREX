"""A fake ``MetaTrader5`` module.

No test in this repository may reach a real terminal, a real account, or the
installed package: every MT5 test builds :class:`FakeMT5` and injects it.  The
fake mirrors the official API's shapes (namedtuple-style results, ``None`` +
``last_error()`` on failure, retcodes) closely enough that the adapter's real
code paths run unchanged.

It also records everything the adapter did, which is how the tests prove the
safety properties: ``initialize_calls``, ``send_requests``, and the repository
state *observed at the instant* ``order_send`` was called.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Callable, Iterable, Mapping, Sequence


def _ns(**fields: Any) -> SimpleNamespace:
    return SimpleNamespace(**fields)


DEFAULT_SYMBOL = {
    "name": "EURUSD.pro",
    "description": "Euro vs US Dollar",
    "digits": 5,
    "point": 0.00001,
    "trade_contract_size": 100000.0,
    "volume_min": 0.01,
    "volume_max": 100.0,
    "volume_step": 0.01,
    "currency_base": "EUR",
    "currency_profit": "USD",
    "currency_margin": "EUR",
    "trade_mode": 4,
    "filling_mode": 1,
    "spread": 12,
    "trade_stops_level": 0,
    "trade_freeze_level": 0,
    "bid": 1.10000,
    "ask": 1.10020,
    "time": 1_772_000_000,
}


class FakeMT5:
    """Scriptable stand-in for the official MetaTrader5 module."""

    __version__ = "5.0.45"
    TIMEFRAME_M1 = 1
    TIMEFRAME_H1 = 16385
    COPY_TICKS_ALL = 0
    ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1
    TRADE_ACTION_DEAL = 1
    POSITION_TYPE_BUY = 0
    POSITION_TYPE_SELL = 1

    def __init__(
        self,
        *,
        account_login: int = 55500123,
        account_server: str = "Broker-Demo",
        currency: str = "USD",
        currency_digits: int = 2,
        leverage: int = 100,
        margin_mode: int = 2,  # RETAIL_HEDGING
        trade_allowed: bool = True,
        terminal_connected: bool = True,
        balance: float = 10_000.0,
        equity: float = 10_000.0,
        margin: float = 0.0,
        margin_free: float = 10_000.0,
        trade_mode: int = 0,  # DEMO
        symbols: Mapping[str, Mapping[str, Any]] | None = None,
        default_symbol: bool = True,
        ticks: Sequence[Mapping[str, Any]] | None = None,
        rates: Sequence[Mapping[str, Any]] | None = None,
        positions: Sequence[Mapping[str, Any]] | None = None,
        history_orders: Sequence[Mapping[str, Any]] | None = None,
        history_deals: Sequence[Mapping[str, Any]] | None = None,
    ):
        self.account_login = account_login
        self.account_server = account_server
        self.currency = currency
        self.currency_digits = currency_digits
        self.leverage = leverage
        self.margin_mode = margin_mode
        self.trade_allowed = trade_allowed
        self.terminal_connected = terminal_connected
        self.balance = balance
        self.equity = equity
        self.margin = margin
        self.margin_free = margin_free
        self.trade_mode = trade_mode
        self.symbols: dict[str, dict[str, Any]] = {}
        if default_symbol:
            self.symbols["EURUSD.pro"] = dict(DEFAULT_SYMBOL)
        for name, override in (symbols or {}).items():
            base = dict(DEFAULT_SYMBOL)
            base.update(override)
            base["name"] = name
            self.symbols[name] = base
        self.ticks = [dict(item) for item in (ticks or [])]
        self.rates = [dict(item) for item in (rates or [])]
        self.positions = [dict(item) for item in (positions or [])]
        self.history_orders = [dict(item) for item in (history_orders or [])]
        self.history_deals = [dict(item) for item in (history_deals or [])]

        # -- scripted behaviour -----------------------------------------
        self.initialize_result = True
        self.initialize_calls: list[tuple[str, dict[str, Any]]] = []
        self.login_calls: list[dict[str, Any]] = []
        self.shutdown_calls = 0
        self.send_requests: list[dict[str, Any]] = []
        #: Each entry is a result dict, ``None`` (module returns None), or a
        #: callable receiving the request.
        self.send_script: list[Any] = []
        self.default_send_result: Any = {
            "retcode": 10009,
            "order": 7001,
            "deal": 8001,
            "volume": None,  # filled in from the request
            "price": None,
            "bid": 1.1,
            "ask": 1.1002,
            "comment": "done",
            "request_id": 1,
            "retcode_external": 0,
        }
        self.check_result: dict[str, Any] | None = {
            "retcode": 0,
            "comment": "ok",
            "margin": 100.0,
        }
        self.history_available = True
        self.last_error_value = (0, "ok")
        self.fail_symbols_get = False
        #: Real ``symbols_get`` returns complete records; a wrapper in the wild may
        #: return bare names, which is what this switch models.
        self.symbols_get_names_only = False
        #: Every ``symbol_select`` call, so a test can prove reading a specification
        #: never writes to the operator's Market Watch.
        self.symbol_select_calls: list[tuple[str, bool]] = []
        self.fail_ticks = False
        self.fail_history = False
        #: Called with the request inside ``order_send`` before answering.
        self.on_send: Callable[[Mapping[str, Any]], None] | None = None

    # -- session --------------------------------------------------------

    def initialize(self, path: str, **kwargs: Any) -> bool:
        self.initialize_calls.append((path, dict(kwargs)))
        return self.initialize_result

    def login(self, login: int, password: str | None = None, server: str | None = None) -> bool:
        self.login_calls.append({"login": login, "server": server})
        return True

    def shutdown(self) -> None:
        self.shutdown_calls += 1

    def last_error(self) -> tuple[int, str]:
        return self.last_error_value

    def terminal_info(self):
        return _ns(
            community_account=False,
            community_connection=False,
            connected=self.terminal_connected,
            dlls_allowed=False,
            trade_allowed=self.trade_allowed,
            tradeapi_disabled=False,
            build=4800,
            company="Broker Ltd",
            name="MetaTrader 5",
            path="C:/Terminal/terminal64.exe",
        )

    def account_info(self):
        return _ns(
            login=self.account_login,
            trade_mode=self.trade_mode,
            leverage=self.leverage,
            limit_orders=200,
            margin_so_mode=0,
            trade_allowed=self.trade_allowed,
            trade_expert=True,
            margin_mode=self.margin_mode,
            currency_digits=self.currency_digits,
            fifo_close=False,
            balance=self.balance,
            credit=0.0,
            profit=0.0,
            equity=self.equity,
            margin=self.margin,
            margin_free=self.margin_free,
            margin_level=0.0,
            margin_so_call=50.0,
            margin_so_so=30.0,
            name="Operator Account",
            server=self.account_server,
            currency=self.currency,
            company="Broker Ltd",
        )

    # -- symbols --------------------------------------------------------

    def symbols_get(self, group: str | None = None):
        if self.fail_symbols_get:
            self.last_error_value = (1, "symbols unavailable")
            return None
        if self.symbols_get_names_only:
            return tuple(_ns(name=name) for name in sorted(self.symbols))
        # The official integration returns each symbol's whole record here.
        return tuple(_ns(**self.symbols[name]) for name in sorted(self.symbols))

    def symbol_select(self, symbol: str, enable: bool = True) -> bool:
        self.symbol_select_calls.append((symbol, bool(enable)))
        return symbol in self.symbols

    def symbol_info(self, symbol: str):
        entry = self.symbols.get(symbol)
        if entry is None:
            self.last_error_value = (4301, "unknown symbol")
            return None
        return _ns(**entry)

    def symbol_info_tick(self, symbol: str):
        entry = self.symbols.get(symbol)
        if entry is None:
            return None
        return _ns(
            time=entry["time"],
            bid=entry["bid"],
            ask=entry["ask"],
            last=entry["bid"],
            volume=1,
            time_msc=entry["time"] * 1000,
            flags=6,
            volume_real=1.0,
        )

    def copy_ticks_range(self, symbol: str, start, end, flags):
        if self.fail_ticks:
            self.last_error_value = (4401, "history unavailable")
            return None
        rows = []
        for item in self.ticks:
            when = item["time"]
            if isinstance(when, datetime):
                seconds = int(when.timestamp())
            else:
                seconds = int(when)
            if int(start.timestamp()) <= seconds < int(end.timestamp()):
                rows.append(_ns(**item))
        return rows

    def copy_rates_range(self, symbol: str, timeframe, start, end):
        return [_ns(**item) for item in self.rates]

    # -- orders ---------------------------------------------------------

    def order_calc_margin(self, action, symbol, volume, price):
        entry = self.symbols.get(symbol)
        if entry is None:
            return None
        contract = float(entry["trade_contract_size"])
        return float(volume) * float(price) * contract / float(self.leverage)

    def order_calc_profit(self, action, symbol, volume, price_open, price_close):
        entry = self.symbols.get(symbol)
        contract = float(entry["trade_contract_size"])
        direction = 1 if int(action) == self.ORDER_TYPE_BUY else -1
        return (float(price_close) - float(price_open)) * float(volume) * contract * direction

    def order_check(self, request: Mapping[str, Any]):
        if self.check_result is None:
            self.last_error_value = (4756, "check failed")
            return None
        return _ns(**self.check_result)

    def order_send(self, request: Mapping[str, Any]):
        self.send_requests.append(dict(request))
        if self.on_send is not None:
            self.on_send(request)
        scripted = self.send_script.pop(0) if self.send_script else self.default_send_result
        if callable(scripted):
            scripted = scripted(request)
        if scripted == "raise":
            raise RuntimeError("terminal disconnected")
        if scripted is None:
            return None
        result = dict(scripted)
        result.setdefault("request_id", 1)
        result.setdefault("comment", "")
        if result.get("retcode") in (10008, 10009, 10010) and result.get("volume") is None:
            result["volume"] = request.get("volume", 0.0)
        if result.get("price") is None and "price" in request:
            result["price"] = request["price"]
        return _ns(**result)

    def positions_get(self, *, symbol: str | None = None, ticket: int | None = None):
        rows = []
        for item in self.positions:
            if symbol is not None and item.get("symbol") != symbol:
                continue
            if ticket is not None and int(item.get("ticket", 0)) != int(ticket):
                continue
            rows.append(_ns(**item))
        return tuple(rows)

    def orders_get(self, **kwargs: Any):
        return ()

    # -- history --------------------------------------------------------

    @staticmethod
    def _in_window(item: Mapping[str, Any], args: Sequence[Any], kwargs: Mapping[str, Any]) -> bool:
        ticket = kwargs.get("ticket")
        position = kwargs.get("position")
        if ticket is not None:
            return int(item.get("ticket", 0)) == int(ticket)
        if position is not None:
            return int(item.get("position_id", 0)) == int(position)
        if len(args) >= 2:
            start, end = args[0], args[1]
            when = item.get(
                "time", item.get("time_setup", item.get("time_done", 0))
            )
            if isinstance(when, datetime):
                seconds = when.timestamp()
            else:
                seconds = float(when)
            return float(start.timestamp()) <= seconds <= float(end.timestamp())
        return True

    def history_orders_get(self, *args: Any, **kwargs: Any):
        if self.fail_history:
            self.last_error_value = (4401, "no history")
            return None
        rows = [
            _ns(**item)
            for item in self.history_orders
            if self._in_window(item, args, kwargs)
        ]
        if not rows and kwargs.get("ticket"):
            return None
        return tuple(rows)

    def history_deals_get(self, *args: Any, **kwargs: Any):
        if self.fail_history:
            self.last_error_value = (4401, "no history")
            return None
        rows = [
            _ns(**item)
            for item in self.history_deals
            if self._in_window(item, args, kwargs)
        ]
        if not rows and kwargs.get("ticket"):
            return None
        return tuple(rows)


def make_history_deal(
    *,
    ticket: int,
    order: int,
    position_id: int,
    symbol: str,
    side: str,
    entry: int,
    volume: float,
    price: float,
    magic: int,
    comment: str,
    time: datetime | int,
) -> dict[str, Any]:
    return {
        "ticket": ticket,
        "order": order,
        "position_id": position_id,
        "symbol": symbol,
        "type": 0 if side.upper() == "BUY" else 1,
        "entry": entry,
        "volume": volume,
        "price": price,
        "profit": 0.0,
        "swap": 0.0,
        "commission": -0.7,
        "magic": magic,
        "comment": comment,
        "time": time,
    }


def make_history_order(
    *,
    ticket: int,
    symbol: str,
    side: str,
    volume: float,
    state: int,
    magic: int,
    comment: str,
    time: datetime | int,
    price_open: float = 1.1,
) -> dict[str, Any]:
    return {
        "ticket": ticket,
        "symbol": symbol,
        "type": 0 if side.upper() == "BUY" else 1,
        "volume_initial": volume,
        "volume_current": volume,
        "price_open": price_open,
        "state": state,
        "magic": magic,
        "comment": comment,
        "time_setup": time,
        "time_done": time,
    }


def make_position(
    *,
    ticket: int,
    symbol: str,
    side: str,
    volume: float,
    price_open: float = 1.1,
    price_current: float = 1.1,
    magic: int = 26081001,
    comment: str = "",
    identifier: int | None = None,
    time: datetime | int = 1_772_000_000,
) -> dict[str, Any]:
    return {
        "ticket": ticket,
        "symbol": symbol,
        "type": 0 if side.upper() == "BUY" else 1,
        "volume": volume,
        "price_open": price_open,
        "price_current": price_current,
        "sl": 0.0,
        "tp": 0.0,
        "swap": 0.0,
        "profit": 0.0,
        "magic": magic,
        "comment": comment,
        "identifier": identifier if identifier is not None else ticket,
        "time": time,
    }


def make_tick(time: datetime, bid: float, ask: float, volume: float = 1.0) -> dict[str, Any]:
    return {
        "time": time,
        "bid": bid,
        "ask": ask,
        "last": bid,
        "volume": volume,
        "volume_real": volume,
    }

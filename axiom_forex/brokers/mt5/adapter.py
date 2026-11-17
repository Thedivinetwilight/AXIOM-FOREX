"""The MT5 trading adapter: the only place AXIOM may place a real order.

Order of gates, in the order they are checked on every submission:

1. **local configuration** -- valid MT5 settings and ``TRADING_ENABLED``;
2. **local arming** -- an explicit in-process call that names the account and
   server; it never survives a restart and is dropped on any doubt;
3. **connection health** -- live terminal, trading allowed, exact account and
   server match;
4. **reconciliation health** -- no unresolved or in-flight MT5 order;
5. **strategy lifecycle** -- the owning strategy must be APPROVED *and* active;
6. **the existing hard-risk gate** -- ``GateDecision.allowed`` with an intent
   that matches the one being submitted, field for field;
7. **instrument terms** -- catalog spec agrees with the broker's symbol;
8. **price and margin** -- fresh tick within the drift allowance, margin
   computable and coverable;
9. **protective stop** -- a configured broker-side stop distance, no open
   position of ours left unprotected, and, after the send, the broker's own
   position record echoing that stop back.  An entry whose stop cannot be
   attached is refused; one whose stop the broker does not report is recorded
   as unconfirmed, trading is disarmed, and no further entry is sent.

Only then is the write-ahead record persisted, exactly one ``order_send`` is
issued, and the answer is classified.  An unanswered order is recorded as
``UNRESOLVED``, trading is disarmed, and nothing further is sent until
reconciliation explains it.

Connecting, loading data, and starting the application never send an order:
:meth:`connect` has no submission path, and ``_armed`` starts ``False``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable, Mapping, Sequence

from app.risk.models import ApprovedOrderIntent
from app.risk.pretrade import GateDecision
from app.universe.specs import CurrencyPairSpec

from ...config.mt5 import (
    MT5ExecutionMode,
    MT5PasswordStore,
    MT5Settings,
    MT5SettingsError,
    MT5TradingNotAuthorised,
    normalise_pair_key,
    parse_stop_distance,
)
from .conformance import conformance_requirements
from .account import AccountTerms, account_terms, margin_for_order
from .identity import account_fingerprint as fingerprint_for_login
from .orders import (
    MT5OrderCheck,
    MT5OrderOutcome,
    MT5Position,
    MT5ProtectionOutcome,
    TRADE_ACTION_SLTP,
    build_market_request,
    check_order,
    close_volume_check,
    filling_for,
    netting_conflict,
    open_positions,
    protective_stop_price,
    send_order,
    send_protection_modification,
    stop_distance_points,
    stop_loss_confirmed,
)
from .reconcile import (
    MT5_BLOCKING_STATES,
    MT5_FINAL_STATES,
    PositionReconciliation,
    RecoveryResult,
    reconcile_positions,
    reconcile_record,
)
from .symbols import (
    SymbolCrossCheck,
    broker_terms_candidate,
    require_data,
    require_trading,
    to_broker_volume,
)
from .terminal import (
    ACCOUNT_MODE_UNCONFIRMED,
    MT5DataRefused,
    MT5Error,
    MT5Terminal,
    MT5TerminalStatus,
    import_mt5,
)

CAPABILITY_VERSION = "1"
BROKER_ID = "mt5_terminal"


@dataclass(frozen=True)
class MT5BrokerCapabilities:
    """What this adapter can do.  Separate from the paper capability type.

    ``BrokerCapabilities`` in the kernel is deliberately paper-only and rejects
    live execution, so the live adapter declares its own capabilities instead of
    loosening that guard.
    """

    broker_id: str = BROKER_ID
    capability_version: str = CAPABILITY_VERSION
    execution_mode: str = "LIVE_TRADING"
    supports_live_orders: bool = True
    supports_partial_fills: bool = True
    supports_netting: bool = True
    supports_hedging: bool = True
    order_types: frozenset[str] = frozenset(
        {"MARKET_DEAL", "CLOSE_POSITION", "MODIFY_POSITION_PROTECTION"}
    )
    provider: str = "official_metatrader5_python"
    requires_installed_terminal: bool = True
    calendar_source: str = "broker_symbol_and_terminal"

    def audit_view(self) -> dict[str, Any]:
        return {
            "broker_id": self.broker_id,
            "capability_version": self.capability_version,
            "execution_mode": self.execution_mode,
            "supports_live_orders": self.supports_live_orders,
            "supports_partial_fills": self.supports_partial_fills,
            "supports_netting": self.supports_netting,
            "supports_hedging": self.supports_hedging,
            "order_types": sorted(self.order_types),
            "provider": self.provider,
            "requires_installed_terminal": self.requires_installed_terminal,
            "calendar_source": self.calendar_source,
        }


@dataclass(frozen=True)
class MT5OrderReceipt:
    """Durable record of one live submission attempt."""

    submission_id: str
    request_id: str
    state: str
    live: bool
    mode: str
    account_login: int
    account_server: str
    created_at: datetime
    decision_id: str
    outcome: MT5OrderOutcome
    capabilities: MT5BrokerCapabilities
    preflight: Mapping[str, Any] = field(default_factory=dict)

    def audit_view(self) -> dict[str, Any]:
        return {
            "submission_id": self.submission_id,
            "request_id": self.request_id,
            "state": self.state,
            "live": self.live,
            "mode": self.mode,
            "account_login": self.account_login,
            "account_server": self.account_server,
            "created_at": self.created_at,
            "decision_id": self.decision_id,
            "outcome": self.outcome.audit_view(),
            "preflight": dict(self.preflight),
            "capabilities": self.capabilities.audit_view(),
        }


class MT5SubmitRefused(MT5Error):
    """A live order was refused before submission.  Nothing was sent."""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class MT5TradingAdapter:
    """Metadata, market data, and live execution over one MT5 terminal."""

    def __init__(
        self,
        *,
        settings: MT5Settings,
        password_store: MT5PasswordStore,
        repository: Any,
        audit_log: Any,
        module: Any | None = None,
        lifecycle_lookup: Callable[[str], Any] | None = None,
        clock: Callable[[], datetime] | None = None,
        capabilities: MT5BrokerCapabilities | None = None,
        account_profile_check: Callable[[AccountTerms], Sequence[str]] | None = None,
        terminal_factory: Callable[[], Any] | None = None,
    ):
        self.settings = settings
        self.password_store = password_store
        self.repository = repository
        self.audit_log = audit_log
        self.module = module
        self.lifecycle_lookup = lifecycle_lookup
        self.clock = clock or _utc_now
        self.capabilities = capabilities or MT5BrokerCapabilities()
        self._terminal: MT5Terminal | None = None
        self._status: MT5TerminalStatus | None = None
        self._terms: AccountTerms | None = None
        #: Trading authorisation is the operator's local setting plus an
        #: explicit arming call.  It deliberately does NOT name an account:
        #: the account is whichever one the terminal has selected.
        self._armed = False
        self._armed_fingerprint: str | None = None
        self._account_validated: dict[str, Any] | None = None
        self.account_profile_check = account_profile_check
        #: When supplied, :meth:`connect` adopts the terminal this callable
        #: returns **if it is already connected** instead of opening a second
        #: session to the same terminal process.  One process, one terminal
        #: connection: a second ``initialize``/``shutdown`` pair would belong to
        #: the same process-wide session and could tear down the operator's
        #: observation connection underneath them.
        self.terminal_factory = terminal_factory
        self._symbol_cache: dict[str, tuple[str, Any]] = {}
        self._last_recovery: tuple[RecoveryResult, ...] = ()
        #: ``(login, server, trade_mode)`` of the account last observed.
        self._account_identity: tuple[int, str, int] | None = None
        self._account_changes: tuple[dict[str, Any], ...] = ()
        #: Last observed account fingerprint, kept across a disconnect so order
        #: records can still be attributed during reporting.
        self._fingerprint_cache: str = ""
        #: Request-id idempotency for non-volume SL/TP actions. Unresolved
        #: outcomes are reconciled against the position read-back, never resent.
        self._protection_modification_results: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}

    # -- connection -----------------------------------------------------

    def _module(self) -> Any:
        if self.module is None:
            self.module = import_mt5()
        return self.module

    @property
    def terminal(self) -> MT5Terminal:
        if self._terminal is None:
            raise MT5Error("mt5_not_connected")
        return self._terminal

    @property
    def connected(self) -> bool:
        return self._terminal is not None and self._terminal.connected

    @property
    def armed(self) -> bool:
        return self._armed

    def connect(self) -> MT5TerminalStatus:
        """Open the terminal and run recovery.  Never sends an order."""

        validation = self.settings.validate()
        if not validation.valid:
            raise MT5SubmitRefused("mt5_settings_invalid", validation.reason)
        self.disarm(reason="connect")
        adopted = self._adoptable_terminal()
        if adopted is not None:
            return self._finish_connect(adopted, adopted.status())
        terminal = MT5Terminal(self.settings, module=self._module(), clock=self.clock)
        try:
            status = terminal.connect()
        except MT5Error as exc:
            # Default behaviour: use the session the operator already opened.
            # A terminal that is not logged in is reported, never logged in
            # automatically and never prompted for a secret.
            if self.settings.use_existing_session or exc.reason not in {
                "mt5_terminal_not_connected",
                "mt5_account_mismatch",
                "mt5_server_mismatch",
            }:
                raise
            login_attempt = self.password_store.secret_for_login()
            status = terminal.connect(password=login_attempt)
            del login_attempt
        return self._finish_connect(terminal, status)

    def _adoptable_terminal(self) -> Any:
        """The already-connected terminal to share, or ``None``."""

        if self.terminal_factory is None:
            return None
        try:
            candidate = self.terminal_factory()
        except Exception:
            return None
        if candidate is None or not getattr(candidate, "connected", False):
            return None
        return candidate

    def _finish_connect(self, terminal: Any, status: Any) -> Any:
        self._terminal = terminal
        self._status = status
        self._terms = account_terms(status)
        self._account_identity = (
            status.account_login,
            status.account_server,
            status.trade_mode,
        )
        self._account_validated = None
        self._fingerprint_cache = self.account_fingerprint(status.account_login)
        self._symbol_cache.clear()
        self._audit(
            "MT5_TERMINAL_CONNECTED",
            allowed=True,
            account_login=status.account_login,
            account_server=status.account_server,
            trade_mode=status.trade_mode_name,
            margin_mode=status.margin_mode_name,
            trade_allowed=status.trade_allowed,
            mode=self.settings.mode_value,
            account_mode=status.account_mode,
            live=False,
        )
        self.validate_account_terms()
        self.recover()
        return status

    def disconnect(self, *, reason: str = "operator") -> None:
        self.disarm(reason=reason)
        if self._terminal is not None:
            self._terminal.disconnect()
        self._audit("MT5_TERMINAL_DISCONNECTED", allowed=True, reason=reason, live=False)
        self._terminal = None
        self._status = None
        self._terms = None
        self._symbol_cache.clear()

    # -- account mode and redaction -------------------------------------

    @property
    def armed_account_matches_current(self) -> bool:
        """Would an order go to the account arming was confirmed for?"""

        if not self._armed or not self._armed_fingerprint:
            return False
        if self._status is None:
            return False
        return self.account_fingerprint() == self._armed_fingerprint

    @property
    def account_mode(self) -> str:
        """The account mode the terminal reports right now, if known."""

        if self._status is None:
            return ACCOUNT_MODE_UNCONFIRMED
        return self._status.account_mode

    @property
    def account_mode_confirmed(self) -> bool:
        return bool(self._status and self._status.account_mode_confirmed)

    @property
    def account_is_demo(self) -> bool:
        """True only when the terminal positively says DEMO."""

        return self.account_mode == "DEMO"

    def account_fingerprint(self, login: int | None = None) -> str:
        """Stable, non-reversible account identifier for audits and reports."""

        if login is None:
            if self._status is not None:
                login = self._status.account_login
            elif self._account_identity is not None:
                login = self._account_identity[0]
            else:
                return ""
        return fingerprint_for_login(login)

    def redacted_account(self) -> dict[str, Any]:
        """The account facts an operator or report may see.  No identifiers."""

        if self._status is None or self._terms is None:
            return {
                "identified": False,
                "account_mode": ACCOUNT_MODE_UNCONFIRMED,
                "account_mode_confirmed": False,
                "balance_reporting": "withheld",
            }
        return {
            "identified": True,
            "login": "<redacted>",
            "account_fingerprint": self.account_fingerprint(),
            "server": self._status.account_server,
            "account_mode": self._status.account_mode,
            "account_mode_confirmed": self._status.account_mode_confirmed,
            "trade_mode": self._status.trade_mode,
            "is_demo": self.account_is_demo,
            "currency": self._terms.currency,
            "leverage": self._terms.leverage,
            "margin_mode": self._terms.margin_mode,
            "position_accounting": self._terms.position_accounting,
            "trade_allowed": self._terms.trade_allowed,
            # Balances, equity, margin, and free margin are never reported.
            "balance_reporting": "withheld",
            "profit_reporting": "withheld",
        }

    def validate_account_terms(self) -> dict[str, Any]:
        """Validate the *currently selected* account's own terms.

        Nothing here is inherited from another account: currency, leverage,
        margin/position accounting, and any declared profile are read from the
        terminal for this account and checked on their own.  Account mode is
        reported and audited, but it is **not** a trading gate: the account is
        the operator's choice in the terminal.
        """

        problems: list[str] = []
        notes: list[str] = []
        if self._status is None or self._terms is None:
            report = {
                "valid": False,
                "problems": ["account_terms_unavailable"],
                "notes": notes,
                "account": self.redacted_account(),
                "validated_at": self.clock(),
            }
            self._account_validated = report
            return report

        terms = self._terms
        if not terms.currency:
            problems.append("account_currency_missing")
        if terms.leverage <= 0:
            problems.append("account_leverage_missing")
        if str(terms.position_accounting).startswith("UNSUPPORTED"):
            problems.append("position_accounting_unsupported")
        if not terms.trade_allowed:
            problems.append("terminal_trading_not_allowed")
        if not self._status.account_mode_confirmed:
            # Visible and audited, deliberately not a blocker.
            notes.append(f"account_mode_unconfirmed:{self._status.account_mode}")

        declared_currency = self.settings.declared_account_currency
        if declared_currency and terms.currency != declared_currency.strip().upper():
            problems.append("account_currency_mismatch")
        declared_leverage = self.settings.declared_leverage
        if declared_leverage is not None and int(declared_leverage) != terms.leverage:
            problems.append("account_leverage_mismatch")
        if self.account_profile_check is not None:
            try:
                problems.extend(str(item) for item in self.account_profile_check(terms))
            except Exception as exc:
                problems.append(f"account_profile_check_failed:{type(exc).__name__}")

        report = {
            "valid": not problems,
            "problems": sorted(set(problems)),
            "notes": notes,
            "account": self.redacted_account(),
            "account_id": self.account_fingerprint(),
            "declared_account_currency": declared_currency,
            "declared_leverage": declared_leverage,
            "validated_at": self.clock(),
        }
        self._account_validated = report
        self._audit(
            "MT5_ACCOUNT_TERMS_VALIDATED",
            allowed=report["valid"],
            reason=",".join(report["problems"]) or "valid",
            account_mode=self._status.account_mode,
            account_server=self._status.account_server,
            account_currency=terms.currency,
            live=False,
        )
        return report

    def _ensure_account_current(self) -> dict[str, Any]:
        """Re-read the terminal account; revalidate it if it changed.

        This is what makes "the account is the one selected in MT5" safe rather
        than convenient: a switch is detected, audited, and triggers a fresh
        validation of *that* account's currency, leverage, margin mode, symbols,
        and execution terms.  Trading authorisation is kept (it is the
        operator's setting, not an account attribute); per-account state is
        dropped, so nothing from the previous account is reused.
        """

        if not self.connected or self._terminal is None:
            raise MT5SubmitRefused("mt5_not_connected")
        try:
            fresh = self._terminal.status()
        except MT5Error as exc:
            self._audit(
                "MT5_ACCOUNT_REFRESH_FAILED",
                allowed=False,
                reason=exc.reason,
                live=False,
            )
            raise MT5SubmitRefused("mt5_account_refresh_failed", exc.reason) from exc

        previous = self._account_identity
        identity = (fresh.account_login, fresh.account_server, fresh.trade_mode)
        changed = previous is not None and identity != previous
        previous_fingerprint = self._armed_fingerprint
        self._status = fresh
        self._account_identity = identity
        self._fingerprint_cache = self.account_fingerprint(fresh.account_login)
        try:
            self._terms = account_terms(fresh)
        except MT5Error as exc:
            self._terms = None
            self._account_validated = None
            self._audit(
                "MT5_ACCOUNT_TERMS_REFUSED",
                allowed=False,
                reason=exc.reason,
                account_mode=fresh.account_mode,
                live=False,
            )
            raise MT5SubmitRefused("mt5_account_terms_refused", exc.reason) from exc

        if changed:
            # Instrument resolution, cached specs, and reconciliation baselines
            # belong to the previous account.
            self._symbol_cache.clear()
            self._account_validated = None
            self._last_recovery = ()
            entry = {
                "to_mode": fresh.account_mode,
                "to_server": fresh.account_server,
                "account_fingerprint": self.account_fingerprint(),
                "previous_account_fingerprint": previous_fingerprint or "",
                "trading_authorisation": "kept" if self._armed else "not_armed",
                "state_reset": ["symbol_cache", "account_validation", "recovery_view"],
                "changed_at": self.clock(),
            }
            self._account_changes = self._account_changes + (entry,)
            self._audit(
                "MT5_ACCOUNT_CHANGED",
                allowed=True,
                reason="terminal_account_changed",
                account_mode=fresh.account_mode,
                account_server=fresh.account_server,
                live=False,
            )
        return self.validate_account_terms()

    def refresh_account(self) -> dict[str, Any]:
        """Public alias: detect an account switch, revalidate, and report.

        Kept for operator tooling.  It never switches accounts, never logs in,
        and never disarms because of a switch.
        """

        self._ensure_account_current()
        return self.redacted_account()

    def account_state(self) -> dict[str, Any]:
        """Everything account-scoped, for audits and reports."""

        validation = self._account_validated or {
            "valid": False,
            "problems": ["account_not_validated"],
            "notes": [],
        }
        return {
            "account": self.redacted_account(),
            "account_id": self.account_fingerprint() if self._status else "",
            # The trading setting and the account mode are independent.
            "trading_enabled_setting": self.settings.trading_enabled,
            "trading_mode_setting": self.settings.mode_label(),
            "armed": self._armed,
            "armed_for_account_fingerprint": self._armed_fingerprint or "",
            "account_validated": dict(validation),
            "unresolved_orders": self.unresolved_request_ids(),
            "unresolved_protection_modifications": self.unresolved_protection_modification_ids(),
            "unattributed_orders": self.unattributed_blocking_orders(),
            "resolved_symbols": sorted(
                symbol for symbol, _ in self._symbol_cache.values()
            ),
            "account_changes": [dict(item) for item in self._account_changes],
        }

    @property
    def account_changes(self) -> tuple[dict[str, Any], ...]:
        return self._account_changes

    def status(self) -> dict[str, Any]:
        return {
            "broker_id": BROKER_ID,
            "provider": "official_metatrader5_python",
            "connected": self.connected,
            "terminal": self._status.audit_view() if self._status else None,
            "account": self._terms.audit_view() if self._terms else None,
            "settings": self.settings.audit_view(),
            "armed": self._armed,
            "armed_account_fingerprint": self._armed_fingerprint or "",
            "armed_account_matches_current": self.armed_account_matches_current,
            "account_mode": self.account_mode,
            "account_mode_confirmed": self.account_mode_confirmed,
            "account_is_demo": self.account_is_demo,
            "account_fingerprint": self.account_fingerprint()
            if self._status is not None
            else "",
            "redacted_account": self.redacted_account(),
            "account_state": self.account_state(),
            "trading_enabled_setting": self.settings.trading_enabled,
            "execution_authorised": self._armed and self.settings.trading_enabled,
            "password": self.password_store.status(),
            "unresolved_orders": self.unresolved_request_ids(),
            "unresolved_protection_modifications": self.unresolved_protection_modification_ids(),
            "unattributed_orders": self.unattributed_blocking_orders(),
            "market_access_token_required": False,
            "last_recovery": [item.audit_view() for item in self._last_recovery],
            "token_required_by_broker": False,
            "capabilities": self.capabilities.audit_view(),
            "conformance": conformance_requirements(),
            "no_order_on_connect": True,
        }

    # -- arming ---------------------------------------------------------

    def arm_trading(
        self,
        *,
        confirm: str,
        account_login: int | None = None,
        server: str | None = None,
        account_fingerprint: str | None = None,
    ) -> dict[str, Any]:
        """Explicit local authorisation to submit live orders.

        Authorisation is the operator's ``TRADING_ENABLED`` setting plus this
        call, **bound to one account**: the fingerprint of the account selected at
        this moment is recorded, and every later submission must still be looking
        at that same account.  If the operator switches accounts in the terminal,
        the arming state is kept (switching is the operator's choice, and AXIOM
        never disarms behind their back) but no order can be sent until it is
        armed again against the account actually on screen.

        ``account_login``/``server``/``account_fingerprint`` are optional
        acknowledgements: when given they must name the account the terminal
        currently has selected, which catches the mistake of arming while looking
        at a different account.
        """

        if not self.connected:
            raise MT5SubmitRefused("mt5_not_connected")
        if not self.settings.trading_enabled:
            raise MT5TradingNotAuthorised(
                "mt5_trading_not_enabled", f"mode={self.settings.mode_label()}"
            )
        assert self._status is not None
        if account_login is not None and int(account_login) != self._status.account_login:
            raise MT5SubmitRefused("mt5_arming_account_mismatch")
        if server is not None and str(server).strip().lower() != (
            self._status.account_server.strip().lower()
        ):
            raise MT5SubmitRefused("mt5_arming_server_mismatch")
        current_fingerprint = self.account_fingerprint()
        if account_fingerprint is not None and str(account_fingerprint).strip() != current_fingerprint:
            # The operator confirmed a different account than the one in front of
            # them.  Refuse: confirmation is only meaningful for what it names.
            raise MT5SubmitRefused(
                "mt5_arming_account_confirmation_mismatch", current_fingerprint
            )
        phrase = str(confirm).strip().upper()
        accepted = {"ENABLE LIVE TRADING", f"ENABLE LIVE TRADING {self._status.account_login}"}
        if phrase not in accepted:
            raise MT5SubmitRefused(
                "mt5_arming_confirmation_mismatch", "ENABLE LIVE TRADING"
            )
        if not self._status.trade_allowed:
            raise MT5SubmitRefused("mt5_terminal_trading_not_allowed")
        validation = self._ensure_account_current()
        if not validation["valid"]:
            raise MT5SubmitRefused(
                "mt5_account_profile_invalid", ",".join(validation["problems"])
            )
        unresolved = self.unresolved_request_ids()
        if unresolved:
            raise MT5SubmitRefused("mt5_unresolved_order_present", ",".join(unresolved))
        self.reconcile_protection_modifications()
        unresolved_protection = self.unresolved_protection_modification_ids()
        if unresolved_protection:
            raise MT5SubmitRefused(
                "mt5_unresolved_protection_modification_present",
                ",".join(unresolved_protection),
            )
        self._armed = True
        self._armed_fingerprint = self.account_fingerprint()
        self._audit(
            "MT5_TRADING_ARMED",
            allowed=True,
            account_server=self._status.account_server,
            account_mode=self._status.account_mode,
            live=False,  # arming is not an order
        )
        return self.status()

    def disarm(self, *, reason: str = "operator") -> None:
        was_armed = self._armed
        # Clear authorization before logging: an audit sink failure must never
        # leave a session armed after a safety-triggered disarm request.
        self._armed = False
        self._armed_fingerprint = None
        if was_armed:
            self._audit("MT5_TRADING_DISARMED", allowed=False, reason=reason, live=False)

    # -- instruments and data -------------------------------------------

    def symbol_for(self, spec: CurrencyPairSpec) -> str:
        cached = self._symbol_cache.get(str(spec.pair))
        if cached and cached[1] is spec:
            return cached[0]
        symbol = self.terminal.resolve_symbol(spec)
        self._symbol_cache[str(spec.pair)] = (symbol, spec)
        return symbol

    def symbol_info(self, spec: CurrencyPairSpec):
        symbol = self.symbol_for(spec)
        return symbol, self.terminal.symbol_info(symbol)

    def cross_check(self, spec: CurrencyPairSpec) -> tuple[str, SymbolCrossCheck]:
        symbol, info = self.symbol_info(spec)
        from .symbols import cross_check as run_check

        return symbol, run_check(spec, info)

    def broker_terms(self, spec: CurrencyPairSpec) -> dict[str, Any]:
        _, info = self.symbol_info(spec)
        return broker_terms_candidate(spec, info)

    def quote(self, spec: CurrencyPairSpec):
        symbol = self.symbol_for(spec)
        _, info = self.symbol_info(spec)
        require_data(spec, info)
        return self.terminal.quote(spec, symbol)

    def bars(
        self,
        spec: CurrencyPairSpec,
        timeframe: str,
        *,
        start: datetime,
        end: datetime,
    ):
        from .terminal import aggregate_ticks_to_bars

        symbol = self.symbol_for(spec)
        _, info = self.symbol_info(spec)
        require_data(spec, info)
        ticks = self.terminal.ticks(symbol, start, end)
        return aggregate_ticks_to_bars(
            spec, symbol, timeframe, ticks, start=start, end=end
        )

    def positions(self, *, symbol: str | None = None) -> list[MT5Position]:
        return open_positions(self.terminal, symbol=symbol)

    def stop_requirement(
        self,
        spec: CurrencyPairSpec,
        *,
        text: str | None = None,
        symbol: str | None = None,
        info: Any = None,
    ) -> dict[str, Any]:
        """Everything needed to judge (and show) one symbol's broker-side stop.

        ``valid`` is true only when a distance is declared *with its unit*, it
        converts to a whole number of the broker's points, and it is at least the
        broker's own ``stops_level``.  Nothing is defaulted: a pair with no entry
        in ``protective_stops`` has no usable distance, however small the risk
        looks.
        """

        pair = str(
            getattr(spec, "canonical_pair", "") or getattr(spec, "pair", "") or ""
        )
        declared = (
            str(text).strip()
            if text is not None
            else self.settings.stop_text_for(pair)
        )
        legacy = int(getattr(self.settings, "protective_stop_points", 0) or 0)
        result: dict[str, Any] = {
            "pair": pair,
            "broker_symbol": str(symbol or ""),
            "declared": declared,
            "unit": "",
            "distance": "",
            "distance_points": None,
            "distance_price": None,
            "point": None,
            "digits": None,
            "pip_size": None,
            "broker_min_points": None,
            "broker_min_price": None,
            "valid": False,
            "reason": "",
            "detail": "",
            "source": "settings" if text is None else "operator_input",
            "legacy_global_points_ignored": legacy,
            "connected": bool(getattr(self, "connected", False)),
        }
        parsed = parse_stop_distance(declared)
        if parsed is not None:
            result["distance"], result["unit"] = (
                format(Decimal(parsed[0]).normalize(), "f"),
                parsed[1],
            )
        if not declared:
            result["reason"] = "mt5_protective_stop_not_configured_for_symbol"
            result["detail"] = (
                f"no distance is declared for {normalise_pair_key(pair) or pair}"
            )
            return result
        if parsed is None:
            result["reason"] = "mt5_protective_stop_invalid"
            result["detail"] = "a distance needs one number and one unit (points or pips)"
            return result
        if not result["connected"]:
            result["reason"] = "mt5_not_connected"
            result["detail"] = "the broker's point size and minimum are unread"
            return result
        try:
            if symbol is None or info is None:
                resolved_symbol, resolved_info = self.symbol_info(spec)
                symbol = symbol or resolved_symbol
                info = info or resolved_info
        except Exception as exc:
            result["reason"] = str(getattr(exc, "reason", "") or "mt5_symbol_unresolved")
            result["detail"] = type(exc).__name__
            return result
        result["broker_symbol"] = str(symbol)
        point = getattr(info, "point", None)
        digits = int(getattr(info, "digits", 0) or 0)
        pip_size = None
        try:
            pip_size = spec.pip_size_decimal()
        except Exception:
            pip_size = None
        result.update(
            {
                "point": None if point is None else str(point),
                "digits": digits,
                "pip_size": None if pip_size is None else str(pip_size),
            }
        )
        points, problem = stop_distance_points(
            parsed[0], parsed[1], point=point, pip_size=pip_size
        )
        broker_min = int(getattr(info, "stops_level", 0) or 0)
        result["broker_min_points"] = broker_min
        if point is not None:
            result["broker_min_price"] = str(
                (Decimal(point) * Decimal(broker_min)).normalize()
            )
        if points is None:
            result["reason"] = problem
            result["detail"] = (
                f"{result['distance']} {result['unit']} cannot be expressed in the "
                f"broker's points (point={result['point']}, pip={result['pip_size']})"
            )
            return result
        result["distance_points"] = points
        result["distance_price"] = (
            None if point is None else str((Decimal(point) * Decimal(points)).normalize())
        )
        if broker_min and points < broker_min:
            result["reason"] = "mt5_protective_stop_below_broker_minimum"
            result["detail"] = (
                f"{points} point(s) is inside the broker's minimum of {broker_min}"
            )
            return result
        result["valid"] = True
        return result

    def unprotected_positions(self) -> list[dict[str, Any]]:
        """Positions this deployment placed that the broker reports without a stop.

        Raised, not swallowed: an unreadable position list is not evidence that
        every position is protected.
        """

        from .reconcile import unprotected_positions as _unprotected

        return list(_unprotected(self.positions(), magic=int(self.settings.magic)))

    # -- recover --------------------------------------------------------

    def _record_account_fingerprint(self, record: Mapping[str, Any]) -> str:
        """The account an order record belongs to, if it can be attributed."""

        explicit = str(record.get("account_fingerprint") or "")
        if explicit:
            return explicit
        try:
            login = int(record.get("account_login") or 0)
        except (TypeError, ValueError):
            return ""
        return self.account_fingerprint(login) if login > 0 else ""

    def _current_fingerprint(self) -> str:
        """Fingerprint of the selected account (also after a disconnect)."""

        if self._status is not None:
            return self.account_fingerprint()
        return self._fingerprint_cache

    def _mt5_orders(self) -> list[dict[str, Any]]:
        try:
            records = self.repository.list_orders()
        except Exception:
            return []
        return [
            record
            for record in records
            if str(record.get("broker_id", "")).lower() == BROKER_ID
        ]

    def account_orders(self) -> list[dict[str, Any]]:
        """MT5 order records belonging to the account selected right now.

        Another account's records are excluded: its unresolved orders,
        positions, and reservations are not this account's state and must not
        influence its risk decisions.
        """

        current = self._current_fingerprint()
        if not current:
            return []
        return [
            record
            for record in self._mt5_orders()
            if self._record_account_fingerprint(record) == current
        ]

    def unattributed_blocking_orders(self) -> list[str]:
        """Blocking records that cannot be attributed to any account.

        They are reported (so an operator can see them) but do not block this
        account, because attributing them to it would reuse another account's
        uncertainty.
        """

        blocking = []
        for record in self._mt5_orders():
            if str(record.get("state", "")).upper() not in MT5_BLOCKING_STATES:
                continue
            if not self._record_account_fingerprint(record):
                blocking.append(str(record.get("request_id", "")))
        return sorted(item for item in blocking if item)

    def unresolved_request_ids(self) -> list[str]:
        """Blocking order ids for the account selected *right now* only."""

        ids: list[str] = []
        for record in self.account_orders():
            if str(record.get("state", "")).upper() in MT5_BLOCKING_STATES:
                ids.append(str(record.get("request_id", "")))
        return sorted(item for item in ids if item)

    def unresolved_protection_modification_ids(self) -> list[str]:
        """Protection changes whose exact SL/TP pair lacks broker read-back."""

        return sorted(
            request_id
            for request_id, (_identity, result) in self._protection_modification_results.items()
            if str(result.get("state") or "").upper() == "UNRESOLVED"
        )

    def reconcile_protection_modifications(self) -> dict[str, list[str]]:
        """Resolve ambiguous SL/TP results only when the same position echoes both fields."""

        resolved: list[str] = []
        unresolved = self.unresolved_protection_modification_ids()
        if not self.connected or self._status is None:
            return {"resolved": resolved, "unresolved": unresolved}
        try:
            self._ensure_account_current()
            current_fingerprint = self.account_fingerprint()
        except Exception:
            return {"resolved": resolved, "unresolved": unresolved}

        for request_id in unresolved:
            identity, prior = self._protection_modification_results[request_id]
            if (
                not current_fingerprint
                or current_fingerprint != str(prior.get("account_fingerprint") or "")
            ):
                continue
            symbol = str(identity.get("symbol") or "")
            try:
                position_ticket = int(identity.get("ticket") or 0)
                expected_stop = Decimal(str(identity.get("stop_price")))
                raw_target = identity.get("target_price")
                expected_target = None if raw_target is None else Decimal(str(raw_target))
                expected = identity.get("expected_position")
                if not isinstance(expected, Mapping):
                    continue
                expected_volume = Decimal(str(expected.get("lots") or expected.get("volume")))
                expected_entry = Decimal(
                    str(expected.get("entry_price") or expected.get("price_open"))
                )
                expected_side = str(expected.get("side") or "").upper()
                expected_symbol = str(expected.get("symbol") or "")
                expected_magic = int(expected.get("magic", -1))
                positions = self.positions(symbol=symbol)
                matches = [item for item in positions if item.ticket == position_ticket]
                if len(matches) != 1:
                    continue
                position = matches[0]
                symbol_info = self.terminal.symbol_info(symbol)
            except Exception:
                continue
            if (
                position.symbol != expected_symbol
                or position.side != expected_side
                or position.volume != expected_volume
                or abs(position.price_open - expected_entry) > symbol_info.point
                or position.magic != expected_magic
                or expected_magic != int(self.settings.magic)
            ):
                continue
            if not self._protection_matches(
                position,
                stop_price=expected_stop,
                target_price=expected_target,
                point=symbol_info.point,
            ):
                continue
            reconciled = {
                **dict(prior),
                "state": "CONFIRMED",
                "unknown": False,
                "verified": True,
                "reason": "broker_readback_confirmed_during_reconciliation",
                "detail": "the exact position now echoes the requested SL and TP",
                "broker_stop_loss": (
                    None if position.stop_loss is None else str(position.stop_loss)
                ),
                "broker_take_profit": (
                    None
                    if position.take_profit is None or position.take_profit <= 0
                    else str(position.take_profit)
                ),
                "permits_resend": False,
            }
            self._protection_modification_results[request_id] = (identity, reconciled)
            self._audit(
                "MT5_PROTECTION_MODIFICATION_RECONCILED",
                allowed=True,
                request_id=request_id,
                position_ticket=position_ticket,
                symbol=symbol,
                verified=True,
                live=True,
            )
            resolved.append(request_id)
        remaining = self.unresolved_protection_modification_ids()
        return {"resolved": sorted(resolved), "unresolved": remaining}

    def recover(self) -> tuple[RecoveryResult, ...]:
        """Resolve what happened to every MT5 order left in doubt."""

        results: list[RecoveryResult] = []
        for record in self.account_orders():
            state = str(record.get("state", "")).upper()
            if state not in MT5_BLOCKING_STATES:
                continue
            try:
                result = reconcile_record(self.terminal, record)
            except MT5Error as exc:
                result = RecoveryResult(
                    request_id=str(record.get("request_id", "")),
                    previous_state=state,
                    state="UNRESOLVED",
                    evidence="recovery_failed",
                    order_ticket=int(record.get("order_ticket") or 0),
                    deal_tickets=(),
                    volume_matched=Decimal(0),
                    resolved=False,
                    resend_allowed=False,
                    detail=exc.reason,
                )
            self._persist_recovery(record, result)
            results.append(result)
        self._last_recovery = tuple(results)
        if any(item.state in {"UNRESOLVED", "ACCEPTED"} for item in results):
            # An order in doubt is a trading gate, not an account artifact: no
            # new order goes out until it is explained.  (A partial fill is
            # likewise already blocking through ``unresolved_request_ids``.)
            self.disarm(reason="unresolved_order_after_recovery")
        return tuple(results)

    def _persist_recovery(self, record: Mapping[str, Any], result: RecoveryResult) -> None:
        merged = dict(record)
        merged.update(
            {
                "state": result.state,
                "recovered_at": self.clock(),
                "recovery": result.audit_view(),
                "resend_allowed": False,
                "updated_at": self.clock(),
            }
        )
        try:
            self.repository.save_orders([merged])
        except Exception:
            pass
        self._audit(
            "MT5_ORDER_RECOVERED",
            allowed=result.resolved,
            request_id=result.request_id,
            state=result.state,
            evidence=result.evidence,
            live=True,
        )

    def reconcile(
        self, ledger_positions: Sequence[Mapping[str, Any]] = ()
    ) -> PositionReconciliation:
        """Compare this account's broker positions with this account's ledger.

        Ledger rows carrying a different ``account_id`` are excluded: the
        broker knows nothing about them, and treating them as missing positions
        would let one account's book masquerade as another's.
        """

        broker = self.positions()
        mapping = {
            symbol: str(spec.pair) for symbol, spec in self._symbol_cache.values()
        }
        current = self._current_fingerprint()
        scoped = [
            row
            for row in ledger_positions
            if not row.get("account_id") or str(row.get("account_id")) == current
        ]
        return reconcile_positions(
            broker,
            scoped,
            pair_for_symbol=mapping,
            magic=int(self.settings.magic),
        )

    def history(
        self, *, start: datetime, end: datetime
    ) -> dict[str, Any]:
        from .reconcile import history_deals, history_orders

        return {
            "deals": [item.audit_view() for item in history_deals(self.terminal, start=start, end=end)],
            "orders": [item.audit_view() for item in history_orders(self.terminal, start=start, end=end)],
        }

    # -- broker-side protection management -----------------------------

    @staticmethod
    def _protection_value(value: Any, *, name: str, optional: bool = False) -> Decimal | None:
        if value is None or (optional and str(value).strip() == ""):
            return None
        try:
            parsed = Decimal(str(value))
        except Exception as exc:
            raise MT5SubmitRefused(f"mt5_{name}_invalid") from exc
        if not parsed.is_finite():
            raise MT5SubmitRefused(f"mt5_{name}_invalid")
        if optional and parsed == 0:
            return None
        if parsed <= 0:
            raise MT5SubmitRefused(f"mt5_{name}_invalid")
        return parsed

    @staticmethod
    def _protection_matches(
        position: MT5Position,
        *,
        stop_price: Decimal,
        target_price: Decimal | None,
        point: Decimal,
    ) -> bool:
        observed_stop = position.stop_loss
        if (
            observed_stop is None
            or not observed_stop.is_finite()
            or observed_stop <= 0
            or abs(observed_stop - stop_price) > point
        ):
            return False
        if position.side == "BUY" and observed_stop < stop_price:
            return False
        if position.side == "SELL" and observed_stop > stop_price:
            return False
        observed_target = position.take_profit
        if observed_target is not None and observed_target <= 0:
            observed_target = None
        if target_price is None:
            return observed_target is None
        return (
            observed_target is not None
            and observed_target.is_finite()
            and abs(observed_target - target_price) <= point
        )

    def modify_position_protection(
        self,
        *,
        request_id: str,
        ticket: int,
        symbol: str,
        spec: CurrencyPairSpec,
        strategy_id: str,
        stop_price: Decimal,
        target_price: Decimal | None,
        expected_position: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Modify one owned position's SL/TP under the existing live adapter.

        This action intentionally does not use ``send_order``: an SLTP request
        has no fill volume.  It still uses the same terminal, account refresh,
        trading-enabled setting, account-bound arming, strategy lifecycle,
        symbol checks, and single ``order_send`` boundary as market orders.
        Success is not reported until the exact position echoes both requested
        protection fields back from ``positions_get``.
        """

        request_id = str(request_id or "").strip()
        try:
            position_ticket = int(ticket)
        except (TypeError, ValueError, OverflowError):
            position_ticket = 0
        requested_stop = None
        requested_target = None
        try:
            requested_stop = self._protection_value(
                stop_price, name="protection_stop"
            )
            if requested_stop is None:
                raise MT5SubmitRefused("mt5_protection_stop_required")
            requested_target = self._protection_value(
                target_price, name="protection_target", optional=True
            )
        except MT5SubmitRefused as exc:
            reason = exc.reason
            detail = exc.detail
            return self._protection_refusal(
                request_id=request_id,
                ticket=position_ticket,
                symbol=str(symbol or ""),
                reason=reason,
                detail=detail,
            )

        if not request_id or len(request_id) > 128 or position_ticket <= 0:
            return self._protection_refusal(
                request_id=request_id,
                ticket=position_ticket,
                symbol=str(symbol or ""),
                reason="mt5_protection_identity_invalid",
            )
        symbol = str(symbol or "").strip()
        strategy_id = str(strategy_id or "").strip()
        expected = expected_position if isinstance(expected_position, Mapping) else {}
        request_identity = {
            "request_id": request_id,
            "ticket": position_ticket,
            "symbol": symbol,
            "strategy_id": strategy_id,
            "stop_price": str(requested_stop),
            "target_price": None if requested_target is None else str(requested_target),
            "expected_position": {
                key: str(expected.get(key, ""))
                for key in (
                    "ticket", "symbol", "side", "lots", "volume", "entry_price",
                    "price_open", "stop_price", "stop_loss", "target_price",
                    "take_profit", "magic",
                )
            },
        }
        self.reconcile_protection_modifications()
        previous = self._protection_modification_results.get(request_id)
        if previous is not None:
            previous_identity, previous_result = previous
            if previous_identity != request_identity:
                return self._protection_refusal(
                    request_id=request_id,
                    ticket=position_ticket,
                    symbol=symbol,
                    reason="mt5_protection_request_id_reused",
                )
            return {**dict(previous_result), "duplicate": True, "sent": False}
        unresolved_protection = self.unresolved_protection_modification_ids()
        if unresolved_protection:
            return self._protection_refusal(
                request_id=request_id,
                ticket=position_ticket,
                symbol=symbol,
                reason="mt5_unresolved_protection_modification_present",
                detail=",".join(unresolved_protection),
            )

        fingerprint = ""
        order_send_attempted = False
        try:
            validation = self.settings.validate()
            if not validation.valid:
                raise MT5SubmitRefused("mt5_settings_invalid", validation.reason)
            self.settings.assert_trading_allowed()
            if not self.connected or self._status is None:
                raise MT5SubmitRefused("mt5_not_connected")
            if not self._armed:
                raise MT5SubmitRefused("mt5_trading_not_armed")

            account_validation = self._ensure_account_current()
            fingerprint = self.account_fingerprint()
            if self._armed_fingerprint and fingerprint != self._armed_fingerprint:
                raise MT5SubmitRefused(
                    "mt5_account_changed_since_confirmation", fingerprint or "unknown"
                )
            if not account_validation.get("valid"):
                raise MT5SubmitRefused(
                    "mt5_account_profile_invalid",
                    ",".join(account_validation.get("problems", ())),
                )
            if self._terms is None:
                raise MT5SubmitRefused("mt5_account_terms_refused")
            if not self._status.trade_allowed:
                raise MT5SubmitRefused("mt5_terminal_trading_not_allowed")
            unresolved = self.unresolved_request_ids()
            if unresolved:
                self.disarm(reason="unresolved_order_present")
                raise MT5SubmitRefused(
                    "mt5_unresolved_order_present", ",".join(unresolved)
                )
            if not strategy_id:
                raise MT5SubmitRefused("mt5_strategy_identity_missing")
            if self.lifecycle_lookup is None:
                raise MT5SubmitRefused("mt5_strategy_lifecycle_not_supplied")
            lifecycle = self.lifecycle_lookup(strategy_id)
            approve = getattr(lifecycle, "approve_active", None)
            if lifecycle is None or not callable(approve) or not approve():
                raise MT5SubmitRefused("mt5_strategy_not_approved_active", strategy_id)
            if str(getattr(spec, "spec_version", "")) == "":
                raise MT5SubmitRefused("mt5_spec_version_missing")

            provider_symbol, info = self.symbol_info(spec)
            if provider_symbol != symbol:
                raise MT5SubmitRefused("mt5_protection_symbol_mismatch")
            _, symbol_check = self.cross_check(spec)
            if not symbol_check.ok:
                raise MT5SubmitRefused(
                    "mt5_symbol_terms_mismatch", ",".join(symbol_check.errors)
                )
            try:
                require_trading(spec, info)
            except MT5DataRefused as exc:
                raise MT5SubmitRefused(exc.reason, exc.detail) from exc

            if not isinstance(expected_position, Mapping):
                raise MT5SubmitRefused("mt5_protection_position_identity_missing")
            positions = self.positions(symbol=symbol)
            candidates = [item for item in positions if item.ticket == position_ticket]
            if len(candidates) != 1:
                raise MT5SubmitRefused("mt5_protection_position_not_unique")
            position = candidates[0]
            expected_ticket = str(expected.get("ticket") or expected.get("position_id") or "")
            expected_symbol = str(expected.get("symbol") or "")
            expected_side = str(expected.get("side") or "").upper()
            expected_volume = self._protection_value(
                expected.get("lots", expected.get("volume")),
                name="protection_position_volume",
            )
            expected_entry = self._protection_value(
                expected.get("entry_price", expected.get("price_open")),
                name="protection_position_entry",
            )
            expected_stop = self._protection_value(
                expected.get("stop_price", expected.get("stop_loss")),
                name="protection_position_stop",
                optional=True,
            )
            expected_target = self._protection_value(
                expected.get("target_price", expected.get("take_profit")),
                name="protection_position_target",
                optional=True,
            )
            if (
                expected_ticket != str(position_ticket)
                or expected_symbol != symbol
                or expected_side != position.side
                or expected_volume != position.volume
                or expected_entry is None
                or abs(expected_entry - position.price_open) > info.point
                or int(expected.get("magic", -1)) != int(self.settings.magic)
                or int(position.magic) != int(self.settings.magic)
            ):
                raise MT5SubmitRefused("mt5_protection_position_identity_changed")
            current_stop = position.stop_loss
            if current_stop is not None and current_stop <= 0:
                current_stop = None
            current_target = position.take_profit
            if current_target is not None and current_target <= 0:
                current_target = None
            if current_stop != expected_stop or current_target != expected_target:
                raise MT5SubmitRefused("mt5_protection_position_changed")
            if current_stop is not None and (
                (position.side == "BUY" and requested_stop < current_stop)
                or (position.side == "SELL" and requested_stop > current_stop)
            ):
                raise MT5SubmitRefused("mt5_protection_stop_widening_refused")
            if requested_target is not None and (
                (position.side == "BUY" and requested_target <= position.price_open)
                or (position.side == "SELL" and requested_target >= position.price_open)
            ):
                raise MT5SubmitRefused("mt5_protection_target_side_invalid")

            # Only the exact target may currently be unprotected; this route is
            # for repairing or tightening that position, never for bypassing a
            # different open exposure's protection blocker.
            other_unprotected = [
                item for item in self.unprotected_positions()
                if int(item.get("ticket", 0) or 0) != position_ticket
            ]
            if other_unprotected:
                raise MT5SubmitRefused(
                    "mt5_other_unprotected_position_present",
                    ",".join(str(item.get("ticket", 0)) for item in other_unprotected),
                )

            if self._protection_matches(
                position,
                stop_price=requested_stop,
                target_price=requested_target,
                point=info.point,
            ):
                already = {
                    "request_id": request_id,
                    "ticket": str(position_ticket),
                    "symbol": symbol,
                    "state": "CONFIRMED",
                    "sent": False,
                    "unknown": False,
                    "verified": True,
                    "retcode": 10025,
                    "retcode_name": "NO_CHANGES",
                    "classification": "NO_CHANGES",
                    "reason": "broker_readback_already_matches",
                    "detail": "",
                    "requested_stop_loss": str(requested_stop),
                    "requested_take_profit": (
                        None if requested_target is None else str(requested_target)
                    ),
                    "broker_stop_loss": str(position.stop_loss),
                    "broker_take_profit": (
                        None if current_target is None else str(current_target)
                    ),
                    "account_fingerprint": fingerprint,
                }
                self._audit(
                    "MT5_PROTECTION_MODIFICATION_CONFIRMED",
                    allowed=True,
                    request_id=request_id,
                    position_ticket=position_ticket,
                    symbol=symbol,
                    protective_stop=str(requested_stop),
                    target_price=requested_target,
                    broker_stop_loss=position.stop_loss,
                    broker_take_profit=current_target,
                    verified=True,
                    classification="NO_CHANGES",
                    live=True,
                )
                self._protection_modification_results[request_id] = (
                    request_identity, already
                )
                return already

            if requested_stop.as_tuple().exponent < -int(info.digits):
                raise MT5SubmitRefused("mt5_protection_stop_precision_invalid")
            if requested_target is not None and requested_target.as_tuple().exponent < -int(info.digits):
                raise MT5SubmitRefused("mt5_protection_target_precision_invalid")
            tick = self.terminal.tick(symbol)
            minimum_points = max(int(info.stops_level or 0), int(info.freeze_level or 0))
            minimum_distance = info.point * minimum_points
            if position.side == "BUY":
                valid_stop = requested_stop < tick.bid and tick.bid - requested_stop >= minimum_distance
                valid_target = requested_target is None or (
                    requested_target > tick.bid
                    and requested_target - tick.bid >= minimum_distance
                )
            else:
                valid_stop = requested_stop > tick.ask and requested_stop - tick.ask >= minimum_distance
                valid_target = requested_target is None or (
                    requested_target < tick.ask
                    and tick.ask - requested_target >= minimum_distance
                )
            if not valid_stop or not valid_target:
                raise MT5SubmitRefused(
                    "mt5_protection_price_outside_broker_distance",
                    f"stops={info.stops_level};freeze={info.freeze_level}",
                )

            request = {
                "action": TRADE_ACTION_SLTP,
                "position": position_ticket,
                "symbol": symbol,
                "sl": float(requested_stop),
                "tp": 0.0 if requested_target is None else float(requested_target),
            }
            check: MT5OrderCheck = check_order(self.terminal, request)
            if not check.allowed:
                refused = {
                    "request_id": request_id,
                    "ticket": str(position_ticket),
                    "symbol": symbol,
                    "state": "REFUSED",
                    "sent": False,
                    "unknown": False,
                    "verified": False,
                    "retcode": check.retcode,
                    "retcode_name": check.retcode_name,
                    "classification": "REJECTED",
                    "reason": "mt5_protection_order_check_refused",
                    "detail": check.comment,
                    "requested_stop_loss": str(requested_stop),
                    "requested_take_profit": (
                        None if requested_target is None else str(requested_target)
                    ),
                    "broker_stop_loss": None if current_stop is None else str(current_stop),
                    "broker_take_profit": None if current_target is None else str(current_target),
                    "account_fingerprint": fingerprint,
                }
                self._audit(
                    "MT5_PROTECTION_MODIFICATION_REFUSED",
                    allowed=False,
                    request_id=request_id,
                    position_ticket=position_ticket,
                    symbol=symbol,
                    reason=refused["reason"],
                    retcode=check.retcode,
                    retcode_name=check.retcode_name,
                    verified=False,
                    live=True,
                )
                self._protection_modification_results[request_id] = (
                    request_identity, refused
                )
                return refused

            self._audit(
                "MT5_PROTECTION_MODIFICATION_SUBMITTING",
                allowed=True,
                request_id=request_id,
                position_ticket=position_ticket,
                symbol=symbol,
                protective_stop=str(requested_stop),
                target_price=requested_target,
                action="TRADE_ACTION_SLTP",
                live=True,
            )
            if not self.terminal.connected:
                raise MT5SubmitRefused("mt5_not_connected")
            order_send_attempted = True
            transport: MT5ProtectionOutcome = send_protection_modification(
                self.terminal,
                request,
                request_id=request_id,
                ticket=position_ticket,
            )
            readback_detail = ""
            observed: MT5Position | None = None
            try:
                after = self.positions(symbol=symbol)
                matches = [item for item in after if item.ticket == position_ticket]
                observed = matches[0] if len(matches) == 1 else None
                if observed is None:
                    readback_detail = "position_not_found_or_duplicated_after_modification"
            except Exception as exc:
                readback_detail = f"positions_unavailable:{type(exc).__name__}"
            verified = bool(
                observed is not None
                and self._protection_matches(
                    observed,
                    stop_price=requested_stop,
                    target_price=requested_target,
                    point=info.point,
                )
            )
            if verified:
                state = "CONFIRMED"
                reason = "broker_readback_confirmed"
                unknown = False
            elif transport.unknown or transport.accepted:
                state = "UNRESOLVED"
                reason = "mt5_protection_outcome_unconfirmed"
                unknown = True
                self.disarm(reason="protection_modification_unconfirmed")
            else:
                state = "REJECTED"
                reason = transport.retcode_name.lower() or "mt5_protection_rejected"
                unknown = False
            result = {
                "request_id": request_id,
                "ticket": str(position_ticket),
                "symbol": symbol,
                "state": state,
                "sent": True,
                "unknown": unknown,
                "verified": verified,
                "retcode": transport.retcode,
                "retcode_name": transport.retcode_name,
                "classification": transport.classification,
                "reason": reason,
                "detail": readback_detail or transport.comment,
                "requested_stop_loss": str(requested_stop),
                "requested_take_profit": (
                    None if requested_target is None else str(requested_target)
                ),
                "broker_stop_loss": (
                    None if observed is None or observed.stop_loss is None
                    else str(observed.stop_loss)
                ),
                "broker_take_profit": (
                    None if observed is None or observed.take_profit is None
                    or observed.take_profit <= 0
                    else str(observed.take_profit)
                ),
                "account_fingerprint": fingerprint,
                "permits_resend": False,
            }
            self._audit(
                "MT5_PROTECTION_MODIFICATION_RESULT",
                allowed=verified,
                request_id=request_id,
                state=state,
                position_ticket=position_ticket,
                symbol=symbol,
                retcode=transport.retcode,
                retcode_name=transport.retcode_name,
                classification=transport.classification,
                unknown=unknown,
                permits_resend=False,
                protective_stop=str(requested_stop),
                target_price=requested_target,
                broker_stop_loss=result["broker_stop_loss"],
                broker_take_profit=result["broker_take_profit"],
                verified=verified,
                reason=reason,
                live=True,
            )
            self._protection_modification_results[request_id] = (
                request_identity, result
            )
            return result
        except Exception as exc:
            reason = str(getattr(exc, "reason", "") or "mt5_protection_modification_refused")
            detail = str(getattr(exc, "detail", "") or type(exc).__name__)
            if order_send_attempted:
                # Once order_send has been entered, later logging/parsing errors
                # cannot turn the action into a clean refusal. Preserve the
                # pending request in the PositionBook and require read-back.
                self.disarm(reason="protection_modification_result_unreadable")
                unresolved = {
                    "request_id": request_id,
                    "ticket": str(position_ticket),
                    "symbol": symbol,
                    "state": "UNRESOLVED",
                    "sent": True,
                    "unknown": True,
                    "verified": False,
                    "reason": "mt5_protection_result_unreadable",
                    "detail": detail,
                    "permits_resend": False,
                    "requested_stop_loss": str(requested_stop),
                    "requested_take_profit": (
                        None if requested_target is None else str(requested_target)
                    ),
                    "account_fingerprint": fingerprint,
                }
                self._protection_modification_results[request_id] = (
                    request_identity, unresolved
                )
                try:
                    self._audit(
                        "MT5_PROTECTION_MODIFICATION_RESULT",
                        allowed=False,
                        request_id=request_id,
                        state="UNRESOLVED",
                        position_ticket=position_ticket,
                        symbol=symbol,
                        reason=unresolved["reason"],
                        unknown=True,
                        permits_resend=False,
                        verified=False,
                        live=True,
                    )
                except Exception:
                    pass
                return unresolved
            return self._protection_refusal(
                request_id=request_id,
                ticket=position_ticket,
                symbol=symbol,
                reason=reason,
                detail=detail,
            )

    def _protection_refusal(
        self,
        *,
        request_id: str,
        ticket: int,
        symbol: str,
        reason: str,
        detail: str = "",
    ) -> dict[str, Any]:
        result = {
            "request_id": request_id,
            "ticket": str(ticket) if ticket > 0 else "",
            "symbol": symbol,
            "state": "REFUSED",
            "sent": False,
            "unknown": False,
            "verified": False,
            "reason": str(reason),
            "detail": str(detail),
            "permits_resend": False,
        }
        self._audit(
            "MT5_PROTECTION_MODIFICATION_REFUSED",
            allowed=False,
            request_id=request_id,
            state="REFUSED",
            position_ticket=ticket if ticket > 0 else 0,
            symbol=symbol,
            reason=reason,
            verified=False,
            live=False,
        )
        return result

    # -- submission -----------------------------------------------------

    def submit(
        self,
        decision: GateDecision,
        intent: ApprovedOrderIntent,
        *,
        spec: CurrencyPairSpec,
        max_quote_age_seconds: Decimal | None = None,
    ) -> MT5OrderReceipt:
        """Submit exactly one live order for a gate-approved intent."""

        preflight = self._preflight(decision, intent, spec, max_quote_age_seconds)
        symbol, info = self.symbol_info(spec)
        lots, volume_result = to_broker_volume(spec, Decimal(str(intent.quantity)), info)
        if lots is None:
            raise MT5SubmitRefused("mt5_volume_invalid", volume_result.reason)
        filling, filling_name = filling_for(info)

        close_ticket = None
        if intent.close_position_id:
            close_ticket = int(preflight["close_position_ticket"])

        side = intent.side.value if hasattr(intent.side, "value") else str(intent.side).upper()
        tick = self.terminal.tick(symbol)
        price = tick.ask if side == "BUY" else tick.bid
        # A close never carries a new stop: it removes the exposure the stop was
        # protecting.  An entry must carry one, sized from the declared settings.
        entry = intent.close_position_id is None
        stop_price: Decimal | None = None
        stop_points = 0
        stop_declared = ""
        if entry:
            requirement = self.stop_requirement(spec, symbol=symbol, info=info)
            stop_declared = str(requirement.get("declared", ""))
            if not requirement.get("valid"):
                stop_problem = requirement.get("reason") or "mt5_protective_stop_unavailable"
                self._audit(
                    "MT5_ORDER_STOP_REFUSED",
                    allowed=False,
                    request_id=intent.request_id,
                    reason=stop_problem,
                    protective_stop=stop_declared,
                    live=False,
                )
                raise MT5SubmitRefused(stop_problem, requirement.get("detail", ""))
            stop_points = int(requirement["distance_points"])
            stop_price, stop_problem = protective_stop_price(
                side=side,
                reference=price,
                point=info.point,
                digits=info.digits,
                points=stop_points,
                stops_level=int(info.stops_level or 0),
            )
            if stop_price is None:
                self._audit(
                    "MT5_ORDER_STOP_REFUSED",
                    allowed=False,
                    request_id=intent.request_id,
                    reason=stop_problem,
                    protective_stop=stop_declared,
                    protective_stop_points=stop_points,
                    live=False,
                )
                raise MT5SubmitRefused(stop_problem or "mt5_protective_stop_unavailable")
        request = build_market_request(
            symbol=symbol,
            side=side,
            volume=lots,
            price=price,
            deviation=self.settings.deviation_points,
            magic=self.settings.magic,
            comment=intent.request_id,
            filling=filling,
            position_ticket=close_ticket,
            stop_loss=stop_price,
        )
        check: MT5OrderCheck = check_order(self.terminal, request)
        if not check.allowed:
            self._audit(
                "MT5_ORDER_CHECK_REFUSED",
                allowed=False,
                request_id=intent.request_id,
                retcode=check.retcode,
                retcode_name=check.retcode_name,
                live=True,
            )
            raise MT5SubmitRefused("mt5_order_check_refused", check.retcode_name)

        # Write-ahead: the record exists before the order can exist.  If this
        # write fails, nothing is sent.
        record = {
            "broker_id": BROKER_ID,
            "request_id": intent.request_id,
            "simulation_id": "",
            "state": "SUBMITTING",
            "side": side,
            "pair": intent.pair,
            "provider_symbol": symbol,
            "quantity": str(intent.quantity),
            "volume": str(lots),
            "price_gated": str(intent.executable_price),
            "price_sent": str(price),
            "magic": self.settings.magic,
            "comment": intent.request_id[:31],
            "account_login": self._status.account_login if self._status else 0,
            "account_server": self._status.account_server if self._status else "",
            "account_fingerprint": self.account_fingerprint()
            if self._status is not None
            else "",
            "account_mode": self.account_mode,
            "close_position_id": intent.close_position_id,
            "spec_version": intent.spec_version,
            "sent_at": self.clock(),
            "updated_at": self.clock(),
            "live": True,
            "mode": "LIVE",
            "filling": filling_name,
            "order_ticket": 0,
            "decision_id": str(preflight.get("decision_id", "")),
            "protective_stop": {
                "declared": stop_declared,
                "requested": "" if stop_price is None else str(stop_price),
                "points": stop_points if entry else 0,
                "verified": None,
                "broker_stop_loss": "",
                "detail": "" if entry else "not_an_entry",
                "ticket": 0,
            },
        }
        try:
            self.repository.save_orders([record])
        except Exception as exc:
            raise MT5SubmitRefused("mt5_write_ahead_failed", type(exc).__name__) from exc

        self._audit(
            "MT5_ORDER_SUBMITTING",
            allowed=True,
            request_id=intent.request_id,
            symbol=symbol,
            side=side,
            volume=lots,
            price=price,
            live=True,
        )
        outcome = send_order(
            self.terminal, request, request_id=intent.request_id, sent_at=record["sent_at"]
        )

        state = outcome.state
        record.update(
            {
                "state": state,
                "order_ticket": outcome.order_ticket,
                "deal_ticket": outcome.deal_ticket,
                "retcode": outcome.retcode,
                "retcode_name": outcome.retcode_name,
                "classification": outcome.classification,
                "volume_filled": str(outcome.volume_filled),
                "unknown": outcome.unknown,
                "permits_resend": outcome.permits_resend,
                "comment": outcome.comment or intent.request_id[:31],
                "updated_at": self.clock(),
                "outcome": outcome.audit_view(),
            }
        )
        try:
            self.repository.save_orders([record])
        except Exception:
            # The write-ahead record is still SUBMITTING, which blocks further
            # orders; recovery will re-read broker history against it.
            pass
        self._audit(
            "MT5_ORDER_RESULT",
            allowed=outcome.accepted,
            request_id=intent.request_id,
            state=state,
            retcode=outcome.retcode,
            retcode_name=outcome.retcode_name,
            classification=outcome.classification,
            unknown=outcome.unknown,
            permits_resend=outcome.permits_resend,
            volume_filled=outcome.volume_filled,
            live=True,
        )
        if entry:
            record["protective_stop"] = self._verify_stop(
                symbol=symbol,
                side=side,
                expected=stop_price,
                points=stop_points,
                declared=stop_declared,
                point=info.point,
                outcome=outcome,
            )
            try:
                self.repository.save_orders([record])
            except Exception:
                # The unverified record is already visible through the audit
                # event below; the SUBMITTING/FILLED row still holds the stop
                # request, so recovery can compare it with the broker.
                pass
            if record["protective_stop"].get("verified") is not True:
                self._audit(
                    "MT5_ORDER_STOP_UNCONFIRMED",
                    allowed=False,
                    request_id=intent.request_id,
                    state=state,
                    reason=record["protective_stop"].get("detail", ""),
                    protective_stop_points=stop_points,
                    protective_stop=str(record["protective_stop"].get("requested", "")),
                    live=True,
                )
                # A position this build cannot prove is protected must not be
                # joined by another: disarm, and reconcile before anything else.
                self.disarm(reason="protective_stop_unconfirmed")
            else:
                self._audit(
                    "MT5_ORDER_STOP_VERIFIED",
                    allowed=True,
                    request_id=intent.request_id,
                    state=state,
                    protective_stop_points=stop_points,
                    protective_stop=str(record["protective_stop"].get("requested", "")),
                    live=True,
                )
        if outcome.unknown or state in {"UNRESOLVED"}:
            # Fail closed: no further live order until reconciled.
            self.disarm(reason="unknown_order_outcome")
        receipt = MT5OrderReceipt(
            submission_id=intent.request_id,
            request_id=intent.request_id,
            state=state,
            live=True,
            mode="LIVE",
            account_login=self._status.account_login if self._status else 0,
            account_server=self._status.account_server if self._status else "",
            created_at=self.clock(),
            decision_id=str(preflight.get("decision_id", "")),
            outcome=outcome,
            capabilities=self.capabilities,
            preflight=preflight,
        )
        return receipt

    # -- preflight ------------------------------------------------------

    def _preflight(
        self,
        decision: GateDecision,
        intent: ApprovedOrderIntent,
        spec: CurrencyPairSpec,
        max_quote_age_seconds: Decimal | None,
    ) -> dict[str, Any]:
        if not isinstance(decision, GateDecision) or not decision.allowed:
            raise MT5SubmitRefused("mt5_gate_decision_not_allowed")
        approved = decision.order_intent
        if approved is None:
            raise MT5SubmitRefused("mt5_gate_decision_missing_intent")
        if approved.request_id != intent.request_id:
            raise MT5SubmitRefused("mt5_intent_request_mismatch")
        if approved.audit_view() != intent.audit_view():
            # A caller must not be able to substitute a different order after
            # the gate approved one.
            raise MT5SubmitRefused("mt5_intent_differs_from_approved_intent")

        validation = self.settings.validate()
        if not validation.valid:
            raise MT5SubmitRefused("mt5_settings_invalid", validation.reason)
        self.settings.assert_trading_allowed()
        if not self.connected or self._status is None:
            raise MT5SubmitRefused("mt5_not_connected")
        if not self._armed:
            raise MT5SubmitRefused("mt5_trading_not_armed")

        # The account is whichever one the terminal has selected.  Re-read it,
        # and if it changed, revalidate *its* currency, leverage, margin mode,
        # symbols, and execution terms before routing anything.
        account_validation = self._ensure_account_current()
        # Arming was confirmed for one account.  A different one may not receive
        # the order, however valid it is on its own terms.
        if self._armed_fingerprint and self.account_fingerprint() != self._armed_fingerprint:
            raise MT5SubmitRefused(
                "mt5_account_changed_since_confirmation",
                self.account_fingerprint() or "unknown",
            )
        if not account_validation["valid"]:
            raise MT5SubmitRefused(
                "mt5_account_profile_invalid",
                ",".join(account_validation["problems"]),
            )
        if self._terms is None:
            raise MT5SubmitRefused("mt5_account_terms_refused")
        if not self._status.trade_allowed:
            raise MT5SubmitRefused("mt5_terminal_trading_not_allowed")

        if str(intent.account_currency).strip().upper() != self._terms.currency:
            raise MT5SubmitRefused(
                "mt5_account_currency_mismatch",
                f"intent={intent.account_currency} broker={self._terms.currency}",
            )

        unresolved = self.unresolved_request_ids()
        if unresolved:
            self.disarm(reason="unresolved_order_present")
            raise MT5SubmitRefused("mt5_unresolved_order_present", ",".join(unresolved))
        unresolved_protection = self.unresolved_protection_modification_ids()
        if unresolved_protection:
            self.disarm(reason="unresolved_protection_modification_present")
            raise MT5SubmitRefused(
                "mt5_unresolved_protection_modification_present",
                ",".join(unresolved_protection),
            )

        if self.lifecycle_lookup is None:
            raise MT5SubmitRefused("mt5_strategy_lifecycle_not_supplied")
        lifecycle = self.lifecycle_lookup(intent.strategy_id)
        if lifecycle is None:
            raise MT5SubmitRefused("mt5_strategy_unknown", intent.strategy_id)
        approve = getattr(lifecycle, "approve_active", None)
        if not callable(approve) or not approve():
            raise MT5SubmitRefused("mt5_strategy_not_approved_active", intent.strategy_id)

        if str(intent.spec_version) != str(spec.spec_version):
            raise MT5SubmitRefused(
                "mt5_spec_version_mismatch",
                f"intent={intent.spec_version} catalog={spec.spec_version}",
            )
        if str(intent.pair).replace("/", "").upper() != str(spec.pair).replace("/", "").upper():
            raise MT5SubmitRefused("mt5_intent_pair_mismatch")

        symbol, info = self.symbol_info(spec)
        try:
            require_trading(spec, info)
        except MT5DataRefused as exc:
            raise MT5SubmitRefused(exc.reason, exc.detail) from exc
        positions = self.positions(symbol=symbol)
        conflict = netting_conflict(
            positions,
            symbol=symbol,
            side=intent.side.value if hasattr(intent.side, "value") else str(intent.side),
            accounting=self._terms.position_accounting,
            approved_close=bool(intent.close_position_id),
        )
        if conflict:
            raise MT5SubmitRefused(conflict)

        close_ticket = 0
        if intent.close_position_id:
            match = [
                position
                for position in positions
                if str(intent.close_position_id) in {
                    str(position.ticket),
                    str(position.identifier),
                }
            ]
            if not match:
                raise MT5SubmitRefused("mt5_close_position_not_found", intent.close_position_id)
            position = match[0]
            requested = Decimal(str(intent.quantity))
            problem = close_volume_check(position, requested)
            if problem:
                raise MT5SubmitRefused("mt5_close_volume_invalid", problem)
            close_ticket = position.ticket

        tick = self.terminal.tick(symbol)
        side = intent.side.value if hasattr(intent.side, "value") else str(intent.side).upper()
        market_price = tick.ask if side == "BUY" else tick.bid
        drift = abs(market_price - Decimal(str(intent.executable_price)))
        point = spec.price_tick_size_decimal()
        drift_points = drift / point if point > 0 else Decimal("Infinity")
        if max_quote_age_seconds is not None:
            age = (self.clock() - tick.time).total_seconds()
            if age > float(max_quote_age_seconds):
                raise MT5SubmitRefused("mt5_quote_stale", f"age={age:.1f}s")
        if drift_points > Decimal(self.settings.max_price_drift_points):
            raise MT5SubmitRefused(
                "mt5_price_drift_exceeded",
                f"drift_points={drift_points.normalize()}",
            )

        lots, volume_result = to_broker_volume(spec, Decimal(str(intent.quantity)), info)
        if lots is None:
            raise MT5SubmitRefused("mt5_volume_invalid", volume_result.reason)
        margin = margin_for_order(
            self.terminal, side=side, symbol=symbol, volume=lots, price=market_price
        )
        if self._terms.margin_free is not None and margin > self._terms.margin_free:
            raise MT5SubmitRefused(
                "mt5_insufficient_free_margin",
                f"required={margin} free={self._terms.margin_free}",
            )

        # Protection is a property of the account, not only of the new order: a
        # position this deployment placed without a stop blocks the next entry
        # until it is dealt with.  Refusing here means the write-ahead row and
        # the order itself are never created.
        stop = {
            "declared": "",
            "distance_points": None,
            "valid": True,
            "reason": "",
        }
        if intent.close_position_id is None:
            stop = self.stop_requirement(spec, symbol=symbol, info=info)
            if not stop.get("valid"):
                raise MT5SubmitRefused(
                    stop.get("reason") or "mt5_protective_stop_unavailable",
                    stop.get("detail", ""),
                )
            try:
                unprotected = self.unprotected_positions()
            except Exception as exc:
                raise MT5SubmitRefused(
                    "mt5_positions_unavailable", type(exc).__name__
                ) from exc
            if unprotected:
                raise MT5SubmitRefused(
                    "mt5_unprotected_position_present",
                    ",".join(str(row.get("ticket", 0)) for row in unprotected),
                )

        return {
            "decision_id": str(getattr(decision, "audit_event", {}).get("event_id", "")),
            "request_id": intent.request_id,
            "strategy_id": intent.strategy_id,
            "strategy_stage": str(getattr(lifecycle, "stage", "")),
            "account_currency": self._terms.currency,
            "account_fingerprint": self.account_fingerprint(),
            "account_mode": self.account_mode,
            "position_accounting": self._terms.position_accounting,
            "symbol": symbol,
            "price_reference": market_price,
            "price_drift_points": drift_points,
            "volume_lots": lots,
            "margin_required": margin,
            "margin_free": self._terms.margin_free,
            "close_position_ticket": close_ticket,
            "protective_stop": dict(stop),
            "protective_stop_points": stop.get("distance_points") or 0,
            "checks": [
                "settings_valid",
                "trading_enabled",
                "connected",
                "armed",
                "account_revalidated",
                "trade_allowed",
                "account_currency_matches",
                "no_unresolved_order",
                "strategy_approved_active",
                "spec_version_matches",
                "symbol_terms_match",
                "no_netting_conflict",
                "quote_fresh",
                "price_drift_within_limit",
                "volume_valid",
                "margin_covered",
                "protective_stop_configured",
                "protective_stop_above_broker_minimum",
                "no_unprotected_position",
            ],
        }

    def _verify_stop(
        self,
        *,
        symbol: str,
        side: str,
        expected: Decimal | None,
        points: int,
        declared: str,
        point: Decimal | None,
        outcome: MT5OrderOutcome,
    ) -> dict[str, Any]:
        """Ask the broker what stop the position actually carries.

        The answer is never inferred from the request: a stop that was sent but
        is not visible on the broker's position record is ``verified: False``.
        """

        record: dict[str, Any] = {
            "declared": declared,
            "requested": "" if expected is None else str(expected),
            "points": int(points),
            "verified": False,
            "broker_stop_loss": "",
            "detail": "",
            "ticket": 0,
        }
        if not outcome.accepted:
            record["detail"] = "order_not_accepted"
            return record
        try:
            positions = self.positions(symbol=symbol)
        except Exception as exc:
            record["detail"] = f"positions_unavailable:{type(exc).__name__}"
            return record
        candidates = [
            position
            for position in positions
            if position.symbol.upper() == str(symbol).upper()
            and position.side == str(side).upper()
            and int(position.magic) == int(self.settings.magic)
        ]
        position = max(candidates, key=lambda item: item.ticket) if candidates else None
        tolerance = Decimal(point) if point is not None else Decimal(0)
        confirmed, detail = stop_loss_confirmed(position, expected, tolerance=tolerance)
        record["verified"] = bool(confirmed)
        record["detail"] = detail
        if position is not None:
            record["ticket"] = int(position.ticket)
            record["broker_stop_loss"] = (
                "" if position.stop_loss is None else str(position.stop_loss)
            )
        return record

    # -- auditing -------------------------------------------------------

    def _audit(self, event_type: str, **fields: Any) -> None:
        if self.audit_log is None:
            return
        payload: dict[str, Any] = {
            "event_type": event_type,
            "broker_id": BROKER_ID,
            "provider": "official_metatrader5_python",
            "execution_mode": self.capabilities.execution_mode,
        }
        for key in (
            "allowed",
            "reason",
            "request_id",
            "state",
            "symbol",
            "side",
            "volume",
            "price",
            "retcode",
            "retcode_name",
            "classification",
            "unknown",
            "permits_resend",
            "volume_filled",
            "account_login",
            "account_server",
            "account_currency",
            "trade_mode",
            "margin_mode",
            "trade_allowed",
            "mode",
            "evidence",
            "protective_stop",
            "protective_stop_points",
            "position_ticket",
            "target_price",
            "broker_stop_loss",
            "broker_take_profit",
            "verified",
            "action",
            "live",
        ):
            if key in fields:
                payload[key] = fields[key]
        self.audit_log.record(payload)


def make_mt5_adapter(
    *,
    settings: MT5Settings,
    password_store: MT5PasswordStore,
    repository: Any,
    audit_log: Any,
    module: Any | None = None,
    lifecycle_lookup: Callable[[str], Any] | None = None,
    terminal_factory: Callable[[], Any] | None = None,
) -> MT5TradingAdapter:
    """Constructor used by the facade and tests.  Sends nothing."""

    if not isinstance(settings, MT5Settings):
        raise MT5SettingsError("invalid_mt5_settings")
    if not isinstance(password_store, MT5PasswordStore):
        raise MT5SettingsError("invalid_mt5_password_store")
    return MT5TradingAdapter(
        settings=settings,
        password_store=password_store,
        repository=repository,
        audit_log=audit_log,
        module=module,
        lifecycle_lookup=lifecycle_lookup,
        terminal_factory=terminal_factory,
    )


__all__ = [
    "MT5BrokerCapabilities",
    "MT5OrderReceipt",
    "MT5SubmitRefused",
    "MT5TradingAdapter",
    "make_mt5_adapter",
    "MT5_FINAL_STATES",
    "MT5ExecutionMode",
]

"""The live-mode preflight: every genuine blocker at once, each with a way out.

The preflight answers one question -- *may live execution be switched on?* -- and
it answers it in full.  It never returns the first problem it found and stops:
the operator gets the complete list in one pass, in the order they would fix it,
each with the exact next step.

Two lists, deliberately separate:

``blockers``
    Conditions that stop live mode being enabled.  Each is checkable *now* from
    the settings document, the environment probe, the observation session, and
    the operator's own confirmations.

``order_requirements``
    Conditions the execution path re-checks for **every single order**, and
    which this screen can only report as "enforced at submission": approved
    active strategy, pre-trade gate, risk limits, exact account identity, quote
    freshness, volume, margin, unresolved orders.  They are listed so nothing
    is hidden, but they are not something a button here can satisfy.

Nothing in this module contacts a terminal, imports the MetaTrader 5 package,
opens a session, or reads a credential.  It reads values that were already
gathered and formats them as operator text.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Mapping

#: Blocker categories, used to group the list on screen.
CATEGORY_ENVIRONMENT = "environment"
CATEGORY_SESSION = "session"
CATEGORY_ACCOUNT = "account"
CATEGORY_CONFIGURATION = "configuration"
CATEGORY_STRATEGY = "strategy"

#: Where the resolution happens, so the UI can say so plainly.
WHERE_TERMINAL = "in MetaTrader 5"
WHERE_SETTINGS = "in mt5_settings.local.json"
WHERE_WORKSTATION = "in this workstation"
WHERE_MACHINE = "on this machine"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class LiveBlocker:
    """One reason live execution cannot be enabled yet, and how to clear it."""

    reason: str
    title: str
    message: str
    resolution: str
    category: str = CATEGORY_SESSION
    where: str = WHERE_WORKSTATION

    def audit_view(self) -> dict[str, Any]:
        return {
            "reason": self.reason,
            "title": self.title,
            "message": self.message,
            "resolution": self.resolution,
            "category": self.category,
            "where": self.where,
        }


@dataclass(frozen=True)
class StrategyBinding:
    """A strategy this deployment would submit through the live path.

    The shipped repository attaches none: AXIOM never invents a strategy.  A
    deployment that has one passes it in, and then the preflight can state
    whether it is approved and active.
    """

    strategy_id: str
    stage: str = ""
    approved_active: bool = False
    version: str = ""


@dataclass(frozen=True)
class PreflightFacts:
    """Everything the live preflight is allowed to look at."""

    settings: Any = None
    settings_reason: str = ""
    environment: Any = None
    session: Mapping[str, Any] = field(default_factory=dict)
    confirmed: bool = False
    selected_symbols: tuple[str, ...] = ()
    strategy: StrategyBinding | None = None
    #: The one-day paper test's own verdict: ``present``/``passed``/``summary``.
    #: ``None`` means this process has no record to read, which is not a pass.
    qualification: Mapping[str, Any] | None = None

    @property
    def connected(self) -> bool:
        return bool(self.session.get("connected"))

    @property
    def source_kind(self) -> str:
        return str(self.session.get("source_kind", "UNKNOWN"))

    @property
    def account(self) -> Mapping[str, Any]:
        snapshot = self.session.get("snapshot")
        if not isinstance(snapshot, Mapping):
            return {}
        account = snapshot.get("account")
        return account if isinstance(account, Mapping) else {}

    @property
    def terminal(self) -> Mapping[str, Any]:
        snapshot = self.session.get("snapshot")
        if not isinstance(snapshot, Mapping):
            return {}
        terminal = snapshot.get("terminal")
        return terminal if isinstance(terminal, Mapping) else {}

    @property
    def error(self) -> Mapping[str, Any]:
        error = self.session.get("error")
        return error if isinstance(error, Mapping) else {}


#: Reasons an explicit Connect attempt can fail, worded for a human, with the
#: step that actually fixes it.  ``mt5_account_mismatch`` is here on purpose:
#: the terminal's identity check is real, and it is surfaced -- never relaxed.
CONNECT_FAILURE_HELP: dict[str, tuple[str, str]] = {
    "mt5_account_mismatch": (
        "MetaTrader 5 is signed in to a different account than the one "
        "mt5_settings.local.json names.",
        "Sign in to the configured account inside MetaTrader 5, then press "
        "Disconnect and Connect again. AXIOM will not treat a different account as "
        "a match; change the settings document instead if you meant that account.",
    ),
    "mt5_server_mismatch": (
        "The terminal is connected to a different broker server than the one "
        "mt5_settings.local.json names.",
        "In MetaTrader 5, switch to the account on the configured server, then press "
        "Disconnect and Connect again. If the server name in the document is wrong, "
        "correct the document -- AXIOM never renames it for you.",
    ),
    "mt5_terminal_not_connected": (
        "The terminal answered but is not connected to a broker.",
        "Press Connect in MetaTrader 5, wait for the connection indicator to turn "
        "green, then press Connect here again.",
    ),
    "mt5_initialize_refused": (
        "MetaTrader 5 refused to start a session for this terminal path.",
        "Open MetaTrader 5 yourself, sign in, and leave it running; then press "
        "Connect here. If it keeps refusing, check the terminal path in the settings "
        "document.",
    ),
    "mt5_initialize_failed": (
        "The MetaTrader 5 package could not start a session.",
        "Make sure MetaTrader 5 is running and logged in on this machine, then press "
        "Connect again.",
    ),
    "metatrader5_package_missing": (
        "The official MetaTrader5 Python package is not installed for this "
        "interpreter.",
        "Install it with: py -m pip install MetaTrader5, then restart the workstation.",
    ),
    "mt5_settings_invalid": (
        "The settings document is incomplete or invalid.",
        "Fix the reported field in mt5_settings.local.json (terminal_path, server, "
        "account_login, mode), then restart the workstation.",
    ),
    "terminal_disconnected": (
        "The terminal closed the session.",
        "Reopen MetaTrader 5, make sure it is signed in to the intended account, then "
        "press Connect.",
    ),
}

#: Every order re-runs these.  They are shown so nothing is hidden; none of them
#: can be waived from a screen, and none of them is replaced by the mode switch.
ORDER_REQUIREMENTS: tuple[dict[str, str], ...] = (
    {
        "reason": "account_identity",
        "title": "Exact selected-account identity",
        "detail": "The account the order belongs to is re-checked against the terminal "
        "before submission; a different account stops the order.",
        "enforced_at": "order submission",
    },
    {
        "reason": "broker_connection",
        "title": "Broker connection",
        "detail": "The terminal must still be connected, with trading permitted in it.",
        "enforced_at": "order submission",
    },
    {
        "reason": "strategy_approved_active",
        "title": "Approved, active strategy",
        "detail": "The order's strategy must be approved and active, and the intent must "
        "match the approved one exactly.",
        "enforced_at": "order submission",
    },
    {
        "reason": "pretrade_gate",
        "title": "Pre-trade gate",
        "detail": "The existing gate, with the configured risk limits, must approve the "
        "request; the order is refused otherwise.",
        "enforced_at": "order submission",
    },
    {
        "reason": "quote_freshness",
        "title": "Fresh two-sided quote",
        "detail": "A stale, crossed, or incomplete quote stops the order.",
        "enforced_at": "order submission",
    },
    {
        "reason": "price_drift",
        "title": "Price drift, volume, and margin",
        "detail": "Drift beyond the configured tolerance, a volume the broker rejects, or "
        "insufficient free margin stops the order.",
        "enforced_at": "order submission",
    },
    {
        "reason": "unresolved_orders",
        "title": "No unresolved orders",
        "detail": "An in-doubt order blocks new orders until it is resolved from broker "
        "history.",
        "enforced_at": "order submission",
    },
    {
        "reason": "arming",
        "title": "Explicit arming of the session",
        "detail": "Live submission requires the terminal session to be armed with its "
        "own explicit phrase; the mode switch never arms anything.",
        "enforced_at": "order submission",
    },
)


def _settings_valid(settings: Any) -> tuple[bool, str]:
    if settings is None:
        return False, ""
    validate = getattr(settings, "validate", None)
    if not callable(validate):
        return True, ""
    result = validate()
    valid = bool(getattr(result, "valid", False))
    return valid, str(getattr(result, "reason", "") or "")


def _trading_enabled(settings: Any) -> bool:
    return bool(getattr(settings, "trading_enabled", False))


def collect_blockers(facts: PreflightFacts) -> list[LiveBlocker]:
    """Every reason live cannot be enabled, in the order they are fixed."""

    blockers: list[LiveBlocker] = []

    def add(
        reason: str,
        title: str,
        message: str,
        resolution: str,
        *,
        category: str = CATEGORY_SESSION,
        where: str = WHERE_WORKSTATION,
    ) -> None:
        blockers.append(
            LiveBlocker(
                reason=reason,
                title=title,
                message=message,
                resolution=resolution,
                category=category,
                where=where,
            )
        )

    # -- environment ----------------------------------------------------
    environment = facts.environment
    if environment is not None:
        if not bool(getattr(environment, "is_windows", True)):
            add(
                "not_windows",
                "This is not Windows",
                "MetaTrader 5 runs on Windows, so live execution cannot attach to a "
                "terminal from this machine.",
                "Run the workstation on the Windows machine that has MetaTrader 5 "
                "installed.",
                category=CATEGORY_ENVIRONMENT,
                where=WHERE_MACHINE,
            )
        packages = {item.name: item for item in getattr(environment, "packages", ())}
        package = packages.get("MetaTrader5")
        if package is not None and not package.installed:
            add(
                "metatrader5_package_missing",
                "MetaTrader5 package missing",
                "The official MetaTrader5 Python package is not installed for the "
                "interpreter running this workstation.",
                "Run: py -m pip install MetaTrader5  — then restart the workstation.",
                category=CATEGORY_ENVIRONMENT,
                where=WHERE_MACHINE,
            )
        if not bool(getattr(environment, "terminal_found", True)):
            add(
                "mt5_terminal_not_found",
                "MetaTrader 5 terminal not found",
                "No MetaTrader 5 terminal was found at the configured path or in the "
                "usual install locations.",
                "Install MetaTrader 5 (or fix terminal_path in mt5_settings.local.json), "
                "then press Check again on the Setup screen.",
                category=CATEGORY_ENVIRONMENT,
                where=WHERE_MACHINE,
            )

    # -- settings document ----------------------------------------------
    settings = facts.settings
    valid, validation_reason = _settings_valid(settings)
    if settings is None:
        reason = facts.settings_reason or "no_settings_document"
        title, message, resolution = (
            (
                "Settings document not loaded",
                f"The local settings document could not be used ({reason}).",
                "Create or fix mt5_settings.local.json from the template on the Setup "
                "screen, then restart the workstation.",
            )
            if reason
            else (
                "No settings document",
                "No mt5_settings.local.json was found next to the launcher.",
                "Create the settings document from the template on the Setup screen, "
                "then restart the workstation.",
            )
        )
        add(
            reason if reason else "no_settings_document",
            title,
            message,
            resolution,
            category=CATEGORY_CONFIGURATION,
            where=WHERE_SETTINGS,
        )
    elif not valid:
        add(
            "mt5_settings_invalid",
            "Settings document is invalid",
            "The settings document failed validation"
            + (f" ({validation_reason})" if validation_reason else "")
            + ".",
            "Fix the reported field in mt5_settings.local.json, then restart the "
            "workstation.",
            category=CATEGORY_CONFIGURATION,
            where=WHERE_SETTINGS,
        )
    elif not _trading_enabled(settings):
        add(
            "trading_not_enabled_in_settings",
            "Trading is not enabled in the settings document",
            "The settings document is in observe-only mode, so live execution is "
            "disabled by configuration.",
            'Set "mode": "TRADING_ENABLED" in mt5_settings.local.json (this is a local '
            "file you own; it holds no password), then restart the workstation.",
            category=CATEGORY_CONFIGURATION,
            where=WHERE_SETTINGS,
        )

    # -- session --------------------------------------------------------
    source_kind = facts.source_kind
    if source_kind == "SIMULATED":
        add(
            "simulated_session_only",
            "Only the offline simulator is running",
            "The simulator never contacts a terminal, so it can never carry live "
            "execution.",
            "Stop the simulator and start the workstation without --simulate on the "
            "Windows machine that runs MetaTrader 5.",
            category=CATEGORY_SESSION,
            where=WHERE_MACHINE,
        )
    elif source_kind == "UNCONFIGURED":
        add(
            "workstation_unconfigured",
            "The workstation is not configured",
            "This workstation has no usable terminal configuration, so it cannot open "
            "a session at all.",
            "Fix the settings document, then restart the workstation.",
            category=CATEGORY_CONFIGURATION,
            where=WHERE_SETTINGS,
        )

    error_reason = str(facts.error.get("reason", "") or "")
    if error_reason:
        title, resolution = CONNECT_FAILURE_HELP.get(
            error_reason,
            (
                "The last connection attempt did not succeed.",
                "Read the message on the Setup screen, fix it, then press Connect again.",
            ),
        )
        detail = str(facts.error.get("detail", "") or "")
        message = title if not detail else f"{title} ({detail})"
        # Never echo a reason twice; and never let a hidden failure sit behind the
        # generic "not connected" line.
        add(
            error_reason,
            "The last connection attempt failed",
            message,
            resolution,
            category=(
                CATEGORY_ACCOUNT
                if error_reason in {"mt5_account_mismatch", "mt5_server_mismatch"}
                else CATEGORY_SESSION
            ),
            where=(
                WHERE_TERMINAL
                if error_reason
                in {
                    "mt5_account_mismatch",
                    "mt5_server_mismatch",
                    "mt5_terminal_not_connected",
                    "mt5_initialize_refused",
                    "terminal_disconnected",
                }
                else WHERE_WORKSTATION
            ),
        )

    if not facts.connected:
        if not error_reason:
            add(
                "not_connected",
                "No terminal session is open",
                "Live execution needs a session that this process opened after you "
                "pressed Connect.",
                "Press Connect on the Setup screen. AXIOM never connects on its own.",
            )
    else:
        account = facts.account
        if not account:
            add(
                "account_not_observed",
                "No account was read",
                "The session is open but the terminal has not reported an account yet.",
                "Press Refresh, or Disconnect and Connect again.",
                category=CATEGORY_ACCOUNT,
            )
        else:
            if settings is not None:
                configured_server = str(getattr(settings, "server", "") or "").strip().lower()
                observed_server = str(account.get("server", "") or "").strip().lower()
                if configured_server and observed_server and configured_server != observed_server:
                    add(
                        "mt5_server_mismatch",
                        "Different broker server than configured",
                        "The connected terminal reports a different server than "
                        "mt5_settings.local.json names. AXIOM will not treat one account "
                        "as another.",
                        "Sign in to the account on the configured server inside "
                        "MetaTrader 5, then press Disconnect and Connect again -- or "
                        "correct the settings document if that server is the one you "
                        "meant.",
                        category=CATEGORY_ACCOUNT,
                        where=WHERE_TERMINAL,
                    )
            if not bool(account.get("mode_confirmed", False)):
                add(
                    "account_mode_unconfirmed",
                    "Account type not reported",
                    "The terminal did not state whether this account is demo or real, and "
                    "AXIOM will not enable live execution while it cannot say which it "
                    "is.",
                    "Update MetaTrader 5 to a current build, reconnect, and check the "
                    "account type shown on Account & symbols.",
                    category=CATEGORY_ACCOUNT,
                    where=WHERE_TERMINAL,
                )
            if not bool(account.get("trade_allowed", False)):
                add(
                    "terminal_trading_not_allowed",
                    "Trading is not allowed in the terminal",
                    "The terminal reports that trading is not permitted for this "
                    "session (Algo Trading may be off, or the account may be read-only).",
                    "Enable Algo Trading in MetaTrader 5's toolbar and make sure the "
                    "account may trade; then Disconnect and Connect again.",
                    category=CATEGORY_ACCOUNT,
                    where=WHERE_TERMINAL,
                )

    # -- the operator's own confirmations -------------------------------
    if not facts.selected_symbols:
        add(
            "symbols_not_selected",
            "No symbols selected",
            "Live execution needs the symbol set you reviewed to be selected.",
            "Choose at least one symbol on the Account & symbols screen.",
            category=CATEGORY_CONFIGURATION,
        )
    if not facts.confirmed:
        add(
            "configuration_not_confirmed",
            "Account and symbols not confirmed",
            "You have not confirmed that the detected account and symbol set are the "
            "ones you mean.",
            "Press Confirm configuration on the Account & symbols screen.",
            category=CATEGORY_CONFIGURATION,
        )

    # -- the paper test that makes LIVE available at all -----------------
    qualification = facts.qualification
    passed = bool((qualification or {}).get("passed", False))
    if not passed:
        summary = str((qualification or {}).get("summary", "") or "").strip()
        add(
            "paper_test_not_passed",
            "The one-day paper test has not passed",
            summary
            or "No live-market paper test has been completed on this installation.",
            "Run the paper test on live quotes for a continuous 24 counted hours and "
            "read its report. It makes LIVE available; it never enables or arms it.",
            category=CATEGORY_SESSION,
        )

    # -- a strategy, when this deployment declares one ------------------
    strategy = facts.strategy
    if strategy is not None and not strategy.approved_active:
        add(
            "strategy_not_approved",
            "The attached strategy is not approved and active",
            f"Strategy {strategy.strategy_id or '<unnamed>'} is in stage "
            f"{strategy.stage or 'unknown'}, so it cannot submit live orders.",
            "Move that strategy to APPROVED through the usual workflow, or attach a "
            "different approved strategy; AXIOM does not approve strategies for you.",
            category=CATEGORY_STRATEGY,
        )

    return blockers


def dedupe(blockers: Iterable[LiveBlocker]) -> tuple[LiveBlocker, ...]:
    """One entry per reason; the first (most actionable) wording wins."""

    seen: set[str] = set()
    ordered: list[LiveBlocker] = []
    for blocker in blockers:
        if blocker.reason in seen:
            continue
        seen.add(blocker.reason)
        ordered.append(blocker)
    return tuple(ordered)


def facts_from(
    *,
    settings: Any = None,
    settings_reason: str = "",
    environment: Any = None,
    session: Mapping[str, Any] | None = None,
    confirmed: bool = False,
    selected_symbols: Iterable[str] = (),
    strategy: StrategyBinding | None = None,
    qualification: Mapping[str, Any] | None = None,
) -> PreflightFacts:
    """Build facts from the objects the workstation already holds."""

    return PreflightFacts(
        settings=settings,
        settings_reason=settings_reason,
        environment=environment,
        session=dict(session or {}),
        confirmed=bool(confirmed),
        selected_symbols=tuple(name for name in selected_symbols if name),
        strategy=strategy,
        qualification=dict(qualification) if qualification else None,
    )


@dataclass
class LivePreflight:
    """Runs the blocker list on demand, from a facts provider."""

    provider: Callable[[], PreflightFacts]
    clock: Callable[[], datetime] | None = None

    def facts(self) -> PreflightFacts:
        return self.provider()

    def blockers(self) -> tuple[LiveBlocker, ...]:
        return dedupe(collect_blockers(self.facts()))

    def view(self) -> dict[str, Any]:
        blockers = self.blockers()
        facts = self.facts()
        account = facts.account
        by_category: dict[str, int] = {}
        for blocker in blockers:
            by_category[blocker.category] = by_category.get(blocker.category, 0) + 1
        return {
            "ready": not blockers,
            "blocked": bool(blockers),
            "blockers": [blocker.audit_view() for blocker in blockers],
            "count": len(blockers),
            "by_category": by_category,
            "order_requirements": [dict(item) for item in ORDER_REQUIREMENTS],
            "strategy_attached": facts.strategy is not None,
            "account": {
                "server": str(account.get("server", "") or ""),
                "mode": str(account.get("mode", "") or "UNKNOWN"),
                "mode_confirmed": bool(account.get("mode_confirmed", False)),
                "margin_mode": str(account.get("margin_mode", "") or ""),
                "currency": str(account.get("currency", "") or ""),
                "fingerprint": str(account.get("fingerprint", "") or ""),
                "sample": bool(account.get("sample", False)),
            },
            "session_connected": facts.connected,
            "source_kind": facts.source_kind,
            "checked_at": (self.clock or _utc_now)(),
            "summary": (
                "Every live check passed. Enabling live still sends nothing; orders "
                "must pass the pre-trade gate and be submitted by an approved strategy."
                if not blockers
                else f"{len(blockers)} check(s) must pass before live execution can be "
                "enabled. Paper trading stays available and immediate."
            ),
        }


__all__ = [
    "CATEGORY_ACCOUNT",
    "CATEGORY_CONFIGURATION",
    "CATEGORY_ENVIRONMENT",
    "CATEGORY_SESSION",
    "CATEGORY_STRATEGY",
    "CONNECT_FAILURE_HELP",
    "LiveBlocker",
    "LivePreflight",
    "ORDER_REQUIREMENTS",
    "PreflightFacts",
    "StrategyBinding",
    "collect_blockers",
    "dedupe",
    "facts_from",
]

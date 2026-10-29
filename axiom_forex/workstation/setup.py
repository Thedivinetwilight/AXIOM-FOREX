"""Guided first-run setup: six steps, derived from facts, never assumed.

The wizard is a pure function of three inputs -- the environment report, the
observation session, and the operator's own choices -- so every state can be
rendered and tested without a terminal.  It holds no secret, writes no file, and
has no code path that can trade.

Step statuses:

``complete``        the step's evidence is present
``action_required`` the operator has something to do here
``blocked``         an earlier fact makes this impossible (with the reason)
``pending``         waiting on an earlier step
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from .probes import EnvironmentReport, prerequisite_help, settings_template
from .session import ObservationSession

COMPLETE = "complete"
ACTION_REQUIRED = "action_required"
BLOCKED = "blocked"
PENDING = "pending"

#: The order the steps are always presented in.
STEP_ORDER: tuple[str, ...] = (
    "environment",
    "sign_in",
    "connect",
    "account",
    "confirm",
    "ready",
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class SetupChoices:
    """Everything the operator decided.  No secrets, no free-text credentials."""

    login_acknowledged: bool = False
    selected_symbols: tuple[str, ...] = ()
    confirmed: bool = False
    configured_at: datetime | None = None

    def with_symbols(self, names: Sequence[str]) -> "SetupChoices":
        cleaned = tuple(sorted({str(name).strip() for name in names if str(name).strip()}))
        return replace(self, selected_symbols=cleaned, confirmed=False)


@dataclass(frozen=True)
class SetupStep:
    id: str
    number: int
    title: str
    summary: str
    status: str
    detail: str = ""
    action: str = ""
    action_label: str = ""
    items: tuple[tuple[str, str, str], ...] = ()

    @property
    def complete(self) -> bool:
        return self.status == COMPLETE

    def audit_view(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "number": self.number,
            "title": self.title,
            "summary": self.summary,
            "status": self.status,
            "detail": self.detail,
            "action": self.action,
            "action_label": self.action_label,
            "items": [
                {"label": label, "value": value, "tone": tone}
                for label, value, tone in self.items
            ],
        }


@dataclass
class SetupWizard:
    """Six steps over the environment, the session, and the operator's choices."""

    environment: EnvironmentReport
    session: ObservationSession
    choices: SetupChoices = field(default_factory=SetupChoices)
    clock: Any = None

    # -- operator actions (each one explicit) ---------------------------

    def acknowledge_sign_in(self) -> SetupChoices:
        self.choices = replace(self.choices, login_acknowledged=True)
        return self.choices

    def unacknowledge_sign_in(self) -> SetupChoices:
        self.choices = replace(self.choices, login_acknowledged=False, confirmed=False)
        return self.choices

    def choose_symbols(self, names: Sequence[str]) -> SetupChoices:
        self.choices = self.choices.with_symbols(names)
        return self.choices

    def confirm(self) -> SetupChoices:
        """Confirm the reviewed configuration.

        Refused unless a *real* terminal account was actually observed. A
        simulated preview has no account, and confirming one would be a
        statement about a broker that was never contacted.
        """

        if not self.live_terminal:
            raise ValueError("confirm_requires_live_terminal")
        if not self.session.connected or not self.session.account():
            raise ValueError("confirm_requires_observed_account")
        if not self.choices.selected_symbols:
            raise ValueError("confirm_requires_selected_symbols")
        self.choices = replace(
            self.choices,
            confirmed=True,
            configured_at=(self.clock or _utc_now)(),
        )
        return self.choices

    def reset_confirmation(self) -> SetupChoices:
        self.choices = replace(self.choices, confirmed=False, configured_at=None)
        return self.choices

    # -- derived --------------------------------------------------------

    @property
    def connected(self) -> bool:
        return self.session.connected

    @property
    def account(self) -> Mapping[str, Any]:
        return self.session.account()

    @property
    def detected_symbols(self) -> tuple[str, ...]:
        names = []
        for item in self.session.symbols():
            name = str(item.get("name", "")).strip()
            if name:
                names.append(name)
        return tuple(names)

    @property
    def effective_symbols(self) -> tuple[str, ...]:
        return self.choices.selected_symbols or self.detected_symbols

    @property
    def simulated(self) -> bool:
        return getattr(self.session.source, "kind", "") == "SIMULATED"

    @property
    def live_terminal(self) -> bool:
        """True only for a session attached to the real local MT5 terminal."""

        return (
            getattr(self.session.source, "kind", "") == "LIVE_TERMINAL"
            and self.session.connected
        )

    @property
    def ready_for_observation(self) -> bool:
        """Ready means: a real terminal answered after an explicit Connect.

        There is deliberately no simulator path to this state. The offline
        preview exists to look at the interface, and a preview must never be
        reported as an observation of a broker account.
        """

        return bool(
            self.live_terminal
            and self.environment.ready_to_connect
            and self.choices.confirmed
            and self.choices.selected_symbols
        )

    @property
    def ready_basis(self) -> str:
        if self.simulated:
            return "simulated_session_only"
        if not self.connected:
            return "not_connected"
        if not self.environment.ready_to_connect:
            return "environment_incomplete"
        return "live_terminal"

    @property
    def blockers(self) -> tuple[str, ...]:
        reasons = list(self.environment.blocking)
        if not self.environment.settings_present:
            reasons.append("settings_missing")
        if self.simulated:
            reasons.append("simulated_session_only")
        if not self.connected:
            reasons.append("not_connected")
        if not self.choices.confirmed:
            reasons.append("configuration_not_confirmed")
        return tuple(dict.fromkeys(reasons))

    def account_mode(self) -> dict[str, Any]:
        """Detected mode, plus whether the operator has to make a choice."""

        account = self.account
        detected = str(account.get("mode", "UNKNOWN")) if account else "UNKNOWN"
        return {
            "detected": detected,
            "detected_confirmed": bool(account.get("mode_confirmed", False)) if account else False,
            "sample": bool(account.get("sample", False)) if account else False,
            "is_demo": detected == "DEMO" and bool(account.get("sample", False)) is False,
            "is_real": detected == "REAL" and bool(account.get("sample", False)) is False,
            "note": (
                "Detected from the terminal. AXIOM never switches accounts; if this is "
                "not the account you meant, change it inside MetaTrader 5 and connect again."
            ),
        }

    # -- the steps ------------------------------------------------------

    def steps(self) -> tuple[SetupStep, ...]:
        env = self.environment
        session = self.session
        steps: list[SetupStep] = []

        # 1. environment
        env_items = [
            (
                "Operating system",
                f"{env.os_name} {env.os_version}".strip(),
                "ok" if env.is_windows else "warn",
            ),
            (
                "Python",
                env.python_version,
                "ok" if env.python_ok else "warn",
            ),
        ]
        for package in env.packages:
            env_items.append(
                (
                    package.name,
                    "installed" if package.installed else package.status.replace("-", " "),
                    "ok" if package.installed else ("warn" if package.required else "muted"),
                )
            )
        env_items.append(
            (
                "MT5 terminal",
                "found" if env.terminal_found else "not found",
                "ok" if env.terminal_found else "warn",
            )
        )
        env_items.append(
            (
                "Local settings",
                "present" if env.settings_present else (env.settings_error or "not created"),
                "ok" if env.settings_present else "warn",
            )
        )
        steps.append(
            SetupStep(
                id="environment",
                number=1,
                title="Check this machine",
                summary=(
                    "We look for Windows, a supported Python, the MetaTrader5 package, "
                    "the MT5 terminal, and your local settings file."
                ),
                status=COMPLETE if env.ready_to_connect else ACTION_REQUIRED,
                detail="" if env.ready_to_connect else "Some prerequisites still need attention.",
                action="recheck",
                action_label="Check again",
                items=tuple(env_items),
            )
        )

        # 2. sign in through MT5 itself
        signed_in = self.choices.login_acknowledged or self.live_terminal
        steps.append(
            SetupStep(
                id="sign_in",
                number=2,
                title="Sign in inside MetaTrader 5",
                summary=(
                    "Open the terminal, log in to the account you want to observe, and "
                    "leave it running. AXIOM reuses that session and never asks for, "
                    "stores, or displays your MT5 password."
                ),
                status=COMPLETE if signed_in else (ACTION_REQUIRED if env.ready_to_connect else PENDING),
                detail=(
                    "Noted: you signed in inside MetaTrader 5."
                    if self.choices.login_acknowledged
                    else (
                        "Connected to the terminal, which means its session is signed in."
                        if self.live_terminal
                        else "Tick the confirmation when the terminal is open and signed in."
                    )
                ),
                action="acknowledge-sign-in",
                action_label="I have signed in to MT5",
            )
        )

        # 3. connect
        if self.live_terminal:
            connect_status = COMPLETE
            connect_detail = (
                "Connected to the local terminal. Whether an approved order would be "
                "simulated or routed to MetaTrader 5 is your choice on the Trading mode "
                "screen; connecting on its own never enables trading."
            )
            connect_action, connect_label = "disconnect", "Disconnect"
        elif self.simulated:
            # The preview is explicitly *not* step 3. It stays open until a real
            # terminal answers, so no green tick can ever mean "MT5 connected".
            connect_status = ACTION_REQUIRED
            connect_detail = (
                "Simulator preview running. No MetaTrader 5 terminal is attached, so "
                "this step is not complete."
                if session.connected
                else "Offline simulator: this button previews the interface with sample "
                "data. It does not contact MetaTrader 5 and cannot complete this step."
            )
            connect_action = "disconnect" if session.connected else "connect"
            connect_label = "Stop simulator preview" if session.connected else "Start simulator preview (not MT5)"
        elif not env.ready_to_connect:
            connect_status = BLOCKED
            connect_detail = "Finish step 1 first: " + ", ".join(env.blocking or ("settings_missing",))
            connect_action, connect_label = "connect", "Connect"
        elif session.state == "ERROR":
            error = session.last_error
            connect_status = ACTION_REQUIRED
            connect_detail = (
                f"Last attempt was refused: {error.reason if error else 'unknown'}. "
                + (error.hint if error and error.hint else "")
            )
            connect_action, connect_label = "connect", "Connect"
        else:
            connect_status = ACTION_REQUIRED
            connect_detail = "Not connected. AXIOM only connects when you press Connect."
            connect_action, connect_label = "connect", "Connect"
        steps.append(
            SetupStep(
                id="connect",
                number=3,
                title="Connect to the terminal",
                summary=(
                    "AXIOM attaches to the terminal that is already running. It reads "
                    "status, account terms, symbols, and the live ticks paper mode is "
                    "priced from. Pressing Connect never places an order."
                ),
                status=connect_status,
                detail=connect_detail,
                action=connect_action,
                action_label=connect_label,
            )
        )

        # 4. what the terminal says
        account = self.account
        if session.connected and account:
            sample = bool(account.get("sample", False))
            items = (
                (
                    "Account",
                    f"…{account.get('fingerprint', '')}"
                    if account.get("fingerprint")
                    else "<redacted>",
                    "ok" if not sample else "warn",
                ),
                ("Broker / server", str(account.get("server", "unknown")), "ok" if not sample else "warn"),
                ("Account currency", str(account.get("currency", "unknown")), "ok" if not sample else "warn"),
                ("Leverage", f"1:{account.get('leverage', 'unknown')}", "ok" if not sample else "warn"),
                (
                    "Account mode",
                    str(account.get("mode", "UNKNOWN")),
                    ("ok" if account.get("mode_confirmed") else "warn") if not sample else "warn",
                ),
                ("Margin mode", str(account.get("margin_mode", "unknown")), "muted"),
                (
                    "Terminal trading permission",
                    "allowed" if account.get("trade_allowed") else "not allowed",
                    "muted",
                ),
                (
                    "Symbols available",
                    f"{len(self.detected_symbols)}",
                    "ok" if self.detected_symbols and not sample else "warn",
                ),
            )
            if sample:
                items = tuple((f"Sample {label}", value, "warn") for label, value, _tone in items)
            steps.append(
                SetupStep(
                    id="account",
                    number=4,
                    title="Review the sample account (preview)" if sample else "Review the detected account",
                    summary=(
                        "Illustrative values from the offline simulator. No broker was "
                        "contacted and nothing here describes a real account."
                        if sample
                        else "Read from the terminal -- nothing here is typed in by AXIOM."
                    ),
                    status=ACTION_REQUIRED if sample else COMPLETE,
                    detail=(
                        "Sample data only. Connect the real terminal on Windows to read "
                        "your actual account."
                        if sample
                        else ""
                    ),
                    items=items,
                )
            )
        else:
            steps.append(
                SetupStep(
                    id="account",
                    number=4,
                    title="Review the detected account",
                    summary="Read from the terminal once connected.",
                    status=PENDING,
                    detail="Connect first; then the account, broker, currency, leverage, and symbols appear here.",
                )
            )

        # 5. confirm the configuration
        if not session.connected:
            confirm_status = PENDING
            confirm_detail = "Available after step 3."
        elif self.simulated:
            confirm_status = BLOCKED
            confirm_detail = (
                "A simulator preview cannot be confirmed. Confirmation records that a "
                "real account and symbol set were reviewed."
            )
        elif not self.choices.selected_symbols:
            confirm_status = ACTION_REQUIRED
            confirm_detail = "Choose at least one symbol to observe."
        elif self.choices.confirmed:
            confirm_status = COMPLETE
            confirm_detail = f"Confirmed with {len(self.choices.selected_symbols)} symbol(s)."
        else:
            confirm_status = ACTION_REQUIRED
            confirm_detail = "Review and confirm the account and symbol selection."
        steps.append(
            SetupStep(
                id="confirm",
                number=5,
                title="Confirm account and symbols",
                summary=(
                    "Nothing is inferred: the broker, account, and symbols are the ones "
                    "you confirm here."
                ),
                status=confirm_status,
                detail=confirm_detail,
                action="confirm" if self.live_terminal else "",
                action_label="Confirm configuration" if self.live_terminal else "",
            )
        )

        # 6. ready
        if self.ready_for_observation:
            ready = SetupStep(
                id="ready",
                number=6,
                title="Ready for observation",
                summary=(
                    "The workstation is watching the account you selected. Observation "
                    "only: no order entry, no arming, no automatic reconnection."
                ),
                status=COMPLETE,
                items=(
                    ("Terminal session", "connected after your click", "ok"),
                    ("Order entry in this interface", "not available", "ok"),
                    ("Arming controls in this interface", "not available", "ok"),
                    ("Execution mode", "chosen on the Trading mode screen", "muted"),
                    ("Auto-reconnect", "off", "ok"),
                    ("Symbols observed", ", ".join(self.choices.selected_symbols) or "none", "muted"),
                ),
            )
        elif self.simulated:
            ready = SetupStep(
                id="ready",
                number=6,
                title="Ready for observation",
                summary="Reached only by a real MetaTrader 5 connection.",
                status=BLOCKED,
                detail=(
                    "The offline simulator can never reach observation mode. On the "
                    "Windows machine: sign in inside MetaTrader 5, press Connect, review "
                    "the detected account, then confirm."
                ),
            )
        else:
            ready = SetupStep(
                id="ready",
                number=6,
                title="Ready for observation",
                summary="Finish the steps above to reach observation mode.",
                status=PENDING,
                detail=", ".join(self.blockers) or "",
            )
        steps.append(ready)
        return tuple(steps)

    # -- surfaces -------------------------------------------------------

    def current_step(self) -> SetupStep:
        for step in self.steps():
            if step.status in {ACTION_REQUIRED, BLOCKED}:
                return step
        return self.steps()[-1]

    def progress(self) -> dict[str, Any]:
        steps = self.steps()
        done = sum(1 for step in steps if step.complete)
        return {
            "completed": done,
            "total": len(steps),
            "percent": round(100 * done / len(steps)) if steps else 0,
            "current_step_id": self.current_step().id,
        }

    def view(self) -> dict[str, Any]:
        return {
            "steps": [step.audit_view() for step in self.steps()],
            "progress": self.progress(),
            "ready_for_observation": self.ready_for_observation,
            "ready_basis": self.ready_basis,
            "simulated": self.simulated,
            "live_terminal": self.live_terminal,
            "blockers": list(self.blockers),
            "account_mode": self.account_mode(),
            "account": dict(self.account),
            "symbols_detected": list(self.detected_symbols),
            "symbols_selected": list(self.choices.selected_symbols),
            "confirmed": self.choices.confirmed,
            "configured_at": self.choices.configured_at,
            "login_acknowledged": self.choices.login_acknowledged,
            "environment": self.environment.audit_view(),
            "prerequisites": prerequisite_help(self.environment),
            "settings_template": settings_template(self.environment),
            "session": self.session.view(),
            "safety": safety_statement(),
            "trading_capability": "none",
        }


def safety_statement() -> dict[str, Any]:
    """The one place the UI's guarantees are written down for display."""

    return {
        #: The one trading-shaped control the interface owns. It changes how an
        #: approved order *would* be executed; it cannot create one.
        "trading_controls": ["execution_mode"],
        "execution_modes": ["PAPER", "LIVE"],
        "order_entry": False,
        "arming": False,
        "account_switching": False,
        "auto_reconnect": False,
        "stores_credentials": False,
        "asks_for_mt5_password": False,
        "market_access_token": False,
        "lines": (
            "Paper mode simulates every fill locally and never sends an order.",
            "Live mode stays off until every live check passes and you type the "
            "confirmation phrase once; the switch itself places no order.",
            "In either mode an order must still pass the pre-trade gate, come from an "
            "approved active strategy, and match the account exactly.",
            "Connects only when you press Connect; disconnects only when you press Disconnect.",
            "Never switches accounts and never logs in to MetaTrader 5 for you.",
            "Never asks for, stores, or displays your MT5 password.",
            "There is no order-entry form, arming control, or cancel/modify control here.",
        ),
    }


__all__ = [
    "ACTION_REQUIRED",
    "BLOCKED",
    "COMPLETE",
    "PENDING",
    "STEP_ORDER",
    "SetupChoices",
    "SetupStep",
    "SetupWizard",
    "safety_statement",
]

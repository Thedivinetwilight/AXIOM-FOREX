"""Read model for the workstation pages.

One place assembles everything a screen shows, from two independent sources that
are never blended:

* **terminal facts** -- whatever the observation session read, labelled
  ``REAL BROKER`` when a live terminal answered and ``SIMULATED`` otherwise;
* **paper facts** -- the fixture/paper platform's own status, always labelled
  with its data class (``SYNTHETIC_FIXTURE``, ``PAPER``, imported research, …).

The execution mode (paper vs live) and the live preflight travel alongside, as
separate layers: neither is derived from the other, and neither changes what the
terminal facts are.

If a read fails, the page renders an error/empty state instead of inventing a
value.  Nothing here mutates anything.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping

from app.audit.log import sanitize_audit_value

from .mode import default_mode_view
from .setup import SetupWizard, safety_statement

#: Data-class labels shown next to every figure, so real/paper/fixture can never
#: be confused at a glance.
DATA_CLASS_LABELS: dict[str, tuple[str, str]] = {
    "LIVE_BROKER": ("REAL BROKER", "live"),
    "LIVE_BROKER_TICK": ("REAL BROKER", "live"),
    "SIMULATED": ("SIMULATED", "simulated"),
    "SYNTHETIC_FIXTURE": ("SAMPLE FIXTURE", "fixture"),
    "USER_SUPPLIED_DEMO": ("SAMPLE (USER)", "fixture"),
    "USER_SUPPLIED_PRODUCTION": ("USER DATA", "paper"),
    "PAPER": ("PAPER", "paper"),
    "IMPORTED_RESEARCH": ("RESEARCH DATA", "paper"),
}


def data_class_label(value: Any) -> dict[str, str]:
    key = str(value or "UNKNOWN").upper()
    label, tone = DATA_CLASS_LABELS.get(key, (key.replace("_", " "), "muted"))
    return {"data_class": key, "label": label, "tone": tone}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class PaperSnapshot:
    """The paper/fixture platform's own numbers, with their provenance."""

    available: bool
    data_class: str
    source: str
    account_currency: str
    equity: str
    gross_exposure: str
    margin: str
    open_positions: tuple[Mapping[str, Any], ...]
    trades_today: str
    realized_pnl_today: str
    calendar_status: str
    calendar_window: str
    calendar_next_open: str
    health_status: str
    health_checks: tuple[tuple[str, bool, str], ...]
    workflow_state: str
    orders: tuple[Mapping[str, Any], ...]
    activity: tuple[Mapping[str, Any], ...]
    error: str = ""

    @staticmethod
    def unavailable(reason: str) -> "PaperSnapshot":
        return PaperSnapshot(
            available=False,
            data_class="UNKNOWN",
            source="",
            account_currency="",
            equity="",
            gross_exposure="",
            margin="",
            open_positions=(),
            trades_today="",
            realized_pnl_today="",
            calendar_status="",
            calendar_window="",
            calendar_next_open="",
            health_status="",
            health_checks=(),
            workflow_state="",
            orders=(),
            activity=(),
            error=reason,
        )


def _safe(value: Any, default: str = "--") -> str:
    if value is None or value == "" or value == [] or value == {}:
        return default
    return str(value)


def read_paper_snapshot(platform: Any, *, activity_limit: int = 8) -> PaperSnapshot:
    """Read the paper platform without letting a failure break the page."""

    try:
        status = sanitize_audit_value(platform.status())
    except Exception as exc:
        return PaperSnapshot.unavailable(type(exc).__name__)
    if not isinstance(status, dict):
        return PaperSnapshot.unavailable("status_not_a_mapping")

    portfolio = status.get("portfolio", {}) or {}
    calendar = status.get("calendar", {}) or {}
    calendar_state = calendar.get("state", {}) or {}
    health = status.get("health", {}) or {}
    workflow = status.get("workflow", {}) or {}
    provenance = status.get("data_provenance", {}) or {}

    positions = tuple(portfolio.get("open_positions", ()) or ())
    checks = tuple(
        (
            str(check.get("name", "")),
            bool(check.get("ok")),
            str(check.get("detail", "")),
        )
        for check in health.get("checks", ()) or ()
    )
    try:
        orders = tuple(platform.orders() or ())
    except Exception:
        orders = ()
    try:
        activity = tuple(platform.audit_events(limit=activity_limit) or ())
    except Exception:
        activity = ()

    return PaperSnapshot(
        available=True,
        data_class=str(provenance.get("data_class", "UNKNOWN")),
        source=str(provenance.get("source", "")),
        account_currency=str(portfolio.get("account_currency", "")),
        equity=_safe(portfolio.get("equity_account")),
        gross_exposure=_safe(portfolio.get("gross_exposure_account")),
        margin=_safe(portfolio.get("margin_account")),
        open_positions=positions,
        trades_today=_safe(portfolio.get("trades_today"), "0"),
        realized_pnl_today=_safe(portfolio.get("realized_pnl_today_account"), "0"),
        calendar_status=str(calendar_state.get("status", "UNKNOWN")),
        calendar_window=str(calendar_state.get("window_label", "")),
        calendar_next_open=str(calendar_state.get("next_open_at", "")),
        health_status=str(health.get("status", "UNKNOWN")),
        health_checks=checks,
        workflow_state=(
            "blocked"
            if workflow.get("blocked")
            else "executed"
            if workflow.get("executed")
            else "not run"
        ),
        orders=orders,
        activity=activity,
    )


@dataclass(frozen=True)
class TerminalView:
    """The observation session, flattened for display."""

    state: str
    connected: bool
    simulated: bool
    source_label: str
    mode: str
    mode_confirmed: bool
    server: str
    currency: str
    leverage: str
    margin_mode: str
    fingerprint: str
    terminal_name: str
    build: str
    trade_allowed: bool
    symbols: tuple[Mapping[str, Any], ...]
    error: Mapping[str, Any] | None
    connected_at: Any
    #: ``TERMINAL_TICKS`` when a live terminal is connected (paper mode would be
    #: priced from real ticks), ``SAMPLE`` for the simulator, ``NONE`` otherwise.
    market_data: str = "NONE"

    @property
    def data_class(self) -> str:
        return "SIMULATED" if self.simulated else "LIVE_BROKER"


def read_terminal_view(session: Any) -> TerminalView:
    view = session.view()
    snapshot = view.get("snapshot", {}) or {}
    account = snapshot.get("account", {}) or {}
    terminal = snapshot.get("terminal", {}) or {}
    return TerminalView(
        state=str(view.get("state", "DISCONNECTED")),
        connected=bool(view.get("connected")),
        simulated=str(view.get("source_kind")) == "SIMULATED",
        source_label=str(view.get("source_label", "")),
        mode=str(account.get("mode", "UNKNOWN")),
        mode_confirmed=bool(account.get("mode_confirmed", False)),
        server=str(account.get("server", "")),
        currency=str(account.get("currency", "")),
        leverage=f"1:{account.get('leverage')}" if account.get("leverage") else "",
        margin_mode=str(account.get("margin_mode", "")),
        fingerprint=str(account.get("fingerprint", "")),
        terminal_name=str(terminal.get("name", "")),
        build=str(terminal.get("build", "")),
        trade_allowed=bool(account.get("trade_allowed")),
        symbols=tuple(snapshot.get("symbols", ()) or ()),
        error=view.get("error"),
        connected_at=view.get("connected_at"),
        market_data=str(view.get("market_data", "NONE") or "NONE"),
    )


@dataclass(frozen=True)
class PageContext:
    """Everything a page template receives."""

    page: str
    title: str
    wizard: SetupWizard
    paper: PaperSnapshot
    terminal: TerminalView
    action_nonce: str
    platform_version: str
    generated_at: datetime
    notice: str = ""
    readonly: bool = True
    extra: Mapping[str, Any] = field(default_factory=dict)
    #: Paper vs live execution mode, as rendered by the persistent indicator.
    execution_mode: Mapping[str, Any] = field(default_factory=dict)
    #: The live preflight, recomputed for each page load.  Never cached.
    preflight: Mapping[str, Any] = field(default_factory=dict)
    #: The paper strategy runner's own state, read from its owner thread.
    #: Empty when no runner is configured, which is the default.
    runner: Mapping[str, Any] = field(default_factory=dict)

    @property
    def safety(self) -> dict[str, Any]:
        return safety_statement()

    @property
    def paper_label(self) -> dict[str, str]:
        return data_class_label(self.paper.data_class)

    @property
    def terminal_label(self) -> dict[str, str]:
        if self.terminal.connected:
            return data_class_label(self.terminal.data_class)
        return {"data_class": "NONE", "label": "NOT CONNECTED", "tone": "muted"}

    @property
    def account_mode(self) -> dict[str, Any]:
        return self.wizard.account_mode()

    @property
    def live_mode(self) -> bool:
        return str(self.execution_mode.get("mode", "PAPER")).upper() == "LIVE"

    @property
    def live_effective(self) -> bool:
        return bool(self.execution_mode.get("live_effective", False))

    @property
    def preflight_ready(self) -> bool:
        return bool(self.preflight.get("ready", False))

    @property
    def runner_available(self) -> bool:
        return bool(self.runner.get("available", True)) and bool(self.runner.get("state"))

    @property
    def runner_running(self) -> bool:
        return bool(self.runner.get("running", False))

    def nav_badge(self, page: str) -> str:
        if page == "positions" and self.paper.open_positions:
            return str(len(self.paper.open_positions))
        return ""


def build_context(
    *,
    platform: Any,
    wizard: SetupWizard,
    page: str,
    title: str,
    action_nonce: str,
    notice: str = "",
    extra: Mapping[str, Any] | None = None,
    paper: PaperSnapshot | None = None,
    execution_mode: Mapping[str, Any] | None = None,
    preflight: Mapping[str, Any] | None = None,
    runner: Mapping[str, Any] | None = None,
) -> PageContext:
    """Assemble a page.

    ``paper`` may carry a snapshot read earlier on the owning thread; the paper
    platform's database handles are not shared across threads, and this UI only
    ever reads them.
    """

    if paper is None:
        paper = (
            read_paper_snapshot(platform)
            if platform is not None
            else PaperSnapshot.unavailable("no_platform")
        )
    return PageContext(
        page=page,
        title=title,
        wizard=wizard,
        paper=paper,
        terminal=read_terminal_view(wizard.session),
        action_nonce=action_nonce,
        platform_version=str(getattr(platform, "version", "") or ""),
        generated_at=_utc_now(),
        notice=notice,
        extra=dict(extra or {}),
        execution_mode=dict(execution_mode or default_mode_view()),
        preflight=dict(preflight or {}),
        runner=dict(runner or {}),
    )

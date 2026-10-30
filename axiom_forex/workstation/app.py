"""The workstation web app: stdlib ``http.server``, loopback only.

The surface is deliberately tiny:

``GET``
    Seven pages. Rendering never changes state and never runs a preflight that
    could change anything.
``POST /actions/{connect,disconnect,acknowledge-sign-in,confirm,unconfirm,symbols}``
    Session and setup actions. Each requires the per-process nonce embedded in
    the page that offered the button, so an unrelated page cannot drive a
    connection.
``POST /actions/{imports-refresh,ticks-convert,dataset-evaluate}``
    The data workflow: re-scan the import folder, convert one broker tick export
    into the canonical CSV, or evaluate the candidate grid on one canonical file.
    They read and write *local files and the research database only*: they never
    connect anywhere, never place an order, and never change the execution mode.
``POST /actions/{provider-setup-save}``
    Write ``provider_setup.local.json`` from values the operator typed plus the
    account facts the connected terminal reports. Validated by the same loader
    the runner uses, so a saved file is one the runner accepts. It never reads or
    writes ``mt5_settings.local.json`` and refuses any credential field.
``POST /actions/{mode-paper,mode-live}``
    The execution-mode switch, and the whole of it. ``mode-paper`` is immediate
    and unconditional. ``mode-live`` recomputes the preflight, refuses while any
    blocker stands, and requires the typed confirmation phrase -- and it sends
    no order, connects nothing, and arms nothing. Changing modes is not a trade.

Anything else is a 405/404. There is no JSON API, no order endpoint, no arming
endpoint, no account switch, and no background timer: a request renders the
current state and then the process waits. Nothing polls, nothing reconnects, and
nothing changes the mode or the account except an explicit click on the page
that offers it.

Credentials never enter this process: the server has no field for a password,
refuses form posts that carry one, and the terminal session it observes was
authenticated by the user inside MetaTrader 5.
"""

from __future__ import annotations

import secrets
import sys
import threading
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Mapping
from urllib.parse import parse_qs, urlparse

from app.audit.log import sanitize_audit_value

from .context import PaperSnapshot, PageContext, build_context, read_paper_snapshot
from .mode import (
    REASON_SESSION_ENDED,
    ExecutionModeController,
    build_controller,
    default_mode_view,
)
from .preflight import LivePreflight, StrategyBinding, facts_from
from .setup import SetupWizard
from .views import render_message, render_page

#: Paths that render a page. Everything else is refused.
GET_ROUTES: dict[str, tuple[str, str]] = {
    "/": ("overview", "Overview"),
    "/overview": ("overview", "Overview"),
    "/setup": ("setup", "Setup"),
    "/account": ("account", "Account & symbols"),
    "/positions": ("positions", "Positions"),
    "/data": ("data", "Data & evaluation"),
    "/provider-setup": ("provider-setup", "Provider setup"),
    "/live": ("live", "Live execution"),
    "/activity": ("activity", "Activity"),
    "/health": ("health", "System health"),
    "/mode": ("mode", "Trading mode"),
}

#: The complete set of state-changing actions, and the whole of it.
#:
#: Grouped by what they touch:
#:   * ``connect`` / ``disconnect`` -- the session, on an explicit click only;
#:   * ``acknowledge-sign-in``     -- a local note that the operator signed in
#:     inside MetaTrader 5; it contacts nothing;
#:   * ``confirm`` / ``unconfirm`` -- the local record that a real account and
#:     symbol set were reviewed;
#:   * ``symbols``                -- which symbols to observe;
#:   * ``mode-paper`` / ``mode-live`` -- the execution mode: simulate fills
#:     locally, or route an approved order to the live execution path;
#:   * ``runner-start`` / ``runner-stop`` -- the paper strategy runner's own
#:     loop.  They start and stop *simulated* cycles; they cannot place an
#:     order, cannot arm anything, and refuse to run outside paper mode.
#:   * ``imports-refresh`` / ``ticks-convert`` / ``dataset-evaluate`` -- the
#:     data workflow.  They touch local files and the research database, and
#:     nothing else: no connection, no order, no arming, no mode change.
#:   * ``live-arm`` / ``live-disarm`` / ``live-start`` / ``live-stop`` -- the
#:     live execution workflow.  Arming writes the operator's plan and arms the
#:     broker adapter (it sends nothing); starting begins the strategy loop;
#:     stopping ends it and disarms.  None of them connects, signs in, changes
#:     the mode, or picks a strategy, symbol, size, or limit.
#:
#: Anything that could trade, arm, switch accounts, or change broker settings is
#: absent by construction, not by a permission check: there is no handler for it,
#: and ``test_workstation_ui`` pins this set exactly.  Note what the mode actions
#: do *not* do: they never connect, never arm, never submit, and never touch the
#: account.  Switching modes cannot place an order.
POST_ACTIONS: frozenset[str] = frozenset(
    {
        "connect",
        "disconnect",
        "acknowledge-sign-in",
        "confirm",
        "unconfirm",
        "symbols",
        "mode-paper",
        "mode-live",
        "runner-start",
        "runner-stop",
        "imports-refresh",
        "ticks-convert",
        "dataset-evaluate",
        # The guided provider setup. It writes one local, Git-ignored document
        # (provider_setup.local.json) and never reads or touches the MT5 settings.
        "provider-setup-save",
        # The live workflow: plan, review, arm, start, stop.
        "live-arm",
        "live-disarm",
        "live-start",
        "live-stop",
    }
)

#: Field names that would mean a credential is being handed to us. Their mere
#: presence rejects the request; nothing is read, echoed, or logged.
CREDENTIAL_FIELDS: frozenset[str] = frozenset(
    {
        "password",
        "passwd",
        "pwd",
        "pass",
        "login",
        "account_login",
        "investor_password",
        "token",
        "access_token",
        "market_access_token",
        "api_key",
        "secret",
    }
)

MAX_BODY_BYTES = 64 * 1024

#: Operator-readable reasons a confirmation can be refused.
CONFIRM_REFUSALS = {
    "confirm_requires_live_terminal": (
        "Not confirmed: only a real terminal account can be confirmed. "
        "A simulator preview never can."
    ),
    "confirm_requires_observed_account": "Not confirmed: no account has been observed yet.",
    "confirm_requires_selected_symbols": "Not confirmed: choose at least one symbol first.",
}

CSP = (
    "default-src 'none'; style-src 'unsafe-inline'; img-src 'none'; "
    "script-src 'none'; connect-src 'none'; form-action 'self'; "
    "base-uri 'none'; frame-ancestors 'self'"
)


def csp_header(allow_framing: bool = False) -> str:
    """The policy for a response.

    ``allow_framing`` exists only for a simulated session shown inside a preview
    pane; a session attached to a real terminal is never framable.
    """

    if not allow_framing:
        return CSP
    return CSP.replace("frame-ancestors 'self'", "frame-ancestors *")


@dataclass
class WorkstationApp:
    """Wires a wizard and a paper platform to the pages and actions."""

    wizard: SetupWizard
    platform: Any = None
    version: str = "0.1.0"
    nonce: str = field(default_factory=lambda: secrets.token_urlsafe(18))
    on_event: Callable[[str], None] | None = None
    #: One request at a time: the session object is not thread-safe, and a
    #: serialized UI also means two tabs cannot race on a connect.
    lock: threading.Lock = field(default_factory=threading.Lock)
    #: Only a simulated session may be embedded in a preview pane.
    allow_framing: bool = False
    #: Paper snapshot read once on this thread.  The paper platform's SQLite
    #: handles are thread-affine, and this interface never writes to it, so one
    #: read at construction is both correct and enough.
    paper: PaperSnapshot | None = None
    #: The execution mode.  Created with no local documents when the caller did
    #: not supply one, so a bare app still shows an honest PAPER state.
    mode: ExecutionModeController | None = None
    #: The live preflight.  Recomputed on every page load from facts that are
    #: already on this thread; it contacts nothing.
    preflight: LivePreflight | None = None
    #: The settings document's own facts (terminal path, server, mode).  When the
    #: caller passes none, the session's source is asked -- the same object the
    #: Connect button would use.
    settings: Any = None
    settings_reason: str = ""
    #: Where execution_mode.local.json lives.  ``None`` keeps the choice in
    #: memory only; the launcher always passes its settings directory.
    mode_path: Any = None
    #: A strategy this deployment attaches to the live path, if any.  AXIOM never
    #: invents one, so the default is ``None``.
    strategy: StrategyBinding | None = None
    #: An audit sink for mode changes (a durable log, when one is available).
    audit_sink: Any = None
    #: The last live-arm result, for the page's notice block.
    live_result: dict[str, Any] = field(default_factory=dict)

    #: The live execution workflow (plan, review, arming, session), if this
    #: deployment was started with the trading adapter.  ``None`` means this
    #: workstation can never send a real order.
    live: Any = None

    #: The paper strategy runner's host, if this deployment has one.  It owns its
    #: own thread and its own paper stack; the UI only sends it start/stop and
    #: reads its state.  ``None`` means no runner is configured -- which is the
    #: default, and leaves every other page exactly as it was.
    runner: Any = None
    #: Where the operator drops broker exports and canonical CSVs.  The data page
    #: lists *names* in this folder and never accepts a path from a form.
    import_dir: Any = None
    #: The research database the runner reads its evidence from.  ``None`` means
    #: this process has no durable research store, so evaluation is unavailable
    #: rather than silently writing somewhere else.
    research_db_path: str = ""
    #: Builds a fresh evaluation platform (same terms as the paper lane) on
    #: demand.  ``None`` means this process cannot evaluate -- the page says so.
    evaluation_platform_builder: Any = None
    #: The last data action's own result, shown on the next render.  Kept in
    #: memory only; a restart forgets it, and the files are the record.
    data_result: dict[str, Any] = field(default_factory=dict)
    #: The provider setup document the runner was pointed at, if any.  Empty
    #: means "not configured", which is a blocker the page states plainly.
    provider_setup_path: str = ""
    #: Where the runner's prices come from: "terminal" or "replay".  A replay
    #: build is a labelled offline walk, not a live lane, and says so.
    runner_source: str = ""
    #: The project root: where ``provider_setup.local.json`` is written.  It is
    #: deliberately the same folder the launcher and the updater use.
    project_root: str = ""

    def __post_init__(self) -> None:
        if self.paper is None and self.platform is not None:
            self.paper = read_paper_snapshot(self.platform)
        if self.settings is None:
            self.settings = getattr(getattr(self.wizard.session, "source", None), "settings", None)
        if self.mode is None:
            self.mode = build_controller(settings_path=self.mode_path, sink=self.audit_sink)
        if self.preflight is None:
            self.preflight = LivePreflight(provider=self.preflight_facts)

    # -- rendering ---------------------------------------------------------
    def page(self, path: str) -> tuple[int, str]:
        route = GET_ROUTES.get(path)
        if route is None:
            ctx = self._context("overview", "Not found")
            return HTTPStatus.NOT_FOUND, render_page(ctx)
        page, title = route
        with self.lock:
            ctx = self._context(page, title)
            html = render_page(ctx)
        return HTTPStatus.OK, html

    def preflight_facts(self):
        """The facts the live preflight is allowed to see, gathered read-only."""

        return facts_from(
            settings=self.settings,
            settings_reason=self.settings_reason,
            environment=getattr(self.wizard, "environment", None),
            session=self.wizard.session.view(),
            confirmed=bool(self.wizard.choices.confirmed),
            selected_symbols=self.wizard.choices.selected_symbols,
            strategy=self.strategy,
        )

    def preflight_view(self) -> dict[str, Any]:
        if self.preflight is None:
            return {"ready": False, "blockers": [], "order_requirements": []}
        return self.preflight.view()

    def mode_view(self, blockers: Any = None) -> dict[str, Any]:
        if self.mode is None:
            return default_mode_view()
        if blockers is None:
            blockers = self.preflight_view().get("blockers", ())
        return self.mode.view(blockers=blockers)

    def runner_view(self, data: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Read the runner's state.  Never raises, never touches its objects."""

        if self.runner is None:
            return {}
        try:
            view = self.runner.view()
        except Exception as exc:
            return {"available": False, "error": type(exc).__name__}
        if not isinstance(view, dict):
            return {"available": False, "error": "runner_view_not_a_mapping"}
        view.setdefault("available", True)
        # What the runner still needs before it can start, in the operator's
        # words.  Merged here so every page that shows the runner card shows the
        # same list, and so a blocked build never degrades into an exception.
        try:
            view["setup_blockers"] = self.runner_setup_blockers(view, data)
        except Exception as exc:  # a read model never raises
            view["setup_blockers"] = [
                {
                    "reason": "runner_status_unreadable",
                    "message": "The runner's setup state could not be read "
                    f"({type(exc).__name__}).",
                    "hint": "Reload the page. Nothing was started or changed.",
                }
            ]
        return sanitize_audit_value(view)

    def runner_setup_blockers(
        self,
        view: Mapping[str, Any],
        data: Mapping[str, Any] | None = None,
    ) -> list[dict[str, str]]:
        """Everything standing between this runner and its first simulated fill.

        Ordered by what the operator must do first.  Each entry is a stable code
        plus human wording; none of them is exception text, and none of them
        changes any state.
        """

        blockers: list[dict[str, str]] = []
        session = getattr(self.wizard, "session", None)
        connected = bool(getattr(session, "connected", False))
        if not connected:
            blockers.append(
                {
                    "reason": "terminal_not_connected",
                    "message": (
                        "No terminal session is open, so there are no symbols, no "
                        "specs, and no ticks to run the strategy on."
                    ),
                    "hint": "Press Connect on the Setup page, then start the runner.",
                }
            )
        if self.runner_source == "terminal":
            if not self.provider_setup_path:
                blockers.append(
                    {
                        "reason": "provider_setup_missing",
                        "message": (
                            "The runner was started without a provider setup document, so "
                            "it has no broker symbol, costs, swap, or risk limits to use."
                        ),
                        "hint": (
                            "Open Provider setup to create provider_setup.local.json, then "
                            "start with --paper-runner --paper-runner-provider-setup <file>."
                        ),
                    }
                )
            else:
                from pathlib import Path as _Path

                configured = _Path(self.provider_setup_path)
                if not configured.is_absolute():
                    configured = self.project_root_path() / configured
                if not configured.exists():
                    blockers.append(
                        {
                            "reason": "provider_setup_file_missing",
                            "message": (
                                "The runner is pointed at "
                                f"{_Path(self.provider_setup_path).name}, and that file "
                                "does not exist yet."
                            ),
                            "hint": (
                                "Open Provider setup in this browser: it writes the file "
                                "from your broker terms plus the account facts your "
                                "terminal reports."
                            ),
                        }
                    )
        if data is None:
            try:
                data = self.data_view()
            except Exception:
                data = {}
        dataset_files = list(data.get("dataset_files") or ())
        if not dataset_files:
            blockers.append(
                {
                    "reason": "no_imported_history",
                    "message": (
                        "No evaluable dataset is in the import folder, so no candidate "
                        "could have been evaluated on your own broker history."
                    ),
                    "hint": (
                        "Export ticks from MetaTrader 5, then use Data & evaluation to "
                        "convert and evaluate them."
                    ),
                }
            )
        evidence = dict(data.get("evidence") or {})
        rows = list(evidence.get("rows") or ())
        selected = [row for row in rows if row.get("selected")]
        if rows and not selected:
            blockers.append(
                {
                    "reason": "strategy_not_evaluated",
                    "message": (
                        "No candidate has passed the predeclared criteria on your own "
                        "data, so there is no approved strategy to run."
                    ),
                    "hint": (
                        "Read the blocking criterion on Data & evaluation. The runner "
                        "will not be retuned onto a candidate that failed."
                    ),
                }
            )
        elif not rows:
            blockers.append(
                {
                    "reason": "strategy_not_evaluated",
                    "message": (
                        "No evaluation evidence is recorded yet, so no candidate has "
                        "been approved for paper trading."
                    ),
                    "hint": "Evaluate a candidate on Data & evaluation first.",
                }
            )
        # Whatever the host itself refused, first: it is the most specific.
        for item in view.get("blockers") or ():
            if isinstance(item, Mapping):
                blockers.insert(
                    0,
                    {
                        "reason": str(item.get("reason", "")),
                        "message": str(item.get("message", "")),
                        "hint": str(item.get("hint", "")),
                    },
                )
        return blockers

    def live_view(self) -> dict[str, Any]:
        """The live page's whole model.  Reads only, never sends."""

        if self.live is None:
            return {
                "configured": False,
                "armed": False,
                "next_signal_sends_real_order": False,
                "blockers": [],
                "candidates": [],
                "symbols": [],
                "plan": {"present": False},
                "account": {},
                "positions": [],
                "confirmation_phrase": "",
                "review": {},
            }
        try:
            return self.live.view()
        except Exception as exc:
            return {
                "configured": True,
                "armed": False,
                "next_signal_sends_real_order": False,
                "blockers": [
                    {
                        "reason": "live_view_unreadable",
                        "message": f"The live view could not be read ({type(exc).__name__}).",
                        "hint": "Nothing is armed if this panel cannot be read.",
                    }
                ],
                "candidates": [],
                "symbols": [],
                "plan": {"present": False},
                "account": {},
                "positions": [],
            }

    def live_summary(self) -> dict[str, Any]:
        """The one fact every page may show: would the next signal send an order?"""

        if self.live is None:
            return {
                "present": False,
                "armed": False,
                "next_signal_sends_real_order": False,
                "sentence": (
                    "No live trading adapter is wired into this workstation, so no "
                    "signal can send an order."
                ),
            }
        try:
            summary = dict(self.live.summary())
        except Exception:
            return {
                "present": True,
                "armed": False,
                "next_signal_sends_real_order": False,
                "sentence": "The live state could not be read; nothing is assumed armed.",
            }
        return summary

    def _context(self, page: str, title: str, notice: str = "") -> PageContext:
        # One preflight per page render: it is read-only, and running it twice
        # would let two panels disagree about the same facts.
        preflight = self.preflight_view()
        # One read of the import folder per render: the runner card and the data
        # page must never disagree about which files exist.
        data = self.data_view()
        return build_context(
            platform=self.platform,
            paper=self.paper,
            wizard=self.wizard,
            page=page,
            title=title,
            action_nonce=self.nonce,
            notice=notice,
            execution_mode=self.mode_view(blockers=preflight.get("blockers", ())),
            preflight=preflight,
            runner=self.runner_view(data=data),
            extra={
                "data": data,
                "provider_setup": (
                    self.provider_setup_view() if page == "provider-setup" else {}
                ),
                "live": self.live_view() if page == "live" else {},
                "live_summary": self.live_summary(),
            },
        )

    # -- actions -----------------------------------------------------------
    def act(self, action: str, form: Mapping[str, list[str]]) -> tuple[int, str, str]:
        """Run one action. Returns ``(status, redirect_path, message)``."""

        if action not in POST_ACTIONS:
            return HTTPStatus.NOT_FOUND, "/", "unknown action"
        if str((form.get("nonce") or [""])[0]) != self.nonce:
            return HTTPStatus.FORBIDDEN, "/", "this form is stale; reload the page and try again"
        if any(name in form for name in CREDENTIAL_FIELDS):
            return (
                HTTPStatus.BAD_REQUEST,
                "/setup",
                "this form must not carry credentials; AXIOM never accepts an MT5 password",
            )

        with self.lock:
            try:
                return self._dispatch(action, form)
            except Exception as exc:  # never leak a stack trace into the page
                self._log(f"action {action} failed: {type(exc).__name__}")
                return HTTPStatus.INTERNAL_SERVER_ERROR, "/", f"{action} failed ({type(exc).__name__})"

    def _dispatch(self, action: str, form: Mapping[str, list[str]]) -> tuple[int, str, str]:
        wizard = self.wizard
        if action == "connect":
            session = wizard.session
            simulated = wizard.simulated
            self._log(
                "simulator preview requested" if simulated else "connect requested (data only)"
            )
            session.connect()
            if session.connected:
                self._log(f"session active: source={session.view().get('source_kind')}")
                live_note = ""
                if self.live is not None and not simulated:
                    # The trading adapter adopts this terminal; it opens no second
                    # session and sends nothing. A refusal is reported, not hidden.
                    try:
                        outcome = self.live.connect()
                    except Exception as exc:
                        outcome = {"connected": False, "reason": type(exc).__name__}
                    if not outcome.get("connected"):
                        self._log(
                            f"live adapter not attached: {outcome.get('reason') or 'unknown'}"
                        )
                        live_note = (
                            " The live trading adapter did not attach ("
                            f"{outcome.get('reason') or 'unknown'}); the Live execution "
                            "page shows the blocker and no order can be sent."
                        )
                if simulated:
                    return (
                        HTTPStatus.SEE_OTHER,
                        "/setup",
                        "Simulator preview started. No terminal is attached and this "
                        "preview can never reach observation mode.",
                    )
                return (
                    HTTPStatus.SEE_OTHER,
                    "/",
                    "Connected to the terminal. Choose how orders would be executed on "
                    "the Trading mode screen." + live_note,
                )
            error = (session.view().get("error") or {})
            reason = str(error.get("reason", "unknown"))
            self._log(f"connect failed: {reason}")
            return HTTPStatus.SEE_OTHER, "/setup", f"Could not connect: {reason}"
        if action == "disconnect":
            self._log("disconnect requested")
            simulate = wizard.simulated
            # Closing the adapter first disarms it and closes the shared terminal
            # session; the session's own disconnect then finds nothing to shut down.
            if self.live is not None:
                try:
                    self.live.disconnect(reason="disconnect_action")
                except Exception as exc:
                    self._log(f"live adapter disconnect failed: {type(exc).__name__}")
            self._stop_live(detail="disconnect action")
            wizard.session.disconnect()
            # Live execution depends on the session that was just closed, so the
            # mode returns to paper -- because the operator clicked Disconnect,
            # not because anything ran in the background.
            ended = self.mode.end_live(reason=REASON_SESSION_ENDED, detail="disconnect action") if self.mode else None
            mode_note = (
                " Live mode ended with the session; paper mode is active."
                if ended is not None and ended.changed
                else ""
            )
            if simulate:
                return HTTPStatus.SEE_OTHER, "/setup", "Simulator preview stopped." + mode_note
            return (
                HTTPStatus.SEE_OTHER,
                "/",
                "Disconnected. Nothing reconnects automatically." + mode_note,
            )
        if action == "mode-paper":
            if self.mode is None:
                return HTTPStatus.INTERNAL_SERVER_ERROR, "/mode", "mode controller unavailable"
            # Moving to paper must end any live session: simulated fills and real
            # orders must never be one click apart.
            self._stop_live(detail="mode switched to paper")
            change = self.mode.set_paper(detail="workstation mode selector")
            self._log(f"execution mode: {change.to_mode} ({change.reason})")
            return HTTPStatus.SEE_OTHER, "/", change.message
        if action == "mode-live":
            if self.mode is None:
                return HTTPStatus.INTERNAL_SERVER_ERROR, "/mode", "mode controller unavailable"
            preflight = self.preflight_view()
            confirmation = str((form.get("confirmation") or [""])[0])
            change = self.mode.enable_live(
                blockers=preflight.get("blockers", ()),
                confirmation=confirmation,
                detail="workstation mode selector",
            )
            if change.changed:
                self._log(f"execution mode: {change.to_mode} ({change.reason})")
                return HTTPStatus.SEE_OTHER, "/", change.message
            self._log(f"live mode refused: {change.reason}")
            return HTTPStatus.SEE_OTHER, "/mode", change.message
        if action in {"runner-start", "runner-stop"}:
            runner = self.runner
            if runner is None:
                return (
                    HTTPStatus.BAD_REQUEST,
                    "/positions",
                    "No paper runner is configured for this workstation.",
                )
            if action == "runner-start":
                try:
                    view = runner.start_runner()
                except Exception as exc:
                    self._log(f"runner start failed: {type(exc).__name__}")
                    return (
                        HTTPStatus.SERVICE_UNAVAILABLE,
                        "/positions",
                        f"The paper runner could not be started ({type(exc).__name__}). "
                        "Nothing was traded.",
                    )
                if view.get("running"):
                    self._log("paper runner started")
                    return (
                        HTTPStatus.SEE_OTHER,
                        "/positions",
                        "Paper runner started. Every fill it produces is simulated locally.",
                    )
                blockers = view.get("blockers") or []
                first = blockers[0] if blockers else {}
                reason = str(first.get("reason") or view.get("reason") or "refused")
                message = str(first.get("message") or "").strip()
                self._log(f"paper runner refused to start: {reason}")
                return (
                    HTTPStatus.SEE_OTHER,
                    "/positions",
                    (
                        f"The paper runner did not start: {reason}."
                        + (f" {message}" if message else "")
                        + " Nothing was traded and the mode was not changed."
                    ),
                )
            try:
                view = runner.stop_runner()
            except Exception as exc:
                self._log(f"runner stop failed: {type(exc).__name__}")
                return (
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    "/positions",
                    f"The paper runner could not be stopped cleanly ({type(exc).__name__}). "
                    "No order was sent by this interface in any case.",
                )
            self._log("paper runner stopped")
            return (
                HTTPStatus.SEE_OTHER,
                "/positions",
                "Paper runner stopped. Open paper positions remain in the local ledger.",
            )
        if action == "provider-setup-save":
            return self._provider_setup_save(form)
        if action in {"live-arm", "live-disarm", "live-start", "live-stop"}:
            return self._live_action(action, form)
        if action in {"imports-refresh", "ticks-convert", "dataset-evaluate"}:
            return self._data_action(action, form)
        if action == "acknowledge-sign-in":
            wizard.acknowledge_sign_in()
            return HTTPStatus.SEE_OTHER, "/setup", "Noted: you signed in inside MetaTrader 5."
        if action == "confirm":
            try:
                wizard.confirm()
            except Exception as exc:
                reason = str(exc) or type(exc).__name__
                self._log(f"confirm refused: {type(exc).__name__}")
                return HTTPStatus.SEE_OTHER, "/account", CONFIRM_REFUSALS.get(
                    reason, f"Not confirmed: {reason}"
                )
            self._log("configuration confirmed")
            return HTTPStatus.SEE_OTHER, "/", "Account and symbols confirmed."
        if action == "unconfirm":
            wizard.reset_confirmation()
            return HTTPStatus.SEE_OTHER, "/account", "Confirmation withdrawn."
        if action == "symbols":
            chosen = [value for value in form.get("symbol", []) if value]
            wizard.choose_symbols(chosen)
            self._log(f"symbols selected: {len(chosen)}")
            return HTTPStatus.SEE_OTHER, "/account", f"{len(chosen)} symbol(s) selected."
        return HTTPStatus.NOT_FOUND, "/", "unknown action"

    # -- live execution -----------------------------------------------------
    def _live_action(self, action: str, form: Mapping[str, list[str]]) -> tuple[int, str, str]:
        """Arm, disarm, start, or stop the live session.  Sends nothing by itself."""

        live = self.live
        if live is None:
            return (
                HTTPStatus.BAD_REQUEST,
                "/live",
                "This workstation has no live trading adapter, so it cannot send a real "
                "order. Start it with the terminal settings to enable live execution.",
            )

        def field(name: str) -> str:
            raw = form.get(name)
            if isinstance(raw, (list, tuple)):
                raw = raw[0] if raw else ""
            return str(raw if raw is not None else "").strip()

        if action == "live-arm":
            values = {
                "candidate_id": field("candidate_id"),
                "pair": field("pair"),
                "quantity": field("quantity"),
                "max_open_positions": field("max_open_positions"),
                "max_daily_loss_account": field("max_daily_loss_account"),
            }
            result = live.arm(
                values,
                confirmation=field("confirmation"),
                reviewed=field("reviewed") == "yes",
            )
            self.live_result = result.audit_view()
            if result.armed:
                self._log(
                    "live plan armed: "
                    f"{values['candidate_id'] or 'unknown'} "
                    f"{values['pair'] or 'unknown'} "
                    f"qty={values['quantity'] or 'unset'}"
                )
                return (
                    HTTPStatus.SEE_OTHER,
                    "/live",
                    "Armed. The next strategy signal would send a REAL order. "
                    "Press Start to begin the session.",
                )
            self._log(f"live arm refused: {result.reason}")
            detail = "; ".join(
                f"{item.message}" for item in result.blockers[:3] if item.message
            )
            return (
                HTTPStatus.SEE_OTHER,
                "/live",
                f"Nothing was armed: {result.message}"
                + (f" ({detail})" if detail and detail != result.message else ""),
            )

        if action == "live-disarm":
            self._stop_live(detail="operator disarm", close=True)
            self._log("live plan disarmed")
            return (
                HTTPStatus.SEE_OTHER,
                "/live",
                "Disarmed. No signal can send an order until you arm again.",
            )

        if action == "live-start":
            outcome = live.start_session()
            if outcome.get("started"):
                self._log("live session started")
                return (
                    HTTPStatus.SEE_OTHER,
                    "/live",
                    "Live session running. Every approved signal now goes to MetaTrader 5.",
                )
            self._log(f"live session refused: {outcome.get('reason')}")
            return (
                HTTPStatus.SEE_OTHER,
                "/live",
                f"The live session did not start: {outcome.get('reason')}. "
                f"{outcome.get('message', '')} Nothing was sent.",
            )

        # live-stop: the prominent control.  Always allowed, never refused.
        live.stop_session(reason="operator_stop", close=True)
        self._log("live session stopped and disarmed")
        return (
            HTTPStatus.SEE_OTHER,
            "/live",
            "Stopped and disarmed. No further order can be sent from this session.",
        )

    def _stop_live(self, *, detail: str, close: bool = True) -> None:
        """Stop the live session and disarm, swallowing nothing a page needs."""

        live = self.live
        if live is None:
            return
        try:
            live.stop_session(reason=detail.replace(" ", "_"), close=close)
        except Exception as exc:
            self._log(f"live stop failed: {type(exc).__name__}")
            try:
                live.disarm(reason="stop_failed")
            except Exception:
                pass

    # -- data workflow -----------------------------------------------------
    def _data_action(self, action: str, form: Mapping[str, list[str]]) -> tuple[int, str, str]:
        """Scan, convert, or evaluate -- local files only, never an order."""

        from . import dataflow

        if action == "imports-refresh":
            self.data_result = {"action": "scan", "ok": True}
            self._log("import folder re-scanned")
            return HTTPStatus.SEE_OTHER, "/data", "Import folder re-scanned."

        def field(name: str) -> str:
            raw = form.get(name)
            if isinstance(raw, (list, tuple)):
                raw = raw[0] if raw else ""
            return str(raw if raw is not None else "").strip()

        if self.import_dir is None:
            return (
                HTTPStatus.BAD_REQUEST,
                "/data",
                "This workstation has no import folder configured. Start it with "
                "--imports-dir <folder> to enable the data workflow.",
            )

        try:
            if action == "ticks-convert":
                report = dataflow.convert_tick_export(
                    self.import_dir,
                    name=field("file"),
                    pair=field("pair"),
                    server_offset_minutes=field("server_offset_minutes") or None,
                    server_timezone=field("server_timezone"),
                    interval_seconds=int(field("interval_seconds") or 60),
                )
                self.data_result = {"action": "convert", "ok": True, **report}
                self._log(
                    f"tick export converted: {report['output_file']} "
                    f"({report['bars_written']} bars from {report['tick_count']} ticks)"
                )
                gaps = report.get("coverage", {}).get("empty_windows", 0)
                return (
                    HTTPStatus.SEE_OTHER,
                    "/data",
                    f"Converted {report['source_file']} into {report['output_file']}: "
                    f"{report['bars_written']} bids and asks from "
                    f"{report['tick_count']} ticks, {gaps} empty window(s) reported as "
                    "gaps. Nothing was filled in. Evaluate the canonical file next.",
                )

            # dataset-evaluate
            if self.evaluation_platform_builder is None:
                raise dataflow.DataflowRefused(
                    "evaluation_not_configured",
                    "this workstation was started without an evaluation platform; "
                    "no candidate can be scored here",
                )
            data_class = field("data_class").upper()
            if not data_class:
                raise dataflow.DataflowRefused(
                    "data_class_required",
                    "declare what this file is; AXIOM will not guess a data class",
                )
            if data_class not in self._data_class_choices():
                raise dataflow.DataflowRefused(
                    "data_class_unknown", data_class
                )
            if field("declare") != "yes":
                raise dataflow.DataflowRefused(
                    "declaration_required",
                    "tick this only after confirming the file is data you exported "
                    "yourself; AXIOM does not vouch for it",
                )
            pair = field("pair").upper()
            if not pair:
                raise dataflow.DataflowRefused(
                    "pair_required", "declare which pair this file is for"
                )
            platform, refusal = self.evaluation_platform_builder(pair)
            if platform is None:
                raise dataflow.DataflowRefused(
                    getattr(refusal, "reason", "platform_unavailable"),
                    getattr(refusal, "detail", ""),
                )
            bars, meta = dataflow.load_bars(
                self.import_dir, name=field("file"), spec=platform.spec
            )
            outcome = dataflow.evaluate_dataset(
                platform=platform,
                bars=bars,
                dataset_id=dataflow.dataset_id_for(meta),
                data_class=data_class,
            )
            self.data_result = {
                "action": "evaluate",
                "ok": True,
                "meta": meta,
                **outcome,
            }
            self._log(
                f"evaluation recorded: {meta['pair']} {meta['bars']} bars, "
                f"{len(outcome['selected'])} candidate(s) at SELECTED_FOR_PAPER"
            )
            if outcome["selected"]:
                message = (
                    f"Evaluated {meta['bars']} bars of {meta['pair']} and recorded the "
                    f"evidence. {len(outcome['selected'])} candidate(s) met the "
                    "predeclared criteria on this sample. That is historical evidence, "
                    "not a forecast; the paper runner must still be started by hand."
                )
            else:
                message = (
                    f"Evaluated {meta['bars']} bars of {meta['pair']} and recorded the "
                    "evidence. No candidate met the predeclared criteria, so the paper "
                    "runner stays blocked. The reasons are on the Data & evaluation page."
                )
            return HTTPStatus.SEE_OTHER, "/data", message
        except dataflow.DataflowRefused as exc:
            self.data_result = {"action": action, "ok": False, **exc.audit_view()}
            self._log(f"data action refused: {exc.reason}")
            return (
                HTTPStatus.SEE_OTHER,
                "/data",
                f"Nothing was written or evaluated: {exc.reason}"
                + (f" ({exc.detail})" if exc.detail else ""),
            )
        except (OSError, ValueError) as exc:
            self.data_result = {
                "action": action,
                "ok": False,
                "reason": type(exc).__name__,
                "detail": str(exc),
            }
            self._log(f"data action failed: {type(exc).__name__}")
            return (
                HTTPStatus.SEE_OTHER,
                "/data",
                f"Nothing was written or evaluated: {type(exc).__name__}: {exc}",
            )

    def _data_class_choices(self) -> tuple[str, ...]:
        """The data classes the operator may *declare*, never one AXIOM picks."""

        return (
            "USER_SUPPLIED_HISTORICAL",
            "USER_SUPPLIED_DEMO",
            "USER_SUPPLIED_PRODUCTION",
        )

    def data_view(self) -> dict[str, Any]:
        """Everything the data page shows: files, the last result, and evidence."""

        from . import dataflow

        files: list[dict[str, Any]] = []
        scan_error = ""
        if self.import_dir is not None:
            try:
                files = [entry.audit_view() for entry in dataflow.scan(self.import_dir)]
            except OSError as exc:
                scan_error = type(exc).__name__
        return {
            "configured": self.import_dir is not None,
            "import_dir": str(self.import_dir) if self.import_dir is not None else "",
            "scan_error": scan_error,
            "files": files,
            "tick_files": [item for item in files if item["can_convert"]],
            "dataset_files": [item for item in files if item["can_evaluate"]],
            "data_classes": list(self._data_class_choices()),
            "promoting_classes": list(dataflow.PROMOTING_DATA_CLASSES),
            "last": dict(self.data_result),
            "evaluation_available": self.evaluation_platform_builder is not None,
            "evidence": self.evidence_view(),
        }

    def evidence_view(self) -> dict[str, Any]:
        """The recorded evidence per candidate, read from the research database.

        Read-only and short: the last recorded evaluation for each candidate the
        runner knows about, plus what the runner currently requires.
        """

        if not self.research_db_path:
            return {"available": False, "reason": "no_research_database", "rows": []}
        from pathlib import Path as _Path

        if not _Path(self.research_db_path).exists():
            # Opening the store would create it; a page render must not write.
            return {"available": False, "reason": "no_evidence_recorded_yet", "rows": []}
        try:
            from axiom_forex.database.repository import ForexRepository
            from axiom_forex.paper_runner import evaluation
            from axiom_forex.paper_runner.strategy import STATUS_SELECTED
            from axiom_forex.paper_runner.strategy import candidate_family, status_of

            repository = ForexRepository(self.research_db_path)
            rows = []
            for candidate in candidate_family():
                latest = evaluation.latest_evidence(
                    repository=repository, candidate_id=candidate.candidate_id
                )
                if latest is None:
                    rows.append(
                        {
                            "candidate_id": candidate.candidate_id,
                            "status": "NO_EVIDENCE",
                            "detail": "never evaluated on a dataset you imported",
                        }
                    )
                    continue
                status = status_of(candidate.candidate_id, latest)
                rows.append(
                    {
                        "candidate_id": candidate.candidate_id,
                        "status": status,
                        "selected": status == STATUS_SELECTED,
                        "passed": bool(latest.get("passed")),
                        "failed_criteria": list(latest.get("failed_criteria", [])),
                        "dataset_id": latest.get("dataset_id", ""),
                        "data_class": latest.get("data_class", ""),
                        "evaluated_at": latest.get("evaluated_at", ""),
                        "summary": evaluation.evidence_view(latest),
                    }
                )
            return {"available": True, "reason": "", "rows": rows}
        except Exception as exc:  # a read failure shows as an empty table
            return {
                "available": False,
                "reason": type(exc).__name__,
                "rows": [],
            }

    # -- guided provider setup ---------------------------------------------
    def project_root_path(self) -> Any:
        from pathlib import Path as _Path

        return _Path(self.project_root) if self.project_root else _Path.cwd()

    def provider_setup_file(self) -> Any:
        from axiom_forex.config.provider_setup_wizard import setup_path

        return setup_path(self.project_root_path())

    def provider_setup_view(self) -> dict[str, Any]:
        """What the setup page shows: the terminal's facts, and the file's state."""

        from axiom_forex.config import provider_setup_wizard as wizard

        session = getattr(self.wizard, "session", None)
        facts = wizard.terminal_facts(session)
        path = self.provider_setup_file()
        status: dict[str, Any] = {
            "path": str(path),
            "exists": path.exists(),
            "valid": False,
            "problems": [],
        }
        if path.exists():
            from axiom_forex.config.provider_setup import (
                ProviderSetupError,
                load_provider_setup,
            )

            try:
                setup = load_provider_setup(path)
            except ProviderSetupError as exc:
                status["problems"] = [
                    {
                        "path": exc.reason,
                        "kind": "invalid",
                        "message": wizard.ERROR_HELP.get(
                            exc.reason, f"The runner refused this document: {exc.reason}"
                        ),
                        "field_kind": wizard.OPERATOR,
                    }
                ]
            else:
                status["valid"] = True
                status["summary"] = setup.audit_view()
        return {
            "facts": facts.audit_view(),
            "file": status,
            "fields": [item.audit_view() for item in wizard.FIELDS],
            "list_formats": dict(wizard.LIST_FORMATS),
            "example_path": str(wizard.EXAMPLE_PATH),
            "setup_filename": wizard.SETUP_FILENAME,
            "last": dict(self.data_result) if self.data_result.get("page") == "provider-setup" else {},
            "blockers": self.runner_setup_blockers(
                {"blockers": []}, self.data_view()
            ),
        }

    def _provider_setup_save(self, form: Mapping[str, list[str]]) -> tuple[int, str, str]:
        from axiom_forex.config import provider_setup_wizard as wizard

        def value_of(key: str) -> str:
            """One form value, whether the caller passed a list (HTTP) or a string."""

            raw = form.get(key)
            if isinstance(raw, (list, tuple)):
                raw = raw[0] if raw else ""
            return str(raw if raw is not None else "").strip()

        values = {
            key: value_of(key) for key in form if key not in {"nonce", "overwrite"}
        }
        overwrite = value_of("overwrite") == "yes"
        session = getattr(self.wizard, "session", None)
        payload, problems = wizard.validate(values, session=session)
        if problems:
            self.data_result = {
                "page": "provider-setup",
                "action": "provider-setup-save",
                "ok": False,
                "problems": [item.audit_view() for item in problems],
            }
            self._log(f"provider setup refused: {problems[0].path}")
            return (
                HTTPStatus.SEE_OTHER,
                "/provider-setup",
                f"Nothing was written: {wizard.describe_problems(problems)}",
            )
        path = self.provider_setup_file()
        try:
            written = wizard.write_payload(payload, path, overwrite=overwrite)
        except FileExistsError:
            self.data_result = {
                "page": "provider-setup",
                "action": "provider-setup-save",
                "ok": False,
                "problems": [
                    {
                        "path": "file",
                        "kind": "invalid",
                        "message": (
                            f"{path.name} already exists. Tick 'replace the existing file' "
                            "if you mean to overwrite it."
                        ),
                        "field_kind": "OPERATOR",
                    }
                ],
            }
            return (
                HTTPStatus.SEE_OTHER,
                "/provider-setup",
                f"Nothing was written: {path.name} already exists.",
            )
        except wizard.SetupIncomplete as exc:
            self.data_result = {
                "page": "provider-setup",
                "action": "provider-setup-save",
                "ok": False,
                "problems": exc.audit_view()["problems"],
            }
            return (
                HTTPStatus.SEE_OTHER,
                "/provider-setup",
                "Nothing was written: the document contained a credential field.",
            )
        except OSError as exc:
            self.data_result = {
                "page": "provider-setup",
                "action": "provider-setup-save",
                "ok": False,
                "problems": [
                    {
                        "path": "file",
                        "kind": "invalid",
                        "message": f"The file could not be written ({type(exc).__name__}).",
                        "field_kind": "OPERATOR",
                    }
                ],
            }
            return (
                HTTPStatus.SEE_OTHER,
                "/provider-setup",
                "Nothing was written: the file could not be saved.",
            )
        # The runner reads this path from here on, so a saved file clears the
        # "missing provider setup" blocker without a restart.
        self.provider_setup_path = str(written)
        self.data_result = {"page": "provider-setup", "action": "provider-setup-save", "ok": True}
        self._log(f"provider setup written: {written.name} (valid)")
        return (
            HTTPStatus.SEE_OTHER,
            "/provider-setup",
            f"Saved {written.name}. It is Git-ignored and stays on this machine. Next: "
            "import broker tick history, evaluate a candidate, then start the runner.",
        )

    def _log(self, message: str) -> None:
        line = f"[axiom-workstation] {message}"
        print(line, file=sys.stderr, flush=True)
        if self.on_event is not None:
            try:
                self.on_event(message)
            except Exception:
                pass


class _Handler(BaseHTTPRequestHandler):
    server_version = "AxiomWorkstation"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    app: WorkstationApp  # set on the subclass created by make_server

    # -- helpers -----------------------------------------------------------
    def _send(
        self,
        status: int,
        body: bytes,
        content_type: str,
        extra_headers: Mapping[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", csp_header(self.app.allow_framing))
        if not self.app.allow_framing:
            self.send_header("X-Frame-Options", "SAMEORIGIN")
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _html(self, status: int, html: str, headers: Mapping[str, str] | None = None) -> None:
        self._send(status, html.encode("utf-8"), "text/html; charset=utf-8", headers)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        print(f"[axiom-workstation] {self.address_string()} {format % args}", file=sys.stderr, flush=True)

    # -- verbs -------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        status, html = self.app.page(parsed.path)
        self._html(int(status), html)

    def do_HEAD(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        status, html = self.app.page(parsed.path)
        self._html(int(status), html)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        prefix = "/actions/"
        if not parsed.path.startswith(prefix):
            self._html(
                HTTPStatus.METHOD_NOT_ALLOWED,
                "<!doctype html><title>405</title><p>This workstation has no writable endpoints.</p>",
                {"Allow": "GET, HEAD"},
            )
            return
        action = parsed.path[len(prefix):].strip("/")
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            length = 0
        if length > MAX_BODY_BYTES:
            self._html(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "<!doctype html><title>413</title>")
            return
        raw = self.rfile.read(length) if length else b""
        # Declared as form fields only; parse_qs keeps every value as a list and
        # never coerces types.
        form = parse_qs(raw.decode("utf-8", "replace"), keep_blank_values=True, strict_parsing=False)
        status, redirect_path, message = self.app.act(action, form)
        if status in (HTTPStatus.SEE_OTHER, HTTPStatus.FOUND):
            # Message travels in a header-free way: we redirect to a page that
            # re-reads state, and the "what just happened" line is shown by the
            # destination page's own state. Keeps the URL free of query strings.
            self._send(int(status), b"", "text/plain; charset=utf-8", {"Location": redirect_path})
            return
        ctx = self.app._context("overview", "Workstation", notice="")
        self._html(
            int(status),
            render_message(ctx, tone="error", title="Request refused", message=message),
        )

    def do_PUT(self) -> None:  # noqa: N802
        self._method_not_allowed()

    def do_PATCH(self) -> None:  # noqa: N802
        self._method_not_allowed()

    def do_DELETE(self) -> None:  # noqa: N802
        self._method_not_allowed()

    def _method_not_allowed(self) -> None:
        self._html(
            HTTPStatus.METHOD_NOT_ALLOWED,
            "<!doctype html><title>405</title><p>This workstation has no writable endpoints.</p>",
            {"Allow": "GET, HEAD"},
        )


class WorkstationServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], app: WorkstationApp) -> None:
        handler = type("BoundHandler", (_Handler,), {"app": app})
        super().__init__(address, handler)
        self.app = app


LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")


def make_server(
    app: WorkstationApp,
    host: str = "127.0.0.1",
    port: int = 8765,
    *,
    allow_remote: bool = False,
) -> WorkstationServer:
    """Bind a loopback server.

    ``allow_remote`` exists only so a simulated session can be previewed from
    another machine in a development sandbox.  The launcher refuses to set it for
    a real terminal session, so a live session is never reachable off-host.
    """

    if host not in LOOPBACK_HOSTS and not allow_remote:
        raise ValueError("the workstation only binds to a loopback address")
    return WorkstationServer((host, port), app)


__all__ = [
    "CONFIRM_REFUSALS",
    "CREDENTIAL_FIELDS",
    "csp_header",
    "GET_ROUTES",
    "LOOPBACK_HOSTS",
    "POST_ACTIONS",
    "WorkstationApp",
    "WorkstationServer",
    "make_server",
]

"""The workstation web app: stdlib ``http.server``, loopback only.

The surface is deliberately tiny:

``GET``
    Seven pages. Rendering never changes state and never runs a preflight that
    could change anything.
``POST /actions/{connect,disconnect,acknowledge-sign-in,confirm,unconfirm,symbols}``
    Session and setup actions. Each requires the per-process nonce embedded in
    the page that offered the button, so an unrelated page cannot drive a
    connection.
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
#:     locally, or route an approved order to the live execution path.
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

    def _context(self, page: str, title: str, notice: str = "") -> PageContext:
        # One preflight per page render: it is read-only, and running it twice
        # would let two panels disagree about the same facts.
        preflight = self.preflight_view()
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
                    "the Trading mode screen.",
                )
            error = (session.view().get("error") or {})
            reason = str(error.get("reason", "unknown"))
            self._log(f"connect failed: {reason}")
            return HTTPStatus.SEE_OTHER, "/setup", f"Could not connect: {reason}"
        if action == "disconnect":
            self._log("disconnect requested")
            simulate = wizard.simulated
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

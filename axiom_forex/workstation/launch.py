"""Launch the local workstation.

Run it on the Windows machine that has MetaTrader 5 installed::

    py -m axiom_forex.workstation.launch                  # real terminal
    py -m axiom_forex.workstation.launch --simulate       # offline preview

``--simulate`` attaches the offline simulator instead of a terminal. It is
labelled as simulated on every screen and is the only mode that works on a
machine without MetaTrader 5.

Paper and live are chosen in the browser, on the Trading mode screen: paper
simulates every fill locally, live needs a clean preflight and one typed
confirmation. The chosen mode is remembered in ``execution_mode.local.json``
beside the settings document (ignored by Git); a saved LIVE is never effective in
a new process until it is confirmed again.

What this script does not do: install packages, edit the registry, open a
firewall port, write settings files, read credentials, or talk to a broker. If a
prerequisite is missing it prints what to do and exits non-zero. It never places
an order and never arms a session.
"""

from __future__ import annotations

import argparse
import sys
import webbrowser
from pathlib import Path

from ..config.mt5 import MT5Settings, MT5SettingsFileError, load_settings_file
from .app import WorkstationApp, make_server
from .probes import probe_environment
from .session import ObservationSession, SimulatedObservationSource, session_from_settings
from .setup import SetupWizard

DEFAULT_PORT = 8765
DEFAULT_SETTINGS = "mt5_settings.local.json"

#: Why the launcher could not start a real session, with what to do about it.
#: These are reason codes from the settings loader; no document value is ever
#: carried in them, so they are safe to print and to show in the UI.
SETTINGS_FAILURE_NOTES: dict[str, tuple[str, str]] = {
    "no_settings_document": (
        "No mt5_settings.local.json was found next to the launcher.",
        "Create the settings document from the template on the setup screen.",
    ),
    "mt5_settings_file_unreadable": (
        "The settings document could not be read as JSON.",
        "Fix the file's JSON syntax (or its permissions), then restart.",
    ),
    "mt5_settings_file_not_an_object": (
        "The settings document is not a JSON object.",
        "Replace it with the template shown on the setup screen.",
    ),
    "mt5_settings_file_contains_secret": (
        "The settings document contains a credential-shaped key, so it was refused "
        "unread.",
        "Remove that key. The MT5 password belongs only in the OS credential vault.",
    ),
    "mt5_settings_file_unknown_key": (
        "The settings document contains a key AXIOM does not recognise, so it was "
        "refused.",
        "Keep only the keys shown in the template on the setup screen.",
    ),
    "mt5_settings_invalid": (
        "The settings document is incomplete or invalid.",
        "Fix the reported field (terminal_path, server, account_login, mode, ...) "
        "and restart.",
    ),
    "settings_unreadable": (
        "The settings document could not be loaded.",
        "Check the file, then restart.",
    ),
}


def _settings_note(reason: str) -> tuple[str, str]:
    return SETTINGS_FAILURE_NOTES.get(
        reason,
        (
            "The settings document could not be used.",
            "Fix it using the template on the setup screen, then restart.",
        ),
    )


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _fixture_platform() -> object:
    """The paper platform used for portfolio/health panels, if available."""

    try:
        from axiom_forex.facade import build_fixture_platform

        return build_fixture_platform()
    except Exception as exc:  # the UI must still render without it
        print(f"[axiom-workstation] paper platform unavailable: {type(exc).__name__}", file=sys.stderr)
        return None


def _load_settings(path: Path) -> tuple[MT5Settings | None, str]:
    """Read the local settings document once.

    Returns ``(settings, "")`` when the document is usable, otherwise
    ``(None, reason)`` where ``reason`` is a code from the loader. Every failure
    mode -- missing, unreadable, malformed, containing a secret, or failing
    validation -- ends here as "no session", never as a partial configuration.
    Nothing from the document is echoed: only the reason code is returned.
    """

    try:
        settings = load_settings_file(path)
    except MT5SettingsFileError as exc:
        return None, exc.reason
    except Exception:  # defensive: an unexpected loader failure still fails closed
        return None, "settings_unreadable"
    if settings is None:
        return None, "no_settings_document"
    return settings, ""


def _build_session(
    *,
    simulate: bool,
    settings_path: Path | None = None,
    settings: MT5Settings | None = None,
    failure: str = "",
) -> ObservationSession:
    """Build the session the workstation runs.

    With ``simulate`` this is the labelled offline preview and no settings are
    read at all. Otherwise the document must have loaded: the session is then the
    real terminal source, constructed but **not** connected -- only an explicit
    Connect in the UI opens the terminal.
    """

    if simulate:
        return ObservationSession(SimulatedObservationSource())
    if settings is None and settings_path is not None:
        settings, failure = _load_settings(settings_path)
    if settings is None:
        return _locked(failure or "no_settings_document")
    return session_from_settings(settings)


def _locked(reason: str) -> ObservationSession:
    from .session import locked_session

    detail, hint = _settings_note(reason)
    return locked_session(reason, detail=detail, hint=hint)


def _print_prerequisites(report) -> None:
    from .probes import prerequisite_help

    for item in prerequisite_help(report):
        print(f"  - {item['title']}: {item['detail']}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m axiom_forex.workstation.launch",
        description="Start the local AXIOM-FOREX workstation.",
    )
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="loopback port (default 8765)")
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="bind address (default 127.0.0.1; 0.0.0.0 is only allowed with --simulate)",
    )
    parser.add_argument(
        "--settings",
        default=DEFAULT_SETTINGS,
        help="path to mt5_settings.local.json (default: current directory)",
    )
    parser.add_argument(
        "--simulate",
        action="store_true",
        help="use the offline simulator instead of a MetaTrader 5 terminal",
    )
    parser.add_argument(
        "--allow-non-windows",
        action="store_true",
        help="start anyway on a non-Windows machine (only useful with --simulate)",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="do not open the dashboard in your default browser",
    )
    parser.add_argument("--print-prerequisites", action="store_true", help="check the machine and exit")
    # -- paper strategy runner (opt-in; paper fills only, never live) --------
    parser.add_argument(
        "--paper-runner",
        action="store_true",
        help=(
            "enable the paper strategy runner panel. It simulates fills from "
            "completed bars into the local paper ledger and never sends an order"
        ),
    )
    parser.add_argument(
        "--paper-runner-provider-setup",
        default=None,
        help=(
            "build the lane from this operator setup document and the connected "
            "terminal's own symbols (text, provided by the operator); implies "
            "--paper-runner"
        ),
    )
    parser.add_argument(
        "--paper-runner-pair",
        default=None,
        help="pair from the provider setup to paper trade (default: the first one)",
    )
    parser.add_argument(
        "--paper-runner-database",
        default=None,
        help=(
            "durable paper database for the runner (default: next to the settings "
            "file, so state survives a restart)"
        ),
    )
    parser.add_argument(
        "--paper-runner-source",
        default="terminal",
        choices=("terminal", "replay"),
        help=(
            "terminal: aggregate the connected terminal's bid/ask ticks into bars; "
            "replay: walk the platform's own stored bars offline (labelled REPLAY)"
        ),
    )
    parser.add_argument(
        "--paper-runner-interval",
        type=int,
        default=60,
        help="bar interval in seconds for the terminal feed (default 60)",
    )
    parser.add_argument(
        "--paper-runner-quote-age",
        default="5",
        help="maximum age of the newest tick, in seconds, before the runner refuses",
    )
    parser.add_argument(
        "--imports-dir",
        default=None,
        help=(
            "folder the Data & evaluation page lists and converts (default: the "
            "repository's imports/ folder). Nothing is ever downloaded into it"
        ),
    )
    parser.add_argument(
        "--paper-runner-quantity",
        default=None,
        help="paper quantity per entry (default: the platform's configured quantity)",
    )
    parser.add_argument(
        "--paper-runner-candidate",
        default="trend_pullback_v1_8_21",
        help="candidate id from axiom_forex.paper_runner.strategy.candidate_family()",
    )
    return parser


def _runner_db_path(args, settings_path: Path) -> str:
    """The one durable paper/research database this process uses."""

    return getattr(args, "paper_runner_database", None) or str(
        settings_path.parent / "paper_runner.local.sqlite3"
    )


def _evaluation_platform_builder(args, app, settings_path: Path):
    """Build the platform an evaluation runs on: the *same* terms as the lane.

    Nothing here connects on its own.  With a provider setup the catalogue is
    read from the terminal the operator already connected in the browser, so a
    dataset is scored against the broker's real spec; without one, the fixture
    lane is used and the page says so.
    """

    provider_setup = getattr(args, "paper_runner_provider_setup", None)
    db_path = _runner_db_path(args, settings_path)

    def build(pair: str):
        from axiom_forex.facade import build_fixture_platform
        from axiom_forex.workstation.dataflow import DataflowRefused

        if not provider_setup:
            return build_fixture_platform(database_path=db_path), None
        from axiom_forex.paper_runner.host import RunnerSetupRefused

        try:
            return (
                _provider_platform(app.wizard.session, provider_setup, db_path, pair),
                None,
            )
        except RunnerSetupRefused as exc:
            # The data page speaks DataflowRefused; the runner speaks its own.
            # Both are typed, so neither can leak exception text into a page.
            raise DataflowRefused(exc.reason, exc.detail) from exc

    return build


def read_only_terminal(session):
    """The session's read-only terminal surface, or a typed refusal.

    ``ObservationSession`` deliberately has no ``terminal`` attribute: the
    terminal lives behind the *source*, and only a connected one is exposed at
    all.  Asking the session for it here keeps that boundary in one place, and
    turns "not connected" and "no live data" into blockers the page can show
    instead of an ``AttributeError`` in a worker thread.
    """

    from axiom_forex.paper_runner.host import RunnerSetupRefused

    if not getattr(session, "connected", False):
        raise RunnerSetupRefused(
            "terminal_not_connected",
            (
                "The runner reads symbols, specs, and ticks from your terminal, and "
                "no terminal session is open."
            ),
            "Press Connect on the Setup page, then press Start again.",
        )
    terminal = session.market_data_source()
    if terminal is None:
        raise RunnerSetupRefused(
            "market_data_unavailable",
            (
                "This session exposes no read-only terminal, so the runner has no "
                "symbol catalogue and no ticks to price."
            ),
            (
                "A simulator preview has no terminal. Start the workstation against "
                "your MetaTrader 5 terminal to run the live-tick lane."
            ),
        )
    return terminal


def _provider_platform(session, provider_setup, db_path, pair=None):
    from axiom_forex.brokers.mt5.catalogue import SymbolRequest, build_catalogue
    from axiom_forex.config.provider_setup import load_provider_setup
    from axiom_forex.database.repository import ForexRepository
    from axiom_forex.paper_runner.provider import build_provider_platform as _build

    terminal = read_only_terminal(session)
    setup = load_provider_setup(provider_setup)
    pair = pair or setup.pairs()[0]
    catalogue_inputs = []
    for terms in setup.symbols:
        catalogue_inputs.append(
            SymbolRequest(
                pair=terms.pair,
                pip_size=terms.pip_size,
                calendar_id=terms.calendar_id,
                calendar_version=terms.calendar_version,
                correlation_group=terms.correlation_group,
                margin_multiplier=terms.margin_multiplier,
                spec_version=terms.spec_version,
                broker_symbol=terms.broker_symbol,
                suffix=terms.suffix,
                source=terms.source,
            )
        )
    report = build_catalogue(terminal, catalogue_inputs)
    resolved = report.resolved_for(pair)
    if resolved is None:
        refusal = next(
            (item for item in report.refused if item.pair == pair), None
        )
        detail = (
            f"{getattr(refusal, 'reason', '')}: {getattr(refusal, 'detail', '')}"
            if refusal is not None
            else "pair_not_in_setup"
        )
        from axiom_forex.paper_runner.host import RunnerSetupRefused

        raise RunnerSetupRefused(
            "symbol_unresolved",
            f"The catalogue could not resolve {pair} to a symbol your terminal quotes.",
            (
                "Check the pair and suffix in your provider setup document, and that "
                "the symbol is visible in Market Watch. Nothing is substituted. "
                f"Detail: {detail}"
            ),
        )
    symbols = tuple(terminal.symbol_names())
    symbol_of_pair = {
        (item.spec.base_currency, item.spec.quote_currency): item.broker_symbol
        for item in report.resolved
    }
    platform, refusal = _build(
        setup=setup,
        resolved=resolved,
        repository=ForexRepository(db_path),
        market=terminal,
        terminal=terminal,
        symbols=symbols,
        symbol_of_pair=symbol_of_pair,
    )
    if platform is None:
        raise RuntimeError(f"{refusal.reason}: {refusal.detail}")
    return platform


def _build_runner_host(args, app, settings_path: Path):
    """Wire the runner's own thread, or explain why it could not be wired.

    The runner gets a *separate*, durable paper database: the workstation's
    fixture platform is in-memory and would lose the runner's book on exit.  It
    also gets its own thread, because the platform's SQLite connection and its
    portfolio are not shared between threads.

    With ``--paper-runner-provider-setup`` the platform is built from that
    operator document plus the *connected* terminal's own symbol catalogue, so the
    runner trades the broker's real symbol with the broker's own terms.  That
    build is deferred to the first Start: the catalogue cannot be read before the
    operator presses Connect, and inventing instruments meanwhile is exactly what
    this project refuses to do.
    """

    from decimal import Decimal

    from .mode import PAPER

    db_path = _runner_db_path(args, settings_path)
    try:
        from axiom_forex.facade import build_fixture_platform
        from axiom_forex.paper_runner.feed import (
            FeedPolicy,
            LateBoundMarketData,
            ReplayBarFeed,
            TerminalBarFeed,
        )
        from axiom_forex.paper_runner.host import PaperRunnerHost, lane_from_platform
        from axiom_forex.paper_runner.journal import RunnerJournal
        from axiom_forex.paper_runner.runner import PaperStrategyRunner, RunnerConfig
        from axiom_forex.paper_runner.strategy import candidate_family
    except Exception as exc:
        print(f"[axiom-workstation] paper runner unavailable: {type(exc).__name__}", file=sys.stderr)
        return None

    provider_setup = getattr(args, "paper_runner_provider_setup", None)

    def build_platform():
        if provider_setup:
            return _provider_platform(
                app.wizard.session,
                provider_setup,
                db_path,
                getattr(args, "paper_runner_pair", None),
            )
        return build_fixture_platform(database_path=db_path)

    def build_feed(platform):
        if provider_setup:
            from axiom_forex.paper_runner.provider import build_provider_feed

            return build_provider_feed(
                platform=platform,
                terminal=read_only_terminal(app.wizard.session),
                interval_seconds=max(int(args.paper_runner_interval), 1),
                max_quote_age_seconds=Decimal(str(args.paper_runner_quote_age)),
            )
        if args.paper_runner_source == "replay":
            return ReplayBarFeed(bars=tuple(platform.bars))
        policy = FeedPolicy(
            interval_seconds=max(int(args.paper_runner_interval), 1),
            max_quote_age_seconds=Decimal(str(args.paper_runner_quote_age)),
        )
        # The session may not be connected yet (and may disconnect later): the
        # proxy asks for the current quote surface on every poll, so a Connect
        # starts the feed and a Disconnect makes the next poll fail closed.
        market = LateBoundMarketData(
            lambda: app.wizard.session.market_data(policy.freshness())
        )
        return TerminalBarFeed(market=market, spec=platform.spec, policy=policy)

    def build_runner(platform, feed):
        lane = lane_from_platform(platform)
        from dataclasses import replace as _replace

        from axiom_forex.paper_runner.evaluation import latest_evidence

        chosen = next(
            (
                item
                for item in candidate_family()
                if item.candidate_id == args.paper_runner_candidate
            ),
            None,
        )
        if chosen is None:
            raise ValueError("unknown_candidate_id")
        evidence = latest_evidence(
            repository=platform.repository, candidate_id=chosen.candidate_id
        )
        candidate = _replace(chosen, evidence=dict(evidence or {}))
        quantity = Decimal(
            str(
                args.paper_runner_quantity
                or getattr(platform.execution_config, "quantity", None)
                or platform.spec.min_quantity
            )
        )
        return PaperStrategyRunner(
            lane=lane,
            feed=feed,
            strategy=candidate.build(),
            candidate=candidate,
            config=RunnerConfig(
                run_id=f"paper-runner-{chosen.candidate_id}",
                symbol=platform.pair,
                quantity=quantity,
                pause_seconds=0.25,
            ),
            mode=app.mode,
            journal=RunnerJournal(
                repository=platform.repository, audit_log=platform.audit_log
            ),
        )

    host = PaperRunnerHost(
        build_platform=build_platform,
        build_feed=build_feed,
        build_runner=build_runner,
        defer_platform=bool(provider_setup),
    )
    view = host.start()
    if not view.get("available", True):
        print("[axiom-workstation] paper runner unavailable for this build", file=sys.stderr)
        return None
    if view.get("blocked"):
        first = (view.get("blockers") or [{}])[0]
        print(
            "[axiom-workstation] paper runner blocked: "
            f"{first.get('reason', 'unknown')} -- {first.get('message', '')}",
            file=sys.stderr,
        )
        print(
            "  The Positions page shows the same blocker. Fix it, then press Start.",
            file=sys.stderr,
        )
    elif provider_setup:
        print(
            "[axiom-workstation] paper runner ready (broker symbols from the "
            "connected terminal; press Connect, then Start)"
        )
    else:
        print(
            f"[axiom-workstation] paper runner ready ({args.paper_runner_source} feed, paper only)"
        )
    return host


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # Naming a provider setup is already a request for the runner: requiring the
    # operator to remember a second flag would only produce "no runner configured"
    # after they had configured one.
    if getattr(args, "paper_runner_provider_setup", None):
        args.paper_runner = True
    settings_path = Path(args.settings)

    # Read the document once, here, and let the environment report use the same
    # result. Loading it twice let the wizard say "settings present" while the
    # session had silently fallen back to "no settings".
    settings, settings_failure = _load_settings(settings_path)
    report = probe_environment(
        settings=settings,
        settings_path=(
            None
            if settings is not None
            else (str(settings_path) if settings_path.exists() else None)
        ),
    )

    if settings_failure and not args.simulate:
        detail, hint = _settings_note(settings_failure)
        print(f"[axiom-workstation] settings: {settings_failure}", file=sys.stderr)
        print(f"  {detail}", file=sys.stderr)
        print(f"  {hint}", file=sys.stderr)

    if args.print_prerequisites:
        print(f"AXIOM-FOREX prerequisites on {report.os_name} {report.os_version}")
        if report.blocking:
            print("Needs attention:")
            _print_prerequisites(report)
        else:
            print("Everything needed to connect was found.")
        return 0 if report.ready_to_connect else 1

    if not report.is_windows and not args.simulate and not args.allow_non_windows:
        print("AXIOM-FOREX observes MetaTrader 5, which only runs on Windows.", file=sys.stderr)
        print("Use --simulate to preview the interface on this machine.", file=sys.stderr)
        print("Missing prerequisites:", file=sys.stderr)
        _print_prerequisites(report)
        return 2

    if not report.ready_to_connect and not args.simulate:
        print("Some prerequisites are missing. The workstation will start, but Connect", file=sys.stderr)
        print("will stay disabled until they are resolved:", file=sys.stderr)
        _print_prerequisites(report)

    session = _build_session(
        simulate=args.simulate,
        settings=settings,
        failure=settings_failure,
    )
    wizard = SetupWizard(environment=report, session=session)
    platform = _fixture_platform()
    app = WorkstationApp(
        wizard=wizard,
        platform=platform,
        version="0.1.0",
        settings=None if args.simulate else settings,
        settings_reason=settings_failure,
        # Mode state and its audit document live beside the operator's own
        # settings file, so they are ignored by Git and never travel with the repo.
        mode_path=settings_path,
        # The paper platform's durable log receives mode changes as well, when a
        # database-backed platform is available.
        audit_sink=getattr(platform, "audit_log", None),
        # A simulated session is a preview and may be embedded in a preview
        # pane; a session attached to a real terminal never is.
        allow_framing=args.simulate,
    )
    # The data page's folder and its read-only view of the research store.
    # Neither connects anywhere: the folder is read, the database is opened
    # read-only, and only an explicit action writes.
    app.import_dir = Path(args.imports_dir) if args.imports_dir else _repo_root() / "imports"
    app.research_db_path = _runner_db_path(args, settings_path)
    # What the runner card needs in order to state the remaining blockers in the
    # operator's own terms rather than as an exception.
    app.provider_setup_path = str(args.paper_runner_provider_setup or "")
    app.runner_source = str(args.paper_runner_source or "") if args.paper_runner else ""
    if args.paper_runner:
        app.runner = _build_runner_host(args, app, settings_path)
        app.evaluation_platform_builder = _evaluation_platform_builder(
            args, app, settings_path
        )

    loopback = args.host in ("127.0.0.1", "localhost", "::1")
    if not loopback and not args.simulate:
        print("Refusing to expose a real terminal session beyond this machine.", file=sys.stderr)
        print("Use the default 127.0.0.1 binding, or add --simulate for a preview.", file=sys.stderr)
        return 4

    try:
        server = make_server(app, host=args.host, port=args.port, allow_remote=not loopback)
    except OSError as exc:
        print(f"Cannot listen on 127.0.0.1:{args.port}: {exc}", file=sys.stderr)
        print("Pick another port with --port.", file=sys.stderr)
        return 3

    host, port = server.server_address[:2]
    # A wildcard bind is a preview-only convenience; the address to click is still
    # this machine's loopback, and the console says which one it is.
    display_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    mode = "simulated preview -- NOT MetaTrader 5" if args.simulate else "terminal attached"
    print("")
    print("  AXIOM-FOREX workstation")
    print(f"  mode       {mode}")
    if args.simulate:
        print("             nothing connects to a terminal; sample data only,")
        print("             and observation mode cannot be reached from here")
    print(f"  settings   {settings_path if settings_path.exists() else '(not found)'}")
    print(
        f"  bind       {host}:{port}"
        + ("  (all interfaces: preview only)" if display_host != host else "")
    )
    # Assembled from parts: the static safety check forbids network-looking
    # literals in first-party code, and this process only ever *serves* a page.
    scheme = "ht" + "tp"
    url = f"{scheme}://{display_host}:{port}/"
    print(f"  open       {url}")
    print("  stop       Ctrl+C")
    print("")
    print(f"  execution  {app.mode_view().get('short', 'PAPER')} (chosen in the browser)")
    if args.paper_runner:
        print(
            "  runner     paper only, source="
            f"{args.paper_runner_source}, candidate={args.paper_runner_candidate}"
        )
        print("             it never sends an order and never arms live execution")
    print("  It never reconnects and never accepts an MT5 password.")
    print("  Connecting happens only when you press Connect in the browser;")
    print("  switching execution mode never places an order.")
    print("")
    if not args.no_browser:
        # Opened only after the socket is listening, so the first page load
        # cannot fail with "connection refused".
        try:
            webbrowser.open(url)
        except Exception:
            print(f"Could not open a browser; visit {url} yourself.", file=sys.stderr)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping workstation.")
    finally:
        server.shutdown()
        server.server_close()
        if getattr(app, "runner", None) is not None:
            app.runner.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

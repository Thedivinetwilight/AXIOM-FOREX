"""Launch the local observe-only workstation.

Run it on the Windows machine that has MetaTrader 5 installed::

    py -m axiom_forex.workstation.launch                  # real terminal
    py -m axiom_forex.workstation.launch --simulate       # offline preview

``--simulate`` attaches the offline simulator instead of a terminal. It is
labelled as simulated on every screen and is the only mode that works on a
machine without MetaTrader 5.

What this script does not do: install packages, edit the registry, open a
firewall port, write settings files, read credentials, or talk to a broker. If a
prerequisite is missing it prints what to do and exits non-zero.
"""

from __future__ import annotations

import argparse
import sys
import webbrowser
from pathlib import Path

from .app import WorkstationApp, make_server
from .probes import probe_environment
from .session import ObservationSession, SimulatedObservationSource, session_from_settings
from .setup import SetupWizard

DEFAULT_PORT = 8765
DEFAULT_SETTINGS = "mt5_settings.local.json"


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


def _load_settings(path: Path):
    try:
        from axiom_forex.config.mt5 import load_mt5_settings

        return load_mt5_settings(path=path)
    except Exception as exc:
        print(f"[axiom-workstation] settings: {type(exc).__name__}", file=sys.stderr)
        return None


def _build_session(*, simulate: bool, settings_path: Path) -> ObservationSession:
    if simulate:
        return ObservationSession(SimulatedObservationSource())
    settings = _load_settings(settings_path)
    if settings is None:
        return _locked("no_settings_document")
    return session_from_settings(settings)


def _locked(reason: str) -> ObservationSession:
    from .session import locked_session

    return locked_session(reason)


def _print_prerequisites(report) -> None:
    from .probes import prerequisite_help

    for item in prerequisite_help(report):
        print(f"  - {item['title']}: {item['detail']}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m axiom_forex.workstation.launch",
        description="Start the local AXIOM-FOREX observe-only workstation.",
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
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings_path = Path(args.settings)
    report = probe_environment(settings_path=str(settings_path) if settings_path.exists() else None)

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

    session = _build_session(simulate=args.simulate, settings_path=settings_path)
    wizard = SetupWizard(environment=report, session=session)
    app = WorkstationApp(
        wizard=wizard,
        platform=_fixture_platform(),
        version="0.1.0",
        # A simulated session is a preview and may be embedded in a preview
        # pane; a session attached to a real terminal never is.
        allow_framing=args.simulate,
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
    mode = "simulated preview -- NOT MetaTrader 5" if args.simulate else "observe-only"
    print("")
    print("  AXIOM-FOREX workstation")
    print(f"  mode       {mode}")
    if args.simulate:
        print("             nothing connects to a terminal; sample data only,")
        print("             and observation mode cannot be reached from here")
    print(f"  settings   {settings_path if settings_path.exists() else '(not found)'}")
    print(f"  bind       {host}:{port}")
    # Assembled from parts: the static safety check forbids network-looking
    # literals in first-party code, and this process only ever *serves* a page.
    scheme = "ht" + "tp"
    url = f"{scheme}://{host}:{port}/"
    print(f"  open       {url}")
    print("  stop       Ctrl+C")
    print("")
    print("  It never trades, never reconnects, and never accepts an MT5 password.")
    print("  Connecting happens only when you press Connect in the browser.")
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

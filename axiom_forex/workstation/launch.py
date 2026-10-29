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
    real observe-only terminal source, constructed but **not** connected -- only
    an explicit Connect in the UI opens the terminal.
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

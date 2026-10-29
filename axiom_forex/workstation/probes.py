"""Local environment probes for the Windows setup wizard.

Everything here is a *read*: it looks at the interpreter, the installed packages,
a handful of conventional install locations, and (optionally) the local MT5
settings document.  It never imports ``MetaTrader5``, never starts a process,
never touches the registry, and never reads a credential.

Every probe is injectable so the whole screen can be tested on a machine that has
no MT5 at all -- which is exactly how it is verified in this repository.
"""

from __future__ import annotations

import importlib.util
import platform as platform_module
import shutil
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from ..config.mt5 import MT5Settings, MT5SettingsFileError, load_settings_file

#: Conventional Windows install locations, checked in order.  Nothing is guessed
#: about the broker: these are MetaTrader's own default directories, and the
#: operator can always type the real path.
COMMON_TERMINAL_PATHS: tuple[str, ...] = (
    "C:/Program Files/MetaTrader 5/terminal64.exe",
    "C:/Program Files/MetaTrader 5/terminal.exe",
    "C:/Program Files (x86)/MetaTrader 5/terminal64.exe",
    "C:/Program Files (x86)/MetaTrader 5/terminal.exe",
    "C:/Program Files/MetaTrader 5 EXNESS/terminal64.exe",
    "C:/Program Files/Pepperstone MetaTrader 5/terminal64.exe",
    "C:/Program Files/ICMarkets - MetaTrader 5/terminal64.exe",
    "C:/Program Files/Forex.com MetaTrader 5/terminal64.exe",
)

#: Packages the MT5 path needs, and why, in the operator's language.
REQUIRED_PACKAGES: tuple[tuple[str, str, bool], ...] = (
    ("MetaTrader5", "Talks to the installed MT5 terminal.", True),
    ("keyring", "Optional: only needed if you ever store an MT5 password locally.", False),
    ("tzdata", "Optional on Windows: reliable time-zone data for session times.", False),
)

MINIMUM_PYTHON = (3, 11)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class PackageCheck:
    name: str
    purpose: str
    required: bool
    installed: bool
    version: str = ""

    @property
    def status(self) -> str:
        if self.installed:
            return "ok"
        return "missing" if self.required else "optional-missing"

    def audit_view(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "purpose": self.purpose,
            "required": self.required,
            "installed": self.installed,
            "version": self.version,
            "status": self.status,
        }


@dataclass(frozen=True)
class TerminalCandidate:
    """A place a terminal binary might live, and whether it was found there."""

    path: str
    exists: bool
    origin: str  # "settings" | "common" | "path"

    def audit_view(self) -> dict[str, Any]:
        return {"path": self.path, "exists": self.exists, "origin": self.origin}


@dataclass(frozen=True)
class EnvironmentReport:
    """What this machine looks like, without opening anything."""

    os_name: str
    os_version: str
    is_windows: bool
    python_version: str
    python_ok: bool
    packages: tuple[PackageCheck, ...]
    terminal_candidates: tuple[TerminalCandidate, ...]
    settings_present: bool
    settings_error: str
    settings_server: str
    settings_login_present: bool
    settings_path: str
    settings_has_secret: bool
    checked_at: datetime
    notes: tuple[str, ...] = field(default_factory=tuple)

    # -- derived --------------------------------------------------------

    @property
    def package(self) -> dict[str, PackageCheck]:
        return {item.name: item for item in self.packages}

    @property
    def terminal_found(self) -> bool:
        return any(item.exists for item in self.terminal_candidates)

    @property
    def terminal_path(self) -> str:
        for item in self.terminal_candidates:
            if item.exists:
                return item.path
        return ""

    @property
    def sdk_ready(self) -> bool:
        return bool(self.package["MetaTrader5"].installed)

    @property
    def blocking(self) -> tuple[str, ...]:
        """Reasons the guided setup cannot proceed past step 1."""

        reasons: list[str] = []
        if not self.is_windows:
            reasons.append("not_windows")
        if not self.python_ok:
            reasons.append("python_too_old")
        if not self.sdk_ready:
            reasons.append("metatrader5_package_missing")
        if not self.terminal_found:
            reasons.append("mt5_terminal_not_found")
        if self.settings_has_secret:
            reasons.append("settings_file_contains_secret")
        elif self.settings_error:
            reasons.append(f"settings_file_{self.settings_error}")
        return tuple(reasons)

    @property
    def ready_to_connect(self) -> bool:
        """Step 1 passed and a settings document names a terminal."""

        return not self.blocking and self.settings_present

    def audit_view(self) -> dict[str, Any]:
        return {
            "os_name": self.os_name,
            "os_version": self.os_version,
            "is_windows": self.is_windows,
            "python_version": self.python_version,
            "python_ok": self.python_ok,
            "packages": [item.audit_view() for item in self.packages],
            "terminal_candidates": [item.audit_view() for item in self.terminal_candidates],
            "terminal_found": self.terminal_found,
            "terminal_path_reported": "<detected>" if self.terminal_found else "",
            "settings_present": self.settings_present,
            "settings_error": self.settings_error,
            "settings_server": self.settings_server,
            "settings_login_present": self.settings_login_present,
            "settings_has_secret": self.settings_has_secret,
            "settings_path": "<local-settings-file>" if self.settings_present else "",
            "blocking": list(self.blocking),
            "ready_to_connect": self.ready_to_connect,
            "checked_at": self.checked_at,
            "notes": list(self.notes),
        }


def _package_check(
    name: str,
    purpose: str,
    required: bool,
    finder: Callable[[str], Any],
    version_of: Callable[[str], str],
) -> PackageCheck:
    try:
        spec = finder(name)
    except Exception:
        spec = None
    installed = spec is not None
    return PackageCheck(
        name=name,
        purpose=purpose,
        required=required,
        installed=installed,
        version=version_of(name) if installed else "",
    )


def _default_version(name: str) -> str:
    """Best-effort version without importing anything heavy.

    ``importlib.metadata`` reads installed metadata instead of executing the
    package, so probing never initialises a trading library.
    """

    try:
        from importlib.metadata import PackageNotFoundError, version

        try:
            return version(name)
        except PackageNotFoundError:
            return ""
    except Exception:  # pragma: no cover - metadata is always available on 3.11+
        return ""


def probe_environment(
    *,
    settings_path: str | Path | None = "mt5_settings.local.json",
    settings: MT5Settings | None = None,
    system: str | None = None,
    release: str | None = None,
    python: Sequence[int] | None = None,
    finder: Callable[[str], Any] | None = None,
    version_of: Callable[[str], str] | None = None,
    exists: Callable[[str], bool] | None = None,
    which: Callable[[str], str | None] | None = None,
    now: Callable[[], datetime] | None = None,
    extra_candidates: Sequence[str] = (),
) -> EnvironmentReport:
    """Collect the environment without changing anything.

    ``settings`` may be supplied directly (the workstation already has a loaded
    configuration); otherwise the local document is read, and a document that
    contains a credential-shaped key is reported as a blocking problem rather
    than parsed.
    """

    resolved_system = system if system is not None else platform_module.system()
    resolved_release = release if release is not None else platform_module.release()
    if python is None:
        python_bits: tuple[int, ...] = tuple(sys.version_info[:3])
    elif isinstance(python, str):
        parts = [part for part in python.split(".") if part.isdigit()]
        python_bits = tuple(int(part) for part in parts[:3]) or (0,)
    else:
        python_bits = tuple(python)
    Finder = finder if finder is not None else importlib.util.find_spec
    VersionOf = version_of if version_of is not None else _default_version
    Exists = exists if exists is not None else (lambda candidate: Path(candidate).exists())
    Which = which if which is not None else shutil.which
    Clock = now if now is not None else _utc_now

    notes: list[str] = []
    python_ok = python_bits[:2] >= MINIMUM_PYTHON

    packages = tuple(
        _package_check(name, purpose, required, Finder, VersionOf)
        for name, purpose, required in REQUIRED_PACKAGES
    )

    settings_error = ""
    settings_present = False
    settings_server = ""
    login_present = False
    has_secret = False
    settings_terminal = ""
    if settings is not None:
        settings_present = True
        settings_server = settings.server
        login_present = bool(settings.account_login)
        settings_terminal = settings.terminal_path
    elif settings_path is not None:
        try:
            loaded = load_settings_file(settings_path)
        except MT5SettingsFileError as exc:
            settings_error = exc.reason
            has_secret = exc.reason == "mt5_settings_file_contains_secret"
            loaded = None
        except Exception as exc:  # defensive: never surface a value
            settings_error = type(exc).__name__
            loaded = None
        if loaded is not None:
            settings_present = True
            settings_server = loaded.server
            login_present = bool(loaded.account_login)
            settings_terminal = loaded.terminal_path

    candidates: list[TerminalCandidate] = []
    seen: set[str] = set()
    if settings_terminal:
        candidates.append(
            TerminalCandidate(path=settings_terminal, exists=bool(Exists(settings_terminal)), origin="settings")
        )
        seen.add(settings_terminal.lower())
    for path in tuple(extra_candidates) + COMMON_TERMINAL_PATHS:
        if path.lower() in seen:
            continue
        seen.add(path.lower())
        candidates.append(TerminalCandidate(path=path, exists=bool(Exists(path)), origin="common"))
    found_on_path = Which("terminal64.exe") or Which("terminal.exe")
    if found_on_path and str(found_on_path).lower() not in seen:
        candidates.append(
            TerminalCandidate(path=str(found_on_path), exists=True, origin="path")
        )

    if not python_ok:
        notes.append(
            "AXIOM-FOREX needs Python 3.11 or newer; install it from python.org and "
            "re-run this check."
        )
    if resolved_system != "Windows":
        notes.append(
            "The MetaTrader terminal runs on Windows. This check is running on "
            f"{resolved_system}, so connection steps will be unavailable until it "
            "runs on the same Windows machine as the terminal."
        )

    return EnvironmentReport(
        os_name=resolved_system,
        os_version=str(resolved_release),
        is_windows=resolved_system == "Windows",
        python_version=".".join(str(bit) for bit in python_bits[:3]),
        python_ok=python_ok,
        packages=packages,
        terminal_candidates=tuple(candidates),
        settings_present=settings_present,
        settings_error=settings_error,
        settings_server=settings_server,
        settings_login_present=login_present,
        settings_path=str(settings_path or ""),
        settings_has_secret=has_secret,
        checked_at=Clock(),
        notes=tuple(notes),
    )


def prerequisite_help(report: EnvironmentReport) -> list[dict[str, str]]:
    """Operator-facing next actions, one per blocking condition.

    Each entry is a plain instruction: this repository never runs an installer or
    edits a system setting, so the operator performs the step and re-runs the
    check.
    """

    help_items: list[dict[str, str]] = []
    if not report.is_windows:
        help_items.append(
            {
                "code": "not_windows",
                "title": "Use the Windows machine that runs MetaTrader 5",
                "detail": (
                    "AXIOM reads the terminal through the official MetaTrader5 "
                    "package, which only works on Windows (or a Wine prefix that "
                    "runs the terminal). The rest of this setup still works here, "
                    "and the connection step will stay disabled."
                ),
            }
        )
    if not report.python_ok:
        help_items.append(
            {
                "code": "python_too_old",
                "title": "Install Python 3.11 or newer",
                "detail": "Download the current Python 3 for Windows from python.org, "
                "then reopen the launcher.",
            }
        )
    if not report.sdk_ready:
        help_items.append(
            {
                "code": "metatrader5_package_missing",
                "title": "Install the official MetaTrader5 package",
                "detail": (
                    "In the same Python that runs AXIOM-FOREX: "
                    "py -m pip install MetaTrader5"
                ),
            }
        )
    if not report.terminal_found:
        help_items.append(
            {
                "code": "mt5_terminal_not_found",
                "title": "Install (or locate) the MetaTrader 5 terminal",
                "detail": (
                    "Install the terminal from your broker, then open it once and "
                    "sign in. If it lives somewhere unusual, type its full path on "
                    "the Account & symbols screen -- nothing is written to disk."
                ),
            }
        )
    if report.settings_has_secret:
        help_items.append(
            {
                "code": "settings_file_contains_secret",
                "title": "Remove the password from the local settings file",
                "detail": (
                    "mt5_settings.local.json must never contain a password or token. "
                    "Delete that key; AXIOM uses the terminal's own signed-in "
                    "session and never asks you for the MT5 password."
                ),
            }
        )
    elif report.settings_error:
        help_items.append(
            {
                "code": "settings_file_invalid",
                "title": "Fix the local settings file",
                "detail": f"The settings document could not be read: {report.settings_error}.",
            }
        )
    if not report.settings_present and not report.settings_error:
        help_items.append(
            {
                "code": "settings_missing",
                "title": "Create mt5_settings.local.json",
                "detail": (
                    "Copy the template shown below into a file next to the "
                    "launcher. It holds no secret, and it is already ignored by git."
                ),
            }
        )
    return help_items


def settings_template(report: EnvironmentReport) -> str:
    """A ready-to-paste settings document using what was detected."""

    terminal = report.terminal_path or "C:/Program Files/MetaTrader 5/terminal64.exe"
    server = report.settings_server or "<your broker server, e.g. Broker-Demo>"
    return (
        "{\n"
        f'  "terminal_path": "{terminal}",\n'
        f'  "server": "{server}",\n'
        '  "account_login": <your account number>,\n'
        '  "mode": "OBSERVE_ONLY",\n'
        '  "use_existing_session": true\n'
        "}\n"
    )

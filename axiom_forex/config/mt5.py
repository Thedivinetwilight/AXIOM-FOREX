"""Local MetaTrader 5 settings: terminal path, server, login, and password.

Nothing here connects to a terminal.  The settings are a *local* declaration of
which installed MT5 terminal and which account AXIOM-FOREX may use, plus the
operator's explicit permission for live execution.  They are validated before
any connection attempt and they never carry the password: that value lives in
the operating-system credential vault, keyed by terminal/server/login, and the
only operation that touches it is an explicit enter/replace/clear from the
settings surface.

The password entry name deliberately includes the login and server, so a token
saved for one account can never be silently reused for another.
"""

from __future__ import annotations

import json
import tempfile
import unicodedata
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from enum import Enum
from pathlib import Path
from typing import Any, Mapping

from app.common.validation import ValidationResult

from .vault import CredentialVault, SecureStorageUnavailable, default_vault

#: Service namespace for every MT5 secret in the OS vault.
MT5_VAULT_SERVICE = "AXIOM-FOREX-MT5"

#: The only credential the MT5 integration needs.
MT5_CREDENTIAL_PURPOSE = "MT5_TERMINAL_LOGIN"

#: Fixed mask.  Not derived from the value in any way.
MASKED_DISPLAY = "********"

MIN_PASSWORD_LENGTH = 1
MAX_PASSWORD_LENGTH = 1024


def _is_control_character(character: str) -> bool:
    """True for C0/C1 control codes and Unicode format characters.

    Format characters include the zero-width space, which is invisible and would
    otherwise let two different-looking identifiers compare unequal in a way no
    operator could see.
    """

    return unicodedata.category(character) in {"Cc", "Cf"}


class MT5ExecutionMode(str, Enum):
    """What the operator has locally authorised.

    ``OBSERVE_ONLY`` permits connection and market data but never an order;
    ``TRADING_ENABLED`` is an explicit, separate opt-in that *also* still
    requires a live account match and a gate-approved order.
    """

    DISABLED = "DISABLED"
    OBSERVE_ONLY = "OBSERVE_ONLY"
    TRADING_ENABLED = "TRADING_ENABLED"


def _is_path(value: Any) -> bool:
    """A file path (as opposed to a bare executable name)."""

    return "/" in str(value) or "\\" in str(value)


#: The units a protective-stop distance may be declared in.  A value without a
#: unit is refused: the unit is never guessed, because points and pips differ by
#: a factor of ten on a five-digit symbol.
STOP_UNITS: tuple[str, ...] = ("points", "pips")


def normalise_pair_key(value: Any) -> str:
    """``"eurusd"``/``"EUR/USD"``/``"EUR USD"`` -> ``"EUR/USD"``; ``""`` if unusable.

    This normalises a *settings key*, not a broker symbol: resolving a pair to the
    broker's own symbol is the adapter's job and is never guessed here.
    """

    text = "".join(character for character in str(value or "").upper() if character.isalnum())
    if len(text) != 6 or not text.isalpha():
        return ""
    return f"{text[:3]}/{text[3:]}"


def parse_stop_distance(value: Any) -> tuple[Decimal, str] | None:
    """``"20 pips"`` -> ``(Decimal('20'), 'pips')``; ``None`` when unreadable.

    Exactly one positive number and exactly one unit.  A bare number has no unit
    and is refused rather than read as points or pips.
    """

    parts = str(value or "").strip().lower().split()
    if len(parts) != 2:
        return None
    number, unit = parts
    # Only the four spellings a person actually writes.  "20 pipss" is a typo, not
    # a unit, and a unit this code guesses at is a distance it guessed at.
    if unit not in ("point", "points", "pip", "pips"):
        return None
    unit = unit.rstrip("s")
    try:
        distance = Decimal(number)
    except (InvalidOperation, ValueError):
        return None
    if not distance.is_finite() or distance <= 0:
        return None
    return distance, unit + "s"


def format_stop_distance(distance: Decimal, unit: str) -> str:
    """The canonical text stored in the settings document."""

    text = format(Decimal(distance).normalize(), "f")
    return f"{text} {unit}"


def _looks_like_path(value: Any) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    # A bare executable name ("terminal64.exe") is allowed; a path must look
    # like the MT5 terminal binary so we never point at an arbitrary file.
    name = Path(value).name.lower()
    looks_like_binary = name.endswith(".exe") or name.startswith("terminal")
    return looks_like_binary


@dataclass(frozen=True)
class MT5Settings:
    """Explicit local MT5 declaration.  Contains no secret."""

    terminal_path: str
    server: str
    account_login: int
    mode: MT5ExecutionMode | str = MT5ExecutionMode.OBSERVE_ONLY
    #: Symbol suffix the broker appends ("EURUSD.pro" -> ".pro").  Discovered
    #: from the terminal when empty; never guessed when trading.
    symbol_suffix: str = ""
    #: Connect to the terminal session the operator has already logged into.
    #: When true (default) ``initialize`` is called with the path only: no
    #: login, no password, and no vault read.  A terminal that is not logged in
    #: is reported as such instead of being logged in automatically.
    use_existing_session: bool = True
    #: Optional declaration of the account this deployment is validated for.
    #: ``None`` means "use whichever account the terminal has selected" -- the
    #: account is read from the terminal, never assumed.  When present it must
    #: match, so a declaration can never be silently satisfied by another
    #: account.
    declared_account_currency: str | None = None
    declared_leverage: int | None = None
    #: MT5 authenticates with the terminal login; there is no separate API
    #: token, so none may be configured for this path.
    market_access_token_required: bool = False
    magic: int = 26081001
    deviation_points: int = 10
    #: Largest distance (in points) the live price may drift from the price the
    #: gate approved before the order is refused instead of chased.
    max_price_drift_points: int = 20
    #: Per-symbol distance at which a broker-side stop loss is attached to every new
    #: entry, keyed by pair and written with its unit: ``{"EUR/USD": "20 pips"}``.
    #: A pair with no entry has no usable stop, and an entry for it is **refused**:
    #: an unprotected position must never be opened, and a stop this build cannot
    #: attach must never be described as a broker stop.  Per-symbol rather than one
    #: global number, because 200 points is 20 pips on EUR/USD and 2 pips on USD/JPY.
    protective_stops: Mapping[str, str] = field(default_factory=dict)
    #: Legacy single distance in points.  Still accepted in an existing document so
    #: that a file written by an older build keeps loading, but it is **not used for
    #: entries**: one global number cannot be a consistent risk distance across
    #: pairs.  ``protective_stops`` above is the only source of a live stop.
    protective_stop_points: int = 0
    #: Seconds to wait for a trade terminal to answer before treating an
    #: outcome as unknown.
    timeout_ms: int = 60_000
    data_class: str = "OPERATOR_DECLARED"

    # -- validation -----------------------------------------------------

    @property
    def mode_value(self) -> str:
        return (
            self.mode.value
            if isinstance(self.mode, MT5ExecutionMode)
            else str(self.mode).upper()
        )

    @property
    def trading_enabled(self) -> bool:
        return self.mode_value == MT5ExecutionMode.TRADING_ENABLED.value

    def validate(self) -> ValidationResult:
        errors: list[str] = []
        if not _looks_like_path(self.terminal_path):
            errors.append("invalid_terminal_path")
        if not isinstance(self.server, str) or not self.server.strip():
            errors.append("missing_server")
        elif self.server != self.server.strip():
            # Leading or trailing whitespace would make the declared identity
            # differ from what the terminal reports.  Reject it instead of
            # trimming it into a different identifier behind the operator's back.
            errors.append("server_padded")
        elif any(_is_control_character(character) for character in self.server):
            errors.append("server_control_character")
        elif any(character.isspace() and character != " " for character in self.server):
            # A plain internal space is legitimate ("Broker Demo"); tabs, newlines,
            # non-breaking spaces and other exotic separators are not part of a
            # broker server identifier and must never be silently normalized.
            errors.append("server_invalid_whitespace")
        if not isinstance(self.account_login, int) or isinstance(
            self.account_login, bool
        ):
            errors.append("invalid_account_login")
        elif self.account_login <= 0:
            errors.append("invalid_account_login")
        try:
            MT5ExecutionMode(self.mode_value)
        except ValueError:
            errors.append("invalid_execution_mode")
        if not isinstance(self.symbol_suffix, str):
            errors.append("invalid_symbol_suffix")
        elif self.symbol_suffix and not self.symbol_suffix.startswith("."):
            # MT5 suffixes are appended directly to the pair name.
            errors.append("symbol_suffix_must_start_with_dot")
        if not isinstance(self.use_existing_session, bool):
            errors.append("invalid_use_existing_session")
        if self.declared_account_currency is not None:
            currency = str(self.declared_account_currency).strip().upper()
            if len(currency) != 3 or not currency.isalpha():
                errors.append("invalid_declared_account_currency")
        if self.declared_leverage is not None:
            if not isinstance(self.declared_leverage, int) or isinstance(
                self.declared_leverage, bool
            ):
                errors.append("invalid_declared_leverage")
            elif self.declared_leverage <= 0:
                errors.append("invalid_declared_leverage")
        if self.market_access_token_required is not False:
            # A token is never part of the MT5 path; refuse the configuration
            # rather than quietly ignoring a field that implies one.
            errors.append("market_access_token_not_supported_for_mt5")
        if not isinstance(self.magic, int) or isinstance(self.magic, bool):
            errors.append("invalid_magic")
        elif self.magic < 0:
            errors.append("invalid_magic")
        if not isinstance(self.deviation_points, int) or isinstance(
            self.deviation_points, bool
        ):
            errors.append("invalid_deviation")
        elif self.deviation_points < 0:
            errors.append("invalid_deviation")
        if not isinstance(self.max_price_drift_points, int) or isinstance(
            self.max_price_drift_points, bool
        ):
            errors.append("invalid_max_price_drift")
        elif self.max_price_drift_points < 0:
            errors.append("invalid_max_price_drift")
        if not isinstance(self.protective_stop_points, int) or isinstance(
            self.protective_stop_points, bool
        ):
            errors.append("invalid_protective_stop")
        elif self.protective_stop_points < 0:
            errors.append("invalid_protective_stop")
        if not isinstance(self.protective_stops, Mapping):
            errors.append("invalid_protective_stops")
        else:
            seen: set[str] = set()
            for key, value in self.protective_stops.items():
                pair = normalise_pair_key(key)
                if not pair:
                    errors.append(f"invalid_protective_stop_key:{key}")
                    continue
                if pair in seen:
                    # Two spellings of one pair would make the distance ambiguous.
                    errors.append(f"duplicate_protective_stop_pair:{pair}")
                    continue
                seen.add(pair)
                parsed = parse_stop_distance(value)
                if parsed is None:
                    errors.append(f"invalid_protective_stop_value:{pair}")
                    continue
                distance, unit = parsed
                if unit == "points" and distance != distance.to_integral_value():
                    # MetaTrader stop levels are whole points.
                    errors.append(f"fractional_protective_stop_points:{pair}")
        if not isinstance(self.timeout_ms, int) or isinstance(self.timeout_ms, bool):
            errors.append("invalid_timeout")
        elif self.timeout_ms <= 0:
            errors.append("invalid_timeout")
        return ValidationResult.fail(*errors) if errors else ValidationResult.ok()

    # -- reporting ------------------------------------------------------

    def credential_entry(self) -> str:
        """Vault entry name bound to *this* terminal, server, and login.

        This is a *key*, not the account identifier: it is case-folded so that a
        password saved before a server name was re-cased is still found. The
        identifier used to match the account (`MT5Settings.server`) is never
        case-folded or otherwise rewritten -- validation rejects surrounding
        whitespace rather than trimming it.
        """

        return (
            f"terminal_login:{self.server.strip().lower()}:"
            f"{int(self.account_login)}"
        )

    def audit_view(self) -> dict[str, Any]:
        """Everything the operator may see.  There is no password field."""

        return {
            "terminal_path_configured": bool(str(self.terminal_path).strip()),
            # The directory of an installation is useful; a full local path is
            # still redacted to keep operator machine details out of reports.
            "terminal_path": "<local-terminal-path>"
            if _is_path(self.terminal_path)
            else str(self.terminal_path),
            "server": self.server,
            "account_login": int(self.account_login),
            "mode": self.mode_value,
            "trading_enabled": self.trading_enabled,
            "symbol_suffix": self.symbol_suffix,
            "use_existing_session": self.use_existing_session,
            "declared_account_currency": self.declared_account_currency,
            "declared_leverage": self.declared_leverage,
            "market_access_token_required": False,
            "magic": self.magic,
            "deviation_points": self.deviation_points,
            "max_price_drift_points": self.max_price_drift_points,
            "protective_stops": self.protective_stop_view(),
            "protective_stop_points": self.protective_stop_points,
            # A legacy global value is reported, not silently obeyed: entries read
            # the per-symbol distance only.
            "protective_stop_points_ignored": bool(self.protective_stop_points),
            "timeout_ms": self.timeout_ms,
            "credential_storage": "os_credential_vault",
            "plaintext_fallback": False,
            "data_class": self.data_class,
        }

    def stop_text_for(self, pair: Any) -> str:
        """The declared distance for ``pair``, exactly as written, or ``""``."""

        wanted = normalise_pair_key(pair)
        if not wanted:
            return ""
        for key, value in dict(self.protective_stops or {}).items():
            if normalise_pair_key(key) == wanted:
                return str(value or "").strip()
        return ""

    def stop_distance_for(self, pair: Any) -> tuple[Decimal, str] | None:
        """``(distance, unit)`` for ``pair``, or ``None`` when there is no usable one."""

        return parse_stop_distance(self.stop_text_for(pair))

    def protective_stop_view(self) -> dict[str, str]:
        """The configured distances, keys normalised, values as written."""

        view: dict[str, str] = {}
        for key, value in dict(self.protective_stops or {}).items():
            pair = normalise_pair_key(key)
            if pair:
                view[pair] = str(value or "").strip()
        return view

    def mode_label(self) -> str:
        if self.mode_value == MT5ExecutionMode.DISABLED.value:
            return "disabled"
        if self.mode_value == MT5ExecutionMode.OBSERVE_ONLY.value:
            return "observe_only"
        return "trading_enabled"

    def assert_trading_allowed(self) -> None:
        """Fail closed unless the operator explicitly enabled trading."""

        if not self.validate().valid:
            raise ValueError("mt5_settings_invalid")
        if not self.trading_enabled:
            raise MT5TradingNotAuthorised(
                "mt5_trading_not_enabled",
                "Local MT5 mode is " + self.mode_label() + "; orders are refused.",
            )


class MT5SettingsError(Exception):
    def __init__(self, reason: str, detail: str = ""):
        self.reason = reason
        self.detail = detail
        super().__init__(reason if not detail else f"{reason}: {detail}")


class MT5TradingNotAuthorised(MT5SettingsError):
    """Local configuration does not authorise order submission."""

    def __init__(self, reason: str, detail: str = ""):
        self.detail = detail
        super().__init__(reason)


@dataclass(frozen=True)
class MT5PasswordStore:
    """Enter / replace / clear the terminal password; report only presence.

    * the value is written to the OS vault and nowhere else;
    * there is no plaintext fallback -- an unavailable vault refuses the save
      *before* the value is even validated;
    * nothing here returns the value: :meth:`status` reports a boolean and a
      fixed mask, and there is deliberately no accessor that reads it back for
      use.  The terminal login itself is performed by the MT5 terminal from its
      own saved credentials; this setting records the operator's intent and
      proves a credential is present.
    """

    settings: MT5Settings
    vault: CredentialVault
    audit_log: Any | None = None

    # -- storage health -------------------------------------------------

    def secure_storage(self) -> dict[str, Any]:
        try:
            available = bool(self.vault.available())
        except Exception:
            available = False
        try:
            described = dict(self.vault.describe())
        except Exception:
            described = {"backend": "unavailable", "secure": True}
        described["available"] = available
        described["plaintext_fallback"] = False
        described.setdefault("service", MT5_VAULT_SERVICE)
        if not available:
            described.setdefault(
                "reason",
                "No operating-system credential vault is available, so the MT5 "
                "password cannot be saved securely.  Install and unlock a "
                "platform keyring backend and try again; this repository will "
                "not store the password anywhere else.",
            )
        return described

    def configured(self) -> bool:
        if not self.secure_storage()["available"]:
            return False
        try:
            return bool(self.vault.read(self.settings.credential_entry()))
        except Exception:
            return False

    def status(self) -> dict[str, Any]:
        configured = self.configured()
        return {
            "setting": "mt5_password",
            "purpose": MT5_CREDENTIAL_PURPOSE,
            "broker_credential": True,
            "trading_credential": True,
            "configured": configured,
            "display": MASKED_DISPLAY if configured else "",
            "placeholder": "Configured" if configured else "Not configured",
            "entry": self.settings.credential_entry(),
            "account_login": int(self.settings.account_login),
            "server": self.settings.server,
            "storage": "os_credential_vault",
            "plaintext_fallback": False,
            "secure_storage": self.secure_storage(),
            "note": (
                "Stored only in the operating-system credential vault, bound to "
                "this server and login.  It is never written to Git, profile "
                "files, .env, SQLite, logs, reports, or API responses."
            ),
        }

    # -- writes ---------------------------------------------------------

    def _audit(self, event_type: str, **fields: Any) -> None:
        """Record the action only; field names are allow-listed."""

        if self.audit_log is None:
            return
        payload: dict[str, Any] = {
            "event_type": event_type,
            "setting": "mt5_password",
            "purpose": MT5_CREDENTIAL_PURPOSE,
            "broker_credential": True,
            "server": self.settings.server,
            "account_login": int(self.settings.account_login),
            "storage": "os_credential_vault",
            "live": False,
        }
        for key in ("action", "reason", "configured", "backend"):
            if key in fields:
                payload[key] = fields[key]
        self.audit_log.record(payload)

    def _require_storage(self, *, action: str) -> None:
        storage = self.secure_storage()
        if storage["available"]:
            return
        self._audit(
            "MT5_PASSWORD_REFUSED",
            action=action,
            reason="secure_storage_unavailable",
            configured=self.configured(),
        )
        raise SecureStorageUnavailable(
            "secure_storage_unavailable", str(storage.get("reason", ""))
        )

    def save(self, password: Any) -> dict[str, Any]:
        self._require_storage(action="save")
        if not isinstance(password, str):
            raise MT5SettingsError("invalid_password_type")
        if not password:
            raise MT5SettingsError("empty_password")
        if len(password) > MAX_PASSWORD_LENGTH:
            raise MT5SettingsError("password_too_long")
        if any(ord(character) < 32 for character in password):
            raise MT5SettingsError("password_contains_control_characters")
        replacing = self.configured()
        entry = self.settings.credential_entry()
        value = password
        try:
            self.vault.write(entry, value)
        finally:
            del value
        self._audit(
            "MT5_PASSWORD_SAVED",
            action="replace" if replacing else "enter",
            configured=True,
            backend=str(self.secure_storage().get("backend", "")),
        )
        return self.status()

    def clear(self) -> dict[str, Any]:
        self._require_storage(action="clear")
        self.vault.erase(self.settings.credential_entry())
        self._audit(
            "MT5_PASSWORD_CLEARED",
            action="clear",
            configured=False,
            backend=str(self.secure_storage().get("backend", "")),
        )
        return self.status()

    def secret_for_login(self) -> str:
        """Read the password for one explicit, audited login attempt.

        This is the only accessor, and it is called only by the connection
        routine after the terminal has refused to reuse an existing session.
        The value is never returned to an interface, never included in an audit
        payload, and never held beyond the ``initialize`` call that consumes it.
        """

        if not self.configured():
            self._audit("MT5_PASSWORD_USE_BLOCKED", action="login", reason="not_configured")
            raise MT5SettingsError("mt5_password_not_configured")
        value = self.vault.read(self.settings.credential_entry())
        if not value:
            self._audit("MT5_PASSWORD_USE_BLOCKED", action="login", reason="vault_read_empty")
            raise MT5SettingsError("mt5_password_not_configured")
        self._audit("MT5_PASSWORD_USED_FOR_LOGIN", action="login", configured=True)
        return value


@dataclass(frozen=True)
class MT5SettingsSurface:
    """The desktop settings surface for MT5 (requires the operator bearer)."""

    settings: MT5Settings
    password_store: MT5PasswordStore
    api_bearer_token: str = ""

    def _authorize(self, presented: Any) -> None:
        import hmac

        expected = str(self.api_bearer_token or "")
        if not expected:
            raise MT5SettingsError("settings_locked_no_operator_token")
        if not isinstance(presented, str) or not presented:
            raise MT5SettingsError("settings_locked_missing_token")
        if not hmac.compare_digest(presented, expected):
            raise MT5SettingsError("settings_locked_invalid_token")

    def status(self, *, presented_token: Any) -> dict[str, Any]:
        self._authorize(presented_token)
        return {
            "mt5": self.settings.audit_view(),
            "password": self.password_store.status(),
            "token_required_by_broker": False,
            "token_note": (
                "MetaTrader 5 authenticates with the terminal login "
                "(server, login, password).  No separate API token exists, so "
                "this integration defines no token field."
            ),
            "valid": self.settings.validate().valid,
        }

    def set_password(self, password: Any, *, presented_token: Any) -> dict[str, Any]:
        self._authorize(presented_token)
        return self.password_store.save(password)

    def clear_password(self, *, presented_token: Any) -> dict[str, Any]:
        self._authorize(presented_token)
        return self.password_store.clear()


#: Local, non-secret settings document.  Deliberately small and explicit: it
#: names the terminal, the account, and the local authorisation mode.  It must
#: never contain the password, and a document that does is refused.
SETTINGS_FILE_NAME = "mt5_settings.local.json"

#: Keys that would mean a secret leaked into a plain-text file.
_SECRET_KEY_FRAGMENTS = (
    "password",
    "passwd",
    "secret",
    "token",
    "apikey",
    "api_key",
    "credential",
)

_ALLOWED_KEYS = {
    "terminal_path",
    "server",
    "account_login",
    "mode",
    "symbol_suffix",
    "use_existing_session",
    "declared_account_currency",
    "declared_leverage",
    "magic",
    "deviation_points",
    "max_price_drift_points",
    "protective_stops",
    "protective_stop_points",
    "timeout_ms",
}


class MT5SettingsFileError(MT5SettingsError):
    """The local settings document is missing, malformed, or unsafe."""


def mt5_settings_document(settings: MT5Settings) -> dict[str, Any]:
    """The document a user may write and commit to their own machine."""

    return {
        "terminal_path": settings.terminal_path,
        "server": settings.server,
        "account_login": settings.account_login,
        "mode": settings.mode_value,
        "symbol_suffix": settings.symbol_suffix,
        "use_existing_session": settings.use_existing_session,
        "declared_account_currency": settings.declared_account_currency,
        "declared_leverage": settings.declared_leverage,
        "magic": settings.magic,
        "deviation_points": settings.deviation_points,
        "max_price_drift_points": settings.max_price_drift_points,
        "protective_stops": dict(settings.protective_stop_view()),
        "timeout_ms": settings.timeout_ms,
        "note": (
            "No secret belongs in this file.  The MT5 password is stored only in "
            "the operating-system credential vault via "
            "`axiom-forex mt5 --mt5-action set-password`.  "
            "`protective_stops` maps a pair to a distance *with its unit*, e.g. "
            '{"EUR/USD": "20 pips"}; a live entry for a pair with no usable distance '
            "is refused rather than sent unprotected.  The older single "
            "`protective_stop_points` value is accepted but not used for entries."
        ),
    }


def _stops_from_document(document: Mapping[str, Any]) -> dict[str, str]:
    """The per-symbol distances in a document, exactly as written.

    A malformed section is refused rather than repaired: a stop this build cannot
    read is a stop that cannot be attached.
    """

    raw = document.get("protective_stops")
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise MT5SettingsFileError("mt5_settings_file_invalid", "protective_stops")
    return {str(key): str(value if value is not None else "") for key, value in raw.items()}


def check_document_keys(document: Mapping[str, Any]) -> None:
    """Refuse a document that carries a secret or a key this build does not know."""

    for key in document:
        lowered = str(key).lower()
        if any(fragment in lowered for fragment in _SECRET_KEY_FRAGMENTS):
            raise MT5SettingsFileError(
                "mt5_settings_file_contains_secret",
                f"key={key}; remove it and use the OS credential vault",
            )
        if lowered not in _ALLOWED_KEYS and lowered != "note":
            raise MT5SettingsFileError("mt5_settings_file_unknown_key", str(key))


def settings_from_document(document: Mapping[str, Any]) -> MT5Settings:
    """Build and validate settings from a document, or raise ``MT5SettingsFileError``."""

    check_document_keys(document)
    try:
        settings = MT5Settings(
            terminal_path=str(document.get("terminal_path", "")),
            server=str(document.get("server", "")),
            account_login=int(document.get("account_login", 0) or 0),
            mode=MT5ExecutionMode(str(document.get("mode", "OBSERVE_ONLY")).upper()),
            symbol_suffix=str(document.get("symbol_suffix", "") or ""),
            use_existing_session=bool(document.get("use_existing_session", True)),
            declared_account_currency=(
                str(document["declared_account_currency"]).upper()
                if document.get("declared_account_currency")
                else None
            ),
            declared_leverage=(
                int(document["declared_leverage"])
                if document.get("declared_leverage") is not None
                else None
            ),
            magic=int(document.get("magic", 26081001) or 0),
            deviation_points=int(document.get("deviation_points", 10) or 0),
            max_price_drift_points=int(
                document.get("max_price_drift_points", 20) or 0
            ),
            protective_stop_points=int(
                document.get("protective_stop_points", 0) or 0
            ),
            protective_stops=_stops_from_document(document),
            timeout_ms=int(document.get("timeout_ms", 60_000) or 0),
        )
    except (TypeError, ValueError) as exc:
        raise MT5SettingsFileError("mt5_settings_file_invalid", type(exc).__name__) from exc
    result = settings.validate()
    if not result.valid:
        raise MT5SettingsFileError("mt5_settings_invalid", result.reason)
    return settings


def write_protective_stop(
    path: str | Path, *, pair: Any, distance: Any
) -> dict[str, Any]:
    """Set one per-symbol stop distance in the local document.

    Only ``protective_stops`` is touched: every other key keeps its value and its
    position, and the result is re-read through the same validator the launcher
    uses before a single byte replaces the file.  Nothing is written when the
    document is missing, carries a secret, or would not load afterwards.
    """

    location = Path(path)
    if not location.exists():
        raise MT5SettingsFileError("mt5_settings_file_missing", str(location.name))
    try:
        document = json.loads(location.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise MT5SettingsFileError("mt5_settings_file_unreadable", type(exc).__name__) from exc
    if not isinstance(document, dict):
        raise MT5SettingsFileError("mt5_settings_file_not_an_object")
    normalised = normalise_pair_key(pair)
    if not normalised:
        raise MT5SettingsFileError("invalid_protective_stop_key", str(pair))
    parsed = parse_stop_distance(distance)
    if parsed is None:
        raise MT5SettingsFileError("invalid_protective_stop_value", f"{pair}={distance}")
    text = format_stop_distance(*parsed)

    existing = dict(document.get("protective_stops") or {})
    # Drop any other spelling of the same pair so the file cannot hold two.
    existing = {
        key: value
        for key, value in existing.items()
        if normalise_pair_key(key) != normalised
    }
    existing[normalised] = text
    candidate = dict(document)
    candidate["protective_stops"] = existing
    # Validate before writing: an unreadable file is worse than an unwritten one.
    settings_from_document(candidate)

    # Written beside the document and renamed over it, so a reader never sees half
    # a file.  No shell-out, no environment, no network: just the one path.
    handle = tempfile.NamedTemporaryFile(
        mode="w",
        prefix=location.name + ".",
        suffix=".tmp",
        dir=str(location.parent),
        delete=False,
        encoding="utf-8",
    )
    temporary = Path(handle.name)
    try:
        with handle as stream:
            json.dump(candidate, stream, indent=2)
            stream.write("\n")
        temporary.replace(location)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise
    return {"pair": normalised, "distance": text, "document": candidate}


def load_settings_file(path: str | Path) -> MT5Settings | None:
    """Read the local document.  ``None`` when the file does not exist."""

    location = Path(path)
    if not location.exists():
        return None
    try:
        document = json.loads(location.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise MT5SettingsFileError("mt5_settings_file_unreadable", type(exc).__name__) from exc
    if not isinstance(document, dict):
        raise MT5SettingsFileError("mt5_settings_file_not_an_object")
    return settings_from_document(document)


def make_password_store(
    settings: MT5Settings, *, vault: CredentialVault | None = None, audit_log: Any = None
) -> MT5PasswordStore:
    return MT5PasswordStore(
        settings=settings, vault=vault if vault is not None else default_vault(), audit_log=audit_log
    )



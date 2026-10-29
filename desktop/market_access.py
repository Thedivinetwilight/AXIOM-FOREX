"""Market Access Token: a vault-backed setting that never leaves the vault.

This is the credential for a *future, explicitly selected market-data provider*.
It is not a broker trading credential, it is not a provider choice, and nothing
in this build sends it anywhere: no request, no retry, no "test" button.

Rules this module enforces:

* the value lives only in the operating system's credential vault, reached
  through :class:`CredentialVault`;
* there is **no plaintext fallback** -- if the vault is unavailable, saving is
  refused with an explanation rather than written to a file, the database, the
  environment, or memory that outlives the call;
* the value is never returned: :meth:`MarketAccessTokenStore.status` reports
  only *whether* one is configured, and there is deliberately no accessor that
  hands the secret back;
* the value never reaches an audit event, a log line, a report, or an API
  response -- audit records carry an action, a boolean, and a refusal reason;
* the setting stays disconnected from data-provider traffic until a provider is
  explicitly selected and configured, which this milestone does not do.

The store is a plain object with an injected vault so tests can drive a fake
credential vault (available, unavailable, failing) without touching the real
operating system keychain.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, runtime_checkable

from app.audit.log import AuditLog

# The vault is shared with every other credential-backed setting (the MT5
# password, for example); this module owns none of that storage.
from axiom_forex.config.vault import (  # noqa: F401  (re-exported)
    CredentialVault,
    KeyringCredentialVault,
    SecureStorageUnavailable,
    default_vault,
)

#: Namespaced entry name inside the vault.  Changing it orphans an old token
#: instead of silently reusing it for a different purpose.
MARKET_ACCESS_TOKEN_ENTRY = "axiom_forex.market_data.market_access_token"

#: The only credential this setting is allowed to describe.
CREDENTIAL_PURPOSE = "MARKET_DATA_READ_ONLY"

#: Shown by the interface when a token exists.  A fixed mask: it is *not*
#: derived from the stored value in any way.
MASKED_DISPLAY = "********"

#: Bounds on what can be stored.  Both are policy, not detection: the token's
#: actual format belongs to whichever provider is eventually selected.
MIN_TOKEN_LENGTH = 8
MAX_TOKEN_LENGTH = 4096


class MarketAccessTokenError(Exception):
    """Raised for a refused save/clear (invalid input, locked settings)."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


class SettingsAccessDenied(MarketAccessTokenError):
    """Raised when the caller has not unlocked the settings surface."""


def _validate_token(token: Any) -> str:
    """Reject anything that is not a plausible single-line secret.

    The format belongs to whichever provider is chosen later, so this checks
    only structural safety: a string, trimmed, of bounded length, with no
    whitespace or control characters (which would corrupt a header and make the
    value untraceable in a vault UI).
    """

    if not isinstance(token, str):
        raise MarketAccessTokenError("invalid_token_type")
    value = token.strip()
    if not value:
        raise MarketAccessTokenError("empty_token")
    if value != token:
        raise MarketAccessTokenError("token_has_surrounding_whitespace")
    if any(character.isspace() for character in value):
        raise MarketAccessTokenError("token_contains_whitespace")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise MarketAccessTokenError("token_contains_control_characters")
    if len(value) < MIN_TOKEN_LENGTH:
        raise MarketAccessTokenError("token_too_short")
    if len(value) > MAX_TOKEN_LENGTH:
        raise MarketAccessTokenError("token_too_long")
    return value


@dataclass(frozen=True)
class MarketAccessTokenStore:
    """Save / replace / clear the market access token; report only its presence."""

    vault: CredentialVault
    audit_log: AuditLog | None = None
    entry: str = MARKET_ACCESS_TOKEN_ENTRY
    #: A market-data provider must be *explicitly selected* before this setting
    #: is allowed to influence anything.  Nothing in this build selects one.
    provider_selected: bool = False
    provider_id: str = ""
    _state: dict[str, Any] = field(
        default_factory=dict, repr=False, compare=False, hash=False
    )

    # -- reporting ------------------------------------------------------

    def secure_storage(self) -> dict[str, Any]:
        """Describe the vault without disclosing anything about its contents."""

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
        if not available:
            described.setdefault(
                "reason",
                "No operating-system credential vault is available, so the "
                "token cannot be saved securely.  Install and unlock a platform "
                "keyring backend (for example a Secret Service provider on "
                "Linux) and try again.  This repository will not store the "
                "token anywhere else.",
            )
        return described

    def configured(self) -> bool:
        """True only when the vault holds an entry.  Never reveals its value."""

        if not self.secure_storage()["available"]:
            return False
        try:
            stored = self.vault.read(self.entry)
        except Exception:
            return False
        return bool(stored)

    def status(self) -> dict[str, Any]:
        configured = self.configured()
        return {
            "setting": "market_access_token",
            "purpose": CREDENTIAL_PURPOSE,
            "broker_trading_use": False,
            "configured": configured,
            "display": MASKED_DISPLAY if configured else "",
            "placeholder": "Not configured" if not configured else "Configured",
            "secure_storage": self.secure_storage(),
            "storage": "os_credential_vault",
            "plaintext_fallback": False,
            "provider_selected": self.provider_selected,
            "provider_id": self.provider_id if self.provider_selected else "",
            "used_for_requests": False,
            "note": (
                "Stored only in the operating-system credential vault.  It is not "
                "a broker trading credential, and no data-provider request uses it "
                "until a provider is explicitly selected and configured."
            ),
        }

    # -- writes ---------------------------------------------------------

    def _audit(self, event_type: str, **fields: Any) -> None:
        """Record the *action*, never the secret.

        Only allow-listed, non-secret fields are copied through, so a future
        caller cannot leak a value by passing it here; the audit sanitizer
        redacts credential-shaped keys as a second line of defence.
        """

        if self.audit_log is None:
            return
        payload: dict[str, Any] = {
            "event_type": event_type,
            "setting": "market_access_token",
            "purpose": CREDENTIAL_PURPOSE,
            "broker_trading_use": False,
            "used_for_requests": False,
            "live": False,
        }
        for key in ("action", "reason", "configured", "storage", "backend"):
            if key in fields:
                payload[key] = fields[key]
        self.audit_log.record(payload)

    def _require_storage(self) -> None:
        storage = self.secure_storage()
        if storage["available"]:
            return
        self._audit(
            "MARKET_ACCESS_TOKEN_SAVE_REFUSED",
            action="save",
            reason="secure_storage_unavailable",
            configured=self.configured(),
            storage="os_credential_vault",
        )
        raise SecureStorageUnavailable(
            "secure_storage_unavailable",
            str(storage.get("reason", "no usable credential vault")),
        )

    def save(self, token: Any) -> dict[str, Any]:
        """Enter or replace the token.  The value is written once, then dropped."""

        # Fail closed *before* touching the value, so a missing vault cannot
        # leave the secret in a local variable that a fallback might persist.
        self._require_storage()
        value = _validate_token(token)
        replacing = self.configured()
        try:
            self.vault.write(self.entry, value)
        finally:
            del value
        self._audit(
            "MARKET_ACCESS_TOKEN_SAVED",
            action="replace" if replacing else "enter",
            configured=True,
            storage="os_credential_vault",
            backend=str(self.secure_storage().get("backend", "")),
        )
        return self.status()

    def clear(self) -> dict[str, Any]:
        """Remove the token.  Clearing an absent token succeeds idempotently."""

        self._require_storage()
        existed = self.configured()
        self.vault.erase(self.entry)
        self._audit(
            "MARKET_ACCESS_TOKEN_CLEARED",
            action="clear",
            configured=False,
            storage="os_credential_vault",
            backend=str(self.secure_storage().get("backend", "")),
        )
        self._state["last_clear_existed"] = existed
        return self.status()

    # -- explicit non-capabilities --------------------------------------

    def for_provider_request(self) -> str:
        """Refuse to hand the token to a request.

        There is no provider in this milestone, so there is nothing that may
        legitimately receive the value.  Keeping the refusal explicit (rather
        than simply omitting an accessor) means an accidental future call site
        fails loudly instead of quietly building a request.
        """

        reason = (
            "provider_not_selected"
            if not self.provider_selected
            else "market_data_requests_disabled"
        )
        self._audit(
            "MARKET_ACCESS_TOKEN_USE_BLOCKED",
            action="request",
            reason=reason,
            configured=self.configured(),
            storage="os_credential_vault",
        )
        raise MarketAccessTokenError(reason)


@dataclass(frozen=True)
class MarketAccessSettings:
    """The desktop settings surface for the market access token.

    Every operation requires the operator's local API bearer token, matching the
    access control on the read-only HTTP surface: the desktop application can
    only change this setting when the operator has unlocked it.  A refused
    unlock touches neither the vault nor the audit trail's content.
    """

    store: MarketAccessTokenStore
    api_bearer_token: str = ""

    def _authorize(self, presented: Any) -> None:
        expected = str(self.api_bearer_token or "")
        if not expected:
            raise SettingsAccessDenied("settings_locked_no_operator_token")
        if not isinstance(presented, str) or not presented:
            raise SettingsAccessDenied("settings_locked_missing_token")
        # Constant-time comparison: the bearer is a secret too.
        import hmac

        if not hmac.compare_digest(presented, expected):
            raise SettingsAccessDenied("settings_locked_invalid_token")

    def status(self, *, presented_token: Any) -> dict[str, Any]:
        self._authorize(presented_token)
        return self.store.status()

    def set_token(self, token: Any, *, presented_token: Any) -> dict[str, Any]:
        self._authorize(presented_token)
        return self.store.save(token)

    def clear_token(self, *, presented_token: Any) -> dict[str, Any]:
        self._authorize(presented_token)
        return self.store.clear()



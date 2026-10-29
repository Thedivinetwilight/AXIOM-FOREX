"""Operating-system credential vault: the only place a secret may be stored.

This module is deliberately tiny and dependency-light.  It defines the narrow
interface the rest of the repository needs (``describe``/``available``/``read``/
``write``/``erase``) and one real implementation that speaks to the platform
store through ``keyring``:

* Windows -- Credential Manager
* macOS -- Keychain
* Linux -- Secret Service / KWallet

There is **no** plaintext fallback anywhere in this repository: no file, no
``.env``, no profile JSON, no SQLite row, no log line.  When no usable backend
exists, callers get :class:`SecureStorageUnavailable` with a reason they can
show the operator, and the save is refused.

``read`` exists for replace/verify flows that must prove the vault is the source
of truth.  No code path in this repository uses it to obtain a secret for
transport.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol, runtime_checkable


class SecureStorageUnavailable(Exception):
    """Raised when no OS credential vault is usable.

    Carries a human-readable reason so an interface can explain *why* the save
    was refused instead of failing silently.
    """

    def __init__(self, reason: str, detail: str = ""):
        self.reason = reason
        self.detail = detail
        super().__init__(reason if not detail else f"{reason}: {detail}")


@runtime_checkable
class CredentialVault(Protocol):
    """The operating-system credential store, as this repository needs it."""

    def describe(self) -> Mapping[str, Any]: ...

    def available(self) -> bool: ...

    def read(self, entry: str) -> str | None: ...

    def write(self, entry: str, value: str) -> None: ...

    def erase(self, entry: str) -> bool: ...


@dataclass(frozen=True)
class KeyringCredentialVault:
    """OS credential vault through ``keyring``; no file or env fallback."""

    service_name: str = "AXIOM-FOREX"

    def _keyring(self) -> Any | None:
        try:  # optional dependency: absent in a bare checkout
            import keyring
        except Exception:
            return None
        try:
            backend = keyring.get_keyring()
        except Exception:
            return None
        name = f"{type(backend).__module__}.{type(backend).__name__}".lower()
        # ``keyring`` ships fail/null backends that raise on use; treating them
        # as available would turn a refusal into a crash mid-save.
        if "fail" in name or "null" in name:
            return None
        return keyring

    def available(self) -> bool:
        return self._keyring() is not None

    def describe(self) -> Mapping[str, Any]:
        return {
            "backend": "os_keyring" if self.available() else "unavailable",
            "secure": True,
            "plaintext_fallback": False,
            "service": self.service_name,
            "note": (
                "Stored by the operating system credential vault, never by this "
                "repository."
            ),
        }

    def read(self, entry: str) -> str | None:
        keyring = self._keyring()
        if keyring is None:
            return None
        try:
            return keyring.get_password(self.service_name, entry)
        except Exception:
            return None

    def write(self, entry: str, value: str) -> None:
        keyring = self._keyring()
        if keyring is None:
            raise SecureStorageUnavailable(
                "secure_storage_unavailable",
                "no usable operating-system credential vault backend",
            )
        try:
            keyring.set_password(self.service_name, entry, value)
        except Exception as exc:  # pragma: no cover - depends on the OS
            # The message may contain a backend path or account name; keep the
            # exception type only.
            raise SecureStorageUnavailable(
                "secure_storage_write_failed", type(exc).__name__
            ) from exc

    def erase(self, entry: str) -> bool:
        keyring = self._keyring()
        if keyring is None:
            raise SecureStorageUnavailable(
                "secure_storage_unavailable",
                "no usable operating-system credential vault backend",
            )
        try:
            keyring.delete_password(self.service_name, entry)
            return True
        except Exception as exc:  # pragma: no cover - depends on the OS
            # Most backends raise when the entry does not exist; clearing an
            # absent secret is already the desired end state.
            if "notfound" in type(exc).__name__.lower() or "password" in str(exc).lower():
                return False
            raise SecureStorageUnavailable(
                "secure_storage_erase_failed", type(exc).__name__
            ) from exc


def default_vault() -> CredentialVault:
    """The vault this installation uses when nothing else is injected."""

    return KeyringCredentialVault()

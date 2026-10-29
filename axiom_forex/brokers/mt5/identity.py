"""Non-reversible account identity, shared by the adapter and the workstation.

Account logins are identifiers, not secrets, but they still identify a real
person's brokerage account.  Every surface outside the terminal therefore shows a
stable *fingerprint* instead: twelve hex characters derived from the login, which
lets an operator correlate two screens without ever printing the account number.
"""

from __future__ import annotations

from hashlib import sha256

PREFIX = "axiom-mt5-account"
FINGERPRINT_LENGTH = 12


def account_fingerprint(login: object) -> str:
    """``login -> 12 hex chars``; empty when there is no usable login."""

    try:
        value = int(login)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return ""
    if value <= 0:
        return ""
    digest = sha256(f"{PREFIX}|{value}".encode("utf-8")).hexdigest()
    return digest[:FINGERPRINT_LENGTH]

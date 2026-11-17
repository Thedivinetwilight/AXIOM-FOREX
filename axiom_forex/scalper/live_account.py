"""Which account is behind the terminal — proved, not assumed.

AX-SCALPER's LIVE lane is for one thing only: trading a **real-money** account
that the operator named in advance.  Everything in this repository that can send
an order already checks *that* the adapter is armed and *that* the armed account
is the selected one; this module adds the question those checks do not ask:

    is the selected account the real account this run was configured for?

The answer comes from the terminal's own account record (``account_info()``,
surfaced by :class:`~axiom_forex.brokers.mt5.adapter.MT5TradingAdapter.status`),
never from a configuration file's hope.  A demo account, a contest account, an
account the terminal cannot classify, or any of the identity fields disagreeing
is a refusal with one named reason per disagreement.

There is no "assume real if it looks real" branch and no way to pass a flag that
turns the check off: the only way to satisfy it is for the terminal to say REAL
and for the server/login to be the ones the operator declared.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

# -- the terminal's own account modes ---------------------------------------

REAL = "REAL"
DEMO = "DEMO"
CONTEST = "CONTEST"
UNCONFIRMED = "UNCONFIRMED"

# -- refusal reasons --------------------------------------------------------

NOT_CONNECTED = "mt5_not_connected"
MODE_UNKNOWN = "account_mode_unknown"
MODE_NOT_REAL = "real_account_required"
MODE_NOT_CONFIRMED = "account_mode_not_confirmed"
SERVER_MISMATCH = "account_server_mismatch"
LOGIN_MISMATCH = "account_login_mismatch"
FINGERPRINT_MISMATCH = "account_fingerprint_mismatch"
CURRENCY_MISMATCH = "account_currency_mismatch"
NOT_DECLARED = "expected_account_not_declared"


def _text(value: Any) -> str:
    return str(value if value is not None else "").strip()


@dataclass(frozen=True)
class ExpectedAccount:
    """The real account this run is allowed to trade, declared by the operator.

    All four fields are required. The fingerprint must derive from the declared
    login, so it cannot silently stand in for a different login. The adapter
    redacts the terminal login; its independently reported fingerprint binds it.
    """

    server: str
    login: int = 0
    currency: str = ""
    fingerprint: str = ""

    def validate(self) -> tuple[str, ...]:
        problems: list[str] = []
        if not _text(self.server):
            problems.append("account.server")
        try:
            login = int(self.login)
        except (TypeError, ValueError):
            login = 0
        if isinstance(self.login, bool) or login <= 0 or _text(self.login) != str(login):
            problems.append("account.login")
        if not _text(self.currency):
            problems.append("account.currency")
        fingerprint = _text(self.fingerprint)
        if not fingerprint or fingerprint != _expected_fingerprint(login):
            problems.append("account.fingerprint")
        return tuple(problems)

    @property
    def declared(self) -> bool:
        return not self.validate()

    def audit_view(self) -> dict[str, Any]:
        # A login is not a secret, but a fingerprint is the portable identity this
        # system reports everywhere else, so both are shown as they are stored.
        return {
            "server": _text(self.server),
            "login": int(self.login) if _text(self.login) else 0,
            "currency": _text(self.currency),
            "fingerprint": _text(self.fingerprint),
        }


@dataclass(frozen=True)
class AccountVerdict:
    """What the terminal says, and whether it is the account this run may trade."""

    ok: bool
    mode: str
    reasons: tuple[str, ...] = ()
    detail: str = ""
    server: str = ""
    login: int = 0
    fingerprint: str = ""
    currency: str = ""

    def audit_view(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "mode": self.mode,
            "reasons": list(self.reasons),
            "detail": self.detail,
            "server": self.server,
            "login": self.login,
            "fingerprint": self.fingerprint,
            "currency": self.currency,
        }

    def as_dict(self) -> dict[str, Any]:
        return self.audit_view()


def _expected_fingerprint(login: int) -> str:
    """The one-way identity the broker adapter would report for ``login``."""

    try:
        from ..brokers.mt5.identity import account_fingerprint

        return account_fingerprint(int(login))
    except Exception:
        return ""


def _field(status: Mapping[str, Any], key: str) -> Any:
    if key in status:
        return status.get(key)
    redacted = status.get("redacted_account")
    if isinstance(redacted, Mapping):
        return redacted.get(key)
    return None


def verify_real_account(
    status: Mapping[str, Any] | None,
    expected: ExpectedAccount | None,
) -> AccountVerdict:
    """Judge one adapter status against one declared real account.

    ``status`` is ``MT5TradingAdapter.status()``.  Nothing is inferred: a field
    the terminal did not report is a refusal, not a pass.
    """

    if not isinstance(status, Mapping):
        return AccountVerdict(
            ok=False,
            mode=UNCONFIRMED,
            reasons=(NOT_CONNECTED,),
            detail="the adapter did not report a status",
        )

    mode = _text(status.get("account_mode")) or UNCONFIRMED
    confirmed = bool(status.get("account_mode_confirmed"))
    connected = bool(status.get("connected"))
    server = _text(_field(status, "server"))
    fingerprint = _text(_field(status, "account_fingerprint"))
    currency = _text(_field(status, "currency"))
    redacted = status.get("redacted_account")
    login = 0
    if isinstance(redacted, Mapping):
        try:
            login = int(redacted.get("login") or 0)
        except (TypeError, ValueError):
            login = 0

    reasons: list[str] = []
    details: list[str] = []

    if not connected:
        reasons.append(NOT_CONNECTED)
        details.append("the terminal is not connected")
    if not confirmed:
        reasons.append(MODE_NOT_CONFIRMED)
        details.append(f"the terminal did not confirm the account type (reported {mode})")
    if mode != REAL:
        # DEMO, CONTEST and anything unclassifiable land here.  A demo account can
        # never satisfy this check, whatever else it matches.
        reasons.append(MODE_NOT_REAL if mode in {DEMO, CONTEST} else MODE_UNKNOWN)
        details.append(
            f"the connected account is {mode}, and this lane trades a real-money "
            "account only"
        )

    if expected is None:
        reasons.append(NOT_DECLARED)
        details.append(
            "no expected account was declared, so there is nothing to verify the "
            "terminal's answer against"
        )
    else:
        problems = expected.validate()
        if problems:
            reasons.append(NOT_DECLARED)
            details.append("the declared account is incomplete: " + ", ".join(problems))
        else:
            if server != _text(expected.server):
                reasons.append(SERVER_MISMATCH)
                details.append(
                    f"the terminal reports server {server!r}, the run was configured for "
                    f"{expected.server!r}"
                )
            # The terminal login is redacted; compare the independently reported
            # fingerprint to the one derived from the declared login as well as
            # to the explicitly declared fingerprint.
            if fingerprint != _expected_fingerprint(int(expected.login)):
                reasons.append(LOGIN_MISMATCH)
                details.append("the terminal's login fingerprint differs from the declared login")
            if fingerprint != _text(expected.fingerprint):
                reasons.append(FINGERPRINT_MISMATCH)
                details.append("the terminal's fingerprint differs from the declared fingerprint")
            if currency != _text(expected.currency):
                reasons.append(CURRENCY_MISMATCH)
                details.append(
                    f"the terminal reports account currency {currency!r}, the run was "
                    f"configured for {expected.currency!r}"
                )

    return AccountVerdict(
        ok=not reasons,
        mode=mode,
        reasons=tuple(dict.fromkeys(reasons)),
        detail="; ".join(dict.fromkeys(details)),
        server=server,
        login=login,
        fingerprint=fingerprint,
        currency=currency,
    )


__all__ = [
    "CONTEST",
    "DEMO",
    "ExpectedAccount",
    "AccountVerdict",
    "FINGERPRINT_MISMATCH",
    "LOGIN_MISMATCH",
    "MODE_NOT_CONFIRMED",
    "MODE_NOT_REAL",
    "MODE_UNKNOWN",
    "NOT_CONNECTED",
    "NOT_DECLARED",
    "REAL",
    "SERVER_MISMATCH",
    "UNCONFIRMED",
    "verify_real_account",
]

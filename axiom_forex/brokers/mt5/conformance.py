"""What must be verified before AXIOM-FOREX may trade this MT5 account.

These are *requirements*, not claims.  Each one names the evidence that has to
exist before the corresponding behaviour is trusted; the offline suite proves
the adapter's own behaviour, and the items marked ``(broker)`` can only be
confirmed against the operator's own terminal and account.
"""

from __future__ import annotations

MT5_CONFORMANCE_REQUIREMENTS: tuple[str, ...] = (
    "official MetaTrader5 Python package imported from the installed terminal, never a bridge",
    "terminal path, server, and login declared locally; password only in the OS credential vault",
    "no order sent on terminal connect, application start, or settings change",
    "explicit local mode selection (OBSERVE_ONLY vs TRADING_ENABLED) plus in-process arming naming the account",
    "broker symbol discovery with suffix/alias resolution and no fuzzy guessing",
    "catalog instrument terms cross-checked against symbol_info before any order (broker)",
    "bid/ask ticks and two-sided bars from ticks; single-sided rate bars labelled as such",
    "account currency, leverage, margin mode, and margin requirement read from the terminal (broker)",
    "netting vs hedging position accounting verified for the account (broker)",
    "order_check before order_send; a failed check sends nothing",
    "every retcode classified; unknown codes treated as unknown outcomes",
    "partial fills recorded with the unfilled remainder, never rounded away",
    "unknown outcomes persisted, trading disarmed, and reconciled from order/deal history before any resend",
    "restart recovery resolves in-doubt orders from broker evidence; absence of evidence is bounded by a query window",
    "open positions and deal history reconciled against the AXIOM ledger, with differences reported rather than corrected",
    "strategy lifecycle APPROVED+active and an allowed hard-risk gate decision required for every live order",
    "daily break, weekend, holiday, and DST handling taken from the broker calendar, not assumed 24/5",
    "swap/commission/rollover terms captured per broker before costs are trusted (broker)",
    "independent audit trail for every connection, arming, submission, and recovery event",
)

#: Items that cannot be verified offline; they need the operator's MT5 account.
BROKER_DEPENDENT_REQUIREMENTS: tuple[str, ...] = (
    "catalog instrument terms cross-checked against symbol_info before any order (broker)",
    "account currency, leverage, margin mode, and margin requirement read from the terminal (broker)",
    "netting vs hedging position accounting verified for the account (broker)",
    "swap/commission/rollover terms captured per broker before costs are trusted (broker)",
)


def conformance_requirements() -> tuple[str, ...]:
    return MT5_CONFORMANCE_REQUIREMENTS


def broker_dependent_requirements() -> tuple[str, ...]:
    return BROKER_DEPENDENT_REQUIREMENTS

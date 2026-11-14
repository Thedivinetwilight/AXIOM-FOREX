"""The execution-mode page: two modes, one of which is in force.

The page answers three questions and nothing else: which mode is in force right
now, what switching would do, and what would refuse a single order once LIVE is
on.  The mode switch is never disabled by a blocker -- a blocker refuses an
*order*, and saying so is the whole point of the page.
"""

from __future__ import annotations

from html import escape
from typing import Any, Mapping, Sequence

from .. import format as fmt
from ..components import (
    action_form,
    blockers,
    button_link,
    cell,
    chip,
    empty_state,
    facts,
    field,
    jump_nav,
    layout,
    notice,
    page_head,
    section,
    table,
)
from . import runner as runner_page


def mode_chip(view: Mapping[str, Any]) -> str:
    live = str(view.get("mode") or "PAPER").upper() == "LIVE"
    return chip("LIVE · MT5 execution" if live else "PAPER · simulated fills", "danger" if live else "paper", dot=True)


def _banner(view: Mapping[str, Any], ctx: Any) -> str:
    live = str(view.get("mode") or "PAPER").upper() == "LIVE"
    effective = bool(getattr(ctx, "live_effective", False))
    title = "Live trading — MetaTrader 5 execution" if live else "Paper trading — live market data"
    effect = str(view.get("trading_effect") or view.get("summary") or "")
    market = str(getattr(getattr(ctx, "terminal", None), "market_data", "NONE") or "NONE")
    market_note = (
        "Prices: MetaTrader 5 terminal ticks."
        if market == "TERMINAL_TICKS"
        else (
            "Prices: sample data from the offline simulator."
            if market == "SAMPLE"
            else "Prices: none — no terminal connected."
        )
    )
    state = (
        chip("Confirmed this session", "danger", dot=True)
        if effective
        else (chip("Awaiting confirmation", "warn", dot=True) if live else chip("Simulated fills only", "paper", dot=True))
    )
    return (
        f"<div class='mode-banner mode-banner--{'live' if live else 'paper'}' role='status'>"
        f"<div><strong>{escape(title)}</strong>"
        f"<p class='small muted' style='margin:4px 0 0'>{escape(effect)} {escape(market_note)}</p></div>"
        f"<div class='spacer'></div>{state}</div>"
    )


def _paper_section(view: Mapping[str, Any], nonce: str) -> str:
    live = str(view.get("mode") or "PAPER").upper() == "LIVE"
    body = (
        "<p style='margin:0 0 10px'>Reads live prices from the selected terminal and simulates "
        "every fill locally. No order is ever sent; the paper ledger keeps the result.</p>"
        + notice(
            "Switching to paper is immediate",
            "No preflight and no confirmation, because the switch can only remove execution "
            "capability. It places no order and closes nothing.",
            tone="muted",
        )
        + "<div class='actions'>"
        + (
            action_form("mode-paper", nonce, "Switch to paper now", tone="primary")
            if live
            else chip("Paper is active", "paper", dot=True)
        )
        + "</div>"
    )
    return section(
        str(view.get("paper_label") or "Paper trading"),
        body,
        tools=chip("ACTIVE", "paper") if not live else "",
        id="mode-paper",
    )


def _live_section(view: Mapping[str, Any], preflight: Mapping[str, Any], ctx: Any, nonce: str) -> str:
    effective = bool(getattr(ctx, "live_effective", False))
    live_requested = str(view.get("mode") or "PAPER").upper() == "LIVE"
    account = dict(preflight.get("account") or {})
    fingerprint = str(account.get("fingerprint") or view.get("session_account") or "")
    phrase = str(view.get("confirmation_phrase") or "")
    tools = (
        chip("ACTIVE", "danger")
        if effective
        else (chip("REQUESTED", "warn") if live_requested else "")
    )

    if effective:
        controls = chip("Live is enabled for this session", "danger", dot=True)
        note = notice(
            "Live execution is on for this session",
            "Every order still passes the pre-trade gate, and the session must be armed "
            "separately on /live. This switch sends nothing.",
            tone="muted",
        )
    else:
        controls = (
            notice(
                "Real-money orders may follow from the active strategy",
                "LIVE routes an order to MetaTrader 5 execution on the account you signed in "
                "to. It does not place one by itself and it does not arm anything: a strategy "
                "still has to signal, the plan still has to be armed, and every per-order check "
                "still applies."
                + (
                    ""
                    if fingerprint
                    else " No account is connected right now, so no order can be sent until "
                    "you connect and confirm one."
                ),
                tone="warn",
            )
            + "<form method='post' action='/actions/mode-live' class='form'>"
            f"<input type='hidden' name='nonce' value='{fmt.attr(nonce)}'>"
            + (
                field(
                    "account_confirmation",
                    "Type the account signature to confirm the account for this session",
                    field_id="mode-account-confirmation",
                    placeholder=fingerprint,
                    required=True,
                    hint="The signature shown in the account panel. A different account refuses the switch.",
                )
                if fingerprint
                else ""
            )
            + field(
                "confirmation",
                "Type the phrase to confirm",
                field_id="mode-confirmation",
                placeholder=phrase,
                required=True,
                hint="Recorded in the local audit document with the switch.",
            )
            + f"<p class='small' style='margin:0 0 8px'>The phrase is "
            f"<span class='mono'>{escape(phrase)}</span>.</p>"
            "<div class='actions'><button class='btn btn--danger' type='submit'>Switch to LIVE</button></div>"
            "</form>"
        )
        note = notice(
            "Two confirmations, and neither of them places an order",
            "The phrase below and the signature of the account you are connected to are both "
            "required and both recorded. Switching to LIVE connects nothing, arms nothing and "
            "sends nothing.",
            tone="muted",
        )

    body = (
        "<p style='margin:0 0 10px'>Routes an approved, gated order through the MetaTrader 5 "
        "execution path to the account you signed in to inside the terminal.</p>"
        + note
        + "<div class='mode-actions'>" + controls + "</div>"
    )
    return section(str(view.get("live_label") or "Live trading"), body, tools=tools, id="mode-live")


def _checks_section(preflight: Mapping[str, Any]) -> str:
    items = tuple(preflight.get("blockers") or ())
    ready = bool(preflight.get("ready"))
    if items:
        body = (
            f"<p class='small muted' style='margin:0 0 10px'>"
            f"{escape(str(preflight.get('summary') or ''))}</p>"
            + blockers(runner_page.blocker_items(items))
        )
    else:
        body = notice(
            "No order-level check is outstanding",
            "The account identity, session, settings and confirmations all line up. Switching "
            "to live still sends nothing.",
            tone="ok",
        )
    body += notice(
        "These never take the mode switch away",
        "Both modes stay selectable whatever this list says. A check that is not met refuses "
        "the specific order and says why; it does not hide, disable or undo your choice of mode.",
        tone="muted",
    )
    return section(
        "What would stop an order from being sent",
        body,
        tools=chip("Clear" if ready else f"{len(items)} outstanding", "ok" if ready else "warn"),
        note="Every genuine blocker, all at once, each with the step that clears it.",
        id="checks",
    )


def _account_section(preflight: Mapping[str, Any], ctx: Any) -> str:
    account = dict(preflight.get("account") or {})
    if not account:
        return section(
            "Selected account",
            empty_state(
                "No account read yet",
                "Connect to the terminal to read the account you signed in to.",
            ),
            id="selected-account",
        )
    fingerprint = str(account.get("fingerprint") or "")
    return section(
        "Selected account",
        facts(
            [
                ("Selected account (server)", fmt.text(account.get("server") or "not read")),
                ("Detected account type", fmt.text(account.get("mode") or "UNKNOWN")),
                ("Account signature", f"<code>…{fmt.text(fingerprint or '—')}</code>"),
                (
                    "Broker connection",
                    chip("connected", "ok", dot=True)
                    if preflight.get("session_connected")
                    else chip("not connected", "muted"),
                ),
            ]
        ),
        note="Read from the terminal's own account record; the mode switch never edits it.",
        id="selected-account",
    )


def _positions_section(ctx: Any) -> str:
    view = dict((ctx.extra or {}).get("live") or {})
    positions = [dict(item) for item in (view.get("positions") or ())]
    if not positions:
        return ""
    rows = [
        [
            cell(fmt.text(item.get("symbol") or "position"), label="Symbol"),
            cell(fmt.text(item.get("direction") or ""), label="Direction"),
            cell(fmt.text(item.get("volume") or ""), label="Volume", num=True),
            cell(fmt.text(item.get("ticket") or "—"), label="Ticket", mono=True),
        ]
        for item in positions[:10]
    ]
    return section(
        f"{len(positions)} live position(s) remain open at the broker",
        table(
            (("Symbol", False), ("Direction", False), ("Volume", True), ("Ticket", False)),
            rows,
            caption="Broker-reported open positions, read from the terminal's own position list.",
        )
        + notice(
            "Changing the mode does not close them",
            "AXIOM never closes a broker position because the mode changed. These stay open in "
            "MetaTrader 5 and remain yours to manage: with LIVE off, AXIOM will not open anything "
            "new and will not manage them for you.",
            tone="warn",
        ),
        note="Read straight from the terminal's own position list.",
        id="open-positions",
    )


def _requirements_section(preflight: Mapping[str, Any]) -> str:
    requirements: Sequence[Mapping[str, Any]] = tuple(preflight.get("order_requirements") or ())
    rows = [
        [
            cell(fmt.text(item.get("title") or item.get("reason") or ""), label="Check"),
            cell(fmt.text(item.get("detail") or ""), label="What stops an order"),
            cell(chip(str(item.get("enforced_at") or "order time"), "muted"), label="When"),
        ]
        for item in requirements
    ]
    return section(
        "Still enforced on every order, whichever mode is on",
        table(
            (("Check", False), ("What stops an order", False), ("When", False)),
            rows,
            caption="Checks re-run for every live order.",
            empty=empty_state(
                "This build lists no per-order check",
                "That is itself a reason to stop: a live order path without checks is not one "
                "this workstation will use.",
            ),
        ),
        note="The mode switch never replaces, relaxes or skips any of these.",
        id="requirements",
    )


def _audit_section(view: Mapping[str, Any]) -> str:
    recent = tuple(view.get("recent") or ())
    rows = [
        [
            cell(fmt.text(item.get("event_type") or ""), label="Event", mono=True),
            cell(fmt.text(item.get("reason") or ""), label="Reason", mono=True),
            cell(fmt.text(item.get("to_mode") or item.get("mode") or ""), label="Mode"),
            cell(fmt.text(item.get("at") or ""), label="When", mono=True),
        ]
        for item in reversed(recent)
    ]
    return section(
        "Execution-mode changes recorded on this machine",
        table(
            (("Event", False), ("Reason", False), ("Mode", False), ("When", False)),
            rows,
            caption="Every switch and every refused switch is written here and to the local audit document.",
            empty=empty_state(
                "No mode changes recorded yet",
                "Every switch and every refused switch is written here and to the local audit "
                "document.",
            ),
        ),
        id="audit",
    )


def render_mode(ctx: Any) -> str:
    view = dict(getattr(ctx, "execution_mode", {}) or {})
    preflight = dict(getattr(ctx, "preflight", {}) or {})
    nonce = fmt.attr(ctx.action_nonce)
    live = str(view.get("mode") or "PAPER").upper() == "LIVE"
    effective = bool(getattr(ctx, "live_effective", False))

    head = page_head(
        "Execution mode",
        "Which execution mode is in force",
        "Exactly one of these is active. Switching sends nothing and places no order either "
        "way; a blocker below refuses an order, never the mode change.",
        badges=mode_chip(view)
        + (
            chip("Confirmed this session", "danger", dot=True)
            if effective
            else chip("Not confirmed", "muted")
        )
        + chip(f"{len(tuple(preflight.get('blockers') or ()))} order check(s) outstanding", "warn" if preflight.get("blockers") else "ok"),
        actions=button_link("/live", "Open the LIVE workflow", small=True),
    )

    aside = _account_section(preflight, ctx) + _positions_section(ctx)

    main = (
        _banner(view, ctx)
        + jump_nav(
            [
                ("mode-paper", "Paper"),
                ("mode-live", "Live"),
                ("checks", "What stops an order"),
                ("requirements", "Per-order checks"),
                ("audit", "Mode history"),
            ]
        )
        + _paper_section(view, nonce)
        + _live_section(view, preflight, ctx, nonce)
        + _checks_section(preflight)
        + _requirements_section(preflight)
        + _audit_section(view)
    )
    if not aside:
        return head + layout(main, wide=True)
    return head + layout(main, aside)

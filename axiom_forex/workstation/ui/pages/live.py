"""LIVE: the guarded, explicit workflow for a real MetaTrader 5 account.

Nothing on this page is a simulated fill, and nothing here is automatic.  The page
is built around one sequence, and every step of it is answered by the runtime
rather than by the layout:

1. **the account** — which account the terminal is on, with its signature, its
   mode (REAL / DEMO / unknown) and the broker's own positions;
2. **the plan** — the exact candidates, symbols and timeframe that would trade;
3. **review** — the last arming decision and what it refused, in its own words;
4. **approval and arming** — the confirmation phrase, the account fingerprint and
   an acknowledgement, all typed by the operator;
5. **the one-shot intent lifecycle** — register, review, approve, arm, submit,
   reconcile, each a separate click with its own phrase;
6. **the broker's answer** — retcode, filled volume, unresolved request ids and
   the reconciliation of the same durable id;
7. **what is still missing** — blockers, not a checklist a click can clear.

PAPER never appears here as a figure: the page says where the paper book is and
leaves it there.
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
    deck,
    empty_state,
    facts,
    field,
    jump_nav_bar,
    layout,
    notice,
    page_head,
    readout,
    section,
    table,
    workgrid,
)
from . import runner as runner_page
from .common import duration_ms, feed_cell, live_latency_facts, live_orders_table

LIFECYCLE = (
    ("configured", "Recorded"),
    ("reviewed", "Reviewed"),
    ("approved", "Approved"),
    ("armed", "Armed"),
    ("submitted", "Submitted"),
    ("reconciled", "Reconciled"),
)


#: Every blocker the live path can raise, with the one place that clears it.  A
#: refusal an operator cannot act on is a dead end, so each row carries its fix;
#: none of these links sends, arms or starts anything by itself.
FIXES: dict[str, tuple[str, str]] = {
    "mode_not_live": ("Switch the mode to LIVE", "/mode"),
    "not_connected": ("Connect the terminal", "/setup"),
    "terminal_not_connected": ("Connect the terminal", "/setup"),
    "no_live_workflow": ("Open setup", "/setup"),
    "live_unavailable": ("Open setup", "/setup"),
    "stack_unavailable": ("Open setup", "/setup"),
    "plan_missing": ("Review the exact plan", "/live#plan"),
    "plan_not_selected": ("Review the exact plan", "/live#plan"),
    "plan_pair_mismatch": ("Review the exact plan", "/live#plan"),
    "plan_timeframe_mismatch": ("Review the exact plan", "/live#plan"),
    "selection_not_applied": ("Review the exact plan", "/live#plan"),
    "not_armed": ("Arm the plan with the phrase", "/live#plan"),
    "paper_not_running": ("Start the PAPER loop", "/autopilot"),
    "paper_lane": ("Start the PAPER loop", "/autopilot"),
    "feed_stale": ("Check the quotes", "/account"),
    "stale_quote": ("Check the quotes", "/account"),
    "unresolved_order": ("Reconcile the unresolved request", "/live#path"),
    "unresolved_requests": ("Reconcile the unresolved request", "/live#path"),
    "account_not_confirmed": ("Confirm the account", "/live#account"),
    "account_mismatch": ("Confirm the account", "/live#account"),
    "risk_limit": ("Read the risk limits", "/live#plan"),
    "live_autopilot_unavailable": ("Open the paper command center", "/autopilot"),
    "terminal_not_connected": ("Connect to MT5 explicitly", "/setup"),
    "live_mode_not_confirmed": ("Confirm LIVE mode", "/mode"),
    "real_account_required": ("Sign in to the declared REAL account", "/setup"),
    "account_identity_mismatch": ("Check the declared account", "/live#local-settings"),
    "scalper_settings_missing": ("Complete the local LIVE settings", "/live#local-settings"),
    "scalper_settings_schema_invalid": ("Repair the local LIVE settings", "/live#local-settings"),
    "no_admitted_candidate": ("Review evidence requirements", "/live#local-settings"),
    "live_admission_review_unavailable": ("Review the signed evidence setup", "/live#local-settings"),
    "another_live_session_running": ("Stop the other LIVE session", "/live#session"),
    "live_adapter_already_armed": ("Disarm the other LIVE plan", "/live#session"),
}
_FALLBACK_FIX = ("Open setup", "/setup")


def fix_for(code: str) -> tuple[str, str]:
    """The control that clears a blocker, or the setup page when it is unknown."""

    return FIXES.get(str(code or ""), _FALLBACK_FIX)


def blockers_with_fix(items: Sequence[Mapping[str, Any]]) -> str:
    """Blockers inline with the control that clears each one.

    Nothing is hidden behind a summary: a refusal that stops real orders is on
    the page, in the words the runtime used, next to its fix.
    """

    rows: list[str] = []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        reason = str(item.get("reason") or "")
        message = str(item.get("message") or item.get("detail") or "")
        hint = str(item.get("hint") or "")
        label, href = fix_for(reason)
        classes = " class='is-danger'" if str(item.get("tone") or "") == "danger" else ""
        rows.append(
            f"<li{classes}>"
            "<span class='glyph' aria-hidden='true'>!</span>"
            f"<span><span class='b-title'>{fmt.text(message or reason or 'Blocked')}</span>"
            + (f"<span class='b-note'>{fmt.text(hint)}</span>" if hint else "")
            + f"<span class='b-code'>{fmt.text(reason)}</span>"
            f"<span class='do'><a class='btn btn--sm' href='{href}'>{fmt.text(label)}</a></span>"
            "</span></li>"
        )
    if not rows:
        return ""
    return f"<ol class='blockers'>{''.join(rows)}</ol>"


def continuous_live_section(view: Mapping[str, Any], ctx: Any) -> str:
    """The continuous guarded session: the only live loop this workstation runs.

    It is the bridge the launcher wires over the workflow this page already
    builds, so the start and stop controls post that object's own actions and
    every order still goes through the same guarded runner and broker adapter.
    There is no second order route, and nothing on this panel sends by itself.
    """

    continuous = dict(view.get("continuous") or {})
    nonce = fmt.attr(getattr(ctx, "action_nonce", ""))
    if not continuous.get("available"):
        return deck(
            "Continuous LIVE Autopilot",
            id="continuous",
            lane="live",
            state="not wired into this process",
            note="continuous, not one-shot",
            readouts=[
                readout("Session", "not available", foot="no live bridge", small=True),
                readout("Order sending", chip("not sending", "muted"), foot="no adapter reached", small=True),
                readout("Orders recorded", "0", foot="nothing was attempted", small=True),
                readout("Broker positions", "not read", foot="no terminal touched", small=True),
                readout("Quote feed", "not measured", foot="no broker quote", small=True),
            ],
            body=notice(
                "No continuous live session is wired into this workstation",
                fmt.text(str(continuous.get("message") or ""))
                or "This process cannot run a continuous live session. The PAPER loop, the "
                "manual arming path and the one-shot intent bridge below are unaffected.",
                tone="muted",
            ),
            foot="Continuous means a loop decides repeatedly. This workstation has none here, "
            "so no button on this page pretends it does. "
            + button_link("/autopilot", "PAPER command center", small=True),
        )

    state = str(continuous.get("state") or "IDLE")
    sending = bool(continuous.get("order_sending"))
    running = state in {"RUNNING", "STARTING"}
    blockers = [dict(item) for item in (continuous.get("blockers") or ())]
    orders = [dict(item) for item in (continuous.get("orders") or ())]
    latency = dict(continuous.get("latency") or {})
    feed = dict(continuous.get("feed") or {})
    strategy = dict(continuous.get("strategy") or {})
    positions = [dict(item) for item in (continuous.get("positions") or ())]
    unresolved = [item for item in orders if item.get("unresolved")]
    match = continuous.get("plan_matches_selection")
    pair = str(continuous.get("pair") or strategy.get("pair") or "")
    symbol = str(continuous.get("broker_symbol") or pair or "not resolved")
    timeframe = str(continuous.get("timeframe") or strategy.get("timeframe") or "")
    candidate = str(strategy.get("candidate_id") or continuous.get("candidate_id") or "")

    account = dict(view.get("account") or {})
    fingerprint = str(account.get("fingerprint") or view.get("account_fingerprint") or "")
    body = facts(
        [
            ("What would run", fmt.text(candidate) or "no candidate selected"),
            (
                "Account this session would trade",
                chip(str(view.get("account_mode") or "unknown").upper(), "danger" if str(view.get("account_mode") or "").upper() == "REAL" else "warn")
                + " "
                + (
                    chip("fingerprint confirmed", "ok", dot=True)
                    if view.get("armed") and fingerprint
                    else chip("not confirmed by an arm", "muted")
                )
                + f" <code>{fmt.text(fingerprint or 'not read')}</code>",
            ),
            (
                "Session mode",
                chip(str(view.get("mode") or "PAPER"), "danger" if view.get("live_effective") else "paper", dot=True),
            ),
            ("Pair and broker symbol", f"{fmt.text(pair) or '—'} · {fmt.text(symbol)}"),
            ("Timeframe", fmt.text(timeframe) or "not resolved"),
            (
                "Plan matches the selection",
                {True: "yes", False: "no", None: "not known"}.get(match, "not known"),
            ),
            (
                "Continuous bridge armed",
                chip("ARMED", "danger", dot=True)
                if continuous.get("armed")
                else chip("Not armed", "muted"),
            ),
            ("Feed detail", fmt.text(str(feed.get("detail") or "not measured"))),
        ]
    )
    if blockers:
        body += "<h3 style='margin:10px 0 5px'>What stops it, and where to fix it</h3>"
        body += blockers_with_fix(blockers)
    else:
        body += notice(
            "Every prerequisite the live path checks is satisfied",
            "That is a statement about the checks, not about the market: the next signal still "
            "passes the adapter's own preflight, the risk limits and the unresolved-order lockout.",
            tone="ok",
        )

    controls: list[str] = []
    if running:
        controls.append(
            action_form("autopilot-live-stop", nonce, "Stop the live session", tone="warn", described_by="continuous-note")
        )
    elif not blockers:
        controls.append(
            action_form("autopilot-live-start", nonce, "Start the live session", tone="danger", described_by="continuous-note")
        )

    record = ""
    if orders:
        record += "<h3 style='margin:12px 0 5px'>Orders the live path recorded</h3>"
        record += live_orders_table(
            orders,
            caption="Every order this session attempted, with the path's own answer and whether "
            "the request is still unresolved at the broker.",
        )
    if positions:
        record += "<h3 style='margin:12px 0 5px'>Broker positions</h3>"
        record += table(
            (("Ticket", False), ("Symbol", False), ("Volume", False), ("Profit", False)),
            [
                [
                    cell(fmt.text(item.get("ticket") or ""), label="Ticket", mono=True),
                    cell(fmt.text(item.get("symbol") or ""), label="Symbol"),
                    cell(fmt.text(item.get("volume") or ""), label="Volume", mono=True),
                    cell(fmt.text(item.get("profit") or ""), label="Profit", mono=True),
                ]
                for item in positions
            ],
            caption="Open positions the terminal reports. Stopping the session never closes them.",
        )
    if record:
        body += record

    readouts = [
        readout(
            "Session",
            chip(state, "danger" if sending else ("ok" if running else "muted"), dot=running),
            foot="a loop is deciding now" if running else "no loop is deciding",
        ),
        readout(
            "Order sending",
            chip("SENDING REAL ORDERS", "danger", dot=True) if sending else chip("not sending", "muted"),
            foot="adapter preflight decides every order",
            tone="live" if sending else "",
        ),
        readout(
            "Orders recorded",
            fmt.text(len(orders)),
            foot=f"{len(unresolved)} unresolved",
            small=True,
        ),
        readout(
            "Broker positions",
            fmt.text(continuous.get("open_positions", len(positions))),
            foot="terminal-reported, not PAPER",
            small=True,
        ),
        readout("Quote feed", feed_cell(feed), foot="what the strategy read", small=True),
    ]
    note = (
        "Starting begins the loop this page already wired: every signal passes that path's gate, "
        "risk limits, account check and unresolved-order lockout. Stopping disarms the plan and "
        "stops new submissions; any open position stays open in the terminal. Arming happens "
        "below, with the phrase."
    )
    latency_html = ""
    if any(value is not None for value in latency.values()):
        latency_html = (
            "<details class='fold'><summary><span class='fold-h'>Measured latency</span>"
            "<span class='fold-n'>how long each step of this path took</span></summary>"
            "<div class='fold-body'>"
            + live_latency_facts(latency)
            + "</div></details>"
        )
    return deck(
        "Continuous LIVE Autopilot",
        id="continuous",
        lane="live",
        state="guarded loop · one runner and one broker adapter",
        note="continuous, not one-shot",
        actions="".join(controls),
        readouts=readouts,
        body=body,
        foot=f"<span id='continuous-note'>{fmt.text(note)}</span>",
    ) + latency_html

def _live_status_banner(view: Mapping[str, Any]) -> str:
    """One line, worded as an answer: will the next signal send a real order?

    It is a single row, not a card: the question a real-account page has to answer
    before anything else is whether the next signal reaches a broker.
    """

    if not view.get("configured"):
        return (
            "<div class='live-banner live-banner--live-off' role='status' data-state='Not sending'>"
            "<span class='lb-title'>No real order can be sent right now</span>"
            "<span class='lb-note'>There is no MetaTrader 5 trading adapter wired in, so "
            "nothing on this page can reach a broker at all.</span>"
            f"<span class='spacer'></span>{chip('Not sending', 'ok', dot=True)}</div>"
        )
    if view.get("next_signal_sends_real_order"):
        title, label, tone, state = "The next strategy signal WILL send a real order", "Sending", "danger", "live-on"
    elif view.get("armed"):
        title, label, tone, state = "Armed, but no session is running", "Armed", "warn", "armed"
    else:
        title, label, tone, state = "No real order can be sent right now", "Not sending", "ok", "off"
    sentence = str(
        (dict(view.get("summary") or {})).get("sentence")
        or (
            "The session is running and an armed plan will be handed to the broker on the "
            "next signal."
            if view.get("next_signal_sends_real_order")
            else (
                "The plan is armed, but no session loop is running, so no signal can reach "
                "the broker."
                if view.get("armed")
                else "No plan is armed and no session is running. Arming is the only step that "
                "could change that, and it sends nothing."
            )
        )
    )
    return (
        f"<div class='live-banner live-banner--live-{state}'"
        f" role='status' data-state='{escape(label)}'>"
        f"<span class='lb-title'>{escape(title)}</span>"
        f"<span class='lb-note'>{escape(sentence)}</span>"
        f"<span class='spacer'></span>{chip(label, tone, dot=True)}</div>"
    )


def _state_chip(armed: bool, configured: bool) -> str:
    if not configured:
        return chip("NO ADAPTER", "muted")
    return chip("ARMED", "danger", dot=True) if armed else chip("Not armed", "muted")


def stop_warnings(view: Mapping[str, Any]) -> str:
    """Risk the operator must see, which arming does not clear.

    An open position this deployment placed without a broker-side stop is the
    state the order path refuses to add to, so it is said first and in the
    danger tone, with the tickets named.  These warnings block no *close*: a
    position without a stop has to stay closable.
    """

    warnings = [dict(item) for item in (view.get("warnings") or ())]
    if not warnings:
        return ""
    urgent = any(item.get("urgent") for item in warnings)
    items = "".join(
        "<li><strong>{}</strong>{}<div class='small muted'>{}</div></li>".format(
            escape(str(item.get("message") or "")),
            (
                f" <code>{escape(', '.join(str(ticket) for ticket in item.get('tickets') or ()))}</code>"
                if item.get("tickets")
                else ""
            ),
            escape(str(item.get("hint") or "")),
        )
        for item in warnings
    )
    return notice(
        "Unprotected exposure" if urgent else "Worth reading",
        f"<ul class='list' role='list'>{items}</ul>",
        tone="danger" if urgent else "warn",
        role="alert",
    )


def broker_stop_state(view: Mapping[str, Any]) -> str:
    """The broker's own answer about the stop on the positions it holds.

    "Verified by broker" is claimed only when the broker reports a stop on every
    position this deployment placed; nothing, or a missing one, is said plainly.
    """

    facts = dict(view.get("broker_stop") or {})
    if not facts.get("available"):
        return chip("not checked", "muted") + (
            " <span class='small muted'>{}</span>".format(
                escape(str(facts.get("detail") or "no broker position has been read"))
            )
        )
    points = int(facts.get("configured_points") or 0)
    declared = str(facts.get("declared") or "unspecified")
    unprotected = [dict(item) for item in (facts.get("unprotected") or ())]
    if points <= 0:
        return chip("not configured", "danger", dot=True) + (
            " <span class='small muted'>no usable distance for this symbol — live entries "
            "are refused until one is declared under Broker-side stop settings</span>"
        )
    if unprotected:
        tickets = ", ".join(escape(str(item.get("ticket") or "")) for item in unprotected)
        return chip("unprotected", "danger", dot=True) + (
            " <span class='small muted'>{} ({} pt) requested · ticket(s) {} report no "
            "stop loss</span>".format(escape(declared), points, tickets)
        )
    checked = int(facts.get("positions_checked") or 0)
    if checked == 0:
        return chip("configured", "muted", dot=True) + (
            " <span class='small muted'>{} ({} pt) · no open position to verify right "
            "now</span>".format(escape(declared), points)
        )
    return chip("verified by broker", "ok", dot=True) + (
        " <span class='small muted'>{} ({} pt) on {} open position(s)</span>".format(
            escape(declared), points, checked
        )
    )


def _account_kind_notice(view: Mapping[str, Any]) -> str:
    kind = str(view.get("account_mode") or "").upper()
    if kind == "REAL":
        return notice(
            "The connected account is a REAL-money account",
            "Every order this page authorises spends real money. A demo account is not a "
            "rehearsal either: the same code path runs, with the same refusals.",
            tone="danger",
            role="alert",
        )
    if kind in {"DEMO", "CONTEST"}:
        return notice(
            f"The connected account is a {kind} account",
            "No real money is at risk on this account type, and the same gates still apply.",
            tone="warn",
        )
    return notice(
        "The account type has not been identified",
        "AXIOM cannot tell whether this is a demo or a real account, so nothing may be armed "
        "from this page until the terminal answers.",
        tone="warn",
    )


def _lifecycle(view: Mapping[str, Any], scalper: Mapping[str, Any]) -> str:
    """Where the exact plan stands, one step at a time."""

    selected = dict(scalper.get("selected") or {})
    submitted = bool(selected.get("broker"))
    reconciled = bool(selected.get("reconciliation"))
    reached = {
        "configured": bool(view.get("configured")),
        "reviewed": bool(selected.get("reviewed")),
        "approved": bool(selected.get("approved")),
        "armed": bool(view.get("armed")) or bool(selected.get("armed")),
        "submitted": submitted,
        "reconciled": reconciled,
    }
    current = ""
    for key, _label in LIFECYCLE:
        if reached.get(key):
            current = key
    if current == "reconciled":
        current = ""  # every stage reached: no step is "the current one" any more
    items = []
    for key, label in LIFECYCLE:
        done = bool(reached.get(key))
        mark = "✓" if done else "·"
        current_attr = " aria-current='step'" if key_done(key, current, done) else ""
        suffix = (
            " <span class='small muted'>· current step</span>"
            if key == current
            else ("" if done else " <span class='small muted'>· not reached</span>")
        )
        items.append(
            f"<li{current_attr}>"
            f"<span class='glyph' aria-hidden='true'>{mark}</span>"
            f"<span>{fmt.text(label)}{suffix}</span></li>"
        )
    status = str(selected.get("status") or "")
    return (
        "<ol class='lifecycle' aria-label='LIVE plan lifecycle'>" + "".join(items) + "</ol>"
        + (
            f"<p class='small muted' style='margin:8px 0 0'>Current status: {fmt.text(status)}</p>"
            if status
            else ""
        )
    )


def key_done(key: str, current: str, done: bool) -> bool:
    """The current step is the last *reached* one, and only while it is the last."""

    return bool(current) and key == current and done


def _account_section(view: Mapping[str, Any]) -> str:
    account = dict(view.get("account") or {})
    positions = [dict(item) for item in (view.get("positions") or ())]
    scalper_live = dict(view.get("scalper_live") or {})
    pnl = dict(scalper_live.get("pnl") or {})
    pnl_value = (
        f"{fmt.text(pnl.get('total'))} {fmt.text(pnl.get('currency'))}"
        if pnl.get("available")
        else "Unavailable · " + fmt.text(pnl.get("reason") or "broker profit was not reported")
    )
    position_rows = [
        [
            cell(fmt.text(item.get("symbol") or ""), label="Symbol"),
            cell(fmt.text(item.get("side") or ""), label="Side"),
            cell(fmt.text(item.get("volume") or ""), label="Volume", num=True),
            cell(fmt.text(item.get("price_open") or ""), label="Open price", num=True),
            cell(fmt.text(item.get("profit") or ""), label="Profit", num=True),
        ]
        for item in positions
    ]
    return section(
        "The account this would trade",
        _account_kind_notice(view)
        + facts(
            [
                ("Connected", chip("yes", "ok") if view.get("connected") else chip("no", "warn", dot=True)),
                ("Account mode", fmt.text(view.get("account_mode") or "unknown")),
                ("Login", fmt.text(account.get("login_masked") or "not reported")),
                ("Server", fmt.text(account.get("server") or "not reported")),
                ("Currency / leverage", f"{fmt.text(account.get('currency') or '—')} · {fmt.text(account.get('leverage') or '—')}"),
                ("Signature", f"<code>{fmt.text(account.get('fingerprint') or view.get('account_fingerprint') or '—')}</code>"),
                ("Trading allowed in terminal", chip("yes", "ok") if account.get("trading_allowed") else chip("no", "warn")),
                ("Orders sent this session", fmt.text(view.get("orders_sent", 0))),
                ("Broker positions", fmt.text(len(positions))),
                ("Broker-reported open-position P&L", pnl_value),
                ("P&L basis", fmt.text(pnl.get("basis") or "broker-reported open-position profit only")),
                ("Unresolved orders", fmt.text(len(view.get("unresolved") or []))),
            ]
        )
        + (
            notice(
                f"{len(positions)} live position(s) are still open at the broker",
                "Stopping new entries does not flatten them. The exact active AX-SCALPER run "
                "may manage only positions it restored and verified; other positions remain "
                "outside this runner. Use the broker table below to review its own report.",
                tone="warn",
            )
            if positions
            else ""
        )
        + "<p class='small'><a href='#broker-positions'>Jump to the broker position table</a></p>"
        + "<h3 class='small muted' style='margin:18px 0 6px' id='broker-positions'>"
        "Broker-reported open positions (not PAPER positions)</h3>"
        + table(
            (("Symbol", False), ("Side", False), ("Volume", True), ("Open price", True), ("Profit", True)),
            position_rows,
            caption="Read from the terminal. The broker is authoritative for these positions.",
            empty=empty_state("No broker position is open", "The broker reports a flat book."),
        ),
        note="PAPER positions and P&L are on /autopilot. They are a different book, read from "
        "a different file, and are never mixed into these rows.",
        id="account",
    )


def broker_protection_status_section(view: Mapping[str, Any]) -> str:
    """Keep broker-reported protection warnings visible without legacy entry controls."""

    warnings_html = stop_warnings(view)
    broker_stop = dict(view.get("broker_stop") or {})
    protective = dict(view.get("protective_stop") or {})
    if not warnings_html and not broker_stop.get("available") and not protective:
        return ""
    pairs = [
        ("Broker-side stop", broker_stop_state(view)),
        (
            "Plan protective stop",
            (f"<code>{fmt.text(protective.get('pips'))}</code> pips "
             if protective.get("pips") else "not reported ")
            + f"<span class='small muted'>{fmt.text(protective.get('note') or 'a limit AXIOM watches')}</span>",
        ),
    ]
    return section(
        "Existing broker protection",
        warnings_html + facts(split=True, pairs=pairs),
        note="The broker is authoritative. This readout does not change an order.",
        id="broker-protection",
    )


def existing_order_reconciliation_section(view: Mapping[str, Any], nonce: str) -> str:
    """Keep exact-id reconciliation reachable for requests recorded by older builds."""

    scalper_live = dict(view.get("scalper_live") or {})
    unresolved = [str(item) for item in (scalper_live.get("unresolved_orders") or ()) if str(item)]
    if not unresolved:
        return ""
    result = dict(scalper_live.get("result") or {})
    rows = "".join(
        "<li><code>{}</code>{}</li>".format(
            fmt.text(intent_id),
            action_form(
                "scalper-live-reconcile", nonce, "Reconcile this exact request id",
                fields=f"<input type='hidden' name='intent_id' value='{fmt.attr(intent_id)}'>",
            ),
        )
        for intent_id in unresolved
    )
    latest = (
        notice(
            "Last reconciliation result",
            fmt.text(result.get("message") or result.get("reason") or "not reported"),
            tone="warn" if not result.get("ok") else "ok",
        )
        if result
        else ""
    )
    return section(
        "Previously recorded order requests",
        notice(
            "Reconciliation only",
            "These requests were recorded by an earlier LIVE workflow. Reconciliation queries the same durable request id and never submits again. New one-shot entry controls are disabled.",
            tone="warn",
        )
        + latest
        + f"<ul class='list' role='list'>{rows}</ul>",
        id="previous-live-requests",
    )


def _plan_section(view: Mapping[str, Any], nonce: str) -> str:
    plan = dict(view.get("plan") or {})
    review = dict(view.get("review") or {})
    candidates = [dict(item) for item in (view.get("candidates") or ())]
    preview = dict(view.get("last_arm") or {})
    phrase = str(view.get("confirmation_phrase") or "")
    fingerprint = str(view.get("account_fingerprint") or (view.get("account") or {}).get("fingerprint") or "")
    blocked = bool(view.get("blockers"))

    candidate_rows = [
        [
            cell(fmt.text(item.get("candidate_id") or "—"), label="Candidate", mono=True),
            cell(
                chip(str(item.get("evidence_state") or "UNVALIDATED"), "ok" if item.get("evidence_state") == "MEASURED" else "warn"),
                label="Evidence state",
            ),
            cell(fmt.text(item.get("evidence_note") or "—"), label="Evidence note"),
            cell(fmt.text(item.get("rationale") or "—"), label="Rationale"),
        ]
        for item in candidates
    ]

    arming = ""
    if view.get("configured"):
        arming = (
            "<h3 class='small muted' style='margin:18px 0 6px'>Review the exact plan, then arm it</h3>"
            f"<p class='small muted'>Type the confirmation phrase <code>{fmt.text(phrase)}</code>, "
            "type the account fingerprint exactly, and acknowledge that you have read the plan. "
            "A mismatch in any of the three refuses the arm and records the refusal.</p>"
            "<form method='post' action='/actions/live-arm' class='form'>"
            f"<input type='hidden' name='nonce' value='{fmt.attr(nonce)}'>"
            + field("phrase", "Confirmation phrase", field_id="arm-phrase", hint="Exactly as written above, including case.")
            + field(
                "account_fingerprint",
                "Account fingerprint",
                field_id="arm-fingerprint",
                value=fingerprint,
                hint="The signature of the account the terminal is on now. It must match this page.",
            )
            + field(
                "acknowledge",
                "I have reviewed the exact plan below and I mean to arm it",
                field_id="arm-acknowledge",
                kind="checkbox",
                hint="Untick this and the arm is refused.",
            )
            + "<div class='actions'><button class='btn btn--danger' type='submit'>Arm this exact plan</button></div>"
            "</form>"
        )

    record = dict(view.get("last_arm") or {})
    decision = {**record, **review}
    refusal = ""
    if decision:
        refusal = facts(
            split=True,
            pairs=[
                ("Last arming decision", chip("Armed" if decision.get("armed") else str(decision.get("outcome") or "refused"), "ok" if decision.get("armed") else "warn", dot=True)),
                ("Reason", f"<code>{fmt.text(decision.get('reason') or '—')}</code>"),
                ("Message", fmt.text(decision.get("message") or "no message was recorded")),
                ("At", fmt.text(fmt.utc(decision.get("at"))) if decision.get("at") else "not recorded"),
                ("A record of the arming click only", "arming writes the plan; it sends nothing"),
            ]
        )
    return section(
        "The exact plan, and arming it",
        "<h3 class='small muted' style='margin:0 0 6px'>Exact plan, candidate and evidence</h3>"
        + facts(
            split=True,
            pairs=[
                ("Plan on record", chip("yes", "ok") if plan.get("present") else chip("no", "warn")),
                ("Plan fingerprint", f"<code>{fmt.text(plan.get('fingerprint') or '—')}</code>"),
                ("Symbols", fmt.text(", ".join(view.get("symbols") or []) or "none named")),
                ("Timeframes", fmt.text(", ".join(view.get("timeframes") or []) or "none named")),
                ("Live timeframe", fmt.text(view.get("live_timeframe") or "not chosen yet")),
                ("Account fingerprint", f"<code>{fmt.text(fingerprint or '—')}</code>"),
                ("Armed", chip("ARMED", "danger", dot=True) if view.get("armed") else chip("Not armed", "muted")),
            ]
        )
        + table(
            (("Candidate", False), ("Evidence state", False), ("Evidence note", False), ("Rationale", False)),
            candidate_rows,
            caption="Strategy candidates and their evidence.",
            empty=empty_state(
                "No strategy candidate is wired into this LIVE path",
                "The operator must type an exact candidate on the arming form. Nothing is picked "
                "for them.",
            ),
        )
        + refusal
        + arming
        + blockers(runner_page.blocker_items(view.get("blockers") or ())),
        note="Review, approval, the phrase and the fingerprint are all on this page and nowhere "
        "else. The PAPER command center cannot arm, submit, or close a broker order.",
        tools=_state_chip(bool(view.get("armed")), bool(view.get("configured"))),
        id="plan",
    )


def _session_section(view: Mapping[str, Any], nonce: str) -> str:
    protective = dict(view.get("protective_stop") or {})
    controls: list[str] = []
    if view.get("armed"):
        controls.append(action_form("live-stop", nonce, "EMERGENCY STOP — stop sending and disarm", tone="danger"))
        controls.append(action_form("live-disarm", nonce, "Disarm", tone="danger"))
    if view.get("session_running"):
        controls.append(action_form("live-stop", nonce, "Stop the live session", tone="warn"))
    else:
        controls.append(action_form("live-start", nonce, "Start live session", tone="primary"))
    body = facts(
        split=True,
        pairs=[
            ("Adapter", chip("configured", "ok") if view.get("configured") else chip("not configured", "muted")),
            ("Armed", chip("ARMED", "danger", dot=True) if view.get("armed") else chip("Not armed", "muted")),
            ("Execution mode", chip(str(view.get("mode") or "PAPER"), "danger" if view.get("live_effective") else "paper", dot=True)),
            ("Session", chip(str(view.get("session_state") or "NOT RUNNING"), "ok" if view.get("session_running") else "muted")),
            ("Next signal sends a real order", chip("YES", "danger", dot=True) if view.get("next_signal_sends_real_order") else chip("no", "ok")),
            ("Orders sent", fmt.text(view.get("orders_sent", 0))),
            ("Stopped because", fmt.text(view.get("stopped_reason") or "—")),
            ("Unresolved broker requests", fmt.text(len(view.get("unresolved") or [])) or "0"),
            ("Broker-side stop", broker_stop_state(view)),
            (
                "Plan protective stop",
                (
                    f"<code>{fmt.text(protective.get('pips'))}</code> pips "
                    if protective.get("pips")
                    else "not stated "
                )
                + f"<span class='small muted'>{fmt.text(str(protective.get('note') or ''))}</span>",
            ),
        ]
    ) + "<div class='actions' style='margin-top:12px'>" + "".join(controls) + "</div>"
    body += notice(
        "The emergency stop is the only control here that acts without a confirmation",
        "It stops sending and disarms, and it closes nothing at the broker: it does not close "
        "a broker position, and it cannot cancel or modify an order the broker already holds.",
        tone="muted",
    )
    return section(
        "Manual session (no strategy bridge)",
        body,
        note="Start begins the loop; stop ends it and disarms. Neither connects, signs in, "
        "changes the mode, or closes a position. The continuous autopilot above uses this same "
        "session underneath: there is one order route, not two.",
        id="session",
    )


def broker_stop_settings_section(view: Mapping[str, Any], nonce: str) -> str:
    """The one control that sets the broker-side stop, with its units shown.

    Every number here comes from the terminal's own symbol record or from the
    settings document: the broker symbol, the point size, the unit of the entered
    value and the broker's minimum distance.  Nothing is pre-filled, and a save is
    refused while trading is armed or running.
    """

    setting = dict(view.get("stop_setting") or {})
    armed = bool(view.get("armed"))
    running = bool(view.get("session_running"))
    pair = str(setting.get("pair") or "")
    symbol = str(setting.get("broker_symbol") or "")

    def present(value: Any) -> str:
        text = str(value if value is not None else "").strip()
        return fmt.text(text) if text else "<span class='small muted'>not reported</span>"

    def pip_in_points() -> str:
        try:
            pip = float(setting.get("pip_size") or 0)
            point = float(setting.get("point") or 0)
        except (TypeError, ValueError):
            return "not reported"
        if not pip or not point:
            return "not reported"
        ratio = pip / point
        text = f"{ratio:.2f}".rstrip("0").rstrip(".")
        return f"{text} point(s)"

    declared = str(setting.get("declared") or "")
    unit = str(setting.get("unit") or "")
    points = setting.get("distance_points")
    if not setting.get("available"):
        state_cell = chip("not checked", "muted") + " <span class='small muted'>" + present(
            setting.get("reason") or setting.get("detail")
        ) + "</span>"
    elif setting.get("valid"):
        state_cell = (
            chip("usable", "ok", dot=True)
            + " <span class='small muted'>"
            + fmt.text(f"{declared}" if declared else "")
            + f" = {present(points)} broker point(s) = {present(setting.get('distance_price'))} in price</span>"
        )
    else:
        state_cell = (
            chip("not usable", "danger", dot=True)
            + f" <span class='mono'>{fmt.text(str(setting.get('reason', '')))}</span> "
            + f"<span class='small muted'>{present(setting.get('detail'))}</span>"
        )

    pairs = [str(item) for item in (setting.get("pairs") or ())]
    rows = [
        ("Pair", fmt.text(pair) if pair else "<span class='small muted'>not chosen yet</span>"),
        ("Broker symbol", present(symbol)),
        (
            "Entered value",
            (
                f"<span class='mono'>{fmt.text(declared)}</span> "
                f"<span class='small muted'>(unit: {fmt.text(unit or 'not stated')})</span>"
            )
            if declared
            else "<span class='small muted'>nothing declared for this symbol</span>",
        ),
        ("Symbol point size", present(setting.get("point"))),
        ("Symbol pip size", present(setting.get("pip_size"))),
        (
            "Broker minimum stop",
            f"{present(setting.get('broker_min_points'))} point(s) = "
            f"{present(setting.get('broker_min_price'))} in price",
        ),
        ("State", state_cell),
    ]
    body = facts(rows)
    if setting.get("pending"):
        body += notice(
            "Saved, not yet in force",
            f"The settings document now says {fmt.text(str(setting.get('file_declared', '')))}, "
            f"but this adapter was built with {fmt.text(declared or 'nothing')}. Reconnect for "
            "the file to be read; until then no live entry is sent.",
            tone="warn",
        )
    if armed or running:
        body += notice(
            "Stop and disarm to change the distance",
            "The adapter reads its settings when it is built. Changing the file now would be a "
            "knob that does nothing, so the write is refused.",
            tone="warn",
        )
    elif not pair and not pairs:
        body += notice(
            "No symbol to configure yet",
            "Declare the pairs you trade in the provider setup, or arm a plan naming one; this "
            "page will not invent a symbol for you.",
            tone="muted",
        )
    else:
        if pair:
            choose = f"<input type='hidden' name='pair' value='{fmt.attr(pair)}'>"
        else:
            choose = (
                "<label class='field'><span class='label'>Symbol this distance applies to</span>"
                "<select name='pair' required>"
                "<option value=''>Choose the symbol…</option>"
                + "".join(
                    f"<option value='{fmt.attr(item)}'>{fmt.text(item)}</option>" for item in pairs
                )
                + "</select><span class='hint'>No plan is armed, so nothing is pre-selected: the "
                "distance is stored for the pair you name here.</span></label>"
            )
        body += (
            "<form method='post' action='/actions/live-stop-settings' class='form' "
            "style='margin-top:14px'>"
            f"<input type='hidden' name='nonce' value='{fmt.attr(nonce)}'>"
            + choose
            + field(
                "distance",
                "Distance",
                hint="One positive number. Nothing is pre-filled and nothing is widened for you.",
                placeholder="e.g. 20",
            )
            + "<label class='field'><span class='label'>Unit</span>"
            "<select name='unit' required>"
            "<option value=''>Choose points or pips…</option>"
            "<option value='pips'>pips</option><option value='points'>points</option></select>"
            "<span class='hint'>A point is the broker's own unit; on this symbol one pip is "
            f"{fmt.text(pip_in_points())}.</span></label>"
            "<div class='actions'><button class='btn' type='submit'>Save the distance</button></div>"
            "</form>"
        )
    return section(
        "Broker-side stop",
        body,
        note="The plan's protective stop, written into the settings document after the "
        "terminal's own symbol record has been checked. It writes no plan and sends nothing.",
        tools=chip("Broker-side stop" if setting.get("available") else "not checked", "muted"),
        id="broker-stop",
    )


def _scalper_live_section(view: Mapping[str, Any], nonce: str) -> str:
    scalper = dict(view.get("scalper_live") or {})
    title = "Guarded AX-SCALPER one-shot LIVE intent (not continuous)"
    if not scalper.get("available"):
        return section(
            title,
            notice(
                "Bridge unavailable",
                "This workstation has no exact typed-intent bridge. PAPER results cannot "
                "authorise LIVE trading.",
                tone="muted",
            )
            + "<p class='small muted'>Nothing here loops: a one-shot intent is typed, reviewed, "
            "approved, armed and submitted once, and the continuous session above is a different "
            "control with a different record.</p>",
            note="One-shot, not a session.",
            tools=chip("ONE-SHOT", "muted"),
            id="one-shot",
        )
    selected = dict(scalper.get("selected") or {})
    broker = dict(selected.get("broker") or {})
    reconciliation = dict(selected.get("reconciliation") or {})
    last_event = dict(selected.get("last_event") or {})
    pnl = dict(scalper.get("pnl") or {})
    one_shot = bool(scalper.get("one_shot_submission_available"))
    intent_id = str(selected.get("intent_id") or "")
    fingerprint = str(selected.get("plan_fingerprint") or "")
    account = str(scalper.get("account_fingerprint") or "")

    body = facts(
        split=True,
        pairs=[
            (
                "One-shot submission",
                chip("available", "ok", dot=True) if one_shot else chip("BLOCKED", "danger", dot=True),
            ),
            (
                "Continuous /live session",
                chip("running", "danger", dot=True) if view.get("session_running") else chip("stopped", "ok"),
            ),
            (
                "Next signal could send",
                chip("yes", "danger", dot=True) if view.get("next_signal_sends_real_order") else chip("no", "ok"),
            ),
            ("Selected intent", f"<code>{fmt.text(intent_id or 'none')}</code>"),
            ("Proposal status", fmt.text(selected.get("status") or "not recorded")),
            ("Plan fingerprint", f"<code>{fmt.text(fingerprint or '—')}</code>"),
            (
                "Review / approval / armed",
                " / ".join(
                    [
                        "reviewed" if selected.get("reviewed") else "not reviewed",
                        "approved" if selected.get("approved") else "not approved",
                        "armed" if selected.get("armed") else "not armed",
                    ]
                ),
            ),
            (
                "Broker open-position P&L",
                (
                    f"{fmt.text(pnl.get('total'))} {fmt.text(pnl.get('currency'))}"
                    if pnl.get("available")
                    else "unavailable — " + fmt.text(pnl.get("reason") or "No complete broker-reported profit is available")
                ),
            ),
            ("P&L basis", fmt.text(pnl.get("basis") or "broker-reported open position profit only")),
            ("Open broker positions", fmt.text(len(scalper.get("positions") or ()))),
            ("Unresolved order ids", fmt.text(len(scalper.get("unresolved_orders") or ()))),
            ("Approval file", f"<code>{fmt.text(scalper.get('approval_file') or 'not wired')}</code>"),
        ]
    )
    if not one_shot:
        body += notice(
            "One-shot submission BLOCKED",
            "Stop the continuous /live session before any one-shot typed intent. The exact "
            "signed evidence, separate approval, account identity and quote freshness are all "
            "required; resolve every blocker below. No order is sent from a blocked state.",
            tone="warn",
        )

    body += (
        "<h3 style='margin:14px 0 4px'>The typed intent, one step at a time</h3>"
        "<p class='small muted'>Each step is its own click with its own phrase. The same durable "
        "request id follows the intent to the broker and back. Nothing here loops.</p>"
    )
    steps = [
        (
            "Record the exact proposal",
            "<form method='post' action='/actions/scalper-live-register' class='form'>"
            f"<input type='hidden' name='nonce' value='{fmt.attr(nonce)}'>"
            + "<div class='form-row'>"
            + field("intent_id", "Exact intent id", field_id="register-intent", value=intent_id, placeholder=intent_id or "paste or keep empty for a new proposal")
            + field("account_confirmation", "Type the exact account fingerprint", field_id="register-account", placeholder=account or "account fingerprint")
            + field("plan_fingerprint", "Type the exact plan fingerprint", field_id="register-plan", value=fingerprint)
            + "</div>"
            "<div class='actions'><button class='btn' type='submit'>Record exact proposal</button></div>"
            "</form>",
        ),
        (
            "Review it",
            "<form method='post' action='/actions/scalper-live-review' class='form'>"
            f"<input type='hidden' name='nonce' value='{fmt.attr(nonce)}'>"
            + "<div class='form-row'>"
            + field("phrase", "Review phrase", field_id="review-phrase", placeholder=str(scalper.get("review_phrase") or ""))
            + field("intent_id", "Intent id", field_id="review-intent", value=intent_id)
            + "</div>"
            "<div class='actions'><button class='btn' type='submit'>Review exact proposal</button></div>"
            "</form>",
        ),
        (
            "Approve the signed evidence",
            "<form method='post' action='/actions/scalper-live-approve' class='form'>"
            f"<input type='hidden' name='nonce' value='{fmt.attr(nonce)}'>"
            + "<div class='form-row'>"
            + field("phrase", "Approval phrase", field_id="approve-phrase", placeholder=str(scalper.get("approval_phrase") or ""))
            + field("intent_id", "Intent id", field_id="approve-intent", value=intent_id)
            + "</div>"
            "<div class='actions'><button class='btn' type='submit'>Verify signed approval and approve</button></div>"
            "</form>",
        ),
        (
            "Arm this exact plan",
            "<form method='post' action='/actions/scalper-live-arm' class='form'>"
            f"<input type='hidden' name='nonce' value='{fmt.attr(nonce)}'>"
            + "<div class='form-row'>"
            + field("confirmation", "Arming confirmation", field_id="one-shot-arm-phrase", placeholder=str(scalper.get("arm_phrase") or ""))
            + field("intent_id", "Intent id", field_id="one-shot-arm-intent", value=intent_id)
            + "</div>"
            "<div class='actions'><button class='btn btn--danger' type='submit'>Arm this exact plan</button></div>"
            "</form>",
        ),
    ]
    body += "<div class='form-steps'>" + "".join(
        f"<div class='form-step'><span class='fs-n'>{index}</span>{form}</div>"
        for index, (_label, form) in enumerate(steps, start=1)
    ) + "</div>"
    body += (
        "<div class='actions' style='margin-top:10px'>"
        + action_form("scalper-live-submit", nonce, "Submit one guarded intent", tone="danger")
        + action_form("scalper-live-reconcile", nonce, "Reconcile the same request id")
        + "</div>"
        '<p class="small muted" style="margin-top:6px">An ambiguous request is reconciled by the '
        "same durable id; it is never re-sent.</p>"
    )

    proposals = [dict(item) for item in (scalper.get("proposals") or ())]
    body += (
        "<h3 class='small muted' style='margin:14px 0 6px' "
        "aria-label='One-shot LIVE intent lifecycle'>One-shot intent lifecycle</h3>"
    )
    body += table(
        (("Intent", False), ("Status", False), ("Candidate", False), ("Evidence", False), ("Reviewed", False), ("Approved", False), ("Armed", False)),
        [
            [
                cell(fmt.text(item.get("intent_id") or "—"), label="Intent", mono=True),
                cell(fmt.text(item.get("status") or "—"), label="Status"),
                cell(fmt.text(item.get("candidate_id") or "—"), label="Candidate", mono=True),
                cell(fmt.text(item.get("evidence") or "—"), label="Evidence", mono=True),
                cell("yes" if item.get("reviewed") else "no", label="Reviewed"),
                cell("yes" if item.get("approved") else "no", label="Approved"),
                cell("yes" if item.get("armed") else "no", label="Armed"),
            ]
            for item in proposals
        ],
        caption="Durable exact-plan proposals.",
        empty=empty_state("No proposal is on record", "Register an exact intent to create one."),
    )

    unresolved = [str(item) for item in (view.get("unresolved") or ())]
    filled = broker.get("volume_filled", broker.get("volume"))
    result = dict(reconciliation.get("result") or {})
    order_record = dict(reconciliation.get("order_record") or {})
    body += (
        "<h3 class='small muted' style='margin:18px 0 6px'>"
        "Broker response and unresolved requests</h3>"
        "<p class='small muted' style='margin:0 0 6px'>Last guarded AX-SCALPER action</p>"
    ) + facts(
        split=True,
        pairs=[
            ("Broker state", fmt.text(broker.get("state") or "not reported")),
            ("Broker request id", f"<code>{fmt.text(broker.get('request_id') or '—')}</code>"),
            ("Broker retcode", fmt.text(broker.get("retcode_name") or broker.get("retcode") or "Broker retcode not reported")),
            (
                "Filled volume",
                f"{fmt.text(filled)}" if filled not in (None, "") else "Filled volume not reported",
            ),
            ("Order record state", fmt.text(order_record.get("state") or "not reported")),
            (
                "Reconciliation",
                "reconciled" if result.get("reconciled") else "Reconciliation not recorded",
            ),
            (
                "Unresolved broker requests",
                fmt.text(", ".join(unresolved)) if unresolved else "No unresolved broker requests",
            ),
            (
                "Last durable event",
                fmt.text(str(last_event.get("event_type") or last_event.get("kind") or "Last durable event not recorded")),
            ),
        ]
    )
    if scalper.get("blockers"):
        body += blockers(runner_page.blocker_items(scalper.get("blockers") or ()))

    return section(
        title,
        body,
        note="One-shot, not a session: one typed intent, one review, one submission, then it is "
        "over — it never loops and it is not the continuous autopilot above. LIVE and PAPER stay "
        "separate: this section never promotes a PAPER result, every step needs its own phrase, "
        "and a blocked state sends nothing.",
        tools=chip("ONE-SHOT", "danger" if one_shot else "muted"),
        id="one-shot",
    )


def _last_path_section(view: Mapping[str, Any]) -> str:
    outcome = dict(view.get("last_outcome") or {})
    refusal = dict(view.get("last_refusal") or {})
    if not outcome and not refusal:
        return section(
            "The last order path",
            empty_state(
                "No order attempt yet",
                "Nothing has been sent in this session. Nothing is retried, and an unknown "
                "outcome stops the session rather than re-sending it.",
            ),
            id="last-path",
        )
    parts: list[str] = []
    if refusal:
        parts.append(
            notice(
                "Last LIVE refusal",
                "Arming or an order was refused. "
                f"{fmt.text(refusal.get('message') or refusal.get('reason') or '')} — "
                "No order was sent; the refusal above is the last recorded outcome.",
                tone="warn",
            )
        )
        rows = [
            ("At", f"<code>{fmt.text(refusal.get('at') or 'not recorded')}</code>"),
            ("Reason", f"<code>{fmt.text(refusal.get('reason') or '—')}</code>"),
            ("Order state", "No order attempt yet — the refusal happened before anything was sent."),
        ]
        parts.append(facts(rows))
        if refusal.get("blockers"):
            parts.append(blockers(runner_page.blocker_items(refusal.get("blockers") or ())))
    if outcome:
        parts.append(
            facts(
                [
                    ("Most recent live outcome", fmt.text(str(outcome.get("state") or "recorded"))),
                    ("At", fmt.text(fmt.utc(outcome.get("at"))) if outcome.get("at") else "not recorded"),
                    ("Reason", f"<code>{fmt.text(outcome.get('reason') or '—')}</code>"),
                    ("Retcode", fmt.text(outcome.get("retcode") or "not reported")),
                    ("Filled", fmt.text(outcome.get("filled") or "0")),
                    ("Ledger", fmt.text(outcome.get("ledger") or "—")),
                ]
            )
        )
    return section(
        "The last order path",
        "".join(parts),
        note="An unknown outcome stops the session and is never re-sent. There is no retry "
        "button anywhere on this page.",
        id="last-path",
    )


def autonomous_scalper_section(view: Mapping[str, Any], nonce: str) -> str:
    """One REAL-only AX-SCALPER run surface, backed by its owner-thread host."""

    scalper = dict(view.get("scalper_autonomous") or {})
    state = str(scalper.get("state") or "UNAVAILABLE")
    config = dict(scalper.get("config") or {})
    account = dict(scalper.get("account") or {})
    tick = dict(scalper.get("latest_tick") or {})
    run = dict(scalper.get("run") or {})
    blockers_list = [dict(item) for item in (scalper.get("blockers") or ())]
    run_blockers = [dict(item) for item in (run.get("blockers") or ())]
    known = {str(item.get("reason") or "") for item in blockers_list}
    blockers_list.extend(item for item in run_blockers if str(item.get("reason") or "") not in known)
    candidates = [dict(item) for item in (scalper.get("eligible_candidates") or ())]
    all_candidates = [dict(item) for item in (scalper.get("candidates") or ())]
    positions = [dict(item) for item in (run.get("open_positions") or scalper.get("open_positions") or ())]
    pending_entry = run.get("pending_submission") or scalper.get("pending_submission")
    pending_closes = [dict(item) for item in (run.get("pending_closes") or ())]
    adapter_armed = bool(scalper.get("adapter_armed"))
    active = state in {"RUNNING", "PAUSED", "ARMED", "RECOVERY_REQUIRED"}
    selected_id = str(scalper.get("candidate_id") or "")

    tone = "danger" if state == "RUNNING" else "warn" if state in {"PAUSED", "RECOVERY_REQUIRED"} else "ok" if state == "ARMED" else "muted"
    readouts = [
        readout("Runner", chip(state, tone, dot=state == "RUNNING"), foot="one strategy, one verified symbol", tone="live" if state == "RUNNING" else ""),
        readout("Account", chip(str(scalper.get("account_mode") or "UNKNOWN"), "danger" if str(scalper.get("account_mode") or "").upper() != "REAL" else "ok", dot=True), foot="REAL only; DEMO is refused", small=True),
        readout("MT5 connection", chip("connected" if scalper.get("connected") else "not connected", "ok" if scalper.get("connected") else "warn"), foot=fmt.text(scalper.get("account_server") or "server not reported"), small=True),
        readout("Quote", chip("fresh" if tick.get("fresh") else "unavailable / stale", "ok" if tick.get("fresh") else "warn"), foot=f"{fmt.text(tick.get('age_ms') if tick.get('age_ms') is not None else '—')} ms · {fmt.text(tick.get('symbol') or config.get('symbol') or 'no symbol')}", small=True),
        readout("Managed positions", fmt.text(len(positions)), foot="broker positions remain open after Stop", small=True),
    ]

    account_pairs = [
        ("Terminal account type", fmt.text(scalper.get("account_mode") or "not reported")),
        ("Declared server", fmt.text(scalper.get("account_server") or account.get("expected", {}).get("server") or "not reported")),
        ("Account currency", fmt.text(scalper.get("account_currency") or account.get("expected", {}).get("currency") or "not reported")),
        ("Account fingerprint", f"<code>{fmt.text(scalper.get('account_fingerprint') or 'not reported')}</code>"),
        ("LIVE mode confirmed", chip("yes", "ok") if scalper.get("live_effective") and str(scalper.get("mode") or "").upper() == "LIVE" else chip("no", "warn")),
        ("Adapter armed", chip("yes", "danger", dot=True) if adapter_armed else chip("no", "muted")),
        ("Symbol", fmt.text(config.get("symbol") or "not configured")),
        ("Broker-confirmed stop setting", "read from MT5 at arm and checked against each strategy stop"),
        ("Settings file", f"<code>{fmt.text(scalper.get('settings_path') or 'not wired')}</code>"),
        ("Recovery file", f"<code>{fmt.text(scalper.get('state_path') or 'not wired')}</code>"),
    ]
    config_body = facts(split=True, pairs=account_pairs)
    risk = dict(config.get("risk") or {})
    costs = dict(config.get("costs") or {})
    if risk or costs:
        rows = [(f"Risk · {key}", fmt.text(value)) for key, value in risk.items()]
        rows.extend((f"Declared cost · {key}", fmt.text(value)) for key, value in costs.items())
        config_body += "<details class='fold'><summary><span class='fold-h'>Declared risk and broker costs</span><span class='fold-n'>the local settings values used by the runner</span></summary><div class='fold-body'>" + facts(split=True, pairs=rows) + "</div></details>"

    blocker_html = blockers_with_fix(blockers_list)
    if not blocker_html and state not in {"RUNNING", "ARMED"}:
        blocker_html = notice("No readiness blockers reported", "The runner still checks the account, quote, risk, strategy admission, and broker protections before each order.", tone="ok")

    execution_mode = str(view.get("mode") or scalper.get("mode") or "PAPER").upper()
    if state == "RUNNING":
        readiness_banner = notice(
            "AX-SCALPER decision loop is running",
            "Every possible order still needs a fresh quote, an admitted strategy, current risk approval and broker-confirmed protection.",
            tone="danger",
        )
    elif state == "ARMED":
        readiness_banner = notice(
            "Armed, not started",
            "No entry loop is running. A separate explicit Start action is required before this lane can make trading decisions.",
            tone="warn",
        )
    else:
        readiness_banner = notice(
            "No real order can be sent right now",
            "The AX-SCALPER REAL runner is not running. Its account, evidence, configuration, recovery and quote gates must pass before Arm and Start are offered.",
            tone="info",
        )
    if execution_mode != "LIVE":
        readiness_banner += notice(
            "Before you use this page",
            "Paper mode is the default. It cannot send a real order; the REAL-only runner requires a confirmed LIVE mode and an MT5 account positively identified as REAL.",
            tone="warn",
        )

    candidate_rows = [
        [
            cell(fmt.text(item.get("name") or item.get("candidate_id") or item.get("fingerprint") or "—"), label="Strategy"),
            cell(fmt.text(item.get("family") or "not reported"), label="Family"),
            cell(fmt.text(item.get("timeframe") or "not reported"), label="Timeframe"),
            cell(chip("ADMITTED" if item.get("eligible") else "BLOCKED", "ok" if item.get("eligible") else "warn"), label="Admission"),
            cell(fmt.text(item.get("reason") or item.get("rationale") or item.get("evidence_note") or "Signed replay review required"), label="Evidence / reason"),
        ]
        for item in all_candidates
    ]
    options = [("", "Choose an admitted strategy")] + [
        (str(item.get("fingerprint") or ""),
         f"{item.get('name') or item.get('candidate_id') or 'AX-SCALPER candidate'} · {item.get('family') or 'family unavailable'}")
        for item in candidates if item.get("fingerprint")
    ]

    controls = ""
    if candidates and not blockers_list and not adapter_armed and not positions:
        controls += (
            "<h3>Arm this exact admitted strategy</h3><p class='small muted'>Arming changes no broker position and sends no order. It requires the exact LIVE account fingerprint and phrase.</p>"
            + action_form("ax-scalper-live-arm", nonce, "Arm AX-SCALPER for this REAL account", tone="danger",
                          fields=field("candidate_id", "Admitted strategy", kind="select", options=options, required=True, field_id="ax-scalper-arm-candidate")
                          + field("account_confirmation", "Type the exact account fingerprint", required=True, field_id="ax-scalper-arm-account", value=str(scalper.get("account_fingerprint") or ""))
                          + field("confirmation", "Type ARM AX-SCALPER LIVE", required=True, field_id="ax-scalper-arm-phrase", placeholder="ARM AX-SCALPER LIVE"))
        )
    if state == "ARMED" and adapter_armed and candidates and not blockers_list:
        controls += (
            "<h3>Start autonomous entries</h3><p class='small muted'>This begins continuous decisions on fresh live MT5 quotes. Every entry uses the guarded adapter, declared risk and broker-confirmed protection.</p>"
            + action_form("ax-scalper-live-start", nonce, "Start AX-SCALPER LIVE", tone="danger",
                          fields=field("candidate_id", "Same admitted strategy", kind="select", options=options, value=selected_id, required=True, field_id="ax-scalper-start-candidate")
                          + field("account_confirmation", "Repeat the exact account fingerprint", required=True, field_id="ax-scalper-start-account", value=str(scalper.get("account_fingerprint") or ""))
                          + field("confirmation", "Type START AX-SCALPER LIVE", required=True, field_id="ax-scalper-start-phrase", placeholder="START AX-SCALPER LIVE"))
        )
    if active or adapter_armed or positions:
        controls += action_form("ax-scalper-live-stop", nonce, "STOP new entries · keep managing open positions", tone="warn")
    if pending_entry:
        controls += action_form("ax-scalper-live-reconcile", nonce, "Reconcile the same pending entry request")
    for index, item in enumerate(pending_closes):
        position_id = str(item.get("position_id") or "")
        if position_id:
            controls += action_form("ax-scalper-live-reconcile-close", nonce,
                                    f"Reconcile pending close · {position_id}",
                                    fields=f"<input type='hidden' name='position_id' value='{fmt.attr(position_id)}'>")

    decisions = list(run.get("decisions") or ())
    latest_decision = decisions[-1] if decisions else {}
    if isinstance(latest_decision, Mapping):
        decision = dict(latest_decision)
    else:
        decision = {}
    last = dict(scalper.get("last_result") or {})
    quote_pairs = [
        ("Latest quote", f"{fmt.text(tick.get('symbol') or '—')} · bid {fmt.text(tick.get('bid') or '—')} / ask {fmt.text(tick.get('ask') or '—')}"),
        ("Quote timestamp", fmt.text(tick.get("source_time") or "not received")),
        ("Received", fmt.text(tick.get("received_at") or "not received")),
        ("Freshness", f"{fmt.text(tick.get('age_ms') if tick.get('age_ms') is not None else '—')} ms · {fmt.text('fresh' if tick.get('fresh') else 'stale/unavailable')}"),
        ("Last decision", fmt.text(decision.get("signal") or decision.get("decision") or last.get("signal") or "not reported")),
        ("Decision reason", fmt.text(decision.get("reason") or decision.get("blocked") or last.get("blocked") or scalper.get("reason") or "not reported")),
        ("Run pause / refusal", fmt.text(run.get("pause_reason") or run.get("stop_reason") or scalper.get("reason") or "none reported")),
        ("Starting equity", fmt.text(run.get("starting_equity") or "unavailable")),
        ("Current equity", fmt.text(run.get("equity") or "unavailable")),
        ("Risk state", fmt.text(run.get("guardian") or "see the live risk blockers above")),
    ]
    activity = facts(split=True, pairs=quote_pairs)
    last_result_html = (
        notice(
            "Last AX-SCALPER runner action",
            fmt.text(last.get("message") or last.get("reason") or "not reported"),
            tone="ok" if last.get("ok") else "warn",
        )
        if last
        else ""
    )
    position_rows = [
        [
            cell(fmt.text(item.get("position_id") or item.get("ticket") or "—"), label="Position", mono=True),
            cell(fmt.text(item.get("symbol") or config.get("symbol") or "—"), label="Symbol"),
            cell(fmt.text(item.get("side") or "—"), label="Side"),
            cell(fmt.text(item.get("lots") or item.get("quantity") or "—"), label="Lots", num=True),
            cell(fmt.text(item.get("entry_price") or "—"), label="Entry", num=True),
            cell(fmt.text(item.get("stop_price") or "—"), label="Broker stop", num=True),
            cell(fmt.text(item.get("unrealized_pnl") or "unavailable"), label="Unrealized P&L", num=True),
        ] for item in positions
    ]
    recovery = dict(run.get("recovery") or {})
    top = deck(
        "AX-SCALPER · REAL LIVE runner",
        id="ax-scalper-live", lane="live", state=state,
        note="one admitted strategy · live MT5 quotes · guarded real orders",
        readouts=readouts,
        body=(
            readiness_banner
            +
            notice("REAL account only", "This runner refuses DEMO, CONTEST, unknown account types, changed account identity, missing signed independent replay admission, stale quotes, and unverified broker protection. It never sends on Connect.", tone="danger", role="alert")
            + blocker_html
            + last_result_html
            + config_body
            + table(( ("Strategy", False), ("Family", False), ("Timeframe", False), ("Admission", False), ("Evidence / reason", False) ), candidate_rows,
                    caption="Candidates reviewed against the signed evidence policy and independent raw-data replay. Fixture test evidence is not admission.",
                    empty=empty_state("No strategy is admitted", "No strategy can trade until real signed evidence passes the independent replay and current policy."))
            + controls
            + (notice("Exact recovery required", fmt.text(recovery.get("detail") or "The persisted run cannot be resumed safely."), tone="danger") if recovery else "")
            + "<details class='fold' id='local-settings'><summary><span class='fold-h'>Local LIVE setup and missing values</span><span class='fold-n'>no capital, costs, contract size, or risk limits are guessed</span></summary><div class='fold-body'>"
            + notice("Settings are incomplete or unavailable", "Create the ignored local file shown below using broker-verified terms. Do not copy PAPER capital or use a demo account fingerprint.", tone="warn")
            + f"<p class='small'>Local LIVE settings: <code>{fmt.text(scalper.get('settings_path') or 'not wired')}</code></p>"
            + f"<p class='small'>Durable LIVE run state: <code>{fmt.text(scalper.get('state_path') or 'not wired')}</code>. Preserve it across restarts.</p>"
            + "<p class='small'>The exact blank template, field units, accepted cost labels and safe launch/update steps are in <code>docs/SCALPER_LIVE_WORKSTATION.md</code> in the project folder.</p>"
            + "<p class='small'>Required values: exact terminal-verified symbol; broker margin fraction; commission and whether observed or estimated; slippage points and state; swap state; every validated risk limit; and exact REAL server, login, currency and account fingerprint. The runner validates units and ranges and refuses missing fields.</p>"
            + "</div></details>"
        ),
        foot="Stop prevents new entries and never closes a position. If positions remain open, this process stays connected and keeps managing them from fresh quotes.",
    )
    return top + section("Live activity and managed positions", activity + table(
        (("Position", False), ("Symbol", False), ("Side", False), ("Lots", True), ("Entry", True), ("Broker stop", True), ("Unrealized P&L", True)),
        position_rows, caption="Positions managed by this exact AX-SCALPER run. P&L is shown only if the run reports it.",
        empty=empty_state("No managed position", "Open positions are never inferred from a PAPER ledger.")),
        note="The broker is authoritative for order and position state. Unknown outcomes stay latched and are queried by the same request id.", id="scalper-activity")


def legacy_live_stop_section(view: Mapping[str, Any], nonce: str) -> str:
    """Report and safely stop an older LIVE session; never offer it a new start."""

    continuous = dict(view.get("continuous") or {})
    running = bool(view.get("session_running") or continuous.get("state") in {"RUNNING", "STARTING"})
    armed = bool(view.get("armed"))
    if not running and not armed:
        return ""
    controls = ""
    if running:
        controls += action_form("autopilot-live-stop", nonce, "Stop the other LIVE session", tone="danger")
    if armed:
        controls += action_form("live-disarm", nonce, "Disarm the other LIVE plan", tone="warn")
    return section(
        "Other LIVE session already active",
        notice(
            "This older LIVE workflow cannot be started here",
            "It is shown only so an existing session can be stopped safely. AX-SCALPER will remain blocked until this session is stopped and the shared adapter is free.",
            tone="danger", role="alert",
        )
        + facts([
            ("Session", fmt.text(continuous.get("state") or view.get("session_state") or "not reported")),
            ("Order sending", chip("yes", "danger", dot=True) if continuous.get("order_sending") else chip("no", "muted")),
            ("Broker positions", fmt.text(len(view.get("positions") or continuous.get("positions") or ()))),
        ])
        + "<div class='actions'>" + controls + "</div>",
        note="Stopping disables that runner's new entries. Broker positions remain open and are never closed by Stop.",
        id="other-live-session",
    )



# --------------------------------------------------------------- demo test
def demo_test_section(ctx: Any, nonce: str) -> str:
    """The one-shot DEMO execution test, and nothing more than that.

    This is the page's only form that can reach a broker, so it says what it is
    before it says what it does: an execution smoke test, not a strategy, not
    AX-SCALPER, and no evidence at all about profitability.  Every value is
    typed, every value is judged against the broker's own terms, and the submit
    button only exists while nothing is blocking.
    """

    view = dict((ctx.extra or {}).get("demo_test") or {})
    if not view:
        return ""
    available = bool(view.get("available"))
    account = dict(view.get("account") or {})
    record = dict(view.get("record") or {})
    symbols = [dict(item) for item in (view.get("symbols") or ())]
    blocking = [dict(item) for item in (view.get("blockers") or ())]
    result = dict(view.get("result") or {})
    status = str(record.get("status") or "IDLE")
    mode = str(account.get("mode") or "UNCONFIRMED")
    fingerprint = str(account.get("fingerprint") or "")

    intro = (
        "<p class='inline-note' role='note'>"
        "<strong>Execution smoke test.</strong> One order, sent once, to a demo account "
        "MetaTrader 5 itself reports as DEMO — so you can see that AXIOM's order path "
        "reaches your terminal. It is <strong>not</strong> a strategy, <strong>not</strong> "
        "AX-SCALPER, and it is no evidence at all about profitability or about whether any "
        "strategy should trade."
        "</p>"
    )

    state_rows = [
        (
            "Account the terminal is on",
            chip(f"{mode}", "ok" if account.get("is_demo") else "danger", dot=True)
            + (
                f" <span class='mono small'>{fmt.text(fingerprint)}</span>"
                if fingerprint
                else ""
            )
            + (
                f" <span class='small muted'>{fmt.text(str(account.get('server') or ''))}</span>"
                if account.get("server")
                else ""
            ),
        ),
        (
            "Terminal confirms the account type",
            chip("confirmed", "ok") if account.get("confirmed") else chip("not confirmed", "warn"),
        ),
        (
            "Axiom execution mode",
            chip(str(dict(view.get("execution") or {}).get("name") or "UNKNOWN"),
                 "danger" if dict(view.get("execution") or {}).get("live_effective") else "warn"),
        ),
        (
            "Live trading armed",
            chip("ARMED", "danger", dot=True) if account.get("armed") else chip("not armed", "muted"),
        ),
        (
            "Last demo test",
            chip(status, "ok" if status in {"FILLED", "PARTIAL"} else "warn" if status in {"SUBMITTING", "UNRESOLVED"} else "muted")
            + (
                f" <span class='mono small'>{fmt.text(str(record.get('request_id') or ''))}</span>"
                if record.get("request_id")
                else ""
            ),
        ),
    ]

    if result:
        outcome_chip = (
            chip(str(result.get("status") or result.get("reason") or "refused"),
                 "ok" if result.get("ok") else "warn")
        )
        outcome = notice(
            "The last demo test action",
            outcome_chip
            + " "
            + fmt.text(str(result.get("message") or result.get("reason") or "")),
            tone="ok" if result.get("ok") else "warn",
            role="status" if result.get("ok") else "alert",
        )
    else:
        outcome = ""

    if not available:
        body = empty_state(
            "No demo test can be built here",
            str(view.get("message") or "This workstation has no MetaTrader 5 adapter."),
        )
        return section(
            "DEMO execution test — one order, DEMO account only",
            intro + body,
            note="Nothing on this page can reach a broker without an adapter.",
            id="demo-test",
        )

    options = [("", "Choose a broker symbol")]
    terms_lines: list[str] = []
    for item in symbols:
        pair = str(item.get("pair") or "")
        broker_symbol = str(item.get("broker_symbol") or "")
        if item.get("verified"):
            options.append((pair, f"{pair} · {broker_symbol}"))
            stop = dict(item.get("stop") or {})
            terms_lines.append(
                f"<li><strong>{fmt.text(pair)}</strong> as "
                f"<span class='mono'>{fmt.text(broker_symbol)}</span>: "
                f"{fmt.text(item.get('volume_min', ''))}–{fmt.text(item.get('volume_max', ''))} "
                f"lots, step {fmt.text(item.get('volume_step', ''))}, "
                f"{fmt.text(item.get('digits', ''))} digits, point "
                f"{fmt.text(item.get('point', ''))}, broker stop level "
                f"{fmt.text(item.get('stops_level', 0))} points, bid "
                f"{fmt.text(item.get('bid', ''))} / ask {fmt.text(item.get('ask', ''))}"
                + (
                    f", broker-side stop in force {stop.get('points')} points"
                    f" ({fmt.text(stop.get('declared', ''))})"
                    if stop.get("valid")
                    else ", <strong>no broker-side stop configured</strong>"
                )
                + "</li>"
            )
        else:
            terms_lines.append(
                f"<li class='muted'>{fmt.text(pair)} — not usable: "
                f"{fmt.text(str(item.get('reason') or 'not verified by the terminal'))}</li>"
            )

    terms = (
        "<details class='disclosure'><summary>The broker's current terms for these symbols</summary>"
        "<ul class='small'>" + "".join(terms_lines) + "</ul></details>"
        if terms_lines
        else ""
    )

    form = action_form(
        "demo-test-submit",
        nonce,
        "Send one DEMO test order",
        tone="danger",
        fields=(
            field(
                "symbol",
                "Broker symbol",
                kind="select",
                options=options,
                hint="Only symbols your provider setup resolved and the terminal confirms.",
            )
            + field(
                "side",
                "Direction",
                kind="select",
                options=[("", "Choose BUY or SELL"), ("BUY", "BUY"), ("SELL", "SELL")],
                hint="Never defaulted; the order goes the way you choose.",
            )
            + field(
                "volume",
                "Volume (lots)",
                hint="The broker's minimum, step and maximum are in the terms above.",
                placeholder="0.01",
            )
            + field(
                "stop_points",
                "Protective stop (points)",
                hint="Must be the broker-side stop in force for that symbol, above the "
                "broker's own minimum. An unprotected order is never sent.",
                placeholder="200",
            )
            + field(
                "confirmation",
                "Confirmation phrase",
                hint=f"Type exactly: {fmt.text(str(view.get('confirmation') or ''))}",
            )
            + field(
                "account_confirmation",
                "Account signature",
                hint="Repeat the short fingerprint in the panel above, so the test names one "
                "account.",
                value="",
            )
        ),
    )

    blocking_html = blockers(
        [
            {
                "title": str(item.get("message") or item.get("reason") or "Blocked"),
                "note": str(item.get("hint") or ""),
                "code": str(item.get("reason") or ""),
                "tone": "danger",
            }
            for item in blocking
        ],
        empty=(
            "No blocker stands between this page and a demo order right now: the terminal "
            "names a DEMO account, LIVE is confirmed and armed. The fields below are judged "
            "again at submission, and the broker's answer is the only result."
        ),
    )

    record_html = ""
    if record.get("request_id"):
        broker = dict(record.get("broker") or {})
        reconciliation = dict(record.get("reconciliation") or {})
        record_html = facts(
            [
                ("Request id (durable)", f"<span class='mono'>{fmt.text(str(record.get('request_id')))}</span>"),
                ("Record status", chip(status, "ok" if status in {"FILLED", "PARTIAL", "RECONCILED"} else "warn")),
                ("Broker retcode", fmt.text(str(broker.get("retcode") or "—"))
                 + (f" {fmt.text(str(broker.get('retcode_name')))}" if broker.get("retcode_name") else "")),
                ("Volume requested / filled", f"{fmt.text(str(broker.get('volume_requested') or '—'))} / "
                 f"{fmt.text(str(broker.get('volume_filled') or '—'))}"),
                ("Reconciled", "yes" if reconciliation else "not yet — reconcile the same id"),
            ],
            split=True,
        )
        reconcile_form = action_form(
            "demo-test-reconcile",
            nonce,
            "Reconcile the same request",
            small=True,
            title="Reads the durable order record and the broker's own order list by the same "
            "id. It never re-sends.",
        )
    else:
        reconcile_form = ""

    every_time = (
        "<ul class='small'>"
        "<li>The account must be one MetaTrader 5 itself reports as <strong>DEMO</strong>. "
        "REAL, CONTEST and an account MT5 cannot classify are refused at submission time.</li>"
        "<li>One order per record. A second submit is a duplicate and the page says so; an "
        "ambiguous answer is never re-sent, only reconciled by the same id.</li>"
        "<li>The order goes through the same gate, the same risk limits, the same router and "
        "the same adapter as a strategy order — this page adds no second way to a broker.</li>"
        "<li>Trading is disarmed as soon as the broker answers, whatever the answer was. If "
        "the order fills, the position stays open on your demo account, protected by the stop "
        "you selected; this page cannot close it.</li>"
        "</ul>"
    )

    return section(
        "DEMO execution test — one order, DEMO account only",
        intro
        + facts(state_rows, split=True)
        + terms
        + outcome
        + blocking_html
        + form
        + (f"<div class='demo-record'>{record_html}{reconcile_form}</div>" if record_html else "")
        + every_time,
        note="One order, sent through the same gate and adapter a strategy order uses. "
        "Not a strategy result, not an AX-SCALPER trade, not a profitability claim.",
        id="demo-test",
    )


def render_live(ctx: Any) -> str:
    view = dict((ctx.extra or {}).get("live") or {})
    nonce = fmt.attr(ctx.action_nonce)
    scalper = dict(view.get("scalper_autonomous") or {})
    state = str(scalper.get("state") or "UNAVAILABLE")
    head = page_head(
        "REAL LIVE · AX-SCALPER",
        "Autonomous scalping",
        "One admitted AX-SCALPER strategy, on verified live MT5 quotes, through the existing guarded order route. Only a terminal-confirmed REAL account is accepted. A demo account, missing signed replay evidence, unknown costs, or incomplete risk terms cannot start.",
        badges=chip("REAL ONLY", "danger", dot=True)
        + chip(str(scalper.get("account_mode") or "ACCOUNT UNKNOWN"), "danger" if str(scalper.get("account_mode") or "").upper() != "REAL" else "ok")
        + chip(state, "danger" if state == "RUNNING" else "warn" if state in {"PAUSED", "RECOVERY_REQUIRED"} else "muted"),
        actions=button_link("/autopilot", "Open PAPER", small=True),
    )
    index = jump_nav_bar(
        [("ax-scalper-live", "AX-SCALPER LIVE"),
         ("scalper-activity", "Activity & positions"),
         ("account", "Broker account"),
         ("broker-protection", "Broker protection")]
    )
    main = (
        broker_protection_status_section(view)
        +
        legacy_live_stop_section(view, nonce)
        + autonomous_scalper_section(view, nonce)
        + existing_order_reconciliation_section(view, nonce)
        + _account_section(view)
    )
    return head + index + workgrid(main, "")

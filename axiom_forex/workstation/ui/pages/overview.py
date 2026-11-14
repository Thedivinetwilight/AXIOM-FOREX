"""The overview: one screen of what this workstation is doing right now.

Four figures, then the terminal, the account mode, the market data, the paper
book, the runner, the broker's positions, the health checks and the last few
events.  Every figure is read from the dictionary the layer below produced, so
this page cannot disagree with Health, Activity, Positions or Performance.
"""

from __future__ import annotations

from typing import Any, Mapping

from .. import format as fmt
from ..components import (
    action_form,
    cell,
    chip,
    empty_state,
    facts,
    jump_nav,
    layout,
    notice,
    page_head,
    section,
    stat,
    stats,
    table,
)
from . import common
from . import monitoring
from . import runner as runner_page


def _connection_chip(ctx: Any) -> str:
    terminal = ctx.terminal
    if terminal.state == "CONNECTING":
        return chip("Connecting", "warn", dot=True)
    if terminal.state == "ERROR":
        return chip("Connection error", "danger", dot=True)
    if terminal.simulated:
        if terminal.connected:
            return chip("Simulator · not MT5", "simulated", dot=True)
        return chip("Simulator · not started", "simulated", dot=True)
    if not terminal.connected:
        return chip("Disconnected", "muted", dot=True)
    mode = terminal.mode if terminal.mode_confirmed else f"{terminal.mode}?"
    return chip(f"Connected · {mode}", "live", dot=True)


def _account_mode_chip(ctx: Any) -> str:
    terminal = ctx.terminal
    if not terminal.connected:
        return chip("No account mode", "muted")
    if terminal.simulated:
        return chip(f"Sample mode: {terminal.mode}", "simulated")
    if not terminal.mode_confirmed:
        return chip(f"Account mode: {terminal.mode} (unconfirmed)", "warn")
    tone = "ok" if terminal.mode == "DEMO" else "warn"
    return chip(f"Account mode: {terminal.mode}", tone)


def _kpis(ctx: Any, account: Mapping[str, Any]) -> str:
    terminal = ctx.terminal
    if terminal.simulated:
        state_value = "Preview running" if terminal.connected else "Preview stopped"
        state_foot = "Offline simulator — not MetaTrader 5"
    else:
        state_value = (
            "Connected"
            if terminal.connected
            else (
                "Connecting"
                if terminal.state == "CONNECTING"
                else ("Error" if terminal.state == "ERROR" else "Disconnected")
            )
        )
        state_foot = (
            f"Source: {fmt.text(terminal.source_label)}"
            if terminal.source_label
            else "Press Connect on the setup screen."
        )

    mode_value = terminal.mode if terminal.connected else "—"
    if terminal.simulated:
        mode_foot = "Sample value — no account was read"
    elif terminal.mode_confirmed:
        mode_foot = "Reported by the connected terminal"
    elif terminal.connected:
        mode_foot = "The terminal did not state a mode"
    else:
        mode_foot = "No terminal attached"

    selected_count = len(ctx.wizard.choices.selected_symbols)
    symbol_value = str(len(terminal.symbols)) if terminal.connected else "—"
    if not terminal.connected:
        symbol_foot = "Available after connecting"
    elif terminal.simulated:
        symbol_foot = (
            f"{selected_count} sample symbol(s) selected" if selected_count else "Sample symbols only"
        )
    elif selected_count:
        symbol_foot = f"{selected_count} selected for observation"
    else:
        symbol_foot = "None selected yet — choose on Account & symbols"

    equity = dict((account.get("metric_by_key") or {}).get("equity") or {})
    if equity:
        label_chip = chip("PAPER", "paper", dot=True)
        paper_value = fmt.text(equity.get("value") or "—")
        paper_foot = fmt.text(equity.get("note") or "paper ledger only")
    elif getattr(ctx.paper, "available", False):
        label_chip = common.data_chip(ctx.paper_label)
        paper_value = fmt.text(ctx.paper.equity or "—")
        paper_foot = f"{fmt.text(ctx.paper.account_currency or 'fixture')} · paper only"
    else:
        label_chip = ""
        paper_value = "—"
        paper_foot = "Paper platform unavailable"

    return stats(
        [
            stat("Terminal session", fmt.text(state_value), foot=fmt.text(state_foot), tone="accent"),
            stat("Selected account mode", fmt.text(mode_value), foot=fmt.text(mode_foot)),
            stat(
                "Sample symbols" if terminal.simulated else "Symbols on the terminal",
                fmt.text(symbol_value),
                foot=fmt.text(symbol_foot),
            ),
            stat("Paper equity", paper_value, foot=paper_foot, label_chip=label_chip),
        ]
    )


def _split_callout(ctx: Any) -> str:
    terminal = ctx.terminal
    if terminal.connected and not terminal.simulated:
        broker = "A real MetaTrader 5 account is connected (read-only)."
    elif terminal.simulated:
        broker = "The offline simulator is running, so even the quotes are samples."
    else:
        broker = "No terminal is connected, so there are no broker quotes at all."
    return (
        "<div class='split-callout' role='note'>"
        "<div><span class='callout-tag callout-tag--live'>REAL BROKER</span>"
        f"<span class='callout-text'>{fmt.text(broker)} Its balance and equity are never read "
        "into these figures.</span></div>"
        "<div><span class='callout-tag callout-tag--paper'>PAPER</span>"
        "<span class='callout-text'>Every order, fill, position and P&amp;L on this page is "
        "simulated on this machine. No order is sent and no broker capital is used.</span></div>"
        "</div>"
    )


def _terminal_section(ctx: Any) -> str:
    terminal = ctx.terminal
    if terminal.state == "CONNECTING":
        return section(
            "Terminal session",
            notice(
                "Connecting to the terminal…",
                "Waiting for MetaTrader 5 to answer. This page does not poll or retry on its own.",
                tone="info",
            ),
            id="terminal",
        )
    if terminal.state == "ERROR" and terminal.error:
        error = dict(terminal.error)
        return section(
            "Terminal session",
            notice(
                f"Connection failed: {error.get('reason', 'unknown')}",
                fmt.text(error.get("hint") or error.get("detail") or ""),
                tone="danger",
            )
            + f"<div class='actions'>{action_form('connect', ctx.action_nonce, 'Try again', tone='primary')}"
            + "</div>",
            id="terminal",
        )
    if not terminal.connected:
        return section(
            "Terminal session",
            empty_state(
                "No terminal connected",
                "AXIOM has not opened a terminal session. It connects only when you press "
                "Connect, using the MetaTrader 5 terminal you are already signed in to.",
                actions=action_form("connect", ctx.action_nonce, "Connect to MetaTrader 5", tone="primary"),
            ),
            note="Nothing on this page starts a session by itself.",
            id="terminal",
        )
    sample = bool(terminal.simulated)
    prefix = "Sample " if sample else ""
    return section(
        "Terminal session",
        facts(
            [
                (
                    "Connection",
                    f"{chip('Simulator', 'simulated', dot=True)}"
                    if sample
                    else f"{chip('Connected', 'ok', dot=True)}",
                ),
                (f"{prefix}Terminal", fmt.text(terminal.terminal_name or "MetaTrader 5")),
                (f"{prefix}Build", fmt.text(terminal.build or "unknown")),
                (f"{prefix}Broker / server", fmt.text(terminal.server or "unknown")),
                ("Data class", common.data_chip(ctx.terminal_label)),
                ("Source", fmt.text(terminal.source_label or "not reported")),
            ]
        )
        + (
            action_form("disconnect", ctx.action_nonce, "Stop simulator preview", tone="warn")
            if sample and terminal.connected
            else ""
        ),
        note="Read from the terminal's own record; this page never edits it.",
        id="terminal",
    )


def _mode_section(ctx: Any) -> str:
    terminal = ctx.terminal
    return section(
        "Execution mode",
        facts(
            [
                ("Selected account mode", _account_mode_chip(ctx)),
                (
                    "Data read",
                    "no account was read" if terminal.simulated else (
                        "reported by the terminal" if terminal.mode_confirmed else "not stated by the terminal"
                    ),
                ),
                (
                    "What the next order would do",
                    chip("send a real order", "danger", dot=True)
                    if getattr(ctx, "live_effective", False)
                    else chip("be simulated locally", "paper", dot=True),
                ),
            ]
        )
        + "<div class='actions'>"
        + f"<a class='btn btn-sm' href='/mode'>Change trading mode</a>"
        "</div>",
        note="Exactly one mode is in force. Switching sends nothing.",
        id="mode",
    )


def _market_section(ctx: Any) -> str:
    terminal = ctx.terminal
    symbols = tuple(terminal.symbols)
    rows = [
        [
            cell(fmt.text(name), label="Symbol", mono=True),
            cell(fmt.text(terminal.market_data or "NONE"), label="Data"),
        ]
        for name in list(symbols)[:12]
    ]
    return section(
        "Market data",
        facts(
            [
                ("Market data", chip(str(terminal.market_data or "NONE"), "muted")),
                ("Symbols reported", fmt.text(len(symbols)) if terminal.connected else "not connected"),
            ]
        )
        + (
            table(
                (("Symbol", False), ("Data", False)),
                rows,
                caption="Symbols the terminal reports for this session.",
                empty=empty_state("No symbols yet", "Connect the terminal to list its Market Watch."),
            )
            if rows
            else empty_state(
                "No symbols yet",
                "Symbols are listed once the terminal is connected and reports its Market Watch.",
            )
        ),
        note="Names are the broker's own and are never rewritten.",
        id="market",
    )


def _paper_section(ctx: Any) -> str:
    paper = ctx.paper
    if not getattr(paper, "available", False):
        return section(
            "Paper account",
            empty_state(
                "Paper platform unavailable",
                fmt.text(getattr(paper, "error", "") or "No status could be read."),
            ),
            id="paper",
        )
    label = ctx.paper_label or {}
    synthetic = str(label.get("label") or "").upper() == "SAMPLE FIXTURE"
    return section(
        "Fixture account (synthetic)" if synthetic else "Paper account",
        facts(
            [
                ("Equity", fmt.text(paper.equity)),
                ("Gross exposure", fmt.text(paper.gross_exposure)),
                ("Margin used", fmt.text(paper.margin)),
                ("Trades today", fmt.text(paper.trades_today)),
                ("Realised P&L today", fmt.text(paper.realized_pnl_today)),
                ("Currency", fmt.text(paper.account_currency or "—")),
                ("Data class", common.data_chip(label)),
                ("Source", f"<code>{fmt.text(paper.source or 'unknown')}</code>"),
            ]
        )
        + notice(
            "Not your paper ledger" if synthetic else "Simulated, not a broker account",
            "The example account declared by the synthetic fixture platform AXIOM ships with — "
            "an illustration, not your paper ledger. Your own paper results are the fills this "
            "machine recorded, on Paper performance."
            if synthetic
            else "Simulated paper ledger. Not broker positions and not a live account.",
            tone="muted",
        )
        + "<div class='actions'>"
        + ('<a class="btn btn-sm" href="/performance">Paper performance</a>' if synthetic else "")
        + "<a class=\"btn btn-sm\" href=\"/autopilot\">PAPER command center</a>"
        "</div>",
        id="paper",
    )


def _runner_section(ctx: Any) -> str:
    runner = dict(ctx.runner or {})
    if not runner:
        return section(
            "Strategy runner",
            empty_state(
                "No strategy runner is configured",
                "The PAPER command center on /autopilot starts the same catalog loop on one page.",
                actions="<a class='btn btn-sm' href='/autopilot'>Open the PAPER command center</a>",
            ),
            id="runner",
        )
    return section(
        "Strategy runner",
        facts(
            [
                ("State", runner_page.state_chip(runner)),
                ("Strategy", fmt.text(runner.get("strategy_id") or "none attached")),
                ("Symbols", fmt.text(len(runner.get("symbols") or ()))),
                ("Decisions today", fmt.text(runner.get("decisions_today", 0))),
                ("Last error", fmt.text(runner.get("last_error") or "none")),
            ]
        ),
        note="Read from the runner's own state, never inferred from a fill.",
        id="runner",
    )


def _positions_section(account: Mapping[str, Any]) -> str:
    positions = [dict(item) for item in (account.get("positions") or ())]
    rows = [
        [
            cell(fmt.text(item.get("symbol") or ""), label="Symbol", mono=True),
            cell(fmt.text(item.get("side") or ""), label="Side"),
            cell(fmt.text(item.get("quantity") or item.get("volume") or ""), label="Quantity", num=True),
            cell(fmt.text(item.get("open_price") or item.get("price_open") or ""), label="Open price", num=True),
            cell(fmt.text(item.get("unrealized_pnl") or item.get("profit") or ""), label="Unrealised", num=True),
        ]
        for item in positions
    ]
    return section(
        "Open paper positions",
        table(
            (("Symbol", False), ("Side", False), ("Quantity", True), ("Open price", True), ("Unrealised", True)),
            rows,
            caption="Paper positions from the local ledger.",
            empty=empty_state(
                "No paper position is open",
                "A simulated fill would appear here and on Positions; nothing is placed by this page.",
            ),
        ),
        id="positions",
    )


def _health_section(ctx: Any) -> str:
    checks = tuple(getattr(ctx.paper, "health_checks", ()) or ())
    failing = [item for item in checks if not item[1]]
    rows = [
        [
            cell(fmt.text(str(item[0]).replace("_", " ")), label="Check"),
            cell(chip("pass" if item[1] else "fail", "ok" if item[1] else "danger"), label="Result"),
            cell(fmt.text(item[2]), label="Note"),
        ]
        for item in checks[:8]
    ]
    return section(
        "Health checks",
        table(
            (("Check", False), ("Result", False), ("Note", False)),
            rows,
            caption="The platform's own checks, as it reported them.",
            empty=empty_state("No checks were reported", "Nothing is assumed healthy in their absence."),
        ),
        note="A failing check refuses the order it governs; none of them stops the mode switch.",
        tools=chip(f"{len(failing)} failing", "danger" if failing else "ok"),
        id="health",
    )


def _activity_section(account: Mapping[str, Any]) -> str:
    events = [dict(item) for item in (account.get("events") or ())][:8]
    rows = [
        [
            cell(fmt.text(monitoring._event_label(str(item.get("event_type") or ""))[0]), label="Event"),
            cell(fmt.text(item.get("pair") or item.get("symbol") or "—"), label="Symbol", mono=True),
            cell(fmt.text(item.get("event_at") or item.get("at") or ""), label="When", mono=True),
        ]
        for item in reversed(events)
    ]
    return section(
        "Latest ledger events",
        table(
            (("Event", False), ("Symbol", False), ("When", False)),
            rows,
            caption="The last few events in the local paper ledger, newest first.",
            empty=empty_state(
                "No ledger event yet",
                "Nothing has been recorded on this machine; the activity page lists every event.",
            ),
        ),
        id="activity",
    )


def render_overview(ctx: Any) -> str:
    account = common.account_view(ctx)
    terminal = ctx.terminal

    head = page_head(
        "Overview",
        "What this workstation is doing",
        "Four figures, then the terminal, the account mode, the market data, the paper book and "
        "the checks behind them. Nothing on this page starts a session, changes a mode or sends "
        "an order.",
        badges=_connection_chip(ctx)
        + _account_mode_chip(ctx)
        + common.data_chip(ctx.paper_label),
        actions="<a class='btn btn-sm' href='/'>Guided start</a>"
        "<a class='btn btn-sm btn--primary' href='/autopilot'>PAPER command center</a>",
    )

    main = (
        _split_callout(ctx)
        + jump_nav(
            [
                ("terminal", "Terminal"),
                ("mode", "Mode"),
                ("market", "Market data"),
                ("paper", "Paper book"),
                ("positions", "Positions"),
                ("health", "Health"),
                ("activity", "Activity"),
            ]
        )
        + _terminal_section(ctx)
        + _mode_section(ctx)
        + _market_section(ctx)
        + _paper_section(ctx)
        + _runner_section(ctx)
        + _positions_section(account)
        + _health_section(ctx)
        + _activity_section(account)
    )
    aside = (
        section("Right now", _kpis(ctx, account), id="right-now", plain=True)
        + common.provenance_section(
            monitoring._ledger_facts(ctx, account),
            title="Where these numbers come from",
            note="Every instant is UTC. Nothing on this page is a broker balance.",
            id="provenance",
        )
        + common.safety_section(id="safety")
        + section(
            "Where to go next",
            "<div class='actions' style='flex-wrap:wrap'>"
            "<a class='btn btn-sm' href='/positions'>Positions</a>"
            "<a class='btn btn-sm' href='/performance'>Performance</a>"
            "<a class='btn btn-sm' href='/activity'>Activity</a>"
            "<a class='btn btn-sm' href='/health'>Health</a>"
            "<a class='btn btn-sm' href='/live'>LIVE workflow</a>"
            "</div>",
            id="next",
            plain=True,
        )
    )
    del terminal
    return head + layout(main, aside)

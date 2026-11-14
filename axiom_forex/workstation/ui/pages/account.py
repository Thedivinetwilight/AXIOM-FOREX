"""Account and symbols: what the terminal has selected, and what may be followed.

Three full-width sections, in the order the operator needs them: the account the
terminal already has selected, the verified forex pairs that can be observed (each
with its reason when it cannot), and the single confirmation that closes setup.

Nothing here switches account, logs in, reads a balance, or changes the execution
mode; the picker only ever offers a symbol the terminal described with both
currency legs, under the broker's own name.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from .. import format as fmt
from ..components import (
    action_form,
    button_link,
    chip,
    empty_state,
    facts,
    jump_nav,
    layout,
    notice,
    page_head,
    section,
)
from axiom_forex.workstation import symbols as symbol_tools

from . import common

#: One cap, owned by the catalogue module, so the page and its tests agree.
MAX_EXCLUDED_SHOWN = symbol_tools.MAX_EXCLUDED_SHOWN


def _catalogue(ctx: Any) -> Any:
    """What the session reports, with the read's own coverage counts kept."""

    session = getattr(ctx.wizard, "session", None)
    read = symbol_tools.SymbolRead()
    reader = getattr(session, "symbol_read", None)
    if callable(reader):
        try:
            read = reader() or read
        except Exception:  # a failed read yields no records, never a page error
            read = symbol_tools.SymbolRead()
    return symbol_tools.build_catalogue(
        read.records,
        sample=bool(getattr(ctx.terminal, "simulated", False)),
        reported_total=read.reported,
        examined=read.examined,
        specs_reported=read.bulk,
    )


def _account_section(ctx: Any) -> str:
    terminal = ctx.terminal
    view = dict(ctx.wizard.view())
    mode = dict(view.get("account_mode") or {})
    sample = bool(terminal.simulated)
    prefix = "Sample " if sample else ""

    if not terminal.connected:
        return section(
            "Detected account",
            empty_state(
                "No account detected",
                (
                    "This is the offline simulator: start the preview to see sample values, or "
                    "launch on the Windows machine without --simulate to read the real terminal."
                    if sample
                    else "Connect to the terminal first. AXIOM reads the account that MetaTrader 5 "
                    "already has selected; it never switches or logs in."
                ),
                actions=action_form(
                    "connect",
                    ctx.action_nonce,
                    "Start the simulator preview (not MT5)" if sample else "Connect to MetaTrader 5",
                    tone="primary",
                )
                + button_link("/setup", "Setup steps", small=True),
            ),
            tools=chip("Not connected", "muted"),
            note="What the terminal already has selected. AXIOM reads it; it never switches "
            "account, logs in, or changes the mode.",
            id="account",
        )

    heading = (
        f"Sample account mode: {mode.get('detected')}"
        if sample
        else f"Detected account mode: {mode.get('detected')}"
    )
    note = (
        "Illustrative value from the offline simulator. No account was read, and nothing here "
        "describes a real broker account."
        if sample
        else str(mode.get("note") or "")
    )
    rows = [
        (
            f"{prefix}Account fingerprint",
            f"<code>…{fmt.text(terminal.fingerprint)}</code> {chip('login redacted', 'muted')}",
        ),
        (f"{prefix}Broker / server", fmt.text(terminal.server or "unknown")),
        (f"{prefix}Currency", fmt.text(terminal.currency or "unknown")),
        (f"{prefix}Leverage", fmt.text(terminal.leverage or "unknown")),
        (f"{prefix}Margin accounting", fmt.text(terminal.margin_mode or "unknown")),
        (
            f"{prefix}Account mode",
            fmt.text(terminal.mode)
            + ("" if terminal.mode_confirmed or sample else " (unconfirmed)"),
        ),
        (f"{prefix}Terminal build", fmt.text(terminal.build or "unknown")),
        (
            f"{prefix}Terminal trading permission",
            fmt.text("allowed" if terminal.trade_allowed else "not allowed"),
        ),
    ]
    return section(
        "Detected account",
        notice(
            heading,
            fmt.text(note),
            tone="warn",
        )
        + facts(rows)
        + "<p class='small muted' style='margin:12px 0 0'>Balances, equity and profit are never "
        "read into this interface, and the account number is shown only as a fingerprint."
        + (" Sample values exist so the layout can be reviewed offline." if sample else "")
        + "</p>",
        tools=(
            chip(str(mode.get("detected") or "UNKNOWN"), "simulated" if sample else ("ok" if mode.get("is_demo") else "warn"), dot=True)
        ),
        note="What the terminal already has selected. AXIOM reads it; it never switches account, "
        "logs in, or changes the mode.",
        id="account",
    )


def _tiles(catalogue: Any, selected: Sequence[str], *, sample: bool) -> str:
    chosen = {str(name) for name in selected if str(name)}
    tiles: list[str] = []
    for choice in catalogue.pairs:
        name = choice.broker_symbol
        tiles.append(
            f"<label class='pick'><input type='checkbox' name='symbol' value='{fmt.attr(name)}'"
            f"{' checked' if name in chosen else ''}>"
            f"<span>{fmt.text(choice.label)}<span class='why'>{fmt.text(name)}</span></span></label>"
        )
    if not tiles:
        return ""
    return f"<div class='pick-grid' role='group' aria-label='Verified forex pairs'>{''.join(tiles)}</div>"


def _excluded_list(catalogue: Any) -> str:
    """The names that cannot be selected, once, with the reason beside each.

    They are listed rather than tiled: a tile says only "not selectable", and the
    reason is the part worth reading.  A long list stays inside its own bounded
    box so the page keeps its shape on an account with hundreds of symbols.
    """

    if not catalogue.excluded:
        return ""
    shown = list(catalogue.excluded)[:MAX_EXCLUDED_SHOWN]
    hidden = len(catalogue.excluded) - len(shown)
    rows = "".join(
        f"<li><code>{fmt.text(item.get('broker_symbol') or '')}</code> — "
        f"{fmt.text(item.get('reason') or 'no reason reported')}</li>"
        for item in shown
    )
    return (
        f"<p class='small muted' style='margin:14px 0 0'>"
        f"{len(catalogue.excluded)} reported symbol(s) not selectable, with the reason:</p>"
        "<ul class='scroll-list small' tabindex='0' role='group' "
        "aria-label='Reported symbols that cannot be selected, with the reason' "
        "style='margin:6px 0 0;padding-left:22px'>"
        f"{rows}</ul>"
        + (
            f"<p class='small muted' style='margin:6px 0 0'>The other {hidden} excluded "
            "symbol(s) are counted but not listed here, to keep this page usable. Every "
            "reported symbol was classified.</p>"
            if hidden
            else ""
        )
    )


def _symbols_section(ctx: Any, catalogue: Any) -> str:
    view = dict(ctx.wizard.view())
    selected = tuple(view.get("symbols_selected") or ())
    sample = bool(ctx.terminal.simulated)
    counts = dict(catalogue.counts)
    header = (
        f"<p class='small muted' style='margin:0 0 10px'>"
        f"{fmt.text(symbol_tools.catalogue_message(catalogue))}</p>"
    )
    chip_row = (
        "<div class='actions' style='margin:0 0 10px'>"
        + chip(f"{len(selected)} selected", "ok" if selected else "warn")
        + chip(f"{int(counts.get('reported') or 0)} reported", "muted")
        + (
            chip(f"{int(counts.get('verified_forex_pairs') or 0)} verified pair(s)", "paper")
            if catalogue.verified
            else chip("names only", "warn")
        )
        + "</div>"
    )
    if sample:
        header += (
            "<p class='small muted' style='margin:0 0 10px'>Sample symbols from the offline "
            "simulator. They carry no specification, so none of them can be verified as a currency "
            "pair, and none of them describes a real broker's symbol.</p>"
        )

    if not catalogue.available:
        body = header + chip_row + empty_state(
            "No symbols yet",
            "Symbols are listed once the terminal is connected and reports its Market Watch.",
        )
        return section(
            "Symbols to observe",
            body,
            note="Only a symbol the terminal describes with both currency legs can be selected.",
            id="symbols",
        )

    body = header + chip_row
    if catalogue.pairs:
        body += _tiles(catalogue, selected, sample=sample)
        # The save sits directly under the grid it saves, before the bulk controls.
        body += (
            "<form method='post' action='/actions/symbols' class='actions' style='margin-top:10px'>"
            f"<input type='hidden' name='nonce' value='{fmt.attr(ctx.action_nonce)}'>"
            "<button class='btn btn--primary' type='submit'>Save selection</button></form>"
        )
    if not catalogue.pairs:
        body += (
            "<p class='small muted' style='margin:10px 0 0'>No symbol reported by the terminal "
            "could be verified as a currency pair, so nothing here can be selected.</p>"
        )

    bulk = (
        "<form method='post' action='/actions/symbols' class='actions' style='margin-top:10px'>"
        f"<input type='hidden' name='nonce' value='{fmt.attr(ctx.action_nonce)}'>"
        "<button class='btn' type='submit' name='bulk' value='all'"
        + ("" if catalogue.select_all_available else " disabled aria-disabled='true'")
        + ">Select all verified forex pairs</button>"
        "<button class='btn' type='submit' name='bulk' value='clear'"
        + ("" if selected else " disabled aria-disabled='true'")
        + ">Clear selection</button></form>"
    )
    body += bulk
    if catalogue.select_all_available:
        body += (
            "<p class='small muted' style='margin:6px 0 0'>Select all takes "
            f"{counts['selectable_for_all']} verified forex pair(s) this account can trade. Names "
            "are the broker's own and are never rewritten.</p>"
        )
    elif catalogue.pairs:
        body += (
            "<p class='small muted' style='margin:6px 0 0'>Select all is unavailable: no verified "
            "pair is reported as tradable with a full specification.</p>"
        )
    if catalogue.skipped_by_select_all:
        items = "".join(
            f"<li><code>{fmt.text(item['broker_symbol'])}</code> — {fmt.text(item['reason'])}</li>"
            for item in catalogue.skipped_by_select_all
        )
        body += (
            "<p class='small muted' style='margin:6px 0 0'>Left out of Select all:</p>"
            f"<ul class='small muted' style='margin:4px 0 0;padding-left:18px'>{items}</ul>"
        )

    body += _excluded_list(catalogue)

    if sample:
        body += (
            "<p class='small muted' style='margin:10px 0 0'>The selection is kept in memory for "
            "this session only, and a simulator preview cannot be confirmed.</p>"
        )
    return section(
        "Symbols to observe",
        body,
        note="Only a symbol the terminal describes with both currency legs can be selected: its "
        "own name, kept exactly, suffixes included. Changing the selection withdraws the "
        "confirmation below until you confirm again.",
        id="symbols",
    )


def _confirm_section(ctx: Any) -> str:
    terminal = ctx.terminal
    view = dict(ctx.wizard.view())
    selected = tuple(view.get("symbols_selected") or ())
    mode = dict(view.get("account_mode") or {})
    sample = bool(terminal.simulated)

    if not terminal.connected:
        return section(
            "Confirm configuration",
            empty_state(
                "Nothing to confirm",
                "Confirmation records that a real account and its symbols were reviewed. Connect "
                "to the terminal first.",
            ),
            note="Step 5 of setup.",
            id="confirm",
        )
    if sample:
        return section(
            "Confirm configuration",
            notice(
                "A simulator preview cannot be confirmed",
                "Confirmation is a statement about a real account. Start the workstation without "
                "--simulate, on the Windows machine that runs MetaTrader 5.",
                tone="warn",
            )
            + facts(
                [
                    ("Account", "not connected to a terminal"),
                    ("Symbols", fmt.text(", ".join(selected) or "none selected")),
                    ("Sample mode", fmt.text(str(mode.get("detected") or ""))),
                    ("Trading capability", "none"),
                ]
            ),
            tools=chip("NOT CONFIRMABLE", "muted"),
            note="Step 5 of setup.",
            id="confirm",
        )

    confirmed = bool(view.get("confirmed"))
    return section(
        "Confirm configuration",
        facts(
            [
                (
                    "Account",
                    fmt.text(terminal.server or "not connected")
                    + (f" · …{fmt.text(terminal.fingerprint)}" if terminal.fingerprint else ""),
                ),
                ("Symbols", fmt.text(", ".join(selected) or "none selected")),
                ("Mode (detected)", fmt.text(str(mode.get("detected") or ""))),
            ]
        )
        + (
            notice(
                "Configuration confirmed",
                f"Confirmed at {fmt.text(fmt.utc(view.get('configured_at')))}. You can re-confirm "
                "after changing anything.",
                tone="ok",
            )
            if confirmed
            else notice(
                "Not confirmed yet",
                "Confirm once the account and symbols match what you intend to observe.",
                tone="warn",
            )
        )
        + "<div class='actions' style='margin-top:12px'>"
        + (
            action_form("unconfirm", ctx.action_nonce, "Withdraw confirmation")
            if confirmed
            else action_form("confirm", ctx.action_nonce, "Confirm configuration", tone="primary")
        )
        + "</div>",
        tools=chip("CONFIRMED", "ok", dot=True) if confirmed else chip("NOT CONFIRMED", "warn"),
        note="Step 5 of setup. A change to the symbol set withdraws this confirmation.",
        id="confirm",
    )


def render_account(ctx: Any) -> str:
    catalogue = _catalogue(ctx)
    terminal = ctx.terminal
    view = dict(ctx.wizard.view())
    selected = tuple(view.get("symbols_selected") or ())
    sample = bool(terminal.simulated)

    head = page_head(
        "Account & symbols",
        "The account and the pairs it may follow",
        "Read from the terminal that is already signed in. This page never logs in, switches "
        "account, reads a balance, or changes the execution mode.",
        badges=common.scope_badges(ctx)
        + (
            chip(f"{len(selected)} selected", "ok" if selected else "warn")
            if terminal.connected
            else chip("Not connected", "muted")
        )
        + (chip("CONFIRMED", "ok", dot=True) if view.get("confirmed") else chip("Not confirmed", "muted")),
        # In a simulator preview the shell's top bar already carries the only
        # way out, so the section does not repeat that control.
        actions=(
            ""
            if sample and terminal.connected
            else button_link("/", "Start PAPER trading", small=True)
        ),
    )

    main = (
        jump_nav([("account", "Account"), ("symbols", "Symbols"), ("confirm", "Confirm")])
        + _account_section(ctx)
        + _symbols_section(ctx, catalogue)
        + _confirm_section(ctx)
    )
    return head + layout(main, wide=True)

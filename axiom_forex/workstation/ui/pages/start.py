"""The guided start page: five steps from a cold machine to a PAPER session.

The steps, their statuses and their blockers are built by
:mod:`axiom_forex.workstation.guide` from the same views every other page reads.
This module only decides how they look: one page head, one honest headline, one
row of primary controls, the standing summary, the five steps, the paper
performance summary and the collapsed list of every other page.

Nothing here connects on its own, changes the execution mode, or sends an order,
and the page says so in the one sentence an operator needs: LIVE is never started
here.
"""

from __future__ import annotations

from html import escape
from typing import Any, Mapping, Sequence

from .. import format as fmt
from ..components import (
    action_form,
    button_link,
    chip,
    empty_state,
    facts,
    layout,
    notice,
    page_head,
    section,
    stat,
    stats,
)
from axiom_forex.workstation import guide
from axiom_forex.workstation import symbols as symbol_tools

from . import common

STEP_TONES = {
    guide.DONE: ("Complete", "ok"),
    guide.RUNNING: ("Running", "paper"),
    guide.NEXT: ("Next", "warn"),
    guide.BLOCKED: ("Blocked", "warn"),
    guide.UNAVAILABLE: ("Unavailable", "muted"),
}


def _symbol_facts(ctx: Any, catalog: Mapping[str, Any]) -> dict[str, Any]:
    """How many symbols the terminal offers, without walking it a second time."""

    reported_view = dict((catalog.get("symbols") or {}) if catalog else {})
    if reported_view.get("available"):
        counts = dict(reported_view.get("counts") or {})
        return {
            "available": True,
            "reported": int(counts.get("reported") or 0),
            "select_all": int(counts.get("usable") or 0),
            "source": "catalog",
        }
    session = getattr(ctx.wizard, "session", None)
    read = symbol_tools.SymbolRead()
    reader = getattr(session, "symbol_read", None)
    if callable(reader):
        try:
            read = reader() or read
        except Exception:  # a read that fails yields no records, never a page error
            read = symbol_tools.SymbolRead()
    catalogue = symbol_tools.build_catalogue(
        read.records,
        sample=bool(getattr(ctx.terminal, "simulated", False)),
        reported_total=read.reported,
        examined=read.examined,
        specs_reported=read.bulk,
    )
    counts = dict(catalogue.counts)
    return {
        "available": bool(catalogue.available),
        "reported": int(counts.get("reported") or 0),
        "select_all": int(counts.get("selectable_for_all") or 0),
        "source": "picker",
    }


def _step_form(ctx: Any, step: Mapping[str, Any], *, primary: bool, candidates: Sequence[Mapping[str, Any]]) -> str:
    label = str(step.get("action_label") or "")
    if not label:
        return ""
    klass = "btn btn--primary" if primary else "btn"
    kind = str(step.get("action_kind") or "")
    if kind == "link":
        return f"<a class='{klass}' href='{fmt.attr(step.get('action_target') or '/')}'>{escape(label)}</a>"
    if kind != "form":
        return ""
    hidden = "".join(
        f"<input type='hidden' name='{fmt.attr(name)}' value='{fmt.attr(value)}'>"
        for name, value in (step.get("fields") or ())
    )
    if not step.get("fields") or not any(name == "return_to" for name, _v in step.get("fields") or ()):
        hidden += "<input type='hidden' name='return_to' value='/'>"
    choices = ""
    if candidates:
        boxes = "".join(
            "<label class='candidate'>"
            f"<input type='checkbox' name='paper' value='{fmt.attr(item.get('key') or '')}' checked>"
            "<span>"
            f"<strong>{escape(str(item.get('symbol') or ''))}</strong>"
            f" · {escape(str(item.get('timeframe') or ''))}"
            f" · {escape(str(item.get('family') or item.get('candidate') or ''))}"
            f"<span class='cand-meta'>{escape(str(item.get('summary') or item.get('candidate') or ''))}</span>"
            "</span></label>"
            for item in candidates
        )
        choices = (
            "<div class='candidate-list' role='group' aria-label='Screened candidates to paper trade'>"
            f"{boxes}</div>"
        )
    return (
        f"<form method='post' action='/actions/{fmt.attr(step.get('action_target') or '')}'>"
        f"<input type='hidden' name='nonce' value='{fmt.attr(ctx.action_nonce)}'>"
        + hidden
        + choices
        + f"<div class='actions'><button class='{klass}' type='submit'>{escape(label)}</button></div>"
        "</form>"
    )


def _step(ctx: Any, step: Mapping[str, Any], *, next_key: str, candidates: Sequence[Mapping[str, Any]]) -> str:
    state = str(step.get("state") or "")
    detail = str(step.get("detail") or "").strip()
    items = [str(item) for item in (step.get("blockers") or ())]
    if detail and items and items[0].strip() == detail:
        items = items[1:]
    shown = items[:4]
    more = len(items) - len(shown)
    blocker_html = ""
    if shown:
        blocker_html = (
            "<ul class='guide-blockers'>"
            + "".join(f"<li>{escape(item)}</li>" for item in shown)
            + (f"<li>and {more} more…</li>" if more > 0 else "")
            + "</ul>"
        )
    links = "".join(
        f"<a class='btn btn-sm btn--ghost' href='{fmt.attr(href)}'>{escape(str(label))}</a>"
        for label, href in (step.get("links") or ())
    )
    actions = _step_form(
        ctx, step, primary=str(step.get("key")) == next_key, candidates=candidates
    )
    why = str(step.get("why") or guide.STEP_WHY.get(str(step.get("key")), ""))
    return (
        f"<li class='guide-step guide-step--{fmt.attr(state)}' id='step-{fmt.attr(step.get('key') or '')}'>"
        "<div class='guide-head'>"
        f"<span class='guide-num' aria-hidden='true'>{escape(str(step.get('number') or ''))}</span>"
        "<div>"
        f"<h3>{escape(str(step.get('title') or ''))}"
        f"<span class='visually-hidden'> — step {escape(str(step.get('number') or ''))}</span></h3>"
        f"<p class='guide-why'>{escape(why)}</p></div>"
        f"<span class='guide-status'>{chip(str(step.get('status') or ''), STEP_TONES.get(state, ('', 'muted'))[1])}</span>"
        "</div>"
        + (f"<p class='guide-detail'>{escape(detail)}</p>" if detail else "")
        + blocker_html
        + (
            f"<div class='actions'>{actions}{links}</div>"
            if actions or links
            else ""
        )
        + "</li>"
    )


def _primary_controls(ctx: Any, model: Mapping[str, Any], account: Mapping[str, Any]) -> str:
    """The one row of controls, each with its own sentence underneath."""

    steps = {step.key: step for step in model.get("steps") or ()}
    paper_step = steps.get("paper")
    running = bool(paper_step is not None and paper_step.state == guide.RUNNING)
    parts: list[str] = []
    notes: list[tuple[str, str]] = []

    if not ctx.terminal.connected:
        label = "Start the simulator preview (not MT5)" if ctx.terminal.simulated else "Connect to MetaTrader 5"
        parts.append(action_form("connect", ctx.action_nonce, label, tone="primary"))
        notes.append((label, "reads the terminal you are already signed in to; it never logs in."))
    else:
        parts.append(
            "<button class='btn' type='button' disabled aria-disabled='true'>"
            + escape("Simulator preview running" if ctx.terminal.simulated else "Terminal connected")
            + "</button>"
        )
        notes.append(("Connection", "already connected: there is nothing to connect to twice."))

    if running:
        parts.append(action_form("catalog-paper-stop", ctx.action_nonce, "Stop paper trading", tone="warn"))
        notes.append(("Stop paper trading", "ends the paper loop; it closes nothing and sends nothing."))
    elif paper_step is not None and paper_step.state in {guide.NEXT, guide.RUNNING}:
        parts.append("<a class='btn' href='#step-paper'>Start paper trading</a>")
        notes.append(
            (
                "Start paper trading",
                "needs the ticked candidates in step 5, which is why that button goes there "
                "rather than starting anything here.",
            )
        )
    else:
        reason = (
            paper_step.detail
            if paper_step is not None and paper_step.detail
            else "The paper session cannot start yet."
        )
        parts.append(
            "<button class='btn' type='button' disabled aria-disabled='true'>Start paper trading</button>"
        )
        notes.append(("Start paper trading is disabled", reason))

    parts.append("<a class='btn' href='/performance'>View performance</a>")
    session = dict(account.get("session") or {})
    if not session.get("running"):
        notes.append(("View performance", "shows the ledger even while the session is stopped."))

    return (
        "<div class='actions' style='margin-top:14px'>"
        + "".join(parts)
        + "</div><ul class='list-reset small muted' style='margin:8px 0 0'>"
        + "".join(f"<li><strong>{escape(label)}</strong> {escape(text)}</li>" for label, text in notes)
        + "</ul>"
    )


def _performance_section(account: Mapping[str, Any]) -> str:
    metrics = dict(account.get("metric_by_key") or {})
    if not metrics:
        return ""
    counts = dict(account.get("counts") or {})
    figures = []
    for key in ("equity", "realized_gross", "net_pnl", "open_positions"):
        metric = dict(metrics.get(key) or {})
        if not metric:
            continue
        foot = fmt.text(metric.get("note") or "")
        if key == "open_positions":
            foot = (
                f"{counts.get('closed_trades', 0)} closed · "
                f"{counts.get('fills', 0)} simulated fill(s)"
            )
        figures.append(stat(fmt.text(metric.get("label") or key), fmt.text(metric.get("value") or "—"), foot=foot))
    capital = dict(account.get("capital") or {})
    setup = (
        ""
        if capital.get("configured")
        else "<p class='small' style='margin:12px 0 0'><a href='/performance'>Set the paper starting "
        "capital</a> to see cash, equity and return. No balance is guessed.</p>"
    )
    return section(
        "Paper performance",
        stats(figures)
        + setup
        + "<div class='actions' style='margin-top:12px'>"
        "<a class='btn btn-sm btn--primary' href='/performance'>View performance</a></div>",
        note="Paper ledger only. The broker account's balance and equity are never mixed into "
        "these figures.",
        tools=chip("PAPER", "paper", dot=True),
        id="performance",
    )


def _tiles_section(model: Mapping[str, Any]) -> str:
    tiles = tuple((model.get("summary") or {}).get("tiles") or ())
    rows = [
        (
            fmt.text(tile.get("label") or ""),
            f"<strong>{fmt.text(tile.get('value') or '—')}</strong>"
            + (f" <span class='small muted'>· {fmt.text(tile.get('detail') or '')}</span>" if tile.get("detail") else ""),
        )
        for tile in tiles
    ]
    return section(
        "Where things stand",
        facts(rows) if rows else empty_state("Nothing has been read yet", "Connect the terminal to begin."),
        note="One summary of the terminal, the paper session, the symbols and strategy, the market "
        "data, and what the session has done so far.",
        id="standings",
    )


def _advanced(model: Mapping[str, Any]) -> str:
    items = tuple(model.get("advanced") or ())
    if not items:
        return ""
    links = "".join(
        f"<a class='btn btn-sm' href='{fmt.attr(href)}'>{escape(str(label))}"
        f"<span class='visually-hidden'> — {escape(str(hint))}</span></a>"
        for label, href, hint in items
    )
    return (
        "<details class='advanced'><summary>Advanced — every other page</summary>"
        "<p class='small muted'>These pages are not needed for the five steps. They hold the same "
        "facts in more detail, and the LIVE workflow with its own safeguards.</p>"
        f"<div class='actions' style='flex-wrap:wrap'>{links}</div></details>"
    )


def render_start(ctx: Any) -> str:
    catalog = dict((ctx.extra or {}).get("catalog") or {})
    account = common.account_view(ctx)
    model = guide.build(
        terminal=ctx.terminal,
        wizard_view=ctx.wizard.view(),
        symbols=_symbol_facts(ctx, catalog),
        catalog=catalog,
        data_check=dict((ctx.extra or {}).get("data_check") or {}),
        runner=dict(ctx.runner or {}),
        account=account,
    )
    summary = dict(model.get("summary") or {})
    next_key = str(summary.get("next_key") or "")
    done = int(summary.get("steps_done") or 0)
    total = int(summary.get("steps_total") or 5)

    if summary.get("ready"):
        headline = notice(
            "Paper trading is running",
            "Live terminal quotes price the entries and exits; the fills are simulated locally "
            "and no order is ever sent. Every figure below is PAPER.",
            tone="ok",
        )
    else:
        headline = notice(
            "Not trading yet",
            fmt.text(summary.get("reason") or ""),
            tone="warn",
        )

    live_notice = (
        notice(
            "The execution mode is LIVE",
            "This guided flow starts the PAPER session, which is unrelated to the LIVE mode and "
            "never sends an order. Arming and starting LIVE has its own review on the Live "
            "execution page, and nothing here can do it.",
            tone="warn",
        )
        if getattr(ctx, "live_mode", False)
        else ""
    )

    head = page_head(
        "Guided start",
        "Start paper trading",
        "Five short steps, in order. PAPER only: quotes come from MetaTrader 5, fills are "
        "simulated on this machine, and no order is ever sent. LIVE is never started here.",
        badges=chip("PAPER · simulated fills", "paper", dot=True)
        + (chip("SIMULATED / SAMPLE DATA", "fixture") if ctx.terminal.simulated else "")
        + chip(f"{done} of {total} done", "ok" if done == total else "warn"),
        actions=button_link("/autopilot", "PAPER command center", small=True),
    )

    steps_html = (
        "<ol class='guide-steps'>"
        + "".join(
            _step(ctx, step.audit_view(), next_key=next_key, candidates=tuple(model.get("candidates") or ()))
            for step in model.get("steps") or ()
        )
        + "</ol>"
    )

    primary = model.get("primary") or ()
    signpost = (
        notice(
            f"Or run the whole loop from one page: {primary[0]}",
            f"{fmt.text(primary[2])} The Autopilot uses the same terminal, the same catalog record "
            "and the same paper ledger as these five steps.",
            tone="info",
        )
        + f"<p style='margin:8px 0 0'><a class='btn btn--primary btn-sm' href='{fmt.attr(primary[1])}'>"
        f"Open {escape(str(primary[0]))}</a></p>"
        if primary
        else ""
    )

    main = (
        headline
        + live_notice
        + f"<p class='small muted' style='margin:10px 0 4px'>{done} of {total} steps complete.</p>"
        + _primary_controls(ctx, model, account)
        + signpost
        + _performance_section(account)
        + _tiles_section(model)
        + section(
            "What to do next",
            steps_html,
            note="Work down the list. The highlighted step is the next one to finish; every button "
            "posts an action this workstation already allows.",
            id="steps",
        )
        + _advanced(model)
    )
    aside = (
        section(
            "PAPER, not LIVE",
            facts(
                [
                    ("Fill model", "simulated on this machine"),
                    ("Quotes", fmt.text("terminal ticks" if not ctx.terminal.simulated else "offline sample data")),
                    ("Orders sent", chip("none — PAPER only", "paper", dot=True)),
                    ("LIVE workflow", "<a href='/live'>/live</a>, reviewed and armed separately"),
                ]
            ),
            note="No control on this page can arm LIVE or send an order.",
            id="lane",
            plain=True,
        )
        + common.safety_section(id="safety")
    )
    return head + layout(main, aside)

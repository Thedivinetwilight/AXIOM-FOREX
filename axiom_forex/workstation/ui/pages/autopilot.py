"""Autopilot: the PAPER command centre.

One page answers the whole paper question, in this order:

1. what the loop is doing right now, and what stopped it;
2. how to start or stop it, with the symbols, timeframe, bounds and lot size the
   run will actually use stated next to the control;
3. the selection that admitted each strategy — counts, policy, and the reason
   every candidate was chosen or left out;
4. what it is following, how fresh its quotes are, what it decided, what it
   filled, what it holds, and what that book is worth;
5. the AX-SCALPER observer, which is a separate paper engine and says so;
6. what taking any of this to LIVE would still require — a list, not a button.

There is no arming control and no order-entry control anywhere on this page, and
the only LIVE control it can draw is a start/stop for the session /live already
wired.  Review, approval, the confirmation phrase and the account fingerprint all
stay on /live.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from .. import format as fmt
from ..components import (
    action_form,
    jump_nav,
    blockers,
    button_link,
    cell,
    chip,
    empty_state,
    facts,
    field,
    layout,
    notice,
    page_head,
    section,
    stat,
    stats,
    table,
)
from . import common
from . import runner as runner_page

STATE_TONE = {
    "RUNNING": "ok",
    "PREPARING": "warn",
    "BLOCKED": "danger",
    "STOPPED": "muted",
}

HEADLINES = {
    "RUNNING": (
        "ok",
        "The paper loop is following the selection",
        "Real terminal quotes, locally simulated fills. No order is sent, no account is "
        "read, and nothing here is a profit claim.",
    ),
    "PREPARING": (
        "warn",
        "Evaluating, then selecting",
        "The catalog is being measured on the terminal's own history and the selection "
        "rules are applied to that evidence. Nothing is traded while this runs; the steps "
        "below are the record of it.",
    ),
    "BLOCKED": (
        "danger",
        "The autopilot is blocked",
        "It stopped at a step it could not complete, and said which. Nothing was traded, "
        "no mode changed, and no open position was closed.",
    ),
    "STOPPED": (
        "muted",
        "The paper loop is stopped",
        "Stopping never closes a position: anything open is still open and is listed "
        "below. Re-selection is a separate start, so a strategy is never swapped under an "
        "open position.",
    ),
}

DEFAULT_HEADLINE = (
    "muted",
    "The autopilot has not run in this process",
    "Press Start to evaluate the catalog on the terminal's own history, select on train "
    "and validation evidence, and follow the selection as a paper test.",
)


#: Where each refusal is cleared.  A blocker is a sentence *and* a route, so the
#: operator is never left reading a code with nowhere to go.
BLOCKER_FIXES: dict[str, tuple[str, str]] = {
    "no_terminal": ("Connect the terminal", "/setup"),
    "no_symbols_selected": ("Choose symbols", "/account"),
    "unsupported_timeframe": ("Choose a timeframe", "/quick-setup"),
    "no_usable_history": ("Check the market data", "/quick-setup"),
    "evaluation_failed": ("Re-run the evaluation", "/quick-setup"),
    "evaluation_record_not_written": ("Re-run the evaluation", "/quick-setup"),
    "no_candidate_met_the_selection_rules": ("See what was evaluated", "/quick-setup"),
    "no_usable_watch": ("See the symbol offer", "/account"),
    "paper_test_refused": ("See the paper test", "/quick-setup"),
    "catalog_panel_missing": ("Open Quick setup", "/quick-setup"),
}


def blocker_fixes(items: Sequence[Mapping[str, Any]]) -> str:
    """The refusal sentences, then one direct link per control that clears one."""

    items = [dict(item) for item in (items or ())]
    if not items:
        return ""
    fixes: list[tuple[str, str]] = []
    for item in items:
        route = BLOCKER_FIXES.get(str(item.get("reason") or ""))
        if route and route not in fixes:
            fixes.append(route)
    links = (
        "<div class='actions' style='margin-top:10px'>"
        + "".join(button_link(href, label, small=True) for label, href in fixes)
        + "</div>"
        if fixes
        else ""
    )
    return blockers(runner_page.blocker_items(items)) + links


def _state_chip(state: str) -> str:
    return chip(state or "UNKNOWN", STATE_TONE.get(state, "muted"), dot=state in {"RUNNING", "PREPARING"})


def _rows(items: Sequence[Mapping[str, Any]], keys: Sequence[str], labels: Sequence[str]) -> list[list[str]]:
    out: list[list[str]] = []
    for item in items:
        cells = []
        for key, label in zip(keys, labels):
            value = item.get(key)
            if isinstance(value, (list, tuple)):
                value = ", ".join(str(part) for part in value)
            cells.append(cell(fmt.text(value if value not in (None, "") else "—"), label=label))
        out.append(cells)
    return out


# ------------------------------------------------------------------ controls
def _start_form(ctx: Any, choices: Mapping[str, Any]) -> str:
    """Symbols, timeframe, bounds and size — the whole of what a start will do."""

    pairs = [dict(item) for item in (choices.get("pairs") or ()) if item.get("broker_symbol")]
    recommended = {str(name) for name in (choices.get("recommended") or ())}
    frames = [str(name) for name in (choices.get("timeframes") or ["1h"])]
    default_frame = str(choices.get("default_timeframe") or (frames[0] if frames else "1h"))

    if pairs:
        options = "".join(
            "<label class='field field--check' for='sym-{slug}'>"
            "<input type='checkbox' id='sym-{slug}' name='symbols' value='{symbol}'{checked}>"
            "<span class='label'><span class='mono'>{symbol}</span> "
            "<span class='muted'>{pair}</span>{major}</span></label>".format(
                slug="".join(ch for ch in str(item.get("broker_symbol", "")) if ch.isalnum()),
                symbol=fmt.attr(item.get("broker_symbol", "")),
                checked=" checked" if str(item.get("broker_symbol")) in recommended else "",
                pair=fmt.text(item.get("pair", "")),
                major=" <span class='muted small'>· major</span>" if item.get("major") else "",
            )
            for item in pairs
        )
        excluded = [dict(item) for item in (choices.get("excluded") or ())]
        excluded_note = (
            "<p class='small muted' style='margin:6px 0 0'>Not offered as forex: "
            + " ".join(
                f"{fmt.text(item.get('broker_symbol', ''))} ({fmt.text(item.get('kind', ''))})"
                for item in excluded[:8]
            )
            + "</p>"
            if excluded
            else ""
        )
        symbols = (
            f"<fieldset><legend>Forex pairs the terminal reports (verified: both currency "
            f"legs named)</legend><div class='split' style='gap:8px'>{options}</div>"
            "<p class='small muted' style='margin:8px 0 0'>Tick none and the run uses the "
            "pairs the terminal recommends. A name the terminal reports without both "
            f"currency legs is not offered here.</p>{excluded_note}</fieldset>"
        )
    else:
        symbols = field(
            "symbols",
            "Broker symbol",
            hint="Type a broker symbol: the terminal reported no verified forex pair, so "
            "type one. A name the terminal does not report will be refused, not guessed.",
            placeholder="e.g. EURUSD",
        )

    timeframe = field(
        "timeframes",
        "Timeframe",
        kind="select",
        value=default_frame,
        options=[(name, name) for name in frames],
        hint="Each timeframe is a separate evaluation pass over the catalog.",
    )

    return (
        "<form method='post' action='/actions/autopilot-start' class='form'>"
        f"<input type='hidden' name='nonce' value='{fmt.attr(ctx.action_nonce)}'>"
        f"{symbols}"
        "<div class='form-row'>"
        f"{timeframe}"
        + "<div class='field'><span class='label'>Simulated lot size</span>"
        f"<span class='mono'>{fmt.text(choices.get('lot', '—'))}</span>"
        "<span class='hint'>Stated, not editable here: the paper test's own size is what "
        "every simulated fill uses.</span></div>"
        + "</div>"
        "<p class='small muted'>One run measures at most "
        f"{int(choices.get('max_symbols', 0) or 0)} symbol(s) and follows at most "
        f"{int(choices.get('max_rows', 0) or 0)} row(s). Both bounds are stated here rather "
        "than applied silently.</p>"
        f"<p class='small muted'>Every simulated fill is <span class='mono'>"
        f"{fmt.text(choices.get('lot', ''))}</span> lots: the paper test's own size. Quick "
        "setup's lot size belongs to the research sweep and does not resize this loop, so "
        "this line states the size that will actually run.</p>"
        "<div class='actions'><button class='btn btn--primary' type='submit'>"
        "Start paper autopilot</button></div></form>"
    )


def _control_section(ctx: Any, view: Mapping[str, Any]) -> tuple[str, bool]:
    state = str(view.get("state") or "IDLE")
    running = state in {"RUNNING", "PREPARING"}
    positions = list(view.get("positions") or ())
    choices = dict(view.get("choices") or {})
    if running:
        body = (
            "<div class='actions'>"
            + action_form("autopilot-stop", ctx.action_nonce, "Stop the paper loop", tone="warn")
            + "</div>"
            "<p class='small muted' style='margin-top:8px'>Stopping ends the loop and leaves "
            "every open position open and visible below. It does not change the execution "
            "mode and does not touch LIVE.</p>"
        )
        if positions:
            body += (
                f"<p class='small muted'>The {len(positions)} open simulated position(s) stay "
                "open while the loop runs. Stop the loop first if you mean to close them: "
                "stopping never closes anything by itself.</p>"
            )
        tools = chip("LOOP RUNNING", "ok", dot=True)
    else:
        body = _start_form(ctx, choices) if view.get("available") else (
            "<p class='small muted'>The catalog autopilot is not wired into this process, so "
            "there is nothing here to start.</p>"
        )
        if positions:
            body += (
                "<div class='actions' style='margin-top:12px'>"
                + action_form(
                    "autopilot-flat",
                    ctx.action_nonce,
                    "Close the simulated positions",
                    tone="danger",
                )
                + "</div><p class='small muted' style='margin-top:6px'>A separate click, and "
                "paper only: it closes the simulated book at the current quote, or refuses "
                "and leaves it alone. Nothing is sent to a broker.</p>"
            )
        tools = chip("LOOP IDLE", "muted")
    return (
        section(
            "Start and stop",
            body,
            note="One start for the whole paper workflow and one stop. Neither can place an "
            "order, arm live trading, or change the execution mode.",
            tools=tools,
            id="start-stop",
        ),
        running,
    )


# ---------------------------------------------------------------- admission
def _selection_section(view: Mapping[str, Any]) -> str:
    selection = dict(view.get("selection") or {})
    decisions = dict(view.get("decisions") or {})
    if not selection:
        return ""
    counts = dict(selection.get("counts") or {})
    policy = dict(selection.get("policy") or {})
    rows = _rows(
        [dict(item) for item in (decisions.get("decisions") or ())],
        ("candidate_id", "symbol", "timeframe", "status", "rank", "reasons", "message"),
        ("Candidate", "Symbol", "TF", "Decision", "Rank", "Reason", "In words"),
    )
    omitted = int(decisions.get("decisions_omitted", 0) or 0)
    policy_pairs = [
        ("Data origin", fmt.text(selection.get("data_origin") or "unknown")),
        ("Rules applied at", fmt.text(selection.get("generated_at") or "not recorded")),
        ("Evaluated at", fmt.text(selection.get("evaluated_at") or "not recorded")),
        ("Policy", fmt.text(policy.get("label") or policy.get("name") or "the selection policy's own audit record")),
        (
            "Selection rules",
            "Ranked on train and validation evidence only. Walk-forward folds that lost money "
            "were left out; a candidate that cannot show its evidence is rejected rather than "
            "assumed.",
        ),
    ]
    if policy:
        policy_pairs.extend(
            (f"Policy · {fmt.humanize_code(key)}", fmt.text(value))
            for key, value in sorted(policy.items())
            if isinstance(value, (str, int, float, bool))
        )

    counts_html = stats(
        [
            stat("Selected", f"{int(selection.get('selected') or 0):,}", foot="these are followed"),
            stat("Eligible, not chosen", f"{int(selection.get('eligible_not_chosen') or 0):,}", foot="passed the screen, lost the rank"),
            stat("Rejected", f"{int(selection.get('rejected') or 0):,}", foot="each with its reason"),
            stat("Evaluated", f"{int(counts.get('evaluated') or counts.get('rows') or 0):,}", foot="catalog rows measured"),
        ]
    )

    body = counts_html + facts(policy_pairs) + table(
        (("Candidate", False), ("Symbol", False), ("TF", False), ("Decision", False), ("Rank", True), ("Reason", False), ("In words", False)),
        rows,
        caption="Why every candidate was selected or left out.",
        empty=empty_state("No candidate was recorded", "A run records one row per candidate it measured."),
    )
    if omitted:
        body += (
            f"<p class='small muted'>{omitted} further decision(s) are in the stored selection "
            "record; this page draws the first 60.</p>"
        )
    body += (
        "<p class='small muted'>Selection record: "
        f"<code>{fmt.text(view.get('selection_record') or 'not written')}</code> · evaluation "
        f"record: <code>{fmt.text(view.get('record') or 'not written')}</code></p>"
    )
    return section(
        "Selection and admission",
        body,
        note="Nothing is admitted without recorded evidence. The counts above and the table "
        "below come from the same selection record.",
        id="selection",
    )


# --------------------------------------------------------------- what it does
def _active_section(paper: Mapping[str, Any]) -> str:
    watches = [dict(item) for item in (paper.get("watches") or ())]
    rows = _rows(
        watches,
        ("symbol", "timeframe", "family", "candidate_id", "trades", "positive_folds"),
        ("Symbol", "TF", "Family", "Candidate", "Trades measured", "Positive folds"),
    )
    return section(
        "Active strategies",
        table(
            (("Symbol", False), ("TF", False), ("Family", False), ("Candidate", False), ("Trades measured", True), ("Positive folds", True)),
            rows,
            caption="One row per symbol and timeframe the loop follows.",
            empty=empty_state("No strategy is being followed", "Start the loop to follow a selection."),
        ),
        note="The rationale for each of these is in the selection table above: the loop "
        "follows what was admitted, and nothing else.",
        id="active",
        flush=True,
    )


def _feed_section(paper: Mapping[str, Any]) -> str:
    quotes = [dict(item) for item in (paper.get("quotes") or ())]
    allowance = paper.get("max_quote_age_seconds")
    rows = [
        [
            cell(fmt.text(item.get("symbol") or ""), label="Symbol"),
            cell(
                chip(str(item.get("state") or "unavailable"), "ok" if item.get("fresh") else "warn", dot=bool(item.get("fresh"))),
                label="State",
            ),
            cell(
                fmt.text("—" if item.get("age_seconds") is None else str(item.get("age_seconds"))),
                label="Quote age (s)",
                num=True,
            ),
            cell(f"{fmt.text(item.get('bid') or '—')} / {fmt.text(item.get('ask') or '—')}", label="Bid / ask", num=True),
            cell(fmt.text(item.get("quote_time") or "—"), label="Quote time (UTC)", mono=True),
            cell(fmt.text(item.get("checked_at") or "—"), label="Last checked (UTC)", mono=True),
        ]
        for item in quotes
    ]
    return section(
        "Feed health",
        table(
            (("Symbol", False), ("State", False), ("Quote age (s)", True), ("Bid / ask", True), ("Quote time (UTC)", False), ("Last checked (UTC)", False)),
            rows,
            caption="The last quote the loop saw, per symbol.",
            empty=empty_state("No quote has been read yet", "Feed health appears after the first tick."),
        ),
        note=f"Quotes older than {fmt.text(allowance if allowance not in (None, '') else 'the configured')} s "
        "are refused: a stale, missing or half-formed quote skips that cycle, and the skip is "
        "written to the ledger rather than filled at a made-up price.",
        id="feed",
        flush=True,
    )


def _decisions_section(paper: Mapping[str, Any]) -> str:
    signals = [dict(item) for item in (paper.get("signals") or ())]
    rows = _rows(
        signals,
        ("symbol", "timeframe", "action", "reason", "detail"),
        ("Symbol", "TF", "Action", "Reason", "Detail"),
    )
    return section(
        "Decisions",
        table(
            (("Symbol", False), ("TF", False), ("Action", False), ("Reason", False), ("Detail", False)),
            rows,
            caption="The last decision per symbol and timeframe.",
            empty=empty_state("No decision has been taken yet", "A decision appears after the first complete cycle."),
        ),
        note="Every cycle writes the decision it took, including the ones that took none.",
        id="decisions",
        flush=True,
    )


def _fills_section(paper: Mapping[str, Any]) -> str:
    entries = [dict(item) for item in (paper.get("entry_fills") or ())]
    exits = [dict(item) for item in (paper.get("exit_fills") or ())]
    entry_rows = _rows(
        entries,
        ("symbol", "at", "side", "price", "lots", "reason"),
        ("Symbol", "At (UTC)", "Side", "Price", "Lots", "Reason"),
    )
    exit_rows = _rows(
        exits,
        ("symbol", "at", "direction", "entry_price", "exit_price", "simulated_net_quote", "reason"),
        ("Symbol", "At (UTC)", "Side", "Entry", "Exit", "Price P&L (before costs)", "Reason"),
    )
    return section(
        "Simulated fills",
        "<h3 class='small muted' style='margin:0 0 6px'>Entry fills</h3>"
        + table(
            (("Symbol", False), ("At (UTC)", False), ("Side", False), ("Price", True), ("Lots", True), ("Reason", False)),
            entry_rows,
            caption="Entry fills, newest last.",
            empty=empty_state("No entry fill yet", "An entry appears only after a signal passes every gate."),
        )
        + "<h3 class='small muted' style='margin:18px 0 6px'>Exit fills (closed trades)</h3>"
        + table(
            (("Symbol", False), ("At (UTC)", False), ("Side", False), ("Entry", True), ("Exit", True), ("Price P&L (before costs)", True), ("Reason", False)),
            exit_rows,
            caption="Exit fills, with the price P&L in the quote currency.",
            empty=empty_state("No exit fill yet", "A close appears only after an open position is exited."),
        ),
        note="These are numbers in a local ledger. No order was sent for any of them.",
        tools=chip(f"{int(paper.get('fills_total') or 0):,} simulated fill(s)", "paper"),
        id="fills",
    )


def _positions_section(paper: Mapping[str, Any]) -> str:
    positions = [dict(item) for item in (paper.get("positions") or ())]
    rows = _rows(
        positions,
        ("symbol", "timeframe", "side", "lots", "entry_price", "target", "opened_at", "restored"),
        ("Symbol", "TF", "Side", "Lots", "Entry", "Target", "Opened", "After restart"),
    )
    return section(
        "Open simulated positions",
        table(
            (("Symbol", False), ("TF", False), ("Side", False), ("Lots", True), ("Entry", True), ("Target", True), ("Opened", False), ("After restart", False)),
            rows,
            caption="Open positions — these stay open when the loop stops.",
            empty=empty_state("No simulated position is open", "Nothing is being held by this loop."),
        ),
        note="Simulated fills on the broker's own quote. Stopping the loop, or closing the "
        "workstation, does not close them: a stored book is restored on the next start.",
        tools=chip(f"{len(positions):,} open", "paper"),
        id="positions",
        flush=True,
    )


def _pnl_section(ctx: Any) -> str:
    account = common.account_view(ctx)
    rows = [
        ("Open positions", f"{int((account.get('counts') or {}).get('open_positions') or 0):,}"),
        ("Closed trades", f"{int((account.get('counts') or {}).get('closed_trades') or 0):,}"),
        ("Net P&L after costs", _metric_or_reason(account, "net_pnl")),
        ("Realised P&L (price)", _metric_or_reason(account, "realized_gross")),
        ("Unrealised P&L (price)", _metric_or_reason(account, "unrealized_gross")),
        ("Paper equity", _metric_or_reason(account, "equity")),
    ]
    return section(
        "Paper P&L",
        facts(rows),
        note="Read from the same durable paper ledger the Performance page reads, so the two "
        "pages cannot disagree. Nothing here is a broker balance or a profit claim.",
        tools=button_link("/performance", "Open Performance", small=True),
        id="pnl",
        plain=True,
    )


def _metric_or_reason(account: Mapping[str, Any], key: str) -> str:
    metric = dict((account.get("metric_by_key") or {}).get(key) or {})
    value = str(metric.get("value") or fmt.UNKNOWN)
    if not metric.get("available"):
        return f"Unavailable — <span class='small muted'>{fmt.text(str(metric.get('note') or ''))}</span>"
    return f"{fmt.text(value)} <span class='small muted'>· {fmt.text(str(metric.get('note') or ''))}</span>"


# ------------------------------------------------------------------- scalper
def render_scalper_panel(view: Mapping[str, Any], nonce: str) -> str:
    """The AX-SCALPER PAPER panel on its own, so it can be reviewed in isolation."""

    return _scalper_section(view, nonce)


def _scalper_section(view: Mapping[str, Any], nonce: str) -> str:
    scalper = dict(view.get("scalper_paper") or {})
    if not scalper.get("available"):
        return section(
            "AX-SCALPER PAPER · live MT5 quotes",
            empty_state(
                "AX-SCALPER PAPER is not wired into this process",
                "The scalper paper observer is not part of this workstation build, so there is "
                "nothing here to start. The catalog autopilot above is unaffected.",
            )
            + "<div class='actions' style='margin-top:12px'>"
            + action_form("connect", nonce, "Connect (observe only)")
            + "</div>"
            + "<p class='small muted' style='margin-top:10px'>LIVE is a separate page: this "
            "PAPER page cannot arm, submit, or close a broker order.</p>",
            note="A separate paper engine, with its own ledger and its own admission rules.",
            id="scalper-paper",
        )
    state = str(scalper.get("state") or "UNKNOWN")
    config = dict(scalper.get("config") or {})
    risk = dict(config.get("risk") or {})
    quote = dict(scalper.get("quote") or {})
    review = dict(scalper.get("strategy_review") or {})
    recovery = [dict(item) for item in (scalper.get("recovery") or ())]

    facts_pairs = [
        (
            "State",
            f"{chip(state, {'RUNNING': 'ok', 'STOPPED': 'muted', 'BLOCKED': 'danger', 'DISCONNECTED': 'muted'}.get(state, 'muted'), dot=state == 'RUNNING')} "
            f"<span class='small muted'>{fmt.text(scalper.get('reason') or config.get('reason') or '')}</span>",
        ),
        ("Terminal", chip("connected", "ok") if scalper.get("connected") else chip("not connected", "muted")),
        ("Account type", fmt.text(scalper.get("account_type") or "UNKNOWN")),
        ("Symbol", fmt.text(config.get("symbol") or "not configured")),
        (
            "Max quote age (ms)",
            fmt.text(config.get("max_quote_age_ms") or risk.get("max_quote_age_ms"))
            if (config.get("max_quote_age_ms") or risk.get("max_quote_age_ms")) not in (None, "")
            else "configured age limit unavailable",
        ),
        (
            "Last quote",
            (
                f"{chip('fresh', 'ok', dot=True) if scalper.get('quote_fresh') else chip('stale', 'warn')} "
                f"<span class='small muted'>{_seconds(quote.get('quote_age_seconds'))} s old</span>"
            ),
        ),
        (
            "Paper orders / fills",
            (
                f"{fmt.text(quote.get('paper_orders'))} / {fmt.text(quote.get('paper_fills'))}"
                if quote.get("paper_orders") not in (None, "")
                and quote.get("paper_fills") not in (None, "")
                else "unavailable — no PAPER quote session has started"
            ),
        ),
        ("Trading ready", chip("yes", "ok") if scalper.get("trading_ready") else chip("no", "warn")),
        ("Observation only", "yes" if scalper.get("observation_only") else "no"),
        (
            "Strategy admission",
            chip("open", "ok") if review.get("trade_admission_open") else chip("closed", "warn")
        ),
        ("Ledger", f"<code>{fmt.text(scalper.get('ledger') or 'not written')}</code>"),
    ]

    risk_pairs = [
        ("Per-trade equity risk (fraction of equity)", fmt.text(risk.get("risk_fraction") or "—")),
        ("Open-position cap (positions)", fmt.text(risk.get("max_open_positions") or "—")),
        ("Max daily loss (fraction of equity)", fmt.text(risk.get("max_daily_loss_fraction") or "—")),
        ("Minimum total / OOS trades", fmt.text(risk.get("min_trades") or "—")),
        ("Point", fmt.text(config.get("point") or "—")),
        ("Contract size", fmt.text(config.get("contract_size") or "—")),
        ("Commission per lot", fmt.text(config.get("commission_per_lot") or "—")),
        ("Slippage points per side", fmt.text(config.get("slippage_points_per_side") or "—")),
    ]

    symbols = [dict(item) for item in (scalper.get("symbols") or ())]
    symbol_rows = _rows(
        symbols[:12],
        ("broker_symbol", "pair", "kind", "digits", "spread_points"),
        ("Broker name", "Pair", "Kind", "Digits", "Spread (points)"),
    )
    exclusions = [dict(item) for item in (scalper.get("symbol_exclusions") or ())]
    exclusion_rows = _rows(
        exclusions[:8],
        ("broker_symbol", "kind", "reason"),
        ("Name", "Kind", "Reason"),
    )

    blockers_html = blockers(_recovery_items(recovery)) or notice(
        "No PAPER blocker is reported right now",
        "Nothing in the stored books is waiting on an operator decision.",
        tone="ok",
    )

    controls: list[str] = []
    if state == "RUNNING":
        controls.append(action_form("scalper-paper-stop", nonce, "Stop AX-SCALPER PAPER", tone="warn"))
    elif scalper.get("start_allowed"):
        controls.append(action_form("scalper-paper-start", nonce, "Start admitted PAPER decisions", tone="primary"))
    elif scalper.get("resume_exact_allowed"):
        controls.append(
            action_form("scalper-paper-observe", nonce, "Verify exact replay and resume PAPER observer", tone="primary")
        )
    elif scalper.get("observation_start_allowed"):
        controls.append(action_form("scalper-paper-observe", nonce, "Start quote observation only", tone="primary"))
    if not scalper.get("connected"):
        controls.append(
            action_form("connect", nonce, "Connect (observe only)")
        )

    start_note = ""
    if state != "RUNNING" and not scalper.get("start_allowed") and not scalper.get("observation_start_allowed") and not scalper.get("resume_exact_allowed"):
        start_note = notice(
            "Start blocked — no PAPER session launched",
            "The observer refuses to start until its own blockers are cleared. Each refusal "
            "names the exact condition; none of them is a silent fallback to something else.",
            tone="warn",
        )

    paused = ""
    if state == "PAUSED":
        paused = notice(
            "PAPER decisions are paused or refused",
            f"The observer is running but taking no decision: {fmt.text(scalper.get('reason') or 'the reason was not reported')}.",
            tone="warn",
        )

    versions = [dict(item) for item in (quote.get("strategy_versions") or ())]
    strategies = (
        table(
            (("Strategy", False), ("Version", False), ("Fingerprint", False)),
            [
                [
                    cell(fmt.text(item.get("strategy") or ""), label="Strategy"),
                    cell(fmt.text(item.get("version") or ""), label="Version"),
                    cell(fmt.text(item.get("fingerprint") or ""), label="Fingerprint", mono=True),
                ]
                for item in versions
            ],
            caption="Exactly the strategies enabled in this PAPER observer",
            dense=True,
        )
        if versions
        else "<p class='small muted'>No strategy is enabled in this PAPER observer.</p>"
        + (
            "<p class='small muted'>Quote counts: unavailable — no tick received.</p>"
            if quote.get("quote_age_seconds") in (None, "")
            else ""
        )
    )

    body = (
        facts(facts_pairs)
        + paused
        + "<div class='actions' style='margin-top:12px'>"
        + "".join(controls)
        + "</div>"
        + start_note
        + "<h3 class='small muted' style='margin:18px 0 6px'>Strategy review — evidence and policy decision</h3>"
        + (
            facts(
                [
                    ("Result", chip(str(review.get("status") or "NOT_APPROVED"), "ok" if review.get("trade_admission_open") else "warn")),
                    ("Reason", fmt.text(review.get("reason") or review.get("message") or "—")),
                    ("Evidence", fmt.text(review.get("evidence") or "—")),
                ]
            )
            if review
            else empty_state("No strategy review", "No review has been recorded for this observer.")
        )
        + "<h3 class='small muted' style='margin:18px 0 6px'>Risk settings, with the unit each value uses</h3>"
        + facts(risk_pairs)
        + "<h3 class='small muted' style='margin:18px 0 6px'>Exactly the enabled strategies "
        "in this PAPER observer</h3>"
        + strategies
        + "<h3 class='small muted' style='margin:18px 0 6px'>Terminal-verified Forex symbols (exact broker names)</h3>"
        + table(
            (("Broker name", False), ("Pair", False), ("Kind", False), ("Digits", True), ("Spread (points)", True)),
            symbol_rows,
            caption="Exactly the symbols the terminal reports, with the name to type.",
            empty=empty_state("No verified symbol", "Connect a real terminal first."),
        )
        + (
            table(
                (("Name", False), ("Kind", False), ("Reason", False)),
                exclusion_rows,
                caption="Excluded terminal symbols",
                dense=True,
            )
            if exclusion_rows
            else ""
        )
    )
    body += "<h3 class='small muted' style='margin:18px 0 6px'>Blockers and their direct fixes</h3>" + blockers_html

    body += notice(
        "This PAPER page cannot arm, submit, or close a broker order.",
        "AX-SCALPER → LIVE is blocked here by design: it does not transfer, approve, or arm "
        "anything. LIVE remains a separate workflow on <a href='/live'>/live</a>, where the "
        "plan is reviewed, the account confirmed and the phrase typed.",
        tone="muted",
    )

    return section(
        "AX-SCALPER PAPER · live MT5 quotes",
        body,
        note="A separate paper engine: its ledger, its admission rules and its own counters. "
        "LIVE is a separate page, and nothing here can reach it.",
        tools=chip(state, {"RUNNING": "ok", "BLOCKED": "danger"}.get(state, "muted")),
        id="scalper-paper",
    )


# -------------------------------------------------------------------- LIVE
def _handoff_section(ctx: Any, view: Mapping[str, Any]) -> str:
    handoff = dict(view.get("handoff") or {})
    live = dict(view.get("live") or {})
    requirements = [str(item) for item in (handoff.get("requirements") or ())]
    proposed = [dict(item) for item in (live.get("proposed") or ())]
    per_order = str(handoff.get("per_order_checks") or "")

    body = notice(
        "LIVE is never automatic",
        "A paper selection is not a live readiness verdict, and this page never arms "
        "anything. Review, approval, the confirmation phrase and the account fingerprint "
        "are on <a href='/live'>/live</a>; this list is what is still missing.",
        tone="warn",
    )
    body += blockers(runner_page.blocker_items(live.get("blockers") or handoff.get("blockers") or ()))
    if requirements:
        body += (
            "<h3 class='small muted' style='margin:18px 0 6px'>What a live order would still require</h3>"
            "<ol class='stack--sm' style='margin:0;padding-left:18px'>"
            + "".join(f"<li>{fmt.text(item)}</li>" for item in requirements)
            + "</ol>"
        )
    body += table(
        (("Candidate", False), ("Symbol", False), ("Side", False), ("Note", False)),
        _rows(proposed, ("candidate_id", "symbol", "side", "note"), ("Candidate", "Symbol", "Side", "Note")),
        caption="The selection this page would propose, if an operator chose to review it.",
        empty=empty_state("Nothing is proposed", "Nothing is selected, so nothing is proposed."),
    )
    if per_order:
        body += f"<p class='small muted' style='margin-top:10px'>{fmt.text(per_order)}</p>"

    controls = []
    if live.get("available") and live.get("enabled"):
        action = "autopilot-live-stop" if live.get("running") else "autopilot-live-start"
        label = "Stop the live session" if live.get("running") else "Start the live session"
        controls.append(
            action_form(
                action,
                ctx.action_nonce,
                label,
                tone="danger" if not live.get("running") else "warn",
                described_by="handoff-note",
            )
        )
    body += (
        "<p class='small muted' id='handoff-note' style='margin-top:10px'>Starting the live "
        "session here only begins the loop /live already wired: it sends nothing, arms "
        "nothing, and picks no strategy, symbol, size or limit. Without that wiring there is "
        "no control at all.</p>"
    )
    if controls:
        body += "<div class='actions' style='margin-top:8px'>" + "".join(controls) + "</div>"

    return section(
        "Taking this to LIVE: what is missing",
        body,
        note="Every line is a refusal with a reason, not a checklist that a click can clear.",
        tools=button_link("/live", "Open the separate LIVE review", small=True),
        id="live-handoff",
    )


def _kronos_section(view: Mapping[str, Any], nonce: str = "") -> str:
    kronos = dict(view.get("kronos") or {})
    policy = dict(view.get("kronos_policy") or {})
    if not kronos:
        return ""
    return section(
        "Kronos, if you turn it on",
        facts(
            [
                ("Mode", fmt.text(kronos.get("mode") or "OFF")),
                ("Enabled", chip("yes", "ok") if kronos.get("enabled") else chip("no", "muted")),
                ("Affects the ranking", "no"),
                ("Can create a trade", "no"),
                ("Can size a position", "no"),
                ("Can only veto", "yes" if policy.get("can_only_veto", True) else "no"),
                (
                    "Forecasts so far",
                    f"{int(kronos.get('vetoed') or 0)} veto(es), {int(kronos.get('unavailable') or 0)} unavailable",
                ),
                ("Label", chip(str(kronos.get("label") or "FORECAST_ONLY_NOT_A_SIGNAL"), "fixture")),
            ]
        )
        + notice(
            "A second opinion, never a signal",
            fmt.text(str(kronos.get("note") or policy.get("note") or "")),
            tone="muted",
        )
        + (
            "<form method='post' action='/actions/autopilot-kronos' class='form'>"
            f"<input type='hidden' name='nonce' value='{fmt.attr(nonce)}'>"
            "<label class='field'><span class='label'>How a forecast may be used</span>"
            "<select name='kronos_mode'>"
            f"<option value='OFF'{' selected' if str(kronos.get('mode') or 'OFF') == 'OFF' else ''}>OFF — no forecast is read</option>"
            f"<option value='VETO'{' selected' if str(kronos.get('mode')) == 'VETO' else ''}>VETO — a forecast may cancel an entry</option>"
            "</select></label>"
            "<label class='small'><input type='checkbox' name='kronos_required' value='1'"
            f"{' checked' if kronos.get('required') else ''}> skip a signal when no forecast is available</label>"
            "<div class='actions'><button class='btn' type='submit'>Set the forecast mode</button></div>"
            "</form>"
            if nonce
            else ""
        ),
        note="Off by default because its forecasts have not been validated against realised "
        "outcomes here.",
        id="kronos",
    )


def _latency_section(view: Mapping[str, Any]) -> str:
    latency = dict(view.get("latency") or {})
    if not latency:
        return ""
    inner = dict(latency.get("decision") or {})
    return section(
        "Observed latency",
        facts(
            [
                ("Selection pass", f"{fmt.text(latency.get('selection_ms', 0))} ms"),
                ("Evaluation", f"{fmt.text(latency.get('evaluation_ms', 0))} ms"),
                ("Decision, last", f"{fmt.text(inner.get('last', 0))} ms"),
                ("Decision, mean", f"{fmt.text(inner.get('mean', 0))} ms"),
                ("Decision, max", f"{fmt.text(inner.get('max', 0))} ms"),
                ("Samples", fmt.text(inner.get("samples", 0))),
            ]
        )
        + notice("Measured here, on this machine", fmt.text(str(latency.get("note") or "")), tone="muted"),
        note="No number here is a service level. Order submission cannot be timed from a "
        "browser page.",
        id="latency",
    )


def _stored_section(view: Mapping[str, Any]) -> str:
    stored = dict(view.get("stored_book") or {})
    refusals = [dict(item) for item in (view.get("watch_refusals") or ())]
    parts: list[str] = []
    if stored and stored.get("available") is not False:
        positions = [dict(item) for item in (stored.get("positions") or ())]
        parts.append(
            facts(
                [
                    ("Stored book", chip("blocked", "warn") if stored.get("blocked") else chip("readable", "ok")),
                    ("Positions on disk", f"{len(positions):,}"),
                    ("Ledger", f"<code>{fmt.text(stored.get('ledger') or 'not reported')}</code>"),
                ]
            )
            + table(
                (("Symbol", False), ("Side", False), ("Lots", True), ("Entry", True), ("Opened", False)),
                _rows(positions, ("symbol", "side", "lots", "entry_price", "opened_at"), ("Symbol", "Side", "Lots", "Entry", "Opened")),
                caption="Open simulated positions, as stored on disk",
                empty=empty_state("Nothing stored", "The stored book is empty."),
            )
        )
    if refusals:
        parts.append(
            table(
                (("Symbol", False), ("Reason", False), ("Detail", False)),
                _rows(refusals, ("symbol", "reason", "detail"), ("Symbol", "Reason", "Detail")),
                caption="Rows the paper loop could not follow",
                dense=True,
                empty=empty_state("No refusals", "Every requested row could be followed."),
            )
            + "<p class='small muted'>These were not swapped for something else: a row that "
            "cannot be followed is refused and named.</p>"
        )
    if not parts:
        return ""
    return section(
        "Open simulated positions, as stored on disk",
        "".join(parts),
        note="The book that survives a restart. It is read, never merged into the running "
        "loop's own list.",
        id="stored",
    )


def getattr_no_terminal(view: Mapping[str, Any]) -> bool:
    """Whether this process has no terminal at all (and so nothing to observe)."""

    terminal = dict(view.get("terminal") or {})
    return bool(terminal.get("disconnected"))


def _seconds(value: Any) -> str:
    if value in (None, ""):
        return "—"
    try:
        return f"{float(value):.3f}"
    except (TypeError, ValueError):
        return fmt.text(value)


def _recovery_items(recovery: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Stored-ledger refusals, in the blocker component's shape."""

    items: list[dict[str, Any]] = []
    for entry in recovery:
        if not isinstance(entry, Mapping):
            continue
        items.append(
            {
                "title": str(entry.get("reason") or "A stored PAPER ledger cannot be resumed"),
                "detail": str(entry.get("detail") or entry.get("message") or "")
                or "Its stored positions and ledger stay untouched and new runs are blocked.",
                "code": str(entry.get("ledger") or ""),
            }
        )
    return items


def _jump(sections: Sequence[tuple[str, str]]) -> str:
    items = "".join(f"<li><a href='#{fmt.attr(slug)}'>{fmt.text(label)}</a></li>" for slug, label in sections)
    return f"<ul class='stack--sm' style='margin:0;padding-left:18px'>{items}</ul>"


def render_autopilot(ctx: Any) -> str:
    view = dict((ctx.extra or {}).get("autopilot") or {})
    nonce = fmt.attr(ctx.action_nonce)
    account = common.account_view(ctx)

    if not view.get("available"):
        message = str(
            view.get("message")
            or "The conventional catalog autopilot is unavailable in this process. "
            "AX-SCALPER PAPER status is shown separately below."
        )
        head = page_head(
            "Paper command center · simulated fills on this machine",
            "Autopilot",
            "The catalog autopilot is not wired into this workstation, so this page can start "
            "nothing. It still shows what is on disk and what the separate scalper observer "
            "is doing.",
            badges=chip("NOT WIRED", "warn", dot=True) + chip("PAPER · SIMULATED FILLS", "paper", dot=True),
            actions=button_link("/live", "LIVE review is on /live", small=True),
        )
        body = head + notice("The catalog autopilot is not wired into this workstation", fmt.text(message), tone="warn")
        body += (
            "<div class='actions' style='margin-bottom:16px'>"
            + button_link("/live", "Open the separate LIVE review")
            + "</div>"
        )
        body += layout(
            _scalper_section(view, nonce)
            + runner_page.sections(ctx)
            + _pnl_section(ctx)
            + section("What this page cannot say", facts(_cannot_pairs()), plain=True, id="limits"),
            section("Status", facts(_status_pairs(ctx, view)), plain=True, id="status")
            + _jump([("scalper-paper", "AX-SCALPER PAPER"), ("pnl", "Paper P&L"), ("limits", "What this page cannot say")]),
        )
        return body

    state = str(view.get("state") or "IDLE")
    paper = dict(view.get("paper") or {})
    positions = list(view.get("positions") or ())
    control_html, running = _control_section(ctx, view)
    tone, headline, detail = HEADLINES.get(state, DEFAULT_HEADLINE)

    head = page_head(
        "Paper command center · simulated fills on this machine; LIVE review and arming are on /live",
        "Autopilot",
        headline,
        badges=_state_chip(state)
        + chip("PAPER · SIMULATED FILLS", "paper", dot=True)
        + common.data_chip(ctx.paper_label),
        actions=button_link("/live", "Open LIVE review", small=True),
    )

    sections_drawn: list[tuple[str, str]] = [("start-stop", "Start and stop")]
    body_parts: list[str] = [
        notice(
            headline,
            f"{detail} {fmt.text(str(view.get('note') or ''))}".strip(),
            tone=tone,
        )
    ]
    body_parts.append(control_html)

    blocker_items = runner_page.blocker_items(view.get("blockers") or ())
    if blocker_items:
        sections_drawn.append(("blocked", "What stopped it"))
        body_parts.append(
            section(
                "What stopped it",
                blocker_fixes(view.get("blockers") or ())
                + "<p class='small muted' style='margin-top:10px'>A blocker is a refusal with "
                "a reason and a direct link to the control that clears it, never a silent "
                "fallback to something else.</p>",
                note="Nothing was substituted, skipped or retried behind your back.",
                tools=button_link("/setup", "Open setup", small=True),
                id="blocked",
            )
        )

    steps = [dict(item) for item in (view.get("steps") or ())]
    if steps:
        sections_drawn.append(("steps", "What it did, in order"))
        body_parts.append(
            section(
                "What it did, in order",
                table(
                    (("Step", False), ("Result", False), ("Took (ms)", True), ("Detail", False)),
                    [
                        [
                            cell(fmt.text(item.get("name") or item.get("step") or "—"), label="Step"),
                            cell(chip("ok", "ok") if item.get("ok") else chip("refused", "warn"), label="Result"),
                            cell(fmt.text(item.get("ms") or "—"), label="Took (ms)", num=True),
                            cell(fmt.text(item.get("detail") or ""), label="Detail"),
                        ]
                        for item in steps
                    ],
                    caption="Every step the run took, in the order it took them.",
                ),
                note="This is the record of the run, not a plan: a step that refused is shown "
                "with the reason it refused.",
                id="steps",
                flush=True,
            )
        )

    selection_html = _selection_section(view)
    if selection_html:
        sections_drawn.append(("selection", "Selection and admission"))
        body_parts.append(selection_html)

    if paper:
        sections_drawn.extend(
            [
                ("active", "Active strategies"),
                ("feed", "Feed health"),
                ("decisions", "Decisions"),
                ("fills", "Simulated fills"),
                ("positions", "Open positions"),
            ]
        )
        body_parts.extend(
            [
                _active_section(paper),
                _feed_section(paper),
                _decisions_section(paper),
                _fills_section(paper),
                _positions_section(paper),
            ]
        )

    body_parts.append(_pnl_section(ctx))
    sections_drawn.append(("pnl", "Paper P&L"))

    sections_drawn.append(("scalper-paper", "AX-SCALPER PAPER"))
    body_parts.append(_scalper_section(view, nonce))

    body_parts.append(_handoff_section(ctx, view))
    sections_drawn.append(("live-handoff", "Taking this to LIVE"))

    body_parts.append(runner_page.sections(ctx))
    sections_drawn.append(("runner", "Paper strategy runner"))

    kronos_html = _kronos_section(view, nonce)
    if kronos_html:
        sections_drawn.append(("kronos", "Kronos"))
        body_parts.append(kronos_html)
    latency_html = _latency_section(view)
    if latency_html:
        sections_drawn.append(("latency", "Observed latency"))
        body_parts.append(latency_html)
    stored_html = _stored_section(view)
    if stored_html:
        sections_drawn.append(("stored", "Stored book"))
        body_parts.append(stored_html)

    all_sections = list(sections_drawn) + [
        ("status", "Status"),
        ("limits", "What this page cannot say"),
    ]
    aside = (
        jump_nav(all_sections)
        + section("Status", facts(_status_pairs(ctx, view)), note="What this page read, and when.", id="status", plain=True)
        + section("What this page cannot say", facts(_cannot_pairs()), plain=True, id="limits")
        + notice(
            "SIMULATED FILLS ONLY",
            "Every fill here is simulated locally from the terminal's own quote. No order was "
            "sent, no account was read and nothing here is a profit claim.",
            tone="paper" if False else "info",
        )
    )
    return head + layout("".join(body_parts), aside)


def _status_pairs(ctx: Any, view: Mapping[str, Any]) -> list[tuple[str, str]]:
    paper = dict(view.get("paper") or {})
    session = dict((common.account_view(ctx).get("session") or {}))
    account = common.account_view(ctx)
    return [
        ("State", _state_chip(str(view.get("state") or "IDLE"))),
        ("Reason", fmt.text(view.get("reason") or "—")),
        ("Started", fmt.text(fmt.utc(view.get("started_at"))) if view.get("started_at") else "not in this process"),
        ("Finished", fmt.text(fmt.utc(view.get("finished_at"))) if view.get("finished_at") else "—"),
        ("Open simulated positions", f"{int(view.get('open_positions') or 0):,}"),
        ("Paper session", chip("RUNNING", "paper") if session.get("running") else chip("NOT RUNNING", "muted")),
        ("Quotes", common.quote_chip(account)),
        ("Lot size", fmt.text(paper.get("lots") or (view.get("choices") or {}).get("lot") or "—")),
        ("Selection at", fmt.text(fmt.utc(view.get("selection_at"))) if view.get("selection_at") else "no selection on record"),
        ("Selection record", f"<code>{fmt.text(view.get('selection_record') or 'not written')}</code>"),
        ("Evaluation record", f"<code>{fmt.text(view.get('record') or 'not written')}</code>"),
        ("Broker account", "Not read, not used, and never mixed into a paper figure."),
    ]


def _cannot_pairs() -> list[tuple[str, str]]:
    return [
        ("Arming", "not offered here — the phrase, fingerprint and review are on /live"),
        ("Order entry", "no form exists, at any mode"),
        ("Broker positions", "not read on this page; they are on /live"),
        ("Profit", "no claim; a simulated fill is not a result"),
        ("Account switching", "not possible from this interface"),
        ("Retrying", "an unknown outcome stops the loop rather than retrying it"),
    ]

"""Quick setup: the short answers, the stored year, and what the run measured.

The page is worked down from the top by one operator, so it carries its own
contents list.  Its order is the order of the work:

* the catalog block (its own module): declared candidates, the forex symbol
  offer, one evaluation, the local paper test, and the whole-catalog audit;
* what the terminal answered, with a provenance word on every row;
* the five answers only the operator can give, and the two cost assumptions the
  terminal cannot report;
* what each mode could do with that setup, said separately for research and LIVE;
* one year of history from the terminal, and which series are usable;
* the run: progress, what it measured, and every candidate that did not run;
* the one-click paper runner, which is a different book from the catalog test.

Nothing here trades, arms, changes a mode, or claims a profit.  A result is a
historical estimate under the cost assumptions printed beside it.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from .. import format as fmt
from ..components import (
    action_form,
    blockers,
    button_link,
    cell,
    chip,
    field,
    jump_nav,
    jump_nav_bar,
    layout,
    meter,
    notice,
    page_head,
    section,
    stat,
    stats,
    table,
)
from . import catalog, common

#: How a provenance word is drawn.  The word travels with the value.
_PROVENANCE_TONES = {"OBSERVED": "ok", "SIMULATED": "simulated", "UNKNOWN": "muted"}

#: How many symbol rows are drawn before the page says what it is not showing.
MAX_SYMBOL_ROWS = 200


def _num(value: Any) -> str:
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return "0"


def _pct(value: Any) -> str:
    if value is None or str(value).strip() == "":
        return ""
    try:
        return f"{float(value):.2%}"
    except (TypeError, ValueError):
        return ""


def _signed(value: Any) -> str:
    if value is None or str(value).strip() == "":
        return ""
    try:
        return f"{float(value):+.4%}"
    except (TypeError, ValueError):
        return ""


def _messages(items: Sequence[Any]) -> list[str]:
    """Blockers arrive as sentences or as ``{reason, message, hint}``."""

    out: list[str] = []
    for entry in items:
        if isinstance(entry, Mapping):
            out.append(str(entry.get("message") or entry.get("reason") or ""))
        else:
            out.append(str(entry))
    return [item for item in out if item]


# ---------------------------------------------------------------------------
# what the terminal answered, and the symbols it reported
# ---------------------------------------------------------------------------


def _terminal_section(view: Mapping[str, Any]) -> str:
    rows = [dict(item) for item in (view.get("observed_rows") or ())]
    if not rows:
        return ""
    facts_view = dict(view.get("facts") or {})
    state = str(facts_view.get("state", "UNKNOWN"))
    tone = (
        "simulated"
        if facts_view.get("simulated")
        else ("ok" if facts_view.get("connected") else "muted")
    )
    return section(
        "What the terminal answered",
        table(
            [
                ("Field", "text"),
                ("Value", "text"),
                ("Provenance", "text"),
                ("What it means", "text"),
            ],
            [
                [
                    cell(fmt.text(str(item.get("label", ""))), label="Field"),
                    cell(f"<code>{fmt.text(str(item.get('value', '')))}</code>", label="Value"),
                    cell(
                        chip(
                            str(item.get("state", "UNKNOWN")),
                            _PROVENANCE_TONES.get(str(item.get("state")), "muted"),
                        ),
                        label="Provenance",
                    ),
                    cell(fmt.text(str(item.get("detail", ""))), label="What it means"),
                ]
                for item in rows
            ],
            caption="Terminal answers and their provenance",
        ),
        note="A value marked unknown was not reported by the terminal. It is never replaced by a "
        "zero, because a zero is a claim.",
        tools=chip(state, tone, dot=True),
        id="terminal-answered",
    )


def _symbols_section(view: Mapping[str, Any]) -> str:
    symbols = [dict(item) for item in (view.get("symbols") or ())]
    if not symbols:
        return section(
            "The symbols your terminal reported",
            notice(
                "No symbols were reported",
                "Connect to your terminal on the Setup page; without it there is nothing to "
                "choose, fetch, or evaluate.",
                tone="warn",
            ),
            id="symbols",
        )
    counted = dict(view.get("symbol_counts") or {})
    reported = int(counted.get("reported") or len(symbols))
    examined = int(counted.get("examined") or len(symbols))
    drawn = symbols[:MAX_SYMBOL_ROWS]
    note = (
        "A pair is named only when the terminal reported both currency legs. A broker's suffix "
        "never becomes part of the pair name."
    )
    if examined < reported:
        note += (
            f" {examined} of {reported} reported names were read; the other {reported - examined} "
            "could not be, so they were not classified."
        )
    if len(symbols) > len(drawn):
        note += (
            f" This table draws the first {len(drawn)} of the {len(symbols)} symbols that were "
            "read, to keep the page usable."
        )
    return section(
        "The symbols your terminal reported",
        table(
            [
                ("Broker symbol", "text"),
                ("Pair", "text"),
                ("Min volume", "num"),
                ("Step", "num"),
                ("Stops level", "num"),
                ("Swap", "text"),
                ("Not reported", "text"),
            ],
            [
                [
                    cell(
                        f"<code>{fmt.text(str(item.get('broker_symbol', '')))}</code>",
                        label="Broker symbol",
                    ),
                    cell(fmt.text(str(item.get("pair", "")) or "unknown"), label="Pair"),
                    cell(
                        fmt.text(str(item.get("volume_min", "")) or "unknown"),
                        label="Min volume",
                        num=True,
                    ),
                    cell(
                        fmt.text(str(item.get("volume_step", "")) or "unknown"),
                        label="Step",
                        num=True,
                    ),
                    cell(
                        fmt.text(str(item.get("stops_level", "")) or "0"),
                        label="Stops level",
                        num=True,
                    ),
                    cell(fmt.text(str(item.get("swap_state", ""))), label="Swap"),
                    cell(
                        fmt.text(
                            ", ".join(str(part) for part in (item.get("missing") or ()))
                            or "nothing"
                        ),
                        label="Not reported",
                    ),
                ]
                for item in drawn
            ],
            caption="Broker-reported symbol specifications",
        ),
        note=note,
        tools=chip(f"{len(drawn)} of {len(symbols)} drawn", "muted"),
        id="symbols",
    )


# ---------------------------------------------------------------------------
# the five answers
# ---------------------------------------------------------------------------


def _picks(symbols: Sequence[Mapping[str, Any]], chosen: Sequence[str]) -> str:
    """The terminal's own symbols, offered as checkboxes, never as free text."""

    if not symbols:
        return field(
            "symbols",
            "Broker symbol",
            placeholder="type one symbol",
            hint="No symbol was reported by the terminal, so the name has to be typed.",
            field_id="quick-symbols",
        )
    tiles = "".join(
        f"<label class='pick'><input type='checkbox' name='symbols' "
        f"value='{fmt.attr(str(item.get('broker_symbol', '')))}'"
        f"{' checked' if str(item.get('broker_symbol')) in chosen else ''}>"
        f"<span><code>{fmt.text(str(item.get('broker_symbol', '')))}</code>"
        f"<span class='why'>{fmt.text(str(item.get('pair', '')))}</span></span></label>"
        for item in symbols
    )
    return (
        "<p class='small muted' style='margin:0 0 8px'>Symbols to trade</p>"
        "<div class='pick-grid' role='group' aria-label='Symbols to trade'>"
        f"{tiles}</div>"
    )


def _answers_section(view: Mapping[str, Any], nonce: str) -> str:
    choices = dict(view.get("choices") or {})
    symbols = [dict(item) for item in (view.get("symbols") or ())]
    chosen = [str(item) for item in (choices.get("symbols") or ())]
    saved = view.get("saved") or None
    body = (
        "<form method='post' action='/actions/quick-setup-save' class='form'>"
        f"<input type='hidden' name='nonce' value='{fmt.attr(nonce)}'>"
        + _picks(symbols, chosen)
        + "<div class='form-row' style='margin-top:14px'>"
        + field(
            "lot_size",
            "Lot size",
            value=str(choices.get("lot_size", "") or ""),
            placeholder="0.10",
            field_id="quick-lot-size",
        )
        + field(
            "max_open_positions",
            "Max open positions",
            value=str(choices.get("max_open_positions", 1)),
            field_id="quick-max-positions",
        )
        + field(
            "max_daily_loss_account",
            "Max daily loss (account currency)",
            value=str(choices.get("max_daily_loss_account", "") or ""),
            field_id="quick-daily-loss",
        )
        + field(
            "max_order_notional_account",
            "Max order notional (account currency)",
            value=str(choices.get("max_order_notional_account", "") or ""),
            field_id="quick-notional",
        )
        + field(
            "max_spread_pips",
            "Max spread (pips)",
            value=str(choices.get("max_spread_pips", "") or ""),
            field_id="quick-spread",
        )
        + field(
            "window_days",
            "Research window (days)",
            value=str(choices.get("window_days", view.get("window_days", 365))),
            field_id="quick-window",
        )
        + "</div><div class='form-row' style='margin-top:14px'>"
        + field(
            "commission_per_lot",
            "Commission per lot, round turn (assumed)",
            value="0",
            hint="The terminal does not report a commission schedule, so this stays an "
            "assumption you state. If your broker charges one, every research result without it "
            "is optimistic by that amount.",
            field_id="quick-commission",
        )
        + field(
            "slippage_pips",
            "Slippage per fill (pips, estimated)",
            value="0.2",
            field_id="quick-slippage",
        )
        + "</div><div class='actions' style='margin-top:16px'>"
        "<button class='btn btn--primary' type='submit'>Save the short setup</button>"
        "<span class='small muted'>Written to a local, Git-ignored file. No credential, no login, "
        "no account number.</span></div></form>"
    )
    return section(
        "The five things only you can answer",
        body,
        note="These are choices, not observations: nothing here is inferred for you. Account "
        "currency, leverage, the broker symbols and their specs come from the terminal; the "
        "commission schedule does not, so it is an assumption you state.",
        tools=(
            chip(f"recorded · {fmt.text(str((saved or {}).get('fingerprint', '')))}", "ok", dot=True)
            if saved
            else chip("not recorded yet", "muted")
        ),
        id="five-answers",
    )


def _readiness_section(view: Mapping[str, Any]) -> str:
    readiness = view.get("readiness") or None
    if not readiness:
        return ""
    blocks = []
    for name, title in (("research", "Research"), ("live", "Live execution")):
        item = dict(readiness.get(name) or {})
        reasons = _messages(list(item.get("blockers") or ()))
        blocks.append(
            f"<div><h3>{fmt.text(title)}</h3>"
            + (
                chip("ready", "ok", dot=True)
                if item.get("ready")
                else chip(f"{len(reasons)} blocker(s)", "warn", dot=True)
            )
            + (
                "<ul class='check-list' style='margin-top:8px'>"
                + "".join(f"<li>{fmt.text(entry)}</li>" for entry in reasons)
                + "</ul>"
                if reasons
                else ""
            )
            + f"<p class='small muted' style='margin:8px 0 0'>"
            f"{fmt.text(str(item.get('note', '')))}</p></div>"
        )
    return section(
        "What each mode can do with this setup",
        f"<div class='split-callout'>{''.join(blocks)}</div>",
        note="Research may run on labelled assumptions. Live execution still passes the "
        "broker-terms document, the pre-trade gate, the risk limits and the protective-stop "
        "requirement. Nothing here shortens that.",
        id="readiness",
    )


# ---------------------------------------------------------------------------
# history, the run, and what it measured
# ---------------------------------------------------------------------------


def _fetch_section(view: Mapping[str, Any], nonce: str) -> str:
    timeframes = [str(item) for item in (view.get("timeframes") or ())]
    defaults = [str(item) for item in (view.get("default_timeframes") or ())]
    history = [dict(item) for item in (view.get("history") or ())]
    boxes = "".join(
        f"<label class='pick'><input type='checkbox' name='timeframes' "
        f"value='{fmt.attr(name)}'{' checked' if name in defaults else ''}>"
        f"<span><code>{fmt.text(name)}</code></span></label>"
        for name in timeframes
    )
    return section(
        "One year of history, from your terminal",
        "<form method='post' action='/actions/research-fetch' class='form'>"
        f"<input type='hidden' name='nonce' value='{fmt.attr(nonce)}'>"
        "<div class='pick-grid' role='group' aria-label='Timeframes to read'>"
        f"{boxes}</div>"
        "<div class='actions' style='margin-top:12px'>"
        "<button class='btn' type='submit'>Read and store that history</button>"
        "<span class='small muted'>Bars come from the terminal's own rate history. Nothing is "
        "downloaded, generated, or substituted.</span></div></form>",
        note="Bars are bid-only in the terminal, so the research series is the bid plus the spread "
        "each bar itself quoted. That is recorded in the price basis of every result.",
        tools=chip(f"{len(history)} stored", "muted"),
        id="history",
    )


def _stored_history_section(view: Mapping[str, Any]) -> str:
    history = [dict(item) for item in (view.get("history") or ())]
    if not history:
        return ""
    usable = sum(1 for item in history if item.get("usable_for_research"))
    return section(
        "Stored history, and whether it is usable",
        table(
            [
                ("Symbol", "text"),
                ("Timeframe", "text"),
                ("Bars", "num"),
                ("First", "text"),
                ("Last", "text"),
                ("Coverage", "text"),
                ("Why not", "text"),
            ],
            [
                [
                    cell(f"<code>{fmt.text(str(item.get('symbol', '')))}</code>", label="Symbol"),
                    cell(fmt.text(str(item.get("timeframe", ""))), label="Timeframe"),
                    cell(fmt.text(str(item.get("bars", 0))), label="Bars", num=True),
                    cell(
                        f"<code>{fmt.text(str(item.get('first_at', ''))[:16])}</code>",
                        label="First",
                    ),
                    cell(
                        f"<code>{fmt.text(str(item.get('last_at', ''))[:16])}</code>",
                        label="Last",
                    ),
                    cell(
                        chip("usable", "ok", dot=True)
                        if item.get("usable_for_research")
                        else chip("not usable", "warn", dot=True),
                        label="Coverage",
                    ),
                    cell(
                        fmt.text(str(item.get("reason", "")) or "coverage accepted"),
                        label="Why not",
                    ),
                ]
                for item in history
            ],
            caption="Stored history coverage and usability",
        ),
        note="Timestamps, gaps and span are audited before anything is measured. A series that is "
        "short, sparse or stale is listed here as not usable instead of being padded or replaced.",
        tools=chip(f"{usable} of {len(history)} usable", "warn" if usable < len(history) else "ok"),
        id="stored-history",
    )


def _run_section(ctx: Any, view: Mapping[str, Any]) -> str:
    running = bool(view.get("running"))
    progress = dict(view.get("progress") or {})
    report = view.get("report") or None
    try:
        fraction = max(0.0, min(1.0, float(progress.get("fraction") or 0.0)))
    except (TypeError, ValueError):
        fraction = 0.0
    budget = 540
    if report:
        try:
            budget = int(float(dict(report.get("summary") or {}).get("budget_seconds", budget)))
        except (TypeError, ValueError):
            budget = 540
    nonce = str(getattr(ctx, "action_nonce", ""))
    action = (
        action_form("research-cancel", nonce, "Cancel the run", tone="danger")
        if running
        else action_form("research-run", nonce, "Run the one-year evaluation", tone="primary")
    )
    body = stats(
        [
            stat(
                "Progress",
                f"{fraction * 100:.0f}%",
                foot=fmt.text(str(progress.get("label", "")) or "not started"),
            ),
            stat("Budget", f"{budget}s", foot="wall clock, then it stops and says so"),
            stat("Variants", "42", foot="predeclared combinations, all recorded"),
        ]
    )
    body += (
        f"<div class='progress' role='progressbar' aria-valuemin='0' aria-valuemax='100' "
        f"aria-valuenow='{fraction * 100:.0f}' aria-label='Evaluation progress' "
        f"style='margin-top:12px'>{meter(fraction)}</div>"
        f"<div class='actions' style='margin-top:12px'>{action}"
        "<span class='small muted'>Train / validation / test is chronological. The test window is "
        "measured once, for the variant chosen on validation.</span></div>"
    )
    return section(
        "Run every candidate over that history",
        body,
        note="Spread comes from each bar's own quote; commission and slippage are the assumptions "
        "you set; swap is included only where the broker reported it. Anything that could not be "
        "priced is named in the result.",
        tools=chip(
            fmt.text(str(progress.get("label", ""))) or "idle",
            "warn" if running else "muted",
            dot=True,
        ),
        id="sweep",
    )


def _measured_section(report: Mapping[str, Any]) -> str:
    summary = dict(report.get("summary") or {})
    status = str(summary.get("status", ""))
    try:
        elapsed = float(summary.get("elapsed_seconds", 0) or 0)
    except (TypeError, ValueError):
        elapsed = 0.0
    try:
        budget = int(float(summary.get("budget_seconds", 0) or 0))
    except (TypeError, ValueError):
        budget = 0
    return section(
        "What the run measured",
        stats(
            [
                stat(
                    "Variants measured",
                    fmt.text(str(summary.get("variants_measured", 0))),
                    foot=f"of {fmt.text(str(summary.get('variants_total', 0)))}",
                ),
                stat(
                    "Usable series",
                    fmt.text(str(summary.get("series_with_usable_history", 0))),
                    foot=f"of {fmt.text(str(summary.get('series', 0)))} requested",
                ),
                stat(
                    "Did not run",
                    fmt.text(str(summary.get("not_run", 0))),
                    foot="listed, never hidden",
                ),
                stat("Elapsed", f"{elapsed:.0f}s", foot=f"budget {budget}s"),
            ]
        )
        + f"<p class='small muted' style='margin:12px 0 0'>{fmt.text(str(report.get('claim', '')))}</p>",
        note="A measured variant is a historical estimate on the bars on record, under the cost "
        "assumptions printed above.",
        tools=chip(status, "ok" if status == "COMPLETE" else "warn", dot=True),
        id="what-measured",
    )


def _results_section(report: Mapping[str, Any]) -> str:
    rows = [dict(item) for item in (report.get("table") or ())]
    if not rows:
        return ""
    notes = [str(item) for item in (report.get("notes") or ())]
    body = table(
        [
            ("Symbol", "text"),
            ("TF", "text"),
            ("Strategy variant", "text"),
            ("Val trades", "num"),
            ("Val net", "num"),
            ("Val cost", "num"),
            ("Val DD", "num"),
            ("Test trades", "num"),
            ("Test net", "num"),
            ("Test DD", "num"),
            ("On test", "text"),
        ],
        [
            [
                cell(f"<code>{fmt.text(str(row.get('symbol', '')))}</code>", label="Symbol"),
                cell(fmt.text(str(row.get("timeframe", ""))), label="TF"),
                cell(
                    f"<code>{fmt.text(str(row.get('strategy', '')))}</code>",
                    label="Strategy variant",
                ),
                cell(
                    fmt.text(str(row.get("validation_trades", ""))),
                    label="Val trades",
                    num=True,
                ),
                cell(_signed(row.get("validation_net_return")), label="Val net", num=True),
                cell(_signed(row.get("validation_cost_return")), label="Val cost", num=True),
                cell(_pct(row.get("validation_max_drawdown")), label="Val DD", num=True),
                cell(
                    fmt.text(str(row.get("test_trades", ""))) if row.get("selected_for_test") else "",
                    label="Test trades",
                    num=True,
                ),
                cell(_signed(row.get("test_net_return")) if row.get("selected_for_test") else "",
                     label="Test net", num=True),
                cell(_pct(row.get("test_max_drawdown")) if row.get("selected_for_test") else "",
                     label="Test DD", num=True),
                cell(
                    chip("chosen on validation", "ok")
                    if row.get("selected_for_test")
                    else "<span class='muted'>not measured</span>",
                    label="On test",
                ),
            ]
            for row in rows
        ],
        caption="Evaluation results by strategy, symbol and timeframe",
        dense=True,
    )
    if notes:
        body += "<ul class='check-list' style='margin-top:10px'>" + "".join(
            f"<li>{fmt.text(note)}</li>" for note in notes
        ) + "</ul>"
    return section(
        "By strategy, symbol and timeframe",
        body,
        note="Validation is the window a candidate may be chosen on. The test window is shown only "
        "for the variant chosen on validation, so it stays a test window. Nothing here is a "
        "profitability claim.",
        tools=chip(f"{_num(report.get('row_count', len(rows)))} rows", "muted"),
        id="results",
    )


def _coverage_section(report: Mapping[str, Any]) -> str:
    coverage = [dict(item) for item in (report.get("coverage") or ())]
    if not coverage:
        return ""
    return section(
        "Which history was actually used",
        table(
            [
                ("Symbol", "text"),
                ("TF", "text"),
                ("Bars", "num"),
                ("Coverage", "num"),
                ("Gaps", "num"),
                ("Used", "text"),
                ("Reason", "text"),
            ],
            [
                [
                    cell(f"<code>{fmt.text(str(item.get('symbol', '')))}</code>", label="Symbol"),
                    cell(fmt.text(str(item.get("timeframe", ""))), label="TF"),
                    cell(fmt.text(str(item.get("bars", 0))), label="Bars", num=True),
                    cell(_pct(item.get("coverage_fraction")), label="Coverage", num=True),
                    cell(fmt.text(str(item.get("gaps", 0))), label="Gaps", num=True),
                    cell(
                        chip("used", "ok", dot=True)
                        if item.get("usable")
                        else chip("not used", "warn", dot=True),
                        label="Used",
                    ),
                    cell(fmt.text(str(item.get("reason", ""))), label="Reason"),
                ]
                for item in coverage
            ],
            caption="History actually used in the evaluation",
        ),
        note="A series that was not used is listed with the reason it was not.",
        id="coverage",
    )


def _fold_sections(report: Mapping[str, Any]) -> str:
    """One section per symbol and timeframe, out of sample and in later chunks."""

    blocks = []
    for item in report.get("walk_forward") or ():
        item = dict(item)
        folds = [dict(entry) for entry in (item.get("folds") or ())]
        blocks.append(
            section(
                f"Out of sample, in later chunks: {str(item.get('symbol', ''))} "
                f"{str(item.get('timeframe', ''))}",
                stats(
                    [
                        stat(
                            "Positive folds",
                            f"{int(item.get('positive_folds', 0) or 0)} of {len(folds)}",
                            foot="later chunks of the same history",
                        ),
                        stat(
                            "Median fold",
                            _signed(item.get("median_net_return")),
                            foot="net of the stated costs",
                        ),
                    ]
                )
                + table(
                    [
                        ("Fold", "num"),
                        ("From", "text"),
                        ("To", "text"),
                        ("Trades", "num"),
                        ("Net", "num"),
                        ("Max DD", "num"),
                    ],
                    [
                        [
                            cell(fmt.text(str(fold.get("fold", ""))), label="Fold", num=True),
                            cell(
                                f"<code>{fmt.text(str(fold.get('from', ''))[:16])}</code>",
                                label="From",
                            ),
                            cell(
                                f"<code>{fmt.text(str(fold.get('to', ''))[:16])}</code>",
                                label="To",
                            ),
                            cell(fmt.text(str(fold.get("trades", 0))), label="Trades", num=True),
                            cell(_signed(fold.get("net_return")), label="Net", num=True),
                            cell(_pct(fold.get("max_drawdown")), label="Max DD", num=True),
                        ]
                        for fold in folds
                    ],
                    caption="Later out-of-sample chunks for this symbol and timeframe",
                ),
                note=str(item.get("note", "")) or "Later chunks of the same history, scored once.",
                id="walk-"
                f"{fmt.attr(str(item.get('symbol', '')))}-{fmt.attr(str(item.get('timeframe', '')))}",
            )
        )
    return "".join(blocks)


def _sensitivity_sections(report: Mapping[str, Any]) -> str:
    blocks = []
    for item in report.get("sensitivity") or ():
        item = dict(item)
        runs = [dict(entry) for entry in (item.get("runs") or ())]
        blocks.append(
            section(
                f"Would a dearer cost assumption change it? {str(item.get('symbol', ''))} "
                f"{str(item.get('timeframe', ''))}",
                table(
                    [
                        ("Cost multiplier", "num"),
                        ("Slippage (pips)", "num"),
                        ("Commission / lot", "num"),
                        ("Trades", "num"),
                        ("Validation net", "num"),
                    ],
                    [
                        [
                            cell(
                                f"{fmt.text(str(run.get('cost_multiplier', '')))}x",
                                label="Cost multiplier",
                                num=True,
                            ),
                            cell(
                                fmt.text(str(run.get("slippage_pips", ""))),
                                label="Slippage (pips)",
                                num=True,
                            ),
                            cell(
                                fmt.text(str(run.get("commission_per_lot", ""))),
                                label="Commission / lot",
                                num=True,
                            ),
                            cell(fmt.text(str(run.get("trades", 0))), label="Trades", num=True),
                            cell(_signed(run.get("net_return")), label="Validation net", num=True),
                        ]
                        for run in runs
                    ],
                    caption="Cost and slippage sensitivity for this symbol and timeframe",
                ),
                note="The same signals under a dearer assumption. If doubling the assumed costs "
                "erases the result, the result was the assumption.",
                id="sensitivity-"
                f"{fmt.attr(str(item.get('symbol', '')))}-{fmt.attr(str(item.get('timeframe', '')))}",
            )
        )
    return "".join(blocks)


def _not_run_section(report: Mapping[str, Any]) -> str:
    problems = [dict(item) for item in (report.get("failures") or ())] + [
        dict(item) for item in (report.get("not_run") or ())
    ]
    if not problems:
        return ""
    return section(
        "What did not run, and why",
        table(
            [
                ("Symbol", "text"),
                ("TF", "text"),
                ("Variant", "text"),
                ("Reason", "text"),
                ("Detail", "text"),
            ],
            [
                [
                    cell(f"<code>{fmt.text(str(item.get('symbol', '')))}</code>", label="Symbol"),
                    cell(fmt.text(str(item.get("timeframe", ""))), label="TF"),
                    cell(
                        f"<code>{fmt.text(str(item.get('variant_id', '')) or 'all')}</code>",
                        label="Variant",
                    ),
                    cell(fmt.text(str(item.get("reason", ""))), label="Reason"),
                    cell(fmt.text(str(item.get("detail", ""))), label="Detail"),
                ]
                for item in problems
            ],
            caption="Candidates that did not run, with the reason",
        ),
        note="A candidate that could not be measured is listed. It was not skipped quietly.",
        tools=chip(f"{len(problems)} listed", "warn"),
        id="what-did-not-run",
    )


# ---------------------------------------------------------------------------
# the runner, which is a different book from the catalog test
# ---------------------------------------------------------------------------


def _paper_runner_section(ctx: Any, view: Mapping[str, Any]) -> str:
    paper = dict(view.get("paper") or {})
    runner = dict(paper.get("runner") or {})
    selection = paper.get("selection") or None
    blocked = [dict(item) for item in (paper.get("blockers") or ())]
    runner_state = (
        "running"
        if runner.get("running")
        else ("ready" if runner.get("available") else "not configured")
    )
    body = stats(
        [
            stat(
                "Selected strategy",
                fmt.text(str((selection or {}).get("variant_id", ""))) or "none",
                foot="the operator's own choice",
            ),
            stat("Paper runner", runner_state, foot="simulated fills, local ledger"),
            stat(
                "Ready to start",
                "yes" if paper.get("ready") else "no",
                foot=f"{len(blocked)} blocker(s)",
            ),
        ]
    )
    body += blockers(
        [
            {
                "title": item.get("message") or item.get("reason") or "Blocked",
                "detail": item.get("hint") or item.get("detail") or "",
                "code": item.get("reason") or "",
                "tone": "danger",
            }
            for item in blocked
        ]
    )
    if runner.get("available"):
        body += (
            "<div class='actions' style='margin-top:12px'>"
            + action_form(
                "runner-start",
                str(getattr(ctx, "action_nonce", "")),
                "Start paper trading",
                tone="primary" if paper.get("ready") else "",
            )
            + "<span class='small muted'>Prices come from your terminal's current quotes; fills "
            "are simulated locally.</span></div>"
        )
    if any(str(item.get("reason")) == "no_strategy_selected" for item in blocked):
        # The blocker names the library page, so say whether that page can answer.
        if bool((ctx.extra or {}).get("strategies_available")):
            body += (
                "<p class='small' style='margin:10px 0 0'><a href='/strategies'>Open the strategy "
                "library</a> and select a variant there. This runner follows that choice; it "
                "never picks one for you, and nothing is selected by default.</p>"
            )
        else:
            body += (
                "<p class='small muted' style='margin:10px 0 0'>This process has no strategy "
                "library, so there is no variant to select and this runner cannot start here. The "
                "catalog paper test above is the paper path that needs no library.</p>"
            )
    return section(
        "Paper strategy runner, one click",
        body,
        note=str(paper.get("note", ""))
        + " A separate book from the catalog paper test above: the two never share a ledger, and "
        "neither sends an order.",
        tools=chip("PAPER", "paper", dot=True),
        id="research-paper",
    )


# ---------------------------------------------------------------------------
# the page
# ---------------------------------------------------------------------------


def _targets(view: Mapping[str, Any], catalog_view: Mapping[str, Any]) -> list[tuple[str, str]]:
    """Only the sections this render will actually draw are listed."""

    report = view.get("report") or None
    targets: list[tuple[str, str]] = [("catalog", "Catalog")]
    if catalog_view.get("available"):
        targets += [
            ("catalog-evaluate", "Evaluate the catalog"),
            ("paper-test", "Paper test"),
            ("catalog-audit", "Full catalog audit"),
        ]
    if view.get("observed_rows"):
        targets.append(("terminal-answered", "What the terminal answered"))
    targets.append(("five-answers", "The five answers"))
    if view.get("readiness"):
        targets.append(("readiness", "What each mode can do"))
    targets.append(("history", "One year of history"))
    targets.append(("sweep", "Run every candidate"))
    if report:
        targets.append(("what-measured", "What the run measured"))
        if report.get("results") or report.get("table"):
            targets.append(("results", "By strategy, symbol and timeframe"))
        if report.get("failures") or report.get("not_run"):
            targets.append(("what-did-not-run", "What did not run"))
    targets.append(("research-paper", "Strategy runner"))
    return targets


def render_quick_setup(ctx: Any) -> str:
    view = dict((ctx.extra or {}).get("research") or {})
    catalog_view = dict((ctx.extra or {}).get("catalog") or {})
    nonce = str(getattr(ctx, "action_nonce", ""))
    saved = view.get("saved") or None

    head = page_head(
        "Quick setup",
        "The short setup, the stored year, and what it measured",
        "What the terminal already knows, the five answers only you can give, one year of "
        "history, and every measured result — with each unknown and each unmeasured candidate "
        "kept visible.",
        badges=common.scope_badges(ctx)
        + (
            chip(f"setup recorded · {fmt.text(str((saved or {}).get('fingerprint', '')))}", "ok", dot=True)
            if saved
            else chip("no setup recorded", "warn", dot=True)
        )
        + (
            chip("catalog wired", "muted")
            if catalog_view.get("available")
            else chip("no catalog panel", "warn")
        )
        + chip("research only", "fixture"),
        actions=button_link("/strategies", "Strategy library", small=True)
        + button_link("/kronos", "Kronos forecast", small=True),
    )

    if not view.get("available"):
        return head + layout(
            notice(
                "The research panel is not wired into this workstation",
                "This process has no research panel, so there is nothing to read or run here. The "
                "rest of the workstation is unaffected.",
                tone="warn",
            )
            + common.safety_section(id="safety"),
            wide=True,
        )

    result = dict(view.get("result") or {})
    report = view.get("report") or None
    body = ""
    if result.get("message"):
        body += notice(
            "The last research action",
            fmt.text(str(result.get("message"))),
            tone="ok" if result.get("ok") else "warn",
            role="status" if result.get("ok") else "alert",
        )
        for row in result.get("rows") or ():
            if isinstance(row, Mapping) and row.get("usable") is False:
                body += notice(
                    f"{row.get('symbol', '')} {row.get('timeframe', '')}: no usable history",
                    fmt.text(str(row.get("reason") or "the terminal returned no bars")),
                    tone="warn",
                )

    body += jump_nav_bar(_targets(view, catalog_view))
    body += jump_nav(_targets(view, catalog_view))
    body += catalog.catalog_sections(catalog_view, nonce)
    body += catalog.rebuild_blockers_section(catalog_view)
    body += notice(
        "Short setup: the terminal answers what it can",
        "Account currency, leverage, the exact broker symbols, their specs and their swap terms "
        "are read from your terminal, never typed in by hand. You are asked only for what no "
        "terminal can know: the symbols to trade, the size, and your hard risk limits. Anything "
        "the terminal did not answer is labelled unknown, with what that affects.",
        tone="ok",
    )
    body += _terminal_section(view)
    body += _symbols_section(view)
    body += _answers_section(view, nonce)
    body += _readiness_section(view)
    body += _fetch_section(view, nonce)
    body += _stored_history_section(view)
    body += _run_section(ctx, view)
    if report:
        body += _measured_section(report)
        body += _results_section(report)
        body += _coverage_section(report)
        body += _fold_sections(report)
        body += _sensitivity_sections(report)
        body += _not_run_section(report)
    body += _paper_runner_section(ctx, view)
    body += notice(
        "No profitability claim is made anywhere on this page",
        "These are historical estimates on broker bars under the stated cost assumptions. A "
        "good-looking window is a reason to keep looking, not a reason to trade. Live execution is "
        "a separate mode with its own gates.",
        tone="ok",
    )
    return head + layout(body, wide=True)

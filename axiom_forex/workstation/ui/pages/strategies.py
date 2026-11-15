"""The strategy library: a research index, read as four separate questions.

The page exists to stop one number being read as another.  Over a thousand
strategies are declared, fewer are compatible with what the operator selected,
fewer still have been measured, and only a handful pass the screen -- so each
count is drawn beside its own label, and the ladder says what the last rung
would require.

Nothing here selects a strategy for the operator, sizes a position, sets a stop,
arms live execution, or changes an execution mode.  A selected variant still has
to go through the live plan's review, the confirmation phrase, the risk gate and
the protective-stop rule.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from .. import format as fmt
from ..components import (
    action_form,
    button_link,
    cell,
    chip,
    empty_state,
    facts,
    jump_nav,
    jump_nav_bar,
    layout,
    notice,
    page_head,
    section,
    stat,
    stats,
    table,
)
from . import common

#: How a recorded status is said.  Never a colour on its own.
_STATUS_CHIPS: dict[str, tuple[str, str, bool]] = {
    "SELECTED_BY_OPERATOR": ("selected by operator", "warn", True),
    "ELIGIBLE_VARIANT": ("eligible variant recorded", "ok", True),
    "ATTEMPTED": ("attempted · not eligible", "muted", True),
    "UNAVAILABLE": ("not evaluable here", "muted", False),
}

#: The four statuses a stored row can carry, and the tone each is drawn in.
_VERDICT_TONES = {
    "Passed": "ok",
    "Failed": "warn",
    "Too little evidence": "muted",
    "Skipped": "muted",
}


def _status_chip(status: str) -> str:
    label, tone, dot = _STATUS_CHIPS.get(
        str(status or "TEMPLATE"), ("template · untested", "paper", False)
    )
    return chip(label, tone, dot=dot)


def _num(value: Any) -> str:
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return "0"


def _text_list(values: Any, empty: str = "none") -> str:
    items = [str(item) for item in (values or ()) if str(item)]
    return fmt.text(", ".join(items)) if items else fmt.text(empty)


# ---------------------------------------------------------------------------
# the four questions, each beside its own label
# ---------------------------------------------------------------------------


def _funnel_counts(funnel: Mapping[str, Any]) -> str:
    library = dict(funnel.get("library") or {})
    compatible = dict(funnel.get("compatible") or {})
    evaluated = dict(funnel.get("evaluated") or {})
    counts = dict(funnel.get("counts") or {})
    return stats(
        [
            stat(
                "In the library",
                _num(library.get("candidates")),
                foot=f"{_num(library.get('structures'))} distinct rule structures",
                tone="accent",
            ),
            stat(
                "Compatible now",
                _num(compatible.get("candidates")),
                foot=f"{_num(compatible.get('incompatible_candidates'))} skipped: "
                "timeframe not offered",
            ),
            stat(
                "Evaluated",
                _num(evaluated.get("candidates")),
                foot=f"{_num(evaluated.get('rows'))} candidate x symbol rows",
            ),
            stat(
                "Passed the screen",
                _num(counts.get("passed_strategies")),
                foot=f"{_num(counts.get('passed_rows'))} rows across symbols",
            ),
            stat(
                "Failed",
                _num(counts.get("failed_strategies")),
                foot=f"{_num(counts.get('failed_rows'))} rows, each with its reason",
            ),
            stat(
                "Too little evidence",
                _num(counts.get("thin_strategies")),
                foot=f"{_num(counts.get('thin_rows'))} rows below the trade minimum",
            ),
            stat(
                "Skipped",
                _num(counts.get("skipped_strategies")),
                foot="declared, but not run on this timeframe",
            ),
            stat(
                "Not yet evaluated",
                _num(counts.get("not_evaluated")),
                foot="in the library with no result on record",
            ),
            stat(
                "Selected",
                _num(counts.get("selected")),
                foot="your operator choices",
            ),
        ]
    )


def _ladder(funnel: Mapping[str, Any]) -> str:
    ladder = dict(funnel.get("ladder") or {})
    return stats(
        [
            stat(
                "Step 1 - In the library",
                _num(ladder.get("in_library")),
                foot="declared in the catalog",
            ),
            stat(
                "Step 2 - Evaluated",
                _num(ladder.get("evaluated")),
                foot="has a result on record",
            ),
            stat(
                "Step 3 - Paper-testable",
                _num(ladder.get("paper_testable")),
                foot="passed the screen; may be paper-tested",
            ),
            stat(
                "Step 4 - Live-ready",
                _num(ladder.get("live_ready")),
                foot="backtests cannot reach this rung",
            ),
        ]
    ) + (
        f"<p class='small muted' style='margin:10px 0 0'>"
        f"{fmt.text(str(ladder.get('live_ready_note', '')))}</p>"
    )


def _funnel_section(funnel: Mapping[str, Any]) -> str:
    library = dict(funnel.get("library") or {})
    compatible = dict(funnel.get("compatible") or {})
    body = _funnel_counts(funnel)
    body += (
        "<p class='small muted' style='margin:12px 0 0'>Counted as "
        "<strong>strategies</strong>, not rows: one candidate measured on two symbols is "
        "one strategy tested twice. The four statuses plus &ldquo;not yet evaluated&rdquo; "
        f"add up to the library figure ({_num(library.get('candidates'))}).</p>"
    )
    if not funnel.get("available"):
        body += notice(
            "No evaluation on record yet",
            "The catalog is declared and the counts above are real, but nothing has been "
            "measured on your symbols yet. Evaluate on Quick setup, then return here to read "
            "each result and its reason.",
            tone="warn",
        )
    else:
        body += (
            "<p class='small muted' style='margin:12px 0 0'>Measured on "
            f"<strong>{_text_list(compatible.get('symbols'), 'none selected')}</strong> at "
            f"<strong>{_text_list(compatible.get('timeframes'), 'none selected')}</strong>"
            + (
                f", data origin <code>{fmt.text(str(funnel.get('data_origin')))}</code>"
                if funnel.get("data_origin")
                else ""
            )
            + ".</p>"
        )
    return section(
        "How many strategies, exactly",
        body,
        note=str(library.get("note", ""))
        or "Four different questions, answered separately, so none can be read as another.",
        tools=chip("FOUR QUESTIONS", "muted"),
        id="funnel",
    )


def _ladder_section(funnel: Mapping[str, Any]) -> str:
    return section(
        "Readiness ladder: in the library, evaluated, paper-testable, live-ready",
        _ladder(funnel),
        note="A ladder rung is a claim about evidence, not about profit. Nothing here "
        "promises a return, and no historical result is a forecast.",
        tools=chip("no profitability claim", "muted"),
        id="ladder",
    )


# ---------------------------------------------------------------------------
# evaluating the compatible catalog
# ---------------------------------------------------------------------------


def _evaluate_all_section(funnel: Mapping[str, Any], nonce: str) -> str:
    library = dict(funnel.get("library") or {})
    compatible = dict(funnel.get("compatible") or {})
    symbols = [str(item) for item in (compatible.get("symbols") or ()) if str(item)]
    timeframes = [str(item) for item in (compatible.get("timeframes") or ()) if str(item)]
    timeframe = timeframes[0] if timeframes else ""
    hidden = "".join(
        f"<input type='hidden' name='symbols' value='{fmt.attr(symbol)}'>" for symbol in symbols
    ) + (
        f"<input type='hidden' name='timeframe' value='{fmt.attr(timeframe)}'>"
        if timeframe
        else ""
    )
    heading = f"Evaluate all {_num(library.get('candidates'))} declared strategies"
    if not symbols:
        return section(
            "Evaluate every compatible strategy",
            empty_state(
                "No symbols selected",
                "The catalog is evaluated on the symbols you selected for observation, at the "
                "timeframe the terminal offers. Choose symbols on the account page first.",
                actions=button_link("/account", "Choose symbols"),
            ),
            note="Nothing is substituted for a symbol you did not choose.",
            tools=chip("NOTHING TO RUN", "muted"),
            id="evaluate-all",
        )
    form = (
        "<form method='post' action='/actions/catalog-evaluate' class='form'>"
        f"<input type='hidden' name='nonce' value='{fmt.attr(nonce)}'>"
        "<input type='hidden' name='return_to' value='/strategies'>"
        f"{hidden}"
        "<div class='actions'><button class='btn btn--primary' type='submit'>"
        f"{heading}</button></div></form>"
    )
    return section(
        "Evaluate every compatible strategy",
        "<p class='small muted' style='margin:0 0 10px'>Runs the declared catalog on the "
        "symbols and timeframe that are selected, using the terminal's own historical bars, "
        "the same bid/ask fills, cost model and calendar the paper lane uses. Every result is "
        "kept, including the failures.</p>"
        + form,
        note="This measures; it does not select, size, arm, or place anything. A candidate "
        "that passes is a short-list entry, not a promise of profit.",
        tools=chip("research only", "muted")
        + chip(f"{_text_list(symbols)} · {fmt.text(timeframe or 'no timeframe')}", "muted"),
        id="evaluate-all",
    )


# ---------------------------------------------------------------------------
# every recorded row, with its reason
# ---------------------------------------------------------------------------


def _filter_links(funnel: Mapping[str, Any]) -> str:
    filters = [dict(item) for item in (funnel.get("filters") or ())]
    current = str(funnel.get("verdict") or "")
    links = "".join(
        f"<a class='btn btn-sm{' btn--primary' if str(item.get('key')) == current else ''}' "
        f"href='/strategies?verdict={fmt.attr(str(item.get('key', '')))}'>"
        f"{fmt.text(str(item.get('label', '')))} ({int(item.get('rows') or 0):,})</a>"
        for item in filters
    )
    links += (
        f"<a class='btn btn-sm{' btn--primary' if not current else ''}' href='/strategies'>"
        f"All rows ({sum(int(item.get('rows') or 0) for item in filters):,})</a>"
    )
    return f"<div class='actions' style='margin-bottom:10px'>{links}</div>"


def _pager(funnel: Mapping[str, Any]) -> str:
    offset = int(funnel.get("offset") or 0)
    size = int(funnel.get("limit") or 0)
    current = str(funnel.get("verdict") or "")
    suffix = f"&verdict={fmt.attr(current)}" if current else ""
    if not (funnel.get("has_previous") or funnel.get("has_next")):
        return ""
    parts = []
    if funnel.get("has_previous"):
        parts.append(f"<a class='btn btn-sm' href='/strategies?from={max(0, offset - size)}{suffix}'>"
                     "Previous page</a>")
    if funnel.get("has_next"):
        parts.append(f"<a class='btn btn-sm' href='/strategies?from={offset + size}{suffix}'>"
                     "Next page</a>")
    return f"<div class='actions' style='margin-top:10px'>{''.join(parts)}</div>"


def _results_rows(rows: Sequence[Mapping[str, Any]]) -> list[list[str]]:
    table_rows: list[list[str]] = []
    for item in rows:
        verdict = str(item.get("verdict_label") or "")
        rule = f"<code>{fmt.text(str(item.get('candidate_id', '')))}</code>"
        params = str(item.get("parameters") or "")
        if params:
            rule += f"<span class='small muted' style='display:block'>{fmt.text(params)}</span>"
        signature = str(item.get("signature") or "")
        if signature:
            rule += (
                f"<span class='small muted' style='display:block' title="
                f"'{fmt.attr(signature)}'>version "
                f"{fmt.text(signature[:64])}{'…' if len(signature) > 64 else ''}</span>"
            )
        table_rows.append(
            [
                cell(rule, label="Candidate"),
                cell(
                    f"{fmt.text(str(item.get('symbol', '')))} / "
                    f"{fmt.text(str(item.get('timeframe', '')))}",
                    label="Symbol / TF",
                ),
                cell(fmt.text(str(item.get("family", ""))), label="Family"),
                cell(chip(verdict, _VERDICT_TONES.get(verdict, "muted")), label="Result"),
                cell(fmt.text(str(item.get("trades", 0))), label="Trades", num=True),
                cell(fmt.text(str(item.get("reason", ""))), label="Why"),
            ]
        )
    return table_rows


def _results_section(funnel: Mapping[str, Any]) -> str:
    rows = [dict(item) for item in (funnel.get("rows") or ())]
    filters = [dict(item) for item in (funnel.get("filters") or ())]
    current = str(funnel.get("verdict") or "")
    shown = int(funnel.get("shown") or 0)
    matching = int(funnel.get("matching") or 0)
    current_label = next(
        (str(item.get("label")) for item in filters if str(item.get("key")) == current), ""
    )
    caption = (
        f"Showing {shown:,} of {matching:,} rows"
        + (f' filtered to "{current_label}"' if current_label else " across every status")
        + ". Every row carries its own reason."
    )
    body = _filter_links(funnel)
    if rows:
        body += table(
            [
                ("Candidate", "text"),
                ("Symbol / TF", "text"),
                ("Family", "text"),
                ("Result", "text"),
                ("Trades", "num"),
                ("Why", "text"),
            ],
            _results_rows(rows),
            caption=caption,
        )
        body += _pager(funnel)
    else:
        body += empty_state(
            "Nothing to list",
            "No evaluation is on record for these symbols yet, so there is no result to show. "
            "Nothing has been hidden: there is simply nothing here.",
        )
    return section(
        "Every evaluated result, with its reason",
        body,
        note="A passing row is a short-list entry, not a profitable strategy: it is measured "
        "on the bars on record, under the cost assumptions in the record's notes, and it says "
        "nothing about the future.",
        tools=chip(
            f"{_num(dict(funnel.get('counts') or {}).get('passed_strategies'))} passed", "ok"
        ),
        id="results",
    )


# ---------------------------------------------------------------------------
# search, selection, and the documented templates
# ---------------------------------------------------------------------------


def _search_section(view: Mapping[str, Any]) -> str:
    query = dict(view.get("query") or {})
    families = [dict(item) for item in (view.get("families") or ())]
    options = "".join(
        f"<option value='{fmt.attr(str(item.get('key', '')))}'"
        f"{' selected' if str(item.get('key')) == query.get('family') else ''}>"
        f"{fmt.text(str(item.get('label', '')))}</option>"
        for item in families
    )
    return section(
        "Search the documented templates",
        "<form method='get' action='/strategies' class='form'>"
        "<div class='form-row'>"
        "<div class='field'><label for='q'>Words</label>"
        f"<input type='text' id='q' name='q' value='{fmt.attr(str(query.get('text', '')))}' "
        "autocomplete='off' placeholder='e.g. breakout, momentum, Bollinger'>"
        "<span class='hint'>Names, summaries, rules, timeframes and sources.</span></div>"
        "<div class='field'><label for='family'>Family</label>"
        f"<select id='family' name='family'><option value=''>All families</option>"
        f"{options}</select></div></div>"
        "<div class='actions'><button class='btn' type='submit'>Filter</button>"
        "<a class='btn' href='/strategies'>Clear</a></div></form>",
        note="Search does not rank by anything resembling performance.",
        id="search",
    )


def _selections_section(view: Mapping[str, Any], nonce: str) -> str:
    selections = [dict(item) for item in (view.get("selections") or ())]
    if not selections:
        return ""
    rows = [
        [
            cell(f"<code>{fmt.text(str(item.get('variant_id', '')))}</code>", label="Variant"),
            cell(fmt.text(str(item.get("symbol", ""))), label="Symbol"),
            cell(fmt.text(str(item.get("timeframe", ""))), label="Timeframe"),
            cell(f"<code>{fmt.text(str(item.get('dataset_id', '')))}</code>", label="Dataset"),
            cell(fmt.text(str(item.get("selected_at", ""))), label="Selected"),
        ]
        for item in selections
    ]
    forms = "".join(
        "<form method='post' action='/actions/strategy-deselect' style='display:inline'>"
        f"<input type='hidden' name='nonce' value='{fmt.attr(nonce)}'>"
        f"<input type='hidden' name='variant_id' "
        f"value='{fmt.attr(str(item.get('variant_id', '')))}'>"
        "<button class='btn btn-sm' type='submit'>Deselect "
        f"{fmt.text(str(item.get('variant_id', '')))}</button></form>"
        for item in selections
    )
    return section(
        "Your selections",
        table(
            [
                ("Variant", "text"),
                ("Symbol", "text"),
                ("Timeframe", "text"),
                ("Dataset", "text"),
                ("Selected", "text"),
            ],
            rows,
            caption="Variants you selected, and the attempt each came from",
        )
        + f"<div class='actions' style='margin-top:10px'>{forms}</div>",
        note="Selecting recorded your choice; it did not trade. Deselecting is always allowed "
        "and takes effect immediately.",
        tools=chip(f"{len(selections)} SELECTED", "warn"),
        id="selections",
    )


def _template_section(
    item: Mapping[str, Any], variants: Sequence[Mapping[str, Any]], nonce: str
) -> str:
    availability = dict(item.get("availability") or {})
    source = dict(item.get("source") or {})
    parameters = [dict(entry) for entry in (item.get("parameters") or ())]
    requires = [str(entry) for entry in (item.get("requires") or ())]
    body = f"<p class='small muted' style='margin:0'>{fmt.text(str(item.get('summary', '')))}</p>"

    body += "<h3 style='margin-top:16px'>Rules</h3><ol class='check-list'>" + "".join(
        f"<li>{fmt.text(str(rule))}</li>" for rule in (item.get("rules") or ())
    ) + "</ol>"

    body += "<h3 style='margin-top:16px'>Parameters</h3>" + table(
        [
            ("Name", "text"),
            ("Kind", "text"),
            ("Default", "num"),
            ("Grid", "text"),
            ("Notes", "text"),
        ],
        [
            [
                cell(f"<code>{fmt.text(str(entry.get('name', '')))}</code>", label="Name"),
                cell(fmt.text(str(entry.get("kind", ""))), label="Kind"),
                cell(fmt.text(str(entry.get("default", ""))), label="Default", num=True),
                cell(
                    fmt.text(", ".join(str(part) for part in (entry.get("grid") or ())) or "—"),
                    label="Grid",
                ),
                cell(fmt.text(str(entry.get("description", ""))), label="Notes"),
            ]
            for entry in parameters
        ],
        caption="The card's declared parameters and the predeclared grid",
    )

    body += "<h3 style='margin-top:16px'>Where it applies</h3>" + facts(
        [
            ("Declared timeframes", _text_list(item.get("timeframes"))),
            (
                "Intended horizon",
                fmt.text(f"{item.get('horizon_bars', '')} bar(s)"),
            ),
            ("Minimum bars", fmt.text(str(item.get("min_bars", "")))),
            ("Data required", _text_list(requires)),
            ("Licence", chip(str(item.get("licence", "")), "muted")),
            ("Source", fmt.text(str(source.get("citation", "")))),
            ("Source kind", fmt.text(str(source.get("kind", "")))),
        ]
    )
    if source.get("note"):
        body += (
            f"<p class='small muted' style='margin:10px 0 0'>{fmt.text(str(source.get('note')))}</p>"
        )

    for heading, key in (("Assumptions", "assumptions"), ("Limitations", "limitations")):
        items = [str(entry) for entry in (item.get(key) or ())]
        if not items:
            continue
        body += (
            f"<h3 style='margin-top:16px'>{heading}</h3><ul class='check-list'>"
            + "".join(f"<li>{fmt.text(entry)}</li>" for entry in items)
            + "</ul>"
        )

    if not availability.get("evaluable"):
        body += notice(
            "Not evaluable in this deployment",
            fmt.text(str(availability.get("reason", ""))),
            tone="warn",
        )

    eligible = [entry for entry in variants if str(entry.get("status")) == "ELIGIBLE"]
    if variants:
        variant_rows = []
        for variant in variants:
            attempt = dict(variant.get("attempt") or {})
            variant_rows.append(
                [
                    cell(
                        f"<code>{fmt.text(str(variant.get('variant_id', '')))}</code>",
                        label="Variant",
                    ),
                    cell(
                        fmt.text(
                            ", ".join(
                                f"{key}={value}"
                                for key, value in (variant.get("parameters") or {}).items()
                            )
                        ),
                        label="Parameters",
                    ),
                    cell(
                        _status_chip(str(variant.get("status", "NOT_ATTEMPTED"))),
                        label="Status",
                    ),
                    cell(fmt.text(str(attempt.get("reason", "")) or "—"), label="Reason"),
                    cell(fmt.text(str(attempt.get("q_value", "")) or "—"), label="q", num=True),
                    cell(
                        fmt.text(str(attempt.get("reality_check", "")) or "—"),
                        label="RC p",
                        num=True,
                    ),
                ]
            )
        body += "<h3 style='margin-top:16px'>Predeclared variants</h3>"
        body += table(
            [
                ("Variant", "text"),
                ("Parameters", "text"),
                ("Status", "text"),
                ("Reason", "text"),
                ("q", "num"),
                ("RC p", "num"),
            ],
            variant_rows,
            caption=f"{len(variants)} predeclared variant(s) — every attempt is recorded",
        )
        if eligible:
            options = "".join(
                f"<option value='{fmt.attr(str(entry.get('variant_id', '')))}'>"
                f"{fmt.text(str(entry.get('variant_id', '')))}</option>"
                for entry in eligible
            )
            body += (
                "<form method='post' action='/actions/strategy-select' class='form' "
                "style='margin-top:12px'>"
                f"<input type='hidden' name='nonce' value='{fmt.attr(nonce)}'>"
                "<div class='field'><label for='select-"
                f"{fmt.attr(str(item.get('card_id', '')))}'>Variant to select (only an eligible "
                f"attempt can be chosen)</label>"
                f"<select id='select-{fmt.attr(str(item.get('card_id', '')))}' "
                f"name='variant_id'>{options}</select></div>"
                "<label class='field field--check'><input type='checkbox' name='confirmed' "
                "value='yes'><span class='label'>This is my own choice, and I understand it is "
                "a historical result, not a promise.</span></label>"
                "<div class='actions'><button class='btn' type='submit'>Record my selection"
                "</button></div></form>"
            )
        else:
            body += (
                "<p class='small muted' style='margin:12px 0 0'>No variant of this template has "
                "an eligible attempt on record, so nothing here can be selected.</p>"
            )
    return section(
        str(item.get("name", "")),
        body,
        note=f"{item.get('family_label', '')} · {item.get('variants', 0)} variant(s) · "
        f"{item.get('attempted', 0)} attempted",
        tools=_status_chip(str(item.get("status", ""))),
        id=f"template-{fmt.attr(str(item.get('card_id', '')).replace('_', '-'))}",
    )


# ---------------------------------------------------------------------------
# screening, one-off evaluation, and the predeclared rules
# ---------------------------------------------------------------------------


def _screen_section(view: Mapping[str, Any], nonce: str) -> str:
    catalogue = [dict(item) for item in (view.get("catalogue") or ())]
    criteria = dict(view.get("criteria") or {})
    options = "".join(
        f"<option value='{fmt.attr(str(item.get('card_id', '')))}'>"
        f"{fmt.text(str(item.get('name', '')))}</option>"
        for item in catalogue
        if (item.get("availability") or {}).get("evaluable")
    )
    form = (
        "<form method='post' action='/actions/strategy-screen' class='form'>"
        f"<input type='hidden' name='nonce' value='{fmt.attr(nonce)}'>"
        "<div class='form-row'>"
        "<div class='field'><label for='screen-card'>Template</label>"
        f"<select id='screen-card' name='card_id'><option value=''>All evaluable templates"
        f"</option>{options}</select></div>"
        "<div class='field'><label for='screen-symbol'>Symbol</label>"
        "<input type='text' id='screen-symbol' name='symbol' value='' placeholder='e.g. EURUSD' "
        "autocomplete='off'></div>"
        "<div class='field'><label for='screen-timeframe'>Timeframe</label>"
        "<input type='text' id='screen-timeframe' name='timeframe' value='' "
        "placeholder='blank = every declared timeframe' autocomplete='off'></div>"
        "</div>"
        "<div class='actions'><button class='btn btn--primary' type='submit'>"
        "Screen recorded evaluations</button></div></form>"
    )
    body = (
        "<p class='small muted' style='margin:0 0 10px'>Screening applies the predeclared "
        "criteria and the multiple-testing control to evaluations recorded on the Data &amp; "
        "evaluation page, then writes every attempt to the ledger — including the ones that "
        "fail.</p>"
        + form
    )
    if not view.get("has_evidence_source"):
        body += notice(
            "No evaluation source is wired in",
            "This deployment cannot read recorded evaluations, so screening would have "
            "nothing to read. Evaluate a candidate on Data & evaluation first.",
            tone="warn",
        )
    return section(
        "Screen and record attempts",
        body,
        note=f"Criteria version {criteria.get('criteria_version', '')}. The thresholds are "
        "AXIOM's declared choices, fixed before any result is read.",
        tools=chip("predeclared", "muted"),
        id="screen",
    )


def _evaluate_one_section(view: Mapping[str, Any], nonce: str) -> str:
    catalogue = [dict(item) for item in (view.get("catalogue") or ())]
    if not any((item.get("availability") or {}).get("evaluable") for item in catalogue):
        return ""
    return section(
        "Evaluate one variant",
        "<p class='small muted' style='margin:0 0 10px'>This runs the same chronological "
        "evaluator, costs and locked-test custody as the paper lane, then records the attempt. "
        "Both the dataset id and the data class must be yours: the class decides whether a "
        "result may ever promote anything.</p>"
        "<form method='post' action='/actions/strategy-evaluate' class='form'>"
        f"<input type='hidden' name='nonce' value='{fmt.attr(nonce)}'>"
        "<div class='form-row'>"
        "<div class='field'><label for='eval-variant'>Variant</label>"
        "<input type='text' id='eval-variant' name='variant_id' autocomplete='off' "
        "placeholder='trend_sma_cross::fast_period=8-slow_period=21'></div>"
        "<div class='field'><label for='eval-symbol'>Symbol</label>"
        "<input type='text' id='eval-symbol' name='symbol' autocomplete='off' "
        "placeholder='EURUSD'></div>"
        "<div class='field'><label for='eval-timeframe'>Timeframe</label>"
        "<input type='text' id='eval-timeframe' name='timeframe' autocomplete='off' "
        "placeholder='5m'></div>"
        "<div class='field'><label for='eval-dataset'>Dataset id</label>"
        "<input type='text' id='eval-dataset' name='dataset_id' autocomplete='off' "
        "placeholder='e.g. eurusd-2026-q1'></div>"
        "<div class='field'><label for='eval-class'>Data class</label>"
        "<select id='eval-class' name='data_class'><option value=''>Choose…</option>"
        "<option value='USER_SUPPLIED_HISTORICAL'>USER_SUPPLIED_HISTORICAL</option>"
        "<option value='USER_SUPPLIED_PRODUCTION'>USER_SUPPLIED_PRODUCTION</option>"
        "<option value='USER_SUPPLIED_DEMO'>USER_SUPPLIED_DEMO</option>"
        "<option value='SYNTHETIC_FIXTURE'>SYNTHETIC_FIXTURE</option></select></div>"
        "</div>"
        "<div class='actions'><button class='btn btn--primary' type='submit'>"
        "Evaluate and record</button></div></form>",
        note="A demo or fixture class can be studied, but it can never make a variant eligible.",
        id="evaluate-one",
    )


def _criteria_section(view: Mapping[str, Any]) -> str:
    criteria = dict(view.get("criteria") or {})
    return section(
        "The predeclared criteria",
        facts(
            [
                (
                    "Closed trades (locked test)",
                    fmt.text(str(criteria.get("min_closed_trades", ""))),
                ),
                ("Max drawdown", fmt.text(str(criteria.get("max_drawdown_fraction", "")))),
                (
                    "Walk-forward folds",
                    fmt.text(str(criteria.get("min_walk_forward_folds", ""))),
                ),
                (
                    "Positive fold fraction",
                    fmt.text(str(criteria.get("min_positive_fold_fraction", ""))),
                ),
                (
                    "Validation positive",
                    "yes" if criteria.get("require_positive_validation") else "no",
                ),
                (
                    "Locked test positive",
                    "yes" if criteria.get("require_positive_locked_test") else "no",
                ),
                (
                    "Data class must be able to promote",
                    "yes" if criteria.get("require_promoting_data_class") else "no",
                ),
                ("FDR q-value limit", fmt.text(str(criteria.get("max_q_value", "")))),
                (
                    "Reality Check p limit",
                    fmt.text(str(criteria.get("max_reality_check", ""))),
                ),
            ]
        ),
        note="Changing a threshold invalidates the attempts recorded under the old version, "
        "which is why the version is stored with every attempt.",
        id="criteria",
    )


def _multiplicity_section(view: Mapping[str, Any]) -> str:
    return section(
        "Multiple testing",
        f"<p class='small muted' style='margin:0'>"
        f"{fmt.text(str(view.get('multiplicity_note', '')))}</p>",
        note="Selection bias is controlled, not removed.",
        tools=chip("selection bias controlled, not removed", "muted"),
        id="multiplicity",
    )


def _monitoring_section(view: Mapping[str, Any], nonce: str) -> str:
    coverage_rows = [dict(item) for item in (view.get("coverage") or ())]
    body = (
        "<div class='actions' style='margin-bottom:10px'>"
        + action_form("strategy-monitor", nonce, "Measure what can be monitored")
        + "</div>"
    )
    if coverage_rows:
        body += table(
            [
                ("Symbol", "text"),
                ("Timeframe", "text"),
                ("Bars", "num"),
                ("Templates", "num"),
                ("Monitored", "text"),
                ("Why not", "text"),
            ],
            [
                [
                    cell(fmt.text(str(row.get("symbol", ""))), label="Symbol"),
                    cell(fmt.text(str(row.get("timeframe", ""))), label="Timeframe"),
                    cell(fmt.text(str(row.get("bars", ""))), label="Bars", num=True),
                    cell(
                        fmt.text(str(len(row.get("templates") or ()))),
                        label="Templates",
                        num=True,
                    ),
                    cell(
                        chip("yes", "ok") if row.get("monitorable") else chip("no", "muted"),
                        label="Monitored",
                    ),
                    cell(fmt.text(str(row.get("reason", "")) or "—"), label="Why not"),
                ]
                for row in coverage_rows
            ],
            caption="Symbols and timeframes, and whether a template declares them",
        )
    else:
        body += empty_state(
            "No coverage measured yet",
            "Coverage needs bars from this feed and a symbol list. A timeframe no template "
            "declares is never analysed: a bar feed is not evidence that a strategy supports it.",
        )
    return section(
        "Automatic monitoring: what can be analysed",
        body,
        note="Monitoring scores only on the timeframes each template declares, and never "
        "promotes anything by itself.",
        tools=chip(f"{len(coverage_rows)} ROW(S)", "muted"),
        id="monitoring",
    )


def _claims_section(view: Mapping[str, Any]) -> str:
    notes = [str(item) for item in (view.get("non_claims") or ())]
    body = (
        "<ul class='check-list'>"
        + "".join(f"<li>{fmt.text(note)}</li>" for note in notes)
        + "</ul>"
        if notes
        else "<p class='small muted' style='margin:0'>This page makes no claim about profit, "
        "and no historical result is a forecast.</p>"
    )
    return section(
        "What this page does not claim",
        body,
        note="A documented template is a shape, not an edge.",
        id="claims",
    )


# ---------------------------------------------------------------------------
# the page
# ---------------------------------------------------------------------------



def _template_cards(catalogue: Sequence[Mapping[str, Any]], nonce: str) -> list[str]:
    """One template open, the rest folded: a library is browsed, not scrolled.

    The first card stays open because the page's own contents list points at it;
    every other card is one click away, with its variant count on the summary and
    its whole table still in the document.
    """

    cards: list[str] = []
    for index, item in enumerate(catalogue):
        variants = [dict(entry) for entry in (item.get("variants_detail") or ())]
        html = _template_section(item, variants, nonce)
        if index == 0:
            cards.append(html)
            continue
        label = str(item.get("title") or item.get("name") or item.get("card_id") or "Template")
        cards.append(
            common.fold(
                label,
                html,
                count=f"{len(variants)} variants",
                note=str(item.get("family") or ""),
            )
        )
    return cards


def render_strategies(ctx: Any) -> str:
    view = dict((ctx.extra or {}).get("strategies") or {})
    nonce = str(getattr(ctx, "action_nonce", ""))
    catalogue = [dict(item) for item in (view.get("catalogue") or ())]
    counts = dict(view.get("counts") or {})
    library = dict(view.get("library") or {})
    result = dict(view.get("result") or {})
    funnel = dict(view.get("funnel") or {})
    selections = [dict(item) for item in (view.get("selections") or ())]

    head = page_head(
        "Strategy library",
        "A research index, not an order screen",
        "Documented templates and their parameter variants, read as four separate questions: "
        "what is declared, what is compatible, what has been measured, and what passed.",
        badges=common.scope_badges(ctx)
        + chip(f"{_num(counts.get('attempts'))} attempt(s) recorded", "muted")
        + chip(f"{_num(counts.get('selected'))} selected", "warn" if counts.get("selected") else "muted")
        + chip(f"grid {fmt.text(str(view.get('grid_version', '')))}", "muted"),
        actions=button_link("/data", "Data &amp; evaluation", small=True)
        + button_link("/autopilot", "PAPER command center", small=True),
    )

    if not view.get("available"):
        return head + layout(
            notice(
                "The strategy library is not wired into this workstation",
                "This process has no library page data. Nothing is lost: the library is a "
                "research index and is not required for trading.",
                tone="warn",
            )
            + common.safety_section(id="safety"),
            wide=True,
        )
    if view.get("error"):
        return head + layout(
            notice(
                "The strategy library could not be read",
                f"The library reported <code>{fmt.text(str(view.get('error')))}</code>. Nothing "
                "was selected, evaluated or written, and the rest of the workstation is "
                "unaffected.",
                tone="danger",
                role="alert",
            )
            + common.safety_section(id="safety"),
            wide=True,
        )

    body = notice(
        "A research index, not an order screen",
        "These are documented templates and their parameter variants. Nothing here selects a "
        "strategy for you, sizes a position, sets a stop, arms live execution, or changes an "
        "execution mode. A selected strategy still goes through the live plan's review, the "
        "confirmation phrase, the risk gate, and the protective-stop rule.",
        tone="info",
    )
    if result.get("message"):
        body += notice(
            "The last library action",
            fmt.text(str(result.get("message"))),
            tone="ok" if result.get("ok") else "warn",
            role="status" if result.get("ok") else "alert",
        )

    jumps = [
        ("funnel", "How many"),
        ("ladder", "Ladder"),
        ("evaluate-all", "Evaluate all"),
        ("results", "Results"),
        ("search", "Search"),
    ]
    if selections:
        jumps.append(("selections", "Selections"))
    if catalogue:
        jumps.append((f"template-{catalogue[0].get('card_id', '')}".replace("_", "-"), "Templates"))
    jumps += [("screen", "Screen"), ("criteria", "Criteria"), ("monitoring", "Monitoring")]

    main = (
        body
        + jump_nav_bar(jumps)
        + jump_nav(jumps)
        + _funnel_section(funnel)
        + _ladder_section(funnel)
        + _evaluate_all_section(funnel, nonce)
        + (funnel and _results_section(funnel) or "")
        + _search_section(view)
        + _selections_section(view, nonce)
        + "".join(_template_cards(catalogue, nonce))
        + _screen_section(view, nonce)
        + _evaluate_one_section(view, nonce)
        + _criteria_section(view)
        + _multiplicity_section(view)
        + _monitoring_section(view, nonce)
        + _claims_section(view)
        + section(
            "What is in the library",
            facts(
                [
                    ("Templates", fmt.text(str(library.get("templates", 0)))),
                    ("Families", fmt.text(str(library.get("families", 0)))),
                    (
                        "Evaluable here",
                        fmt.text(str(library.get("evaluable_templates", 0))),
                    ),
                    ("Variants", fmt.text(str(library.get("variants", 0)))),
                    ("Attempts recorded", fmt.text(str(counts.get("attempts", 0)))),
                    ("Selected", fmt.text(str(counts.get("selected", 0)))),
                ]
            ),
            note=str(library.get("note", ""))
            or "Documented shapes and their predeclared parameter combinations.",
            tools=chip(f"grid {fmt.text(str(view.get('grid_version', '')))}", "muted"),
            id="library",
        )
    )
    return head + layout(main, wide=True)

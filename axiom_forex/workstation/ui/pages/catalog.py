"""The declared catalog, its forex symbol offer, and the paper test that follows it.

Three questions, kept apart on purpose:

* which symbols the terminal actually reports as currency pairs, and which ones
  it does not (a metal, a share CFD and an index are excluded, with the reason);
* what the stored evaluation measured, over every declared candidate;
* whether the local paper test is built, and what it is following.

Nothing on this page can send an order.  The paper test's fills are simulated
locally from the terminal's own quotes, and the audit is a report writer: it
reads the stored record and lists every candidate, including the ones that never
ran.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from .. import format as fmt
from ..components import (
    action_form,
    blockers,
    cell,
    chip,
    empty_state,
    field,
    notice,
    section,
    stat,
    stats,
    table,
)

#: How a catalog verdict is said, and in which tone.
_VERDICT_TONES = {"TESTED": "ok", "SKIPPED": "warn", "FAILED": "danger"}

MAX_SYMBOL_ROWS = 12
MAX_EXCLUDED_LISTED = 10


def _num(value: Any) -> str:
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return "0"


def _blockers(items: Sequence[Mapping[str, Any]]) -> str:
    """The engine's own refusals, with their reasons and their hints."""

    if not items:
        return ""
    return blockers(
        [
            {
                "title": item.get("message") or item.get("reason") or "Blocked",
                "detail": item.get("hint") or item.get("detail") or "",
                "code": item.get("reason") or "",
                "tone": "danger",
            }
            for item in items
        ]
    )


# ---------------------------------------------------------------------------
# 1. what the catalog is, and which symbols it may run on
# ---------------------------------------------------------------------------


def _counts_section(view: Mapping[str, Any]) -> str:
    evaluation = dict(view.get("evaluation") or {})
    counts = dict(evaluation.get("catalog") or {})
    families = counts.get("families") or {}
    return section(
        "Catalog and the Monday paper test",
        stats(
            [
                stat(
                    "Candidates",
                    _num(counts.get("candidates")),
                    foot="declared, reproducible",
                ),
                stat(
                    "Distinct rule structures",
                    _num(counts.get("distinct_rule_structures")),
                    foot="logic, before parameters",
                ),
                stat(
                    "Families",
                    str(len(families)),
                    foot="trend, momentum, reversion, breakout, volatility, multi-timeframe",
                ),
            ]
        )
        + "<p class='small muted' style='margin:12px 0 0'>A candidate is a declared rule structure at "
        "one predeclared parameter set. A candidate that passes the screen is a short-list "
        "entry, not a profitable strategy and not a licence to trade.</p>",
        note=str(view.get("claim", ""))
        or "The catalog is a declared search on historical bars.",
        tools=chip("declared, not discovered", "muted"),
        id="catalog",
    )


def _symbols_section(view: Mapping[str, Any]) -> str:
    symbols = dict(view.get("symbols") or {})
    if not symbols.get("available"):
        return section(
            "Forex symbols from your terminal",
            notice(
                "No forex symbols to choose yet",
                fmt.text(
                    str(
                        symbols.get("message")
                        or "Connect the terminal first: the symbol offer comes from what the "
                        "terminal reports, not from a list in this repository."
                    )
                ),
                tone="warn",
            ),
            note="Nothing is substituted for a symbol the terminal did not report.",
            tools=chip("NOT CONNECTED", "muted"),
            id="fx-symbols",
        )

    counted = dict(symbols.get("counts") or {})
    note = (
        f"{_num(counted.get('forex_pairs'))} currency pair(s) of "
        f"{_num(counted.get('reported'))} reported symbol(s); "
        f"{_num(counted.get('excluded'))} excluded as not a pair (metals, share CFDs, indices). "
        "Majors first: " + ", ".join(str(item) for item in (symbols.get("majors") or ())) + "."
    )
    not_examined = int(counted.get("not_examined", 0) or 0)
    if not_examined:
        note += (
            f" Only {_num(counted.get('examined'))} of those names could be read from this "
            f"terminal wrapper, so {_num(not_examined)} name(s) were not classified and are not "
            "counted above."
        )
    body = notice("Forex symbols from your terminal", fmt.text(note), tone="ok")

    recommended = [str(item) for item in (symbols.get("recommended") or ())]
    if recommended:
        body += (
            "<p class='small muted' style='margin:12px 0 0'>Recommended now: "
            + " ".join(chip(name, "ok") for name in recommended)
            + "</p>"
        )

    choices = [dict(item) for item in (symbols.get("choices") or ())]
    if choices:
        shown = choices[:MAX_SYMBOL_ROWS]
        caption = "Forex symbols the terminal reports"
        if len(choices) > len(shown):
            caption += (
                f" — first {len(shown)} of the {_num(counted.get('choices_shown', len(choices)))} "
                "this page carries"
            )
        body += table(
            [
                ("Symbol", "text"),
                ("Pair", "text"),
                ("Major", "text"),
                ("Usable", "text"),
                ("If not usable, why", "text"),
            ],
            [
                [
                    cell(f"<code>{fmt.text(str(item.get('broker_symbol', '')))}</code>", label="Symbol"),
                    cell(fmt.text(str(item.get("pair", ""))), label="Pair"),
                    cell("yes" if item.get("major") else "no", label="Major"),
                    cell(
                        chip("usable", "ok") if item.get("usable") else chip("not usable", "warn"),
                        label="Usable",
                    ),
                    cell(fmt.text(str(item.get("reason", ""))), label="If not usable, why"),
                ]
                for item in shown
            ],
            caption=caption,
        )

    excluded = [dict(item) for item in (symbols.get("excluded") or ())]
    if excluded:
        listed = excluded[:MAX_EXCLUDED_LISTED]
        text = " ".join(
            f"{fmt.text(str(item.get('broker_symbol', '')))} "
            f"({fmt.text(str(item.get('kind', '')))})"
            for item in listed
        )
        hidden = int(counted.get("excluded", len(excluded))) - len(listed)
        body += f"<p class='small muted' style='margin:12px 0 0'>Not offered as forex: {text}"
        if hidden > 0:
            body += (
                f" — and {_num(hidden)} more, all counted; every excluded symbol and its reason "
                "is on the Account and symbols page."
            )
        body += "</p>"
    return section(
        "Forex symbols from your terminal",
        body,
        note="Only a symbol the terminal describes with both currency legs is offered. A metal or "
        "an index is excluded by name and by reason, never by a guess.",
        tools=chip(f"{_num(counted.get('forex_pairs'))} PAIR(S)", "ok"),
        id="fx-symbols",
    )


# ---------------------------------------------------------------------------
# 2. the evaluation and the paper test
# ---------------------------------------------------------------------------


def _evaluate_section(view: Mapping[str, Any], nonce: str) -> str:
    symbols = dict(view.get("symbols") or {})
    evaluation = dict(view.get("evaluation") or {})
    timeframes = [str(item) for item in (symbols.get("timeframes") or ("1h",))]
    default_tf = str(symbols.get("default_timeframe") or "")
    usable = [dict(item) for item in (symbols.get("choices") or ()) if item.get("usable")]
    options = "".join(
        f"<option value='{fmt.attr(str(item.get('broker_symbol', '')))}'"
        f"{' selected' if item.get('recommended') else ''}>"
        f"{fmt.text(str(item.get('label') or item.get('broker_symbol') or ''))}</option>"
        for item in usable
    )
    body = ""
    if evaluation.get("running"):
        body += notice(
            "Evaluation running",
            f"Started {fmt.text(str(evaluation.get('started_at', '')))}. It runs in the background "
            "on the terminal's own bars; reload this page for the result.",
            tone="info",
        )
    if evaluation.get("on_record"):
        recorded = dict(evaluation.get("counts") or {})
        screen = dict(recorded.get("screen") or {})
        body += notice(
            "Evaluation on record",
            f"{_num(recorded.get('tested'))} row(s) tested, {_num(recorded.get('skipped'))} "
            f"skipped, {_num(recorded.get('failed'))} failed; "
            f"{_num(screen.get('PASSED_SCREEN'))} passed the screen. Data origin: "
            f"{fmt.text(str(evaluation.get('data_origin', 'unknown')))}. Paper set: "
            f"{fmt.text(str(evaluation.get('paper_set_summary', 'none')))}.",
            tone="ok",
        )
        for line in evaluation.get("table") or ():
            body += f"<pre class='template small'>{fmt.text(str(line))}</pre>"
    elif evaluation.get("message"):
        body += notice(
            "The last evaluation",
            fmt.text(str(evaluation.get("message"))),
            tone="warn",
        )

    if usable:
        body += (
            "<form method='post' action='/actions/catalog-evaluate' class='form'>"
            f"<input type='hidden' name='nonce' value='{fmt.attr(nonce)}'>"
            "<div class='form-row'>"
            "<div class='field'><label for='catalog-symbols'>Symbols "
            "<span class='hint' style='display:inline'>(Ctrl/Cmd-click for more than one)</span>"
            f"</label><select id='catalog-symbols' name='symbols' multiple size='5'>{options}"
            "</select></div>"
            + field(
                "timeframe",
                "Timeframe",
                kind="select",
                value=default_tf or (timeframes[0] if timeframes else ""),
                options=[(item, item) for item in timeframes],
                field_id="catalog-timeframe",
            )
            + "</div><div class='actions' style='margin-top:12px'>"
            "<button class='btn btn--primary' type='submit'>Evaluate the catalog on this history"
            "</button></div></form>"
        )
    else:
        body += empty_state(
            "No usable forex symbol to evaluate",
            "The catalog is evaluated on the terminal's own bars for a symbol whose specification "
            "the terminal reported. Nothing is substituted.",
        )
    return section(
        "1 · Evaluate the catalog",
        body,
        note="Uses the terminal's own historical bars and its own symbol specs. Skipped rows are "
        "listed with a reason; nothing is replaced with sample data.",
        tools=chip("research only", "muted"),
        id="catalog-evaluate",
    )


def _paper_section(view: Mapping[str, Any], nonce: str) -> str:
    paper = dict(view.get("paper") or {})
    snapshot = dict(paper.get("snapshot") or {})
    status = str(snapshot.get("status") or "IDLE")
    open_positions = len(snapshot.get("positions") or ())
    built = bool(paper.get("built"))
    body = stats(
        [
            stat("Paper test", fmt.text(status), foot="locally simulated fills"),
            stat(
                "Open paper positions",
                str(open_positions),
                foot="priced from broker ticks",
            ),
            stat(
                "Simulated closes",
                str(len(snapshot.get("fills") or ())),
                foot="ledger on disk",
            ),
        ]
    )
    active = ", ".join(str(item) for item in (snapshot.get("active_symbols") or ())) or "none"
    body += (
        f"<p class='small muted' style='margin:12px 0 0'>Active symbols: "
        f"{fmt.text(active)}</p>"
    )
    quotes = [dict(item) for item in (snapshot.get("quotes") or ())]
    if quotes:
        parts = []
        for item in quotes[:8]:
            age = (
                f" ({fmt.text(str(item.get('age_seconds')))}s)"
                if item.get("age_seconds") is not None
                else ""
            )
            parts.append(
                f"{fmt.text(str(item.get('symbol', '')))} {fmt.text(str(item.get('state', '')))}{age}"
            )
        body += (
            "<p class='small muted' style='margin:6px 0 0'>Quote freshness: "
            + " ".join(parts)
            + f" · allowed age {fmt.text(str(snapshot.get('max_quote_age_seconds', '')))}s</p>"
        )

    watches = [dict(item) for item in (paper.get("watches") or ())]
    if watches:
        body += table(
            [
                ("Symbol", "text"),
                ("TF", "text"),
                ("Family", "text"),
                ("Trades in evaluation", "num"),
                ("Positive folds", "num"),
                ("Logic", "text"),
            ],
            [
                [
                    cell(fmt.text(str(item.get("symbol", ""))), label="Symbol"),
                    cell(fmt.text(str(item.get("timeframe", ""))), label="TF"),
                    cell(fmt.text(str(item.get("family", ""))), label="Family"),
                    cell(
                        fmt.text(str(item.get("trades_in_evaluation", 0))),
                        label="Trades in evaluation",
                        num=True,
                    ),
                    cell(
                        fmt.text(str(item.get("folds_positive", 0))),
                        label="Positive folds",
                        num=True,
                    ),
                    cell(fmt.text(str(item.get("summary", ""))), label="Logic"),
                ]
                for item in watches
            ],
            caption="The evaluated rows this test follows",
        )

    positions = [dict(item) for item in (snapshot.get("positions") or ())]
    if positions:
        body += table(
            [
                ("Row", "text"),
                ("Side", "text"),
                ("Entry", "num"),
                ("Stop", "num"),
                ("Opened", "text"),
            ],
            [
                [
                    cell(f"<code>{fmt.text(str(item.get('candidate_id', '')))}</code>", label="Row"),
                    cell(fmt.text(str(item.get("direction", ""))), label="Side"),
                    cell(fmt.text(str(item.get("entry_price", ""))), label="Entry", num=True),
                    cell(fmt.text(str(item.get("stop", ""))), label="Stop", num=True),
                    cell(fmt.text(str(item.get("entry_at", ""))), label="Opened"),
                ]
                for item in positions
            ],
            caption="Open paper positions",
        )

    signals = [dict(item) for item in (snapshot.get("signals") or ())]
    if signals:
        body += table(
            [
                ("Row", "text"),
                ("Decision", "text"),
                ("Reason", "text"),
                ("At", "text"),
            ],
            [
                [
                    cell(f"<code>{fmt.text(str(item.get('key', '')))}</code>", label="Row"),
                    cell(fmt.text(str(item.get("action", ""))), label="Decision"),
                    cell(fmt.text(str(item.get("reason", ""))), label="Reason"),
                    cell(fmt.text(str(item.get("at", ""))), label="At"),
                ]
                for item in signals[:12]
            ],
            caption="Latest decision per row",
        )

    orders = [dict(item) for item in (snapshot.get("orders") or ())]
    if orders:
        body += table(
            [
                ("Row", "text"),
                ("Side", "text"),
                ("Filled at", "num"),
                ("At", "text"),
                ("Priced from", "text"),
            ],
            [
                [
                    cell(f"<code>{fmt.text(str(item.get('candidate_id', '')))}</code>", label="Row"),
                    cell(fmt.text(str(item.get("direction", ""))), label="Side"),
                    cell(fmt.text(str(item.get("price", ""))), label="Filled at", num=True),
                    cell(fmt.text(str(item.get("at", ""))), label="At"),
                    cell(fmt.text(str(item.get("priced_from", ""))), label="Priced from"),
                ]
                for item in orders[-10:]
            ],
            caption="Simulated entry fills from this session (the newest ten)",
        )

    body += _blockers([dict(item) for item in (paper.get("blockers") or ())])

    controls = action_form(
        "catalog-paper-start", nonce, "Start live-market paper test", tone="primary"
    )
    if built:
        controls += action_form("catalog-paper-stop", nonce, "Stop the paper test")
    if open_positions:
        controls += action_form("catalog-paper-flat", nonce, "Simulate closing open positions")
    body += f"<div class='actions' style='margin-top:12px'>{controls}</div>"
    if not built:
        body += (
            "<p class='small muted' style='margin:8px 0 0'>There is nothing to stop or close yet: "
            "Start builds the test from the stored evaluation and then follows it, so those two "
            "controls appear once the test exists.</p>"
        )
    claim = str(snapshot.get("claim") or paper.get("headline") or "")
    if claim:
        body += f"<p class='small muted' style='margin:8px 0 0'>{fmt.text(claim)}</p>"

    return section(
        "Paper test",
        body,
        note="The catalog test this page builds and follows. Its fills are simulated locally from "
        "the terminal's own quotes; no order is sent.",
        tools=chip(f"PAPER · {fmt.text(status)}", "paper" if status != "IDLE" else "muted"),
        id="paper-test",
    )


# ---------------------------------------------------------------------------
# 3. the whole-catalog audit
# ---------------------------------------------------------------------------


def _audit_rows(candidates: Sequence[Mapping[str, Any]]) -> list[list[str]]:
    rows = []
    for item in candidates:
        rows.append(
            [
                cell(
                    f"<code>{fmt.text(str(item.get('candidate_id', '')))}</code>",
                    label="Candidate",
                ),
                cell(
                    chip(
                        str(item.get("status", "")),
                        _VERDICT_TONES.get(str(item.get("status", "")), "muted"),
                    ),
                    label="Status",
                ),
                cell(
                    fmt.text(
                        ", ".join(str(part) for part in (item.get("reasons") or ())) or "—"
                    ),
                    label="Reasons",
                ),
                cell(
                    "{}/{}/{}".format(
                        item.get("tested_cells", 0),
                        item.get("skipped_cells", 0),
                        item.get("failed_cells", 0),
                    ),
                    label="Rows tested / skipped / failed",
                ),
            ]
        )
    return rows


def _audit_section(view: Mapping[str, Any], nonce: str) -> str:
    audit = dict(view.get("audit") or {})
    evaluation = dict(view.get("evaluation") or {})
    if not audit.get("present"):
        return section(
            "Full catalog audit",
            notice(
                "Account for every declared candidate, not just the ones that ran",
                "The audit reads the stored evaluation record and lists every declared candidate "
                "as tested, skipped, failed, or not considered -- with the reason. It runs "
                "nothing, tunes nothing and promotes nothing.",
                tone="ok" if evaluation.get("on_record") else "warn",
            )
            + "<div class='actions' style='margin-top:12px'>"
            + action_form("catalog-audit", nonce, "Audit the whole catalog")
            + "</div>",
            id="catalog-audit",
        )

    counts = stats(
        [
            stat("Declared", _num(audit.get("declared")), foot="every strategy in this build"),
            stat("Tested", _num(audit.get("tested")), foot="ran and produced a measurement"),
            stat("Skipped", _num(audit.get("skipped")), foot="reached, not run — reason shown"),
            stat("Failed", _num(audit.get("failed")), foot="ran and could not be measured"),
            stat(
                "Not considered",
                _num(audit.get("not_considered")),
                foot="never planned by the run",
            ),
        ]
    )
    body = counts
    candidates = [dict(item) for item in (audit.get("candidates") or ())]
    if candidates:
        body += table(
            [
                ("Candidate", "text"),
                ("Status", "text"),
                ("Reasons", "text"),
                ("Rows tested / skipped / failed", "text"),
            ],
            _audit_rows(candidates),
            caption="Every candidate, with its status and reasons",
        )
        total = int(audit.get("candidates_total") or len(candidates))
        if total > len(candidates):
            body += (
                f"<p class='small muted' style='margin:10px 0 0'>Showing the first "
                f"{len(candidates)} of {_num(total)} candidates; the full list is in the report "
                "file.</p>"
            )
    else:
        body += empty_state(
            "No candidate is listed",
            "That would be a fault in the report, not an empty catalog.",
        )
    if audit.get("tuning_note"):
        body += (
            f"<p class='small muted' style='margin:10px 0 0'>"
            f"{fmt.text(str(audit.get('tuning_note')))}</p>"
        )
    body += notice(
        "What this audit is",
        fmt.text(str(audit.get("claim", ""))),
        tone="ok" if audit.get("tested") else "warn",
    )
    body += "<div class='actions' style='margin-top:12px'>" + action_form(
        "catalog-audit", nonce, "Re-run the audit"
    ) + "</div>"
    return section(
        "Full catalog audit",
        body,
        note=f"Generated {fmt.text(str(audit.get('generated_at', '')))} from "
        f"{fmt.text(str(audit.get('source', '')))}. Reading it evaluates nothing and promotes "
        "nothing.",
        tools=chip("report", "muted"),
        id="catalog-audit",
    )


# ---------------------------------------------------------------------------
# the block the page composes
# ---------------------------------------------------------------------------


def catalog_sections(view: Mapping[str, Any], nonce: str) -> str:
    """The catalog block: counts, the symbol offer, the evaluation, the test, the audit."""

    if not view.get("available"):
        # The card is titled for what it does only when it can do it: a process
        # with no catalog panel draws no catalog card at all, just the reason.
        return section(
            "Catalog",
            notice(
                "The catalog panel is not wired into this workstation",
                str(
                    view.get("message")
                    or "This process has no catalog panel, so there is nothing to evaluate or "
                    "paper trade here. Nothing else is affected."
                ),
                tone="warn",
            ),
            note="The rest of this page reads the research panel, which is unaffected.",
            tools=chip("NOT WIRED", "muted"),
            id="catalog",
        )
    result = dict(view.get("result") or {})
    body = ""
    if result.get("message"):
        body += notice(
            "The last catalog action",
            fmt.text(str(result.get("message"))),
            tone="ok" if result.get("ok") else "warn",
            role="status" if result.get("ok") else "alert",
        )
        for row in result.get("rows") or ():
            if isinstance(row, Mapping) and row.get("usable") is False:
                body += notice(
                    f"{row.get('symbol', '')} {row.get('timeframe', '')}",
                    fmt.text(str(row.get("reason") or "no usable history")),
                    tone="warn",
                )
    return (
        body
        + _counts_section(view)
        + _symbols_section(view)
        + _evaluate_section(view, nonce)
        + _paper_section(view, nonce)
        + _audit_section(view, nonce)
        + notice(
            "What this test is, and is not",
            "Real terminal quotes, locally simulated fills, a ledger file on this machine. It does "
            "not send an order, does not arm live trading, does not change the execution mode, and "
            "makes no claim that any candidate will make money.",
            tone="ok",
        )
    )


def rebuild_blockers_section(view: Mapping[str, Any]) -> str:
    """What a *fresh* build from the stored record would hit, said once."""

    paper = dict(view.get("paper") or {})
    rebuild = [dict(item) for item in (paper.get("rebuild_blockers") or ())]
    if not rebuild:
        return ""
    return section(
        "What a rebuild from the stored record would need",
        _blockers(rebuild),
        note="The paper test above is already built and is unaffected. These are the conditions a "
        "fresh build from the stored evaluation would hit, which is why nothing is offered to "
        "rebuild it.",
        id="catalog-rebuild",
    )

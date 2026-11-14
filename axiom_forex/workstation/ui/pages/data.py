"""Data and evaluation: the folder, what is in it, and what can be done with it.

The page has one job and says it once: it reads a folder the operator named, it
describes every file rather than interpreting it, and it offers exactly the two
things a described file can be used for -- convert a broker tick export into a
canonical bid/ask file, or evaluate candidates on a canonical file you exported
yourself and declared.

Nothing here downloads, fills a gap, guesses a clock basis, promotes a strategy
on a sample, or reaches a broker.  Every control posts an action the application
already handles, with the process nonce.
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
    facts,
    jump_nav,
    layout,
    notice,
    page_head,
    section,
    table,
)
from . import common, runner

#: How a file kind is named.  The name is the description, never a guess.
_KIND_CHIPS: dict[str, tuple[str, str]] = {
    "MT5_TICK_EXPORT": ("broker tick export", "live"),
    "CANONICAL_CSV": ("canonical bid/ask", "paper"),
}


def _bytes(size: int) -> str:
    value = float(size or 0)
    for unit in ("B", "kB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def _kind(kind: str) -> str:
    label, tone = _KIND_CHIPS.get(str(kind), ("unrecognised", "muted"))
    return chip(label, tone)


def _slug(text: str, index: int) -> str:
    """A short id that is unique on this page, and stable for one file."""

    stem = "".join(ch for ch in str(text) if ch.isalnum())[:20] or "file"
    return f"{stem}-{index}"


def _text_list(values: Any) -> str:
    items = [str(item) for item in (values or ()) if str(item)]
    return fmt.text(", ".join(items)) if items else "—"


# ---------------------------------------------------------------------------
# the states the page can be in before it has any folder at all
# ---------------------------------------------------------------------------


def _unavailable(view: Mapping[str, Any]) -> str:
    reason = str(view.get("unavailable_reason", ""))
    return notice(
        "The data tools could not be loaded on this machine",
        "Nothing here is hidden or guessed — the importer, the converter and the dataset "
        "evaluation are simply not available in this Python. What the interpreter reported: "
        f"<code>{fmt.text(reason)}</code>. Install the project's dependencies into the "
        "environment that runs AXIOM (<code>.venv\\Scripts\\python.exe -m pip install -e .</code>), "
        "then restart the workstation.",
        tone="danger",
        role="alert",
    )


def _not_configured() -> str:
    return notice(
        "No import folder configured",
        "Start the workstation with <code>--imports-dir &lt;folder&gt;</code> to list and "
        "convert files here. Nothing is downloaded, and AXIOM never invents market data.",
        tone="warn",
    )


# ---------------------------------------------------------------------------
# the folder and its files
# ---------------------------------------------------------------------------


def _folder_section(view: Mapping[str, Any], nonce: str) -> str:
    listing = list(view.get("files") or ())
    scan_error = str(view.get("scan_error", "")) or "none"
    return section(
        "Import folder",
        facts(
            [
                ("Folder", f"<code>{fmt.text(str(view.get('import_dir', '')))}</code>"),
                ("Files listed", fmt.text(str(len(listing)))),
                ("Scan error", fmt.text(scan_error)),
            ]
        )
        + "<div class='actions' style='margin-top:12px'>"
        + action_form("imports-refresh", nonce, "Re-scan folder", tone="primary")
        + "</div>",
        note="Drop broker exports and canonical CSVs here, then re-scan. A re-scan only "
        "reads names, sizes and headers; it writes nothing.",
        tools=chip("LOCAL FILES ONLY", "ok", dot=True),
        id="folder",
    )



def _file_rows(files: Sequence[Mapping[str, Any]]) -> list[list[str]]:
    rows: list[list[str]] = []
    for entry in files:
        flags = []
        if entry.get("looks_like_sample"):
            flags.append(chip("sample — never evaluated as history", "warn"))
        if entry.get("errors"):
            flags.append(chip(f"{entry['errors']} error(s)", "danger"))
        if entry.get("warnings"):
            flags.append(chip(f"{entry['warnings']} warning(s)", "warn"))
        if entry.get("provenance"):
            flags.append(chip("has provenance sidecar", "paper"))
        detail = _bytes(int(entry.get("size_bytes") or 0))
        if entry.get("pairs"):
            detail += f" · {', '.join(str(item) for item in entry['pairs'])}"
        if entry.get("timeframes"):
            detail += f" · {', '.join(str(item) for item in entry['timeframes'])}"
        span = (
            f"<span class='mono'>{fmt.text(str(entry.get('first_at'))[:19])} → "
            f"{fmt.text(str(entry.get('last_at'))[:19])}</span>"
            if entry.get("first_at")
            else "—"
        )
        rows.append(
            [
                cell(
                    f"<code>{fmt.text(str(entry.get('name', '')))}</code>"
                    f"<span class='small muted' style='display:block'>{fmt.text(detail)}</span>",
                    label="File",
                ),
                cell(_kind(str(entry.get("kind", ""))), label="Kind"),
                cell(
                    f"{int(entry.get('records') or 0)} records "
                    f"({int(entry.get('bars') or 0)} bars, "
                    f"{int(entry.get('quotes') or 0)} quotes/ticks)",
                    label="Contents",
                    num=True,
                ),
                cell(span, label="Span (UTC)"),
                cell(" ".join(flags) or chip("no flags", "muted"), label="Flags"),
                cell(fmt.text(str(entry.get("note", "")) or "—"), label="Next step"),
            ]
        )
    return rows


def _files_section(view: Mapping[str, Any]) -> str:
    files = list(view.get("files") or ())
    return section(
        "Files in the import folder",
        table(
            [
                ("File", "text"),
                ("Kind", "text"),
                ("Contents", "text"),
                ("Span (UTC)", "text"),
                ("Flags", "text"),
                ("Next step", "text"),
            ],
            _file_rows(files),
            caption="Files, described rather than interpreted",
            empty=empty_state(
                "No files yet",
                "Put a MetaTrader 5 tick export or a canonical CSV in the folder above, "
                "then re-scan. Nothing is fetched for you.",
            ),
        ),
        note="A kind is recognised from the file's own header. A file this build cannot "
        "describe is listed as unrecognised, with the reason, and is never guessed at.",
        tools=chip(f"{len(files)} FILE(S)", "muted"),
        id="files",
        flush=True,
    )


# ---------------------------------------------------------------------------
# the two things a described file can be used for
# ---------------------------------------------------------------------------


def _convert_form(nonce: str, entry: Mapping[str, Any], index: int) -> str:
    anchor = _slug(str(entry.get("name", "")), index)
    name = fmt.attr(str(entry.get("name", "")))
    return (
        "<form method='post' action='/actions/ticks-convert' class='form'>"
        f"<input type='hidden' name='nonce' value='{fmt.attr(nonce)}'>"
        f"<input type='hidden' name='file' value='{name}'>"
        "<div class='form-row'>"
        f"<div class='field'><label for='pair-{anchor}'>Pair (canonical)</label>"
        f"<input type='text' id='pair-{anchor}' name='pair' placeholder='EUR/USD' "
        "autocomplete='off' spellcheck='false' required>"
        "<span class='hint'>The pair this file is, as the canonical "
        "<code>BASE/QUOTE</code> name. It is not read from the file name.</span></div>"
        f"<div class='field'><label for='offset-{anchor}'>Server UTC offset (minutes)</label>"
        f"<input type='number' id='offset-{anchor}' name='server_offset_minutes' "
        "placeholder='e.g. 120' step='1' min='-840' max='840'>"
        "<span class='hint'>The broker's clock, in minutes from UTC. No default.</span></div>"
        f"<div class='field'><label for='zone-{anchor}'>…or server zone (IANA)</label>"
        f"<input type='text' id='zone-{anchor}' name='server_timezone' "
        "placeholder='Europe/Athens' autocomplete='off' spellcheck='false'>"
        "<span class='hint'>Instead of the offset, if you know the zone by name.</span></div>"
        f"<div class='field'><label for='interval-{anchor}'>Bar interval</label>"
        f"<select id='interval-{anchor}' name='interval_seconds'>"
        "<option value='60'>1 minute</option><option value='300'>5 minutes</option>"
        "<option value='900'>15 minutes</option><option value='3600'>1 hour</option>"
        "</select></div>"
        "</div>"
        "<div class='actions'><button class='btn btn--primary' type='submit'>"
        "Convert to canonical CSV</button>"
        "<span class='small muted'>The server clock basis has no default. Empty windows are "
        "reported as gaps, never filled.</span></div>"
        "</form>"
    )


def _evaluate_form(
    nonce: str, entry: Mapping[str, Any], classes: Sequence[str], index: int
) -> str:
    anchor = _slug(str(entry.get("name", "")), index)
    name = fmt.attr(str(entry.get("name", "")))
    options = "".join(
        f"<option value='{fmt.attr(str(value))}'>"
        f"{fmt.text(str(value).replace('_', ' ').lower())}</option>"
        for value in classes
    )
    pairs = ", ".join(str(item) for item in (entry.get("pairs") or ()))
    return (
        "<form method='post' action='/actions/dataset-evaluate' class='form'>"
        f"<input type='hidden' name='nonce' value='{fmt.attr(nonce)}'>"
        f"<input type='hidden' name='file' value='{name}'>"
        "<div class='form-row'>"
        f"<div class='field'><label for='pair-{anchor}'>Pair (canonical)</label>"
        f"<input type='text' id='pair-{anchor}' name='pair' value='{fmt.attr(pairs)}' "
        "autocomplete='off' spellcheck='false' required>"
        "<span class='hint'>Taken from the file's own header, and editable if the file "
        "covers more than one pair.</span></div>"
        f"<div class='field'><label for='class-{anchor}'>Declared data class</label>"
        f"<select id='class-{anchor}' name='data_class'>{options}</select>"
        "<span class='hint'>Only a class you supplied yourself can promote a strategy.</span>"
        "</div></div>"
        f"<label class='field field--check' for='declare-{anchor}'>"
        f"<input type='checkbox' id='declare-{anchor}' name='declare' value='yes' required>"
        "<span class='label'>I exported this file myself and confirm what it is. AXIOM will not "
        "guess, and a sample or demo file can never promote a strategy.</span></label>"
        "<div class='actions'><button class='btn btn--primary' type='submit'>"
        "Evaluate candidates</button>"
        "<span class='small muted'>Chronological split, walk-forward folds, and one locked "
        "test window per dataset. Evidence is recorded; nothing is traded.</span></div>"
        "</form>"
    )


def _file_job(index: int, entry: Mapping[str, Any], form: str, *, badge: str) -> str:
    return (
        "<div class='job'>"
        "<div class='job-head'>"
        f"<h3><code>{fmt.text(str(entry.get('name', '')))}</code></h3>"
        f"{badge}</div>"
        f"{form}</div>"
    )


def _jobs(view: Mapping[str, Any], nonce: str) -> str:
    tick_files = list(view.get("tick_files") or ())
    dataset_files = list(view.get("dataset_files") or ())
    classes = list(view.get("data_classes") or ())
    blocks: list[str] = []

    if tick_files:
        forms = "".join(
            _file_job(
                index,
                entry,
                _convert_form(nonce, entry, index),
                badge=chip(f"{int(entry.get('records') or 0)} ticks", "live"),
            )
            for index, entry in enumerate(tick_files)
        )
        blocks.append(
            section(
                "1 · Convert a broker tick export",
                forms,
                note="A broker tick export becomes a canonical bid/ask file. The clock basis "
                "is what you declare, not what AXIOM infers.",
                id="convert",
            )
        )
    if dataset_files:
        forms = "".join(
            _file_job(
                index,
                entry,
                _evaluate_form(nonce, entry, classes, index),
                badge=chip(f"{int(entry.get('bars') or 0)} bars", "paper"),
            )
            for index, entry in enumerate(dataset_files)
        )
        blocks.append(
            section(
                "2 · Evaluate candidates on this dataset",
                forms,
                note="Evaluation records evidence for every candidate and spends the dataset's "
                "locked test window once. No result here is a forecast.",
                id="evaluate",
            )
        )
    if not blocks:
        blocks.append(
            section(
                "Convert and evaluate",
                empty_state(
                    "Nothing to convert or evaluate",
                    "A tick export must be a broker-exported bid/ask file, and an evaluable "
                    "dataset must be a canonical CSV you exported. A candle export cannot be "
                    "turned into bid/ask data here.",
                ),
                note="The page describes what is present. It never substitutes a file.",
                id="jobs",
            )
        )
    return "".join(blocks)


# ---------------------------------------------------------------------------
# the report of the last action
# ---------------------------------------------------------------------------


def _convert_report(last: Mapping[str, Any]) -> str:
    coverage = dict(last.get("coverage") or {})
    gaps = list(coverage.get("gap_windows") or ())
    gap_rows = [
        [
            cell(fmt.text(str(item.get("window", ""))[:19]), label="Window start (UTC)"),
            cell(fmt.text(str(item.get("reason", ""))), label="Reason"),
        ]
        for item in gaps[:50]
    ]
    report = facts(
        [
            ("Source", f"<code>{fmt.text(str(last.get('source_file', '')))}</code>"),
            ("Written", f"<code>{fmt.text(str(last.get('output_file', '')))}</code>"),
            ("Ticks read", fmt.text(str(last.get("tick_count", 0)))),
            ("Bars written", fmt.text(str(last.get("bars_written", 0)))),
            ("Duplicate ticks", fmt.text(str(last.get("duplicates", 0)))),
            ("Rejected rows", fmt.text(str(len(last.get("rejected_rows") or ())))),
            ("Server time basis", fmt.text(str(last.get("server_time_basis", "")))),
            ("Time basis detail", fmt.text(str(last.get("time_basis_detail", ""))) or "—"),
            ("Empty windows", fmt.text(str(coverage.get("empty_windows", 0)))),
            ("Largest gap (s)", fmt.text(str(coverage.get("largest_gap_seconds", "0")))),
            (
                "Trailing partial window",
                fmt.text(str(coverage.get("trailing_partial_window", ""))) or "none",
            ),
            (
                "Source SHA-256",
                f"<code>{fmt.text(str(last.get('source_sha256', ''))[:32])}…</code>",
            ),
            ("Provenance", f"<code>{fmt.text(str(last.get('provenance_path', '')))}</code>"),
        ]
    )
    if gap_rows:
        report += table(
            [
                ("Window start (UTC)", "text"),
                ("Reason", "text"),
            ],
            gap_rows,
            caption="Empty windows, reported not filled",
        )
    else:
        report += (
            "<p class='small muted' style='margin-top:10px'>Every window in this range had "
            "ticks; no gaps were found.</p>"
        )
    return section(
        "Conversion report",
        report,
        note="Only complete windows became bars. A gap is named, never interpolated.",
        tools=chip("CONVERTED", "ok", dot=True),
        id="report",
    )


def _evaluate_report(last: Mapping[str, Any]) -> str:
    results = list(last.get("results") or ())
    rows = []
    for item in results:
        aggregate = dict(item.get("aggregate") or {})
        rows.append(
            [
                cell(
                    f"<code>{fmt.text(str(item.get('candidate_id', '')))}</code>",
                    label="Candidate",
                ),
                cell(
                    chip(
                        str(item.get("status", "")),
                        "ok" if item.get("status") == "SELECTED_FOR_PAPER" else "warn",
                    ),
                    label="Status",
                ),
                cell(
                    fmt.text(
                        ", ".join(str(part) for part in (item.get("failed_criteria") or ()))
                        or str(item.get("ineligible_reason", ""))
                        or "—"
                    ),
                    label="Why not selected",
                ),
                cell(
                    fmt.text(str(aggregate.get("return_fraction", "—"))),
                    label="Return (gross of nothing)",
                    num=True,
                ),
                cell(
                    fmt.text(str(aggregate.get("max_drawdown_fraction", "—"))),
                    label="Max drawdown",
                    num=True,
                ),
                cell(
                    fmt.text(str(aggregate.get("closed_trades", "—"))),
                    label="Closed trades",
                    num=True,
                ),
            ]
        )
    meta = dict(last.get("meta") or {})
    spent = bool(last.get("locked_test_spent"))
    return section(
        "Evaluation report",
        facts(
            [
                ("Dataset", f"<code>{fmt.text(str(last.get('dataset_id', '')))}</code>"),
                ("Pair", fmt.text(str(meta.get("pair", "")))),
                ("Bars", fmt.text(str(last.get("bars", 0)))),
                ("Declared data class", fmt.text(str(last.get("data_class", "")))),
                (
                    "Locked test window",
                    chip("spent on this dataset" if spent else "not spent", "warn" if spent else "muted"),
                ),
                (
                    "Candidates selected",
                    fmt.text(", ".join(str(item) for item in (last.get("selected") or ())) or "none"),
                ),
            ]
        )
        + table(
            [
                ("Candidate", "text"),
                ("Status", "text"),
                ("Why not selected", "text"),
                ("Return (gross of nothing)", "num"),
                ("Max drawdown", "num"),
                ("Closed trades", "num"),
            ],
            rows,
            caption="Candidate evidence on the sample you supplied",
        )
        + (
            "<p class='small muted' style='margin-top:10px'>"
            + fmt.text(str(last.get("note", "")))
            + " Assumptions: "
            + fmt.text("; ".join(str(item) for item in (last.get("assumptions") or ())))
            + "</p>"
        ),
        note="Evidence, not a forecast. Paper results follow only from the simulated lane.",
        tools=chip("EVALUATED", "ok", dot=True),
        id="report",
    )


def _last_section(last: Mapping[str, Any]) -> str:
    if not last:
        return ""
    if last.get("ok"):
        if last.get("action") == "convert":
            return _convert_report(last)
        if last.get("action") == "evaluate":
            return _evaluate_report(last)
        return ""
    return section(
        "Last action",
        notice(
            str(last.get("reason", "refused")),
            fmt.text(str(last.get("detail", ""))) or "Nothing was written.",
            tone="warn",
            role="alert",
        ),
        note="The folder is unchanged and nothing was evaluated.",
        tools=chip("REFUSED", "warn", dot=True),
        id="report",
    )


# ---------------------------------------------------------------------------
# what the research database already holds
# ---------------------------------------------------------------------------


def _evidence_section(view: Mapping[str, Any]) -> str:
    evidence = dict(view.get("evidence") or {})
    rows = []
    for row in list(evidence.get("rows") or ()):
        rows.append(
            [
                cell(
                    f"<code>{fmt.text(str(row.get('candidate_id', '')))}</code>",
                    label="Candidate",
                ),
                cell(
                    chip(
                        str(row.get("status", "")),
                        "ok" if row.get("selected") else "muted",
                    ),
                    label="Status",
                ),
                cell(fmt.text(str(row.get("dataset_id", "")) or "—"), label="Dataset"),
                cell(fmt.text(str(row.get("data_class", "")) or "—"), label="Data class"),
                cell(
                    fmt.text(str(row.get("evaluated_at", ""))[:19] or "—"),
                    label="Evaluated (UTC)",
                ),
                cell(
                    fmt.text(
                        ", ".join(str(part) for part in (row.get("failed_criteria") or ()))
                        or str(row.get("detail", ""))
                        or "—"
                    ),
                    label="Blocking criteria",
                ),
            ]
        )
    available = bool(evidence.get("available", True))
    return section(
        "Recorded evidence",
        table(
            [
                ("Candidate", "text"),
                ("Status", "text"),
                ("Dataset", "text"),
                ("Data class", "text"),
                ("Evaluated (UTC)", "text"),
                ("Blocking criteria", "text"),
            ],
            rows,
            caption="What the research database holds for each candidate",
            empty=empty_state(
                "No evidence recorded",
                "No candidate has been evaluated on an imported dataset yet."
                if available
                else "Evidence store unavailable "
                f"({fmt.text(str(evidence.get('reason', '')))}).",
            ),
        ),
        note="The paper runner reads exactly this; it will not start without a selected candidate.",
        tools=chip(f"{len(rows)} RECORD(S)", "muted"),
        id="evidence",
    )


# ---------------------------------------------------------------------------
# the page
# ---------------------------------------------------------------------------


def render_data(ctx: Any) -> str:
    view = dict((ctx.extra or {}).get("data") or {})
    nonce = str(getattr(ctx, "action_nonce", ""))

    head = page_head(
        "Data &amp; evaluation",
        "The files you supplied, and what they can be used for",
        "This page reads one folder on this machine. It connects to nothing, places no order, "
        "arms nothing, and never changes the execution mode.",
        badges=common.scope_badges(ctx)
        + (
            chip("LOCAL FILES ONLY", "ok", dot=True)
            if view.get("configured")
            else chip("NO FOLDER", "warn", dot=True)
        )
        + chip(
            "evidence recorded" if view.get("evaluation_available") else "no evaluation platform",
            "paper" if view.get("evaluation_available") else "warn",
        ),
    )

    if view.get("unavailable_reason"):
        # The tooling itself could not be imported; the rest of the workstation is
        # unaffected, so the page keeps its frame and reports the interpreter.
        return head + layout(
            _unavailable(view) + runner.sections(ctx) if ctx.runner else _unavailable(view),
            wide=True,
        )
    if not view.get("configured"):
        return head + layout(
            _not_configured() + runner.sections(ctx) if ctx.runner else _not_configured(),
            wide=True,
        )

    jobs: list[tuple[str, str]] = []
    if list(view.get("tick_files") or ()):
        jobs.append(("convert", "Convert"))
    if list(view.get("dataset_files") or ()):
        jobs.append(("evaluate", "Evaluate"))

    main = (
        jump_nav(
            [("folder", "Folder"), ("files", "Files")]
            + jobs
            + ([("report", "Last result")] if view.get("last") else [])
            + [("evidence", "Evidence")]
        )
        + _folder_section(view, nonce)
        + _files_section(view)
        + _jobs(view, nonce)
        + _last_section(dict(view.get("last") or {}))
        + _evidence_section(view)
    )
    return head + layout(main, wide=True)

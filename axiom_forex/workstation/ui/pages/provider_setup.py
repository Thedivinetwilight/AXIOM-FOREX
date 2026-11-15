"""Broker terms: the one local document the paper runner reads before it starts.

The page is a form and an account of that form.  In order:

* the account facts the terminal proves, which AXIOM fills in and no one types;
* the current ``provider_setup.local.json``: whether it exists, whether the
  runner's own loader accepts it, and what it says if it does;
* every term the document needs, grouped the way the document is grouped, each
  one marked as either *from terminal* or *you supply*, with its help text and
  its placeholder wired to the control;
* the list-shaped sections (symbols, swap, sessions, rates), one line per entry;
* the save, behind one explicit replace tick;
* what is still missing before the runner could trade paper, and the shipped
  example document.

Nothing here connects to a broker, reads or writes ``mt5_settings.local.json``,
stores a password or token, or sends an order.  It writes one file, and only
when every required term validates against the same loader the runner uses.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from .. import format as fmt
from ..components import (
    blockers,
    button_link,
    chip,
    empty_state,
    facts,
    field,
    jump_nav,
    jump_nav_bar,
    layout,
    notice,
    page_head,
    section,
)
from . import common

#: The document's own grouping, in the order it is read.
_GROUPS: tuple[tuple[str, str, str], ...] = (
    (
        "account",
        "Account",
        "A label, the starting balance, your declared conversion source, and the entry cap.",
    ),
    ("costs", "Costs", "Commission, slippage, and financing, from your broker's schedule."),
    (
        "rollover",
        "Rollover and swap",
        "The broker's daily cutoff, triple-swap weekday, and swap schedule.",
    ),
    ("risk", "Risk limits", "Every limit the pre-trade gate enforces. None of them has a default."),
    (
        "calendar",
        "Trading calendar",
        "The session windows the market is actually open, with validity bounds.",
    ),
    ("execution", "Execution", "Lot size, position count, and whether shorts are allowed."),
)

#: The list-shaped sections, with the line format each one takes.
_LISTS: tuple[tuple[str, str, str], ...] = (
    (
        "symbols",
        "Symbols",
        "One line per pair. Blank broker symbol and pip size are filled from your terminal.",
    ),
    ("swap", "Swap", "Long and short swap per lot per night, as your broker quotes them."),
    (
        "calendar.windows",
        "Trading windows",
        "One line per session window, inside the validity bounds above.",
    ),
    ("calendar.holidays", "Holidays", "Optional: one line per closed day."),
    (
        "conversion_rates",
        "Conversion rates",
        "Required when the account currency is not the pair's quote currency.",
    ),
)

_TERMINAL = "TERMINAL"


def _ident(path: str) -> str:
    """A stable id for one term: ``account.entry_exposure_limit.amount`` reads as
    ``f-account-entry-exposure-limit-amount``."""

    return "f-" + path.replace(".", "-").replace("_", "-")


def _anchor(name: str) -> str:
    return "t-" + name.replace(".", "-").replace("_", "-")


def _problem_list(problems: Sequence[Mapping[str, Any]]) -> str:
    """The loader's own refusals, with the field they name and what to do."""

    return blockers(
        [
            {
                "title": fmt.plain(item.get("message") or "This term is not accepted")
                or "This term is not accepted",
                "detail": (
                    f"Fix {fmt.plain(item.get('path') or 'this field')} and save again."
                    if item.get("kind") == "missing"
                    else ""
                ),
                "code": fmt.plain(item.get("path") or ""),
                "tone": "danger",
            }
            for item in problems
        ]
    )


def _open_blockers(items: Sequence[Mapping[str, Any]]) -> str:
    """What still stands between the runner and its first simulated fill."""

    return blockers(
        [
            {
                "title": fmt.humanize_code(item.get("reason") or "") or "Blocked",
                "detail": " ".join(
                    part
                    for part in (
                        fmt.plain(item.get("message") or ""),
                        fmt.plain(item.get("hint") or ""),
                    )
                    if part
                ),
                "code": fmt.plain(item.get("reason") or ""),
                "tone": "warn",
            }
            for item in items
        ]
    )


# ---------------------------------------------------------------------------
# what the terminal proves
# ---------------------------------------------------------------------------


def _facts_section(view: Mapping[str, Any]) -> str:
    payload = dict(view.get("facts") or {})
    if not payload.get("connected"):
        return section(
            "Account facts from your terminal",
            empty_state(
                "Not connected",
                "Connect on the Setup page first. AXIOM will not guess your account currency, your "
                "leverage, or a symbol's tick size. You can still read the term list below.",
                actions=button_link("/setup", "Open setup", small=True),
            ),
            note="These are read from the terminal; they are the only fields AXIOM fills in for "
            "you.",
            tools=chip("not connected", "muted", dot=True),
            id="terminal-facts",
        )
    carried = len(payload.get("symbols") or ())
    total = int(payload.get("symbols_total") or carried)
    watch = fmt.text(str(total))
    if total > carried:
        watch += f" ({carried} names carried here)"
    return section(
        "Account facts from your terminal",
        facts(
            [
                ("Currency", fmt.text(str(payload.get("currency", ""))) or fmt.EMPTY),
                ("Leverage", fmt.text(str(payload.get("leverage", ""))) or fmt.EMPTY),
                ("Server", fmt.text(str(payload.get("server", ""))) or fmt.EMPTY),
                ("Margin mode", fmt.text(str(payload.get("margin_mode", ""))) or fmt.EMPTY),
                ("Symbols in Market Watch", watch),
            ]
        ),
        note="These are read from the terminal; they are the only fields AXIOM fills in for you.",
        tools=chip("terminal connected", "live", dot=True),
        id="terminal-facts",
    )


def _file_section(view: Mapping[str, Any]) -> str:
    status = dict(view.get("file") or {})
    path = fmt.text(str(status.get("path", ""))) or fmt.text(
        str(view.get("setup_filename", "provider_setup.local.json"))
    )
    if not status.get("exists"):
        return section(
            "Current setup file",
            empty_state(
                "No setup file yet",
                f"{fmt.text(str(view.get('setup_filename', 'provider_setup.local.json')))} does "
                "not exist in the project root. Fill in the terms below and save it.",
            ),
            note="Located in the project root, next to the launcher. Ignored by Git.",
            tools=chip("not created", "muted", dot=True),
            id="current-file",
        )
    if not status.get("valid"):
        return section(
            "Current setup file",
            facts([("File", f"<code>{path}</code>")])
            + _problem_list([dict(item) for item in (status.get("problems") or ())]),
            note="The runner's own loader refused this document; nothing here will use it until "
            "the term it names is fixed.",
            tools=chip("needs attention", "warn", dot=True),
            id="current-file",
        )
    summary = dict(status.get("summary") or {})
    return section(
        "Current setup file",
        facts(
            [
                ("File", f"<code>{path}</code>"),
                ("Pairs", fmt.text(", ".join(summary.get("pairs") or ()) ) or fmt.EMPTY),
                (
                    "Account currency",
                    fmt.text(str(summary.get("account_currency", ""))) or fmt.EMPTY,
                ),
                ("Leverage", fmt.text(str(summary.get("leverage", ""))) or fmt.EMPTY),
                ("Cost model", fmt.text(str(summary.get("cost_model", ""))) or fmt.EMPTY),
                ("Calendar", fmt.text(str(summary.get("calendar_id", ""))) or fmt.EMPTY),
                ("Quantity", fmt.text(str(summary.get("quantity", ""))) or fmt.EMPTY),
            ]
        ),
        note="It loads cleanly with the runner's own loader. It is Git-ignored.",
        tools=chip("valid", "ok", dot=True),
        id="current-file",
    )


# ---------------------------------------------------------------------------
# the term list
# ---------------------------------------------------------------------------


def _control(item: Mapping[str, Any], payload: Mapping[str, Any]) -> str:
    """One term: its label, its provenance, its help, and a control that works."""

    path = str(item.get("path", ""))
    kind = str(item.get("kind", ""))
    ident = _ident(path)
    hint_id = f"{ident}-hint"
    label = fmt.text(str(item.get("label", ""))) + (
        fmt.text(" (optional)") if item.get("optional") else ""
    )
    described = f" aria-describedby='{fmt.attr(hint_id)}'"
    if kind == _TERMINAL:
        # The terminal is the authority for these two; they carry no name, so the
        # form cannot post a value the operator typed over.
        value = {
            "account.currency": str(payload.get("currency", "")),
            "account.leverage": str(payload.get("leverage", "")),
        }.get(path, "")
        control = (
            f"<input id='{fmt.attr(ident)}' type='text' value='{fmt.attr(value)}' readonly "
            f"aria-readonly='true'{described}>"
        )
        provenance = chip("from terminal", "live")
    else:
        control = (
            f"<input id='{fmt.attr(ident)}' type='text' name='{fmt.attr(path)}' value=''"
            f" placeholder='{fmt.attr(str(item.get('placeholder', '')))}' autocomplete='off'"
            f" spellcheck='false'{described}>"
        )
        provenance = chip("you supply", "warn")
    return (
        f"<div class='field'><label for='{fmt.attr(ident)}'>{label} {provenance}</label>"
        f"{control}"
        f"<span class='hint' id='{fmt.attr(hint_id)}'>{fmt.text(str(item.get('help', '')))}</span>"
        "</div>"
    )


def _group_sections(view: Mapping[str, Any]) -> list[tuple[str, str, str]]:
    """(id, title, section html) for every group that has terms in this build."""

    payload = dict(view.get("facts") or {})
    fields = [dict(item) for item in (view.get("fields") or ())]
    out: list[tuple[str, str, str]] = []
    for key, title, hint in _GROUPS:
        members = [item for item in fields if str(item.get("path", "")).startswith(key + ".")]
        if not members:
            continue
        out.append(
            (
                key,
                title,
                section(
                    title,
                    "<div class='form-row'>"
                    + "".join(_control(item, payload) for item in members)
                    + "</div>",
                    note=hint,
                    id=key,
                ),
            )
        )
    return out


def _lists_section(view: Mapping[str, Any]) -> str:
    formats = dict(view.get("list_formats") or {})
    areas = []
    for name, title, hint in _LISTS:
        ident = _anchor(name)
        hint_id = f"{ident}-hint"
        note = fmt.text(str(hint))
        if formats.get(name):
            note += f" Format: {fmt.text(str(formats[name]))}"
        areas.append(
            "<div style='grid-column:1/-1'>"
            + field(
                name,
                title,
                kind="textarea",
                rows=3,
                hint=note,
                field_id=ident,
            )
            + "</div>"
        )
    return section(
        "Symbols, swap, sessions, and rates",
        "<div class='form-row'>" + "".join(areas) + "</div>",
        note="Blank broker symbol or pip size is filled from the terminal, because the terminal is "
        "the authority for those two. Everything else you type is stored exactly as typed.",
        id="lists",
    )


def _save_section() -> str:
    return section(
        "Save",
        "<label class='field field--check' for='provider-overwrite'>"
        "<input id='provider-overwrite' type='checkbox' name='overwrite' value='yes'>"
        "<span class='label'>Replace an existing provider_setup.local.json</span></label>"
        "<div class='actions' style='margin-top:12px'>"
        "<button class='btn btn--primary' type='submit'>Validate and save the setup file</button>"
        "<span class='small muted'>Validation uses the same loader the runner uses. Nothing is "
        "written until every required term validates.</span></div>",
        note="Nothing is written until every required term validates.",
        id="save",
    )


# ---------------------------------------------------------------------------
# the page
# ---------------------------------------------------------------------------


def _targets(groups: Sequence[tuple[str, str, str]]) -> list[tuple[str, str]]:
    targets: list[tuple[str, str]] = [
        ("terminal-facts", "Account facts"),
        ("current-file", "Current setup file"),
    ]
    targets += [(key, title) for key, title, _html in groups]
    targets += [
        ("lists", "Symbols, swap, sessions, rates"),
        ("save", "Save"),
        ("runner-blockers", "Still missing"),
        ("example", "Example document"),
    ]
    return targets


def render_provider_setup(ctx: Any) -> str:
    view = dict((ctx.extra or {}).get("provider_setup") or {})
    payload = dict(view.get("facts") or {})
    connected = bool(payload.get("connected"))
    last = dict(view.get("last") or {})
    nonce = str(getattr(ctx, "action_nonce", ""))
    status = dict(view.get("file") or {})

    head = page_head(
        "Broker terms",
        "The document the paper runner reads before it may start",
        "Build provider_setup.local.json from terms you type plus the account facts your terminal "
        "reports. One local, Git-ignored file — and nothing else.",
        badges=common.scope_badges(ctx)
        + (
            chip("terminal connected", "live", dot=True)
            if connected
            else chip("terminal not connected", "muted", dot=True)
        )
        + (
            chip("setup file valid", "ok", dot=True)
            if status.get("valid")
            else (
                chip("setup file needs attention", "warn", dot=True)
                if status.get("exists")
                else chip("no setup file", "muted", dot=True)
            )
        ),
        actions=button_link("/setup", "Open setup", small=True)
        + button_link("/positions", "Paper runner", small=True),
    )

    result = ""
    if last:
        if last.get("ok"):
            result = notice(
                "Setup file saved",
                "It is local, Git-ignored, and loads with the runner's own loader.",
                tone="ok",
                role="status",
            )
        else:
            result = section(
                "Nothing was written",
                _problem_list([dict(item) for item in (last.get("problems") or ())])
                or "<p class='small muted'>No detail was recorded.</p>",
                note="The existing file, if any, was left exactly as it was.",
                tools=chip("refused", "warn", dot=True),
                id="last-attempt",
            )

    groups = _group_sections(view)
    form = (
        "<form method='post' action='/actions/provider-setup-save' class='form'>"
        f"<input type='hidden' name='nonce' value='{fmt.attr(nonce)}'>"
        + "".join(html for _key, _title, html in groups)
        + _lists_section(view)
        + _save_section()
        + "</form>"
    )

    open_blockers = [dict(item) for item in (view.get("blockers") or ())]
    blockers_section = section(
        "What is still missing before the runner can trade paper",
        _open_blockers(open_blockers)
        or "<p class='small muted'>Nothing: the setup file exists, a dataset is imported, and a "
        "candidate has been selected. Start the runner on Positions.</p>",
        note="The same list appears on Positions, so the two pages cannot disagree.",
        tools=chip(f"{len(open_blockers)} open", "warn" if open_blockers else "ok"),
        id="runner-blockers",
    )

    example = section(
        "Example document",
        f"<p class='small'><code>{fmt.text(str(view.get('example_path', '')))}</code> shows every "
        "field of the real <code>axiom-provider-setup-v1</code> schema. It is an example only: all "
        "of its values are placeholders and it will not run until you replace them.</p>"
        "<p class='small muted' style='margin:8px 0 0'>It is not "
        "<code>profiles/provider_onboarding_template.json</code>, which is a different schema for a "
        "different purpose.</p>",
        note="Read it for the shape; do not copy it into place.",
        id="example",
    )

    body = (
        notice(
            "Writes one local file, and nothing else",
            "This page builds provider_setup.local.json from terms you type plus the account facts "
            "your terminal reports. It never reads or changes mt5_settings.local.json, never stores "
            "a password or token, and never sends an order.",
            tone="ok" if connected else "warn",
        )
        + result
        + _facts_section(view)
        + _file_section(view)
        + jump_nav_bar(_targets(groups))
        + jump_nav(_targets(groups))
        + form
        + blockers_section
        + example
        + common.safety_section(id="safety")
    )
    return head + layout(body, wide=True)

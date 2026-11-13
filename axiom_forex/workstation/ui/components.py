"""The primitives every page is composed from.

Each one emits semantic HTML with the classes :mod:`..ui.theme` styles, and each
one has a single job so a page reads as a list of facts rather than a wall of
markup.  Two rules hold throughout:

* every dynamic string is escaped here, not at the call site;
* nothing is emitted that claims a state the caller did not pass in — a missing
  value becomes an explicit "Unavailable" sentence, never a zero or a dash alone.
"""

from __future__ import annotations

from html import escape
from typing import Any, Iterable, Mapping, Sequence

from .format import attr, plain, text


# --------------------------------------------------------------------- chips
def chip(label: Any, tone: str = "muted", *, dot: bool = False) -> str:
    marker = "<span class='dot' aria-hidden='true'></span>" if dot else ""
    return f"<span class='chip chip--{attr(tone)}'>{marker}{escape(str(label))}</span>"


def badge(label: Any, tone: str = "muted") -> str:
    return chip(label, tone)


def chip_row(chips: Iterable[str]) -> str:
    body = "".join(item for item in chips if item)
    return f"<div class='chip-row'>{body}</div>" if body else ""


# ------------------------------------------------------------------- notices
_GLYPHS = {"ok": "✓", "warn": "!", "danger": "×", "info": "i", "muted": "·"}


def notice(
    title: str,
    body: str = "",
    *,
    tone: str = "info",
    glyph: str = "",
    role: str = "",
    id: str = "",
    attrs: str = "",
) -> str:
    """One qualified statement.  ``body`` may contain markup the caller built."""

    mark = glyph or _GLYPHS.get(tone, "·")
    ident = f" id='{attr(id)}'" if id else ""
    live = f" role='{attr(role)}'" if role else ""
    heading = f"<strong class='n-title'>{escape(title)}</strong>" if title else ""
    inner = f"<span class='n-body'>{body}</span>" if body else ""
    return (
        f"<div class='notice notice--{attr(tone)}'{ident}{live}{attrs}>"
        f"<span class='glyph' aria-hidden='true'>{escape(mark)}</span>"
        f"<div>{heading}{inner}</div></div>"
    )


def notice_stack(items: Iterable[str]) -> str:
    body = "".join(item for item in items if item)
    return f"<div class='notice-stack'>{body}</div>" if body else ""


# -------------------------------------------------------------------- layout
def section(
    title: str,
    body: str,
    *,
    note: str = "",
    tools: str = "",
    id: str = "",
    plain: bool = False,
    flush: bool = False,
    footer: str = "",
    kicker: str = "",
) -> str:
    """A titled region.  The heading is what the page's outline and jumps use."""

    ident = f" id='{attr(id)}'" if id else ""
    labelled = f" aria-labelledby='{attr(id)}-h'" if id else ""
    title_id = f" id='{attr(id)}-h'" if id else ""
    kick = f"<span class='kicker'>{escape(kicker)}</span>" if kicker else ""
    note_html = f"<p class='section-note'>{note}</p>" if note else ""
    heading = (
        f"<div class='section-title'>{kick}"
        f"<div class='h'><h2{title_id}>{escape(title)}</h2></div>"
        f"{note_html}</div>"
    )
    tools_html = f"<div class='section-tools'>{tools}</div>" if tools else ""
    head = (
        f"<div class='section-head'>{heading}{tools_html}</div>"
        if (title or tools)
        else ""
    )
    body_class = "section-body"
    if flush:
        body_class += " section-body--flush"
    elif plain:
        body_class += " section-body--tight"
    foot = f"<div class='section-foot'>{footer}</div>" if footer else ""
    klass = "section section--plain" if plain else "section"
    return (
        f"<section class='{klass}'{ident}{labelled}>{head}"
        f"<div class='{body_class}'>{body}</div>{foot}</section>"
    )


def page_head(
    kicker: str,
    title: str,
    lede: str = "",
    *,
    badges: str = "",
    actions: str = "",
) -> str:
    """The one ``h1`` on the page, with its scope, purpose and primary actions."""

    chips = f"<div class='chip-row'>{badges}</div>" if badges else ""
    actions_html = f"<div class='page-actions'>{actions}</div>" if actions else ""
    lede_html = f"<p class='lede'>{lede}</p>" if lede else ""
    return (
        "<div class='page-head'>"
        "<div class='page-head-row'>"
        "<div class='page-head-main'>"
        f"<p class='kicker'>{escape(kicker)}</p>"
        f"<h1>{escape(title)}</h1>"
        f"{lede_html}{chips}"
        "</div>"
        f"{actions_html}"
        "</div></div>"
    )


def layout(*columns: str, wide: bool = False) -> str:
    if len(columns) == 1:
        klass = "layout layout--wide" if wide else "layout layout--single"
        return f"<div class='{klass}'><div class='col'>{columns[0]}</div></div>"
    main, aside = columns[0], columns[1]
    return (
        f"<div class='layout'><div class='col'>{main}</div>"
        f"<div class='col col--aside'>{aside}</div></div>"
    )


# --------------------------------------------------------------------- facts
def facts(
    pairs: Sequence[tuple[str, str]],
    *,
    loose: bool = False,
) -> str:
    """A definition list: ``(label, value-html)``.

    This replaces the old tile grid on purpose — the label and the value it
    qualifies stay on one line, and a long "why it is unavailable" note wraps
    underneath its own figure instead of inside a box of its own.
    """

    rows: list[str] = []
    for label, value in pairs:
        rows.append(f"<dt>{escape(label)}</dt><dd>{value}</dd>")
    klass = "facts facts--loose" if loose else "facts"
    return f"<dl class='{klass}'>{''.join(rows)}</dl>"


def fact_line(pairs: Sequence[tuple[str, str]]) -> str:
    """The same pairs on one wrapping line, for the page's shared status strip."""

    body = "".join(
        f"<span class='pair'><span class='k'>{escape(label)}</span>"
        f"<span class='v'>{value}</span></span>"
        for label, value in pairs
    )
    return f"<div class='facts facts--inline'>{body}</div>"


def stat(
    label: str,
    value: str,
    *,
    foot: str = "",
    tone: str = "",
    small: bool = False,
    label_chip: str = "",
) -> str:
    klass = "stat" + (f" stat--{tone}" if tone else "")
    value_klass = "v v--sm" if small else "v"
    foot_html = f"<span class='f'>{foot}</span>" if foot else ""
    return (
        f"<div class='{klass}'><span class='k'>{label_chip}{escape(label)}</span>"
        f"<span class='{value_klass}'>{value}</span>{foot_html}</div>"
    )


def stats(items: Iterable[str]) -> str:
    body = "".join(item for item in items if item)
    return f"<div class='stats'>{body}</div>" if body else ""


# -------------------------------------------------------------------- tables
def cell(
    value: str,
    *,
    label: str = "",
    num: bool = False,
    mono: bool = False,
) -> str:
    klass = " ".join(part for part in ("num" if num else "", "mono" if mono else "") if part)
    klass_attr = f" class='{klass}'" if klass else ""
    label_attr = f" data-label='{attr(label)}'" if label else ""
    return f"<td{klass_attr}{label_attr}>{value}</td>"


def table(
    columns: Sequence[tuple[str, bool]],
    rows: Sequence[Sequence[str]],
    *,
    caption: str = "",
    dense: bool = False,
    stack: bool = True,
    empty: str = "",
    row_labels: bool = False,
) -> str:
    """A real table: caption, scoped headers, right-aligned numerics.

    ``stack=True`` emits the class that turns each row into a labelled block at
    narrow widths (see the 620px rules), so no table ever needs a sideways scroll
    on a phone.  Callers put the label on every cell with :func:`cell`.
    """

    head = "".join(
        "<th scope='col'%s>%s</th>" % (" class='num'" if num else "", escape(str(name)))
        for name, num in columns
    )
    body: list[str] = []
    for row in rows:
        cells = list(row)
        if row_labels and cells:
            cells[0] = cells[0].replace("<td", "<th scope='row'", 1)
        body.append(f"<tr>{''.join(cells)}</tr>")
    if not body:
        return f"<div class='table-empty'>{empty or 'Nothing to show.'}</div>"
    cap = f"<caption>{caption}</caption>" if caption else ""
    classes = ["table"]
    if dense:
        classes.append("table--dense")
    if stack:
        classes.append("table--stack")
    return (
        f"<div class='table-wrap{'' if not stack else ' table-wrap--stack'}'>"
        f"<table class='{' '.join(classes)}'>{cap}"
        f"<thead><tr>{head}</tr></thead><tbody>{''.join(body)}</tbody></table></div>"
    )


# ------------------------------------------------------------------ timeline
def timeline(
    events: Sequence[Mapping[str, str]],
    *,
    attention: bool = False,
    caption: str = "",
) -> str:
    klass = "events events--attention" if attention else "events"
    rows: list[str] = []
    for event in events:
        when = escape(str(event.get("when", "")))
        what = event.get("what", "")
        meta = event.get("meta", "")
        meta_html = f"<span class='meta'>{meta}</span>" if meta else ""
        rows.append(
            f"<li><span class='when'>{when}</span>"
            f"<span class='what'>{what}{meta_html}</span></li>"
        )
    cap = f"<p class='small muted'>{caption}</p>" if caption else ""
    return f"{cap}<ol class='{klass}'>{''.join(rows)}</ol>"


# ------------------------------------------------------------------ blockers
def blockers(items: Sequence[Mapping[str, Any]], *, empty: str = "") -> str:
    if not items:
        return f"<p class='muted small'>{empty}</p>" if empty else ""
    rows: list[str] = []
    for item in items:
        title = escape(plain(item.get("title") or item.get("code") or "Blocked"))
        note = plain(item.get("detail") or item.get("note") or "")
        code = plain(item.get("code") or "")
        danger = str(item.get("tone") or "") == "danger"
        rows.append(
            "<li%s>" % (" class='is-danger'" if danger else "")
            + f"<span class='glyph' aria-hidden='true'>{'×' if danger else '!'}</span>"
            "<span><span class='b-title'>"
            + title
            + "</span>"
            + (f"<span class='b-note'>{escape(note)}</span>" if note else "")
            + (f"<span class='b-code'>{escape(code)}</span>" if code else "")
            + "</span></li>"
        )
    return f"<ol class='blockers'>{''.join(rows)}</ol>"


# ---------------------------------------------------------------------- misc
def empty_state(title: str, message: str, *, actions: str = "") -> str:
    acts = f"<div class='e-actions'>{actions}</div>" if actions else ""
    return (
        "<div class='empty'>"
        f"<span class='e-title'>{escape(title)}</span>"
        f"<p class='e-body'>{message}</p>{acts}</div>"
    )


def meter(fraction: float, *, tone: str = "") -> str:
    width = max(0.0, min(1.0, float(fraction))) * 100
    klass = "meter" + (f" meter--{tone}" if tone else "")
    return f"<div class='{klass}'><span style='width:{width:.0f}%'></span></div>"


def link(href: str, label: str, *, klass: str = "") -> str:
    return f"<a class='{klass}' href='{attr(href)}'>{escape(label)}</a>"


def button_link(href: str, label: str, *, tone: str = "", small: bool = False) -> str:
    classes = ["btn"]
    if tone:
        classes.append(f"btn--{tone}")
    if small:
        classes.append("btn--sm")
    return f"<a class='{' '.join(classes)}' href='{attr(href)}'>{escape(label)}</a>"


def action_form(
    action: str,
    nonce: str,
    label: str,
    *,
    tone: str = "",
    fields: str = "",
    described_by: str = "",
    small: bool = False,
    title: str = "",
) -> str:
    """A plain POST: no script, no fetch, no storage.

    The per-process nonce proves the request came from a page this process
    rendered; the dispatcher rejects anything else before it looks at the action.
    """

    classes = ["btn"]
    if tone:
        classes.append(f"btn--{tone}")
    if small:
        classes.append("btn--sm")
    described = f" aria-describedby='{attr(described_by)}'" if described_by else ""
    heading = f" title='{attr(title)}'" if title else ""
    return (
        f"<form method='post' action='/actions/{attr(action)}'>"
        f"<input type='hidden' name='nonce' value='{attr(nonce)}'>"
        f"{fields}"
        f"<button class='{' '.join(classes)}' type='submit'{described}{heading}>"
        f"{escape(label)}</button></form>"
    )


def field(
    name: str,
    label: str,
    *,
    kind: str = "text",
    value: str = "",
    hint: str = "",
    placeholder: str = "",
    required: bool = False,
    options: Sequence[tuple[str, str]] = (),
    rows: int = 0,
    field_id: str = "",
) -> str:
    """One labelled control, with its hint wired up instead of floating loose."""

    ident = field_id or f"f-{name.replace('_', '-')}"
    hint_id = f"{ident}-hint" if hint else ""
    described = f" aria-describedby='{attr(hint_id)}'" if hint else ""
    req = " required" if required else ""
    req_mark = "<span aria-hidden='true'> *</span>" if required else ""
    hint_html = f"<span class='hint' id='{attr(hint_id)}'>{hint}</span>" if hint else ""
    if kind == "select":
        body = "".join(
            f"<option value='{attr(value_)}'"
            f"{' selected' if str(value_) == str(value) else ''}>{escape(str(text_))}</option>"
            for value_, text_ in options
        )
        control = f"<select id='{attr(ident)}' name='{attr(name)}'{req}{described}>{body}</select>"
    elif kind in {"textarea"}:
        control = (
            f"<textarea id='{attr(ident)}' name='{attr(name)}' rows='{rows or 3}'"
            f"{req}{described}>{escape(str(value))}</textarea>"
        )
    elif kind == "checkbox":
        return (
            f"<div class='field field--check'>"
            f"<input id='{attr(ident)}' type='checkbox' name='{attr(name)}' value='1'"
            f"{' checked' if value else ''}{req}{described}>"
            f"<label class='label' for='{attr(ident)}'>{escape(label)}{req_mark}"
            f"{hint_html}</label></div>"
        )
    else:
        placeholder_attr = f" placeholder='{attr(placeholder)}'" if placeholder else ""
        control = (
            f"<input id='{attr(ident)}' type='{attr(kind)}' name='{attr(name)}'"
            f" value='{attr(value)}'{req}{described}{placeholder_attr}>"
        )
    return (
        f"<div class='field'><label for='{attr(ident)}'>{escape(label)}{req_mark}</label>"
        f"{control}{hint_html}</div>"
    )


def segmented(name: str, label: str, choices: Sequence[tuple[str, str]], *, value: str = "") -> str:
    """A radio group drawn as a segmented control — still a fieldset with a legend."""

    options = "".join(
        f"<label><input type='radio' name='{attr(name)}' value='{attr(value_)}'"
        f"{' checked' if str(value_) == str(value) else ''}> {escape(str(text_))}</label>"
        for value_, text_ in choices
    )
    return (
        f"<fieldset class='field'><legend>{escape(label)}</legend>"
        f"<div class='segmented'>{options}</div></fieldset>"
    )


def pager(page: int, pages: int, href_for: Any, *, noun: str = "rows") -> str:
    if pages <= 1:
        return ""
    parts = []
    if page > 1:
        parts.append(button_link(href_for(page - 1), "← Previous", small=True))
    parts.append(
        f"<span class='muted'>Page {page} of {pages}</span>"
        f"<span class='muted'>{noun}</span>"
    )
    if page < pages:
        parts.append(button_link(href_for(page + 1), "Next →", small=True))
    return f"<nav class='pager' aria-label='Pagination'>{parts[0]}<span class='spacer'></span>{''.join(parts[1:])}</nav>"


def key_values(pairs: Sequence[tuple[str, str]]) -> str:
    """Compact key/value chips for provenance lines (``Data source … · read …``)."""

    return fact_line(pairs)


def code(value: Any) -> str:
    return f"<code>{escape(str(value))}</code>"


def strong(value: Any) -> str:
    return f"<strong>{escape(str(value))}</strong>"


def paragraph(value: str, *, klass: str = "") -> str:
    klass_attr = f" class='{klass}'" if klass else ""
    return f"<p{klass_attr}>{value}</p>"

"""Pieces more than one page needs.

Provenance, data-class attribution, the safety statement and the "where this
came from" block are written once here so two pages cannot describe the same
source differently — the failure mode the old UI had when four panels each grew
their own footnote.
"""

from __future__ import annotations

from html import escape
from typing import Any, Mapping, Sequence

from .. import format as fmt
from ..components import (
    cell,
    chip,
    facts,
    key_values,
    notice,
    paragraph,
    section,
    table,
)
from ..shell import PAGE_SCOPES, page_scope


def data_chip(label: Mapping[str, str]) -> str:
    return chip(str(label.get("label") or "UNKNOWN"), str(label.get("tone") or "muted"), dot=True)


def account_view(ctx: Any) -> dict[str, Any]:
    return dict((ctx.extra or {}).get("account") or {})


def data_view(ctx: Any) -> dict[str, Any]:
    return dict((ctx.extra or {}).get("data") or {})


def query_value(ctx: Any, key: str, default: str = "") -> str:
    query = dict((ctx.extra or {}).get("query") or {})
    values = query.get(key) or []
    return str(values[0]) if values else default


def query_page(ctx: Any, key: str = "page") -> int:
    raw = query_value(ctx, key, "1")
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return 1


def page_of(rows: Sequence[Any], page: int, per_page: int = 25) -> list[Any]:
    start = max(1, page) - 1
    return list(rows)[start * per_page : start * per_page + per_page]


def pages_for(rows: Sequence[Any], per_page: int = 25) -> int:
    return max(1, (len(list(rows)) + per_page - 1) // per_page)


def href_with(page: str, *, query: str = "") -> str:
    return f"{page}?{query}" if query else page


def scope_badges(ctx: Any) -> str:
    _key, label, tone, _note = page_scope(ctx)
    badges = [chip(label, tone, dot=True)]
    paper_label = getattr(ctx, "paper_label", {"label": "UNKNOWN", "tone": "muted"})
    badges.append(data_chip(paper_label))
    return "".join(badges)


def safety_section(*, id: str = "safety") -> str:
    from ...setup import safety_statement

    safety = safety_statement()
    items = "".join(f"<li>{fmt.text(line)}</li>" for line in safety["lines"])
    body = f"<ul class='stack--sm' style='padding-left:18px;margin:0'>{items}</ul>"
    return section(
        "What this workstation will not do",
        body,
        note="These are enforced by construction, not by convention.",
        id=id,
    )


def boundaries_section(rows: Sequence[tuple[str, str]], *, id: str = "boundaries") -> str:
    return section(
        "Boundaries",
        facts(rows),
        note="Read-only view of the platform's own safety state.",
        id=id,
    )


def provenance_section(
    pairs: Sequence[tuple[str, str]],
    *,
    title: str,
    note: str = "",
    id: str = "provenance",
) -> str:
    """The block that answers "where did these numbers come from?"."""

    return section(title, facts(pairs), note=note, id=id, plain=True)


def provenance_line(pairs: Sequence[tuple[str, str]]) -> str:
    return key_values(pairs)


def quote_facts(account: Mapping[str, Any]) -> list[tuple[str, str]]:
    """Quote freshness as facts, with the age of each against the ledger read."""

    read_at = account.get("read_at")
    quotes = [dict(item) for item in (account.get("quotes") or ())]
    if not quotes:
        return [("Quotes", chip("no quote yet", "muted"))]
    rows = []
    for quote in quotes:
        symbol = fmt.text(quote.get("symbol") or "?")
        when = quote.get("quote_time") or quote.get("at") or quote.get("timestamp")
        fresh = bool(quote.get("fresh"))
        label = chip("fresh" if fresh else "stale", "ok" if fresh else "warn")
        age = fmt.text(quote.get("age_text") or "") or (
            fmt.age(when, read_at) if when else "no timestamp"
        )
        stamp = f" <span class='mono small'>{fmt.text(when)}</span>" if when else ""
        rows.append((f"Quote {symbol}", f"{label} {age}{stamp}"))
    return rows


def quotes_chip(account: Mapping[str, Any]) -> str:
    """The one chip that says whether decisions may be taken right now."""

    trading = dict(account.get("trading") or {})
    if trading.get("paused"):
        return chip("PAUSED", "warn", dot=True)
    quotes = [dict(item) for item in (account.get("quotes") or ())]
    if not quotes:
        return chip("NO QUOTE YET", "muted")
    if all(bool(item.get("fresh")) for item in quotes):
        return chip("QUOTES FRESH", "ok", dot=True)
    return chip("QUOTES STALE", "warn", dot=True)


def metric_rows(metrics: Sequence[Mapping[str, Any]]) -> list[list[str]]:
    """The accounting layer's own metric rows: label, value, basis.

    The value and its note come from the module that computed them, so this table
    cannot sharpen, round or re-word a number on its way to the screen.
    """

    rows: list[list[str]] = []
    for metric in metrics:
        label = fmt.text(metric.get("label") or metric.get("key") or "")
        value = str(metric.get("value") or fmt.UNKNOWN)
        available = bool(metric.get("available"))
        value_html = fmt.text(value) if available else (
            f"<span class='muted'>{fmt.text(value)}</span>"
        )
        rows.append(
            [
                cell(label, label="Metric"),
                cell(value_html, label="Value"),
                cell(fmt.text(metric.get("note") or ""), label="Basis"),
            ]
        )
    return rows


def metrics_table(
    metrics: Sequence[Mapping[str, Any]],
    *,
    caption: str,
    empty: str = "No figure was reported.",
) -> str:
    return table(
        (("Metric", False), ("Value", False), ("Basis", False)),
        metric_rows(metrics),
        caption=caption,
        empty=empty,
        stack=True,
    )


def no_quote_notice(account: Mapping[str, Any]) -> str:
    from ..components import notice

    if account.get("quotes"):
        return ""
    return notice(
        "PAUSED · NO QUOTES",
        "No quote has been recorded yet, so no mark can be judged and no decision "
        "can be taken.",
        tone="warn",
    )


def quote_chip(account: Mapping[str, Any]) -> str:
    """Freshness of the newest quotes, as one chip and never an invented state."""

    from ..components import chip

    quotes = [dict(item) for item in (account.get("quotes") or ())]
    if not quotes:
        return chip("no quote yet", "muted")
    fresh = sum(1 for item in quotes if item.get("fresh"))
    if fresh == len(quotes):
        return chip(f"quotes fresh · {fresh} of {len(quotes)}", "ok", dot=True)
    return chip(f"stale quotes · {len(quotes) - fresh} of {len(quotes)}", "warn", dot=True)


def paused_notice(account: Mapping[str, Any]) -> str:
    """Why decisions are not being taken, in the ledger's own sentence."""

    from ..components import notice

    trading = dict(account.get("trading") or {})
    if not trading.get("paused"):
        return ""
    reasons = [str(item) for item in (trading.get("reasons") or ()) if str(item).strip()]
    detail = " · ".join(reasons) or str(trading.get("reason") or "")
    return notice(
        "Trading decisions are paused",
        f"{chip('PAUSED', 'warn', dot=True)} {escape(detail)}",
        tone="warn",
    )


def scope_note_for(page: str) -> str:
    return PAGE_SCOPES.get(page, ("", "", "", ""))[3]


def small_note(text: str) -> str:
    return paragraph(fmt.text(text), klass="small muted")


# ---------------------------------------------------------------- live path facts
def feed_cell(feed: Mapping[str, Any]) -> str:
    """A live feed's freshness: fresh, stale, or not measured -- never assumed."""

    if not feed:
        return "not measured"
    fresh = feed.get("fresh")
    if fresh is None:
        return chip("not measured", "muted")
    age = feed.get("age_seconds")
    age_text = "age unknown" if age is None else f"{float(age):.1f}s old"
    return (
        chip("fresh", "ok", dot=True) if fresh else chip("stale", "danger", dot=True)
    ) + (
        " <span class='small muted'>{} · {} · {}</span>".format(
            fmt.text(age_text),
            fmt.text(str(feed.get("source") or "")),
            fmt.text(str(feed.get("symbol") or "")),
        )
    )


def duration_ms(value: Any) -> str:
    """A duration the live path measured, or the words for one it did not."""

    if value is None or value == "":
        return "not measured by this path"
    try:
        return f"{float(value):.1f} ms"
    except (TypeError, ValueError):
        return "not measured by this path"


def live_orders_table(orders: Sequence[Mapping[str, Any]], *, caption: str) -> str:
    """Every order the live path attempted, with the path's own answer."""

    return table(
        (("Request", False), ("At", False), ("Status", False), ("Refusal", False),
         ("Detail", False), ("Unresolved", False)),
        [
            [
                cell(fmt.text(item.get("request_id") or ""), label="Request", mono=True),
                cell(fmt.text(item.get("at") or ""), label="At", mono=True),
                cell(fmt.text(item.get("status") or ""), label="Status"),
                cell(fmt.text(item.get("refusal") or "—"), label="Refusal"),
                cell(fmt.text(item.get("detail") or "—"), label="Detail"),
                cell(fmt.text("yes" if item.get("unresolved") else "no"), label="Unresolved"),
            ]
            for item in orders
        ],
        caption=caption,
    )


def live_latency_facts(latency: Mapping[str, Any]) -> str:
    """Measured timings, with the sentence that stops them reading as fill speed."""

    return facts(
        [
            ("Decision", duration_ms(latency.get("decision_ms"))),
            ("Submission call", duration_ms(latency.get("submit_ms"))),
            ("Session start", duration_ms(latency.get("start_ms"))),
            ("Reading this page", duration_ms(latency.get("view_ms"))),
            ("Cycles measured", fmt.text(latency.get("cycles") or 0)),
        ]
    ) + (
        "<p class='small muted'>Measured on this machine, in this process. Not a fill time, "
        "not a broker measurement, and not a promise about execution speed.</p>"
    )



def fold(title: str, html: str, *, count: str = "", note: str = "", anchor: str = "") -> str:
    """A section an operator can put away without losing it.

    History belongs on the page; it does not have to be on the screen while a
    session is being operated.  Nothing is removed from the document -- the same
    markup, the same ids and the same tables are inside the disclosure.
    """

    badge = f"<span class='chip chip--muted'>{escape(count)}</span>" if count else ""
    sub = f"<span class='fold-n'>{escape(note)}</span>" if note else ""
    ident = f" id='{escape(anchor)}'" if anchor else ""
    return (
        f"<details class='fold'{ident}><summary>"
        f"<span class='fold-h'>{escape(title)}</span>{badge}{sub}"
        "</summary>"
        f"<div class='fold-body'>{html}</div></details>"
    )

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
from ..components import chip, facts, key_values, paragraph, section, table, cell
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
        when = quote.get("at") or quote.get("timestamp")
        fresh = bool(quote.get("fresh"))
        label = chip("fresh" if fresh else "stale", "ok" if fresh else "warn")
        age = fmt.age(when, read_at) if when else "no timestamp"
        rows.append((f"Quote {symbol}", f"{label} {fmt.text(age)}"))
    return rows


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


def paused_notice(account: Mapping[str, Any]) -> str:
    """Why decisions are not being taken, in the ledger's own sentence."""

    from ..components import notice

    trading = dict(account.get("trading") or {})
    if not trading.get("paused"):
        return ""
    reasons = [str(item) for item in (trading.get("reasons") or ()) if str(item).strip()]
    detail = " · ".join(reasons) or str(trading.get("reason") or "")
    return notice("Trading decisions are paused", escape(detail), tone="warn")


def scope_note_for(page: str) -> str:
    return PAGE_SCOPES.get(page, ("", "", "", ""))[3]


def small_note(text: str) -> str:
    return paragraph(fmt.text(text), klass="small muted")

"""Page dispatch: the new pages, one registry, and nothing implicit.

``PAGES`` is the whole of the redesigned surface.  A page that is not in it yet
falls back to the legacy renderer, which is the transitional step of a
page-by-page replacement; the fallback disappears with ``views.py`` itself.
"""

from __future__ import annotations

from typing import Any, Callable

from html import escape

from ..context import PageContext
from . import shell
from .components import button_link, notice, page_head
from .pages import autopilot, monitoring
from .theme import STYLES  # re-exported for the tests that read it

PAGES: dict[str, Callable[[PageContext], str]] = {
    "health": monitoring.render_health,
    "activity": monitoring.render_activity,
    "positions": monitoring.render_positions,
    "performance": monitoring.render_performance,
    "autopilot": autopilot.render_autopilot,
}

#: Pages whose new composition is complete.  Everything else is still the old
#: renderer and its own document, until its module lands here.
_REDESIGNED = frozenset(PAGES)


def render_page(ctx: PageContext) -> str:
    page = str(getattr(ctx, "page", ""))
    renderer = PAGES.get(page)
    if renderer is None:  # transitional
        from .. import views as legacy

        return legacy.render_page(ctx)
    return shell.document(ctx, renderer(ctx))


def render_connecting(ctx: PageContext) -> str:
    from .. import views as legacy

    return legacy.render_connecting(ctx)


def render_message(
    ctx: PageContext,
    *,
    tone: str,
    title: str,
    message: str,
    actions: str = "",
) -> str:
    """A standalone answer for a route that cannot render a page."""

    mapped = {"error": "danger", "danger": "danger", "warn": "warn"}.get(tone, "info")
    head = page_head("Workstation", title, "")
    body = (
        head
        + notice(title, escape(message), tone=mapped, role="alert")
        + (
            f"<div class='actions' style='margin-top:16px'>{actions}</div>"
            if actions
            else "<div class='actions' style='margin-top:16px'>"
            + button_link("/autopilot", "Back to Autopilot")
            + "</div>"
        )
    )
    return shell.document(ctx, body)


__all__ = ["PAGES", "STYLES", "render_message", "render_page", "render_connecting"]

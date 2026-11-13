"""The AXIOM-FOREX workstation interface.

This package is the whole presentation layer: the stylesheet, the shell, the
components, and one module per page.  Pages read the context the application
already built and render it; nothing here reads a ledger, a file or a broker
itself, so a page can never show a fact that was not read for it.

``render_page`` is the only entry point the server calls.
"""

from __future__ import annotations

from .render import render_connecting, render_message, render_page
from .shell import NAV, NAV_GROUPS, PAGE_PATHS, PAGE_SCOPES, page_scope
from .theme import BREAKPOINTS, STYLES

__all__ = [
    "BREAKPOINTS",
    "NAV",
    "NAV_GROUPS",
    "PAGE_PATHS",
    "PAGE_SCOPES",
    "STYLES",
    "page_scope",
    "render_connecting",
    "render_message",
    "render_page",
]

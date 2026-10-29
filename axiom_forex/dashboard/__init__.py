"""Pure HTML dashboard rendering from sanitized status view models."""

from .view import calendar_rows, health_rows, render_dashboard, risk_rows

__all__ = ["calendar_rows", "health_rows", "render_dashboard", "risk_rows"]

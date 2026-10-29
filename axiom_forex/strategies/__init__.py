"""Strategy interfaces and explicit example strategies (none active by default)."""

from .base import Signal, Strategy
from .examples import SmaCrossStrategy

__all__ = ["Signal", "SmaCrossStrategy", "Strategy"]

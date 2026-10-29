"""Bid/ask next-bar backtesting with costs, calendar gaps, and OOS validation."""

from .engine import BacktestConfig, BacktestResult, ForexBacktestEngine
from .validation import OOSValidation, run_oos

__all__ = [
    "BacktestConfig",
    "BacktestResult",
    "ForexBacktestEngine",
    "OOSValidation",
    "run_oos",
]

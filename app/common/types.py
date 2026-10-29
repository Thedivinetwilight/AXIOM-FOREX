"""Small enums shared across the M1 layers."""

from enum import Enum


class TradingMode(str, Enum):
    RESEARCH = "RESEARCH"
    PAPER = "PAPER"
    LIVE = "LIVE"


class ExecutionMode(str, Enum):
    LOCAL_PAPER = "LOCAL_PAPER"


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"

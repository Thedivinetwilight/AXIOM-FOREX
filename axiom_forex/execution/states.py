from __future__ import annotations

from enum import Enum


class OrderState(str, Enum):
    NEW = "NEW"
    SIMULATED = "SIMULATED"
    FILLED = "FILLED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    UNRESOLVED = "UNRESOLVED"


FINAL_STATES = frozenset(
    {OrderState.FILLED.value, OrderState.REJECTED.value, OrderState.CANCELLED.value}
)

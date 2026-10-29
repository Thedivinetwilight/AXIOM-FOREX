"""Shared execution configuration for backtests and paper workflows."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any


@dataclass(frozen=True)
class ExecutionConfig:
    dataset_id: str
    quantity: Decimal
    provider_id: str
    provider_symbol: str
    strategy_id: str
    max_open_positions: int = 1
    allow_short: bool = True
    final_bar_signal_cancelled: bool = True
    rollover_cutoffs: tuple[datetime, ...] = ()

    def audit_view(self) -> dict[str, Any]:
        return asdict(self)

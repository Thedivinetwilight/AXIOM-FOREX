from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class LifecycleStage(str, Enum):
    RESEARCH = "RESEARCH"
    VALIDATION = "VALIDATION"
    WALK_FORWARD = "WALK_FORWARD"
    SHADOW = "SHADOW"
    CANARY = "CANARY"
    APPROVED = "APPROVED"
    RETIRED = "RETIRED"


LADDER = (
    LifecycleStage.RESEARCH,
    LifecycleStage.VALIDATION,
    LifecycleStage.WALK_FORWARD,
    LifecycleStage.SHADOW,
    LifecycleStage.CANARY,
    LifecycleStage.APPROVED,
)


@dataclass
class StrategyLifecycle:
    strategy_id: str
    stage: LifecycleStage = LifecycleStage.RESEARCH
    active: bool = False
    history: list[dict[str, str]] = field(default_factory=list)

    def promote(self) -> LifecycleStage:
        if self.stage == LifecycleStage.RETIRED:
            raise ValueError("Retired strategies cannot be promoted")
        index = LADDER.index(self.stage)
        if index + 1 >= len(LADDER):
            return self.stage
        self.stage = LADDER[index + 1]
        self.history.append({"from": LADDER[index].value, "to": self.stage.value})
        return self.stage

    def retire(self) -> None:
        self.stage = LifecycleStage.RETIRED
        self.active = False
        self.history.append({"from": "ANY", "to": self.stage.value})

    def approve_active(self) -> bool:
        return self.stage == LifecycleStage.APPROVED and self.active

    def audit_view(self) -> dict[str, object]:
        return {
            "strategy_id": self.strategy_id,
            "stage": self.stage.value,
            "active": self.active,
            "history": list(self.history),
            "tradable": self.approve_active(),
        }

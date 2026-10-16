"""Main-run outcome contracts for the AG-UI adapter."""

from __future__ import annotations

from typing import Literal

from ag_ui.core import BaseEvent
from ag_ui.core import Interrupt as AgUiInterrupt
from pydantic import Field, model_validator

from tinkerfin_contracts import RunIdentity as RunIdentity

from .models import RuntimeModel


class AgentRunOutcome(RuntimeModel):
    """Typed success or interrupt outcome returned to a lifecycle owner."""

    type: Literal["success", "interrupt"] = Field(
        description="Main run completion type"
    )
    interrupts: tuple[AgUiInterrupt, ...] = Field(
        default=(),
        description="AG-UI interrupts present when the run pauses",
    )

    @model_validator(mode="after")
    def validate_interrupts(self) -> AgentRunOutcome:
        """Require interrupts exactly when the outcome type is `interrupt`."""

        if self.type == "success" and self.interrupts:
            raise ValueError("success outcome cannot contain interrupts")
        if self.type == "interrupt" and not self.interrupts:
            raise ValueError("interrupt outcome requires interrupts")
        return self


AgentEvent = BaseEvent

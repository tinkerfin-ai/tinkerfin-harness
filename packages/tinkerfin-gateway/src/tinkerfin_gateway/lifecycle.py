"""Host registration, confirmed resume outcomes, and committed run observations."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Literal, Protocol, TypeAlias, cast

from ag_ui.core import BaseEvent, RunErrorEvent, RunFinishedEvent, RunStartedEvent
from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator

from tinkerfin import AgUiResumeReceipt
from tinkerfin_contracts import RunIdentity


@dataclass(frozen=True, slots=True)
class RunAcceptance:
    """Identify prepared new execution or attachment to a retained command."""

    identity: RunIdentity
    kind: Literal["new", "existing"]


class RunRegistration(Protocol):
    """Settle host-owned registration for one command submission.

    Bind identities and the submission's ownership token before passing this
    object. Methods may outlive the HTTP request: use application resources and
    short transactions, never an open request transaction. Confirmation failure
    retains the registration for reconciliation against authoritative run facts.
    """

    async def confirm(self, acceptance: RunAcceptance) -> None:
        """Confirm prepared new execution or attachment to the same saved command.

        New execution is prepared but has not consumed its source. An existing
        delivery does not prove that Runtime preparation has finished. Confirm
        idempotently and start any host projection needed for either outcome.
        """
        ...

    async def release(self) -> None:
        """Release only this submission's unaccepted reservation.

        Another submission may already own this run, including one with conflicting
        content. Never delete or revert another submission's accepted registration.
        No release is reported after prepared execution or confirmed attachment.
        """
        ...


class ResumeSettlement(Protocol):
    """Persist confirmed decision outcomes independently of command admission.

    Saved receipts can repeat with the same receipt ID. An unused retry source or
    unknown checkpoint outcome may invoke neither method. Never interpret silence,
    cancellation, admission, or delivery termination as proof of not-saved state.
    """

    async def saved(self, receipt: AgUiResumeReceipt) -> None:
        """Record idempotently that the decisions were saved before continuation."""
        ...

    async def not_saved(self) -> None:
        """Release decision claims only after Runtime proves they were not saved."""
        ...


@dataclass(frozen=True, slots=True)
class CommittedRunEvent:
    """Observe a main-run lifecycle event after durable output commit.

    This producer-only observation never repeats on output replay. Observer
    failures do not roll back execution; use registration or resume settlement
    for required business writes. Diagnostic errors are trusted-host information
    and must never be included in client payloads.
    """

    identity: RunIdentity
    event: RunStartedEvent | RunFinishedEvent | RunErrorEvent
    diagnostic_error: Exception | None = field(default=None, repr=False)


CommittedRunObserver: TypeAlias = Callable[[CommittedRunEvent], Awaitable[None]]


class RunPresentation(BaseModel):
    """Decorate committed output without changing its lifecycle or command binding.

    Attributes are additional JSON fields on the main RUN_STARTED event. Reserved
    protocol fields cannot be replaced. Presentation does not change when a retry
    attaches to events already stored by the original producer.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    start_attributes: dict[str, JsonValue] = Field(default_factory=dict)
    cancelled_message: str | None = None

    @field_validator("start_attributes")
    @classmethod
    def protect_protocol_fields(
        cls, value: dict[str, JsonValue]
    ) -> dict[str, JsonValue]:
        """Keep identity, ordering, and lifecycle fields controlled by the Runtime."""
        reserved = set(RunStartedEvent.model_fields)
        reserved.update(
            item.alias for item in RunStartedEvent.model_fields.values() if item.alias
        )
        if reserved.intersection(value):
            raise ValueError("start_attributes cannot replace AG-UI protocol fields")
        return value

    def _apply(self, event: BaseEvent, identity: RunIdentity) -> BaseEvent:
        raw: object = event.raw_event
        if isinstance(event, RunStartedEvent) and event.run_id == identity.run_id:
            return event.model_copy(update=self.start_attributes, deep=True)
        if (
            isinstance(event, RunErrorEvent)
            and event.code == "cancelled"
            and self.cancelled_message is not None
            and isinstance(raw, dict)
            and cast(Mapping[str, object], raw).get("runId") == identity.run_id
        ):
            return event.model_copy(
                update={"message": self.cancelled_message}, deep=True
            )
        return event


__all__ = [
    "CommittedRunEvent",
    "CommittedRunObserver",
    "ResumeSettlement",
    "RunAcceptance",
    "RunPresentation",
    "RunRegistration",
]

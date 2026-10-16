"""AG-UI main-run lifecycle event construction."""

from __future__ import annotations

from typing import cast

from ag_ui.core import (
    BaseEvent,
    RunErrorEvent,
    RunFinishedEvent,
    RunFinishedInterruptOutcome,
    RunFinishedSuccessOutcome,
    RunStartedEvent,
)

from .contracts import AgentRunOutcome, RunIdentity


class AgUiLifecycleEventFactory:
    """Build exactly-one-owner events from one canonical run identity.

    The factory is stateless: it does not decide terminal ownership, access a
    graph or checkpointer, or send events over a transport. Callers must emit a
    start before converted child events, then one finished or failed terminal
    after all open child lifecycles have been closed.
    """

    def started(
        self,
        *,
        identity: RunIdentity,
        parent_run_id: str | None = None,
    ) -> BaseEvent:
        """Build the main start event without fabricating protocol input.

        Args:
            identity: Canonical public, Graph, checkpoint, and delivery identity.
            parent_run_id: Optional completed or resumable parent run in the same
                canonical thread.

        Returns:
            A validated AG-UI start event whose optional input is omitted.

        Raises:
            TypeError: The identity or parent identifier has the wrong type.
            ValueError: The parent identifier is empty, non-canonical, or self-referential.
        """

        self.validate_identity(identity)
        self.validate_parent_run_id(parent_run_id, identity=identity)
        return RunStartedEvent(
            thread_id=identity.thread_id,
            run_id=identity.run_id,
            parent_run_id=parent_run_id,
        )

    @staticmethod
    def validate_identity(identity: RunIdentity) -> None:
        """Reject values outside the shared RunIdentity contract."""

        if not isinstance(identity, RunIdentity):
            raise TypeError("identity must be a RunIdentity")

    @staticmethod
    def validate_parent_run_id(
        parent_run_id: str | None,
        *,
        identity: RunIdentity,
    ) -> None:
        """Require an optional canonical parent distinct from the current run."""

        if parent_run_id is None:
            return
        if not isinstance(parent_run_id, str):
            raise TypeError("parent_run_id must be a string or None")
        if not parent_run_id or parent_run_id != parent_run_id.strip():
            raise ValueError("parent_run_id must be a canonical non-empty string")
        if parent_run_id == identity.run_id:
            raise ValueError("parent_run_id must differ from identity.run_id")

    def finished(
        self,
        *,
        identity: RunIdentity,
        outcome: AgentRunOutcome,
    ) -> BaseEvent:
        """Build a success or interrupt terminal from an adapter outcome."""

        self.validate_identity(identity)
        if outcome.type == "interrupt":
            run_outcome = RunFinishedInterruptOutcome(
                interrupts=list(outcome.interrupts)
            )
        else:
            run_outcome = RunFinishedSuccessOutcome()
        return RunFinishedEvent(
            thread_id=identity.thread_id,
            run_id=identity.run_id,
            outcome=run_outcome,
        )

    def failed(
        self,
        *,
        identity: RunIdentity,
        message: str,
        code: str,
        parent_run_id: str | None = None,
    ) -> BaseEvent:
        """Build a main error terminal from the canonical runtime identity."""

        self.validate_identity(identity)
        self.validate_parent_run_id(parent_run_id, identity=identity)
        raw_event = {
            "threadId": identity.thread_id,
            "runId": identity.run_id,
        }
        if parent_run_id is not None:
            raw_event["parentRunId"] = parent_run_id
        return RunErrorEvent(
            message=message,
            code=code,
            raw_event=raw_event,
        )

    def is_main_lifecycle(self, event: BaseEvent, *, identity: RunIdentity) -> bool:
        """Return whether an event competes for the identified main lifecycle."""

        self.validate_identity(identity)
        return (
            isinstance(
                event,
                RunStartedEvent | RunFinishedEvent | RunErrorEvent,
            )
            and self.event_run_id(event) == identity.run_id
        )

    @staticmethod
    def event_run_id(event: BaseEvent) -> str | None:
        """Read a run ID from the standard field or validated `rawEvent`."""

        run_id = getattr(event, "run_id", None)
        if isinstance(run_id, str) and run_id:
            return run_id
        raw_event = event.raw_event
        if not isinstance(raw_event, dict):
            return None
        raw_run_id = cast(dict[object, object], raw_event).get("runId")
        return raw_run_id if isinstance(raw_run_id, str) else None

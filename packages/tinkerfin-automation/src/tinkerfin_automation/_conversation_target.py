"""Settle scheduled conversations through public Gateway delivery contracts."""

from __future__ import annotations

import asyncio
import json
from uuid import NAMESPACE_URL, uuid5

from ag_ui.core import RunErrorEvent, RunFinishedEvent

from tinkerfin import AgentRuntime
from tinkerfin.plan import AgentMode
from tinkerfin_agui_adapter.media import user_content_to_agui
from tinkerfin_gateway import (
    CommittedRunObserver,
    Gateway,
    RunAcceptance,
    RunPresentation,
    RunRegistration,
    StartRun,
)
from tinkerfin_messaging.agui import AgUiCodec

from ._tasks import capture, join_owned_task, select_failure
from .errors import TargetExecutionError
from .models import ExecutionFailure
from .targets import (
    ExecutionFailed,
    ExecutionInterrupted,
    ExecutionOutcome,
    ExecutionRequest,
    ExecutionSucceeded,
    ExecutionUncertain,
)


class _Admission:
    """Retain producer ownership even when host confirmation is interrupted."""

    def __init__(self, registration: RunRegistration | None) -> None:
        self._registration = registration
        self.owned = False

    async def confirm(self, acceptance: RunAcceptance) -> None:
        self.owned = acceptance.kind == "new"
        if self._registration is not None:
            await self._registration.confirm(acceptance)

    async def release(self) -> None:
        self.owned = False
        if self._registration is not None:
            await self._registration.release()


async def run_conversation(
    runtime: AgentRuntime[None],
    request: ExecutionRequest,
    *,
    gateway: Gateway,
    mode: AgentMode | None,
    registration: RunRegistration | None,
    on_committed: CommittedRunObserver | None,
    presentation: RunPresentation | None,
) -> ExecutionOutcome:
    """Wait through delivery settlement without buffering conversation content."""
    identity = request.execution.identity
    admission = _Admission(registration)
    terminal: RunFinishedEvent | RunErrorEvent | None = None
    codec = AgUiCodec()
    delivery_called = False
    try:
        if identity.namespace != runtime.namespace:
            raise TargetExecutionError("Execution and Runtime namespaces must match")
        if set(request.input) != {"messages"}:
            raise TargetExecutionError(
                "Conversation executions require a messages-only input"
            )
        command = StartRun.model_validate(
            {
                "thread_id": identity.thread_id,
                "run_id": identity.run_id,
                "messages": request.input["messages"],
                "mode": runtime.default_mode if mode is None else mode,
            }
        )
        # Graph-style task messages may omit protocol IDs. Allocate them from the
        # exact saved execution and position, preserving every supplied ID so
        # retries, delivery and historical projection observe the same messages.
        messages = tuple(
            message
            if "id" in message
            else {
                **message,
                "id": "automation-message-"
                + str(
                    uuid5(
                        NAMESPACE_URL,
                        json.dumps(
                            [
                                identity.namespace,
                                identity.thread_id,
                                identity.run_id,
                                index,
                            ]
                        ),
                    )
                ),
            }
            for index, message in enumerate(command.messages)
        )
        messages = tuple(
            {**message, "content": user_content_to_agui(message.get("content"))}
            if message.get("role") == "user"
            else message
            for message in messages
        )
        command = command.model_copy(update={"messages": messages})
        delivery_called = True
        output = await gateway.stream(
            runtime,
            command,
            after=0,
            registration=admission,
            on_committed=on_committed,
            presentation=presentation,
        )
        # The terminal event can precede source cleanup. Exhausting the bounded
        # subscription waits for Messaging's durable producer settlement instead
        # of declaring success from an observer callback or RUN_FINISHED alone.
        async with output:
            async for item in output:
                event = item.data
                if isinstance(event, RunFinishedEvent | RunErrorEvent) and (
                    codec.ends_publication(event, identity=identity)
                ):
                    if terminal is not None:
                        raise TargetExecutionError(
                            "Conversation emitted multiple main terminal events"
                        )
                    terminal = event
        if terminal is None:
            raise TargetExecutionError("Conversation ended without a main terminal")
    except BaseException as error:
        if admission.owned or (not delivery_called and registration is not None):
            # Gateway owns admission and Messaging owns its producer. This target
            # owns only the newly accepted run's cancellation request. Join cleanup
            # despite repeated cancellation before returning control to Automation.
            operation = (
                gateway.run(identity).cancel()
                if admission.owned
                else admission.release()
            )
            cleanup = asyncio.create_task(
                capture(operation),
                name=f"automation-conversation-cancel:{identity.run_id}",
            )
            try:
                await join_owned_task(cleanup)
            except BaseException as cleanup_error:  # noqa: BLE001 - retain cancellation and cleanup failures
                raise select_failure(error, cleanup_error)
        raise

    if isinstance(terminal, RunErrorEvent):
        failure = ExecutionFailure(
            code=terminal.code or "automation.conversation_failed",
            message=terminal.message,
        )
        if terminal.code == "cancelled":
            return ExecutionUncertain(failure)
        return ExecutionFailed(failure)
    if terminal.outcome is not None and terminal.outcome.type == "interrupt":
        return ExecutionInterrupted(
            tuple(item.id for item in terminal.outcome.interrupts)
        )
    return ExecutionSucceeded()

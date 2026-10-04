"""Admitted output precedes slow workspaces without losing run ownership."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from ag_ui.core import RunErrorEvent, RunStartedEvent
from deepagents.backends import StateBackend
from deepagents.backends.protocol import BackendProtocol
from langchain_core.messages import AIMessage
from starlette.types import Message
from test_gateway import Model, command

from tinkerfin import TinkerFin
from tinkerfin_contracts import PreparedWorkspace, RunIdentity
from tinkerfin_gateway import Gateway, RunAcceptance
from tinkerfin_gateway.starlette import sse_response
from tinkerfin_messaging import MemoryBackend, Messaging
from tinkerfin_messaging.backend_contract import (
    MessagingTransition,
    MessagingTransitionResult,
)
from tinkerfin_notifications import Notifications
from tinkerfin_tracing import RunFact, Tracer


class WaitingWorkspace:
    """Expose explicit preparation and release signals for one owned borrow."""

    def __init__(self, *, failure: Exception | None = None) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.closed = asyncio.Event()
        self.failure = failure
        self.opens = 0

    @asynccontextmanager
    async def prepare(
        self, identity: RunIdentity
    ) -> AsyncIterator[PreparedWorkspace[None, BackendProtocol]]:
        del identity
        self.opens += 1
        self.entered.set()
        try:
            await self.release.wait()
            if self.failure is not None:
                raise self.failure
            yield PreparedWorkspace(None, StateBackend())
        finally:
            self.closed.set()


class HeldFirstAppendBackend(MemoryBackend):
    """Hold the first durable append before acknowledging its storage result."""

    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def commit_messaging_transition(
        self, transition: MessagingTransition
    ) -> MessagingTransitionResult:
        if transition.kind == "append_message" and not self.entered.is_set():
            self.entered.set()
            await self.release.wait()
        return await super().commit_messaging_transition(transition)


async def test_response_headers_do_not_wait_for_workspace_preparation() -> None:
    workspace = WaitingWorkspace()
    model = Model(responses=[AIMessage(content="ready")])
    writer = Tracer()
    reader = Tracer(store=writer.store)
    confirmations: list[RunAcceptance] = []

    class Registration:
        async def confirm(self, acceptance: RunAcceptance) -> None:
            history = await reader.get(
                acceptance.identity.thread, head_run_id=acceptance.identity.run_id
            )
            assert history.head_run_id == acceptance.identity.run_id
            events = await history.events(limit=10)
            assert [
                event.fact.phase
                for event in events.items
                if isinstance(event.fact, RunFact)
            ] == ["started"]
            assert not workspace.entered.is_set()
            confirmations.append(acceptance)

        async def release(self) -> None:
            pytest.fail("Accepted registration must not be released")

    runtime = (
        TinkerFin()
        .with_namespace("tenant")
        .with_observer(writer)
        .build(model, backend=workspace)
    )
    headers_sent = asyncio.Event()
    frames: list[Message] = []

    async def receive() -> Message:
        await asyncio.Event().wait()
        raise AssertionError("The response does not consume request messages")

    async def send(message: Message) -> None:
        frames.append(message)
        if message["type"] == "http.response.start":
            headers_sent.set()

    async with Messaging() as messaging, Notifications() as notifications:
        gateway = Gateway(messaging=messaging, notifications=notifications)
        admission = asyncio.create_task(
            sse_response(
                gateway.stream(runtime, command(), registration=Registration())
            )
        )
        preparing = asyncio.create_task(workspace.entered.wait())
        serving: asyncio.Task[None] | None = None
        try:
            await asyncio.wait(
                (admission, preparing), return_when=asyncio.FIRST_COMPLETED
            )
            assert admission.done(), "Workspace preparation blocked the HTTP response"
            response = await admission
            assert [item.kind for item in confirmations] == ["new"]
            serving = asyncio.create_task(
                response(
                    {"type": "http", "asgi": {"spec_version": "2.4"}},
                    receive,
                    send,
                )
            )
            await headers_sent.wait()
            await preparing
            assert not workspace.release.is_set()
            assert model.inputs == []
        finally:
            workspace.release.set()
            response = await admission
            if serving is not None:
                await serving
            await response.aclose()
            preparing.cancel()
            await asyncio.gather(preparing, return_exceptions=True)
    assert frames[0]["type"] == "http.response.start"
    assert workspace.closed.is_set()


@pytest.mark.parametrize("outcome", ["cancel", "failure"])
async def test_start_is_committed_before_workspace_and_preparation_settles(
    outcome: str,
) -> None:
    failure = OSError("workspace unavailable") if outcome == "failure" else None
    workspace = WaitingWorkspace(failure=failure)
    backend = HeldFirstAppendBackend()
    model = Model(responses=[AIMessage(content="unused")])
    runtime = TinkerFin().with_namespace("tenant").build(model, backend=workspace)
    async with (
        Messaging(backend=backend) as messaging,
        Notifications() as notifications,
    ):
        gateway = Gateway(messaging=messaging, notifications=notifications)
        admission = asyncio.create_task(gateway.stream(runtime, command()))
        appending = asyncio.create_task(backend.entered.wait())
        preparing = asyncio.create_task(workspace.entered.wait())
        try:
            await asyncio.wait(
                (appending, preparing), return_when=asyncio.FIRST_COMPLETED
            )
            assert appending.done(), (
                "Workspace preparation preceded durable RUN_STARTED"
            )
            assert not workspace.entered.is_set()
            backend.release.set()
            subscription = await admission
            async with subscription:
                replies = aiter(subscription)
                first = await anext(replies)
                assert isinstance(first.data, RunStartedEvent)
                await preparing
                assert not workspace.release.is_set()
                assert model.inputs == []
                if outcome == "cancel":
                    run = gateway.run(runtime.run_identity("thread", "run"))
                    assert await run.cancel()
                else:
                    workspace.release.set()
                remaining = [message.data async for message in replies]
            assert len(remaining) == 1
            assert isinstance(remaining[0], RunErrorEvent)
            assert remaining[0].code == (
                "cancelled" if outcome == "cancel" else "runtime_initialization_error"
            )
            assert workspace.closed.is_set()
            assert model.inputs == []
        finally:
            backend.release.set()
            workspace.release.set()
            subscription = await admission
            await subscription.aclose()
            for waiting in (appending, preparing):
                waiting.cancel()
            await asyncio.gather(appending, preparing, return_exceptions=True)
    assert workspace.opens == 1

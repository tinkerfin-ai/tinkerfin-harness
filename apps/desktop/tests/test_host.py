"""云端工具审批与本机执行的重连、恢复和取消边界"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
from collections.abc import Callable, Sequence
from contextlib import aclosing
from typing import Any
from uuid import uuid4

import httpx
import pytest
from ag_ui.core.types import ResumeEntry
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from langchain_core.tools import BaseTool
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import create_async_engine
from websockets.asyncio.client import process_exception
from websockets.asyncio.server import ServerConnection, serve
from websockets.datastructures import Headers
from websockets.exceptions import (
    ConnectionClosed,
    ConnectionClosedError,
    InvalidMessage,
    InvalidStatus,
)
from websockets.frames import Close
from websockets.http11 import Request, Response

from tinkerfin import AgUiResumeRequest, TinkerFin
from tinkerfin_contracts.media import Attachment
from tinkerfin_desktop import host as host_module
from tinkerfin_desktop.cloud import CloudError
from tinkerfin_desktop.configuration import CompanionConfiguration
from tinkerfin_desktop.host import CompanionHost, ConnectionState
from tinkerfin_remote import RemoteTools


class Model(FakeMessagesListChatModel):
    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | Callable[..., Any] | BaseTool],
        **kwargs: Any,
    ) -> Model:
        return self


async def until(signal: asyncio.Event, *owners: asyncio.Task) -> None:
    waiting = asyncio.create_task(signal.wait())
    try:
        done, _ = await asyncio.wait(
            (waiting, *owners), return_when=asyncio.FIRST_COMPLETED
        )
        if not signal.is_set():
            for owner in done:
                owner.result()
            pytest.fail("执行结束但未提交预期信号")
    finally:
        waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)


async def test_cloud_approval_survives_companion_restart_without_local_model(
    tmp_path, native_sandbox, tls_context, monkeypatch
):
    device_id, authorization, project_id = str(uuid4()), str(uuid4()), str(uuid4())
    configuration = CompanionConfiguration(
        server="https://localhost",
        device_id=device_id,
        namespace="ns_1",
        credential=SecretStr("ab" * 32),
        workspace_root=tmp_path / "projects",
    )
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'cloud.sqlite'}")
    observed: list[httpx.Request] = []
    output = tmp_path / "report.txt"
    source = tmp_path / "source.txt"
    source.write_text("读取后仍可恢复审批", encoding="utf-8")
    attachment = Attachment(
        id="f" * 32, name="report.txt", mime_type="text/plain", size_bytes=4
    )
    delivered: list[tuple[str, bytes]] = []

    async def respond(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT" and request.url.path == "/api/devices/presence":
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "message": "success",
                    "data": json.loads(request.content)["capabilities"],
                },
            )
        observed.append(request)
        if request.method == "POST":
            assert request.url.path == f"/api/devices/executions/{authorization}/files"
            assert (
                request.url.params["thread_id"] == "cloud"
                and request.url.params["run_id"] == "resumed"
            )
            assert request.url.params["name"] == attachment.name
            invocation_id = request.url.params["invocation_id"]
            assert invocation_id
            delivered.append((invocation_id, request.content))
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "message": "success",
                    "data": attachment.model_dump(mode="json"),
                },
            )
        assert (
            request.url.path == f"/api/devices/executions/{authorization}/configuration"
        )
        assert request.url.params["thread_id"] == "cloud"
        return httpx.Response(
            200,
            json={
                "code": 0,
                "message": "success",
                "data": {
                    "authorization": authorization,
                    "workspace_id": project_id,
                    "skills": [],
                },
            },
        )

    monkeypatch.setattr(
        httpx, "AsyncHTTPTransport", lambda **_kwargs: httpx.MockTransport(respond)
    )
    model = Model(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "source",
                        "name": "read_file",
                        "args": {"file_path": str(source)},
                    }
                ],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "write",
                        "name": "execute",
                        "args": {
                            "command": f"printf once >> {shlex.quote(str(output))}"
                        },
                    }
                ],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "deliver",
                        "name": "deliver_file",
                        "args": {"file_path": str(output), "name": attachment.name},
                    }
                ],
            ),
            AIMessage(content="完成"),
        ]
    )
    states: list[ConnectionState] = []

    async def report(state: ConnectionState) -> None:
        states.append(state)

    try:
        async with RemoteTools(engine) as remote:
            endpoint = remote.endpoint(namespace="ns_1", target=device_id)

            async def broker(socket: ServerConnection) -> None:
                await socket.recv()
                await socket.send(
                    json.dumps(
                        {
                            "device_id": device_id,
                            "namespace": "ns_1",
                            "name": "Mac",
                            "connection_id": str(uuid4()),
                        }
                    )
                )
                try:

                    async def receive() -> str:
                        value = await socket.recv()
                        assert isinstance(value, str)
                        return value

                    await endpoint.serve(send=socket.send, receive=receive)
                except ConnectionClosed:
                    pass

            async with serve(broker, "127.0.0.1", 0, ssl=tls_context) as listener:
                port = next(iter(listener.sockets)).getsockname()[1]
                configuration = configuration.model_copy(
                    update={"server": f"https://localhost:{port}"}
                )
                configured = TinkerFin(checkpointer=InMemorySaver()).with_namespace(
                    "ns_1"
                )
                pending = None
                for run_id in ("request", "resumed"):
                    runtime = configured.build(
                        model,
                        backend=endpoint.workspace(authorization=authorization),
                        interrupt_on={"execute": True},
                    )
                    async with CompanionHost(
                        configuration,
                        state_directory=tmp_path / "state",
                        sandbox_executable=native_sandbox,
                        report=report,
                    ) as host:
                        delivery = asyncio.create_task(host.connect_once())
                        reading = None
                        try:
                            if pending is None:
                                stream = runtime.open_agui_run(
                                    thread_id="cloud",
                                    run_id=run_id,
                                    messages=[
                                        {
                                            "id": "user",
                                            "role": "user",
                                            "content": "写入报告",
                                        }
                                    ],
                                )
                            else:
                                stream = runtime.open_agui_run(
                                    thread_id="cloud",
                                    run_id=run_id,
                                    resume=AgUiResumeRequest(
                                        entries=tuple(
                                            ResumeEntry(
                                                interrupt_id=entry["id"],
                                                status="resolved",
                                                payload={"type": "approve"},
                                            )
                                            for entry in pending
                                        )
                                    ),
                                )

                            async def collect():
                                async with aclosing(stream):
                                    return [
                                        event.model_dump(mode="json", by_alias=True)
                                        async for event in stream
                                    ]

                            reading = asyncio.create_task(collect())
                            done, _ = await asyncio.wait(
                                (reading, delivery), return_when=asyncio.FIRST_COMPLETED
                            )
                            if reading not in done:
                                delivery.result()
                                pytest.fail("工具结束前连接意外退出")
                            events = reading.result()
                            assert not any(
                                event["type"] == "RUN_ERROR" for event in events
                            ), events
                            terminal = [
                                event
                                for event in events
                                if event["type"] == "RUN_FINISHED"
                            ]
                            assert len(terminal) == 1
                            if pending is None:
                                assert terminal[0]["outcome"]["type"] == "interrupt"
                                pending = terminal[0]["outcome"]["interrupts"]
                                assert not output.exists()
                                assert "读取后仍可恢复审批" in json.dumps(
                                    events, ensure_ascii=False
                                )
                            else:
                                assert terminal[0]["outcome"]["type"] == "success"
                                assert output.read_text() == "once"
                                assert (
                                    len(delivered) == 1 and delivered[0][1] == b"once"
                                )
                                assert attachment.id in json.dumps(events)
                            await host.idle()
                        finally:
                            delivery.cancel()
                            if reading is not None:
                                reading.cancel()
                                await asyncio.gather(reading, return_exceptions=True)
                            await asyncio.gather(delivery, return_exceptions=True)
                assert {request.url.params["run_id"] for request in observed} == {
                    "request",
                    "resumed",
                }
                assert not (
                    tmp_path / "state" / device_id / "checkpoints.sqlite"
                ).exists()
                assert (tmp_path / "state" / device_id / "executions.sqlite").is_file()
    finally:
        await engine.dispose()


async def test_handshake_eof_keeps_the_running_tool_and_does_not_repeat_effects(
    tmp_path, native_sandbox, monkeypatch
):
    device_id, authorization, project_id = str(uuid4()), str(uuid4()), str(uuid4())
    configuration = CompanionConfiguration(
        server="http://127.0.0.1",
        device_id=device_id,
        namespace="ns_1",
        credential=SecretStr("ab" * 32),
        workspace_root=tmp_path / "projects",
    )
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'cloud.sqlite'}")
    output, finished = tmp_path / "once.txt", tmp_path / "finished.txt"
    ready_path, release_path = tmp_path / "ready", tmp_path / "release"
    os.mkfifo(ready_path)
    os.mkfifo(release_path)
    ready_fd = os.open(ready_path, os.O_RDWR | os.O_NONBLOCK)
    release_fd = os.open(release_path, os.O_RDWR | os.O_NONBLOCK)
    ready = asyncio.Event()
    loop = asyncio.get_running_loop()

    def started() -> None:
        os.read(ready_fd, 4096)
        ready.set()

    loop.add_reader(ready_fd, started)
    command = f"printf once >> {shlex.quote(str(output))}; printf ready > {shlex.quote(str(ready_path))}; read value < {shlex.quote(str(release_path))}; printf finished > {shlex.quote(str(finished))}"
    model = Model(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {"id": "write", "name": "execute", "args": {"command": command}}
                ],
            ),
            AIMessage(content="完成"),
        ]
    )
    openings: list[str] = []

    async def respond(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT" and request.url.path == "/api/devices/presence":
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "message": "success",
                    "data": json.loads(request.content)["capabilities"],
                },
            )
        assert (
            request.url.path == f"/api/devices/executions/{authorization}/configuration"
        )
        openings.append(request.url.params["run_id"])
        return httpx.Response(
            200,
            json={
                "code": 0,
                "message": "success",
                "data": {
                    "authorization": authorization,
                    "workspace_id": project_id,
                    "skills": [],
                },
            },
        )

    monkeypatch.setattr(
        httpx, "AsyncHTTPTransport", lambda **_kwargs: httpx.MockTransport(respond)
    )
    retry_entered = (asyncio.Event(), asyncio.Event())
    retry_allowed = (asyncio.Event(), asyncio.Event())
    retries = 0

    async def retry_delay(_delay: float) -> None:
        nonlocal retries
        index = retries
        retries += 1
        assert index < len(retry_entered)
        retry_entered[index].set()
        await retry_allowed[index].wait()

    monkeypatch.setattr(host_module, "sleep", retry_delay)
    classified: list[Exception] = []

    def classify(error: Exception) -> Exception | None:
        classified.append(error)
        return process_exception(error)

    monkeypatch.setattr(host_module, "process_exception", classify)
    handshakes = 0
    sockets: list[ServerConnection] = []
    reconnected = asyncio.Event()
    detached = asyncio.Event()

    def handshake(socket: ServerConnection, _request: Request) -> Response | None:
        nonlocal handshakes
        handshakes += 1
        if handshakes == 2:
            socket.transport.abort()
            return socket.respond(503, "Synthetic handshake interruption")
        return None

    states: list[ConnectionState] = []

    async def report(state: ConnectionState) -> None:
        states.append(state)

    try:
        async with RemoteTools(engine) as remote:
            endpoint = remote.endpoint(namespace="ns_1", target=device_id)

            async def broker(socket: ServerConnection) -> None:
                sockets.append(socket)
                await socket.recv()
                await socket.send(
                    json.dumps(
                        {
                            "device_id": device_id,
                            "namespace": "ns_1",
                            "name": "Mac",
                            "connection_id": str(uuid4()),
                        }
                    )
                )
                if len(sockets) == 2:
                    reconnected.set()
                try:

                    async def receive() -> str:
                        value = await socket.recv()
                        assert isinstance(value, str)
                        return value

                    await endpoint.serve(send=socket.send, receive=receive)
                except ConnectionClosed:
                    pass
                finally:
                    if socket is sockets[0]:
                        detached.set()

            async with serve(
                broker, "127.0.0.1", 0, process_request=handshake
            ) as listener:
                configuration = configuration.model_copy(
                    update={
                        "server": f"http://127.0.0.1:{next(iter(listener.sockets)).getsockname()[1]}"
                    }
                )
                runtime = (
                    TinkerFin()
                    .with_namespace("ns_1")
                    .build(
                        model, backend=endpoint.workspace(authorization=authorization)
                    )
                )

                async def run_cloud():
                    async with aclosing(
                        runtime.open_agui_run(
                            thread_id="cloud",
                            run_id="request",
                            messages=[
                                {
                                    "id": "user",
                                    "role": "user",
                                    "content": "写入一次报告",
                                }
                            ],
                        )
                    ) as stream:
                        return [
                            event.model_dump(mode="json", by_alias=True)
                            async for event in stream
                        ]

                async with CompanionHost(
                    configuration,
                    state_directory=tmp_path / "state",
                    sandbox_executable=native_sandbox,
                    report=report,
                ) as host:
                    delivery = asyncio.create_task(host.run())
                    execution = asyncio.create_task(run_cloud())
                    try:
                        await until(ready, delivery, execution)
                        assert output.read_text() == "once" and not finished.exists()
                        await sockets[0].close()
                        await until(retry_entered[0], delivery, execution)
                        await until(detached, delivery, execution)
                        retry_allowed[0].set()
                        await until(retry_entered[1], delivery, execution)
                        assert openings == ["request"]
                        assert output.read_text() == "once" and not finished.exists()
                        retry_allowed[1].set()
                        await until(reconnected, delivery, execution)
                        os.write(release_fd, b"complete\n")
                        done, _ = await asyncio.wait(
                            (execution, delivery), return_when=asyncio.FIRST_COMPLETED
                        )
                        if execution not in done:
                            delivery.result()
                            pytest.fail("工具结束前重连循环意外退出")
                        events = execution.result()
                        assert not any(
                            event["type"] == "RUN_ERROR" for event in events
                        ), events
                        assert (
                            len(
                                [
                                    event
                                    for event in events
                                    if event["type"] == "RUN_FINISHED"
                                ]
                            )
                            == 1
                        )
                        await host.idle()
                        assert (
                            output.read_text() == "once"
                            and finished.read_text() == "finished"
                        )
                        assert openings == ["request"]
                        assert len(classified) == 1 and isinstance(
                            classified[0], InvalidMessage
                        )
                        assert isinstance(classified[0].__cause__, EOFError)
                        assert states == [
                            "connecting",
                            "online",
                            "offline",
                            "connecting",
                            "offline",
                            "connecting",
                            "online",
                        ]
                    finally:
                        os.write(release_fd, b"cleanup\n")
                        for gate in retry_allowed:
                            gate.set()
                        execution.cancel()
                        delivery.cancel()
                        await asyncio.gather(
                            execution, delivery, return_exceptions=True
                        )
    finally:
        loop.remove_reader(ready_fd)
        os.close(ready_fd)
        os.close(release_fd)
        await engine.dispose()


@pytest.mark.parametrize(
    "error",
    [
        CloudError(401),
        CloudError(403),
        InvalidStatus(Response(401, "Unauthorized", Headers())),
        InvalidStatus(Response(403, "Forbidden", Headers())),
        ConnectionClosedError(Close(1008, "revoked"), None),
        InvalidMessage("invalid response"),
        RuntimeError("unexpected worker failure"),
    ],
)
async def test_reconnect_classification_retains_authentication_and_fatal_errors(
    tmp_path,
    monkeypatch,
    error: Exception,
):
    states: list[ConnectionState] = []

    async def report(state: ConnectionState) -> None:
        states.append(state)

    host = CompanionHost(
        CompanionConfiguration(
            server="http://127.0.0.1",
            device_id=str(uuid4()),
            namespace="ns_1",
            credential=SecretStr("ab" * 32),
            workspace_root=tmp_path / "projects",
        ),
        state_directory=tmp_path / "state",
        sandbox_executable=tmp_path / "unused",
        report=report,
    )

    async def fail() -> None:
        raise error

    async def unexpected_retry(_delay: float) -> None:
        pytest.fail("认证拒绝或协议错误不能进入重连等待")

    monkeypatch.setattr(host, "connect_once", fail)
    monkeypatch.setattr(host_module, "sleep", unexpected_retry)
    try:
        if isinstance(error, CloudError | InvalidStatus | ConnectionClosedError):
            await host.run()
            assert states == ["unauthorized"]
        else:
            with pytest.raises(type(error)) as failure:
                await host.run()
            assert failure.value is error and states == []
    finally:
        await host.aclose()


async def test_temporary_http_handshake_failure_retries_without_swallowing_cancellation(
    tmp_path, monkeypatch
):
    states: list[ConnectionState] = []
    attempts = 0
    retries = 0

    async def report(state: ConnectionState) -> None:
        states.append(state)

    host = CompanionHost(
        CompanionConfiguration(
            server="http://127.0.0.1",
            device_id=str(uuid4()),
            namespace="ns_1",
            credential=SecretStr("ab" * 32),
            workspace_root=tmp_path / "projects",
        ),
        state_directory=tmp_path / "state",
        sandbox_executable=tmp_path / "unused",
        report=report,
    )

    async def attempt() -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise InvalidStatus(Response(503, "Unavailable", Headers()))
        raise asyncio.CancelledError

    async def retry(_delay: float) -> None:
        nonlocal retries
        retries += 1

    monkeypatch.setattr(host, "connect_once", attempt)
    monkeypatch.setattr(host_module, "sleep", retry)
    try:
        with pytest.raises(asyncio.CancelledError):
            await host.run()
        assert attempts == 2 and retries == 1 and states == ["offline"]
    finally:
        await host.aclose()

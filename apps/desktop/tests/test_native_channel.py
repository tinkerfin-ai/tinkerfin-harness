"""原生授权管道的身份、串行容量、迟到答复和取消所有权"""

import asyncio
import socket

import pytest
from pydantic import ValidationError

from tinkerfin_desktop.desktop import desktop_tools
from tinkerfin_desktop.native import (
    DesktopCommand,
    DesktopReply,
    NativeChannel,
    NativeReadiness,
    OpenURLCommand,
    ReadinessChanged,
    StdioChannel,
    Stop,
    incoming,
    native_tools,
)


async def test_native_reply_is_bound_to_one_pending_request_and_workspace_scope():
    sent = []
    entered = asyncio.Event()

    async def send(payload):
        sent.append(payload)
        entered.set()

    channel = NativeChannel(
        send, readiness=NativeReadiness(accessibility=False, screen_capture=False)
    )
    assert desktop_tools(None, "a" * 64) == ()
    tools = desktop_tools(channel, "a" * 64)
    operation = asyncio.create_task(tools[1].ainvoke({"application": "test.editor"}))
    try:
        await entered.wait()
        request = sent[0]
        assert request["workspace_scope_id"] == "a" * 64
        assert request["command"]["application"] == "test.editor"
        assert request["command"]["include_image"] is False
        channel.receive(
            DesktopReply(
                kind="desktop_result", request_id="0" * 32, result={"wrong": True}
            )
        )
        assert not operation.done()
        with pytest.raises(ValueError, match="顺序操作"):
            await channel.request("b" * 64, DesktopCommand(action="apps"))
        reply = DesktopReply(
            kind="desktop_result",
            request_id=request["request_id"],
            result={"title": "approved"},
        )
        channel.receive(reply)
        channel.receive(reply)
        blocks = await operation
        assert blocks[0]["type"] == "text" and "approved" in blocks[0]["text"]
        assert len(sent) == 1
    finally:
        channel.close()
        operation.cancel()
        await asyncio.gather(operation, return_exceptions=True)


async def test_repeated_cancellation_settles_withdrawal_before_accepting_another_request():
    sent = []
    entered, withdrawing, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def send(payload):
        sent.append(payload)
        if payload["kind"] == "desktop_request":
            entered.set()
        else:
            withdrawing.set()
            await release.wait()

    channel = NativeChannel(
        send, readiness=NativeReadiness(accessibility=False, screen_capture=False)
    )
    operation = asyncio.create_task(
        channel.request("a" * 64, DesktopCommand(action="apps"))
    )
    try:
        await entered.wait()
        operation.cancel()
        await withdrawing.wait()
        operation.cancel()
        with pytest.raises(ValueError, match="顺序操作"):
            await channel.request("b" * 64, DesktopCommand(action="apps"))
        channel.receive(
            DesktopReply(
                kind="desktop_result",
                request_id=sent[0]["request_id"],
                result={"late": True},
            )
        )
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await operation
        assert [item["kind"] for item in sent] == ["desktop_request", "desktop_cancel"]
        assert sent[0]["request_id"] == sent[1]["request_id"]
    finally:
        release.set()
        channel.close()
        operation.cancel()
        await asyncio.gather(operation, return_exceptions=True)


async def test_native_disconnect_cancels_pending_tools_without_writing_to_a_closed_pipe():
    sent = []
    entered = asyncio.Event()

    async def send(payload):
        sent.append(payload)
        entered.set()

    channel = NativeChannel(
        send, readiness=NativeReadiness(accessibility=False, screen_capture=False)
    )
    operation = asyncio.create_task(
        channel.request("a" * 64, DesktopCommand(action="apps"))
    )
    await entered.wait()
    channel.close()
    with pytest.raises(asyncio.CancelledError):
        await operation
    assert len(sent) == 1
    with pytest.raises(RuntimeError, match="已关闭"):
        await channel.request("a" * 64, DesktopCommand(action="apps"))


async def test_stdio_messages_keep_line_boundaries_and_reject_truncated_or_unknown_input():
    left, right = socket.socketpair()
    local_reader, local_writer = await asyncio.open_connection(
        sock=left, limit=4 * 1024 * 1024
    )
    remote_reader, remote_writer = await asyncio.open_connection(
        sock=right, limit=4 * 1024 * 1024
    )
    channel = StdioChannel(local_reader, local_writer)
    try:
        await asyncio.gather(
            channel.send({"kind": "state", "state": "online"}),
            channel.send({"kind": "state", "state": "offline"}),
        )
        lines = {await remote_reader.readline(), await remote_reader.readline()}
        assert lines == {
            b'{"kind":"state","state":"online"}\n',
            b'{"kind":"state","state":"offline"}\n',
        }
        remote_writer.write(b'{"kind":"stop"}\n')
        await remote_writer.drain()
        assert isinstance(await channel.receive(), Stop)
        remote_writer.write(b'{"kind":"stop"}')
        await remote_writer.drain()
        remote_writer.write_eof()
        with pytest.raises(ValueError, match="不完整"):
            await channel.receive()
        with pytest.raises(ValidationError):
            incoming.validate_json('{"kind":"execute","command":"unapproved"}')
    finally:
        for writer in (local_writer, remote_writer):
            writer.transport.abort()
            await writer.wait_closed()


async def test_open_url_uses_native_receipt_without_accessibility_or_screen_permission():
    sent = []
    channel: NativeChannel

    async def send(payload):
        sent.append(payload)
        channel.receive(
            DesktopReply(
                kind="desktop_result",
                request_id=payload["request_id"],
                result={"accepted": True},
            )
        )

    channel = NativeChannel(
        send, readiness=NativeReadiness(accessibility=False, screen_capture=False)
    )
    try:
        selected = native_tools(channel, "authorized-workspace")
        assert native_tools(None, "authorized-workspace") == ()
        assert len(selected) == 1 and selected[0].name == "open_url"
        assert await selected[0].ainvoke({"url": "http://localhost:3000/page"}) == {
            "accepted": True
        }
        assert sent[0]["workspace_scope_id"] == "authorized-workspace"
        assert sent[0]["command"] == {
            "action": "open_url",
            "url": "http://localhost:3000/page",
            "include_image": False,
        }
        updated = NativeReadiness(accessibility=True, screen_capture=False)
        assert channel.update_readiness(updated)
        assert not channel.update_readiness(updated)
        assert channel.readiness == updated
        message = incoming.validate_python(
            {
                "kind": "native_readiness",
                "readiness": {"accessibility": True, "screen_capture": False},
            }
        )
        assert isinstance(message, ReadinessChanged) and message.readiness == updated
    finally:
        channel.close()


@pytest.mark.parametrize(
    "url",
    [
        "file:///tmp/report",
        "javascript:alert(1)",
        "tinkerfin://workspace",
        "https:///example.test",
        "https://user:secret@example.test",
        "https://@example.test",
        "https://example.test\n",
        " https://example.test",
        "https://example.test:0",
        "https://example.test:65536",
    ],
)
def test_open_url_refuses_non_web_targets_and_embedded_credentials(url):
    with pytest.raises(ValidationError):
        OpenURLCommand(url=url)

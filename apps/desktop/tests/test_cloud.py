"""执行授权、附件原件与设备凭证的 HTTP 边界"""

import base64
import json
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr, ValidationError
from websockets.asyncio.server import ServerConnection, serve

from tinkerfin_contracts import RunIdentity
from tinkerfin_contracts.media import Attachment
from tinkerfin_desktop.cloud import CloudError, DeviceCapabilities, StudioClient
from tinkerfin_desktop.configuration import (
    CompanionConfiguration,
    ExecutionConfiguration,
)
from tinkerfin_remote import WorkspaceRequest


def configuration(directory: Path) -> CompanionConfiguration:
    return CompanionConfiguration(
        server="https://studio.test/prefix",
        device_id=str(uuid4()),
        namespace="ns_1",
        credential=SecretStr("ab" * 32),
        workspace_root=directory / "projects",
    )


def execution_request(
    identity: CompanionConfiguration,
    *,
    authorization: str | None = None,
    target: str | None = None,
    namespace: str | None = None,
    scope: str = "c" * 64,
    run_id: str = "run",
) -> WorkspaceRequest:
    return WorkspaceRequest(
        identity=RunIdentity(
            namespace=namespace or identity.namespace, thread_id="cloud", run_id=run_id
        ),
        target=target or identity.device_id,
        authorization=authorization or str(uuid4()),
        workspace_scope_id=scope,
        session_id="d" * 64,
    )


def execution_configuration(authorization: str) -> dict:
    return {
        "authorization": authorization,
        "workspace_id": str(uuid4()),
        "skills": [
            {
                "installation_id": "skill",
                "name": "reports",
                "digest": "b" * 64,
                "selected": True,
            }
        ],
    }


async def test_configuration_is_bound_to_device_namespace_and_execution(tmp_path):
    identity = configuration(tmp_path)
    request = execution_request(identity)
    selected = execution_configuration(request.authorization)
    observed: list[httpx.Request] = []

    async def respond(call: httpx.Request) -> httpx.Response:
        observed.append(call)
        assert (
            call.url.path
            == f"/prefix/api/devices/executions/{request.authorization}/configuration"
        )
        assert dict(call.url.params) == {"thread_id": "cloud", "run_id": "run"}
        assert call.headers["Authorization"] == identity.authorization
        return httpx.Response(
            200, json={"code": 0, "message": "success", "data": selected}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        cloud = StudioClient(identity, client)
        current = await cloud.configuration_for(request)
        assert current == ExecutionConfiguration.model_validate(selected)
        for forged in (
            execution_request(identity, target=str(uuid4())),
            execution_request(identity, namespace="ns_2"),
            execution_request(identity, authorization="../configuration"),
        ):
            with pytest.raises(ValueError):
                await cloud.configuration_for(forged)
        assert len(observed) == 1
        selected["authorization"] = str(uuid4())
        with pytest.raises(ValueError, match="不一致"):
            await cloud.configuration_for(request)
    assert identity.credential.get_secret_value() not in identity.model_dump_json()
    with pytest.raises(ValidationError):
        ExecutionConfiguration.model_validate({**selected, "model": {}})


async def test_assets_use_original_bytes_and_stable_invocation_identity(tmp_path):
    identity = configuration(tmp_path)
    request = execution_request(identity)
    content = b"%PDF-1.4\n\xff\x00\xe2\xe3\xcf\xd3\n"
    attachment = Attachment(
        id="e" * 32,
        name="报告.pdf",
        mime_type="application/pdf",
        size_bytes=len(content),
    )
    observed: list[httpx.Request] = []

    async def respond(call: httpx.Request) -> httpx.Response:
        observed.append(call)
        assert call.headers["Authorization"] == identity.authorization
        assert (
            call.url.params["thread_id"] == "cloud"
            and call.url.params["run_id"] == "run"
        )
        if call.method == "POST":
            assert call.content == content
            assert call.url.params["invocation_id"] == "a" * 64
            assert call.url.params["name"] == attachment.name
            data = attachment.model_dump(mode="json")
        elif call.url.path.endswith("/" + attachment.id):
            assert "variant" not in call.url.params
            data = {
                "mime_type": attachment.mime_type,
                "content": base64.b64encode(content).decode(),
            }
        else:
            data = [attachment.model_dump(mode="json")]
        return httpx.Response(200, json={"code": 0, "message": "success", "data": data})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        cloud = StudioClient(identity, client)
        assert await cloud.attachments(request) == [attachment]
        assert await cloud.read(request, attachment) == content
        assert (
            await cloud.deliver(request, "a" * 64, attachment.name, content)
            == attachment
        )
        with pytest.raises(ValueError):
            await cloud.deliver(request, "a" * 64, attachment.name, b"")
    assert len(observed) == 3


async def test_configuration_does_not_follow_redirects_or_accept_unbounded_bodies(
    tmp_path,
):
    identity = configuration(tmp_path)
    request = execution_request(identity)
    calls: list[str] = []

    async def redirect(call: httpx.Request) -> httpx.Response:
        calls.append(call.url.host)
        return httpx.Response(302, headers={"location": "https://foreign.test"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(redirect), follow_redirects=True
    ) as client:
        with pytest.raises(CloudError) as failure:
            await StudioClient(identity, client).configuration_for(request)
        assert failure.value.status == 302
    assert calls == ["studio.test"]
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, content=b"x" * (128 * 1024 + 1))
        )
    ) as client:
        with pytest.raises(CloudError):
            await StudioClient(identity, client).configuration_for(request)


@pytest.mark.parametrize(
    "server",
    [
        "http://public.test",
        "https://user:password@studio.test",
        "https://studio.test?token=secret",
        "file:///tmp/studio",
    ],
)
def test_credentials_cannot_be_bound_to_an_insecure_or_ambiguous_server(
    tmp_path, server
):
    identity = configuration(tmp_path)
    with pytest.raises(ValidationError):
        CompanionConfiguration.model_validate(
            {
                **identity.model_dump(),
                "credential": identity.credential,
                "server": server,
            }
        )


async def test_current_capabilities_update_over_http_without_replacing_tool_socket(
    tmp_path,
):
    connection_id = str(uuid4())
    initial = DeviceCapabilities(browser=True, open_url=True)
    updated = DeviceCapabilities(
        browser=True, desktop=True, desktop_capture=False, open_url=True
    )
    observed = []
    identity = configuration(tmp_path)

    async def broker(socket: ServerConnection) -> None:
        assert json.loads(await socket.recv()) == {
            "capabilities": initial.model_dump(mode="json")
        }
        await socket.send(
            json.dumps(
                {
                    "device_id": identity.device_id,
                    "namespace": identity.namespace,
                    "name": "Test Mac",
                    "connection_id": connection_id,
                }
            )
        )
        async for message in socket:
            await socket.send(message)

    async def respond(request: httpx.Request) -> httpx.Response:
        observed.append(request)
        assert request.method == "PUT" and request.url.path == "/api/devices/presence"
        assert request.headers["Authorization"] == identity.authorization
        assert request.headers["Content-Type"] == "application/json"
        assert json.loads(request.content) == {
            "connection_id": connection_id,
            "capabilities": updated.model_dump(mode="json"),
        }
        return httpx.Response(
            200,
            json={
                "code": 0,
                "message": "success",
                "data": updated.model_dump(mode="json"),
            },
        )

    async with serve(broker, "127.0.0.1", 0) as server:
        port = next(iter(server.sockets)).getsockname()[1]
        identity = identity.model_copy(update={"server": f"http://127.0.0.1:{port}"})
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            cloud = StudioClient(identity, client)
            assert not await cloud.update_capabilities(updated)
            async with cloud.connect(initial) as socket:
                with pytest.raises(RuntimeError, match="一条连接"):
                    async with cloud.connect(initial):
                        pytest.fail("同一客户端不能同时拥有两条连接")
                assert await cloud.update_capabilities(updated)
                await socket.send("tool-transport-still-owned")
                assert await socket.recv() == "tool-transport-still-owned"
            assert not await cloud.update_capabilities(updated)
    assert len(observed) == 1

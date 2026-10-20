"""实际宿主装配中的授权技能读取与输入目录隔离"""

import asyncio
import shlex
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from test_assets import skill
from test_cloud import configuration

from tinkerfin_contracts import (
    RunIdentity,
    ToolExecutionScope,
    bind_workspace_operation,
)
from tinkerfin_desktop import host as host_module
from tinkerfin_desktop.configuration import ExecutionConfiguration
from tinkerfin_remote import RemoteTools


@pytest.mark.parametrize("network", ["normal", "restricted"])
async def test_host_exposes_only_authorized_read_only_skill_inputs(
    tmp_path, native_sandbox, monkeypatch, network
):
    payload, reference = skill()
    config = configuration(tmp_path)
    config = config.model_copy(
        update={
            "permissions": config.permissions.model_copy(update={"network": network})
        }
    )
    authorization, without_skills, project = (str(uuid4()) for _ in range(3))
    to_cloud, to_worker = asyncio.Queue(64), asyncio.Queue(64)

    class Socket:
        async def recv(self):
            return await to_worker.get()

        async def send(self, value):
            await to_cloud.put(value)

    class Cloud:
        def __init__(self, *_args):
            pass

        async def configuration_for(self, request):
            return ExecutionConfiguration(
                authorization=request.authorization,
                workspace_id=project,
                skills=() if request.authorization == without_skills else (reference,),
            )

        async def skill(self, _request, installation_id):
            assert installation_id == reference.installation_id
            return payload

        @asynccontextmanager
        async def connect(self, _capabilities):
            yield Socket()

        async def update_capabilities(self, _capabilities):
            return True

    async def report(_state):
        pass

    async def emit(_value):
        pass

    monkeypatch.setattr(host_module, "StudioClient", Cloud)
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'cloud.sqlite'}")
    async with AsyncExitStack() as stack:
        stack.push_async_callback(engine.dispose)
        remote = await stack.enter_async_context(RemoteTools(engine))
        endpoint = remote.endpoint(namespace=config.namespace, target=config.device_id)
        host = await stack.enter_async_context(
            host_module.CompanionHost(
                config,
                state_directory=tmp_path / "state",
                sandbox_executable=native_sandbox,
                report=report,
            )
        )
        delivery = asyncio.create_task(
            endpoint.serve(send=to_worker.put, receive=to_cloud.get)
        )
        connection = asyncio.create_task(host.connect_once())
        try:
            identity = RunIdentity(
                namespace=config.namespace, thread_id="skill-inputs", run_id="read"
            )
            async with endpoint.workspace(authorization=authorization).prepare(
                identity
            ) as prepared:
                assert len(prepared.skill_sources) == 1
                assert prepared.workspace_scope_id is not None
                directory = Path(prepared.skill_sources[0].directory)
                scope = ToolExecutionScope(
                    identity=identity,
                    origin_identity=identity,
                    workspace_scope_id=prepared.workspace_scope_id,
                    invocation_id="skill-read",
                    graph_namespace=(),
                    purpose="tool",
                    tool_call_id="skill-read",
                )
                with bind_workspace_operation(scope, emit_output=emit):
                    listing = await prepared.workspace.als(
                        prepared.skill_sources[0].directory
                    )
                    assert listing.error is None and listing.entries is not None
                    assert [item["path"] for item in listing.entries] == [
                        str(directory / "reports") + "/"
                    ]
                    downloaded = await prepared.workspace.adownload_files(
                        [listing.entries[0]["path"] + "SKILL.md"]
                    )
                    assert downloaded[0].error is None
                    assert downloaded[0].content is not None
                    assert b"Read the inputs." in downloaded[0].content
                    content = await prepared.workspace.aread(
                        str(directory / "reports/SKILL.md")
                    )
                    assert content.error is None and content.file_data is not None
                    assert "Read the inputs." in content.file_data["content"]
                    result = await prepared.workspace.aexecute(
                        "python "
                        + shlex.quote(str(directory / "reports/scripts/report.py"))
                    )
                    assert result.exit_code == 0 and result.output == "report\n"
                    denied = await prepared.workspace.awrite(
                        str(directory / "reports/scripts/extra.py"), "changed"
                    )
                    assert denied.error is not None
                    future = directory.parent / "future-scope"
                    future.mkdir()
                    (future / "private.txt").write_text("other-scope-secret-value")
                    private = await prepared.workspace.aexecute(
                        "cat " + shlex.quote(str(future / "private.txt"))
                    )
                    assert private.exit_code != 0
                    assert "other-scope-secret-value" not in private.output
                other_identity = RunIdentity(
                    namespace=config.namespace, thread_id="no-skills", run_id="read"
                )
                async with endpoint.workspace(authorization=without_skills).prepare(
                    other_identity
                ) as other:
                    assert other.skill_sources == ()
                    assert other.workspace_scope_id is not None
                    other_scope = ToolExecutionScope(
                        identity=other_identity,
                        origin_identity=other_identity,
                        workspace_scope_id=other.workspace_scope_id,
                        invocation_id="unauthorized-read",
                        graph_namespace=(),
                        purpose="tool",
                        tool_call_id="unauthorized-read",
                    )
                    with bind_workspace_operation(other_scope, emit_output=emit):
                        denied_read = await other.workspace.aexecute(
                            "cat " + shlex.quote(str(directory / "reports/SKILL.md"))
                        )
                    assert denied_read.exit_code != 0
                    assert "Read the inputs." not in denied_read.output
        finally:
            for task in (connection, delivery):
                task.cancel()
            results = await asyncio.gather(connection, delivery, return_exceptions=True)
            failures = [result for result in results if isinstance(result, Exception)]
            if failures:
                raise ExceptionGroup("测试传输关闭失败", failures)

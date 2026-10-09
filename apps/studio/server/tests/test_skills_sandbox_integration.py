"""在本轮独占的真实沙箱中验证技能脚本、重建和取消归还"""

import asyncio
import shlex
from datetime import timedelta
from functools import partial
from unittest.mock import create_autospec

import anyio
import pytest
from anyio.to_thread import run_sync
from langgraph.store.memory import InMemoryStore
from opensandbox.config import ConnectionConfig
from tests.support.docker_services import (
    OpenSandboxDockerRuntime,
    OpenSandboxTestService,
)

from tinkerfin import TinkerFin
from tinkerfin_contracts import RunIdentity
from tinkerfin_sandbox import OpenSandboxClient, OpenSandboxConfig, OpenSandboxManager
from tinkerfin_studio.skills.content import SkillContentStore
from tinkerfin_studio.skills.execution import SkillsWorkspace
from tinkerfin_studio.skills.library import SkillLibrary
from tinkerfin_studio.skills.packages import SkillFile, parse_package

pytestmark = [pytest.mark.docker_integration, pytest.mark.opensandbox_e2e]


async def test_skill_scripts_resources_rebuild_and_cancel_in_owned_sandbox(
    opensandbox_test_service: OpenSandboxTestService,
    opensandbox_docker_runtime: OpenSandboxDockerRuntime,
) -> None:
    package = parse_package(
        (
            SkillFile(
                "SKILL.md",
                b"---\nname: reports\ndescription: Read report resources\n---\nRun scripts/run.py",
            ),
            SkillFile(
                "scripts/run.py",
                b"from helper import read_data\nprint(read_data().hex())\n",
            ),
            SkillFile(
                "scripts/helper.py",
                b"from pathlib import Path\ndef read_data():\n    return Path('references/data.bin').read_bytes()\n",
            ),
            SkillFile("references/data.bin", bytes(range(256))),
        )
    )
    content = SkillContentStore(TinkerFin(store=InMemoryStore()))
    await content.save(1, package)
    identity = RunIdentity(
        namespace="skills-integration", thread_id="thread", run_id="run"
    )
    client = OpenSandboxClient(
        connection_config=ConnectionConfig(
            domain=opensandbox_test_service.domain,
            api_key=opensandbox_test_service.api_key,
            use_server_proxy=True,
        ),
        config=OpenSandboxConfig(
            image=opensandbox_docker_runtime.image,
            workspace_root="/workspace",
            warm_pool_size=0,
            ttl=timedelta(minutes=20),
            metadata=opensandbox_test_service.sandbox_metadata,
            enable_capture_offload=True,
        ),
    )
    library = create_autospec(SkillLibrary, instance=True)
    library.workspace_files.return_value = tuple(
        (f"{package.name}/{file.path}", file.content) for file in package.files
    )
    sandbox_ids: list[str] = []
    async with OpenSandboxManager[str](client=client) as manager:
        workspace = SkillsWorkspace(
            manager.workspace("user", workspace_key="default"),
            library=library,
            user_id=1,
            project_id="default",
        )
        try:
            for _ in range(2):
                async with workspace.prepare(identity) as prepared:
                    backend = prepared.workspace
                    sandbox_ids.append(backend.id)
                    root = f"/skills/{package.name}"
                    result = await backend.aexecute(
                        f"cd {shlex.quote(backend.to_shell_path(root))} && python3 scripts/run.py"
                    )
                    assert result.exit_code == 0
                    assert result.output.strip() == bytes(range(256)).hex()
                    downloaded = await backend.adownload_files(
                        [root + "/references/data.bin"]
                    )
                    assert downloaded[0].content == bytes(range(256))
                    offload = await backend.aexecute_with_offload(
                        "python3 -c 'print(\"line\\n\" * 20)'",
                        "/capture.txt",
                        max_inline_bytes=8,
                    )
                    assert offload.offloaded and offload.preview_has_truncation_marker
                await manager.destroy("user")
            assert sandbox_ids[0] != sandbox_ids[1]
            entered = asyncio.Event()
            release = asyncio.Event()

            async def hold_workspace() -> None:
                async with workspace.prepare(identity):
                    entered.set()
                    await release.wait()

            task = asyncio.create_task(hold_workspace())
            task.add_done_callback(lambda _: entered.set())
            try:
                await entered.wait()
                if task.done():
                    await task
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        finally:
            await manager.destroy("user")
    owned = await run_sync(
        partial(
            opensandbox_docker_runtime.client.containers.list,
            all=True,
            filters={
                "label": [
                    f"{key}={value}"
                    for key, value in opensandbox_test_service.sandbox_metadata.items()
                ]
            },
        ),
        limiter=anyio.CapacityLimiter(1),
    )
    assert owned == []

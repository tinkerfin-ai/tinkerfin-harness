"""Read real project files without creating or borrowing an execution session."""

from datetime import timedelta

import pytest
from opensandbox.config import ConnectionConfig
from tests.support.docker_services import (
    OpenSandboxDockerRuntime,
    OpenSandboxTestService,
)

from tinkerfin_sandbox import (
    OpenSandboxClient,
    OpenSandboxConfig,
    OpenSandboxManager,
    OpenSandboxNotTextError,
    OpenSandboxPausedError,
    OpenSandboxWorkspaceNotInitializedError,
    WorkspaceChange,
)

pytestmark = [pytest.mark.docker_integration, pytest.mark.opensandbox_e2e]


async def test_read_existing_project_files_and_follow_changes(
    opensandbox_test_service: OpenSandboxTestService,
    opensandbox_docker_runtime: OpenSandboxDockerRuntime,
) -> None:
    client = OpenSandboxClient(
        connection_config=ConnectionConfig(
            domain=opensandbox_test_service.domain,
            api_key=opensandbox_test_service.api_key,
            request_timeout=timedelta(minutes=3),
            use_server_proxy=True,
            disable_metrics=True,
        ),
        config=OpenSandboxConfig(
            image=opensandbox_docker_runtime.image,
            metadata=opensandbox_test_service.sandbox_metadata,
            warm_pool_size=0,
            ttl=timedelta(minutes=20),
            resource={"cpu": "2", "memory": "4Gi"},
            command_timeout=180,
        ),
    )
    async with OpenSandboxManager(client=client) as manager:
        first = manager.workspace("owner", workspace_key="project-a")
        second = manager.workspace("owner", workspace_key="project-b")
        try:
            with pytest.raises(OpenSandboxWorkspaceNotInitializedError):
                await first.list_directory()
            assert await manager.get_details("owner") is None
            async with first.open() as files:
                uploads = await files.aupload_files(
                    [
                        ("/scripts/run.py", b"print('hello')\n"),
                        ("/scripts/large.txt", ("中" * 50_000).encode()),
                        ("/a.txt", "甲\n乙\n丙\n".encode()),
                        ("/binary.bin", b"\x00\xff"),
                    ]
                )
                assert all(item.error is None for item in uploads)
                result = await files.aexecute("ln -s /etc/passwd link.txt")
                assert result.exit_code == 0
                with pytest.raises(OpenSandboxWorkspaceNotInitializedError):
                    await second.list_directory()
                page = await first.list_directory(limit=2)
                assert [entry.path for entry in page.entries] == ["/scripts", "/a.txt"]
                assert page.next_cursor is not None
                tail = await first.list_directory(limit=2, cursor=page.next_cursor)
                assert [entry.path for entry in tail.entries] == [
                    "/binary.bin",
                    "/link.txt",
                ]
                assert tail.next_cursor is None
                text = await first.read_text("/a.txt", max_bytes=100, max_lines=2)
                assert text.text == "甲\n乙\n" and text.truncated
                prefix = await first.read_text(
                    "/scripts/large.txt", max_bytes=100 * 1024
                )
                assert prefix.text == "中" * (100 * 1024 // 3) and prefix.truncated
                assert (await first.get_file_info("/link.txt")).kind == "symlink"
                for path in ("/binary.bin", "/link.txt"):
                    with pytest.raises(OpenSandboxNotTextError):
                        await first.read_text(path, max_bytes=100)
                async with first.watch() as changes:
                    assert (await files.aedit("/a.txt", "甲", "新内容")).error is None
                    assert await anext(changes) is WorkspaceChange.FILES_CHANGED
                    changed = await first.read_text("/a.txt", max_bytes=100)
                    assert changed.text == "新内容\n乙\n丙\n"
                    assert changed.file.etag != text.file.etag
            assert (
                await first.read_text("/scripts/run.py", max_bytes=100)
            ).text == "print('hello')\n"
            await manager.pause("owner")
            with pytest.raises(OpenSandboxPausedError):
                await first.list_directory()
            await manager.resume("owner")
            await first.delete()
            with pytest.raises(OpenSandboxWorkspaceNotInitializedError):
                await first.list_directory()
        finally:
            await manager.destroy("owner")

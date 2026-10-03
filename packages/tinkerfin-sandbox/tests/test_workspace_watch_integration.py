"""Project hints through real Redis and owned Linux workspace collectors."""

from __future__ import annotations

from contextlib import AsyncExitStack
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from opensandbox.config import ConnectionConfig
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import create_async_engine
from test_workspace_watch import _manager, _prepare, _watch_world
from tests.support.docker_services import (
    OpenSandboxDockerRuntime,
    OpenSandboxTestService,
)

from tinkerfin_notifications import Notifications, ResyncRequired
from tinkerfin_notifications.redis import RedisBackend
from tinkerfin_sandbox import (
    OpenSandboxClient,
    OpenSandboxConfig,
    OpenSandboxManager,
    SQLAlchemyOpenSandboxState,
    WorkspaceChange,
)

pytestmark = [pytest.mark.docker_integration, pytest.mark.redis_e2e]


async def test_real_redis_routes_project_hints_between_independent_workers(
    tmp_path: Path, redis_url: str
) -> None:
    channel = f"test:workspace-watch:{uuid4().hex}"
    async with AsyncExitStack() as resources:
        first_client, second_client = (
            Redis.from_url(redis_url),
            Redis.from_url(redis_url),
        )
        resources.push_async_callback(first_client.aclose)
        resources.push_async_callback(second_client.aclose)
        first_notifications = await resources.enter_async_context(
            Notifications(backend=RedisBackend(first_client, channel=channel))
        )
        second_notifications = await resources.enter_async_context(
            Notifications(backend=RedisBackend(second_client, channel=channel))
        )
        async with _watch_world(tmp_path) as (world, remote):
            first_manager, _first_state = await _manager(world, first_notifications)
            second_manager, _second_state = await _manager(world, second_notifications)
            await _prepare(first_manager)
            async with first_manager.workspace(
                "owner", workspace_key="project-a"
            ).watch() as first:
                first_stream = await remote.watch_opened.get()
                async with second_manager.workspace(
                    "owner", workspace_key="project-a"
                ).watch() as second:
                    second_stream = await remote.watch_opened.get()
                    await first_stream.pending.put({"type": "changed"})
                    assert await anext(first) is WorkspaceChange.FILES_CHANGED
                    assert await anext(second) is WorkspaceChange.FILES_CHANGED
                    await second_stream.pending.put(
                        {"type": "resync", "reason": "topology"}
                    )
                    assert await anext(first) == ResyncRequired("reconnected")
                    await second_manager.aclose()
                    assert await anext(second) == ResyncRequired("disconnected")
                    with pytest.raises(StopAsyncIteration):
                        await anext(second)
                    await first_stream.pending.put({"type": "changed"})
                    assert await anext(first) is WorkspaceChange.FILES_CHANGED
            assert all(stream.closed.is_set() for stream in remote.change_streams)


@pytest.mark.opensandbox_e2e
async def test_public_watch_observes_real_files_and_ends_on_pause_and_delete(
    tmp_path: Path,
    redis_url: str,
    opensandbox_test_service: OpenSandboxTestService,
    opensandbox_docker_runtime: OpenSandboxDockerRuntime,
) -> None:
    channel = f"test:workspace-watch:{uuid4().hex}"
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'watch.db'}")
    async with AsyncExitStack() as resources:
        resources.push_async_callback(engine.dispose)
        managers: list[OpenSandboxManager[str]] = []
        for _worker in range(2):
            redis_client = Redis.from_url(redis_url)
            resources.push_async_callback(redis_client.aclose)
            notifications = await resources.enter_async_context(
                Notifications(backend=RedisBackend(redis_client, channel=channel))
            )
            manager = await resources.enter_async_context(
                OpenSandboxManager(
                    client=OpenSandboxClient(
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
                    ),
                    state=SQLAlchemyOpenSandboxState(engine=engine, namespace="watch"),
                    notifications=notifications,
                )
            )
            managers.append(manager)
        first_manager, second_manager = managers
        first = first_manager.workspace("owner", workspace_key="project-a")
        second = second_manager.workspace("owner", workspace_key="project-a")
        resources.push_async_callback(first_manager.destroy, "owner")
        async with first.open() as files:
            assert (await files.awrite("/initial.txt", "initial")).error is None
            async with first.watch() as local, second.watch() as peer:
                assert (await files.aupload_files([("/upload.txt", b"uploaded")]))[
                    0
                ].error is None
                assert await anext(local) is WorkspaceChange.FILES_CHANGED
                assert await anext(peer) is WorkspaceChange.FILES_CHANGED
                assert (
                    await files.aread_bytes("/upload.txt", max_bytes=100) == b"uploaded"
                )
            async with first.watch() as changes:
                assert (await files.awrite("/tool.txt", "first")).error is None
                assert await anext(changes) is WorkspaceChange.FILES_CHANGED
            async with first.watch() as changes:
                assert (await files.aedit("/tool.txt", "first", "second")).error is None
                assert await anext(changes) is WorkspaceChange.FILES_CHANGED
            async with first.watch() as changes:
                assert (await files.adelete("/tool.txt")).error is None
                assert await anext(changes) is WorkspaceChange.FILES_CHANGED
            mutations = (
                "printf changed > initial.txt",
                "truncate -s 0 initial.txt",
                "mv initial.txt renamed.txt",
                "rm renamed.txt",
                "mkdir sub && printf nested > sub/nested.txt",
                'python -I -S -c \'from pathlib import Path; Path("script.txt").write_text("script")\'',
            )
            for command in mutations:
                async with first.watch() as changes:
                    result = await files.aexecute(command)
                    assert result.exit_code == 0, result.output
                    change = await anext(changes)
                    assert change is WorkspaceChange.FILES_CHANGED or change == (
                        ResyncRequired("reconnected")
                    )
        async with first.watch() as changes:
            await first_manager.pause("owner")
            assert await anext(changes) == ResyncRequired("disconnected")
            with pytest.raises(StopAsyncIteration):
                await anext(changes)
        await first_manager.resume("owner")
        separate = first_manager.workspace("owner", workspace_key="project-b")
        async with separate.open() as separate_files:
            assert (await separate_files.awrite("/keep.txt", "retained")).error is None
            async with first.watch() as changes, separate.watch() as other:
                await first.delete()
                assert await anext(changes) == ResyncRequired("disconnected")
                with pytest.raises(StopAsyncIteration):
                    await anext(changes)
                assert (
                    await separate_files.awrite("/after.txt", "independent")
                ).error is None
                assert await anext(other) is WorkspaceChange.FILES_CHANGED
                assert (
                    await separate_files.aread_bytes("/keep.txt", max_bytes=100)
                ) == b"retained"

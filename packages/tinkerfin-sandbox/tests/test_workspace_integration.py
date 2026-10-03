"""Validate public project isolation through owned Server and Linux runtime images."""

from __future__ import annotations

import json
import shlex
from datetime import timedelta
from pathlib import Path

import pytest
from opensandbox.config import ConnectionConfig
from sqlalchemy.ext.asyncio import create_async_engine
from tests.support.docker_services import (
    OpenSandboxDockerRuntime,
    OpenSandboxTestService,
)

from tinkerfin_contracts import RunIdentity
from tinkerfin_sandbox import (
    OpenSandboxClient,
    OpenSandboxConfig,
    OpenSandboxManager,
    OpenSandboxPurposeError,
    SQLAlchemyOpenSandboxState,
)

pytestmark = [pytest.mark.docker_integration, pytest.mark.opensandbox_e2e]


async def test_public_project_files_network_and_deletion(
    opensandbox_test_service: OpenSandboxTestService,
    opensandbox_docker_runtime: OpenSandboxDockerRuntime,
) -> None:
    connection = ConnectionConfig(
        domain=opensandbox_test_service.domain,
        api_key=opensandbox_test_service.api_key,
        request_timeout=timedelta(minutes=3),
        use_server_proxy=True,
        disable_metrics=True,
    )
    client = OpenSandboxClient(
        connection_config=connection,
        config=OpenSandboxConfig(
            image=opensandbox_docker_runtime.image,
            metadata=opensandbox_test_service.sandbox_metadata,
            warm_pool_size=0,
            ttl=timedelta(minutes=20),
            resource={"cpu": "2", "memory": "4Gi"},
            command_timeout=180,
            enable_capture_offload=True,
        ),
    )
    async with OpenSandboxManager(client=client) as manager:
        first = manager.workspace("users/7", workspace_key="project-a")
        second = manager.workspace("users/7", workspace_key="project-b")
        async with first.open() as project_a:
            assert (
                await project_a.awrite("/.probe-user-data", "retained")
            ).error is None
        async with first.open() as project_a, second.open() as project_b:
            assert (
                await project_a.aread_bytes("/.probe-user-data", max_bytes=100)
            ) == b"retained"
            assert project_a.id == project_b.id
            physical_id = project_a.id
            with pytest.raises(OpenSandboxPurposeError):
                await manager.get("users/7")
            uploaded = await project_a.aupload_files([("/folder/input.txt", b"hello")])
            assert uploaded[0].error is None
            assert (await project_a.aexecute("cat folder/input.txt")).output == "hello"
            assert (await project_a.awrite("/note.txt", "alpha\nbeta\n")).error is None
            assert (await project_a.aedit("/note.txt", "beta", "gamma")).error is None
            read = await project_a.aread("/note.txt")
            assert read.error is None
            assert b"gamma" in await project_a.aread_bytes("/note.txt", max_bytes=100)
            large = "large edit payload " * 5000
            assert (await project_a.aedit("/note.txt", "alpha", large)).error is None
            assert (
                await project_a.aread_bytes("/note.txt", max_bytes=100_000)
            ).startswith(large.encode())
            captured = await project_a.aexecute_with_offload(
                "printf captured-output", "/capture.txt", max_inline_bytes=4
            )
            assert captured.offloaded
            assert (await project_a.adownload_files(["/capture.txt"]))[
                0
            ].content == b"captured-output"
            async with first.prepare(
                RunIdentity(
                    namespace="another-logical-scope", thread_id="thread", run_id="run"
                )
            ) as same_project:
                assert same_project.workspace.id == project_a.id
                assert (
                    await same_project.workspace.aread_bytes(
                        "/capture.txt", max_bytes=100
                    )
                    == b"captured-output"
                )
            assert (await project_a.aglob("*.txt", "/")).error is None
            assert (await project_a.agrep("gamma", "/")).error is None
            await project_b.aupload_files([("/secret.txt", b"project-b")])
            bounds = await project_a.aexecute(
                "test ! -e /root && test ! -e /opt/opensandbox && "
                "test ! -e /var/lib/tinkerfin-workspaces/records && test ! -e /run/control.sock && "
                "test ! -e /workspace/secret.txt && printf isolated"
            )
            assert bounds.exit_code == 0 and bounds.output == "isolated"
            credentials = """import os
assert os.getuid() == 1000
with open('/proc/self/status') as status:
    fields = dict(line.split(':', 1) for line in status if ':' in line)
for name in ('CapPrm', 'CapEff', 'CapBnd', 'CapAmb'):
    assert int(fields[name].strip(), 16) == 0, name
print('unprivileged')
"""
            permissions = await project_a.aexecute(
                "python -I -S -c " + shlex.quote(credentials)
            )
            assert permissions.exit_code == 0 and "unprivileged" in permissions.output
            denied_mount = await project_a.aexecute(
                "mkdir -p /tmp/mount-check && mount -t tmpfs tmpfs /tmp/mount-check"
            )
            assert denied_mount.exit_code != 0
            parent = await client.connect(project_a.id, purpose="workspaces")
            try:
                address = await parent.aexecute(
                    "python -I -S -c 'import socket; print(socket.gethostbyname(socket.gethostname()))'"
                )
                assert address.exit_code == 0
            finally:
                await parent.aclose()
            neighbor = await manager.get("neighbor")
            authentication = (
                "import urllib.error, urllib.request\n"
                f"address = {address.output.strip()!r}\n"
                + """
for headers in ({}, {'X-EXECD-ACCESS-TOKEN': 'wrong-token'}):
    request = urllib.request.Request('http://' + address + ':44772/ping', headers=headers)
    try:
        urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request)
    except urllib.error.HTTPError as failure:
        assert failure.code == 401, failure.code
    else:
        raise AssertionError('parent control plane accepted an unauthenticated neighbor')
print('authenticated-parent')
"""
            )
            refusal = await neighbor.aexecute(
                "python -I -S -c " + shlex.quote(authentication)
            )
            assert refusal.exit_code == 0 and "authenticated-parent" in refusal.output
            setup = await project_a.aexecute(
                "python -m pip install --disable-pip-version-check --no-cache-dir --no-deps --ignore-installed six==1.17.0 && "
                "npm install --prefix /dependencies/node --ignore-scripts --no-audit --no-fund is-number@7.0.0 && "
                "git ls-remote https://github.com/pallets/markupsafe.git HEAD"
            )
            assert setup.exit_code == 0, setup.output
            browse = """import asyncio, os
from playwright.async_api import async_playwright
async def main():
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(proxy={'server':os.environ['HTTPS_PROXY']})
        try:
            page = await browser.new_page()
            await page.goto('https://example.com')
            assert await page.title() == 'Example Domain'
            assert (await page.screenshot()).startswith(b'\\x89PNG')
        finally:
            await browser.close()
asyncio.run(main())
print('browser-passed')
"""
            browser = await project_a.aexecute("python -c " + shlex.quote(browse))
            assert browser.exit_code == 0 and "browser-passed" in browser.output, (
                browser
            )
            independent = await project_b.aexecute(
                "test ! -e /dependencies/node/node_modules/is-number && "
                "test ! -e /dependencies/python/lib/python3.11/site-packages/six.py && printf independent"
            )
            assert independent.exit_code == 0
            await first.delete()
            assert (await project_b.aexecute("cat secret.txt")).output == "project-b"
            assert project_b.id == physical_id
        async with second.open() as project_b:
            assert (await project_b.aexecute("cat secret.txt")).output == "project-b"
        async with first.open() as empty_project:
            assert (
                await empty_project.aexecute("test ! -e note.txt && printf empty")
            ).output == "empty"
        await first.delete()
        await second.delete()
        print(json.dumps({"physical_sandbox": physical_id, "result": "passed"}))


async def test_another_worker_deletes_only_the_selected_live_project(
    opensandbox_test_service: OpenSandboxTestService,
    opensandbox_docker_runtime: OpenSandboxDockerRuntime,
    tmp_path: Path,
) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'workspaces.db'}")
    connection = ConnectionConfig(
        domain=opensandbox_test_service.domain,
        api_key=opensandbox_test_service.api_key,
        request_timeout=timedelta(minutes=3),
        use_server_proxy=True,
        disable_metrics=True,
    )
    config = OpenSandboxConfig(
        image=opensandbox_docker_runtime.image,
        metadata=opensandbox_test_service.sandbox_metadata,
        warm_pool_size=0,
        ttl=timedelta(minutes=20),
    )
    first_manager = OpenSandboxManager(
        client=OpenSandboxClient(connection_config=connection, config=config),
        state=SQLAlchemyOpenSandboxState(engine=engine),
    )
    second_manager = OpenSandboxManager(
        client=OpenSandboxClient(connection_config=connection, config=config),
        state=SQLAlchemyOpenSandboxState(engine=engine),
    )
    try:
        async with first_manager, second_manager:
            first = first_manager.workspace("users/7", workspace_key="project-a")
            second = first_manager.workspace("users/7", workspace_key="project-b")
            deletion = second_manager.workspace("users/7", workspace_key="project-a")
            try:
                async with first.open() as project_a, second.open() as project_b:
                    assert project_a.id == project_b.id
                    assert (
                        await project_b.awrite("/keep.txt", "retained")
                    ).error is None
                    await deletion.delete()
                    assert (
                        await project_b.aexecute("cat keep.txt")
                    ).output == "retained"
                async with second.open() as reopened:
                    assert (
                        await reopened.aexecute("cat keep.txt")
                    ).output == "retained"
                await second.delete()
            finally:
                await second_manager.destroy("users/7")
    finally:
        await engine.dispose()

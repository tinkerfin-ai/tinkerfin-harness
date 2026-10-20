"""技能完整性、稳定只读目录与本机二进制文件交付"""

import base64
import json

import httpx
import pytest
from pydantic import BaseModel
from test_cloud import configuration, execution_request
from tool_context import tool_context

from tinkerfin_contracts.media import Attachment
from tinkerfin_desktop.assets import SkillDownload, SkillFile, SkillInputs
from tinkerfin_desktop.cloud import StudioClient
from tinkerfin_desktop.configuration import SkillReference
from tinkerfin_desktop.files import file_tools
from tinkerfin_sandbox import LocalAccess, LocalWorkspace
from tinkerfin_studio.skills.packages import SkillFile as PackageFile
from tinkerfin_studio.skills.packages import parse_package


def skill():
    package = parse_package(
        (
            PackageFile(
                "SKILL.md",
                b"---\nname: reports\ndescription: Prepare reports\n---\nRead the inputs.",
            ),
            PackageFile("scripts/report.py", b"print('report')"),
        )
    )
    payload = SkillDownload(
        name=package.name,
        digest=package.digest,
        files=tuple(
            SkillFile(path=file.path, content=base64.b64encode(file.content).decode())
            for file in package.files
        ),
    )
    reference = SkillReference(
        installation_id="reports",
        name=package.name,
        digest=package.digest,
        selected=True,
    )
    return payload, reference


@pytest.mark.parametrize(
    "damage", ["traversal", "digest", "case_collision", "unicode_collision", "base64"]
)
async def test_untrusted_skill_content_fails_before_execution_and_cleans_only_owned_inputs(
    tmp_path, damage
):
    payload, reference = skill()
    value = payload.model_dump(mode="json")
    if damage == "traversal":
        value["files"][0]["path"] = "../escape"
    elif damage == "digest":
        value["files"][0]["content"] = base64.b64encode(b"changed").decode()
    elif damage == "base64":
        value["files"][0]["content"] = "~"
    else:
        value["files"] = [
            *value["files"],
            {"path": "A.md" if damage == "case_collision" else "é.md", "content": ""},
            {
                "path": "a.md" if damage == "case_collision" else "e\u0301.md",
                "content": "",
            },
        ]
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    borrowed = inputs / "retained"
    borrowed.write_text("keep")
    identity = configuration(tmp_path)
    request = execution_request(identity)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200, json={"code": 0, "message": "success", "data": value}
            )
        )
    ) as client:
        with pytest.raises(ValueError):
            async with SkillInputs(inputs).prepare(
                StudioClient(identity, client), request, (reference,)
            ):
                pytest.fail("无效技能不能产生可执行目录")
    assert list(inputs.iterdir()) == [borrowed] and borrowed.read_text() == "keep"


async def test_skill_scope_paths_survive_reopen_and_keep_borrowed_files(
    tmp_path, native_sandbox
):
    payload, reference = skill()
    inputs, project = tmp_path / "inputs", tmp_path / "project"
    inputs.mkdir()
    project.mkdir()
    identity = configuration(tmp_path)
    request = execution_request(identity)
    manager = SkillInputs(inputs)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                json={
                    "code": 0,
                    "message": "success",
                    "data": payload.model_dump(mode="json"),
                },
            )
        )
    ) as client:
        cloud = StudioClient(identity, client)
        async with manager.prepare(cloud, request, (reference,)) as directory:
            assert directory is not None
            async with manager.prepare(
                cloud,
                request.model_copy(
                    update={
                        "identity": request.identity.model_copy(
                            update={"run_id": "resumed"}
                        )
                    }
                ),
                (reference,),
            ) as resumed:
                assert resumed == directory
            assert (
                directory / "reports/scripts/report.py"
            ).read_bytes() == b"print('report')"
            async with LocalWorkspace(
                project,
                sandbox_executable=native_sandbox,
                access=LocalAccess(
                    read_directories=(directory,), read_only_paths=(inputs,)
                ),
            ) as workspace:
                async with workspace.prepare(request.identity) as prepared:
                    result = await prepared.workspace.aread(
                        str(directory / "reports/SKILL.md")
                    )
                    assert "Read the inputs" in str(result)
                    denied = await prepared.workspace.awrite(
                        str(directory / "reports/scripts/report.py"), "changed"
                    )
                    assert denied.error is not None
            saved = {
                path.relative_to(directory): path.read_bytes()
                for path in directory.rglob("*")
                if path.is_file()
            }
        assert not directory.exists()
        async with manager.prepare(cloud, request, (reference,)) as reopened:
            assert reopened == directory
        for relative, content in saved.items():
            target = directory / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        async with manager.prepare(cloud, request, (reference,)) as borrowed:
            assert borrowed == directory
        assert directory.exists()
        (directory / "reports/scripts/report.py").write_text("tampered")
        with pytest.raises(ValueError, match="摘要"):
            async with manager.prepare(cloud, request, (reference,)):
                pytest.fail("修改后的技能不能执行")
        assert (directory / "reports/scripts/report.py").read_text() == "tampered"


async def test_file_tools_import_and_deliver_exact_binary_bytes(
    tmp_path, native_sandbox
):
    header = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"
    content = header + b"0" * (617_780 - len(header) - 6) + b"\n%%EOF"
    attachment = Attachment(
        id="e" * 32,
        name="导入报告.pdf",
        mime_type="application/pdf",
        size_bytes=len(content),
    )
    project = tmp_path / "project"
    project.mkdir()
    destination = tmp_path / "用户指定目录" / attachment.name
    identity = configuration(tmp_path)
    request = execution_request(identity)
    delivered: list[bytes] = []

    async def respond(call: httpx.Request) -> httpx.Response:
        assert call.url.params["thread_id"] == request.identity.thread_id
        assert call.url.params["run_id"] == request.identity.run_id
        if call.method == "POST":
            assert call.url.params["invocation_id"] == "deliver-call"
            delivered.append(call.content)
            data = attachment.model_dump(mode="json")
        elif call.url.path.endswith("/files"):
            data = [attachment.model_dump(mode="json")]
        else:
            assert call.url.path.endswith("/files/" + attachment.id)
            data = {
                "mime_type": attachment.mime_type,
                "content": base64.b64encode(content).decode(),
            }
        return httpx.Response(200, json={"code": 0, "message": "success", "data": data})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        tools = file_tools(StudioClient(identity, client), request)
        assert [entry.name for entry in tools] == ["deliver_file", "import_attachment"]
        for entry, mode in zip(tools, ("read", "write"), strict=True):
            schema_type = entry.tool_call_schema
            assert isinstance(schema_type, type) and issubclass(schema_type, BaseModel)
            schema = schema_type.model_json_schema()
            assert "runtime" not in schema["properties"]
            assert (
                schema["properties"]["file_path"]["x-tinkerfin-file-path"]["operation"]
                == mode
            )
        async with LocalWorkspace(
            project,
            sandbox_executable=native_sandbox,
            access=LocalAccess(filesystem="user"),
        ) as workspace:
            async with workspace.prepare(request.identity) as prepared:
                with tool_context(
                    prepared.workspace, request, "import-call"
                ) as context:
                    result = await tools[1].ainvoke(
                        {
                            "attachment_id": attachment.id,
                            "file_path": str(destination),
                            "runtime": context,
                        }
                    )
                    assert json.loads(result) == {
                        "file_path": str(destination),
                        "size_bytes": len(content),
                    }
                with tool_context(
                    prepared.workspace, request, "deliver-call"
                ) as context:
                    result = await tools[0].ainvoke(
                        {
                            "file_path": str(destination),
                            "name": attachment.name,
                            "runtime": context,
                        }
                    )
                    assert result == [attachment.content_block()]
    assert destination.read_bytes() == content and delivered == [content]


async def test_cancelled_skill_publish_finishes_owned_cleanup_before_reopen(
    tmp_path, monkeypatch
):
    import asyncio
    import os
    import threading
    from pathlib import Path

    payload, reference = skill()
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    retained = inputs / "retained"
    retained.write_text("borrowed")
    identity = configuration(tmp_path)
    request = execution_request(identity)
    manager = SkillInputs(inputs)
    entered = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    original = os.open

    def open_file(path, *args, **kwargs):
        if (
            isinstance(path, Path)
            and path.name == "SKILL.md"
            and path.is_relative_to(inputs)
        ):
            loop.call_soon_threadsafe(entered.set)
            release.wait()
        return original(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", open_file)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                json={
                    "code": 0,
                    "message": "success",
                    "data": payload.model_dump(mode="json"),
                },
            )
        )
    ) as client:
        cloud = StudioClient(identity, client)

        async def prepare() -> None:
            async with manager.prepare(cloud, request, (reference,)):
                pytest.fail("已取消的技能准备不能交付目录")

        operation = asyncio.create_task(prepare())
        signal = asyncio.create_task(entered.wait())
        try:
            await asyncio.wait((operation, signal), return_when=asyncio.FIRST_COMPLETED)
            if not entered.is_set():
                operation.result()
                pytest.fail("技能准备未到达受控文件写入")
            operation.cancel()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await operation
            assert list(inputs.iterdir()) == [retained]
            async with manager.prepare(cloud, request, (reference,)) as directory:
                assert directory is not None
                assert (directory / "reports/SKILL.md").is_file()
            assert list(inputs.iterdir()) == [retained]
        finally:
            release.set()
            operation.cancel()
            signal.cancel()
            await asyncio.gather(operation, signal, return_exceptions=True)

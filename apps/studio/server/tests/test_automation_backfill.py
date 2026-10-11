"""一次性补登记保持原结果、缺失事实与重复执行的边界"""

import importlib.util
import sys
from pathlib import Path

import pytest
from sqlalchemy import delete, select
from test_automation_integration import (
    automation_environment,
    automation_resources,
    automation_worker,
    configuration,
)

from tinkerfin_studio.attachments.entity import AttachmentCollection
from tinkerfin_studio.automation.schemas import SaveTask, TaskCommand, TaskConfiguration
from tinkerfin_studio.automation.service import StudioAutomationService
from tinkerfin_studio.conversation.models import (
    ConversationRunRegistration,
    ConversationThread,
)
from tinkerfin_studio.conversation.repository import ConversationRepository

__all__ = ["automation_environment", "automation_resources", "automation_worker"]

_spec = importlib.util.spec_from_file_location(
    "automation_backfill_tool",
    Path(__file__).resolve().parents[1] / "tools/backfill_automation_conversations.py",
)
assert _spec is not None and _spec.loader is not None
_conversion = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _conversion
_spec.loader.exec_module(_conversion)


@pytest.mark.parametrize("missing", [None, "trace", "services"])
async def test_backfill_preserves_original_evidence_and_never_resurrects_removed_entries(
    automation_resources, automation_worker, missing
):
    resources = automation_resources
    service = StudioAutomationService(resources, user_id=1, project_id="project-1")
    task = await service.save(
        SaveTask(
            request_id="backfill",
            configuration=TaskConfiguration.model_validate(configuration()),
        )
    )
    queued = await service.task_command(
        task.id, "run", TaskCommand(request_id="run", expected_revision=task.revision)
    )
    assert queued is not None
    await automation_worker.wait_until_idle()
    original = await service.result(queued.id)
    async with resources.database.session() as session:
        thread = await ConversationRepository(session).get_thread(
            user_id=1, thread_id=original.thread_id
        )
        assert thread is not None
        await session.execute(
            delete(ConversationRunRegistration).where(
                ConversationRunRegistration.conversation_thread_id == thread.id
            )
        )
        await session.execute(
            delete(ConversationThread).where(ConversationThread.id == thread.id)
        )
        if missing == "services":
            collection = await session.get(AttachmentCollection, queued.id)
            assert collection is not None
            collection.configuration = {"task": collection.configuration["task"]}
        await session.commit()
    if missing == "trace":
        handle = await resources.tracer.get(
            (
                await resources.automation.for_owner("1:project-1").get_run(queued.id)
            ).identity.thread
        )
        await handle.delete()
    assert (
        await _conversion.preflight(resources.database, resources.components_database)
        is False
    )
    report = await _conversion.backfill(
        resources.database, resources.automation, resources.tracer, apply=False
    )
    async with resources.database.session() as session:
        assert (
            await session.scalar(
                select(ConversationThread.id).where(
                    ConversationThread.thread_id == original.thread_id
                )
            )
            is None
        )
    if missing:
        assert queued.id in report.unavailable
        assert not report.created
        return
    assert report.created == [queued.id]
    report = await _conversion.backfill(
        resources.database, resources.automation, resources.tracer, apply=True
    )
    assert report.created == [queued.id]
    restored = await service.result(queued.id)
    assert restored.messages == original.messages
    assert restored.conversation.state == "available"
    async with resources.database.session() as session:
        thread = await ConversationRepository(session).get_thread(
            user_id=1, thread_id=original.thread_id
        )
        assert thread is not None and thread.status == "idle"
        assert thread.last_run_id == original.run_id
        assert thread.message_count == 2
        assert (
            thread.title_source,
            thread.title_generation_status,
            thread.title_seq,
        ) == ("generated", "succeeded", 1)
        exact = await resources.tracer.get_run(
            (
                await resources.automation.for_owner("1:project-1").get_run(queued.id)
            ).identity
        )
        assert thread.updated_at == exact.summary.last_occurred_at.replace(tzinfo=None)
        from datetime import datetime

        thread.deleted_at = datetime(2030, 1, 1)
        await session.commit()
    again = await _conversion.backfill(
        resources.database, resources.automation, resources.tracer, apply=True
    )
    assert again.present == [queued.id] and not again.created
    assert (await service.result(queued.id)).conversation.state == "deleted"


async def test_cli_inspects_saved_runs_with_an_owned_read_client(
    automation_resources, automation_worker, monkeypatch, capsys
):
    from types import SimpleNamespace

    resources = automation_resources
    service = StudioAutomationService(resources, user_id=1, project_id="project-1")
    task = await service.save(
        SaveTask(
            request_id="cli",
            configuration=TaskConfiguration.model_validate(configuration()),
        )
    )
    run = await service.task_command(
        task.id,
        "run",
        TaskCommand(request_id="execute", expected_revision=task.revision),
    )
    assert run is not None
    await automation_worker.wait_until_idle()
    monkeypatch.setattr(
        sys, "argv", ["backfill", "--env-file", "unused-in-fixture.env"]
    )
    monkeypatch.setattr(
        _conversion,
        "load_settings",
        lambda **kwargs: SimpleNamespace(
            business_database=SimpleNamespace(
                url=resources.database.engine.url.render_as_string(hide_password=False)
            ),
            components_database=SimpleNamespace(
                url=resources.components_database.engine.url.render_as_string(
                    hide_password=False
                )
            ),
        ),
    )
    await _conversion.main()
    import json

    report = json.loads(capsys.readouterr().out)
    assert report["present"] == [run.id]
    assert report["applied"] is False and report["created"] == []

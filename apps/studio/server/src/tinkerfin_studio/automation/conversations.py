"""将自动化执行登记为用户可继续访问的独立会话"""

from __future__ import annotations

from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from pydantic import JsonValue
from sqlalchemy.exc import IntegrityError

from tinkerfin_automation import AutomationExecution
from tinkerfin_studio.conversation.repository import ConversationRepository
from tinkerfin_studio.conversation.run_preparation import (
    AutomationRunContext,
    PreparedRunRequest,
    StartChatIntent,
)
from tinkerfin_studio.conversation.run_registration import (
    ConversationRunPreparer,
    PreparedExecution,
)
from tinkerfin_studio.models.schemas import AgentModelConfig
from tinkerfin_studio.services.schemas import ServiceBindings

from .schemas import TaskConfiguration

if TYPE_CHECKING:
    from tinkerfin_studio.resources import ApplicationResources


async def register_conversation(
    resources: ApplicationResources,
    *,
    execution: AutomationExecution,
    configuration: TaskConfiguration,
    user_id: int,
    model: AgentModelConfig,
    services: ServiceBindings,
    content: list[JsonValue],
) -> tuple[PreparedRunRequest, PreparedExecution]:
    """按已授权执行身份幂等登记会话，并沿用普通会话的并发与快照校验"""
    identity = execution.identity
    stamp = execution.queued_at.astimezone(ZoneInfo("Asia/Shanghai"))
    title = f"{configuration.name[:18]} · {stamp:%m-%d %H:%M}"
    message_id = f"automation-message-{execution.execution_id}"
    message: dict[str, JsonValue] = {
        "id": message_id,
        "role": "user",
        "content": content,
    }
    forwarded: dict[str, JsonValue] = {
        "projectId": configuration.project_id,
        "model": configuration.model_id,
        "accessMode": configuration.access_mode,
        "command": {"plan": "off"},
    }
    prepared = PreparedRunRequest(
        project_id=configuration.project_id,
        input_json={
            "threadId": identity.thread_id,
            "runId": identity.run_id,
            "messages": [message],
            "state": {},
            "tools": [],
            "context": [],
            "forwardedProps": forwarded,
        },
        messages=(message,),
        identity=identity,
        parent_run_id=None,
        parameters={"forwarded_props": forwarded},
        message_ids=(message_id,),
        mode="default",
        access_mode=configuration.access_mode,
    )
    async with resources.database.session() as session:
        repository = ConversationRepository(session)
        thread = await repository.get_thread(
            user_id=user_id, thread_id=identity.thread_id
        )
        created = False
        if thread is None:
            try:
                thread = await repository.create_thread(
                    user_id=user_id,
                    project_id=configuration.project_id,
                    thread_id=identity.thread_id,
                    title=title,
                    model_id=model.model_id,
                )
                thread.title_source = "generated"
                thread.title_generation_status = "succeeded"
                thread.title_seq = 1
                # 登记会重新锁定并刷新会话，先写入自动化标题，避免被旧默认值覆盖
                await session.flush()
                created = True
            except IntegrityError:
                await repository.rollback()
                thread = await repository.get_thread(
                    user_id=user_id, thread_id=identity.thread_id
                )
                if thread is None:
                    raise
        registered = await ConversationRunPreparer(
            session,
            user_id=user_id,
            attachments=resources.attachments,
            notifications=resources.notifications,
        ).register(
            intent=StartChatIntent(attachments=(), title=title),
            prepared=prepared,
            model=model,
            thread=thread,
            thread_created=created,
            automation=AutomationRunContext(execution.execution_id, services),
        )
        return prepared, registered

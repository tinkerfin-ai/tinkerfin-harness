"""按执行快照准备用户模型和文件，运行生命周期交给框架"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from pydantic import JsonValue, ValidationError

from tinkerfin_automation import ExecutionFailure, InterruptedExecution, TinkerFinTarget
from tinkerfin_automation.targets import (
    ExecutionFailed,
    ExecutionOutcome,
    ExecutionRequest,
)
from tinkerfin_studio.agent.runtime import build_automation_runtime
from tinkerfin_studio.api.errors import BusinessException, ModelErrorCode
from tinkerfin_studio.auth.models import User
from tinkerfin_studio.models.repository import AgentModelRepository
from tinkerfin_studio.models.service import AgentModelService

from .service import NAMESPACE, collection_id, task_configuration

if TYPE_CHECKING:
    from tinkerfin_studio.resources import ApplicationResources


async def fail_interactive_execution(
    execution: InterruptedExecution,
) -> ExecutionFailure:
    """只读自动化不处理人工请求，结束执行而不提交审批决定"""
    return ExecutionFailure(
        code="studio.interaction_required",
        message="任务需要人工处理，自动化不会继续执行",
    )


class StudioAutomationTarget:
    """只决定业务用户、模型和附件，借用框架的执行与取消能力"""

    def __init__(self, resources: ApplicationResources) -> None:
        self._resources = resources

    @property
    def cancellation_is_final(self) -> bool:
        """外部工具可能已产生效果，不能承诺取消会撤销外部执行"""
        return False

    async def run(self, request: ExecutionRequest) -> ExecutionOutcome:
        """核验用户仍可用并按输入快照执行；错误不暴露模型连接凭据"""
        execution = request.execution
        if execution.namespace != NAMESPACE:
            raise ValueError("自动化命名空间无效")
        user_id = int(execution.owner_id)
        config = task_configuration(execution)
        async with self._resources.database.session() as session:
            user = await session.get(User, user_id)
            if user is None or user.disabled:
                return ExecutionFailed(
                    ExecutionFailure(
                        code="studio.user_unavailable", message="任务所属用户不可用"
                    )
                )
            models = AgentModelService(AgentModelRepository(session, user_id=user_id))
            try:
                model = await models.resolve(config.model_id)
                image_model = await models.resolve_image_model()
            except BusinessException:
                return ExecutionFailed(
                    ExecutionFailure(
                        code="studio.model_unavailable", message="任务模型不可用"
                    )
                )
        files = self._resources.attachments
        try:
            inputs = await files.list_collection(
                user_id=user_id, collection_id=collection_id(execution)
            )
            if set(config.attachments) != {file.id for file in inputs}:
                raise ValueError("任务附件快照不一致")
            if model.image_support != "supported" and any(
                file.mime_type.startswith("image/") for file in inputs
            ):
                raise BusinessException(ModelErrorCode.IMAGE_UNSUPPORTED)
            await files.create_collection(
                user_id=user_id,
                collection_id=execution.execution_id,
                purpose="execution",
                attachment_ids=tuple(config.attachments),
                configuration=config.model_dump(mode="json", by_alias=True),
                task_id=execution.task_id,
            )
            content: list[JsonValue] = [
                {"type": "text", "text": config.prompt},
                *[file.content_block() for file in inputs],
            ]
            runtime = build_automation_runtime(
                resources=self._resources,
                user_id=user_id,
                thread_id=execution.identity.thread_id,
                execution_id=execution.execution_id,
                model_config=model,
                image_model=image_model,
                access_mode=config.access_mode,
            )
            prepared = replace(
                execution, input={"messages": [{"role": "user", "content": content}]}
            )
            return await TinkerFinTarget(runtime).run(
                ExecutionRequest(execution=prepared, deadline=request.deadline)
            )
        except (BusinessException, ValidationError):
            return ExecutionFailed(
                ExecutionFailure(
                    code="studio.input_unavailable", message="任务配置或参考文件不可用"
                )
            )

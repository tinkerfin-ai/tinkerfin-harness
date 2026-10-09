"""按执行快照准备用户模型和文件，运行生命周期交给框架"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from pydantic import JsonValue, ValidationError

from tinkerfin_automation import (
    AutomationExecution,
    ExecutionFailure,
    InterruptedExecution,
    TinkerFinTarget,
)
from tinkerfin_automation.targets import (
    ExecutionFailed,
    ExecutionOutcome,
    ExecutionRequest,
)
from tinkerfin_studio.agent.runtime import build_automation_runtime
from tinkerfin_studio.api.errors import (
    AttachmentErrorCode,
    BusinessException,
    ServiceErrorCode,
)
from tinkerfin_studio.auth.models import User
from tinkerfin_studio.models.repository import AgentModelRepository
from tinkerfin_studio.models.service import AgentModelService
from tinkerfin_studio.projects.repository import ProjectRepository
from tinkerfin_studio.services.repository import ServiceConfigRepository
from tinkerfin_studio.services.schemas import ServiceBindings
from tinkerfin_studio.services.service import (
    ResolvedService,
    ServiceConfigService,
    bindings_for,
)
from tinkerfin_studio.skills.repository import SkillRepository

from .ownership import automation_owner, parse_automation_owner
from .schemas import TaskConfiguration
from .service import NAMESPACE, collection_id, task_configuration

if TYPE_CHECKING:
    from tinkerfin_studio.resources import ApplicationResources


async def fail_interactive_execution(
    execution: InterruptedExecution,
) -> ExecutionFailure:
    """自动化不处理人工请求，结束执行而不提交审批决定"""
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

    async def _execution_services(
        self, execution: AutomationExecution, config: TaskConfiguration, *, user_id: int
    ) -> tuple[ResolvedService | None, ResolvedService | None]:
        """首次启动固定服务；重入或显式重试只读取已建立的绑定"""
        files = self._resources.attachments
        task = config.model_dump(mode="json", by_alias=True)
        stored = await files.collection_configuration(
            user_id=user_id, collection_id=execution.execution_id
        )
        fresh: tuple[ResolvedService | None, ResolvedService | None] | None = None
        if stored is None:
            bindings: ServiceBindings | None = None
            if execution.retry_of is not None:
                inherited = await files.collection_configuration(
                    user_id=user_id, collection_id=execution.retry_of
                )
                if inherited is None:
                    original = (
                        await self._resources.automation.for_owner(
                            automation_owner(user_id, config.project_id)
                        ).get_run(execution.retry_of)
                    ).snapshot
                    if original.execution_started_at is not None:
                        raise BusinessException(ServiceErrorCode.CONFIGURATION_CHANGED)
                elif inherited.get("task") != task:
                    raise ValueError("原执行缺少可继承的服务绑定")
                else:
                    if inherited.get("services") is None:
                        raise BusinessException(ServiceErrorCode.CONFIGURATION_CHANGED)
                    bindings = ServiceBindings.model_validate(inherited.get("services"))
            if bindings is None:
                async with self._resources.database.session() as session:
                    services = ServiceConfigService(
                        ServiceConfigRepository(session, user_id=user_id)
                    )
                    search = await services.resolve("web_search")
                    image = await services.resolve("image_generation")
                    fresh = search, image
                    bindings = bindings_for(search, image)
            candidate: dict[str, JsonValue] = {
                "task": task,
                "services": bindings.model_dump(mode="json"),
            }
            try:
                await files.create_collection(
                    user_id=user_id,
                    project_id=config.project_id,
                    collection_id=execution.execution_id,
                    purpose="execution",
                    attachment_ids=tuple(config.attachments),
                    configuration=candidate,
                    task_id=execution.task_id,
                    source_collection_id=collection_id(execution),
                )
                stored = candidate
            except BusinessException as error:
                if error.error_code != AttachmentErrorCode.REFERENCE_CONFLICT:
                    raise
                stored = await files.collection_configuration(
                    user_id=user_id, collection_id=execution.execution_id
                )
                fresh = None
        if stored is None or stored.get("task") != task:
            raise ValueError("执行配置与已保存快照不一致")
        if stored.get("services") is None:
            raise BusinessException(ServiceErrorCode.CONFIGURATION_CHANGED)
        bindings = ServiceBindings.model_validate(stored.get("services"))
        if fresh is not None:
            return fresh
        async with self._resources.database.session() as session:
            services = ServiceConfigService(
                ServiceConfigRepository(session, user_id=user_id)
            )
            search = (
                None
                if bindings.web_search is None
                else await services.require_bound(
                    "web_search",
                    id=bindings.web_search.id,
                    fingerprint=bindings.web_search.fingerprint,
                )
            )
            image = (
                None
                if bindings.image_generation is None
                else await services.require_bound(
                    "image_generation",
                    id=bindings.image_generation.id,
                    fingerprint=bindings.image_generation.fingerprint,
                )
            )
            return search, image

    async def run(self, request: ExecutionRequest) -> ExecutionOutcome:
        """核验用户仍可用并按输入快照执行；错误不暴露模型连接凭据"""
        execution = request.execution
        if execution.namespace != NAMESPACE:
            raise ValueError("自动化命名空间无效")
        user_id, project_id = parse_automation_owner(execution.owner_id)
        config = task_configuration(execution)
        if config.project_id != project_id:
            raise ValueError("任务配置与所属项目不一致")
        async with self._resources.database.session() as session:
            user = await session.get(User, user_id)
            if user is None or user.disabled:
                return ExecutionFailed(
                    ExecutionFailure(
                        code="studio.user_unavailable", message="任务所属用户不可用"
                    )
                )
            await ProjectRepository(session, user_id).require(config.project_id)
            models = AgentModelService(AgentModelRepository(session, user_id=user_id))
            try:
                model = await models.resolve(config.model_id)
                await SkillRepository(session, user_id).capture(
                    execution.identity, project_id=config.project_id
                )
                await session.commit()
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
            search_service, image_service = await self._execution_services(
                execution, config, user_id=user_id
            )
            content: list[JsonValue] = [
                {"type": "text", "text": config.prompt},
                *[file.content_block() for file in inputs],
            ]
            runtime = build_automation_runtime(
                resources=self._resources,
                user_id=user_id,
                thread_id=execution.identity.thread_id,
                project_id=config.project_id,
                execution_id=execution.execution_id,
                model_config=model,
                search_service=search_service,
                image_service=image_service,
                access_mode=config.access_mode,
            )
            prepared = replace(
                execution, input={"messages": [{"role": "user", "content": content}]}
            )
            return await TinkerFinTarget(runtime).run(
                ExecutionRequest(execution=prepared, deadline=request.deadline)
            )
        except BusinessException as error:
            if error.error_code == ServiceErrorCode.CONFIGURATION_CHANGED:
                return ExecutionFailed(
                    ExecutionFailure(
                        code="studio.service_changed",
                        message="原执行使用的服务配置已变化，请新建一次运行",
                    )
                )
            return ExecutionFailed(
                ExecutionFailure(
                    code="studio.input_unavailable", message="任务配置或参考文件不可用"
                )
            )
        except (ValidationError, ValueError):
            return ExecutionFailed(
                ExecutionFailure(
                    code="studio.input_unavailable", message="任务配置或参考文件不可用"
                )
            )

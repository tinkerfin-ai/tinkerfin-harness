"""把认证用户的业务任务交给框架调度，并读取授权结果"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING
from uuid import NAMESPACE_URL, uuid5

from tinkerfin.agui import AgUiHistory
from tinkerfin_automation import (
    AutomationExecution,
    AutomationTask,
    ExecutionFilter,
    ExecutionStatus,
    TaskFilter,
    TaskStatus,
)
from tinkerfin_automation.errors import (
    AutomationError,
    ExecutionNotFoundError,
    InvalidScheduleError,
    QueueFullError,
    RequestConflictError,
    TaskConflictError,
    TaskNotFoundError,
)
from tinkerfin_contracts.media import Attachment
from tinkerfin_studio.api.errors import (
    AttachmentErrorCode,
    AutomationErrorCode,
    BusinessException,
)
from tinkerfin_studio.models.repository import AgentModelRepository
from tinkerfin_studio.models.service import AgentModelService
from tinkerfin_tracing import TraceThreadNotFound

from .schemas import (
    BatchCommand,
    BatchResult,
    RunDetail,
    RunList,
    RunView,
    SaveTask,
    TaskCommand,
    TaskConfiguration,
    TaskList,
    TaskView,
)

if TYPE_CHECKING:
    from tinkerfin_studio.resources import ApplicationResources

NAMESPACE = "studio_automation"
TARGET = "studio_agent"


def task_configuration(task: AutomationTask | AutomationExecution) -> TaskConfiguration:
    """解析已保存的业务输入，不允许执行记录选择任意 target 或用户"""
    return TaskConfiguration.model_validate(task.input["configuration"])


def collection_id(task: AutomationTask | AutomationExecution) -> str:
    """读取框架输入中已保存的附件集合引用"""
    value = task.input["attachmentCollectionId"]
    if not isinstance(value, str) or not value:
        raise ValueError("任务附件集合引用无效")
    return value


def automation_error(error: AutomationError | ValueError) -> BusinessException:
    """将框架失败映射为稳定业务错误，不返回异常细节"""
    if isinstance(error, (TaskNotFoundError, ExecutionNotFoundError)):
        code = AutomationErrorCode.NOT_FOUND
    elif isinstance(error, (TaskConflictError, RequestConflictError)):
        code = AutomationErrorCode.CONFLICT
    elif isinstance(error, QueueFullError):
        code = AutomationErrorCode.QUEUE_FULL
    elif isinstance(error, (InvalidScheduleError, ValueError)):
        code = AutomationErrorCode.INVALID_CONFIGURATION
    else:
        code = AutomationErrorCode.UNAVAILABLE
    return BusinessException(code)


def run_view(run: AutomationExecution) -> RunView:
    """只公开安全的状态说明，不把模型异常或密钥放入历史"""
    failure = None
    if run.failure_code == "studio.interaction_required":
        failure = "任务需要人工处理，自动化不会继续执行"
    elif run.status == ExecutionStatus.FAILED:
        failure = "任务执行失败，请检查模型、指令和参考文件"
    elif run.status == ExecutionStatus.TIMED_OUT:
        failure = "任务执行超时"
    elif run.status == ExecutionStatus.NEEDS_ATTENTION:
        failure = "执行结果尚未确认，请检查运行记录"
    return RunView(
        id=run.execution_id,
        task_id=run.task_id,
        name=run.task_name,
        status=run.status,
        trigger=run.origin.value,
        queued_at=run.queued_at,
        started_at=run.execution_started_at,
        finished_at=run.finished_at,
        error=failure,
    )


class StudioAutomationService:
    """绑定当前用户后完成任务命令、查询和文件授权"""

    def __init__(self, resources: ApplicationResources, *, user_id: int) -> None:
        self._resources = resources
        self._user_id = user_id
        self._automation = resources.automation.for_owner(
            str(user_id), execution_namespace=f"ns_{user_id}"
        )

    async def _task_view(self, task: AutomationTask) -> TaskView:
        config = task_configuration(task)
        files = await self._resources.attachments.list_collection(
            user_id=self._user_id, collection_id=collection_id(task)
        )
        return TaskView(
            **config.model_dump(),
            id=task.task_id,
            enabled=task.status == TaskStatus.ENABLED,
            revision=task.revision,
            next_run_at=task.next_run_at,
            input_files=files,
        )

    async def _validate_configuration(self, config: TaskConfiguration) -> None:
        async with self._resources.database.session() as session:
            models = AgentModelService(
                AgentModelRepository(session, user_id=self._user_id)
            )
            await models.resolve(config.model_id)
        for identity in config.attachments:
            await self._resources.attachments.get(identity, user_id=self._user_id)

    async def save(self, command: SaveTask, *, task_id: str | None = None) -> TaskView:
        """先保留不可变输入，再提交可幂等重试的调度命令"""
        config = command.configuration
        if (task_id is None) != (command.expected_revision is None):
            raise BusinessException(AutomationErrorCode.INVALID_CONFIGURATION)
        task = None if task_id is None else await self._automation.task(task_id)
        await self._validate_configuration(config)
        identity = str(
            uuid5(
                NAMESPACE_URL, f"studio-automation:{self._user_id}:{command.request_id}"
            )
        )
        files = self._resources.attachments
        await files.create_collection(
            user_id=self._user_id,
            collection_id=identity,
            purpose="input",
            attachment_ids=tuple(config.attachments),
            configuration=config.model_dump(mode="json", by_alias=True),
        )
        anchor = await files.collection_created_at(
            user_id=self._user_id, collection_id=identity
        )
        schedule = config.framework_schedule(anchor)
        saved_input = {
            "configuration": config.model_dump(mode="json", by_alias=True),
            "attachmentCollectionId": identity,
        }
        try:
            if task is None:
                task = await self._automation.create_task(
                    name=config.name,
                    schedule=schedule,
                    target=TARGET,
                    input=saved_input,
                    request_id=command.request_id,
                )
            else:
                assert command.expected_revision is not None
                await task.update(
                    expected_revision=command.expected_revision,
                    name=config.name,
                    schedule=schedule,
                    input=saved_input,
                    request_id=command.request_id,
                )
        except (InvalidScheduleError, TaskConflictError):
            await files.discard_collection(
                user_id=self._user_id, collection_id=identity
            )
            raise
        await files.mark_collection_task(
            user_id=self._user_id, collection_id=identity, task_id=task.id
        )
        return await self._task_view(task.snapshot)

    async def get_task(self, task_id: str) -> TaskView:
        """读取本人任务的完整可编辑配置"""
        return await self._task_view((await self._automation.task(task_id)).snapshot)

    async def list_tasks(
        self,
        *,
        query: str | None = None,
        status: TaskStatus | None = None,
        cursor: str | None = None,
        limit: int = 50,
    ) -> TaskList:
        """筛选发生在存储端，返回稳定游标和真实下次执行时间"""
        page = await self._automation.list_tasks(
            filters=TaskFilter(
                name_contains=query, statuses=(status,) if status else ()
            ),
            cursor=cursor,
            limit=limit,
        )
        return TaskList(
            items=[await self._task_view(task.snapshot) for task in page.items],
            next_cursor=page.next_cursor,
        )

    async def task_counts(self, *, query: str | None = None) -> dict[TaskStatus, int]:
        """统计全部匹配任务，不受当前分页和状态选择影响"""
        return await self._automation.summarize_tasks(
            filters=TaskFilter(name_contains=query)
        )

    async def task_command(
        self, task_id: str, operation: str, command: TaskCommand
    ) -> TaskView | RunView | None:
        """执行启停、删除或手动运行；删除保留历史和附件引用"""
        if operation == "delete":
            await self._automation.delete_task(
                task_id,
                expected_revision=command.expected_revision,
                request_id=command.request_id,
            )
            return None
        task = await self._automation.task(task_id)
        if operation == "run":
            run = await task.run(
                expected_revision=command.expected_revision,
                request_id=command.request_id,
            )
            return run_view(run.snapshot)
        change = {"pause": task.pause, "enable": task.enable}.get(operation)
        if change is None:
            raise BusinessException(AutomationErrorCode.INVALID_CONFIGURATION)
        await change(
            expected_revision=command.expected_revision, request_id=command.request_id
        )
        return await self._task_view(task.snapshot)

    async def batch(self, command: BatchCommand) -> list[BatchResult]:
        """逐项返回有限批量命令结果，取消请求时保持取消传播"""
        results = []
        for item in command.items:
            try:
                await self.task_command(
                    item.task_id,
                    command.operation,
                    TaskCommand(
                        request_id=item.request_id,
                        expected_revision=item.expected_revision,
                    ),
                )
            except (AutomationError, BusinessException, ValueError) as error:
                failure = (
                    error
                    if isinstance(error, BusinessException)
                    else automation_error(error)
                )
                results.append(
                    BatchResult(
                        task_id=item.task_id,
                        succeeded=False,
                        error=failure.error_code.message,
                    )
                )
            else:
                results.append(BatchResult(task_id=item.task_id, succeeded=True))
        return results

    async def list_runs(
        self,
        *,
        queued_from: datetime | None = None,
        queued_until: datetime | None = None,
        task_id: str | None = None,
        query: str | None = None,
        status: ExecutionStatus | None = None,
        cursor: str | None = None,
        limit: int = 50,
    ) -> RunList:
        """按北京时间换算后的范围读取执行历史，时间范围左闭右开"""
        page = await self._automation.list_runs(
            task_id=task_id,
            filters=ExecutionFilter(
                name_contains=query,
                statuses=(status,) if status else (),
                queued_from=queued_from,
                queued_until=queued_until,
            ),
            cursor=cursor,
            limit=limit,
        )
        return RunList(
            items=[run_view(run.snapshot) for run in page.items],
            next_cursor=page.next_cursor,
        )

    async def run_counts(
        self, *, queued_from: datetime, queued_until: datetime, query: str | None = None
    ) -> dict[ExecutionStatus, int]:
        """按名称及日期范围汇总历史的完整状态分布"""
        return await self._automation.summarize_runs(
            filters=ExecutionFilter(
                name_contains=query, queued_from=queued_from, queued_until=queued_until
            ),
        )

    async def result(self, execution_id: str) -> RunDetail:
        """先核验执行归属，再读取框架的只读消息与当前执行附件"""
        run = (await self._automation.get_run(execution_id)).snapshot
        view = run_view(run)
        if run.execution_started_at is None:
            return RunDetail(**view.model_dump(), result_available=False)
        try:
            history = await AgUiHistory(
                self._resources.tracer, namespace=run.identity.namespace
            ).get(run.identity.thread_id, head_run_id=run.identity.run_id, limit=100)
        except TraceThreadNotFound:
            return RunDetail(**view.model_dump(), result_available=False)
        files = await self._resources.attachments.list_collection(
            user_id=self._user_id, collection_id=execution_id
        )
        original_ids = set(task_configuration(run).attachments)
        return RunDetail(
            **view.model_dump(),
            result_available=True,
            messages=[
                message
                for message in history.snapshot.messages
                if message.role == "assistant"
            ],
            output_files=[file for file in files if file.id not in original_ids],
        )

    async def deliver_files(
        self, execution_id: str, attachment_ids: list[str]
    ) -> list[Attachment]:
        """核验本人执行及所选产物，复用持久附件，不重新生成或复制

        Args:
            execution_id: 已查询的执行 ID
            attachment_ids: 运行结果 outputFiles 中选定的文件 ID，不含参考附件

        Returns:
            按请求顺序返回可交付附件，重复 ID 只保留第一次

        Raises:
            ExecutionNotFoundError: 执行不存在或不属于当前用户
            BusinessException: 所选文件不属于本次产物或已经不可用
            ValueError: 文件数量不在一至一百之间
        """
        if not 1 <= len(attachment_ids) <= 100:
            raise ValueError("请选择一至一百个运行产物")
        run = (await self._automation.get_run(execution_id)).snapshot
        inputs = set(task_configuration(run).attachments)
        files: list[Attachment] = []
        for identity in dict.fromkeys(attachment_ids):
            if identity in inputs:
                raise BusinessException(AttachmentErrorCode.NOT_FOUND)
            files.append(
                await self._resources.attachments.get(
                    identity, user_id=self._user_id, collection_id=execution_id
                )
            )
        return files

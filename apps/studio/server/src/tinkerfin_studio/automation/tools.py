"""对话中的任务管理，复用页面的用户授权、配置与附件保存规则"""

from __future__ import annotations

import json
from collections.abc import Awaitable
from datetime import date, datetime
from typing import TYPE_CHECKING, Literal
from uuid import NAMESPACE_URL, uuid5

from langchain_core.tools import BaseTool, ToolException, tool
from pydantic import BaseModel, JsonValue

from tinkerfin.tools import ToolRuntime
from tinkerfin_automation import AutomationError, ExecutionStatus, TaskStatus
from tinkerfin_studio.agent.access import AccessMode

from .schemas import SaveTask, ScheduleInput, TaskCommand, TaskConfiguration
from .service import StudioAutomationService, automation_error

if TYPE_CHECKING:
    from tinkerfin_studio.resources import ApplicationResources


def build_automation_tools(
    resources: ApplicationResources,
    *,
    user_id: int,
    model_id: str,
    access_mode: AccessMode,
) -> tuple[BaseTool, ...]:
    """把当前用户的任务管理交给对话主 Agent，后台与子 Agent 不注册

    Args:
        resources: 应用拥有的共享资源，工具只在当前运行中借用
        user_id: 已认证用户，不能由模型参数覆盖
        model_id: 创建任务时采用的当前会话模型
        access_mode: 创建任务时沿用的文件审批选择

    Returns:
        任务管理、执行查询及已有产物交付工具；写操作保留明确任务版本
    """
    service = StudioAutomationService(resources, user_id=user_id)

    def request_id(runtime: ToolRuntime, operation: str) -> str:
        info = runtime.execution_info
        if info is None or not runtime.tool_call_id:
            raise ToolException("任务操作缺少调用标识，请重新发起对话")
        # 原生执行任务与工具调用在 checkpoint 恢复中保持不变；不同节点即使
        # 使用相同工具调用 ID 也属于不同命令，不能使用恢复后变化的 run_id
        identity = [
            str(user_id),
            runtime.identity.thread_id,
            operation,
            info.task_id,
            runtime.tool_call_id,
        ]
        return str(uuid5(NAMESPACE_URL, json.dumps(identity, ensure_ascii=False)))

    async def result(operation: Awaitable[BaseModel | None]) -> str:
        try:
            value = await operation
        except AutomationError as error:
            raise automation_error(error) from error
        return (
            value.model_dump_json(by_alias=True)
            if value is not None
            else '{"deleted":true}'
        )

    @tool(parse_docstring=True, error_on_invalid_docstring=True)
    async def create_automation(
        name: str,
        prompt: str,
        schedule: ScheduleInput,
        runtime: ToolRuntime,
        starts_on: date | None = None,
        ends_on: date | None = None,
        attachments: list[str] | None = None,
    ) -> str:
        """为当前用户创建并启用独立的定时任务，成功后才报告已创建

        Args:
            name: 简短明确的任务名称
            prompt: 后台可独立执行的完整指令，不依赖本轮对话历史
            schedule: 北京时间日程；工作日只指周一至周五，间隔至少一分钟
            starts_on: 可选开始日期，包含当天
            ends_on: 可选结束日期，包含当天
            attachments: 用户明确要求用于任务的已上传附件 ID，最多五个；不自动收集整个会话

        Returns:
            保存后的任务配置、任务 ID、修订号和下次执行时间

        Raises:
            BusinessException: 模型、附件、日程或任务保存不可用
            ValueError: 任务配置不合法
            ToolException: 缺少稳定工具调用标识
        """
        config = TaskConfiguration(
            name=name,
            prompt=prompt,
            schedule=schedule,
            starts_on=starts_on,
            ends_on=ends_on,
            model_id=model_id,
            access_mode=access_mode,
            attachments=attachments or [],
        )
        return await result(
            service.save(
                SaveTask(configuration=config, request_id=request_id(runtime, "create"))
            )
        )

    @tool(parse_docstring=True, error_on_invalid_docstring=True)
    async def update_automation(
        task_id: str,
        expected_revision: int,
        configuration: TaskConfiguration,
        runtime: ToolRuntime,
    ) -> str:
        """按已读取的任务版本保存完整配置，保留用户未要求修改的字段

        Args:
            task_id: get_automation 或 list_automations 返回的任务 ID
            expected_revision: 读取该任务时返回的 revision，不猜测或自动采用新版本
            configuration: 修改后的完整任务配置，保留原模型、权限、附件和未修改日程字段

        Returns:
            保存后的任务及新的修订号

        Raises:
            BusinessException: 任务不存在、版本冲突、资源无权访问或保存失败
            ValueError: 配置不合法
            ToolException: 缺少稳定工具调用标识
        """
        return await result(
            service.save(
                SaveTask(
                    configuration=configuration,
                    expected_revision=expected_revision,
                    request_id=request_id(runtime, "update"),
                ),
                task_id=task_id,
            )
        )

    async def command(
        task_id: str,
        expected_revision: int,
        runtime: ToolRuntime,
        operation: Literal["pause", "enable", "delete", "run"],
    ) -> str:
        return await result(
            service.task_command(
                task_id,
                operation,
                TaskCommand(
                    expected_revision=expected_revision,
                    request_id=request_id(runtime, operation),
                ),
            )
        )

    @tool(parse_docstring=True, error_on_invalid_docstring=True)
    async def pause_automation(
        task_id: str, expected_revision: int, runtime: ToolRuntime
    ) -> str:
        """暂停任务后续定时触发，不取消已经提交的执行

        Args:
            task_id: 已明确指认的当前用户任务 ID
            expected_revision: 查询任务时返回的 revision

        Returns:
            暂停后的任务配置和修订号

        Raises:
            BusinessException: 任务不存在、版本冲突或服务不可用
            ToolException: 缺少稳定工具调用标识
        """
        return await command(task_id, expected_revision, runtime, "pause")

    @tool(parse_docstring=True, error_on_invalid_docstring=True)
    async def enable_automation(
        task_id: str, expected_revision: int, runtime: ToolRuntime
    ) -> str:
        """从当前时间启用未来定时触发，不补跑暂停期间的任务

        Args:
            task_id: 已明确指认的当前用户任务 ID
            expected_revision: 查询任务时返回的 revision

        Returns:
            启用后的任务及真实下次执行时间

        Raises:
            BusinessException: 任务不存在、版本冲突、日程到期或服务不可用
            ToolException: 缺少稳定工具调用标识
        """
        return await command(task_id, expected_revision, runtime, "enable")

    @tool(parse_docstring=True, error_on_invalid_docstring=True)
    async def delete_automation(
        task_id: str, expected_revision: int, runtime: ToolRuntime
    ) -> str:
        """按用户明确指令删除任务，取消尚未开始的执行并保留运行历史

        仅在目标明确时调用，不需要再次确认；重名或指代不清时先询问用户。

        Args:
            task_id: 用户明确要求删除的当前用户任务 ID
            expected_revision: 查询该任务时返回的 revision

        Returns:
            删除成功标记；任务定义不可恢复，历史仍可查看

        Raises:
            BusinessException: 任务不存在、版本冲突或服务不可用
            ToolException: 缺少稳定工具调用标识
        """
        return await command(task_id, expected_revision, runtime, "delete")

    @tool(parse_docstring=True, error_on_invalid_docstring=True)
    async def run_automation_task_now(
        task_id: str, expected_revision: int, runtime: ToolRuntime
    ) -> str:
        """给已有任务提交一次额外执行，不改变日程也不等待完成

        Args:
            task_id: 用户明确要求立即运行的任务 ID
            expected_revision: 查询该任务时返回的 revision

        Returns:
            执行 ID 和已保存状态；已提交不代表执行成功

        Raises:
            BusinessException: 任务不存在、版本冲突、队列已满或服务不可用
            ToolException: 缺少稳定工具调用标识
        """
        return await command(task_id, expected_revision, runtime, "run")

    @tool(parse_docstring=True, error_on_invalid_docstring=True)
    async def get_automation(task_id: str) -> str:
        """读取本人任务的完整配置和修订号，供修改或控制前核对

        Args:
            task_id: 已知的任务 ID，不能用任务名称代替

        Returns:
            完整任务配置、状态、revision、下次执行时间及 inputFiles 参考附件；不包含执行产物

        Raises:
            BusinessException: 任务不存在或服务不可用
        """
        return await result(service.get_task(task_id))

    @tool(parse_docstring=True, error_on_invalid_docstring=True)
    async def list_automations(
        query: str | None = None,
        status: TaskStatus | None = None,
        cursor: str | None = None,
        limit: int = 50,
    ) -> str:
        """查询当前用户的任务，重名时据返回结果澄清目标

        Args:
            query: 可选的任务名称关键词
            status: 可选 enabled 或 paused 状态
            cursor: 上一页返回的游标，查询条件须保持一致
            limit: 每页任务数，范围一至一百

        Returns:
            任务列表和下一页游标；inputFiles 仅是参考附件，执行情况须查询 list_automation_runs

        Raises:
            BusinessException: 服务不可用
            ValueError: 分页参数不合法
        """
        return await result(
            service.list_tasks(query=query, status=status, cursor=cursor, limit=limit)
        )

    @tool(parse_docstring=True, error_on_invalid_docstring=True)
    async def list_automation_runs(
        task_id: str | None = None,
        query: str | None = None,
        queued_from: datetime | None = None,
        queued_until: datetime | None = None,
        status: ExecutionStatus | None = None,
        cursor: str | None = None,
        limit: int = 20,
    ) -> str:
        """查询本人自动化的真实执行记录，用于确认是否执行及执行次数

        Args:
            task_id: 可选的准确任务 ID，任务已删除仍可查历史
            query: 可选的执行时任务名称关键词
            queued_from: 入队时间下界，包含该时刻；须含时区，北京时间使用 +08:00
            queued_until: 入队时间上界，不包含该时刻；须含时区
            status: 可选的执行状态
            cursor: 上一页返回的游标，其他条件须保持一致
            limit: 每页数量，默认最近二十条，范围一至一百

        Returns:
            最近执行优先的记录与下一页游标；空页仅表示当前筛选无结果，不能推断其他时间或分页

        Raises:
            BusinessException: 查询服务不可用
            ValueError: 时间范围或分页条件不合法
        """
        return await result(
            service.list_runs(
                task_id=task_id,
                query=query,
                queued_from=queued_from,
                queued_until=queued_until,
                status=status,
                cursor=cursor,
                limit=limit,
            )
        )

    @tool(parse_docstring=True, error_on_invalid_docstring=True)
    async def get_automation_run(execution_id: str) -> str:
        """读取指定执行的状态、助手回复和已交付文件，不重新运行任务

        Args:
            execution_id: list_automation_runs 或立即运行工具返回的执行 ID，不是任务 ID

        Returns:
            执行事实、回复及 outputFiles 产物；resultAvailable 为 false 表示结果尚不可读取，不代表未执行或没有文件

        Raises:
            BusinessException: 执行不存在、无权访问或查询服务不可用
            TracingError: 执行历史无法读取，不能据此声称没有执行
        """
        return await result(service.result(execution_id))

    @tool(parse_docstring=True, error_on_invalid_docstring=True)
    async def deliver_automation_files(
        execution_id: str, attachment_ids: list[str]
    ) -> list[dict[str, JsonValue]]:
        """向用户发送已保存的自动化产物，复用原文件，不重新生成或运行任务

        Args:
            execution_id: 已查询并确认的执行 ID
            attachment_ids: get_automation_run 的 outputFiles 中选定的文件 ID，一至一百个

        Returns:
            可下载的原附件引用；只在用户要求获取文件时调用

        Raises:
            BusinessException: 执行或文件不存在、无权访问或文件不属于指定执行
            ValueError: 没有选择文件或超过数量限制
        """
        try:
            files = await service.deliver_files(execution_id, attachment_ids)
        except AutomationError as error:
            raise automation_error(error) from error
        return [file.content_block() for file in files]

    return (
        create_automation,
        update_automation,
        pause_automation,
        enable_automation,
        delete_automation,
        run_automation_task_now,
        get_automation,
        list_automations,
        list_automation_runs,
        get_automation_run,
        deliver_automation_files,
    )

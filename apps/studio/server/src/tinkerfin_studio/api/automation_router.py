"""认证用户的任务管理和只读执行历史接口"""

from collections.abc import AsyncIterator
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Request

from tinkerfin_automation import ExecutionStatus, TaskStatus
from tinkerfin_automation.errors import AutomationError
from tinkerfin_studio.api.dependencies import UserContextDep
from tinkerfin_studio.api.responses import ApiResponse
from tinkerfin_studio.automation.schemas import (
    BatchCommand,
    BatchResult,
    RunDetail,
    RunList,
    RunView,
    SaveTask,
    TaskCommand,
    TaskList,
    TaskView,
)
from tinkerfin_studio.automation.service import (
    StudioAutomationService,
    automation_error,
)
from tinkerfin_studio.resources import get_resources

router = APIRouter(prefix="/automation", tags=["自动化"])


async def automation_service(
    request: Request, user: UserContextDep
) -> AsyncIterator[StudioAutomationService]:
    """将用户绑定到业务服务；框架错误统一转换为可恢复的 HTTP 错误"""
    try:
        yield StudioAutomationService(get_resources(request.app), user_id=user.user_id)
    except (AutomationError, ValueError) as error:
        raise automation_error(error) from error


Service = Annotated[StudioAutomationService, Depends(automation_service)]
Limit = Annotated[int, Query(ge=1, le=100)]
Search = Annotated[str | None, Query(max_length=255)]
Cursor = Annotated[str | None, Query(max_length=2048)]


@router.get("/tasks", response_model=ApiResponse[TaskList])
async def list_tasks(
    service: Service,
    query: Search = None,
    status: TaskStatus | None = None,
    cursor: Cursor = None,
    limit: Limit = 50,
) -> ApiResponse[TaskList]:
    """分页读取本人任务，筛选覆盖全部记录"""
    return ApiResponse.success(
        await service.list_tasks(query=query, status=status, cursor=cursor, limit=limit)
    )


@router.get("/tasks/counts", response_model=ApiResponse[dict[TaskStatus, int]])
async def task_counts(
    service: Service, query: Search = None
) -> ApiResponse[dict[TaskStatus, int]]:
    """返回名称筛选下的启用和暂停任务数量"""
    return ApiResponse.success(await service.task_counts(query=query))


@router.post("/tasks", response_model=ApiResponse[TaskView])
async def create_task(command: SaveTask, service: Service) -> ApiResponse[TaskView]:
    """保存任务并提交未来调度，重复请求不会创建第二个任务"""
    return ApiResponse.success(await service.save(command))


@router.post("/tasks/batch", response_model=ApiResponse[list[BatchResult]])
async def batch_tasks(
    command: BatchCommand, service: Service
) -> ApiResponse[list[BatchResult]]:
    """逐项暂停或删除已选任务并返回失败项"""
    return ApiResponse.success(await service.batch(command))


@router.get("/tasks/{task_id}", response_model=ApiResponse[TaskView])
async def get_task(task_id: str, service: Service) -> ApiResponse[TaskView]:
    """读取本人任务配置"""
    return ApiResponse.success(await service.get_task(task_id))


@router.put("/tasks/{task_id}", response_model=ApiResponse[TaskView])
async def update_task(
    task_id: str, command: SaveTask, service: Service
) -> ApiResponse[TaskView]:
    """按预期修订保存配置，不改写已经入队的执行"""
    return ApiResponse.success(await service.save(command, task_id=task_id))


@router.delete("/tasks/{task_id}", response_model=ApiResponse[None])
async def delete_task(
    task_id: str, command: TaskCommand, service: Service
) -> ApiResponse[None]:
    """删除任务、取消排队执行，保留运行历史及其附件"""
    await service.task_command(task_id, "delete", command)
    return ApiResponse.success()


@router.post(
    "/tasks/{task_id}/{operation}", response_model=ApiResponse[TaskView | RunView]
)
async def task_action(
    task_id: str,
    operation: Literal["pause", "enable", "run"],
    command: TaskCommand,
    service: Service,
) -> ApiResponse[TaskView | RunView]:
    """暂停、启用或立即运行；手动运行不受计划有效期限制"""
    return ApiResponse.success(await service.task_command(task_id, operation, command))


@router.get("/runs", response_model=ApiResponse[RunList])
async def list_runs(
    service: Service,
    queued_from: Annotated[datetime, Query(alias="from")],
    queued_until: Annotated[datetime, Query(alias="until")],
    query: Search = None,
    status: ExecutionStatus | None = None,
    cursor: Cursor = None,
    limit: Limit = 50,
) -> ApiResponse[RunList]:
    """按带时区的左闭右开日期范围读取执行历史"""
    return ApiResponse.success(
        await service.list_runs(
            queued_from=queued_from,
            queued_until=queued_until,
            query=query,
            status=status,
            cursor=cursor,
            limit=limit,
        )
    )


@router.get("/runs/counts", response_model=ApiResponse[dict[ExecutionStatus, int]])
async def run_counts(
    service: Service,
    queued_from: Annotated[datetime, Query(alias="from")],
    queued_until: Annotated[datetime, Query(alias="until")],
    query: Search = None,
) -> ApiResponse[dict[ExecutionStatus, int]]:
    """汇总当前日期范围和名称下全部执行状态"""
    return ApiResponse.success(
        await service.run_counts(
            queued_from=queued_from, queued_until=queued_until, query=query
        )
    )


@router.get("/runs/{execution_id}", response_model=ApiResponse[RunDetail])
async def run_result(execution_id: str, service: Service) -> ApiResponse[RunDetail]:
    """读取本人执行的只读消息、错误和文件，不提供审批恢复"""
    return ApiResponse.success(await service.result(execution_id))

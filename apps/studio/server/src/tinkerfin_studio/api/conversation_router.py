"""AG-UI 实时流、Trace 历史和会话命令 HTTP 入口"""

from datetime import datetime
from typing import Annotated, TypeAlias

from ag_ui.core import RunAgentInput
from fastapi import APIRouter, Depends, Header, Path, Query, Request
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError
from starlette.responses import Response, StreamingResponse

from tinkerfin_studio.api.dependencies import (
    ConversationCommandDep,
    ConversationHistoryDep,
    SessionDep,
    UserContextDep,
)
from tinkerfin_studio.api.errors import BusinessException, ConversationErrorCode
from tinkerfin_studio.api.responses import (
    ApiResponse,
    sse_response,
    trace_sse_response,
)
from tinkerfin_studio.conversation.repository import ConversationRepository
from tinkerfin_studio.conversation.request import ChatRequest, CompactRequest
from tinkerfin_studio.conversation.schemas import (
    CancelRunResponse,
    ConversationHistoryDetail,
    ConversationHistoryGroupConfig,
    ConversationHistoryListItem,
    ConversationHistoryListResponse,
    ConversationThreadUpdate,
    ConversationTitle,
)
from tinkerfin_studio.conversation.service import ConversationChatService
from tinkerfin_studio.conversation.trace_responses import ConversationGraphQueryPage
from tinkerfin_studio.resources import get_resources
from tinkerfin_tracing import (
    TraceGraphFilter,
    TraceGraphNodeKind,
    TraceGraphNodeStatus,
)

router = APIRouter(prefix="/conversation", tags=["会话"])

ThreadIdPath: TypeAlias = Annotated[
    str,
    Path(
        min_length=1,
        max_length=128,
        description="会话 threadId",
    ),
]


def _graph_namespace(value: str) -> tuple[str, ...]:
    if value == "root":
        return ()
    segments = tuple(value.split("|"))
    if any(not segment or segment != segment.strip() for segment in segments):
        raise ValueError("graph_namespace 必须是 root 或以 | 分隔的规范路径")
    return segments


async def _trace_graph_filter(
    kinds: Annotated[list[TraceGraphNodeKind] | None, Query(alias="kind")] = None,
    statuses: Annotated[
        list[TraceGraphNodeStatus] | None, Query(alias="status")
    ] = None,
    model_call_id: Annotated[
        str | None, Query(alias="modelCallId", min_length=1)
    ] = None,
    agents: Annotated[list[str] | None, Query(alias="agent")] = None,
    providers: Annotated[list[str] | None, Query(alias="provider")] = None,
    models: Annotated[list[str] | None, Query(alias="model")] = None,
    graph_namespaces: Annotated[
        list[str] | None, Query(alias="graph_namespace")
    ] = None,
    search: Annotated[
        str | None, Query(alias="query", min_length=1, max_length=255)
    ] = None,
    started_after: Annotated[datetime | None, Query(alias="startedAfter")] = None,
    started_before: Annotated[datetime | None, Query(alias="startedBefore")] = None,
) -> TraceGraphFilter:
    """把可读查询参数转换为框架直接过滤条件"""

    try:
        return TraceGraphFilter(
            kinds=set(kinds or ()),
            statuses=set(statuses or ()),
            model_call_id=model_call_id,
            agent_names=set(agents or ()),
            providers=set(providers or ()),
            models=set(models or ()),
            graph_namespaces={
                _graph_namespace(value) for value in graph_namespaces or ()
            },
            search=search,
            started_after=started_after,
            started_before=started_before,
        )
    except ValidationError as error:
        raise RequestValidationError(error.errors(include_input=False)) from error
    except ValueError as error:
        raise RequestValidationError(
            [
                {
                    "type": "value_error",
                    "loc": ("query", "graph_namespace"),
                    "msg": str(error),
                    "input": None,
                }
            ]
        ) from error


TraceGraphFilterDep: TypeAlias = Annotated[
    TraceGraphFilter,
    Depends(_trace_graph_filter),
]


@router.get("/history", response_model=ApiResponse[ConversationHistoryListResponse])
async def list_history(
    service: ConversationHistoryDep,
    page_size: Annotated[int, Query(alias="pageSize", ge=1, le=100)] = 20,
    cursor: Annotated[str | None, Query()] = None,
    query: Annotated[str | None, Query(max_length=255)] = None,
) -> ApiResponse[ConversationHistoryListResponse]:
    """分页返回当前用户历史会话"""

    return ApiResponse.success(
        await service.list_history(page_size=page_size, cursor=cursor, query=query)
    )


@router.get("/config", response_model=ApiResponse[ConversationHistoryGroupConfig])
async def get_conversation_config(
    service: ConversationHistoryDep,
) -> ApiResponse[ConversationHistoryGroupConfig]:
    """返回历史会话分组等前端查询配置"""

    return ApiResponse.success(service.group_config())


@router.get("/{thread_id}/title", response_model=ApiResponse[ConversationTitle])
async def get_title(
    thread_id: ThreadIdPath,
    session: SessionDep,
    user: UserContextDep,
) -> ApiResponse[ConversationTitle]:
    """读取当前用户的标题快照，聊天结束后仍可查询总结结果"""
    thread = await ConversationRepository(session).get_thread(
        user_id=user.user_id,
        thread_id=thread_id,
    )
    if thread is None:
        raise BusinessException(ConversationErrorCode.NOT_FOUND)
    return ApiResponse.success(ConversationTitle.model_validate(thread))


@router.get(
    "/{thread_id}/history", response_model=ApiResponse[ConversationHistoryDetail]
)
async def get_history(
    thread_id: ThreadIdPath,
    service: ConversationHistoryDep,
    history_cursor: Annotated[
        str | None,
        Query(alias="historyCursor", min_length=1),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
    include_task_trace: Annotated[bool, Query(alias="includeTaskTrace")] = True,
) -> Response:
    """返回一个会话的固定前缀 Trace 视图"""

    detail = await service.get_detail(
        thread_id,
        history_cursor=history_cursor,
        limit=limit,
        include_task_trace=include_task_trace,
    )
    envelope = ApiResponse[ConversationHistoryDetail].success(detail)
    content = envelope.model_dump_json(by_alias=True, exclude_none=False).encode()
    return Response(content=content, media_type="application/json")


@router.get("/{thread_id}/runs/{run_id}/events", response_class=StreamingResponse)
async def follow_run_events(
    thread_id: ThreadIdPath,
    run_id: Annotated[str, Path(min_length=1, max_length=255)],
    service: ConversationHistoryDep,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
    include_task_trace: Annotated[bool, Query(alias="includeTaskTrace")] = True,
) -> StreamingResponse:
    """恢复已有运行的可见内容并继续接收后续输出"""
    body = await service.follow_live(
        thread_id,
        run_id=run_id,
        last_event_id=last_event_id,
        include_task_trace=include_task_trace,
    )
    return sse_response(body)


@router.get("/{thread_id}/trace", response_class=StreamingResponse)
async def follow_trace(
    thread_id: ThreadIdPath,
    service: ConversationHistoryDep,
    include_task_trace: Annotated[bool, Query(alias="includeTaskTrace")] = True,
) -> StreamingResponse:
    """鉴权后先发送 Trace snapshot，再持续发送语义增量"""

    events = await service.follow_trace(
        thread_id,
        include_task_trace=include_task_trace,
    )
    return trace_sse_response(events)


@router.get(
    "/{thread_id}/trace/graph",
    response_model=ApiResponse[ConversationGraphQueryPage],
)
async def query_trace_graph(
    thread_id: ThreadIdPath,
    service: ConversationHistoryDep,
    where: TraceGraphFilterDep,
    cursor: Annotated[str | None, Query(min_length=1, max_length=16_384)] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
) -> ApiResponse[ConversationGraphQueryPage]:
    """按当前会话归属直接筛选链路节点"""

    return ApiResponse.success(
        await service.query_trace_graph(
            thread_id,
            where=where,
            cursor=cursor,
            limit=limit,
        )
    )


@router.get("/{thread_id}/trace/graph/follow", response_class=StreamingResponse)
async def follow_trace_graph(
    thread_id: ThreadIdPath,
    service: ConversationHistoryDep,
    where: TraceGraphFilterDep,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
) -> StreamingResponse:
    """发送链路筛选快照并持续跟随匹配变化"""

    events = await service.follow_trace_graph(
        thread_id,
        where=where,
        limit=limit,
    )
    return trace_sse_response(events)


@router.patch("/{thread_id}", response_model=ApiResponse[ConversationHistoryListItem])
async def update_thread(
    thread_id: ThreadIdPath,
    payload: ConversationThreadUpdate,
    service: ConversationCommandDep,
) -> ApiResponse[ConversationHistoryListItem]:
    """重命名或置顶会话"""

    return ApiResponse.success(
        await service.update(
            thread_id=thread_id,
            title=payload.title,
            pinned=payload.pinned,
        )
    )


@router.delete("/{thread_id}", response_model=ApiResponse[None])
async def delete_thread(
    thread_id: ThreadIdPath,
    service: ConversationCommandDep,
) -> ApiResponse[None]:
    """删除已终止会话的全部关联数据"""

    await service.delete(thread_id=thread_id)
    return ApiResponse.success()


@router.post("/chat", response_class=StreamingResponse)
async def chat(
    input_data: RunAgentInput,
    request: Request,
    session: SessionDep,
    user: UserContextDep,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> StreamingResponse:
    """启动或附着 durable Agent run 并返回 AG-UI SSE"""

    try:
        chat_request = ChatRequest.from_agui(input_data)
    except ValidationError as error:
        raise RequestValidationError(error.errors(include_input=False)) from error
    prepared = await ConversationChatService(
        session,
        user=user,
        resources=get_resources(request.app),
    ).start(chat_request, last_event_id=last_event_id)
    return sse_response(prepared.body)


@router.post(
    "/{thread_id}/compact",
    response_class=StreamingResponse,
)
async def compact(
    thread_id: ThreadIdPath,
    input_data: CompactRequest,
    request: Request,
    session: SessionDep,
    user: UserContextDep,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> StreamingResponse:
    """整理已有会话上下文；相同运行 ID 附着或重放原操作"""

    prepared = await ConversationChatService(
        session,
        user=user,
        resources=get_resources(request.app),
    ).start(input_data, thread_id=thread_id, last_event_id=last_event_id)
    return sse_response(prepared.body)


@router.post(
    "/{thread_id}/runs/{run_id}/cancel",
    response_model=ApiResponse[CancelRunResponse],
)
async def cancel_run(
    thread_id: ThreadIdPath,
    run_id: Annotated[str, Path(min_length=1, max_length=128)],
    request: Request,
    session: SessionDep,
    user: UserContextDep,
) -> ApiResponse[CancelRunResponse]:
    """请求取消当前用户的指定 durable run"""

    service = ConversationChatService(
        session,
        user=user,
        resources=get_resources(request.app),
    )
    return ApiResponse.success(await service.cancel(thread_id=thread_id, run_id=run_id))

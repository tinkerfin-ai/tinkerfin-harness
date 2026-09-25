"""用户归属校验后的 Trace 历史分页与实时跟随"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from collections.abc import AsyncGenerator
from datetime import datetime

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    field_validator,
)

from tinkerfin import SseBody
from tinkerfin.agui import (
    AgUiGraphQuery,
    AgUiHistory,
    AgUiHistoryView,
    AgUiReplayChannel,
    AgUiTraceGraphPage,
)
from tinkerfin_messaging import InvalidCursor as InvalidDeliveryCursor
from tinkerfin_messaging import MessagingError, RunNotFound, StreamExpired
from tinkerfin_studio.api.errors import (
    BusinessException,
    ConversationErrorCode,
    SystemException,
)
from tinkerfin_studio.conversation.failures import (
    FAILURE_PROJECTION,
    visible_run_failures,
)
from tinkerfin_studio.conversation.models import ConversationThread
from tinkerfin_studio.conversation.repository import ConversationRepository
from tinkerfin_studio.conversation.schemas import (
    ConversationHistoryDetail,
    ConversationHistoryGroupConfig,
    ConversationHistoryListItem,
    ConversationHistoryListResponse,
    ConversationRunSnapshotEvent,
    ConversationTraceErrorEvent,
    ConversationTraceGraphErrorEvent,
    ConversationTraceGraphSnapshotEvent,
    ConversationTraceGraphUpdateEvent,
    ConversationTraceSnapshotEvent,
    ConversationTraceUpdateEvent,
    PendingInteractionKind,
)
from tinkerfin_studio.conversation.todo_groups import (
    TaskTraceQueryTimeout,
    TaskTraceSnapshot,
    TodoGroupProjector,
    TodoGroupQueryExecutor,
)
from tinkerfin_tracing import (
    InvalidTraceCursor,
    TraceGraphFilter,
    Tracer,
    TraceThread,
    TraceThreadNotFound,
    TracingError,
)

_HISTORY_PAGE_SIZE_MAX = 100
_HISTORY_DAY_RANGES = (7, 30)
logger = logging.getLogger(__name__)
_PENDING_KIND = TypeAdapter(PendingInteractionKind | None)


class _HistoryCursorPayload(BaseModel):
    """历史 keyset 游标的严格边界数据"""

    model_config = ConfigDict(extra="forbid", strict=True, populate_by_name=True)

    pinned: bool = Field(description="游标行是否置顶")
    updated_at: datetime = Field(
        alias="updatedAt", description="游标行的无时区最近 Trace 活动时间"
    )
    row_id: int = Field(alias="id", gt=0, description="游标行数据库主键")
    query: str | None = Field(
        default=None,
        max_length=255,
        description="游标所属的规范化标题查询词",
    )

    @field_validator("updated_at")
    @classmethod
    def updated_at_is_naive(cls, value: datetime) -> datetime:
        """拒绝不能与数据库无时区字段直接比较的时间"""

        if value.utcoffset() is not None:
            raise ValueError("updatedAt 必须是不含时区的 ISO 时间")
        return value


def _history_item(thread: ConversationThread) -> ConversationHistoryListItem:
    return ConversationHistoryListItem(
        id=thread.id,
        threadId=thread.thread_id,
        title=thread.title,
        titleSource=thread.title_source,
        titleGenerationStatus=thread.title_generation_status,
        titleSeq=thread.title_seq,
        status=thread.status,
        lastRunId=thread.last_run_id,
        lastModel=thread.last_model,
        accessMode=thread.last_access_mode,
        messageCount=thread.message_count,
        toolCallCount=thread.tool_call_count,
        hasPendingInterrupt=thread.has_pending_interrupt,
        pendingInteractionKind=_PENDING_KIND.validate_python(
            thread.pending_interaction_kind
        ),
        pinned=thread.pinned,
        createdAt=thread.created_at,
        updatedAt=thread.updated_at,
    )


class ConversationHistoryService:
    """从 Studio 读取归属与摘要，从 Trace 读取唯一会话正文"""

    def __init__(
        self,
        repository: ConversationRepository,
        *,
        user_id: int,
        tracer: Tracer,
        todo_group_query: TodoGroupQueryExecutor,
        conversation_channel: AgUiReplayChannel | None = None,
    ) -> None:
        self._repository = repository
        self._user_id = user_id
        # 读取源与用户作用域在此绑定，快照、分页和跟随均使用同一会话身份
        self._history = AgUiHistory(tracer, namespace=f"ns_{user_id}")
        self._todo_group_query = todo_group_query
        self._conversation_channel = conversation_channel

    async def list_history(
        self,
        *,
        page_size: int,
        cursor: str | None,
        query: str | None = None,
    ) -> ConversationHistoryListResponse:
        """按标题查询、置顶和最近 Trace 活动时间稳定分页"""

        resolved_query = (query.strip() or None) if query is not None else None
        resolved = self._decode_cursor(cursor, query=resolved_query)
        resolved_page_size = min(max(page_size, 1), _HISTORY_PAGE_SIZE_MAX)
        threads = await self._repository.list_threads(
            user_id=self._user_id,
            page_size=resolved_page_size,
            cursor=resolved,
            query=resolved_query,
        )
        has_more = len(threads) > resolved_page_size
        page = threads[:resolved_page_size]
        next_cursor = (
            self._encode_cursor(page[-1], query=resolved_query)
            if has_more and page
            else None
        )
        return ConversationHistoryListResponse(
            items=[_history_item(thread) for thread in page],
            nextCursor=next_cursor,
        )

    @staticmethod
    def group_config() -> ConversationHistoryGroupConfig:
        """返回前端按本地自然日分组使用的服务端范围"""

        return ConversationHistoryGroupConfig(dayRanges=list(_HISTORY_DAY_RANGES))

    async def get_detail(
        self,
        thread_id: str,
        *,
        history_cursor: str | None = None,
        limit: int = 100,
        include_task_trace: bool = True,
    ) -> ConversationHistoryDetail:
        """返回一个固定 as-of、可继续向前扩展的 Trace 视图"""

        thread, history = await self._load_trace(
            thread_id,
            history_cursor=history_cursor,
            limit=limit,
        )
        trace = history.trace
        projector: TodoGroupProjector | None = None
        try:
            task_trace = None
            if include_task_trace:
                projector = await self._project_task_trace(trace)
                task_trace = projector.snapshot(
                    status=trace.status,
                    completeness=trace.completeness,
                )
            detail = await self._detail(
                thread=thread,
                history=history,
                task_trace=task_trace,
            )
            await self._repository.commit()
            return detail
        finally:
            if projector is not None:
                projector.close()

    async def follow_live(
        self,
        thread_id: str,
        *,
        run_id: str,
        last_event_id: str | None,
        include_task_trace: bool = True,
    ) -> SseBody[bytes]:
        """读取已授权运行的历史基线并续播，不重新执行模型或业务登记"""
        thread = await self._require_thread(thread_id)
        registration = await self._repository.get_run(
            thread_pk=thread.id, run_id=run_id
        )
        if registration is None:
            raise BusinessException(ConversationErrorCode.RUN_NOT_FOUND)
        await self._repository.commit()
        if self._conversation_channel is None:
            raise RuntimeError("未配置会话续播通道")
        try:
            live = await self._history.open_live(
                thread_id,
                head_run_id=run_id,
                channel=self._conversation_channel,
                last_event_id=last_event_id,
                projections=(FAILURE_PROJECTION,),
            )
        except InvalidDeliveryCursor as error:
            raise BusinessException(
                ConversationErrorCode.INVALID_LAST_EVENT_ID
            ) from error
        except RunNotFound as error:
            raise BusinessException(ConversationErrorCode.RUN_NOT_FOUND) from error
        except StreamExpired as error:
            raise BusinessException(
                ConversationErrorCode.MESSAGING_STREAM_EXPIRED
            ) from error
        except MessagingError as error:
            raise SystemException(
                ConversationErrorCode.MESSAGING_UNAVAILABLE
            ) from error
        except ValueError as error:
            raise BusinessException(
                ConversationErrorCode.INVALID_LAST_EVENT_ID
            ) from error
        except TracingError as error:
            raise SystemException(ConversationErrorCode.TRACE_UNAVAILABLE) from error

        snapshot: ConversationRunSnapshotEvent | None = None
        body: SseBody[bytes]

        async def prepare_snapshot() -> None:
            nonlocal snapshot
            if last_event_id is not None:
                return
            projector = (
                await self._project_task_trace(live.history.trace)
                if include_task_trace
                else None
            )
            try:
                task_trace = (
                    projector.snapshot(
                        status=live.history.trace.status,
                        completeness=live.history.trace.completeness,
                    )
                    if projector is not None
                    else None
                )
                detail = await self._detail(
                    thread=thread, history=live.history, task_trace=task_trace
                )
                snapshot = ConversationRunSnapshotEvent(
                    snapshot=detail, replay=live.body is not None
                )
                await self._repository.commit()
            finally:
                if projector is not None:
                    projector.close()

        async def iterate() -> AsyncGenerator[bytes, None]:
            if snapshot is not None:
                yield (
                    b"event: snapshot\ndata: "
                    + snapshot.model_dump_json(by_alias=True).encode()
                    + b"\n\n"
                )
            if live.body is not None:
                async for chunk in live.body:
                    yield chunk

        body = SseBody(source_factory=iterate, close=live.aclose)
        await body.prepare(preflight=prepare_snapshot)
        return body

    async def follow_trace(
        self,
        thread_id: str,
        *,
        include_task_trace: bool = True,
    ) -> AsyncGenerator[
        ConversationTraceSnapshotEvent
        | ConversationTraceUpdateEvent
        | ConversationTraceErrorEvent,
        None,
    ]:
        """先返回权威快照，再按框架顺序跟随同一 generation 的语义增量"""

        thread, history = await self._load_trace(
            thread_id,
            history_cursor=None,
            limit=100,
        )
        trace = history.trace
        projector: TodoGroupProjector | None = None
        try:
            task_trace = None
            if include_task_trace:
                projector = await self._project_task_trace(trace)
                task_trace = projector.snapshot(
                    status=trace.status,
                    completeness=trace.completeness,
                )
            detail = await self._detail(
                thread=thread,
                history=history,
                task_trace=task_trace,
            )
            # 归属和 Run 配置已固定到 snapshot；长流开始前归还业务连接
            await self._repository.commit()
        except BaseException:
            if projector is not None:
                projector.close()
            raise

        async def events() -> AsyncGenerator[
            ConversationTraceSnapshotEvent
            | ConversationTraceUpdateEvent
            | ConversationTraceErrorEvent,
            None,
        ]:
            updates = history.follow()
            last_revision = projector.revision if projector is not None else 0
            last_task_trace = task_trace
            user_runs = {
                item.id: item.run_id
                for item in trace.messages
                if item.role == "user" and not item.graph_namespace
            }
            last_status = trace.status
            last_completeness = trace.completeness
            try:
                async with updates:
                    yield ConversationTraceSnapshotEvent(snapshot=detail)
                    async for update in updates:
                        for message_id in update.messages.removes:
                            user_runs.pop(message_id, None)
                        for item in update.messages.upserts:
                            if item.role == "user" and not item.graph_namespace:
                                user_runs[item.id] = item.run_id
                        task_trace_update = None
                        if projector is not None:
                            for event in update.events:
                                projector.consume(event)
                            if (
                                projector.revision != last_revision
                                or update.status != last_status
                                or update.completeness != last_completeness
                            ):
                                candidate = projector.snapshot(
                                    status=update.status,
                                    completeness=update.completeness,
                                )
                                last_revision = projector.revision
                                last_status = update.status
                                last_completeness = update.completeness
                                if candidate != last_task_trace:
                                    task_trace_update = candidate
                                    last_task_trace = candidate
                        yield ConversationTraceUpdateEvent(
                            update=update,
                            runFailures=visible_run_failures(
                                update.projections[FAILURE_PROJECTION],
                                set(user_runs.values()),
                            ),
                            taskTrace=task_trace_update,
                        )
            except asyncio.CancelledError:
                raise
            except TracingError as error:
                logger.error(
                    "Trace follow 异常结束: thread_id=%s",
                    thread.thread_id,
                    exc_info=(type(error), error, error.__traceback__),
                )
                yield ConversationTraceErrorEvent()
            finally:
                if projector is not None:
                    projector.close()

        return events()

    async def query_trace_graph(
        self,
        thread_id: str,
        *,
        where: TraceGraphFilter,
        cursor: str | None,
        limit: int,
    ) -> AgUiTraceGraphPage:
        """在框架 Store 内筛选当前会话链路节点"""

        query = await self._load_graph_query(
            thread_id,
            where=where,
            cursor=cursor,
            limit=limit,
        )
        return query.snapshot

    async def follow_trace_graph(
        self,
        thread_id: str,
        *,
        where: TraceGraphFilter,
        limit: int,
    ) -> AsyncGenerator[
        ConversationTraceGraphSnapshotEvent
        | ConversationTraceGraphUpdateEvent
        | ConversationTraceGraphErrorEvent,
        None,
    ]:
        """先发送当前筛选页，再跟随同一 generation 的链路变化"""

        query = await self._load_graph_query(
            thread_id,
            where=where,
            cursor=None,
            limit=limit,
        )

        async def events() -> AsyncGenerator[
            ConversationTraceGraphSnapshotEvent
            | ConversationTraceGraphUpdateEvent
            | ConversationTraceGraphErrorEvent,
            None,
        ]:
            try:
                async with query.follow() as updates:
                    yield ConversationTraceGraphSnapshotEvent(snapshot=query.snapshot)
                    async for update in updates:
                        yield ConversationTraceGraphUpdateEvent(update=update)
            except asyncio.CancelledError:
                raise
            except TracingError as error:
                logger.error(
                    "链路跟随异常结束: thread_id=%s",
                    thread_id,
                    exc_info=(type(error), error, error.__traceback__),
                )
                yield ConversationTraceGraphErrorEvent()

        return events()

    async def _load_graph_query(
        self,
        thread_id: str,
        *,
        where: TraceGraphFilter,
        cursor: str | None,
        limit: int,
    ) -> AgUiGraphQuery:
        """校验会话归属并在释放业务连接后查询框架索引"""

        thread = await self._require_thread(thread_id)
        head_run_id = thread.last_run_id
        if head_run_id is None:
            raise SystemException(ConversationErrorCode.TRACE_UNAVAILABLE)
        await self._repository.commit()
        try:
            return await self._history.query(
                thread.thread_id,
                where=where,
                head_run_id=head_run_id,
                cursor=cursor,
                limit=limit,
            )
        except InvalidTraceCursor as error:
            raise BusinessException(ConversationErrorCode.INVALID_CURSOR) from error
        except (TraceThreadNotFound, TracingError) as error:
            raise SystemException(ConversationErrorCode.TRACE_UNAVAILABLE) from error

    async def _load_trace(
        self,
        thread_id: str,
        *,
        history_cursor: str | None,
        limit: int,
    ) -> tuple[ConversationThread, AgUiHistoryView]:
        """释放业务事务后读取组件库中的运行轨迹"""

        thread = await self._require_thread(thread_id)
        head_run_id = thread.last_run_id
        if head_run_id is None:
            raise SystemException(ConversationErrorCode.TRACE_UNAVAILABLE)
        # 先结束归属查询，避免读取轨迹期间继续占用业务库连接
        await self._repository.commit()
        try:
            history = await self._history.get(
                thread.thread_id,
                head_run_id=None if history_cursor is not None else head_run_id,
                history_cursor=history_cursor,
                projections=(FAILURE_PROJECTION,),
                limit=min(max(limit, 1), _HISTORY_PAGE_SIZE_MAX),
            )
        except InvalidTraceCursor as error:
            raise BusinessException(ConversationErrorCode.INVALID_CURSOR) from error
        except (TraceThreadNotFound, TracingError) as error:
            raise SystemException(ConversationErrorCode.TRACE_UNAVAILABLE) from error
        return thread, history

    async def _project_task_trace(
        self,
        trace: TraceThread,
    ) -> TodoGroupProjector:
        """把任务轨迹读取失败映射为现有 Trace 不可用错误"""

        try:
            return await self._todo_group_query.project(trace)
        except (TaskTraceQueryTimeout, TracingError) as error:
            raise SystemException(ConversationErrorCode.TRACE_UNAVAILABLE) from error

    async def _detail(
        self,
        *,
        thread: ConversationThread,
        history: AgUiHistoryView,
        task_trace: TaskTraceSnapshot | None,
    ) -> ConversationHistoryDetail:
        snapshot = history.snapshot
        trace = history.trace
        registration = await self._repository.get_run(
            thread_pk=thread.id,
            run_id=snapshot.head_run_id,
        )
        if registration is None:
            raise SystemException(ConversationErrorCode.TRACE_UNAVAILABLE)
        summary = snapshot.summary
        interactions = {item.id: item for item in snapshot.interactions}
        # Pending 项必须跨越可见 Turn 窗口保留，避免历史分页隐藏仍需用户处理的交互
        interactions.update({item.id: item for item in summary.pending_interactions})
        return ConversationHistoryDetail(
            id=thread.id,
            threadId=thread.thread_id,
            title=thread.title,
            titleSource=thread.title_source,
            titleGenerationStatus=thread.title_generation_status,
            titleSeq=thread.title_seq,
            lastModel=registration.model_id,
            accessMode=registration.access_mode,
            pinned=thread.pinned,
            asOfSeq=snapshot.as_of_seq,
            generation=snapshot.generation,
            observedAt=snapshot.observed_at,
            headRunId=snapshot.head_run_id,
            availableHeads=snapshot.available_heads,
            historyCursor=snapshot.history_cursor,
            messageCount=summary.message_count,
            toolCallCount=summary.tool_call_count,
            messages=snapshot.messages,
            runFailures=visible_run_failures(
                trace.projections[FAILURE_PROJECTION],
                {
                    item.run_id
                    for item in snapshot.messages
                    if item.role == "user" and not item.graph_namespace
                },
            ),
            reasoning=snapshot.reasoning,
            graph=snapshot.graph,
            state=snapshot.state,
            interactions=tuple(
                sorted(
                    interactions.values(),
                    key=lambda item: (item.trace_seq, item.id),
                )
            ),
            status=summary.status,
            completeness=summary.completeness,
            taskTrace=task_trace,
            createdAt=thread.created_at,
            updatedAt=thread.updated_at,
        )

    async def _require_thread(self, thread_id: str) -> ConversationThread:
        thread = await self._repository.get_thread(
            user_id=self._user_id,
            thread_id=thread_id,
        )
        if thread is None:
            raise BusinessException(ConversationErrorCode.NOT_FOUND)
        return thread

    @staticmethod
    def _encode_cursor(
        thread: ConversationThread,
        *,
        query: str | None = None,
    ) -> str:
        payload = json.dumps(
            {
                "pinned": thread.pinned,
                "updatedAt": thread.updated_at.isoformat(),
                "id": thread.id,
                "query": query,
            },
            separators=(",", ":"),
        )
        return base64.urlsafe_b64encode(payload.encode()).decode()

    @staticmethod
    def _decode_cursor(
        value: str | None,
        *,
        query: str | None = None,
    ) -> tuple[bool, datetime, int] | None:
        if value is None:
            return None
        try:
            decoded = base64.b64decode(
                value.encode(),
                altchars=b"-_",
                validate=True,
            )
            payload = _HistoryCursorPayload.model_validate_json(decoded, strict=True)
            if payload.query != query:
                raise ValueError("游标查询词与当前请求不一致")
            return payload.pinned, payload.updated_at, payload.row_id
        except (ValueError, TypeError, ValidationError):
            raise BusinessException(ConversationErrorCode.INVALID_CURSOR) from None


__all__ = ["ConversationHistoryService"]

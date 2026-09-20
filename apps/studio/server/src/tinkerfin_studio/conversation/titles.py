"""使用会话模型完成一次标题总结，不持有连接或事件流"""

from __future__ import annotations

import asyncio
import json
import logging
import unicodedata

import anyio
import httpx
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from tinkerfin_studio.conversation.repository import ConversationRepository
from tinkerfin_studio.conversation.schemas import ConversationTitle
from tinkerfin_studio.infrastructure.database import Database
from tinkerfin_studio.models.chat import create_chat_model
from tinkerfin_studio.models.schemas import AgentModelConfig

logger = logging.getLogger(__name__)
_TITLE_TIMEOUT_SECONDS = 60
_TITLE_MAX_INPUT_BYTES = 4096
_TITLE_MAX_OUTPUT_TOKENS = 256
_TITLE_CAPACITY = anyio.CapacityLimiter(4)
_TITLE_CLEANUP_SECONDS = 5
_TITLE_MAX_PENDING = 64


def _title_prompt(text: str) -> str:
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if (
            len(json.dumps(text[:middle], ensure_ascii=False).encode())
            <= _TITLE_MAX_INPUT_BYTES
        ):
            low = middle
        else:
            high = middle - 1
    return json.dumps(text[:low], ensure_ascii=False)


async def summarize_conversation_title(
    *,
    database: Database,
    thread_pk: int,
    text: str,
    model: BaseChatModel,
) -> ConversationTitle | None:
    """认领一次总结，只返回成功提交且未被用户覆盖的标题

    Args:
        database: 应用数据库，模型调用期间不持有连接
        thread_pk: 已完成用户归属校验的会话主键
        text: 本次普通提问的用户文本，空文本不消耗总结机会
        model: 当前对话所选模型，调用方构建时关闭推理和 SDK 重试

    Returns:
        已提交的标题快照；无需总结、失败或已手动命名时为 None

    Raises:
        asyncio.CancelledError: 标题任务取消，结算后继续传播
    """
    if not text.strip():
        return None
    claimed = False

    async def claim() -> bool:
        nonlocal claimed
        # 认领提交独立完成后再判断所有权，避免取消时误结算另一个请求的认领
        async with asyncio.timeout(min(_TITLE_TIMEOUT_SECONDS, _TITLE_CLEANUP_SECONDS)):
            async with database.session() as session:
                repository = ConversationRepository(session)
                won = await repository.claim_title(thread_pk)
                await repository.commit()
                claimed = won
                return won

    claim_task: asyncio.Task[bool] | None = None
    saved = False
    try:
        async with asyncio.timeout(_TITLE_TIMEOUT_SECONDS):
            claim_task = asyncio.create_task(claim(), name="conversation-title-claim")
            claimed = await asyncio.shield(claim_task)
            if not claimed:
                return None
            async with _TITLE_CAPACITY:
                async with database.session() as session:
                    thread = await ConversationRepository(session).get_thread_by_pk(
                        thread_pk
                    )
                    if (
                        thread is None
                        or thread.title_source != "default"
                        or thread.title_generation_status != "running"
                        or thread.status == "deleting"
                    ):
                        return None
                result = await model.ainvoke(
                    [
                        SystemMessage(
                            content="根据用户输入生成简短会话标题。使用输入的语言，中文约10个字，其他语言约5个单词。只返回一行自然语言标题，不加引号、解释、Markdown或代码。"
                        ),
                        HumanMessage(content=_title_prompt(text.strip())),
                    ],
                    max_tokens=_TITLE_MAX_OUTPUT_TOKENS,
                )
                if (
                    not isinstance(result, AIMessage)
                    or result.tool_calls
                    or result.response_metadata.get("finish_reason")
                    in {"length", "tool_calls"}
                ):
                    raise ValueError("标题模型未返回完整文本")
                title = " ".join(
                    "".join(
                        char
                        for char in result.text
                        if char.isspace()
                        or unicodedata.category(char) not in {"Cc", "Cf"}
                    ).split()
                ).strip("\"'` ")
                title = title[:32]
                if not title:
                    raise ValueError("标题模型返回空文本")
                async with database.session() as session:
                    repository = ConversationRepository(session)
                    updated = await repository.finish_title(thread_pk, title)
                    await repository.commit()
                    saved = updated
                    if saved:
                        thread = await repository.get_thread_by_pk(thread_pk)
                        if thread is not None and thread.title_source == "generated":
                            return ConversationTitle.model_validate(thread)
    except Exception as error:  # noqa: BLE001 - 辅助模型失败不影响主回复，不记录敏感异常正文
        logger.warning(
            "会话标题总结失败 thread=%s reason=%s", thread_pk, type(error).__name__
        )
    finally:

        async def settle_title() -> BaseException | None:
            nonlocal claimed
            # 认领和失败写回属于同一次标题尝试；先确认认领提交，才能结算该记录
            try:
                if claim_task is not None:
                    try:
                        claimed = await claim_task
                    except Exception:  # noqa: BLE001 - 提交不明时不修改可能属于其他请求的记录
                        pass
                if claimed and not saved:
                    try:
                        async with asyncio.timeout(_TITLE_CLEANUP_SECONDS):
                            async with database.session() as session:
                                repository = ConversationRepository(session)
                                await repository.finish_title(thread_pk, None)
                                await repository.commit()
                    except Exception as error:  # noqa: BLE001 - 辅助失败不覆盖主回复，不输出敏感正文
                        logger.warning(
                            "会话标题结算失败 thread=%s reason=%s",
                            thread_pk,
                            type(error).__name__,
                        )
            except BaseException as error:  # noqa: BLE001 - 交回调用方，避免清理任务泄漏控制异常
                return error
            return None

        settlement = asyncio.create_task(
            settle_title(), name="conversation-title-settlement"
        )
        cancellation: asyncio.CancelledError | None = None
        with anyio.CancelScope(shield=True):
            while not settlement.done():
                try:
                    await asyncio.shield(settlement)
                except asyncio.CancelledError as error:
                    cancellation = cancellation or error
        failure = settlement.result()
        if failure is not None:
            if cancellation is not None and isinstance(failure, Exception):
                try:
                    raise failure
                except BaseException:  # noqa: BLE001 - 保留调用者取消与清理失败
                    raise cancellation
            raise failure
        if cancellation is not None:
            raise cancellation
    return None


class ConversationTitles:
    """应用拥有标题任务；聊天结束和断连不取消，应用关闭时取消并结算

    至多保留 64 个任务，每项输入最多 4096 字符；模型并发与总超时由总结函数限制。
    借用的数据库与模型客户端必须在本对象关闭之后释放。
    """

    def __init__(
        self,
        *,
        database: Database,
        http_client: httpx.AsyncClient,
        http_transport: httpx.AsyncBaseTransport | None,
    ) -> None:
        self._database = database
        self._http_client = http_client
        self._http_transport = http_transport
        self._tasks: dict[int, asyncio.Task[None]] = {}
        self._closed = False

    async def start(
        self, *, thread_pk: int, text: str, model: AgentModelConfig
    ) -> None:
        """为已受理的会话安排一次总结，重复请求不重复排队"""
        if not text.strip() or thread_pk in self._tasks:
            return
        if self._closed or len(self._tasks) >= _TITLE_MAX_PENDING:
            await self._fail_unstarted(thread_pk)
            return
        task = asyncio.create_task(
            self._generate(thread_pk, text[:_TITLE_MAX_INPUT_BYTES], model),
            name=f"conversation-title:{thread_pk}",
        )
        self._tasks[thread_pk] = task

        def finished(completed: asyncio.Task[None]) -> None:
            self._tasks.pop(thread_pk, None)
            if (
                not completed.cancelled()
                and (error := completed.exception()) is not None
            ):
                logger.warning(
                    "会话标题任务失败 thread=%s reason=%s",
                    thread_pk,
                    type(error).__name__,
                )

        task.add_done_callback(finished)

    async def _fail_unstarted(self, thread_pk: int) -> None:
        try:
            async with asyncio.timeout(_TITLE_CLEANUP_SECONDS):
                async with self._database.session() as session:
                    repository = ConversationRepository(session)
                    if await repository.claim_title(thread_pk):
                        await repository.finish_title(thread_pk, None)
                    await repository.commit()
        except Exception as error:  # noqa: BLE001 - 标题失败不影响主回复
            logger.warning(
                "会话标题排队失败 thread=%s reason=%s", thread_pk, type(error).__name__
            )

    async def _generate(
        self, thread_pk: int, text: str, config: AgentModelConfig
    ) -> None:
        try:
            model = create_chat_model(
                config,
                reasoning_enabled=False,
                max_retries=0,
                max_tokens=_TITLE_MAX_OUTPUT_TOKENS,
                timeout=_TITLE_TIMEOUT_SECONDS,
                http_async_client=self._http_client,
                http_async_transport=self._http_transport,
            )
        except Exception as error:  # noqa: BLE001 - 模型配置失败保留临时标题
            logger.warning(
                "会话标题模型创建失败 thread=%s reason=%s",
                thread_pk,
                type(error).__name__,
            )
            await self._fail_unstarted(thread_pk)
            return
        await summarize_conversation_title(
            database=self._database,
            thread_pk=thread_pk,
            text=text,
            model=model,
        )

    async def aclose(self) -> None:
        """停止接收任务，并等待所有标题取消后的数据库结算"""
        self._closed = True
        pending = tuple(self._tasks.items())
        tasks = tuple(task for _, task in pending)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        # 尚未取得执行机会的任务也必须结算，不能把 idle 留给客户端无限查询
        for thread_pk, _ in pending:
            await self._fail_unstarted(thread_pk)

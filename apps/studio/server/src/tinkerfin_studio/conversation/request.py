"""AG-UI chat 请求的 Studio 协议边界"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal, cast

from ag_ui.core import RunAgentInput
from ag_ui.core.types import (
    Context,
    ResumeEntry,
    Tool,
)
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    field_validator,
    model_validator,
)

from tinkerfin import AgUiUserInput
from tinkerfin_studio.agent.access import AccessMode
from tinkerfin_studio.api.errors import BusinessException, ConversationErrorCode

MAX_USER_MESSAGE_BYTES = 256 * 1024


def _utf8_byte_length(value: str) -> int:
    try:
        return len(value.encode("utf-8"))
    except UnicodeEncodeError as error:
        raise ValueError("用户消息必须是有效 UTF-8 文本") from error


class ConversationCommand(BaseModel):
    """前端声明的一次运行命令集合"""

    model_config = ConfigDict(extra="allow")

    plan: Literal["on", "off"] = Field(description="本次运行期望的 Plan 状态")


class ConversationForwardedProps(BaseModel):
    """前端传给一次 run 的扩展属性"""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    access_mode: AccessMode = Field(
        default="full",
        alias="accessMode",
        description="写文件工具是否需要人工审批",
    )

    model: str = Field(min_length=1, max_length=64, description="数据库模型稳定 ID")
    command: ConversationCommand = Field(
        description="本次运行的命令映射；未知命令仅透传，不由当前业务执行"
    )

    @model_validator(mode="before")
    @classmethod
    def reject_removed_mode(cls, value: object) -> object:
        """拒绝已从当前传输契约删除的 mode 字段"""

        if isinstance(value, Mapping) and "mode" in value:
            raise ValueError("forwardedProps.mode 已删除，请使用 command.plan")
        return value

    @property
    def agent_mode(self) -> Literal["default", "plan"]:
        """把传输命令转换为框架需要的运行模式"""

        return "plan" if self.command.plan == "on" else "default"


class ChatRequest(BaseModel):
    """完成 AG-UI 协议校验并移除客户端消息 ID 的 Studio 请求"""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    thread_id: str = Field(
        alias="threadId",
        default="",
        max_length=128,
        description="现有会话的稳定 thread ID；新会话可为空",
    )
    run_id: str = Field(
        alias="runId", min_length=1, max_length=128, description="当前主 run ID"
    )
    parent_run_id: str | None = Field(
        default=None,
        alias="parentRunId",
        min_length=1,
        max_length=128,
        description="标准 AG-UI 父 run ID",
    )
    state: JsonValue = Field(description="客户端状态快照，仅持久化和透传")
    messages: list[dict[str, JsonValue]] = Field(
        description="保持标准角色、内容和扩展字段但不含客户端消息 ID"
    )
    tools: list[Tool] = Field(description="客户端工具定义，仅持久化和透传")
    context: list[Context] = Field(description="AG-UI 上下文，仅持久化和透传")
    forwarded_props: ConversationForwardedProps = Field(
        alias="forwardedProps", description="模型与前端扩展属性"
    )
    resume: list[ResumeEntry] | None = Field(default=None, description="HITL 恢复条目")

    @field_validator("thread_id", "run_id", "parent_run_id")
    @classmethod
    def identifiers_are_canonical(cls, value: str | None) -> str | None:
        """拒绝后续技术边界不会接受的首尾空白身份"""

        if value is not None and value != value.strip():
            raise ValueError("身份字段不得包含首尾空白")
        return value

    @field_validator("messages")
    @classmethod
    def messages_exclude_client_ids(
        cls,
        value: list[dict[str, JsonValue]],
    ) -> list[dict[str, JsonValue]]:
        """客户端消息 ID 只在 HTTP 协议边界使用，不进入业务身份"""

        if any("id" in message for message in value):
            raise ValueError("ChatRequest.messages 不得保留客户端消息 ID")
        return value

    @model_validator(mode="after")
    def parent_is_a_distinct_run(self) -> ChatRequest:
        """拒绝无法形成分支的自引用 parentRunId"""

        if self.parent_run_id == self.run_id:
            raise ValueError("parentRunId 必须与 runId 不同")
        return self

    @model_validator(mode="after")
    def messages_follow_the_current_delta_contract(self) -> ChatRequest:
        """限制为一条文字及附件增量，或无消息的审批恢复"""

        if self.resume is not None:
            if self.messages:
                raise ValueError("恢复运行不得同时提交新消息")
            return self
        if len(self.messages) != 1 or self.messages[0].get("role") != "user":
            raise ValueError("普通运行必须且只能提交一条 user 消息")
        content = self.messages[0].get("content")
        if isinstance(content, str):
            texts = [content]
            attachment_ids: list[str] = []
        elif isinstance(content, list):
            texts = []
            attachment_ids = []
            for block in content:
                if not isinstance(block, dict):
                    raise ValueError("消息内容块必须是对象")  # noqa: TRY004 - Pydantic 边界需要返回校验错误
                if block.get("type") == "text" and isinstance(block.get("text"), str):
                    texts.append(str(block["text"]))
                elif block.get("type") in {"image", "document"}:
                    source = block.get("source")
                    if not isinstance(source, dict) or source.get("type") != "url":
                        raise ValueError("附件只能使用服务端稳定引用")
                    value = source.get("value")
                    if (
                        not isinstance(value, str)
                        or not value.startswith("attachment:")
                        or not value.removeprefix("attachment:")
                    ):
                        raise ValueError("附件引用不合法")
                    attachment_ids.append(value.removeprefix("attachment:"))
                else:
                    raise ValueError("消息内容类型不支持")
        else:
            raise ValueError("用户消息必须包含文字或附件")  # noqa: TRY004 - Pydantic 边界需要返回校验错误
        if not any(text.strip() for text in texts) and not attachment_ids:
            raise ValueError("用户消息必须包含文字或附件")
        if len(attachment_ids) > 5 or len(set(attachment_ids)) != len(attachment_ids):
            raise ValueError("最多提交 5 个不同附件")
        if sum(_utf8_byte_length(text) for text in texts) > MAX_USER_MESSAGE_BYTES:
            raise ValueError("用户消息超过当前 UTF-8 字节上限")
        return self

    @property
    def user_input(self) -> AgUiUserInput:
        """读取文字及附件引用；附件权限由当前用户的仓储查询确认"""

        if self.resume is not None:
            raise ValueError("恢复请求没有新的用户输入")
        return AgUiUserInput.model_validate(self.messages[0])

    @classmethod
    def from_agui(cls, value: RunAgentInput) -> ChatRequest:
        """保留标准 AG-UI 数据并增加 Studio 必需的业务校验"""

        if not isinstance(value, RunAgentInput):
            raise TypeError("value 必须是 RunAgentInput")
        if value.resume is None and len(value.messages) == 1:
            content = value.messages[0].content
            if isinstance(content, str):
                try:
                    content_size = _utf8_byte_length(content)
                except ValueError:
                    content_size = None
                if content_size is not None and content_size > MAX_USER_MESSAGE_BYTES:
                    raise BusinessException(ConversationErrorCode.REQUEST_TOO_LARGE)
        payload = value.model_dump(mode="json", by_alias=True, exclude_none=False)
        payload["messages"] = [
            message.model_dump(
                mode="json",
                by_alias=True,
                exclude={"id"},
                exclude_none=False,
            )
            for message in value.messages
        ]
        return cls.model_validate(payload)

    def normalized_json(
        self,
        *,
        thread_id: str,
        message_ids: tuple[str, ...],
    ) -> dict[str, JsonValue]:
        """返回绑定服务端会话与权威消息 ID 的标准请求快照"""

        if len(message_ids) != len(self.messages) or any(
            not value for value in message_ids
        ):
            raise ValueError("message_ids 必须完整覆盖本次用户输入")

        payload = self.model_dump(mode="json", by_alias=True)
        payload["threadId"] = thread_id
        payload["messages"] = [
            {
                "id": message_id,
                **message,
            }
            for message_id, message in zip(message_ids, self.messages, strict=True)
        ]
        normalized = RunAgentInput.model_validate(payload)
        return cast(
            dict[str, JsonValue],
            normalized.model_dump(
                mode="json",
                by_alias=True,
                exclude_none=False,
            ),
        )

"""会话任务轨迹的当前 HTTP 边界模型"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic.alias_generators import to_camel

TodoGroupStatus: TypeAlias = Literal[
    "running",
    "completed",
    "incomplete",
    "failed",
    "cancelled",
]
TodoTraceItemStatus: TypeAlias = Literal[
    "pending",
    "running",
    "completed",
    "incomplete",
    "failed",
    "cancelled",
]
TaskTraceErrorCode: TypeAlias = Literal[
    "trace_incomplete",
    "todo_state_omitted",
    "todo_state_invalid",
]


class _TaskTraceModel(BaseModel):
    """统一任务轨迹的严格 camelCase 序列化边界"""

    model_config = ConfigDict(
        alias_generator=to_camel,
        extra="forbid",
        frozen=True,
        populate_by_name=True,
        strict=True,
    )


class TodoTraceItem(_TaskTraceModel):
    """展示根任务清单；incomplete 表示本轮已结束但未确认该项完成"""

    id: str = Field(min_length=1, max_length=2048)
    content: str = Field(min_length=1)
    status: TodoTraceItemStatus

    @field_validator("id")
    @classmethod
    def id_is_canonical(cls, value: str) -> str:
        """拒绝会产生两个展示身份的空白别名"""

        if value != value.strip():
            raise ValueError("Todo ID 必须是无首尾空白的非空文本")
        return value

    @field_validator("content")
    @classmethod
    def content_is_visible(cls, value: str) -> str:
        """要求任务内容至少包含一个可见字符"""

        if not value.strip():
            raise ValueError("Todo 内容必须包含可见字符")
        return value


class TodoGroup(_TaskTraceModel):
    """展示一个用户轮次内已确认的根任务组"""

    id: str = Field(min_length=1, max_length=2048)
    user_message_id: str = Field(min_length=1, max_length=2048)
    user_message_preview: str = Field(min_length=1, max_length=161)
    group_tool_call_id: str = Field(min_length=1, max_length=2048)
    created_at: datetime
    status: TodoGroupStatus
    todos: tuple[TodoTraceItem, ...]

    @field_validator("id", "user_message_id", "group_tool_call_id")
    @classmethod
    def identifiers_are_canonical(cls, value: str) -> str:
        """保持分组、消息与 Tool 身份可直接用于稳定匹配"""

        if value != value.strip():
            raise ValueError("任务轨迹身份必须是无首尾空白的非空文本")
        return value

    @field_validator("user_message_preview")
    @classmethod
    def preview_is_visible(cls, value: str) -> str:
        """拒绝无法作为抽屉根节点标题的空预览"""

        if not value.strip():
            raise ValueError("用户消息预览必须包含可见字符")
        return value

    @field_validator("created_at")
    @classmethod
    def created_at_is_utc(cls, value: datetime) -> datetime:
        """要求所有组使用可比较的 UTC 创建时间"""

        if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
            raise ValueError("任务组创建时间必须是 UTC 时间")
        return value

    @model_validator(mode="after")
    def todo_ids_are_unique(self) -> TodoGroup:
        """避免一个组内的 React 与定位身份发生冲突"""

        identifiers = tuple(item.id for item in self.todos)
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("同一任务组内的 Todo ID 必须唯一")
        return self


class TaskTraceSnapshot(_TaskTraceModel):
    """返回一次固定 Trace 前缀的完整任务轨迹结果"""

    status: Literal["ready", "unavailable"]
    todo_groups: tuple[TodoGroup, ...]
    error_code: TaskTraceErrorCode | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )

    @model_validator(mode="after")
    def status_matches_payload(self) -> TaskTraceSnapshot:
        """区分真实空轨迹与无法可靠重建的轨迹"""

        if self.status == "ready" and self.error_code is not None:
            raise ValueError("ready 任务轨迹不能包含错误码")
        if self.status == "unavailable":
            if self.error_code is None:
                raise ValueError("unavailable 任务轨迹必须包含错误码")
            if self.todo_groups:
                raise ValueError("unavailable 任务轨迹不能返回部分分组")
        return self


__all__ = [
    "TaskTraceErrorCode",
    "TaskTraceSnapshot",
    "TodoGroup",
    "TodoGroupStatus",
    "TodoTraceItem",
    "TodoTraceItemStatus",
]

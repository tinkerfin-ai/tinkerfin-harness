"""自动化 HTTP 配置和可见运行事实"""

from datetime import UTC, date, datetime, time, timedelta
from typing import Annotated, Literal, Self
from zoneinfo import ZoneInfo

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    RootModel,
    field_validator,
    model_validator,
)
from pydantic.alias_generators import to_camel

from tinkerfin.agui import AgUiTraceMessage
from tinkerfin_automation import (
    CronSchedule,
    ExecutionStatus,
    IntervalSchedule,
    OnceSchedule,
)
from tinkerfin_automation.schedules import ScheduleSpec
from tinkerfin_contracts.media import Attachment
from tinkerfin_studio.agent.access import AccessMode

ZONE = ZoneInfo("Asia/Shanghai")
TimeText = Annotated[str, Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")]


class Boundary(BaseModel):
    """统一使用驼峰 JSON，拒绝未声明的业务字段"""

    model_config = ConfigDict(
        extra="forbid", populate_by_name=True, alias_generator=to_camel
    )


class Once(Boundary):
    """指定北京时间的单次日程"""

    kind: Literal["once"]
    date: date
    time: TimeText


class Daily(Boundary):
    """每天或周一至周五执行，不按法定节假日调整"""

    kind: Literal["daily", "workdays"]
    time: TimeText


class Weekly(Boundary):
    """每周指定执行日，周一为0，周日为6"""

    kind: Literal["weekly"]
    weekdays: list[Annotated[int, Field(ge=0, le=6)]] = Field(
        min_length=1, max_length=7
    )
    time: TimeText

    @field_validator("weekdays")
    @classmethod
    def unique_days(cls, value: list[int]) -> list[int]:
        """归一化不重复的执行日"""
        if len(value) != len(set(value)):
            raise ValueError("执行日不能重复")
        return sorted(value)


class Monthly(Boundary):
    """每月指定日期，不存在的日期跳过"""

    kind: Literal["monthly"]
    day: int = Field(ge=1, le=31)
    time: TimeText


class Interval(Boundary):
    """从固定起点按间隔执行，不随查询时间改变锚点"""

    kind: Literal["interval"]
    every: int = Field(ge=1, le=999)
    unit: Literal["minutes", "hours", "days"]

    @model_validator(mode="after")
    def supported_duration(self) -> Self:
        """最长执行间隔为365天"""
        if (
            self.every * {"minutes": 60, "hours": 3600, "days": 86400}[self.unit]
            > 365 * 86400
        ):
            raise ValueError("执行间隔不得超过365天")
        return self


class ScheduleInput(
    RootModel[
        Annotated[
            Once | Daily | Weekly | Monthly | Interval, Field(discriminator="kind")
        ]
    ]
):
    """按 kind 选择日程字段，接口与模型工具均使用 JSON 对象"""

    model_config = ConfigDict(json_schema_extra={"type": "object"})


class TaskConfiguration(Boundary):
    """用户保存的任务配置，运行按该快照执行而不包含模型凭据"""

    name: str = Field(min_length=1, max_length=255)
    prompt: str = Field(min_length=1, max_length=50000)
    schedule: ScheduleInput
    starts_on: date | None = None
    ends_on: date | None = None
    model_id: str = Field(min_length=1, max_length=64)
    access_mode: AccessMode = "full"
    attachments: list[str] = Field(default_factory=list, max_length=5)

    @field_validator("name", "prompt", "model_id")
    @classmethod
    def nonempty_text(cls, value: str) -> str:
        """去除首尾空白后仍须有内容"""
        value = value.strip()
        if not value or "\x00" in value:
            raise ValueError("内容不能为空或包含空字符")
        return value

    @model_validator(mode="after")
    def valid_window(self) -> Self:
        """检查起止日期、单次执行范围及附件唯一性"""
        if self.ends_on == date.max:
            raise ValueError("结束日期必须早于9999-12-31")
        if self.starts_on and self.ends_on and self.starts_on > self.ends_on:
            raise ValueError("结束日期不能早于开始日期")
        schedule = self.schedule.root
        if isinstance(schedule, Once) and (
            (self.starts_on and schedule.date < self.starts_on)
            or (self.ends_on and schedule.date > self.ends_on)
        ):
            raise ValueError("执行日期必须在有效期内")
        if len(self.attachments) != len(set(self.attachments)):
            raise ValueError("参考文件不能重复")
        return self

    def framework_schedule(self, anchor: datetime) -> ScheduleSpec:
        """将用户日历选项交给框架计算下一次执行，手动运行不受有效期限制"""
        active_from = (
            datetime.combine(self.starts_on, time(), ZONE) if self.starts_on else None
        )
        active_until = (
            datetime.combine(self.ends_on + timedelta(days=1), time(), ZONE)
            if self.ends_on
            else None
        )
        schedule = self.schedule.root
        if isinstance(schedule, Once):
            return OnceSchedule(
                at=datetime.combine(
                    schedule.date, time.fromisoformat(schedule.time), ZONE
                ),
                active_from=active_from,
                active_until=active_until,
            )
        if isinstance(schedule, Interval):
            every_seconds = (
                schedule.every
                * {"minutes": 60, "hours": 3600, "days": 86400}[schedule.unit]
            )
            return IntervalSchedule(
                every_seconds=every_seconds,
                start_at=active_from
                or anchor.astimezone(UTC) + timedelta(seconds=every_seconds),
                active_from=active_from,
                active_until=active_until,
            )
        hour, minute = schedule.time.split(":")
        days = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
        weekday = (
            ",".join(days[day] for day in schedule.weekdays)
            if isinstance(schedule, Weekly)
            else "mon-fri"
            if schedule.kind == "workdays"
            else "*"
        )
        monthday = str(schedule.day) if isinstance(schedule, Monthly) else "*"
        return CronSchedule(
            expression=f"{int(minute)} {int(hour)} {monthday} * {weekday}",
            timezone="Asia/Shanghai",
            active_from=active_from,
            active_until=active_until,
        )


class SaveTask(Boundary):
    """一次可安全重试的任务保存命令"""

    request_id: str = Field(min_length=1, max_length=128)
    configuration: TaskConfiguration
    expected_revision: int | None = Field(default=None, ge=1)


class TaskCommand(Boundary):
    """绑定当前修订的启停、删除或手动执行命令"""

    request_id: str = Field(min_length=1, max_length=128)
    expected_revision: int = Field(ge=1)


class BatchItem(TaskCommand):
    """批量操作中的独立任务命令"""

    task_id: str = Field(min_length=1, max_length=36)


class BatchCommand(Boundary):
    """每项独立返回结果的有限批量操作"""

    operation: Literal["pause", "delete"]
    items: list[BatchItem] = Field(min_length=1, max_length=100)


class BatchResult(Boundary):
    """保留失败任务供用户重新操作"""

    task_id: str
    succeeded: bool
    error: str | None = None


class TaskView(TaskConfiguration):
    """持久任务与服务端计算的下一次执行时间"""

    id: str
    enabled: bool
    revision: int
    next_run_at: datetime | None
    input_files: list[Attachment] = Field(
        default_factory=list, description="任务配置的参考附件，不包含执行产物"
    )


class TaskList(Boundary):
    """服务端筛选的任务页"""

    items: list[TaskView]
    next_cursor: str | None


class RunView(Boundary):
    """一次执行的真实时间和状态，任务改名不会改变当次名称"""

    id: str
    task_id: str | None
    name: str | None
    status: ExecutionStatus
    trigger: str
    queued_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    error: str | None = None


class RunList(Boundary):
    """服务端筛选的执行页"""

    items: list[RunView]
    next_cursor: str | None


class RunDetail(RunView):
    """经过归属校验的只读消息和运行附件"""

    messages: list[AgUiTraceMessage] = Field(default_factory=list)
    output_files: list[Attachment] = Field(
        default_factory=list, description="本次执行已交付的文件，不包含参考附件"
    )
    result_available: bool

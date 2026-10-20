"""项目创建、命名与查询的 HTTP 边界"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic.alias_generators import to_camel


class ProjectName(BaseModel):
    """项目创建和重命名共用相同名称约束"""

    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=64)

    @field_validator("name")
    @classmethod
    def meaningful_name(cls, value: str) -> str:
        value = value.strip()
        if not value or any(ord(char) < 32 for char in value):
            raise ValueError("项目名称不能为空或包含控制字符")
        return value


class ProjectView(BaseModel):
    """不暴露其他用户信息的项目列表项"""

    model_config = ConfigDict(
        from_attributes=True, alias_generator=to_camel, populate_by_name=True
    )
    id: str
    name: str
    created_at: datetime
    updated_at: datetime

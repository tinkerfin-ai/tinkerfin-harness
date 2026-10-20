"""项目记忆文件的页面数据与文本编辑约束"""

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic.alias_generators import to_camel

from tinkerfin.files import PersistentFile

MAX_MEMORY_BYTES = 256 * 1024
MemoryPath = Annotated[str, Field(min_length=2, max_length=512)]
MemoryEtag = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


class MemoryWrite(BaseModel):
    """页面创建记忆所需的路径与纯文本"""

    model_config = ConfigDict(extra="forbid")
    path: MemoryPath
    content: str = Field(max_length=MAX_MEMORY_BYTES)

    @field_validator("path")
    @classmethod
    def clean_path(cls, value: str) -> str:
        if any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("记忆路径不能包含控制字符")
        return value

    @field_validator("content")
    @classmethod
    def text_limit(cls, value: str) -> str:
        if "\x00" in value or len(value.encode("utf-8")) > MAX_MEMORY_BYTES:
            raise ValueError("记忆需要是不超过 256 KB 的文本")
        return value


class MemoryUpdate(MemoryWrite):
    etag: MemoryEtag


class MemoryItem(BaseModel):
    """列表保留不可编辑文件的路径和删除条件"""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)
    path: str
    etag: str
    size_bytes: int
    updated_at: datetime
    editable: bool
    preview: str

    @classmethod
    def from_file(cls, item: PersistentFile) -> "MemoryItem":
        try:
            content = item.content.decode("utf-8")
            editable = "\x00" not in content and len(item.content) <= MAX_MEMORY_BYTES
        except UnicodeDecodeError:
            content, editable = "", False
        return cls(
            path=item.path,
            etag=item.etag,
            size_bytes=len(item.content),
            updated_at=item.updated_at,
            editable=editable,
            preview=" ".join(content.split())[:160],
        )


class MemoryDetail(MemoryItem):
    content: str

    @classmethod
    def read_file(cls, item: PersistentFile) -> "MemoryDetail":
        summary = MemoryItem.from_file(item)
        if not summary.editable:
            raise ValueError("此文件不是可编辑的记忆文本")
        return cls(**summary.model_dump(), content=item.content.decode("utf-8"))


class MemoryPage(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)
    items: list[MemoryItem]
    next_offset: int | None

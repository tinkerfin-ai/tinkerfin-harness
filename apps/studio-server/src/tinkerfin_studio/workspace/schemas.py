"""工作区目录、文件信息与有界源码预览的 HTTP 数据"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


class WorkspaceFileView(BaseModel):
    """只包含项目虚拟路径的文件信息"""

    model_config = ConfigDict(
        from_attributes=True, alias_generator=to_camel, populate_by_name=True
    )
    path: str = Field(description="以 / 为根的项目虚拟路径，不包含宿主目录")
    name: str
    kind: Literal["file", "directory", "symlink", "other"]
    size_bytes: int | None = Field(description="普通文件的字节数，其他类型为空")
    modified_at: datetime = Field(description="文件最后修改时间，使用 UTC")
    etag: str = Field(description="判断文件信息是否变化的标识，不是内容哈希")


class WorkspaceDirectoryView(BaseModel):
    """单个目录的有界页面，尚未准备工作区与空目录分别表达"""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)
    state: Literal["ready", "uninitialized"]
    path: str
    entries: list[WorkspaceFileView]
    next_cursor: str | None = Field(
        default=None, description="当前目录下一页游标，目录变化后须从第一页重读"
    )


class WorkspaceTextView(BaseModel):
    """只供显示和选择复制的源码片段，不执行或渲染文件内容"""

    kind: Literal["text"] = "text"
    file: WorkspaceFileView
    text: str
    truncated: bool


class WorkspaceUnsupportedView(BaseModel):
    """非文本文件保留信息，不提供文件内容或下载地址"""

    kind: Literal["unsupported"] = "unsupported"
    file: WorkspaceFileView


WorkspacePreviewView = WorkspaceTextView | WorkspaceUnsupportedView

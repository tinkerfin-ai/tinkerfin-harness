"""将项目文件查询收敛为目录浏览与轻量源码预览"""

from pathlib import PurePosixPath

from tinkerfin_sandbox import (
    OpenSandboxNotTextError,
    OpenSandboxWorkspaceNotInitializedError,
    SandboxWorkspace,
)

from .schemas import (
    WorkspaceDirectoryView,
    WorkspaceFileView,
    WorkspacePreviewView,
    WorkspaceTextView,
    WorkspaceUnsupportedView,
)

PREVIEW_BYTES = 100 * 1024
PREVIEW_LINES = 200
_NON_TEXT_SUFFIXES = frozenset(
    {
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".webp",
        ".bmp",
        ".ico",
        ".avif",
        ".pdf",
        ".doc",
        ".docx",
        ".xls",
        ".xlsx",
        ".ppt",
        ".pptx",
        ".zip",
        ".gz",
        ".tar",
        ".7z",
        ".rar",
        ".bz2",
        ".xz",
        ".mp3",
        ".mp4",
        ".wav",
        ".mov",
        ".woff",
        ".woff2",
        ".ttf",
        ".exe",
        ".dll",
        ".so",
        ".pyc",
        ".sqlite",
        ".db",
        ".bin",
    }
)


class WorkspaceFileService:
    """借用已授权项目的框架查询，读取不会准备沙箱或执行任务"""

    def __init__(self, workspace: SandboxWorkspace[str]) -> None:
        self.workspace = workspace

    async def directory(self, path: str, cursor: str | None) -> WorkspaceDirectoryView:
        """按项目返回目录页，连接错误仍交由调用方呈现"""
        try:
            page = await self.workspace.list_directory(path, limit=200, cursor=cursor)
        except OpenSandboxWorkspaceNotInitializedError:
            return WorkspaceDirectoryView(state="uninitialized", path=path, entries=[])
        return WorkspaceDirectoryView(
            state="ready",
            path=page.path,
            entries=[WorkspaceFileView.model_validate(item) for item in page.entries],
            next_cursor=page.next_cursor,
        )

    async def preview(self, path: str) -> WorkspacePreviewView:
        """文本最多读取两百行或一百 KiB，其他格式仅展示文件信息"""
        info = await self.workspace.get_file_info(path)
        if (
            info.kind != "file"
            or PurePosixPath(info.path).suffix.lower() in _NON_TEXT_SUFFIXES
        ):
            return WorkspaceUnsupportedView(file=WorkspaceFileView.model_validate(info))
        try:
            result = await self.workspace.read_text(
                path, max_bytes=PREVIEW_BYTES, max_lines=PREVIEW_LINES
            )
        except OpenSandboxNotTextError:
            return WorkspaceUnsupportedView(file=WorkspaceFileView.model_validate(info))
        return WorkspaceTextView(
            file=WorkspaceFileView.model_validate(result.file),
            text=result.text,
            truncated=result.truncated,
        )

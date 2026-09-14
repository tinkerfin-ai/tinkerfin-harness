"""工作文件描述与生成产物保存，不创建会话附件"""

import base64
import json
from dataclasses import asdict, dataclass
from uuid import uuid4

from deepagents.backends.sandbox import MAX_BINARY_BYTES

from tinkerfin_sandbox import RootedOpenSandboxBackend
from tinkerfin_studio.attachments.documents import DocumentProcessor
from tinkerfin_studio.attachments.processing import MAX_FILE_BYTES, MIME_TYPES


@dataclass(frozen=True, slots=True)
class WorkFile:
    """当前用户工作区中的文件，交付时复制该路径当时的内容

    file_path 是框架工作区路径，不是宿主机路径或附件下载地址。
    size_bytes 表示保存时的字节数，后续编辑不更新此工具结果。
    preview_file_path 是大图片保存时的缩略图，仅供看图；交付始终选择原文件。
    """

    file_path: str
    name: str
    mime_type: str
    size_bytes: int
    preview_file_path: str | None = None

    def tool_result(self) -> str:
        """返回模型可直接用于读取和交付的工作文件描述"""
        return json.dumps(
            {key: value for key, value in asdict(self).items() if value is not None},
            ensure_ascii=False,
        )


async def describe_work_file(
    workspace: RootedOpenSandboxBackend,
    processor: DocumentProcessor,
    *,
    path: str,
    name: str,
    data: bytes,
) -> WorkFile:
    """为已保存产物补充可供视觉模型读取的有界预览

    原图始终保留；缩略图仅写入工作区，生成失败不返回成功描述。
    图片处理由可取消的文档子进程完成，不占用异步事件循环。

    Args:
        workspace: 当前用户工作区
        processor: 应用拥有的有界文档处理器
        path: 已保存原文件的工作区路径
        name: 原文件显示名称
        data: 原文件内容

    Returns:
        原文件描述，大图片还包含独立 JPEG 预览路径

    Raises:
        ValueError: 图片无效或预览不符合大小限制
        OSError: 预览保存失败
        OpenSandboxError: 工作区不可用或传输失败
        TimeoutError: 预览生成超时
    """
    mime = MIME_TYPES[name.rsplit(".", 1)[-1].lower()]
    preview_path = None
    if mime.startswith("image/") and len(data) > MAX_BINARY_BYTES:
        result = await processor.run(
            {
                "operation": "preview_image",
                "data": base64.b64encode(data).decode("ascii"),
                "max_bytes": MAX_BINARY_BYTES,
            }
        )
        encoded = result.get("data")
        if not isinstance(encoded, str):
            raise ValueError("图片处理未返回预览")
        preview = base64.b64decode(encoded, validate=True)
        if not preview.startswith(b"\xff\xd8\xff") or len(preview) > MAX_BINARY_BYTES:
            raise ValueError("图片预览不符合读取限制")
        preview_path = f"/preview-{uuid4().hex}.jpg"
        results = await workspace.aupload_files([(preview_path, preview)])
        if (
            len(results) != 1
            or results[0].path != preview_path
            or results[0].error is not None
        ):
            raise OSError("图片预览保存失败")
    return WorkFile(path, name, mime, len(data), preview_path)


async def save_work_file(
    workspace: RootedOpenSandboxBackend,
    processor: DocumentProcessor,
    *,
    name: str,
    data: bytes,
) -> WorkFile:
    """将有界产物保存到独立工作文件，保存失败或取消时不报告成功

    工作区由运行环境提供，框架负责路径约束、异步传输与资源释放。
    每次使用独立路径，避免同名产物相互覆盖；交付时再验证文件内容。

    Args:
        workspace: 当前用户工作区
        processor: 应用拥有的有界文档处理器
        name: 文件名称，不包含目录，扩展名必须是支持的文件格式
        data: 非空且不超过单文件限制的原始内容

    Returns:
        已保存的工作文件描述

    Raises:
        ValueError: 名称、格式或内容大小不符合要求
        OSError: 工作区未成功保存文件
        OpenSandboxError: 工作区不可用或传输失败
        TimeoutError: 图片预览生成超时
    """
    extension = name.rsplit(".", 1)[-1].lower()
    if (
        not name
        or len(name) > 255
        or any(char in name for char in ("/", "\\", "\0", "\r", "\n"))
        or extension not in MIME_TYPES
    ):
        raise ValueError("文件名须为不含目录的受支持格式名称")
    if not data or len(data) > MAX_FILE_BYTES:
        raise ValueError("工作文件不能为空，且单个不能超过 10 MiB")
    path = f"/artifact-{uuid4().hex}.{extension}"
    results = await workspace.aupload_files([(path, data)])
    if len(results) != 1 or results[0].path != path or results[0].error is not None:
        raise OSError("工作文件保存失败")
    return await describe_work_file(
        workspace, processor, path=path, name=name, data=data
    )

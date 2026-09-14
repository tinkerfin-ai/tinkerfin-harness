"""附件存储契约，与供应商客户端和地址配置无关"""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class UploadForm:
    """浏览器直接提交的表单；字段按原样发送，最后添加文件"""

    url: str
    fields: dict[str, str]
    expires_in: int


@dataclass(frozen=True, slots=True)
class DownloadLink:
    """有效期内持有者可读取的下载地址，不得持久化到消息中"""

    url: str
    expires_in: int


class AttachmentStorage(Protocol):
    """保存附件并提供浏览器传输许可，连接由应用资源入口拥有

    标识由业务服务分配，原件、派生图及待确认上传使用不同标识。
    实现必须限制读取大小，完整写入后才可读，删除不存在的对象视为成功。
    不存在的读取抛出 FileNotFoundError，存储故障抛出 OSError，取消原样传播。
    """

    async def put(self, key: str, chunks: AsyncIterator[bytes]) -> None:
        """完整写入不超过附件大小上限的内容"""
        ...

    async def read(self, key: str) -> bytes:
        """读取至多附件大小上限加一个检测字节"""
        ...

    async def delete(self, key: str) -> None:
        """幂等删除指定对象"""
        ...

    async def upload_form(self, attachment_id: str, size_bytes: int) -> UploadForm:
        """签发只能写入该附件待确认内容的限时表单"""
        ...

    async def download_link(
        self, key: str, *, name: str, mime_type: str, inline: bool
    ) -> DownloadLink:
        """签发已获业务授权的对象读取链接"""
        ...

    async def check_ready(self) -> None:
        """确认存储可访问，失败抛出 OSError"""
        ...

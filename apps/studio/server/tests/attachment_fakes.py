"""附件业务测试使用的内存对象存储，真实签名另由集成测试验证"""

from collections.abc import AsyncIterator

from tinkerfin_studio.attachments.storage import DownloadLink, UploadForm


class MemoryAttachmentStorage:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    async def put(self, key: str, chunks: AsyncIterator[bytes]) -> None:
        parts = [chunk async for chunk in chunks]
        self.objects[key] = b"".join(parts)

    async def read(self, key: str) -> bytes:
        try:
            return self.objects[key]
        except KeyError as error:
            raise FileNotFoundError(key) from error

    async def delete(self, key: str) -> None:
        self.objects.pop(key, None)

    async def upload_form(self, attachment_id: str, size_bytes: int) -> UploadForm:
        return UploadForm(
            "https://storage.example/upload", {"key": attachment_id + "-upload"}, 600
        )

    async def download_link(
        self, key: str, *, name: str, mime_type: str, inline: bool
    ) -> DownloadLink:
        return DownloadLink("https://storage.example/" + key, 300)

    async def check_ready(self) -> None:
        pass

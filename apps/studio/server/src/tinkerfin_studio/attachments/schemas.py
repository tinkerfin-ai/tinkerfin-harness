"""浏览器附件直传和下载许可的 HTTP 数据"""

from pydantic import BaseModel, Field

from tinkerfin_studio.attachments.processing import MAX_FILE_BYTES


class UploadRequest(BaseModel):
    """申请上传所需的文件元信息，内容由完成接口核验"""

    name: str = Field(min_length=1, max_length=255)
    size_bytes: int = Field(gt=0, le=MAX_FILE_BYTES, strict=True)


class UploadResponse(BaseModel):
    """提交全部表单字段和文件后，使用附件 ID 确认上传"""

    attachment_id: str
    url: str
    fields: dict[str, str]
    expires_in: int = Field(description="上传许可从签发起的有效秒数")


class DownloadResponse(BaseModel):
    """鉴权后的短期读取许可，地址不应保存或分享"""

    url: str
    expires_in: int = Field(description="下载链接从签发起的有效秒数")

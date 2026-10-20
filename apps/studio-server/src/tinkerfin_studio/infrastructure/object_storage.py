"""应用文件的 MinIO 存储、私有附件许可与公开头像地址"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator, Iterator
from contextlib import AsyncExitStack, asynccontextmanager, contextmanager
from typing import TYPE_CHECKING
from urllib.parse import quote

import anyio
from aiobotocore.config import AioConfig
from aiobotocore.session import get_session
from botocore.exceptions import BotoCoreError, ClientError
from pydantic import JsonValue, TypeAdapter

from tinkerfin_studio.attachments.processing import MAX_FILE_BYTES
from tinkerfin_studio.attachments.storage import DownloadLink, UploadForm
from tinkerfin_studio.config.settings import S3StorageSettings

if TYPE_CHECKING:
    from types_aiobotocore_s3.client import S3Client

_UPLOAD_SECONDS = 600
_DOWNLOAD_SECONDS = 300
_TEMPORARY_PREFIX = "attachments/uploads/"
_AVATAR_PREFIX = "avatars/"
_AVATAR_READ_POLICY = "tinkerfin-avatars-read"


@contextmanager
def _storage_errors() -> Iterator[None]:
    try:
        yield
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") in {
            "NoSuchKey",
            "404",
            "NotFound",
        }:
            raise FileNotFoundError("存储对象不存在") from error
        raise OSError("对象存储请求失败") from error
    except BotoCoreError as error:
        raise OSError("对象存储连接失败") from error


class MinioStorage:
    """通过同一组异步客户端保存附件和头像，浏览器使用独立的公开地址

    使用 open 上下文创建实例，退出时关闭全部连接。最多并发四个对象操作；
    单次操作最长 60 秒，连接超时 5 秒，不进行隐式请求重试。
    直接访问配置的存储地址，不继承进程或系统代理。
    """

    def __init__(
        self, client: S3Client, signer: S3Client, bucket: str, public_endpoint: str
    ) -> None:
        self._client = client
        self._signer = signer
        self._bucket = bucket
        self._public_endpoint = public_endpoint.rstrip("/")
        self._operations = anyio.CapacityLimiter(4)

    @classmethod
    @asynccontextmanager
    async def open(cls, settings: S3StorageSettings) -> AsyncIterator[MinioStorage]:
        """打开存储连接，异常及取消时也关闭已创建的客户端"""
        session = get_session()
        config = AioConfig(
            proxies={},
            signature_version="s3v4",
            s3={"addressing_style": "path"},
            connect_timeout=5,
            read_timeout=30,
            max_pool_connections=4,
            retries={"total_max_attempts": 1},
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
        )
        async with AsyncExitStack() as stack:
            client: S3Client = await stack.enter_async_context(
                session.create_client(
                    "s3",
                    endpoint_url=settings.endpoint,
                    region_name="us-east-1",
                    aws_access_key_id=settings.access_key.get_secret_value(),
                    aws_secret_access_key=settings.secret_key.get_secret_value(),
                    config=config,
                )
            )
            signer: S3Client = await stack.enter_async_context(
                session.create_client(
                    "s3",
                    endpoint_url=settings.public_endpoint,
                    region_name="us-east-1",
                    aws_access_key_id=settings.access_key.get_secret_value(),
                    aws_secret_access_key=settings.secret_key.get_secret_value(),
                    config=config,
                )
            )
            yield cls(client, signer, settings.bucket, settings.public_endpoint)

    def _key(self, key: str) -> str:
        if not re.fullmatch(r"[a-f0-9]{32}(?:-preview|-model|-upload)?", key):
            raise ValueError("附件存储标识不合法")
        if key.endswith("-upload"):
            return _TEMPORARY_PREFIX + key.removesuffix("-upload")
        return "attachments/" + key

    async def initialize(self) -> None:
        """初始化用户指定桶和待确认上传清理规则，不修改其他桶"""
        with _storage_errors(), anyio.fail_after(60):
            try:
                await self._client.head_bucket(Bucket=self._bucket)
            except ClientError as error:
                if error.response.get("Error", {}).get("Code") not in {
                    "404",
                    "NoSuchBucket",
                }:
                    raise
                try:
                    await self._client.create_bucket(Bucket=self._bucket)
                except ClientError as create_error:
                    if (
                        create_error.response.get("Error", {}).get("Code")
                        != "BucketAlreadyOwnedByYou"
                    ):
                        raise
            # 仅更新附件上传前缀规则，保留应用桶中其他用途的策略
            try:
                lifecycle = await self._client.get_bucket_lifecycle_configuration(
                    Bucket=self._bucket
                )
                rules = lifecycle["Rules"]
            except ClientError as error:
                if (
                    error.response.get("Error", {}).get("Code")
                    != "NoSuchLifecycleConfiguration"
                ):
                    raise
                rules = []
            rules = [
                rule for rule in rules if rule.get("ID") != "studio-attachment-uploads"
            ]
            rules.append(
                {
                    "ID": "studio-attachment-uploads",
                    "Status": "Enabled",
                    "Filter": {"Prefix": _TEMPORARY_PREFIX},
                    "Expiration": {"Days": 1},
                }
            )
            await self._client.put_bucket_lifecycle_configuration(
                Bucket=self._bucket,
                LifecycleConfiguration={"Rules": rules},
            )
            # 只公开头像的读取，不开放附件、列举或写入，保留桶的其他策略
            try:
                existing = await self._client.get_bucket_policy(Bucket=self._bucket)
                policy = TypeAdapter(dict[str, JsonValue]).validate_json(
                    existing["Policy"]
                )
            except ClientError as error:
                if error.response.get("Error", {}).get("Code") != "NoSuchBucketPolicy":
                    raise
                policy = {"Version": "2012-10-17", "Statement": []}
            entries = policy.get("Statement", [])
            if isinstance(entries, dict):
                entries = [entries]
            statements = TypeAdapter(list[dict[str, JsonValue]]).validate_python(
                entries
            )
            statements = [
                entry for entry in statements if entry.get("Sid") != _AVATAR_READ_POLICY
            ]
            statements.append(
                {
                    "Sid": _AVATAR_READ_POLICY,
                    "Effect": "Allow",
                    "Principal": {"AWS": ["*"]},
                    "Action": ["s3:GetObject"],
                    "Resource": [f"arn:aws:s3:::{self._bucket}/{_AVATAR_PREFIX}*"],
                }
            )
            policy["Statement"] = list[JsonValue](statements)
            await self._client.put_bucket_policy(
                Bucket=self._bucket, Policy=json.dumps(policy)
            )

    async def upload_avatar(self, image_id: str, data: bytes) -> str:
        """保存已校验的 JPEG 头像，返回不依赖临时签名的长期地址

        头像是不可变的持久上传文件，数据库只保存当前使用的地址。
        替换资料不会删除历史上传，避免并发更新或提交结果不确定时破坏图片。

        Args:
            image_id: 应用生成的 32 位十六进制随机标识
            data: 已完成大小限制和内容校验的 JPEG 数据

        Returns:
            浏览器可直接读取的长期对象地址，不包含临时签名

        Raises:
            ValueError: 标识格式不正确
            OSError: 对象存储不可用，或目标标识已被使用
            TimeoutError: 上传超过对象操作期限
        """
        if not re.fullmatch(r"[a-f0-9]{32}", image_id):
            raise ValueError("头像存储标识不合法")
        key = f"{_AVATAR_PREFIX}{image_id}.jpg"
        async with self._operations:
            with _storage_errors(), anyio.fail_after(60):
                await self._client.put_object(
                    Bucket=self._bucket,
                    Key=key,
                    Body=data,
                    ContentType="image/jpeg",
                    CacheControl="public, max-age=31536000, immutable",
                    IfNoneMatch="*",
                )
        return f"{self._public_endpoint}/{quote(self._bucket, safe='')}/{key}"

    async def check_ready(self) -> None:
        with _storage_errors():
            await self._client.head_bucket(Bucket=self._bucket)

    async def put(self, key: str, chunks: AsyncIterator[bytes]) -> None:
        async with self._operations:
            with _storage_errors(), anyio.fail_after(60):
                content = bytearray()
                async for chunk in chunks:
                    if len(content) + len(chunk) > MAX_FILE_BYTES:
                        raise ValueError("附件超过 10 MiB")
                    content.extend(chunk)
                await self._client.put_object(
                    Bucket=self._bucket, Key=self._key(key), Body=bytes(content)
                )

    async def read(self, key: str) -> bytes:
        async with self._operations:
            with _storage_errors(), anyio.fail_after(60):
                response = await self._client.get_object(
                    Bucket=self._bucket, Key=self._key(key)
                )
                async with response["Body"] as body:
                    data = bytearray()
                    while len(data) <= MAX_FILE_BYTES:
                        chunk = await body.read(
                            min(64 * 1024, MAX_FILE_BYTES + 1 - len(data))
                        )
                        if not chunk:
                            break
                        data.extend(chunk)
                    return bytes(data)

    async def delete(self, key: str) -> None:
        async with self._operations:
            with _storage_errors(), anyio.fail_after(60):
                await self._client.delete_object(
                    Bucket=self._bucket, Key=self._key(key)
                )

    async def upload_form(self, attachment_id: str, size_bytes: int) -> UploadForm:
        with _storage_errors():
            result = await self._signer.generate_presigned_post(
                Bucket=self._bucket,
                Key=self._key(attachment_id + "-upload"),
                Conditions=[["content-length-range", size_bytes, size_bytes]],
                ExpiresIn=_UPLOAD_SECONDS,
            )
        return UploadForm(
            url=TypeAdapter(str).validate_python(result["url"]),
            fields=TypeAdapter(dict[str, str]).validate_python(result["fields"]),
            expires_in=_UPLOAD_SECONDS,
        )

    async def download_link(
        self, key: str, *, name: str, mime_type: str, inline: bool
    ) -> DownloadLink:
        with _storage_errors():
            url = await self._signer.generate_presigned_url(
                "get_object",
                Params={
                    "Bucket": self._bucket,
                    "Key": self._key(key),
                    "ResponseContentType": mime_type,
                    "ResponseCacheControl": "private, no-store",
                    "ResponseContentDisposition": f"{'inline' if inline else 'attachment'}; filename*=UTF-8''{quote(name, safe='')}",
                },
                ExpiresIn=_DOWNLOAD_SECONDS,
            )
        return DownloadLink(url=url, expires_in=_DOWNLOAD_SECONDS)

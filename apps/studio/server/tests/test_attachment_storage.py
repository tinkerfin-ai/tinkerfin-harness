"""使用真实 SDK 签名及受控响应验证对象存储契约"""

import base64
import json
from urllib.parse import parse_qs, urlsplit

import pytest
from aiobotocore.stub import AioStubber
from pydantic import SecretStr

from tinkerfin_studio.attachments.minio import MinioAttachmentStorage
from tinkerfin_studio.config.settings import S3StorageSettings


def storage_settings() -> S3StorageSettings:
    return S3StorageSettings(
        bucket="chosen-bucket",
        endpoint="http://minio:9000",
        public_endpoint="https://files.example.com",
        access_key=SecretStr("test-access"),
        secret_key=SecretStr("test-secret"),
    )


async def test_signatures_target_public_host_and_exact_temporary_object():
    async with MinioAttachmentStorage.open(storage_settings()) as storage:
        form = await storage.upload_form("a" * 32, 42)
        assert form.url == "https://files.example.com/chosen-bucket"
        assert form.expires_in == 600
        assert form.fields["key"] == "attachments/uploads/" + "a" * 32
        policy = json.loads(base64.b64decode(form.fields["policy"]))
        assert ["content-length-range", 42, 42] in policy["conditions"]
        assert {"key": "attachments/uploads/" + "a" * 32} in policy["conditions"]
        link = await storage.download_link(
            "a" * 32, name="月报.md", mime_type="text/markdown", inline=False
        )
        url = urlsplit(link.url)
        assert url.netloc == "files.example.com"
        assert url.path == "/chosen-bucket/attachments/" + "a" * 32
        query = parse_qs(url.query)
        assert query["X-Amz-Expires"] == ["300"]
        assert query["response-content-type"] == ["text/markdown"]
        assert query["response-content-disposition"][0].startswith("attachment;")


async def test_storage_errors_distinguish_missing_content_and_unavailable_storage():
    async with MinioAttachmentStorage.open(storage_settings()) as storage:
        with AioStubber(storage._client) as stub:
            stub.add_client_error(
                "get_object", service_error_code="NoSuchKey", http_status_code=404
            )
            with pytest.raises(FileNotFoundError):
                await storage.read("a" * 32)
            stub.add_client_error(
                "head_bucket", service_error_code="AccessDenied", http_status_code=403
            )
            with pytest.raises(OSError, match="附件存储请求失败"):
                await storage.check_ready()
            stub.add_response(
                "delete_object",
                {},
                {"Bucket": "chosen-bucket", "Key": "attachments/" + "a" * 32},
            )
            await storage.delete("a" * 32)
            stub.assert_no_pending_responses()

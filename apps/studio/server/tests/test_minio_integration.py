"""独占 MinIO 容器中的真实签名、对象持久化与 HTTP 附件契约"""

from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr
from testcontainers.core.container import DockerContainer
from testcontainers.core.wait_strategies import HttpWaitStrategy
from tests.support.docker_services import _running_container, _with_loopback_port

from tinkerfin_studio.api.dependencies import get_user_context
from tinkerfin_studio.application import create_application
from tinkerfin_studio.attachments.minio import MinioAttachmentStorage
from tinkerfin_studio.attachments.service import AttachmentService
from tinkerfin_studio.auth.types import UserContext
from tinkerfin_studio.config.settings import S3StorageSettings

pytestmark = pytest.mark.docker_integration
_IMAGE = "quay.io/minio/minio:RELEASE.2025-09-07T16-13-09Z@sha256:14cea493d9a34af32f524e538b8346cf79f3321eff8e708c1e2960462bd8936e"


@pytest.fixture
def minio_settings(docker_test_client, docker_test_run_id):
    container = _with_loopback_port(DockerContainer(_IMAGE), 9000)
    container.with_command("server /data")
    container.with_env("MINIO_ROOT_USER", "integration-access")
    container.with_env("MINIO_ROOT_PASSWORD", "integration-secret")
    container.with_env("MINIO_API_CORS_ALLOW_ORIGIN", "*")
    container.with_kwargs(labels={"tinkerfin.test/run": docker_test_run_id})
    container.waiting_for(
        HttpWaitStrategy(9000, "/minio/health/live").with_startup_timeout(60)
    )
    container_id = None
    try:
        with _running_container(container):
            container_id = container.get_wrapped_container().id
            endpoint = f"http://127.0.0.1:{container.get_exposed_port(9000)}"
            yield S3StorageSettings(
                bucket="test-" + uuid4().hex,
                endpoint=endpoint,
                public_endpoint=endpoint,
                access_key=SecretStr("integration-access"),
                secret_key=SecretStr("integration-secret"),
            )
    finally:
        assert all(
            c.id != container_id for c in docker_test_client.containers.list(all=True)
        )


async def test_real_direct_upload_download_and_reopening(minio_settings, database):
    data = b"# report\n" + b"content\n" * 180_000
    async with MinioAttachmentStorage.open(minio_settings) as storage:
        await storage.initialize()
        await storage.initialize()
        service = AttachmentService(database, storage)
        app = create_application(lifespan=None)
        app.state.resources = SimpleNamespace(attachments=service)
        app.dependency_overrides[get_user_context] = lambda: UserContext(
            user_id=1,
            username="test",
            display_name="test",
            avatar_url=None,
            roles=(),
            disabled=False,
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://studio"
        ) as api:
            async with httpx.AsyncClient(trust_env=False) as direct:
                response = await api.post(
                    "/api/attachments/uploads",
                    json={"name": "报告.md", "size_bytes": len(data)},
                )
                assert response.status_code == 200
                permit = response.json()["data"]
                preflight = await direct.options(
                    permit["url"],
                    headers={
                        "Origin": "http://localhost:5173",
                        "Access-Control-Request-Method": "POST",
                    },
                )
                assert preflight.status_code in {200, 204}
                assert preflight.headers["access-control-allow-origin"] in {
                    "*",
                    "http://localhost:5173",
                }
                oversized = await direct.post(
                    permit["url"],
                    data=permit["fields"],
                    files={"file": ("报告.md", data + b"x")},
                )
                assert oversized.status_code >= 400
                sent = await direct.post(
                    permit["url"],
                    data=permit["fields"],
                    files={"file": ("报告.md", data)},
                )
                assert sent.status_code == 204
                attachment_id = permit["attachment_id"]
                confirmed = await api.post(f"/api/attachments/{attachment_id}/complete")
                assert confirmed.status_code == 200
                assert confirmed.json()["data"]["size_bytes"] == len(data)
                signed = await api.get(f"/api/attachments/{attachment_id}/download-url")
                download = await direct.get(signed.json()["data"]["url"])
                assert download.content == data
                assert download.headers["content-disposition"].startswith("attachment;")
                assert (
                    await direct.get(
                        minio_settings.endpoint
                        + "/"
                        + minio_settings.bucket
                        + "/attachments/"
                        + attachment_id
                    )
                ).status_code == 403
                # 未过期的表单只能改写临时对象，正式下载内容不会变化
                await direct.post(
                    permit["url"],
                    data=permit["fields"],
                    files={"file": ("报告.md", b"z" * len(data))},
                )
                assert (await direct.get(signed.json()["data"]["url"])).content == data
    async with MinioAttachmentStorage.open(minio_settings) as reopened:
        assert await reopened.read(attachment_id) == data
        await reopened.delete(attachment_id)
        await reopened.delete(attachment_id)
        with pytest.raises(FileNotFoundError):
            await reopened.read(attachment_id)


async def test_initialization_preserves_other_application_lifecycle_rules(
    minio_settings,
):
    async with MinioAttachmentStorage.open(minio_settings) as storage:
        await storage.initialize()
        await storage._client.put_bucket_lifecycle_configuration(
            Bucket=minio_settings.bucket,
            LifecycleConfiguration={
                "Rules": [
                    {
                        "ID": "other-files",
                        "Status": "Enabled",
                        "Filter": {"Prefix": "reports/tmp/"},
                        "Expiration": {"Days": 7},
                    }
                ]
            },
        )
        await storage.initialize()
        result = await storage._client.get_bucket_lifecycle_configuration(
            Bucket=minio_settings.bucket
        )
        assert {rule.get("ID") for rule in result["Rules"]} == {
            "other-files",
            "studio-attachment-uploads",
        }

"""独占 MinIO 容器中的真实签名、对象持久化与 HTTP 附件契约"""

import io
import json
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from PIL import Image
from pydantic import SecretStr
from test_auth_service import TokenMemoryStore
from testcontainers.core.container import DockerContainer
from tests.support.docker_services import (
    _MappedPortHttpWaitStrategy,
    _running_container,
    _with_loopback_port,
)

from tinkerfin_studio.api.dependencies import get_auth_service, get_user_context
from tinkerfin_studio.application import create_application
from tinkerfin_studio.attachments.service import AttachmentService
from tinkerfin_studio.auth.models import User
from tinkerfin_studio.auth.repository import UserRepository
from tinkerfin_studio.auth.service import AuthService
from tinkerfin_studio.auth.types import UserContext
from tinkerfin_studio.config.settings import S3StorageSettings
from tinkerfin_studio.infrastructure.object_storage import MinioStorage

pytestmark = [pytest.mark.docker_integration, pytest.mark.usefixtures("projects")]
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
        _MappedPortHttpWaitStrategy(9000, "/minio/health/live").with_startup_timeout(60)
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


async def test_real_direct_upload_download_and_reopening(
    notifications, minio_settings, database, monkeypatch
):
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        monkeypatch.setenv(name, "http://127.0.0.1:9")
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.setenv(name, "")
    data = b"# report\n" + b"content\n" * 180_000
    async with MinioStorage.open(minio_settings) as storage:
        await storage.initialize()
        await storage.initialize()
        service = AttachmentService(database, storage, notifications=notifications)
        app = create_application(lifespan=None)
        app.state.resources = SimpleNamespace(attachments=service)
        app.dependency_overrides[get_user_context] = lambda: UserContext(
            user_id=1,
            username="test",
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
                    json={
                        "project_id": "project-1",
                        "name": "报告.md",
                        "size_bytes": len(data),
                    },
                )
                assert response.status_code == 200
                permit = response.json()["data"]
                preflight = await direct.options(
                    permit["url"],
                    headers={
                        "Origin": "http://localhost:5190",
                        "Access-Control-Request-Method": "POST",
                    },
                )
                assert preflight.status_code in {200, 204}
                assert preflight.headers["access-control-allow-origin"] in {
                    "*",
                    "http://localhost:5190",
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
    async with MinioStorage.open(minio_settings) as reopened:
        assert await reopened.read(attachment_id) == data
        await reopened.delete(attachment_id)
        await reopened.delete(attachment_id)
        with pytest.raises(FileNotFoundError):
            await reopened.read(attachment_id)


async def test_initialization_preserves_other_application_lifecycle_rules(
    minio_settings,
):
    async with MinioStorage.open(minio_settings) as storage:
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


async def test_avatar_urls_are_durable_and_other_objects_stay_private(
    minio_settings, session
):
    image_id = uuid4().hex
    attachment_id = uuid4().hex
    async with MinioStorage.open(minio_settings) as storage:
        await storage.initialize()
        await storage._client.put_bucket_policy(
            Bucket=minio_settings.bucket,
            Policy=json.dumps(
                {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Sid": "existing-reports",
                            "Effect": "Allow",
                            "Principal": {"AWS": ["*"]},
                            "Action": ["s3:GetObject"],
                            "Resource": [
                                f"arn:aws:s3:::{minio_settings.bucket}/reports/public/*"
                            ],
                        }
                    ],
                }
            ),
        )
        await storage.initialize()
        policy = json.loads(
            (await storage._client.get_bucket_policy(Bucket=minio_settings.bucket))[
                "Policy"
            ]
        )
        assert {entry["Sid"] for entry in policy["Statement"]} == {
            "existing-reports",
            "tinkerfin-avatars-read",
        }
        url = await storage.upload_avatar(image_id, b"avatar-image")
        with pytest.raises(OSError):
            await storage.upload_avatar(image_id, b"replacement")

        async def content():
            yield b"private-file"

        await storage.put(attachment_id, content())
        user = User(
            username="avatar-owner", password_hash="hash", roles=[], disabled=False
        )
        session.add(user)
        await session.commit()
        auth = AuthService(
            UserRepository(session), TokenMemoryStore(), token_expire_seconds=1800
        )
        context = await auth.get_user(user.id)
        app = create_application(lifespan=None)
        app.state.resources = SimpleNamespace(object_storage=storage)
        app.dependency_overrides[get_auth_service] = lambda: auth
        app.dependency_overrides[get_user_context] = lambda: context
        picture = io.BytesIO()
        Image.new("RGB", (512, 128), "blue").save(picture, "PNG")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://studio"
        ) as api:
            result = await api.put(
                "/api/user/me/avatar",
                content=picture.getvalue(),
                headers={"Content-Type": "image/png"},
            )
            assert result.status_code == 200
            current_url = result.json()["data"]["avatar_url"]
            assert current_url == user.avatar_url
            assert current_url.startswith(
                f"{minio_settings.public_endpoint}/{minio_settings.bucket}/avatars/"
            )
    async with httpx.AsyncClient(trust_env=False) as client:
        response = await client.get(url)
        assert response.status_code == 200
        assert response.content == b"avatar-image"
        assert response.headers["content-type"] == "image/jpeg"
        assert "X-Amz" not in url
        prefix = f"{minio_settings.public_endpoint}/{minio_settings.bucket}"
        assert (
            await client.get(prefix + "/attachments/" + attachment_id)
        ).status_code == 403
        assert (await client.get(prefix + "?list-type=2")).status_code == 403
        assert (await client.put(url, content=b"overwrite")).status_code == 403
        uploaded = await client.get(current_url)
        assert uploaded.status_code == 200
        with Image.open(io.BytesIO(uploaded.content)) as avatar:
            assert avatar.format == "JPEG"
            assert avatar.size == (256, 64)

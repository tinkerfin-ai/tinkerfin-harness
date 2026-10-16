"""个人服务配置、凭证归属与运行解析"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import uuid4

import anyio
from pydantic import TypeAdapter
from sqlalchemy.ext.asyncio import AsyncSession

from tinkerfin_studio.api.errors import BusinessException, ServiceErrorCode
from tinkerfin_studio.services.entity import ServiceConfig
from tinkerfin_studio.services.repository import ServiceConfigRepository
from tinkerfin_studio.services.schemas import (
    Capability,
    ImageConfig,
    SearchConfig,
    ServiceBinding,
    ServiceBindings,
    ServiceConfiguration,
    ServiceSave,
    ServiceSettings,
)

_configuration_adapter = TypeAdapter(ServiceConfiguration)


@asynccontextmanager
async def _write_transaction(session: AsyncSession) -> AsyncIterator[None]:
    """提交个人配置；失败或取消后等待请求会话回滚"""
    try:
        yield
        await session.commit()
    except BaseException as primary:

        async def rollback() -> BaseException | None:
            try:
                await session.rollback()
            except BaseException as error:  # noqa: BLE001 - 清理完成后保留调用方取消或原始异常
                return error
            return None

        cleanup = asyncio.create_task(rollback(), name="service-config-rollback")
        cancellation = primary if isinstance(primary, asyncio.CancelledError) else None
        with anyio.CancelScope(shield=True):
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError as error:
                    cancellation = cancellation or error
        cleanup_error = cleanup.result()
        if cancellation is not None:
            raise cancellation from cleanup_error
        if cleanup_error is not None:
            raise primary from cleanup_error
        raise


def _configuration(row: ServiceConfig) -> SearchConfig | ImageConfig:
    return _configuration_adapter.validate_python(row.config)


def _fingerprint(config: SearchConfig | ImageConfig, key: str) -> str:
    payload = config.model_dump(mode="json")
    encoded = json.dumps(
        payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode()
    return hashlib.sha256(encoded + b"\0" + key.encode()).hexdigest()


def _requires_key(config: SearchConfig | ImageConfig) -> bool:
    if config.provider_id != "custom":
        return True
    return config.request is not None and config.request.auth != "none"


def _auth_target(config: SearchConfig | ImageConfig) -> tuple[str, str, str, str, str]:
    request = config.request
    return (
        config.provider_id,
        config.endpoint.rstrip("/"),
        "preset" if request is None else request.auth,
        "" if request is None else request.header.casefold(),
        "" if request is None else request.prefix,
    )


@dataclass(frozen=True, slots=True)
class ResolvedService:
    """本次运行固定的服务参数和密钥，不写入运行登记"""

    id: str
    configuration: SearchConfig | ImageConfig
    fingerprint: str
    api_key: str = field(repr=False)


def bindings_for(
    search: ResolvedService | None, image: ResolvedService | None
) -> ServiceBindings:
    """仅把服务 ID 与摘要写入运行登记，不保存密钥"""
    return ServiceBindings(
        web_search=None
        if search is None
        else ServiceBinding(id=search.id, fingerprint=search.fingerprint),
        image_generation=None
        if image is None
        else ServiceBinding(id=image.id, fingerprint=image.fingerprint),
    )


def _settings(row: ServiceConfig) -> ServiceSettings:
    config = _configuration(row)
    fingerprint = _fingerprint(config, row.api_key)
    return ServiceSettings(
        id=row.id,
        configuration=config,
        enabled=row.enabled,
        has_key=bool(row.api_key),
        test_status=(
            "success"
            if row.test_fingerprint == fingerprint and row.test_status == "success"
            else "failed"
            if row.test_fingerprint == fingerprint and row.test_status == "failed"
            else None
        ),
        test_code=row.test_code if row.test_fingerprint == fingerprint else None,
        tested_at=row.tested_at.isoformat()
        if row.tested_at and row.test_fingerprint == fingerprint
        else None,
    )


class ServiceConfigService:
    """维护本人唯一配置，保存时不向提供方发请求"""

    def __init__(self, repository: ServiceConfigRepository) -> None:
        self._repository = repository

    async def settings(self, capability: Capability) -> ServiceSettings | None:
        row = await self._repository.get(capability)
        if row is None:
            return None
        return _settings(row)

    async def save(self, capability: Capability, value: ServiceSave) -> ServiceSettings:
        """串行保存本人配置，更换认证目标时要求重新提供密钥"""
        config = value.configuration
        if config.capability != capability:
            raise BusinessException(ServiceErrorCode.INVALID_CONFIGURATION)
        session = self._repository.session
        async with _write_transaction(session):
            await self._repository.lock_owner()
            row = await self._repository.get(capability, for_update=True)
            old_config = _configuration(row) if row is not None else None
            previous = (
                None
                if row is None or old_config is None
                else (_fingerprint(old_config, row.api_key), row.enabled)
            )
            key = ""
            if _requires_key(config):
                if value.api_key is not None:
                    key = value.api_key.get_secret_value().strip()
                elif row is not None and old_config is not None:
                    if _auth_target(old_config) != _auth_target(config):
                        raise BusinessException(ServiceErrorCode.KEY_ENDPOINT_CHANGED)
                    key = row.api_key
                if not key:
                    raise BusinessException(ServiceErrorCode.KEY_REQUIRED)
            if row is None:
                row = ServiceConfig(
                    id=str(uuid4()),
                    user_id=self._repository.user_id,
                    capability=capability,
                )
                session.add(row)
            changed = previous != (_fingerprint(config, key), value.enabled)
            row.provider_id = config.provider_id
            row.config = config.model_dump(mode="json")
            row.api_key = key
            row.enabled = value.enabled
            row.updated_at = datetime.now(UTC).replace(tzinfo=None)
            if changed:
                row.test_status = None
                row.test_code = None
                row.test_fingerprint = None
                row.tested_at = None
            await session.flush()
            result = _settings(row)
        return result

    async def clear(self, capability: Capability) -> None:
        session = self._repository.session
        async with _write_transaction(session):
            await self._repository.lock_owner()
            row = await self._repository.get(capability, for_update=True)
            if row is not None:
                await self._repository.delete(row)

    async def resolve(
        self, capability: Capability, *, for_update: bool = False
    ) -> ResolvedService | None:
        """只解析本人启用配置；缺失与停用都表示本次运行没有该服务"""
        row = await self._repository.get(capability, for_update=for_update)
        if row is None or not row.enabled:
            return None
        config = _configuration(row)
        return ResolvedService(
            row.id, config, _fingerprint(config, row.api_key), row.api_key
        )

    async def require_bound(
        self, capability: Capability, *, id: str, fingerprint: str
    ) -> ResolvedService:
        resolved = await self.resolve(capability, for_update=True)
        if resolved is None or resolved.id != id or resolved.fingerprint != fingerprint:
            raise BusinessException(ServiceErrorCode.CONFIGURATION_CHANGED)
        return resolved

    async def record_test(
        self, service: ResolvedService, *, status: str, code: str
    ) -> None:
        session = self._repository.session
        async with _write_transaction(session):
            await self._repository.lock_owner()
            row = await self._repository.get(
                service.configuration.capability, for_update=True
            )
            if row is None or row.id != service.id or not row.enabled:
                return
            config = _configuration(row)
            if _fingerprint(config, row.api_key) != service.fingerprint:
                return
            await self._repository.record_test(
                row, status=status, code=code, fingerprint=service.fingerprint
            )

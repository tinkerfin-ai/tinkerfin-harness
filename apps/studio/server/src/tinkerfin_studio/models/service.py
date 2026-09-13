"""用户提供方连接、模型目录和默认选择"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Literal

import anyio
from pydantic import SecretStr, ValidationError

from tinkerfin_studio.api.errors import (
    BusinessException,
    ModelErrorCode,
    SystemException,
)
from tinkerfin_studio.models.entity import AgentModel, ModelConnection
from tinkerfin_studio.models.repository import AgentModelRepository
from tinkerfin_studio.models.schemas import (
    AgentModelCatalog,
    AgentModelCatalogItem,
    AgentModelConfig,
    AgentModelSave,
    AgentModelSettings,
    AgentModelWrite,
    ModelConnectionSave,
    ModelConnectionSettings,
    ModelProvider,
)
from tinkerfin_studio.models.transport import validate_model_url


def model_settings(row: AgentModel) -> AgentModelSettings:
    return AgentModelSettings.model_validate(row, from_attributes=True)


def connection_provider(connection: ModelConnection) -> ModelProvider:
    """将服务选择对应到已安装的模型集成"""
    if connection.api_type == "ollama":
        return "ollama"
    return "deepseek" if connection.provider_id == "deepseek" else "openai"


def resolved_model(
    value: AgentModelWrite, connection: ModelConnection
) -> AgentModelConfig:
    """绑定已经核验归属的连接，供本次运行或测试使用"""
    if (
        connection.auth_type == "api_key"
        or connection_provider(connection) == "deepseek"
    ) and not connection.api_key.strip():
        raise BusinessException(ModelErrorCode.KEY_REQUIRED)
    if value.purpose == "image" and connection.api_type != "openai_chat_completions":
        raise BusinessException(
            ModelErrorCode.INVALID_CONFIGURATION, message="图片生成需要 OpenAI 兼容接口"
        )
    options = value.chat_options
    if connection.api_type != "ollama" and (
        options.context_window is not None or options.keep_alive is not None
    ):
        raise BusinessException(
            ModelErrorCode.INVALID_CONFIGURATION,
            message="上下文容量和保持加载时间仅适用于 Ollama",
        )
    if value.reasoning_enabled and connection_provider(connection) == "openai":
        raise BusinessException(
            ModelErrorCode.INVALID_CONFIGURATION,
            message="当前兼容接口不提供推理内容展示，请使用推理强度参数",
        )
    return AgentModelConfig.model_validate(
        {
            **value.model_dump(),
            "provider": connection_provider(connection),
            "base_url": connection.base_url,
            "api_key": SecretStr(connection.api_key),
        }
    )


class AgentModelService:
    """维护本人连接与模型，在同一用户锁内完成配置和默认项写入"""

    def __init__(self, repository: AgentModelRepository) -> None:
        self._repository = repository

    async def list_catalog(self) -> AgentModelCatalog:
        rows = await self._repository.list_enabled()
        try:
            items = [
                AgentModelCatalogItem.model_validate(row, from_attributes=True)
                for row in rows
            ]
        except ValidationError as error:
            raise SystemException(ModelErrorCode.CATALOG_UNAVAILABLE) from error
        return AgentModelCatalog(
            items=items,
            defaultModelId=next(
                (item.model_id for item in items if item.is_default), None
            ),
        )

    async def resolve(
        self, model_id: str, *, purpose: Literal["chat", "image"] = "chat"
    ) -> AgentModelConfig:
        """核验本人模型的用途和启用状态，固定本次调用的连接与生成参数"""
        row = await self._repository.get(model_id)
        if row is None:
            raise BusinessException(ModelErrorCode.NOT_FOUND)
        if not row.enabled:
            raise BusinessException(ModelErrorCode.DISABLED)
        if row.purpose != purpose:
            raise BusinessException(ModelErrorCode.PURPOSE_MISMATCH)
        try:
            return resolved_model(
                model_settings(row), await self.require_connection(row.connection_id)
            )
        except ValidationError as error:
            raise SystemException(ModelErrorCode.CATALOG_UNAVAILABLE) from error

    async def settings(self) -> list[AgentModelSettings]:
        return [model_settings(row) for row in await self._repository.list_settings()]

    async def connections(self) -> list[ModelConnectionSettings]:
        return [
            ModelConnectionSettings.model_validate(
                {
                    "connection_id": row.connection_id,
                    "display_name": row.display_name,
                    "provider_id": row.provider_id,
                    "api_type": row.api_type,
                    "base_url": row.base_url,
                    "auth_type": row.auth_type,
                    "has_key": bool(row.api_key),
                }
            )
            for row in await self._repository.connections()
        ]

    async def require_connection(
        self, connection_id: str, *, for_update: bool = False
    ) -> ModelConnection:
        connection = await self._repository.connection(
            connection_id, for_update=for_update
        )
        if connection is None:
            raise BusinessException(
                ModelErrorCode.INVALID_CONFIGURATION,
                message="提供方连接不存在，请先添加连接",
            )
        return connection

    async def resolve_draft(self, value: AgentModelSave) -> AgentModelConfig:
        """测试未保存的模型参数，使用本人已保存连接，外部请求前归还数据库会话"""
        return resolved_model(value, await self.require_connection(value.connection_id))

    async def save_connection(self, value: ModelConnectionSave) -> None:
        """保存连接，地址或接口改变时禁止复用密钥，运行期间保护关联模型

        Args:
            value: 本人连接的完整配置；无需认证时清除密钥

        Raises:
            BusinessException: 地址、认证或接口不合法，或关联模型仍在使用
        """
        async with self._write_transaction():
            await self._repository.lock_owner()
            try:
                validate_model_url(value.base_url)
            except ValueError as error:
                raise BusinessException(
                    ModelErrorCode.INVALID_CONFIGURATION, message=str(error)
                ) from error
            existing = await self._repository.connection(
                value.connection_id, for_update=True
            )
            rows = await self._repository.connection_models(value.connection_id)
            for row in rows:
                if await self._repository.in_use(row.model_id):
                    raise BusinessException(ModelErrorCode.IN_USE)
            if (
                any(row.purpose == "image" for row in rows)
                and value.api_type != "openai_chat_completions"
            ):
                raise BusinessException(
                    ModelErrorCode.INVALID_CONFIGURATION,
                    message="连接下已有生图模型，请先移除后再更改接口",
                )
            if (
                value.provider_id == "deepseek"
                and value.api_type == "openai_chat_completions"
                and value.auth_type == "none"
            ):
                raise BusinessException(
                    ModelErrorCode.INVALID_CONFIGURATION,
                    message="DeepSeek 连接需要 API 密钥",
                )
            key = ""
            if value.auth_type == "api_key":
                if value.api_key is None:
                    if existing is not None and (
                        existing.base_url.rstrip("/") != value.base_url.rstrip("/")
                        or existing.api_type != value.api_type
                    ):
                        raise BusinessException(ModelErrorCode.KEY_ENDPOINT_CHANGED)
                    key = existing.api_key if existing is not None else ""
                else:
                    key = value.api_key.get_secret_value().strip()
                if not key:
                    raise BusinessException(ModelErrorCode.KEY_REQUIRED)
            # 更换接口前校验已有模型参数，避免连接保存后才发现模型不可用
            candidate = ModelConnection(
                api_type=value.api_type,
                provider_id=value.provider_id,
                base_url=value.base_url,
                auth_type=value.auth_type,
                api_key=key,
            )
            for row in rows:
                resolved_model(model_settings(row), candidate)
            await self._repository.save_connection(value, key)
            await self._repository.commit()

    async def delete_connection(self, connection_id: str) -> None:
        """删除本人连接及其模型配置，保留会话和运行历史"""
        async with self._write_transaction():
            await self._repository.lock_owner()
            for row in await self._repository.connection_models(connection_id):
                if await self._repository.in_use(row.model_id):
                    raise BusinessException(ModelErrorCode.IN_USE)
            await self._repository.delete_connection(connection_id)
            await self._repository.commit()

    async def save_settings(self, value: AgentModelSave) -> None:
        """保存模型参数，引用本人已保存的连接；事务失败或取消时回滚"""
        await self.save_models([value])

    async def save_models(self, values: list[AgentModelSave]) -> None:
        """在一次事务内保存所选模型，任一项不合法时全部回滚"""
        async with self._write_transaction():
            await self._repository.lock_owner()
            if len({value.model_id for value in values}) != len(values):
                raise BusinessException(
                    ModelErrorCode.INVALID_CONFIGURATION,
                    message="同一批次包含重复模型标识",
                )
            for value in values:
                if await self._repository.in_use(value.model_id):
                    raise BusinessException(ModelErrorCode.IN_USE)
                resolved_model(
                    value,
                    await self.require_connection(value.connection_id, for_update=True),
                )
                if value.is_default:
                    await self._repository.clear_default(value.purpose)
                await self._repository.upsert(value)
            await self._repository.commit()

    async def set_default(self, model_id: str) -> None:
        """启用本人模型并设为同用途默认项，不改变连接或进行中的运行"""
        async with self._write_transaction():
            await self._repository.lock_owner()
            model = await self._repository.get_for_update(model_id)
            if model is None:
                raise BusinessException(ModelErrorCode.NOT_FOUND)
            resolved_model(
                model_settings(model),
                await self.require_connection(model.connection_id, for_update=True),
            )
            await self._repository.set_default(model_id, purpose=model.purpose)
            await self._repository.commit()

    async def delete_settings(self, model_id: str) -> None:
        """删除本人模型配置，事务失败或取消时回滚，会话历史保持不变"""
        async with self._write_transaction():
            await self._repository.lock_owner()
            if await self._repository.in_use(model_id):
                raise BusinessException(ModelErrorCode.IN_USE)
            await self._repository.delete(model_id)
            await self._repository.commit()

    @asynccontextmanager
    async def _write_transaction(self) -> AsyncIterator[None]:
        """模型写命令失败时完成一次回滚，保留原始失败与重复取消"""

        try:
            yield
        except BaseException as primary:  # noqa: BLE001 - 完成回滚后继续交付原始失败

            async def rollback() -> BaseException | None:
                try:
                    await self._repository.rollback()
                except BaseException as error:  # noqa: BLE001 - 控制异常由写命令所有者交付
                    return error
                return None

            task = asyncio.create_task(rollback(), name="studio-model-rollback")
            failures: list[BaseException] = [primary]
            cancellation = (
                primary if isinstance(primary, asyncio.CancelledError) else None
            )
            with anyio.CancelScope(shield=True):
                while True:
                    try:
                        await asyncio.wait((task,))
                    except asyncio.CancelledError as error:
                        if cancellation is None:
                            cancellation = error
                            failures.append(error)
                        if not task.done():
                            continue
                    break
            rollback_failure = task.result()
            if rollback_failure is not None:
                failures.append(rollback_failure)
            chosen = next(
                (
                    error
                    for error in failures
                    if not isinstance(error, (Exception, asyncio.CancelledError))
                ),
                cancellation or primary,
            )
            remaining = [error for error in failures if error is not chosen]
            if remaining:
                secondary = (
                    remaining[0]
                    if len(remaining) == 1
                    else BaseExceptionGroup("模型写入和回滚同时失败", remaining)
                )
                try:
                    raise secondary
                except BaseException:  # noqa: BLE001 - 保留各异常既有cause，以context交付另一项失败
                    raise chosen
            raise chosen

    async def resolve_image_model(self) -> AgentModelConfig | None:
        """返回本人启用的默认生图服务，未设置默认项时不调用其他服务"""
        rows = await self._repository.list_settings()
        selected = next(
            (
                row
                for row in rows
                if row.purpose == "image" and row.enabled and row.is_default
            ),
            None,
        )
        if selected is None:
            return None
        return await self.resolve(selected.model_id, purpose="image")

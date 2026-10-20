"""在用户隔离的持久 Store 中保存完整技能目录"""

import base64
import binascii

import anyio
from anyio.to_thread import run_sync
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from tinkerfin import TinkerFin
from tinkerfin_studio.api.errors import BusinessException, SkillErrorCode
from tinkerfin_studio.skills.packages import (
    MAX_FILES,
    SkillFile,
    SkillPackage,
    parse_package,
)


class _StoredFile(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    path: str
    content: str = Field(description="原文件字节的 base64 编码")


class _StoredPackage(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    files: list[_StoredFile] = Field(min_length=1, max_length=MAX_FILES)


def _encode(package: SkillPackage) -> dict[str, object]:
    return {
        "files": [
            {
                "path": file.path,
                "content": base64.b64encode(file.content).decode("ascii"),
            }
            for file in package.files
        ]
    }


def _decode(value: dict[str, object], digest: str) -> SkillPackage:
    try:
        stored = _StoredPackage.model_validate(value)
        package = parse_package(
            tuple(
                SkillFile(file.path, base64.b64decode(file.content, validate=True))
                for file in stored.files
            )
        )
        if package.digest != digest:
            raise ValueError("技能内容摘要不匹配")
        return package
    except (ValueError, binascii.Error, ValidationError, BusinessException) as error:
        raise BusinessException(SkillErrorCode.CONTENT_UNAVAILABLE) from error


class SkillContentStore:
    """借用框架公开 Store，在安装发布前完成单个内容对象的持久写入"""

    def __init__(self, tinkerfin: TinkerFin) -> None:
        self._tinkerfin = tinkerfin
        self._limiter = anyio.CapacityLimiter(2)

    async def save(self, user_id: int, package: SkillPackage) -> None:
        """内容摘要为键；同一用户相同内容重复保存结果不变"""
        value = await run_sync(_encode, package, limiter=self._limiter)
        await self._tinkerfin.with_namespace(f"ns_{user_id}").store.aput(
            ("skills",), package.digest, value
        )

    async def load(self, user_id: int, digest: str) -> SkillPackage:
        """读取原始字节并验证摘要，内容缺失时阻止执行"""
        item = await self._tinkerfin.with_namespace(f"ns_{user_id}").store.aget(
            ("skills",), digest
        )
        if item is None:
            raise BusinessException(SkillErrorCode.CONTENT_UNAVAILABLE)
        return await run_sync(_decode, item.value, digest, limiter=self._limiter)

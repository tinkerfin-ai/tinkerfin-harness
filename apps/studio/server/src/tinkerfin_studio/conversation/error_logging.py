"""将流内失败写入宿主日志，关联会话身份并保护模型凭据"""

import logging
import re
import traceback

from anyio import CapacityLimiter
from anyio.to_thread import run_sync

from tinkerfin import RunIdentity
from tinkerfin_studio.models.schemas import AgentModelConfig

logger = logging.getLogger(__name__)
_FORMAT_LIMITER = CapacityLimiter(2)


def _failure_detail(error: Exception | None, keys: tuple[str, ...]) -> str:
    if error is None:
        return "未提供原始异常"
    detail = "".join(
        traceback.TracebackException.from_exception(
            error, limit=30, capture_locals=False
        ).format()
    )
    for key in keys:
        if key:
            detail = detail.replace(key, "[redacted]")
    detail = re.sub(r"\b(?:sk-|tvly-)[A-Za-z0-9_-]+", "[redacted]", detail)
    return detail[-16000:]


async def log_conversation_error(
    *,
    identity: RunIdentity,
    model: AgentModelConfig,
    image_model: AgentModelConfig | None,
    code: str | None,
    error: Exception | None,
) -> None:
    """记录实时主运行失败；格式化最多占用两个线程，不读取模型消息

    Args:
        identity: 当前用户作用域内的会话与运行身份
        model: 本次聊天模型及其需要脱敏的凭据
        image_model: 本次生图配置，用于脱敏相关异常
        code: 框架提供的失败分类
        error: 公开运行流保留的原始异常
    """
    keys = (model.api_key.get_secret_value(),)
    if image_model is not None:
        keys += (image_model.api_key.get_secret_value(),)
    detail = await run_sync(_failure_detail, error, keys, limiter=_FORMAT_LIMITER)
    logger.error(
        "会话执行失败 namespace=%s thread_id=%s run_id=%s model_id=%s "
        "model=%s provider=%s code=%s\n%s",
        identity.namespace,
        identity.thread_id,
        identity.run_id,
        model.model_id,
        model.model_name,
        model.provider,
        code,
        detail,
    )

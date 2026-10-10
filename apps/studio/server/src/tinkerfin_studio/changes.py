"""已提交业务变化的通知，不携带正文或读取许可"""

import logging
from typing import Literal

from pydantic import JsonValue

from tinkerfin_notifications import (
    Notification,
    NotificationError,
    Notifications,
    NotificationScope,
)

logger = logging.getLogger(__name__)

StudioChangeTopic = Literal[
    "studio.projects.changed",
    "studio.memories.changed",
    "studio.conversation.changed",
    "studio.conversation.title.changed",
    "studio.conversation.interactions.changed",
    "studio.attachments.changed",
    "studio.skills.changed",
]


async def notify_change(
    notifications: Notifications,
    *,
    user_id: int,
    topic: StudioChangeTopic,
    key: str,
    details: dict[str, JsonValue] | None = None,
) -> None:
    """提交并释放业务锁后提示当前用户重新读取资源

    通知只保证有界受理，投递失败由通知服务诊断；受理失败不撤销业务提交。
    调用方只提供资源标识及顺序号，客户端仍须通过鉴权接口读取内容。
    """
    try:
        await notifications.publish(
            Notification(
                scope=NotificationScope(f"ns_{user_id}"),
                topic=topic,
                key=key,
                details=details or {},
            )
        )
    except NotificationError as error:
        logger.warning("业务变化通知未受理 topic=%s code=%s", topic, error.code)

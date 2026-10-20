"""用户沙箱与预热容量的生命周期日志"""

from __future__ import annotations

import json
import logging

from tinkerfin_sandbox import OpenSandboxLifecycleEvent, OpenSandboxLifecycleEventType

logger = logging.getLogger(__name__)


class SandboxEventLogger:
    """将已发生的沙箱状态变化写入宿主日志，不参与恢复决策"""

    async def on_sandbox_event(self, event: OpenSandboxLifecycleEvent) -> None:
        """记录事件身份、原因和工作区变化提示，供排查故障与恢复

        Args:
            event: 框架已确认的生命周期事件，诊断内容不写入日志
        """
        if event.type is OpenSandboxLifecycleEventType.RECOVERY_FAILED:
            level = logging.ERROR
        elif event.type in {
            OpenSandboxLifecycleEventType.UNAVAILABLE,
            OpenSandboxLifecycleEventType.WARM_CAPACITY_DEGRADED,
        }:
            level = logging.WARNING
        else:
            level = logging.INFO
        if not logger.isEnabledFor(level):
            return
        # 限制身份长度并转义控制字符，使异常身份也只能产生一条有界日志
        payload = {
            "event": event.type.value,
            "reason": event.reason.value,
            "event_id": event.event_id[:64],
            "owner_key": None if event.owner_key is None else event.owner_key[:128],
            "occurred_at": event.occurred_at.isoformat(),
            "workspace_may_have_changed": event.workspace_may_have_changed,
        }
        logger.log(level, "沙箱生命周期 %s", json.dumps(payload, separators=(",", ":")))

"""自动化任务的用户与项目归属"""


def automation_owner(user_id: int, project_id: str) -> str:
    """生成交给调度服务的归属标识，查询和通知都使用同一范围"""
    if user_id < 1 or not project_id or ":" in project_id:
        raise ValueError("自动化任务归属无效")
    return f"{user_id}:{project_id}"


def parse_automation_owner(owner_id: str) -> tuple[int, str]:
    """解析已持久化的任务归属，拒绝不完整或非规范标识"""
    user, project_id = owner_id.split(":", 1)
    user_id = int(user)
    if automation_owner(user_id, project_id) != owner_id:
        raise ValueError("自动化任务归属无效")
    return user_id, project_id


def automation_execution_namespace(owner_id: str) -> str:
    """自动化与会话使用同一用户身份，执行工作区仍由任务项目决定"""
    user_id, _ = parse_automation_owner(owner_id)
    return f"ns_{user_id}"

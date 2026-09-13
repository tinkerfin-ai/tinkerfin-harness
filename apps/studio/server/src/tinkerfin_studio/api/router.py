"""应用 API 路由装配"""

from fastapi import APIRouter

from tinkerfin_studio.api.attachment_router import router as attachment_router
from tinkerfin_studio.api.auth_router import router as auth_router
from tinkerfin_studio.api.automation_router import router as automation_router
from tinkerfin_studio.api.conversation_router import router as conversation_router
from tinkerfin_studio.api.model_router import router as model_router
from tinkerfin_studio.api.user_router import router as user_router


def create_api_router() -> APIRouter:
    """创建带统一 `/api` 前缀的顶层路由"""

    router = APIRouter(prefix="/api")
    router.include_router(automation_router)
    router.include_router(attachment_router)
    router.include_router(auth_router)
    router.include_router(conversation_router)
    router.include_router(model_router)
    router.include_router(user_router)
    return router

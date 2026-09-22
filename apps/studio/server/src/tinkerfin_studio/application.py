"""FastAPI 应用装配入口"""

from typing import cast

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse
from starlette.types import ExceptionHandler, Lifespan

from tinkerfin_studio.api.errors import (
    ApplicationException,
    application_exception_handler,
    http_exception_handler,
    request_validation_handler,
    unexpected_exception_handler,
)
from tinkerfin_studio.api.router import create_api_router
from tinkerfin_studio.version import __version__


def create_application(
    *,
    lifespan: Lifespan[FastAPI] | None,
) -> FastAPI:
    """创建不隐式连接外部资源的 FastAPI 应用"""

    application = FastAPI(
        title="TinkerFin Studio",
        version=__version__,
        lifespan=lifespan,
    )
    # 浏览器可从任意来源连接，认证仍使用显式 Bearer 令牌，不依赖 Cookie
    application.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["Content-Disposition"],
        allow_credentials=False,
    )
    application.add_exception_handler(
        ApplicationException,
        cast(ExceptionHandler, application_exception_handler),
    )
    application.add_exception_handler(
        RequestValidationError,
        cast(ExceptionHandler, request_validation_handler),
    )
    application.add_exception_handler(
        HTTPException,
        cast(ExceptionHandler, http_exception_handler),
    )
    application.add_exception_handler(
        Exception,
        cast(ExceptionHandler, unexpected_exception_handler),
    )
    application.include_router(create_api_router())

    @application.get("/health/live", include_in_schema=False)
    async def liveness() -> dict[str, str]:
        """返回进程存活状态"""

        return {"status": "ok"}

    @application.get("/health/ready", include_in_schema=False)
    async def readiness() -> JSONResponse:
        """返回请求链外部依赖就绪状态"""

        resources = getattr(application.state, "resources", None)
        if resources is None:
            return JSONResponse(
                status_code=503,
                content={"status": "not_ready", "components": {}},
            )
        components = await resources.readiness.check()
        ready = all(components.values())
        return JSONResponse(
            status_code=200 if ready else 503,
            content={
                "status": "ready" if ready else "not_ready",
                "components": components,
            },
        )

    return application

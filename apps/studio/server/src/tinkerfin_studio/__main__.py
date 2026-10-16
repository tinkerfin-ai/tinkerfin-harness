"""TinkerFin Studio 服务进程入口"""

from argparse import ArgumentParser, BooleanOptionalAction, Namespace
from collections.abc import Sequence

import uvicorn
from fastapi import FastAPI

from tinkerfin_studio.application import create_application
from tinkerfin_studio.config.logging import setup_console_logging
from tinkerfin_studio.config.settings import get_settings
from tinkerfin_studio.resources import build_lifespan


def create_app() -> FastAPI:
    """在服务进程中创建应用并由生命周期加载资源"""
    return create_application(lifespan=build_lifespan())


def parse_args(args: Sequence[str] | None = None) -> Namespace:
    """解析服务监听与热重载参数"""

    parser = ArgumentParser(description="启动 TinkerFin Studio 服务")
    parser.add_argument("--host", default="127.0.0.1", help="服务监听地址")
    parser.add_argument("--port", type=int, default=8090, help="服务监听端口")
    parser.add_argument(
        "--reload",
        action=BooleanOptionalAction,
        default=True,
        help="是否开启开发热重载",
    )
    parser.add_argument(
        "--graceful-shutdown-timeout-seconds",
        type=int,
        default=10,
        help="停止接收请求后等待现有连接结束的秒数",
    )
    return parser.parse_args(args)


def main(args: Sequence[str] | None = None) -> None:
    """按命令行参数启动 HTTP 服务"""

    options = parse_args(args)
    setup_console_logging(get_settings().log_level)
    uvicorn.run(
        "tinkerfin_studio.__main__:create_app",
        factory=True,
        host=options.host,
        port=options.port,
        reload=options.reload,
        timeout_graceful_shutdown=options.graceful_shutdown_timeout_seconds,
        log_config=None,
    )


if __name__ == "__main__":
    main()

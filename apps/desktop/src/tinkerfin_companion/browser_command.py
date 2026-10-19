#!/usr/bin/env python3
"""向当前执行的私有入口请求浏览器，然后直接运行随附的原生命令"""

import json
import os
import socket
import sys


def main() -> None:
    """同步 CLI 准备只处理进程连接，不解释网页动作或读取用户浏览器资料"""
    endpoint = os.environ.get("TINKERFIN_BROWSER_SOCKET")
    token = os.environ.get("TINKERFIN_BROWSER_TOKEN")
    if not endpoint or not token or len(token) != 64:
        raise RuntimeError("当前执行没有本机浏览器入口")
    instructions = tuple(sys.argv[1:3]) in {
        ("skills", "get"),
        ("skills", "list"),
    } or sys.argv[1:] in (["--help"], ["-h"], ["--version"], ["-V"], ["help"])
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(10)
        connection.connect(endpoint)
        action = b"describe " if instructions else b"prepare "
        connection.sendall(action + token.encode("ascii") + b"\n")
        # 等待和取消由外层命令期限统一控制，不额外缩短首次下载的等待时间
        connection.settimeout(None)
        with connection.makefile("rb") as response:
            payload = response.readline(16 * 1024 + 1)
            if payload == b'{"status":"preparing"}\n':
                print("正在准备浏览器…", file=sys.stderr, flush=True)
                payload = response.readline(16 * 1024 + 1)
    if len(payload) > 16 * 1024:
        raise ValueError("浏览器准备响应过大")
    reply = json.loads(payload)
    if not isinstance(reply, dict) or set(reply) != {"cli", "environment"}:
        raise RuntimeError("浏览器未能准备完成")
    cli, environment = reply["cli"], reply["environment"]
    if (
        not isinstance(cli, str)
        or not os.path.isabs(cli)
        or not isinstance(environment, dict)
        or any(
            not isinstance(key, str)
            or not key.startswith("AGENT_BROWSER_")
            or not isinstance(value, str)
            for key, value in environment.items()
        )
        or "AGENT_BROWSER_DAEMON" in environment
    ):
        raise ValueError("浏览器准备响应无效")
    current = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("AGENT_BROWSER_")
    }
    os.execve(cli, [cli, *sys.argv[1:]], {**current, **environment})


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, ValueError, TypeError):
        print("浏览器未能准备完成，请重试当前操作", file=sys.stderr)
        raise SystemExit(1) from None

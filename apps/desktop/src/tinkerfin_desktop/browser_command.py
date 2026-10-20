#!/usr/bin/env python3
"""向当前执行的私有入口请求浏览器，并调用官方工具目录中的动作"""

import argparse
import json
import os
import socket
import sys
from pathlib import Path

_INPUT_LIMIT = 1024 * 1024
_OUTPUT_LIMIT = 8 * 1024 * 1024


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="tinkerfin-browser",
        description=Path(__file__).resolve().with_name("browser_help.md").read_text(),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    commands = parser.add_subparsers(dest="method", required=True)
    commands.add_parser("tools", help="列出可用工具")
    describe = commands.add_parser("describe", help="读取工具用途与参数")
    describe.add_argument("name")
    call = commands.add_parser("call", help="执行一个浏览器工具")
    call.add_argument("name")
    call.add_argument("arguments", help="符合工具参数的 JSON 对象")
    return parser.parse_args()


def prepare() -> dict[str, str]:
    endpoint = os.environ.get("TINKERFIN_BROWSER_SOCKET")
    token = os.environ.get("TINKERFIN_BROWSER_TOKEN")
    if not endpoint or not token or len(token) != 64:
        raise RuntimeError("当前执行没有本机浏览器入口")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(10)
        connection.connect(endpoint)
        connection.sendall(b"prepare " + token.encode("ascii") + b"\n")
        # 首次下载的等待与取消由外层命令期限统一控制
        connection.settimeout(None)
        with connection.makefile("rb") as response:
            payload = response.readline(16 * 1024 + 1)
            if payload == b'{"status":"preparing"}\n':
                print("正在准备浏览器…", file=sys.stderr, flush=True)
                payload = response.readline(16 * 1024 + 1)
    if len(payload) > 16 * 1024:
        raise ValueError("浏览器准备响应过大")
    reply = json.loads(payload)
    if not isinstance(reply, dict) or set(reply) != {"environment"}:
        raise RuntimeError("浏览器未能准备完成")
    environment = reply["environment"]
    if (
        not isinstance(environment, dict)
        or set(environment)
        != {"TINKERFIN_BROWSER_SESSION_SOCKET", "TINKERFIN_BROWSER_SESSION_TOKEN"}
        or any(not isinstance(value, str) for value in environment.values())
        or not os.path.isabs(environment["TINKERFIN_BROWSER_SESSION_SOCKET"])
        or len(environment["TINKERFIN_BROWSER_SESSION_TOKEN"]) != 64
    ):
        raise ValueError("浏览器准备响应无效")
    return environment


def main() -> None:
    """同步命令只请求当前会话，不启动守护进程或自动重试动作"""
    selected = arguments()
    request = {"method": selected.method}
    if selected.method in {"describe", "call"}:
        request["name"] = selected.name
    if selected.method == "call":
        if len(selected.arguments.encode()) > _INPUT_LIMIT - 4096:
            raise ValueError("浏览器输入超过容量")
        value = json.loads(selected.arguments)
        if not isinstance(value, dict):
            raise ValueError("浏览器参数必须是 JSON 对象")
        request["arguments"] = value
    # 参数在准备浏览器前验证，错误命令不能产生启动副作用
    json.dumps(request, allow_nan=False)
    environment = prepare()
    request["token"] = environment["TINKERFIN_BROWSER_SESSION_TOKEN"]
    payload = json.dumps(request, allow_nan=False).encode() + b"\n"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(10)
        connection.connect(environment["TINKERFIN_BROWSER_SESSION_SOCKET"])
        connection.sendall(payload)
        connection.settimeout(None)
        with connection.makefile("rb") as response:
            content = response.readline(_OUTPUT_LIMIT + 1)
    if len(content) > _OUTPUT_LIMIT:
        raise ValueError("浏览器响应超过容量")
    reply = json.loads(content)
    if not isinstance(reply, dict) or set(reply) not in ({"result"}, {"error"}):
        raise ValueError("浏览器响应无效")
    if "error" in reply:
        print(reply["error"], file=sys.stderr)
        raise SystemExit(1)
    result = reply["result"]
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    if isinstance(result, dict) and result.get("isError") is True:
        raise SystemExit(1)


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, ValueError, TypeError):
        print("浏览器操作未确认完成，请核对结果后重试", file=sys.stderr)
        raise SystemExit(1) from None

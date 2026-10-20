"""读取 macOS 应用资源目录中的固定程序，不从工作区发现可执行文件"""

import os
import platform
import sys
from dataclasses import dataclass
from pathlib import Path

from .browser import BrowserInstallation


@dataclass(frozen=True, slots=True)
class Installation:
    """原生入口提供的已安装资源，构建脚本负责相同目录约定"""

    directory: Path
    sandbox: Path
    browser: BrowserInstallation

    @classmethod
    def read(cls, directory: Path) -> "Installation":
        """核验平台和随附程序；由启动入口通过单个受控工作线程调用"""
        if sys.platform != "darwin" or platform.machine() != "arm64":
            raise ValueError("本机客户端需要 Apple Silicon Mac")
        root = directory.resolve(strict=True)

        def resource(relative: str, *, executable: bool = False) -> Path:
            path = (root / relative).resolve(strict=True)
            if not path.is_relative_to(root) or not path.is_file():
                raise ValueError("应用资源不完整或指向安装目录之外")
            if executable and not os.access(path, os.X_OK):
                raise ValueError("应用程序缺少执行权限，请重新安装")
            return path

        node = resource("node/bin/node", executable=True)
        resource("node/bin/npm", executable=True)
        resource("node/bin/npx", executable=True)
        resource(
            "python/lib/python3.11/site-packages/tinkerfin_desktop/browser_command.py",
            executable=True,
        )
        resource(
            "python/lib/python3.11/site-packages/tinkerfin_desktop/browser_help.md"
        )
        resource("runtime/node_modules/@playwright/mcp/package.json")
        resource("runtime/node_modules/playwright/package.json")
        return cls(
            directory=root,
            sandbox=resource(
                "runtime/node_modules/@anthropic-ai/sandbox-runtime/dist/cli.js",
                executable=True,
            ),
            browser=BrowserInstallation(
                node=node,
                service=resource("runtime/browser-service.mjs"),
            ),
        )

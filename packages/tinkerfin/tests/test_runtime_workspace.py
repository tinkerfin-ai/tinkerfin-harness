"""Run-scoped workspace borrowing and role-local business tools."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from contextvars import ContextVar
from pathlib import Path

from deepagents.backends import StateBackend
from deepagents.backends.protocol import BackendProtocol

from tinkerfin_contracts import (
    PreparedWorkspace,
    RunIdentity,
)


class _Workspace:
    def __init__(self, backend: BackendProtocol | None = None) -> None:
        self.backend = StateBackend() if backend is None else backend
        self.opened: list[RunIdentity] = []
        self.closed: list[RunIdentity] = []
        self.context = ContextVar("test_workspace", default="outside")

    @asynccontextmanager
    async def prepare(
        self, identity: RunIdentity
    ) -> AsyncGenerator[PreparedWorkspace[Path, BackendProtocol]]:
        self.opened.append(identity)
        token = self.context.set(identity.namespace)
        try:
            yield PreparedWorkspace(
                Path("/files") / identity.namespace,
                self.backend,
                filesystem_instructions="File paths are relative to the selected workspace.",
            )
        finally:
            self.context.reset(token)
            self.closed.append(identity)

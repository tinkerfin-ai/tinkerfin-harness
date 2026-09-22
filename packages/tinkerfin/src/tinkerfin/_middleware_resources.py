"""Keep declared Deep Agents middleware within the Runtime's borrowed resources.

Deep Agents 0.7.5 accepts backend instances inside its public middleware as well as
the graph factory. These known declarations share the same Store validation and
async adaptation. Arbitrary application objects are not inspected or rewritten.
"""

from collections.abc import Sequence
from copy import copy
from typing import Any, cast

from deepagents.backends.protocol import BackendProtocol
from deepagents.middleware.filesystem import FilesystemMiddleware, FsToolName
from deepagents.middleware.memory import MemoryMiddleware
from deepagents.middleware.skills import SkillsMiddleware
from deepagents.middleware.subagents import SubAgentMiddleware
from deepagents.middleware.summarization import (
    SummarizationMiddleware,
    SummarizationToolMiddleware,
)
from langchain.agents.middleware import AgentMiddleware

from ._store import validate_store_backend
from ._store_backend import async_store_backend
from ._subagents import validate_subagent_resources
from ._summarization import ObservedCompactionTool, observe_summarization

_Middleware = AgentMiddleware[Any, Any, Any]


def _backend(middleware: _Middleware) -> BackendProtocol | None:
    if isinstance(middleware, FilesystemMiddleware):
        return middleware.backend
    if isinstance(
        middleware, (MemoryMiddleware, SkillsMiddleware, SummarizationMiddleware)
    ):
        return middleware._backend
    return None


def validate_middleware_resources(middleware: Sequence[_Middleware]) -> None:
    """Reject visible Store or compiled-Graph overrides before any run starts."""

    for item in middleware:
        validate_store_backend(_backend(item))
        if isinstance(item, SummarizationToolMiddleware):
            validate_middleware_resources((item._summarization,))
        if isinstance(item, SubAgentMiddleware):
            # 0.7.5's task tool uses the compiled specs, not the saved _backend.
            # Validate those specs with the same inheritance rule as build(subagents=).
            validate_subagent_resources(item._subagents)


def prepare_middleware_resources(
    middleware: Sequence[_Middleware],
) -> tuple[_Middleware, ...]:
    """Derive built-in declarations without changing caller-owned configuration.

    Memory, Skills, and automatic summarization hooks read their instance backend.
    Filesystem and manual summarization tools capture their original middleware in
    closures, so their constructors must bind new tools to the derived instance.
    Exact type checks preserve application subclasses and their async responsibility.
    See test_middleware_resources for public-path isolation and configuration checks.
    """

    validate_middleware_resources(middleware)
    derived: dict[int, _Middleware] = {}

    def prepare(item: _Middleware) -> _Middleware:
        existing = derived.get(id(item))
        if existing is not None:
            return existing
        if type(item) is SummarizationToolMiddleware:
            # The nested automatic middleware may also occur in the public stack.
            summary = prepare(item._summarization)
            assert isinstance(summary, SummarizationMiddleware)
            result: _Middleware = ObservedCompactionTool(
                summary, system_prompt=item.system_prompt
            )
        elif type(item) is FilesystemMiddleware:
            backend = async_store_backend(item.backend)
            if backend is item.backend:
                return item
            # The upstream constructor types tools as FsToolName but stores the
            # same names as frozenset[str]. Retain that validated selection.
            tools = (
                None
                if item._enabled_tools is None
                else cast(list[FsToolName], sorted(item._enabled_tools))
            )
            result = FilesystemMiddleware(
                backend=backend,
                system_prompt=item._custom_system_prompt,
                custom_tool_descriptions=dict(item._custom_tool_descriptions),
                tool_token_limit_before_evict=item._tool_token_limit_before_evict,
                human_message_token_limit_before_evict=item._human_message_token_limit_before_evict,
                max_execute_timeout=item._max_execute_timeout,
                grep_max_count=item._grep_max_count,
                tools=tools,
                _permissions=list(item._permissions),
            )
        elif type(item) is MemoryMiddleware:
            result = copy(item)
            result._backend = async_store_backend(item._backend)
            result.sources = list(item.sources)
        elif type(item) is SkillsMiddleware:
            result = copy(item)
            result._backend = async_store_backend(item._backend)
            result.sources = list(item.sources)
            result.source_labels = list(item.source_labels)
        elif type(item) is SummarizationMiddleware:
            result = observe_summarization(item)
            result._backend = async_store_backend(item._backend)
        else:
            result = item
        derived[id(item)] = result
        return result

    return tuple(prepare(item) for item in middleware)

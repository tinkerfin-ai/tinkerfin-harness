from __future__ import annotations

import inspect
from collections.abc import AsyncIterator, Callable

from langgraph.graph.state import CompiledStateGraph


class _SourceGraph:
    def __init__(self, source_factory: Callable[[], AsyncIterator[object]]) -> None:
        self._source_factory = source_factory

    def astream(
        self,
        *_args: object,
        **_options: object,
    ) -> AsyncIterator[object]:
        return self._source_factory()


setattr(
    _SourceGraph.astream,
    "__signature__",
    inspect.signature(CompiledStateGraph.astream),
)

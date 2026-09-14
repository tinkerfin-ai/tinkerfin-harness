"""AG-UI conversation access with explicit borrowed history sources."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ._optional_dependencies import require_agui, require_tracing

if TYPE_CHECKING:
    from tinkerfin_tracing import Tracer

    from ._agui_history import AgUiGraphQuery as AgUiGraphQuery
    from ._agui_history import AgUiHistory as AgUiHistory
    from ._agui_history import AgUiHistoryView as AgUiHistoryView
    from ._agui_history import AgUiLiveView as AgUiLiveView
    from ._agui_history import AgUiReplayChannel as AgUiReplayChannel
    from ._agui_history_models import (
        AgUiMessageReference as AgUiMessageReference,
    )
    from ._agui_history_models import (
        AgUiSubagentReference as AgUiSubagentReference,
    )
    from ._agui_history_models import (
        AgUiToolMessageReference as AgUiToolMessageReference,
    )
    from ._agui_history_models import (
        AgUiToolReference as AgUiToolReference,
    )
    from ._agui_history_models import (
        AgUiTraceGraph as AgUiTraceGraph,
    )
    from ._agui_history_models import (
        AgUiTraceGraphDelta as AgUiTraceGraphDelta,
    )
    from ._agui_history_models import (
        AgUiTraceGraphNode as AgUiTraceGraphNode,
    )
    from ._agui_history_models import (
        AgUiTraceGraphPage as AgUiTraceGraphPage,
    )
    from ._agui_history_models import (
        AgUiTraceHistory as AgUiTraceHistory,
    )
    from ._agui_history_models import (
        AgUiTraceInteraction as AgUiTraceInteraction,
    )
    from ._agui_history_models import (
        AgUiTraceMessage as AgUiTraceMessage,
    )
    from ._agui_history_models import (
        AgUiTraceSummary as AgUiTraceSummary,
    )
    from ._agui_history_models import (
        AgUiTraceUpdate as AgUiTraceUpdate,
    )


class RuntimeAgUi:
    """Access conversation history in one Runtime's immutable namespace.

    Obtain this access point from ``runtime.agui``. History sources are borrowed
    explicitly; registering a run observer does not select a history store.
    """

    _namespace: str

    def __init__(self) -> None:
        """Require access through ``runtime.agui`` instead of direct construction."""
        raise TypeError("RuntimeAgUi is obtained from runtime.agui")

    @classmethod
    def _create(cls, namespace: str) -> RuntimeAgUi:
        result = cls.__new__(cls)
        result._namespace = namespace
        return result

    def history(self, tracer: Tracer) -> AgUiHistory:
        """Read from a borrowed history source in this Runtime's namespace.

        Args:
            tracer: Explicit source of recorded runs; it need not be a write observer.

        Returns:
            A history reader that never closes the Tracer or its Store.

        Raises:
            ModuleNotFoundError: The AG-UI or tracing integration is not installed.
            TypeError: The history source is not a Tracer.
        """
        require_agui()
        require_tracing()
        from ._agui_history import AgUiHistory

        return AgUiHistory(tracer, namespace=self._namespace)


_HISTORY_TYPES = frozenset(
    [
        "AgUiMessageReference",
        "AgUiToolMessageReference",
        "AgUiToolReference",
        "AgUiSubagentReference",
        "AgUiTraceMessage",
        "AgUiTraceGraphNode",
        "AgUiTraceInteraction",
        "AgUiTraceGraph",
        "AgUiTraceGraphPage",
        "AgUiTraceGraphDelta",
        "AgUiTraceSummary",
        "AgUiTraceUpdate",
        "AgUiTraceHistory",
    ]
)
_HISTORY_READERS = frozenset(
    {
        "AgUiHistory",
        "AgUiHistoryView",
        "AgUiGraphQuery",
        "AgUiLiveView",
        "AgUiReplayChannel",
    }
)

if not TYPE_CHECKING:

    def __getattr__(name: str) -> object:
        if name not in _HISTORY_TYPES and name not in _HISTORY_READERS:
            raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
        require_agui()
        require_tracing()
        if name in _HISTORY_READERS:
            from . import _agui_history as module
        else:
            from . import _agui_history_models as module
        value = getattr(module, name)
        globals()[name] = value
        return value


__all__ = [
    "AgUiGraphQuery",
    "AgUiHistory",
    "AgUiHistoryView",
    "AgUiLiveView",
    "AgUiMessageReference",
    "AgUiReplayChannel",
    "AgUiSubagentReference",
    "AgUiToolMessageReference",
    "AgUiToolReference",
    "AgUiTraceGraph",
    "AgUiTraceGraphDelta",
    "AgUiTraceGraphNode",
    "AgUiTraceGraphPage",
    "AgUiTraceHistory",
    "AgUiTraceInteraction",
    "AgUiTraceMessage",
    "AgUiTraceSummary",
    "AgUiTraceUpdate",
    "RuntimeAgUi",
]

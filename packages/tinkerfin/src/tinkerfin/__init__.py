"""Request-scoped Deep Agents runtime with native, AG-UI, and SSE streams."""

from typing import TYPE_CHECKING

from tinkerfin_contracts import ContextKind as ContextKind
from tinkerfin_contracts import RunIdentity as RunIdentity

from ._call_observation import TraceContribution as TraceContribution
from ._call_observation import trace_contribution as trace_contribution
from ._lazy_run import AgUiRunStream as AgUiRunStream
from ._lazy_run import NativeRunStream as NativeRunStream
from ._optional_dependencies import require_agui
from .errors import AgUiResumeBindingError as AgUiResumeBindingError
from .errors import RunObservationError as RunObservationError
from .errors import TinkerFinError as TinkerFinError
from .errors import TinkerFinErrorCode as TinkerFinErrorCode
from .errors import TinkerFinLifecycleError as TinkerFinLifecycleError
from .errors import TinkerFinStreamProtocolError as TinkerFinStreamProtocolError
from .media import AttachmentContent as AttachmentContent
from .media import AttachmentSupport as AttachmentSupport
from .plan import AgentMode as AgentMode
from .runtime import AgentRuntime as AgentRuntime
from .runtime import AgUiSettlementTimeoutError as AgUiSettlementTimeoutError
from .runtime import EventObserver as EventObserver
from .runtime import NativeStreamPart as NativeStreamPart
from .runtime import PartObserver as PartObserver
from .runtime import SseBody as SseBody
from .runtime import SseEventIdResolver as SseEventIdResolver
from .runtime import SseMapper as SseMapper
from .runtime import SsePayload as SsePayload
from .runtime import SsePreflight as SsePreflight
from .runtime import TinkerFin as TinkerFin

if TYPE_CHECKING:
    from .agui_input import AgUiUserInput as AgUiUserInput
    from .agui_resume import AgUiResumeBinding as AgUiResumeBinding
    from .agui_resume import AgUiResumeCheckpoint as AgUiResumeCheckpoint
    from .agui_resume import (
        AgUiResumeCheckpointObserver as AgUiResumeCheckpointObserver,
    )
    from .agui_resume import (
        AgUiResumeNotSavedObserver as AgUiResumeNotSavedObserver,
    )
    from .agui_resume import AgUiResumeRequest as AgUiResumeRequest

_AGUI_RESUME_EXPORTS = frozenset(
    {
        "AgUiResumeBinding",
        "AgUiResumeCheckpoint",
        "AgUiResumeCheckpointObserver",
        "AgUiResumeNotSavedObserver",
        "AgUiResumeRequest",
    }
)


# Static declarations enumerate the lazy exports; dynamic lookup is runtime-only
# so type checkers continue to reject names outside the public API.
if not TYPE_CHECKING:

    def __getattr__(name: str) -> object:
        """Load AG-UI input and resume contracts only when their optional extra is present."""

        if name == "AgUiUserInput":
            require_agui()
            from .agui_input import AgUiUserInput

            globals()[name] = AgUiUserInput
            return AgUiUserInput
        if name not in _AGUI_RESUME_EXPORTS:
            raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
        require_agui()
        from . import agui_resume

        value = getattr(agui_resume, name)
        globals()[name] = value
        return value


__all__ = [
    "AgUiResumeBinding",
    "AgUiResumeBindingError",
    "AgUiResumeCheckpoint",
    "AgUiResumeCheckpointObserver",
    "AgUiResumeNotSavedObserver",
    "AgUiResumeRequest",
    "AgUiRunStream",
    "AgUiSettlementTimeoutError",
    "AgUiUserInput",
    "AgentMode",
    "AgentRuntime",
    "AttachmentContent",
    "AttachmentSupport",
    "ContextKind",
    "EventObserver",
    "NativeRunStream",
    "NativeStreamPart",
    "PartObserver",
    "RunIdentity",
    "RunObservationError",
    "SseBody",
    "SseEventIdResolver",
    "SseMapper",
    "SsePayload",
    "SsePreflight",
    "TinkerFin",
    "TinkerFinError",
    "TinkerFinErrorCode",
    "TinkerFinLifecycleError",
    "TinkerFinStreamProtocolError",
    "TraceContribution",
    "trace_contribution",
]

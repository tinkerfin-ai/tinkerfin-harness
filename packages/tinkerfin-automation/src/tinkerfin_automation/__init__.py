"""Schedule and execute bounded TinkerFin tasks without owning business policy."""

from typing import TYPE_CHECKING

from .agent_tools import create_automation_tools as create_automation_tools
from .engine import AutomationEngine as AutomationEngine
from .engine import OnInterrupt as OnInterrupt
from .errors import (
    AutomationError as AutomationError,
)
from .errors import (
    AutomationErrorCode as AutomationErrorCode,
)
from .errors import (
    AutomationLifecycleError as AutomationLifecycleError,
)
from .errors import (
    AutomationSchedulerError as AutomationSchedulerError,
)
from .errors import (
    AutomationStoreError as AutomationStoreError,
)
from .errors import (
    AutomationStoreProtocolError as AutomationStoreProtocolError,
)
from .errors import (
    AutomationStoreTimeout as AutomationStoreTimeout,
)
from .errors import (
    ClaimLostError as ClaimLostError,
)
from .errors import (
    ExecutionBusyError as ExecutionBusyError,
)
from .errors import (
    ExecutionNotFoundError as ExecutionNotFoundError,
)
from .errors import (
    InterruptCallbackError as InterruptCallbackError,
)
from .errors import (
    InvalidScheduleError as InvalidScheduleError,
)
from .errors import (
    QueueFullError as QueueFullError,
)
from .errors import (
    RequestConflictError as RequestConflictError,
)
from .errors import (
    ResolutionNotAllowedError as ResolutionNotAllowedError,
)
from .errors import (
    RetryNotAllowedError as RetryNotAllowedError,
)
from .errors import (
    StartAlreadyAuthorizedError as StartAlreadyAuthorizedError,
)
from .errors import (
    TargetExecutionError as TargetExecutionError,
)
from .errors import (
    TargetNotFoundError as TargetNotFoundError,
)
from .errors import (
    TaskConflictError as TaskConflictError,
)
from .errors import (
    TaskNotFoundError as TaskNotFoundError,
)
from .memory import MemoryAutomationStore as MemoryAutomationStore
from .memory import MemoryStoreLimits as MemoryStoreLimits
from .models import (
    AttentionResolution as AttentionResolution,
)
from .models import (
    AutomationExecution as AutomationExecution,
)
from .models import (
    AutomationTask as AutomationTask,
)
from .models import (
    ExecutionFailure as ExecutionFailure,
)
from .models import (
    ExecutionOrigin as ExecutionOrigin,
)
from .models import (
    ExecutionPage as ExecutionPage,
)
from .models import (
    ExecutionStatus as ExecutionStatus,
)
from .models import (
    InterruptedExecution as InterruptedExecution,
)
from .models import (
    JsonObject as JsonObject,
)
from .models import (
    TaskPage as TaskPage,
)
from .models import (
    TaskStatus as TaskStatus,
)
from .policies import ExecutionLimits as ExecutionLimits
from .policies import MisfireMode as MisfireMode
from .policies import MisfirePolicy as MisfirePolicy
from .queries import ExecutionFilter as ExecutionFilter
from .queries import TaskFilter as TaskFilter
from .runtime_target import TinkerFinTarget as TinkerFinTarget
from .schedules import CronSchedule as CronSchedule
from .schedules import IntervalSchedule as IntervalSchedule
from .schedules import OnceSchedule as OnceSchedule
from .schedules import Schedule as Schedule
from .schedules import next_run_after as next_run_after
from .schedules import preview_schedule as preview_schedule
from .service import AutomationService as AutomationService
from .targets import AutomationTarget as AutomationTarget
from .targets import ExecutionFailed as ExecutionFailed
from .targets import ExecutionInterrupted as ExecutionInterrupted
from .targets import ExecutionOutcome as ExecutionOutcome
from .targets import ExecutionRequest as ExecutionRequest
from .targets import ExecutionSucceeded as ExecutionSucceeded
from .targets import ExecutionUncertain as ExecutionUncertain
from .targets import FunctionTarget as FunctionTarget

if TYPE_CHECKING:
    from .sqlalchemy import SqlAlchemyAutomationStore as SqlAlchemyAutomationStore

__all__ = [
    "AttentionResolution",
    "AutomationEngine",
    "AutomationError",
    "AutomationErrorCode",
    "AutomationExecution",
    "AutomationLifecycleError",
    "AutomationSchedulerError",
    "AutomationService",
    "AutomationStoreError",
    "AutomationStoreProtocolError",
    "AutomationStoreTimeout",
    "AutomationTarget",
    "AutomationTask",
    "ClaimLostError",
    "CronSchedule",
    "ExecutionBusyError",
    "ExecutionFailed",
    "ExecutionFailure",
    "ExecutionFilter",
    "ExecutionInterrupted",
    "ExecutionLimits",
    "ExecutionNotFoundError",
    "ExecutionOrigin",
    "ExecutionOutcome",
    "ExecutionPage",
    "ExecutionRequest",
    "ExecutionStatus",
    "ExecutionSucceeded",
    "ExecutionUncertain",
    "FunctionTarget",
    "InterruptCallbackError",
    "InterruptedExecution",
    "IntervalSchedule",
    "InvalidScheduleError",
    "JsonObject",
    "MemoryAutomationStore",
    "MemoryStoreLimits",
    "MisfireMode",
    "MisfirePolicy",
    "OnInterrupt",
    "OnceSchedule",
    "QueueFullError",
    "RequestConflictError",
    "ResolutionNotAllowedError",
    "RetryNotAllowedError",
    "Schedule",
    "SqlAlchemyAutomationStore",
    "StartAlreadyAuthorizedError",
    "TargetExecutionError",
    "TargetNotFoundError",
    "TaskConflictError",
    "TaskFilter",
    "TaskNotFoundError",
    "TaskPage",
    "TaskStatus",
    "TinkerFinTarget",
    "create_automation_tools",
    "next_run_after",
    "preview_schedule",
]


def __getattr__(name: str) -> object:
    """Load optional integrations only when their public symbol is requested."""

    if name == "SqlAlchemyAutomationStore":
        try:
            from .sqlalchemy import SqlAlchemyAutomationStore
        except ModuleNotFoundError as error:
            if error.name not in {"sqlalchemy", "tinkerfin_sqlalchemy"} and not str(
                error.name
            ).startswith("sqlalchemy."):
                raise
            raise ImportError(
                f"{name} requires a SQL extra; install "
                '"tinkerfin-automation[sqlalchemy]"'
            ) from error
        globals()[name] = SqlAlchemyAutomationStore
        return SqlAlchemyAutomationStore
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

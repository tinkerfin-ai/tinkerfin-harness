"""Read retained evidence without dispatching or repeating a remote operation."""

from dataclasses import dataclass
from typing import Literal, TypeAlias

from tinkerfin_contracts import ToolResult

OperationState: TypeAlias = Literal[
    "pending", "started", "completed", "failed", "cancelled", "uncertain"
]


@dataclass(frozen=True, slots=True)
class OperationStatus:
    """Describe the latest retained evidence within one authenticated endpoint.

    ``pending`` means delivery was recorded, not that an effect did not occur.
    Cancellation intent does not confirm termination. ``result`` contains a
    retained inline result when available; transferred results are not downloaded
    by this query. A missing or expired result never authorizes another execution.

    Args:
        operation_id: Stable identifier supplied by the original operation.
        state: Latest recorded operation lifecycle state.
        cancellation_requested: Whether durable stop intent has been saved.
        result_available: Whether result replay data is retained; pending states
            may report true before a final result exists.
        result: Retained inline terminal result, if present.
        message: Retained terminal explanation safe for the authenticated caller.
    """

    operation_id: str
    state: OperationState
    cancellation_requested: bool
    result_available: bool
    result: ToolResult | None = None
    message: str | None = None

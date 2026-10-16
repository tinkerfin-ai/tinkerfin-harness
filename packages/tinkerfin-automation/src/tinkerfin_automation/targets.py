"""Trusted host execution targets and normalized Automation outcomes."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, TypeAlias, TypeGuard

from pydantic import JsonValue, TypeAdapter, ValidationError

from tinkerfin_native_stream import NativeRuntimeInterrupt

from .errors import TargetExecutionError
from .models import AutomationExecution, ExecutionFailure, JsonObject

_JSON_VALUE: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)


@dataclass(frozen=True, slots=True)
class ExecutionRequest:
    """Immutable execution snapshot supplied to one trusted target."""

    execution: AutomationExecution
    deadline: datetime

    @property
    def input(self) -> JsonObject:
        """Return the saved execution input without another source or lookup."""
        return self.execution.input


@dataclass(frozen=True, slots=True)
class ExecutionSucceeded:
    """A target completed and returned a JSON result."""

    result: JsonValue | None = None


@dataclass(frozen=True, slots=True)
class ExecutionInterrupted:
    """A graph returned unfinished interrupt identities without a decision."""

    interrupt_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        """Require at least one canonical interrupt identity."""

        if not self.interrupt_ids:
            raise ValueError("interrupt_ids must not be empty")
        if any(not value or value != value.strip() for value in self.interrupt_ids):
            raise ValueError("interrupt_ids must contain canonical values")


@dataclass(frozen=True, slots=True)
class ExecutionFailed:
    """A target returned an explicit safe failure."""

    failure: ExecutionFailure


@dataclass(frozen=True, slots=True)
class ExecutionUncertain:
    """A target cannot prove whether external execution has stopped."""

    failure: ExecutionFailure


ExecutionOutcome: TypeAlias = (
    ExecutionSucceeded | ExecutionInterrupted | ExecutionFailed | ExecutionUncertain
)


class AutomationTarget(Protocol):
    """Execute one immutable Automation request supplied by the trusted host."""

    @property
    def cancellation_is_final(self) -> bool:
        """Whether settled coroutine cancellation proves external work stopped."""

        ...

    async def run(self, request: ExecutionRequest) -> ExecutionOutcome:
        """Run one authorized execution and return a normalized outcome."""

        ...


TargetCallable: TypeAlias = Callable[[ExecutionRequest], Awaitable[object]]


class FunctionTarget:
    """Adapt one trusted asynchronous callable to the target contract."""

    def __init__(
        self,
        execute: TargetCallable,
        *,
        cancellation_is_final: bool = False,
    ) -> None:
        """Bind a callable and its explicit cancellation guarantee."""

        if not callable(execute):
            raise TypeError("execute must be callable")
        self._execute = execute
        self._cancellation_is_final = cancellation_is_final

    @property
    def cancellation_is_final(self) -> bool:
        """Return the host-declared cancellation guarantee."""

        return self._cancellation_is_final

    async def run(self, request: ExecutionRequest) -> ExecutionOutcome:
        """Execute and normalize the trusted callable result."""

        result = self._execute(request)
        if not inspect.isawaitable(result):
            raise TargetExecutionError("Target callable must return an awaitable")
        return normalize_target_result(await result)


def _is_mapping(value: object) -> TypeGuard[Mapping[object, object]]:
    return isinstance(value, Mapping)


def _is_sequence(value: object) -> TypeGuard[Sequence[object]]:
    return isinstance(value, Sequence) and not isinstance(
        value, str | bytes | bytearray
    )


def normalize_target_result(value: object) -> ExecutionOutcome:
    """Normalize a target result and detect TinkerFin's interrupt state shape.

    Args:
        value: Target output. Runtime interrupt results contain ``__interrupt__``
            records, either NativeRuntimeInterrupt values or serialized mappings.

    Returns:
        A validated execution result or the pending interrupt identities.

    Raises:
        TargetExecutionError: Interrupt records or result JSON are invalid.
    """

    if isinstance(
        value,
        (ExecutionSucceeded, ExecutionInterrupted, ExecutionFailed, ExecutionUncertain),
    ):
        return value
    if isinstance(value, ExecutionFailure):
        return ExecutionFailed(value)
    if _is_mapping(value) and "__interrupt__" in value:
        raw_interrupts = value["__interrupt__"]
        if not _is_sequence(raw_interrupts):
            raise TargetExecutionError("Runtime interrupt state has an invalid shape")
        interrupt_ids: list[str] = []
        for item in raw_interrupts:
            if isinstance(item, NativeRuntimeInterrupt):
                interrupt_id = item.id
            elif _is_mapping(item):
                candidate = item.get("id")
                if not isinstance(candidate, str):
                    raise TargetExecutionError(
                        "Runtime interrupt record has no string identity"
                    )
                interrupt_id = candidate
            else:
                raise TargetExecutionError(
                    "Runtime interrupt record has an invalid type"
                )
            if interrupt_id not in interrupt_ids:
                interrupt_ids.append(interrupt_id)
        if interrupt_ids:
            return ExecutionInterrupted(tuple(interrupt_ids))
    try:
        result = _JSON_VALUE.validate_python(value)
    except ValidationError as error:
        raise TargetExecutionError(
            "Target result is not valid JSON",
            cause=error,
        ) from error
    return ExecutionSucceeded(result)


__all__ = [
    "AutomationTarget",
    "ExecutionFailed",
    "ExecutionInterrupted",
    "ExecutionOutcome",
    "ExecutionRequest",
    "ExecutionSucceeded",
    "ExecutionUncertain",
    "FunctionTarget",
    "TargetCallable",
    "normalize_target_result",
]

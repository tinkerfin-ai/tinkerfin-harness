"""Resume standard delegated retries without replaying completed attempts.

LangGraph 1.2.11 restores successful functional-task returns but deliberately
re-executes ERROR writes. Each attempt therefore returns a private record reference
after committing its result and retry decision. An interrupted attempt retains its
native task and child checkpoint; earlier failures never run their handlers again.
"""

from __future__ import annotations

import base64
from asyncio import sleep
from collections.abc import Awaitable, Callable, Sequence
from contextvars import ContextVar
from dataclasses import dataclass
from dis import get_instructions
from functools import partial
from time import time
from types import CodeType, FunctionType
from typing import Any, Literal, cast

from langchain.agents.middleware import AgentMiddleware, ToolRetryMiddleware
from langchain.agents.middleware._retry import (
    OnFailure,
    calculate_delay,
    should_retry_exception,
)
from langchain.agents.middleware.types import ToolCallRequest
from langchain.tools import ToolRuntime as NativeToolRuntime
from langchain_core.messages import ToolMessage
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.errors import GraphBubbleUp
from langgraph.func import task
from langgraph.runtime import get_runtime
from langgraph.types import Command
from pydantic import BaseModel, ConfigDict, Field

from ._agent_spec import AgentMiddlewareType
from ._agui_lineage_state import LINEAGE_CONFIG_KEY, LineageMarker
from ._delegation_journal import (
    DelegationJournal,
    DelegationRecord,
    content_digest,
    json_payload,
)
from .errors import DelegationFailedError, DelegationReplayError

_ToolResult = ToolMessage | Command[Any]
_ExecuteTool = Callable[[ToolCallRequest], Awaitable[_ToolResult]]
_ATTEMPT_NODE = "delegation_attempt"
_CURRENT_ATTEMPT: ContextVar[DelegationAttempt | None] = ContextVar(
    "tinkerfin_delegation_attempt", default=None
)
_CURRENT_DISPATCH: ContextVar[DelegationDispatch | None] = ContextVar(
    "tinkerfin_delegation_dispatch", default=None
)


def _constant_identity(value: object) -> object:
    if isinstance(value, CodeType):
        return _code_identity(value)
    if type(value) is tuple:
        return {
            "tuple": [
                _constant_identity(item) for item in cast(tuple[object, ...], value)
            ]
        }
    if type(value) is frozenset:
        return {
            "frozenset": sorted(
                content_digest(_constant_identity(item))
                for item in cast(frozenset[object], value)
            )
        }
    if value is None or type(value) in (str, bool, int):
        return value
    if type(value) is bytes:
        return {"bytes": value.hex()}
    if type(value) in (float, complex):
        return {type(value).__name__: repr(value)}
    if value is Ellipsis:
        return {"ellipsis": True}
    if isinstance(value, type) and issubclass(value, BaseException):
        return {"exception": f"{value.__module__}.{value.__qualname__}"}
    raise TypeError(
        "persistent delegation retry callback arguments and defaults must be "
        "immutable literal values"
    )


def _code_identity(code: CodeType) -> object:
    return {
        "instructions": code.co_code.hex(),
        "constants": [_constant_identity(value) for value in code.co_consts],
        "names": code.co_names,
        "freevars": code.co_freevars,
        "arguments": [
            code.co_argcount,
            code.co_posonlyargcount,
            code.co_kwonlyargcount,
        ],
        "exception_table": code.co_exceptiontable.hex(),
    }


def _written_captures(code: CodeType, names: set[str]) -> set[str]:
    shared = names.intersection(code.co_freevars)
    written: set[str] = set()
    for instruction in get_instructions(code):
        name: object = instruction.argval
        if (
            instruction.opname in {"STORE_DEREF", "DELETE_DEREF"}
            and isinstance(name, str)
            and name in shared
        ):
            written.add(name)
    for value in code.co_consts:
        if isinstance(value, CodeType):
            written.update(_written_captures(value, shared))
    return written


def _closure_identity(function: FunctionType) -> dict[str, object]:
    # Lexical writes identify callback state, including counters updated inside
    # nested functions. Intersecting each scope's free variables excludes local
    # names that merely shadow a captured configuration value.
    written = _written_captures(function.__code__, set(function.__code__.co_freevars))
    values: dict[str, object] = {}
    for name, cell in zip(
        function.__code__.co_freevars, function.__closure__ or (), strict=True
    ):
        if name in written:
            continue
        try:
            value: object = cell.cell_contents
        except ValueError:
            values[name] = {"empty_cell": True}
            continue
        try:
            values[name] = _constant_identity(value)
        except TypeError:
            # Mutable captures include observation lists and borrowed services.
            # Their contents are not a declaration, so completed decisions remain
            # authoritative without comparing those objects' current state.
            continue
    return values


def _callable_identity(value: object) -> str:
    """Identify code, defaults, immutable captures and explicit partial bindings.

    Installation paths, line numbers and debug tables do not change a policy.
    Captured mutable state and external services are deliberately not fingerprinted:
    their past decisions are persisted, rather than evaluated again during replay.
    Cells assigned by the callback or its nested functions are runtime state too.
    Bound methods, callable objects and opaque arguments have no supported policy
    description and must be rejected before persistent delegation starts.

    Args:
        value: Standard failure action, exception-type tuple, or supported callback.

    Returns:
        A stable declaration identity independent of installation paths and state.

    Raises:
        TypeError: A callback or its explicit bindings cannot be described exactly.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, tuple):
        return content_digest(
            [_callable_identity(item) for item in cast(tuple[object, ...], value)]
        )
    if type(value) is partial:
        binding = cast(partial[object], value)
        return "functools.partial:" + content_digest(
            {
                "function": _callable_identity(binding.func),
                "arguments": _constant_identity(binding.args),
                "keywords": [
                    [key, _constant_identity(item)]
                    for key, item in binding.keywords.items()
                ],
            }
        )
    if isinstance(value, type) and issubclass(value, BaseException):
        return f"{value.__module__}.{value.__qualname__}"
    if not isinstance(value, FunctionType):
        raise TypeError(
            "persistent delegation retry callbacks must be Python functions "
            "or functools.partial of them"
        )
    implementation = content_digest(
        {
            "code": _code_identity(value.__code__),
            "closure": _closure_identity(value),
            "defaults": _constant_identity(value.__defaults__),
            "keyword_defaults": {
                key: _constant_identity(item)
                for key, item in (value.__kwdefaults__ or {}).items()
            },
        }
    )
    return f"{value.__module__}.{value.__qualname__}:{implementation}"


def _tool_filter(policy: ToolRetryMiddleware[Any, Any]) -> list[str] | None:
    # LangChain 1.3.18 ToolRetryMiddleware.__init__ keeps the normalized `tools`
    # argument only in _tool_filter. The public `tools` attribute means tools added
    # by a middleware and is always empty here. Keep this locked dependency access
    # in one boundary; no fallback or duplicate host configuration is permitted.
    selected: object = policy._tool_filter
    if selected is None:
        return None
    if not isinstance(selected, list) or any(
        not isinstance(item, str) for item in cast(list[object], selected)
    ):
        raise TypeError("ToolRetryMiddleware has an unsupported tool filter")
    return list(selected)


def _applies_to_delegation(policy: ToolRetryMiddleware[Any, Any]) -> bool:
    selected = _tool_filter(policy)
    return selected is None or "task" in selected


def _policy_snapshot(
    policy: ToolRetryMiddleware[Any, Any],
) -> ToolRetryMiddleware[Any, Any]:
    """Bind all scalar settings once at the actual tool-entry boundary."""
    selected = _tool_filter(policy)
    failure = policy.on_failure
    on_failure: OnFailure
    if callable(failure):
        on_failure = failure
    elif failure == "error":
        on_failure = "error"
    elif failure == "continue":
        on_failure = "continue"
    else:
        raise ValueError("ToolRetryMiddleware has an unsupported failure policy")
    return ToolRetryMiddleware(
        tools=None if selected is None else [name for name in selected],
        max_retries=policy.max_retries,
        retry_on=policy.retry_on,
        on_failure=on_failure,
        initial_delay=policy.initial_delay,
        max_delay=policy.max_delay,
        backoff_factor=policy.backoff_factor,
        jitter=policy.jitter,
    )


def _policy_description(policy: ToolRetryMiddleware[Any, Any]) -> dict[str, object]:
    return {
        "tools": _tool_filter(policy),
        "max_retries": policy.max_retries,
        "retry_on": _callable_identity(policy.retry_on),
        "on_failure": _callable_identity(policy.on_failure),
        "initial_delay": policy.initial_delay,
        "max_delay": policy.max_delay,
        "backoff_factor": policy.backoff_factor,
        "jitter": policy.jitter,
    }


def prepare_delegation_retry(
    middleware: Sequence[AgentMiddlewareType],
    checkpointer: BaseCheckpointSaver[Any] | None,
) -> list[AgentMiddlewareType]:
    """Bind supported persistent retries without changing ordinary tool policies."""
    result: list[AgentMiddlewareType] = []
    for item in middleware:
        if checkpointer is None or not isinstance(item, ToolRetryMiddleware):
            result.append(item)
            continue
        if type(item) is not ToolRetryMiddleware:
            validate_delegation_retry([item], persistent=True)
            result.append(item)
            continue
        result.append(_DurableDelegationRetry(item, checkpointer))
    if checkpointer is not None:
        selected = next(
            (item for item in middleware if isinstance(item, ToolRetryMiddleware)), None
        )
        result.insert(0, _DelegationPolicyGuard(checkpointer, selected))
    return result


def validate_delegation_retry(
    middleware: Sequence[AgentMiddlewareType], *, persistent: bool
) -> None:
    """Reject undescribed retry semantics before persistent runs are admitted."""
    if not persistent:
        return
    for item in middleware:
        if not (
            isinstance(item, ToolRetryMiddleware)
            and item.max_retries > 0
            and _applies_to_delegation(item)
        ):
            continue
        if type(item) is not ToolRetryMiddleware:
            raise ValueError(
                "persistent delegation retries require standard ToolRetryMiddleware; "
                "custom retry subclasses cannot guarantee checkpoint recovery"
            )
        _policy_description(item)


class _Outcome(BaseModel):
    """Persist decisions made against original exceptions, never their objects."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    decision: Literal["return", "retry", "raise"]
    result_type: str = ""
    result_data: str = Field(
        default="",
        description="Base64 bytes produced by the borrowed checkpoint serializer",
    )
    failure_type: str = ""
    failure_message: str = ""
    policy_failure_type: str = ""
    policy_failure_message: str = ""
    retry_at: float = Field(
        default=0.0,
        allow_inf_nan=False,
        description="Unix timestamp in seconds when the next attempt may begin",
    )


class _Started(BaseModel):
    """Retain the actual task scope even when cached replay omits task metadata."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    request_digest: str
    index: int = Field(
        ge=0,
        description="Zero-based attempt position within the original tool invocation",
    )
    graph_namespace: str
    task_id: str


@dataclass(frozen=True, slots=True)
class _Request:
    """Keep the original delegating Graph and requested role before tool edits."""

    graph_namespace: tuple[str, ...]
    graph_task_id: str
    agent_name: str
    description: str
    digest: str


class DelegationAttempt:
    """Own one effective tool input inside a checkpointed delegated attempt."""

    def __init__(self, journal: DelegationJournal, key: str) -> None:
        self.journal = journal
        self.key = key
        self._invoked = False

    def owns_request(self, request: ToolCallRequest) -> bool:
        native = cast(NativeToolRuntime[Any, Any], request.runtime)  # pyright: ignore[reportUnknownMemberType]
        info = native.execution_info
        return info is not None and info.task_id == self.journal.parent_task_id

    async def accept_request(self, request: ToolCallRequest) -> None:
        """Reject changed effective arguments before executing the delegated tool."""
        if self._invoked:
            error = DelegationReplayError(
                "custom middleware cannot repeat delegation inside one persistent attempt"
            )
            if dispatch := _CURRENT_DISPATCH.get():
                dispatch.conflict = error
            raise error
        self._invoked = True
        if (
            request.tool_call["id"] != self.journal.tool_call_id
            or request.tool_call["name"] != "task"
            or request.tool is None
            or request.tool.name != "task"
        ):
            raise DelegationReplayError(
                "Delegation middleware changed the request identity"
            )
        await self.journal.save(self.key, "effective", json_payload(request.tool_call))


def current_delegation_attempt() -> DelegationAttempt | None:
    """Return the current task-owned binding for the final tool invocation boundary."""
    return _CURRENT_ATTEMPT.get()


class DelegationDispatch:
    """Own one tool entry, its stable policy and direct-execution limit."""

    def __init__(
        self, journal: DelegationJournal, policy: ToolRetryMiddleware[Any, Any] | None
    ) -> None:
        self.journal = journal
        self.policy = policy
        self.conflict: DelegationReplayError | None = None
        self._invoked = False

    def owns_request(self, request: ToolCallRequest) -> bool:
        native = cast(NativeToolRuntime[Any, Any], request.runtime)  # pyright: ignore[reportUnknownMemberType]
        return (
            native.execution_info is not None
            and native.execution_info.task_id == self.journal.parent_task_id
        )

    def accept_direct(self, request: ToolCallRequest) -> None:
        if (
            self._invoked
            or request.tool_call["id"] != self.journal.tool_call_id
            or request.tool_call["name"] != "task"
        ):
            self.conflict = DelegationReplayError(
                "custom middleware cannot repeat persistent delegation outside standard ToolRetryMiddleware"
            )
            raise self.conflict
        self._invoked = True


def current_delegation_dispatch() -> DelegationDispatch | None:
    """Return the outer managed tool-entry owner without introducing graph state."""
    return _CURRENT_DISPATCH.get()


def _request_journal(
    request: ToolCallRequest, saver: BaseCheckpointSaver[Any]
) -> DelegationJournal:
    # ToolCallRequest leaves the third-party runtime state generic unbound.
    native = cast(NativeToolRuntime[Any, Any], request.runtime)  # pyright: ignore[reportUnknownMemberType]
    info = native.execution_info
    if info is None:
        raise DelegationReplayError("Delegation requires native execution identity")
    components = tuple(info.checkpoint_ns.split("|"))
    if not components or components[-1] != f"tools:{info.task_id}":
        raise DelegationReplayError("Delegation has an invalid parent task scope")
    tool_call_id = request.tool_call["id"]
    if not isinstance(tool_call_id, str) or not tool_call_id:
        raise DelegationReplayError("Delegation requires a stable tool call ID")
    return DelegationJournal(
        saver,
        native.config,
        graph_namespace="|".join(components[:-1]),
        checkpoint_id=info.checkpoint_id,
        parent_task_id=info.task_id,
        tool_call_id=tool_call_id,
    )


class _DelegationPolicyGuard(AgentMiddleware):
    """Reject policy removal before ordinary error handlers can consume the error.

    This middleware creates no graph node or child scope. A zero-retry policy
    remains ordinary execution until the exact parent checkpoint proves it owns an
    unfinished persistent delegation under another policy.
    """

    def __init__(
        self,
        saver: BaseCheckpointSaver[Any],
        policy: ToolRetryMiddleware[Any, Any] | None,
    ) -> None:
        self._saver = saver
        self._policy = policy

    async def awrap_tool_call(
        self, request: ToolCallRequest, handler: _ExecuteTool
    ) -> _ToolResult:
        if request.tool_call["name"] == "task":
            native = cast(NativeToolRuntime[Any, Any], request.runtime)  # pyright: ignore[reportUnknownMemberType]
            if isinstance(
                native.config.get("configurable", {}).get(LINEAGE_CONFIG_KEY),
                LineageMarker,
            ):
                journal = _request_journal(request, self._saver)
                if self._policy is not None:
                    validate_delegation_retry([self._policy], persistent=True)
                policy = (
                    _policy_snapshot(self._policy)
                    if type(self._policy) is ToolRetryMiddleware
                    else None
                )
                description = (
                    _policy_description(policy)
                    if policy is not None
                    and policy.max_retries > 0
                    and _applies_to_delegation(policy)
                    else None
                )
                previous = await journal.read(journal.request_key, "request")
                if previous is not None and (
                    description is None
                    or not isinstance(previous.payload, dict)
                    or content_digest(previous.payload.get("policy"))
                    != content_digest(description)
                ):
                    raise DelegationReplayError(
                        "Delegation retry policy changed while its attempt was pending"
                    )
                dispatch = DelegationDispatch(journal, policy)
                token = _CURRENT_DISPATCH.set(dispatch)
                try:
                    result = await handler(request)
                    if dispatch.conflict is not None:
                        raise dispatch.conflict
                    return result
                finally:
                    _CURRENT_DISPATCH.reset(token)
        return await handler(request)


class _DurableDelegationRetry(AgentMiddleware):
    """Adapt the standard policy only for the framework-owned task tool."""

    def __init__(
        self, policy: ToolRetryMiddleware[Any, Any], saver: BaseCheckpointSaver[Any]
    ) -> None:
        self._policy = policy
        self._saver = saver

    @property
    def name(self) -> str:
        return "ToolRetryMiddleware"

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], _ToolResult],
    ) -> _ToolResult:
        if request.tool_call["name"] == "task":
            raise NotImplementedError(
                "Persistent delegation requires asynchronous execution"
            )
        return self._policy.wrap_tool_call(request, handler)

    async def awrap_tool_call(
        self, request: ToolCallRequest, handler: _ExecuteTool
    ) -> _ToolResult:
        if request.tool_call["name"] != "task":
            return await self._policy.awrap_tool_call(request, handler)
        dispatch = _CURRENT_DISPATCH.get()
        if dispatch is None or dispatch.policy is None:
            raise DelegationReplayError(
                "Delegation retry has no managed policy binding"
            )
        policy = dispatch.policy
        if policy.max_retries == 0 or not _applies_to_delegation(policy):
            return await policy.awrap_tool_call(request, handler)
        try:
            return await self._execute(request, handler, policy)
        except DelegationReplayError as error:
            dispatch.conflict = error
            raise

    async def _execute(
        self,
        request: ToolCallRequest,
        handler: _ExecuteTool,
        policy: ToolRetryMiddleware[Any, Any],
    ) -> _ToolResult:
        journal, declaration = await self._request(request, policy)
        from ._call_observation import (
            register_delegation_invocation,
            register_delegation_retry,
        )

        register_delegation_retry(
            parent_namespace=declaration.graph_namespace,
            parent_graph_task_id=declaration.graph_task_id,
            parent_tool_call_id=journal.tool_call_id,
            node_name=_ATTEMPT_NODE,
            agent_name=declaration.agent_name,
            description=declaration.description,
        )
        for index in range(policy.max_retries + 1):
            key = journal.attempt_key(index)
            live_failure: list[Exception] = []
            started = await journal.read(key, "started")
            if started is not None:
                restored = self._register_started(started, declaration, journal, index)
                previous = await journal.read(key, "outcome")
                if previous is not None:
                    self._register_result(previous, declaration, restored.task_id)

            @task(name=_ATTEMPT_NODE)
            async def execute_attempt(
                attempt_index: int, request_digest: str
            ) -> dict[str, str]:
                if attempt_index != index or request_digest != declaration.digest:
                    raise DelegationReplayError(
                        "Delegation replay changed its invocation order"
                    )
                record = await self._attempt(
                    request,
                    handler,
                    journal,
                    declaration,
                    key,
                    index,
                    live_failure,
                    policy,
                )
                info = get_runtime().execution_info
                if info is None:
                    raise DelegationReplayError(
                        "Delegation result has no execution identity"
                    )
                return self._register_result(record, declaration, info.task_id)

            register_delegation_invocation(
                parent_namespace=declaration.graph_namespace,
                parent_graph_task_id=declaration.graph_task_id,
                node_name=_ATTEMPT_NODE,
                arguments=(index, declaration.digest),
            )
            reference = await execute_attempt(index, declaration.digest)
            record = await journal.read(key, "outcome")
            if record is None or reference != {
                "attempt_key": key,
                "record_digest": record.digest,
            }:
                raise DelegationReplayError(
                    "Delegation task returned an invalid outcome reference"
                )
            try:
                outcome = _Outcome.model_validate(record.payload)
            except ValueError as error:
                raise DelegationReplayError(
                    "Delegation outcome is invalid", cause=error
                ) from error
            if outcome.decision == "return":
                return self._result(outcome)
            if outcome.decision == "raise":
                if live_failure:
                    raise live_failure[0]
                raise DelegationFailedError(
                    "Delegated task failed",
                    diagnostic_context={
                        "attempt_key": key,
                        "failure_type": outcome.failure_type,
                        "failure_message": outcome.failure_message,
                        "policy_failure_type": outcome.policy_failure_type,
                        "policy_failure_message": outcome.policy_failure_message,
                    },
                )
            if index >= policy.max_retries:
                raise DelegationReplayError(
                    "Delegation journal exceeds the retry policy"
                )
            await self._backoff(journal, key, outcome)
        raise DelegationReplayError("Delegation retry has no terminal result")

    async def _request(
        self, request: ToolCallRequest, policy: ToolRetryMiddleware[Any, Any]
    ) -> tuple[DelegationJournal, _Request]:
        journal = _request_journal(request, self._saver)
        parent = (
            tuple(journal.graph_namespace.split("|")) if journal.graph_namespace else ()
        )
        args: dict[str, Any] = request.tool_call["args"]
        agent, description = args.get("subagent_type"), args.get("description")
        if not isinstance(agent, str) or not isinstance(description, str):
            raise DelegationReplayError(
                "Delegation requires an agent and task description"
            )
        payload = json_payload(
            {"tool_call": request.tool_call, "policy": _policy_description(policy)}
        )
        await journal.save(journal.request_key, "request", payload)
        return journal, _Request(
            parent, journal.parent_task_id, agent, description, content_digest(payload)
        )

    async def _attempt(
        self,
        request: ToolCallRequest,
        handler: _ExecuteTool,
        journal: DelegationJournal,
        declaration: _Request,
        key: str,
        index: int,
        live_failure: list[Exception],
        policy: ToolRetryMiddleware[Any, Any],
    ) -> DelegationRecord:
        info = get_runtime().execution_info
        if info is None:
            raise DelegationReplayError("Delegation attempt has no execution identity")
        started = await journal.save(
            key,
            "started",
            json_payload(
                {
                    "request_digest": declaration.digest,
                    "index": index,
                    "graph_namespace": info.checkpoint_ns,
                    "task_id": info.task_id,
                }
            ),
        )
        previous = await journal.read(key, "outcome")
        if previous is not None:
            return previous
        self._register_started(started, declaration, journal, index)
        token = _CURRENT_ATTEMPT.set(DelegationAttempt(journal, key))
        try:
            try:
                result = await handler(request)
            except (GraphBubbleUp, DelegationReplayError):
                raise
            except Exception as error:  # noqa: BLE001 - persist the declared retry decision
                try:
                    outcome = self._failure(error, request, index, policy)
                except (GraphBubbleUp, DelegationReplayError):
                    raise
                except Exception as policy_error:  # noqa: BLE001 - a failed policy must not rerun the completed attempt
                    outcome = _Outcome(
                        decision="raise",
                        failure_type=f"{type(error).__module__}.{type(error).__qualname__}",
                        failure_message=str(error),
                        policy_failure_type=(
                            f"{type(policy_error).__module__}."
                            f"{type(policy_error).__qualname__}"
                        ),
                        policy_failure_message=str(policy_error),
                    )
                    live_failure.append(policy_error)
                else:
                    if outcome.decision == "raise":
                        live_failure.append(error)
            else:
                dispatch = _CURRENT_DISPATCH.get()
                if dispatch is not None and dispatch.conflict is not None:
                    raise dispatch.conflict
                outcome = self._returned(result)
            return await journal.save(
                key, "outcome", json_payload(outcome.model_dump(mode="json"))
            )
        finally:
            _CURRENT_ATTEMPT.reset(token)

    @staticmethod
    def _register_started(
        record: DelegationRecord,
        declaration: _Request,
        journal: DelegationJournal,
        index: int,
    ) -> _Started:
        from ._call_observation import register_delegation_scope

        try:
            started = _Started.model_validate(record.payload)
        except ValueError as error:
            raise DelegationReplayError(
                "Delegation task provenance is invalid", cause=error
            ) from error
        if started.request_digest != declaration.digest or started.index != index:
            raise DelegationReplayError(
                "Delegation task provenance changed during replay"
            )
        # Registration restores evidence; it does not start or count execution.
        # LangGraph can emit cached task-start parts without namespace metadata.
        register_delegation_scope(
            namespace=tuple(started.graph_namespace.split("|")),
            parent_namespace=declaration.graph_namespace,
            parent_graph_task_id=declaration.graph_task_id,
            parent_tool_call_id=journal.tool_call_id,
            graph_task_id=started.task_id,
            node_name=_ATTEMPT_NODE,
            agent_name=declaration.agent_name,
            description=declaration.description,
        )
        return started

    @staticmethod
    def _register_result(
        record: DelegationRecord, declaration: _Request, task_id: str
    ) -> dict[str, str]:
        from ._call_observation import register_delegation_result

        reference = {"attempt_key": record.attempt_key, "record_digest": record.digest}
        register_delegation_result(
            parent_namespace=declaration.graph_namespace,
            graph_task_id=task_id,
            reference=reference,
        )
        return reference

    def _returned(self, result: _ToolResult) -> _Outcome:
        kind, data = self._saver.serde.dumps_typed(result)
        return _Outcome(
            decision="return",
            result_type=kind,
            result_data=base64.b64encode(data).decode("ascii"),
        )

    def _result(self, outcome: _Outcome) -> _ToolResult:
        try:
            value: object = self._saver.serde.loads_typed(
                (
                    outcome.result_type,
                    base64.b64decode(outcome.result_data, validate=True),
                )
            )
        except Exception as error:
            raise DelegationReplayError(
                "Delegation result cannot be restored", cause=error
            ) from error
        if not isinstance(value, ToolMessage | Command):
            raise DelegationReplayError("Delegation result is not a tool response")
        return cast(_ToolResult, value)

    def _failure(
        self,
        error: Exception,
        request: ToolCallRequest,
        index: int,
        policy: ToolRetryMiddleware[Any, Any],
    ) -> _Outcome:
        failure_type = f"{type(error).__module__}.{type(error).__qualname__}"
        failure_message = str(error)
        retryable = should_retry_exception(error, policy.retry_on)
        if retryable and index < policy.max_retries:
            delay = calculate_delay(
                index,
                initial_delay=policy.initial_delay,
                max_delay=policy.max_delay,
                backoff_factor=policy.backoff_factor,
                jitter=policy.jitter,
            )
            return _Outcome(
                decision="retry",
                failure_type=failure_type,
                failure_message=failure_message,
                retry_at=time() + delay,
            )
        if not retryable or policy.on_failure == "error":
            return _Outcome(
                decision="raise",
                failure_type=failure_type,
                failure_message=failure_message,
            )
        # An explicit formatter chooses its public content against the live error.
        # The default message must not copy provider diagnostics into Agent state.
        # Both paths preserve the standard error ToolMessage and invoke a custom
        # formatter once; a later replay only decodes the recorded response.
        content = (
            policy.on_failure(error)
            if callable(policy.on_failure)
            else "The delegated task could not be completed."
        )
        returned = self._returned(
            ToolMessage(
                content=content,
                name="task",
                tool_call_id=request.tool_call["id"] or "",
                status="error",
            )
        )
        return returned.model_copy(
            update={
                "failure_type": failure_type,
                "failure_message": failure_message,
            }
        )

    async def _backoff(
        self, journal: DelegationJournal, key: str, outcome: _Outcome
    ) -> None:
        if await journal.read(key, "backoff_done") is not None:
            return
        delay = max(0.0, outcome.retry_at - time())
        if delay:
            await sleep(delay)
        await journal.save(key, "backoff_done", {"retry_at": outcome.retry_at})

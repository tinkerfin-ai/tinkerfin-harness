"""Bind tool approvals and cancellation to their persisted tool calls."""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Mapping, Sequence
from pathlib import PurePosixPath
from typing import Any, Literal, TypeAlias, cast
from uuid import NAMESPACE_URL, uuid5

from deepagents.middleware.filesystem import FilesystemPermission
from langchain.agents.middleware import (
    AgentState,
    HumanInTheLoopMiddleware,
    InterruptOnConfig,
)
from langchain.agents.middleware.human_in_the_loop import Decision, HITLRequest
from langchain.agents.middleware.types import ToolCallRequest
from langchain_core.messages import AIMessage, ToolCall, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.config import get_config
from langgraph.errors import GraphInterrupt
from langgraph.runtime import Runtime
from langgraph.types import Interrupt, interrupt
from wcmatch import glob as wcglob

from ._agui_lineage_state import RUN_ID_METADATA_KEY
from ._hitl_state import (
    TOOL_REVIEW_CHANNEL,
    PendingToolReview,
    pending_tool_review,
    tool_review_digest,
    tool_review_owner,
)
from ._tasks import run_async_owned
from .errors import TinkerFinLifecycleError

CANCEL_DECISION_TYPE = "tinkerfin_cancel"
HITL_CONTRACT_ID = "tinkerfin.deepagents.hitl-cancel"

_GLOB_FLAGS = wcglob.BRACE | wcglob.GLOBSTAR
_GLOB_WILDCARD_CHARS = frozenset("*?[") | frozenset("{")
_CheckpointSaver: TypeAlias = (
    BaseCheckpointSaver[int] | BaseCheckpointSaver[float] | BaseCheckpointSaver[str]
)

_FilesystemOperation = Literal["read", "write"]
_ToolScope = Literal["exact", "bulk"]
_FS_TOOL_PATH_ARGS: dict[
    str,
    tuple[_FilesystemOperation, str, _ToolScope, str | None],
] = {
    "ls": ("read", "path", "bulk", None),
    "read_file": ("read", "file_path", "exact", None),
    "write_file": ("write", "file_path", "exact", None),
    "edit_file": ("write", "file_path", "exact", None),
    "delete": ("write", "file_path", "bulk", None),
    "glob": ("read", "path", "bulk", "pattern"),
    "grep": ("read", "path", "bulk", None),
}


def _normalize_path(path: str) -> str:
    """Normalize the virtual path shape used by Deep Agents 0.7.5 permissions."""

    posix = path.replace("\\", "/")
    parts = PurePosixPath(posix).parts
    if ".." in parts or path.startswith("~") or re.match(r"^[a-zA-Z]:", path):
        raise ValueError("path is outside the Deep Agents virtual path contract")
    normalized = os.path.normpath(path).replace("\\", "/")
    if not normalized.startswith("/"):
        normalized = f"/{normalized}"
    if ".." in normalized.split("/"):
        raise ValueError("normalized path contains traversal")
    return normalized


def _glob_anchor(pattern: str) -> str:
    parts = PurePosixPath(pattern.replace("\\", "/")).parts
    safe: list[str] = []
    for part in parts:
        if any(character in _GLOB_WILDCARD_CHARS for character in part):
            break
        safe.append(part)
    return "/" if not safe else str(PurePosixPath(*safe))


def _paths_overlap(first: str, second: str) -> bool:
    left = PurePosixPath(first)
    right = PurePosixPath(second)
    return left == right or left.is_relative_to(right) or right.is_relative_to(left)


def _permission_mode(
    rules: Sequence[FilesystemPermission],
    operation: _FilesystemOperation,
    path: str,
) -> Literal["allow", "deny", "interrupt"]:
    for rule in rules:
        if operation not in rule.operations:
            continue
        if any(
            wcglob.globmatch(path, pattern, flags=_GLOB_FLAGS) for pattern in rule.paths
        ):
            return rule.mode
    return "allow"


def _exact_when(
    rules: tuple[FilesystemPermission, ...],
    operation: _FilesystemOperation,
    path_argument: str,
) -> Callable[[ToolCallRequest], bool]:
    def when(request: ToolCallRequest) -> bool:
        raw_path = request.tool_call.get("args", {}).get(path_argument)
        if not isinstance(raw_path, str):
            return False
        try:
            normalized = _normalize_path(raw_path)
        except ValueError:
            return False
        return _permission_mode(rules, operation, normalized) == "interrupt"

    return when


def _bulk_pattern_fires(raw_pattern: str, anchors: tuple[str, ...]) -> bool:
    normalized = raw_pattern.replace("\\", "/")
    if normalized.startswith("/"):
        return any(_paths_overlap(_glob_anchor(raw_pattern), item) for item in anchors)
    return ".." in PurePosixPath(normalized).parts


def _bulk_when(
    rules: tuple[FilesystemPermission, ...],
    operation: _FilesystemOperation,
    path_argument: str,
    pattern_argument: str | None,
) -> Callable[[ToolCallRequest], bool]:
    anchors = tuple(
        _glob_anchor(pattern)
        for rule in rules
        if rule.mode == "interrupt" and operation in rule.operations
        for pattern in rule.paths
    )

    def when(request: ToolCallRequest) -> bool:
        if not anchors:
            return False
        arguments = request.tool_call.get("args", {})
        raw_path = arguments.get(path_argument)
        if not isinstance(raw_path, str):
            return raw_path is None
        try:
            normalized = _normalize_path(raw_path)
        except ValueError:
            return False
        if normalized == "/.":
            normalized = "/"
        if any(_paths_overlap(normalized, anchor) for anchor in anchors):
            return True
        if pattern_argument is None:
            return False
        raw_pattern = arguments.get(pattern_argument)
        return isinstance(raw_pattern, str) and _bulk_pattern_fires(
            raw_pattern,
            anchors,
        )

    return when


def _permission_interrupts(
    rules: tuple[FilesystemPermission, ...],
) -> dict[str, InterruptOnConfig]:
    """Reproduce the reviewed Deep Agents 0.7.5 public permission behavior."""

    allowed: list[Literal["approve", "edit", "reject", "respond"]] = [
        "approve",
        "edit",
        "reject",
        "respond",
    ]
    result: dict[str, InterruptOnConfig] = {}
    for tool_name, (
        operation,
        path_argument,
        scope,
        pattern_argument,
    ) in _FS_TOOL_PATH_ARGS.items():
        if not any(
            rule.mode == "interrupt" and operation in rule.operations for rule in rules
        ):
            continue
        when = (
            _exact_when(rules, operation, path_argument)
            if scope == "exact"
            else _bulk_when(
                rules,
                operation,
                path_argument,
                pattern_argument,
            )
        )
        result[tool_name] = InterruptOnConfig(
            allowed_decisions=allowed,
            when=when,
        )
    return result


async def _save_tool_review(
    saver: _CheckpointSaver, config: RunnableConfig, review: PendingToolReview
) -> None:
    """Finish the evidence write before publishing an interrupt or releasing resources.

    LangGraph 1.2.10 reapplies writes only for real task IDs. A distinct owner keeps
    this record out of both completed-task detection and null-task resume slots.
    The record is saver evidence; it does not add a Graph state channel.
    """

    try:
        await run_async_owned(
            lambda: saver.aput_writes(
                config,
                ((TOOL_REVIEW_CHANNEL, review.model_dump(mode="json")),),
                tool_review_owner(review.task_id),
            ),
            task_name="tinkerfin-tool-review-save",
        )
    except Exception as error:
        raise TinkerFinLifecycleError(
            "could not save pending tool review", cause=error
        ) from error


class ToolReviewMiddleware(HumanInTheLoopMiddleware):
    """Bind asynchronous approvals and cancellation to the exact persisted tool batch."""

    @property
    def name(self) -> str:
        """Identify the framework-owned tool review step."""

        return "ToolReview"

    def __init__(
        self,
        interrupt_on: dict[str, bool | InterruptOnConfig],
    ) -> None:
        HumanInTheLoopMiddleware.__init__(self, interrupt_on=interrupt_on)

    def after_model(
        self, state: AgentState, runtime: Runtime[Any]
    ) -> dict[str, object] | None:
        """Require async execution so pending approvals can be saved safely."""

        del state, runtime
        raise NotImplementedError("TinkerFin tool review requires async execution")

    async def aafter_model(
        self, state: AgentState, runtime: Runtime[Any]
    ) -> dict[str, object] | None:
        """Save each review and reject changed tool selection before consuming decisions.

        Request construction and decision processing follow LangChain 1.3.14's
        HumanInTheLoopMiddleware.after_model. The additional saver record preserves
        exact tool IDs across Runtime rebuilds; tests cover policy changes, nested
        interrupts, retries, and all native decision types.
        """

        configurable = get_config().get("configurable", {})
        message = next(
            (
                item
                for item in reversed(state["messages"])
                if isinstance(item, AIMessage)
            ),
            None,
        )
        tool_calls = [] if message is None else message.tool_calls
        request = HITLRequest(action_requests=[], review_configs=[])
        selected: dict[int, InterruptOnConfig] = {}
        for index, tool_call in enumerate(tool_calls):
            policy = self.interrupt_on.get(tool_call["name"])
            if policy is None or not self._should_interrupt(
                tool_call, policy, state, runtime
            ):
                continue
            action, review = self._create_action_and_config(
                tool_call, policy, state, runtime
            )
            request["action_requests"].append(action)
            request["review_configs"].append(review)
            selected[index] = policy
        reviewed_ids: list[str] = []
        for index in selected:
            tool_call_id = tool_calls[index].get("id")
            if not isinstance(tool_call_id, str) or not tool_call_id:
                raise ValueError("tool review requires stable tool call IDs")
            reviewed_ids.append(tool_call_id)
        digest = tool_review_digest(
            request,
            tool_calls=tool_calls,
            reviewed_ids=reviewed_ids,
        )
        execution = runtime.execution_info
        saver_value = configurable.get("__pregel_checkpointer")
        saver: _CheckpointSaver | None = None
        source_config: RunnableConfig = {}
        graph_namespace = ""
        if isinstance(saver_value, BaseCheckpointSaver) and execution is not None:
            if configurable.get("__pregel_durability") != "sync":
                raise ValueError("TinkerFin tool review requires durability='sync'")
            saver = cast(_CheckpointSaver, saver_value)
            # prepare_single_task adds a final node/task component. Retain every
            # parent Graph component and invocation slot when locating its saver.
            graph_namespace = execution.checkpoint_ns.rpartition("|")[0]
            source_config = {
                "configurable": {
                    "thread_id": configurable["thread_id"],
                    "checkpoint_ns": graph_namespace,
                    "checkpoint_id": execution.checkpoint_id,
                }
            }
            checkpoint = await saver.aget_tuple(source_config)
            if checkpoint is None:
                raise TinkerFinLifecycleError(
                    "tool review source checkpoint is unavailable"
                )
            saved = pending_tool_review(checkpoint, task_id=execution.task_id)
            # A changed policy may now select no tools. Check before the empty
            # request return, otherwise previously cancelled actions could execute.
            if saved is not None and (
                saved.task_id != execution.task_id
                or saved.graph_namespace != graph_namespace
                or saved.batch_digest != digest
            ):
                raise TinkerFinLifecycleError(
                    "tool review changed while awaiting a decision"
                )
        if not selected:
            return None
        try:
            response: object = interrupt(request)
        except GraphInterrupt as paused:
            if saver is None or execution is None:
                raise TinkerFinLifecycleError(
                    "tool review requires an asynchronous checkpointer"
                ) from paused
            pending = cast(Sequence[Interrupt], paused.args[0])
            if len(pending) != 1:
                raise TinkerFinLifecycleError(
                    "tool review requires one native interrupt group"
                ) from paused
            run_id = configurable.get(RUN_ID_METADATA_KEY)
            record = PendingToolReview(
                graph_namespace=graph_namespace,
                run_id=run_id if isinstance(run_id, str) else None,
                task_id=execution.task_id,
                interrupt_id=pending[0].id,
                batch_digest=digest,
            )
            await _save_tool_review(saver, source_config, record)
            raise
        if not isinstance(response, Mapping):
            raise TypeError("tool review decisions must be a mapping")
        decisions_value = cast(Mapping[object, object], response).get("decisions")
        if not isinstance(decisions_value, list):
            raise TypeError("tool review decisions must be a list")
        decisions = cast(list[Decision], decisions_value)
        if len(decisions) != len(selected):
            raise ValueError(
                "tool review decisions must cover the complete action batch"
            )
        revised: list[ToolCall] = []
        tool_messages: list[ToolMessage] = []
        decision_index = 0
        for index, tool_call in enumerate(tool_calls):
            policy = selected.get(index)
            if policy is None:
                revised.append(tool_call)
                continue
            updated, result = self._process_decision(
                decisions[decision_index], tool_call, policy
            )
            decision_index += 1
            if updated is not None:
                revised.append(updated)
            if result is not None:
                tool_messages.append(result)
        assert message is not None
        message.tool_calls = revised
        return {"messages": [message, *tool_messages]}

    @staticmethod
    def _process_decision(
        decision: Decision,
        tool_call: ToolCall,
        config: InterruptOnConfig,
    ) -> tuple[ToolCall | None, ToolMessage | None]:
        raw_decision = cast(Mapping[str, object], decision)
        if raw_decision.get("type") != CANCEL_DECISION_TYPE:
            return HumanInTheLoopMiddleware._process_decision(
                decision,
                tool_call,
                config,
            )
        tool_call_id = tool_call.get("id")
        tool_name = tool_call.get("name")
        if not isinstance(tool_call_id, str) or not tool_call_id:
            raise ValueError("cancelled Tool call requires a stable ID")
        if not isinstance(tool_name, str) or not tool_name:
            raise ValueError("cancelled Tool call requires a stable name")
        message = ToolMessage(
            content=f"Tool call `{tool_name}` was cancelled before execution.",
            name=tool_name,
            tool_call_id=tool_call_id,
            id=str(
                uuid5(
                    NAMESPACE_URL,
                    f"{HITL_CONTRACT_ID}:{tool_name}:{tool_call_id}",
                )
            ),
            status="error",
            additional_kwargs={
                "tinkerfin": {
                    "schema": HITL_CONTRACT_ID,
                    "outcome": "cancelled",
                    "executed": False,
                }
            },
        )
        return None, message


def _as_permissions(value: object) -> tuple[FilesystemPermission, ...]:
    if value is None:
        return ()
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise TypeError("permissions must be a sequence or None")
    rules = tuple(cast(Sequence[object], value))
    if any(not isinstance(rule, FilesystemPermission) for rule in rules):
        raise TypeError("permissions must contain FilesystemPermission values")
    return cast(tuple[FilesystemPermission, ...], rules)


def _as_interrupt_on(value: object) -> dict[str, bool | InterruptOnConfig]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise TypeError("interrupt_on must be a mapping or None")
    return dict(cast(Mapping[str, bool | InterruptOnConfig], value))


def create_tool_review(
    permissions: Sequence[FilesystemPermission],
    interrupt_on: Mapping[str, bool | InterruptOnConfig] | None,
) -> ToolReviewMiddleware | None:
    """Create one review owner for a role's explicit and file-based policies.

    The caller places review before application middleware in the stack, so
    LangChain's reverse after-model ordering reviews the final effective calls.
    File rules retain their normal first-match behavior; cancellation and persisted
    review identity remain framework responsibilities.
    """
    merged = {
        **_permission_interrupts(_as_permissions(permissions)),
        **_as_interrupt_on(interrupt_on),
    }
    if not merged:
        return None
    review = ToolReviewMiddleware(merged)
    return review if review.interrupt_on else None


__all__ = ["CANCEL_DECISION_TYPE", "HITL_CONTRACT_ID"]

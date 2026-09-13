"""Checkpoint lineage owned by the public AG-UI run boundary."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Literal, TypeAlias, cast

from langchain_core.messages import AIMessage, BaseMessage, ToolCall
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver, CheckpointTuple
from langgraph.graph.message import Messages, add_messages
from langgraph.types import StateSnapshot
from pydantic import ValidationError

from tinkerfin_contracts import RunIdentity
from tinkerfin_native_stream import NativeRuntimeInterrupt

from ._agui_lineage_state import (
    CHECKPOINT_ROLE_METADATA_KEY,
    LINEAGE_METADATA_KEY,
    NAMESPACE_METADATA_KEY,
    NATIVE_CHECKPOINT_ROLE,
    PARENT_RUN_ID_METADATA_KEY,
    PLANNING_CHECKPOINT_ROLE,
    RESUME_CONFIG_KEY,
    RESUME_METADATA_KEY,
    RUN_ID_METADATA_KEY,
    RUNTIME_PROFILE_METADATA_KEY,
    LineageMarker,
    LineageRole,
    ResumeAnchor,
    ResumeIntent,
    bind_checkpoint_run,
)
from ._hitl_state import pending_tool_review
from ._tasks import run_async_owned
from .errors import TinkerFinLifecycleError

if TYPE_CHECKING:
    from .agui_resume import AgUiResumeBinding
    from .runtime_profile import DeepAgentsRuntimeProfile

_CHECKPOINTER_RUN_ID_KEY = "run_id"
_MAX_RESUME_ANCESTRY_DEPTH = 4096
_MAX_RESUME_GRAPH_SCOPES = 4096

_CheckpointSaver: TypeAlias = (
    BaseCheckpointSaver[int] | BaseCheckpointSaver[float] | BaseCheckpointSaver[str]
)


@dataclass(frozen=True, slots=True)
class AgUiLineageResolution:
    """Resolved invocation checkpoint and durable resume progress."""

    config: RunnableConfig
    resume_phase: Literal["none", "unstaged", "prepared", "accepted"]
    parent_run_id: str | None
    checkpoint_role: LineageRole
    missing_interrupt_ids: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class _DurableResumeProgress:
    """Verified staged intent, its source checkpoint, and submission progress."""

    head: CheckpointTuple
    stage: CheckpointTuple
    source: CheckpointTuple
    marker: ResumeIntent
    phase: Literal["prepared", "accepted"]
    missing_interrupt_ids: frozenset[str]


@dataclass(frozen=True, slots=True)
class AgUiThreadHead:
    """Saver-neutral canonical thread head and its owning Graph role."""

    config: RunnableConfig
    role: LineageRole


@dataclass(frozen=True, slots=True)
class AgUiResumeContext:
    """Hold trusted pending interrupts and message correlation from one checkpoint."""

    interrupts: tuple[NativeRuntimeInterrupt, ...]
    messages_by_graph_namespace: Mapping[tuple[str, ...], tuple[BaseMessage, ...]]
    interrupt_graph_namespaces: Mapping[str, tuple[str, ...]]
    cancellation_interrupt_ids: frozenset[str]
    sources: Mapping[str, _InterruptSource]


@dataclass(frozen=True, slots=True)
class _ResumeGraphEvidence:
    messages: Mapping[tuple[str, ...], tuple[BaseMessage, ...]]
    sources: Mapping[str, _InterruptSource]


@dataclass(frozen=True, slots=True)
class _InterruptSource:
    graph_namespace: tuple[str, ...]
    config: RunnableConfig
    task_id: str | None


def _record_interrupt_source(
    sources: dict[str, _InterruptSource], interrupt_id: str, source: _InterruptSource
) -> None:
    # LangGraph repeats descendant interrupts on ancestor tasks. Keep the deepest
    # owner, and reject conflicting branches instead of choosing by scan order.
    previous = sources.get(interrupt_id)
    if previous is None:
        sources[interrupt_id] = source
        return
    left, right = previous.graph_namespace, source.graph_namespace
    if left == right:
        if previous.config.get("configurable", {}).get(
            "checkpoint_id"
        ) != source.config.get("configurable", {}).get("checkpoint_id"):
            raise TinkerFinLifecycleError(
                "checkpoint interrupt has conflicting source checkpoints"
            )
    elif right[: len(left)] == left:
        sources[interrupt_id] = source
    elif left[: len(right)] != right:
        raise TinkerFinLifecycleError(
            "checkpoint interrupt has conflicting graph sources"
        )


def _checkpoint_id(checkpoint: CheckpointTuple) -> str:
    value = checkpoint.config.get("configurable", {}).get("checkpoint_id")
    if not isinstance(value, str) or not value:
        raise TinkerFinLifecycleError("AG-UI lineage checkpoint has no stable ID")
    return value


def _checkpoint_ancestry_key(checkpoint: CheckpointTuple) -> tuple[str, str, str]:
    """Return the complete saver location used to detect corrupt parent cycles."""

    configurable = checkpoint.config.get("configurable", {})
    thread_id = configurable.get("thread_id")
    namespace = configurable.get("checkpoint_ns", "")
    if not isinstance(thread_id, str) or not thread_id:
        raise TinkerFinLifecycleError(
            "AG-UI lineage checkpoint has no stable thread identity"
        )
    if not isinstance(namespace, str):
        raise TinkerFinLifecycleError(
            "AG-UI lineage checkpoint has no stable namespace"
        )
    return thread_id, namespace, _checkpoint_id(checkpoint)


def _checkpoint_role(checkpoint: CheckpointTuple) -> str:
    marker = _checkpoint_lineage(checkpoint)
    if marker is None:
        raise TinkerFinLifecycleError(
            "AG-UI lineage checkpoint has no private lineage marker"
        )
    return marker.role


def _require_runtime_profile(
    checkpoint: CheckpointTuple,
    *,
    expected: str,
) -> None:
    """Reject a checkpoint created by another explicit Runtime Profile."""

    marker = _checkpoint_lineage(checkpoint)
    if marker is None:
        raise TinkerFinLifecycleError(
            "checkpoint has no private Runtime Profile marker"
        )
    if marker.runtime_profile != expected:
        raise TinkerFinLifecycleError(
            "checkpoint belongs to another Runtime Profile",
            context={"runtime_profile": expected},
            diagnostic_context={"checkpoint_runtime_profile": marker.runtime_profile},
        )


def _checkpoint_lineage(checkpoint: CheckpointTuple) -> LineageMarker | None:
    """Read canonical ownership written by the scoped saver, never Graph state."""

    raw = checkpoint.metadata.get(LINEAGE_METADATA_KEY)
    if raw is None:
        return None
    try:
        if not isinstance(raw, str):
            raise TypeError("lineage metadata must be canonical JSON")
        marker = LineageMarker.model_validate_json(raw)
        if raw != marker.canonical_json():
            raise ValueError("lineage metadata is not canonical")
        if marker.thread_id != checkpoint.config.get("configurable", {}).get(
            "thread_id"
        ):
            raise ValueError("lineage metadata belongs to another thread")
    except (TypeError, ValueError) as error:
        raise TinkerFinLifecycleError(
            "checkpoint contains invalid run ownership evidence", cause=error
        ) from error
    return marker


async def _run_checkpoints(
    checkpointer: _CheckpointSaver,
    *,
    thread_id: str,
    run_id: str,
) -> list[CheckpointTuple]:
    """List root checkpoints created by one run from newest to oldest."""

    config: RunnableConfig = {
        "configurable": {"thread_id": thread_id, "checkpoint_ns": ""}
    }
    candidates: list[CheckpointTuple] = []
    async for checkpoint in checkpointer.alist(config):
        marker = _checkpoint_lineage(checkpoint)
        if marker is not None and marker.run_id == run_id:
            candidates.append(checkpoint)
    return candidates


def _unique_leaf(
    checkpoints: list[CheckpointTuple],
    *,
    missing_message: str,
    ambiguous_message: str,
    run_id: str,
) -> CheckpointTuple:
    """Select one durable leaf without trusting list order or saver internals.

    Parent checkpoint references define the local DAG. Zero leaves means missing
    evidence and multiple leaves mean ambiguous lineage; neither case may silently pick
    the newest row because branch and resume must be deterministic across saver types.
    """

    if not checkpoints:
        raise TinkerFinLifecycleError(
            missing_message,
            context={"parent_run_id": run_id},
        )

    checkpoint_ids = {_checkpoint_id(checkpoint) for checkpoint in checkpoints}
    referenced_parents = {
        parent_id
        for checkpoint in checkpoints
        if checkpoint.parent_config is not None
        for parent_id in (
            checkpoint.parent_config.get("configurable", {}).get("checkpoint_id"),
        )
        if isinstance(parent_id, str) and parent_id in checkpoint_ids
    }
    heads = [
        checkpoint
        for checkpoint in checkpoints
        if _checkpoint_id(checkpoint) not in referenced_parents
    ]
    if len(heads) != 1:
        raise TinkerFinLifecycleError(
            ambiguous_message,
            context={"parent_run_id": run_id},
        )
    return heads[0]


def _completed_plan_native_leaf(
    checkpoints: list[CheckpointTuple],
    *,
    run_id: str,
) -> CheckpointTuple | None:
    """Resolve a completed Planning branch to its durable native terminal."""

    completed_ids: set[str] = set()
    for checkpoint in checkpoints:
        marker = _checkpoint_lineage(checkpoint)
        if marker is None or marker.role != PLANNING_CHECKPOINT_ROLE:
            continue
        channel_values = checkpoint.checkpoint.get("channel_values")
        if not isinstance(channel_values, Mapping):
            continue
        plan = cast(Mapping[object, object], channel_values).get("tinkerfin_plan")
        if not isinstance(plan, Mapping):
            continue
        handoff = cast(Mapping[object, object], plan).get("handoff")
        if not isinstance(handoff, Mapping):
            continue
        handoff_values = cast(Mapping[object, object], handoff)
        if handoff_values.get("phase") != "completed":
            continue
        completed_id = handoff_values.get(
            "completedCheckpointId",
            handoff_values.get("completed_checkpoint_id"),
        )
        if not isinstance(completed_id, str) or not completed_id:
            raise TinkerFinLifecycleError(
                "completed Plan lineage has no native terminal checkpoint",
                context={"parent_run_id": run_id},
            )
        completed_ids.add(completed_id)
    if not completed_ids:
        return None
    if len(completed_ids) != 1:
        raise TinkerFinLifecycleError(
            "completed Plan lineage references ambiguous native terminals",
            context={"parent_run_id": run_id},
        )
    completed_id = next(iter(completed_ids))
    matches = [
        checkpoint
        for checkpoint in checkpoints
        if _checkpoint_id(checkpoint) == completed_id
        and (marker := _checkpoint_lineage(checkpoint)) is not None
        and marker.role == NATIVE_CHECKPOINT_ROLE
    ]
    if len(matches) != 1:
        raise TinkerFinLifecycleError(
            "completed Plan lineage cannot resolve its native terminal",
            context={"parent_run_id": run_id},
        )
    return matches[0]


async def _run_head(
    checkpointer: _CheckpointSaver,
    *,
    thread_id: str,
    run_id: str,
) -> CheckpointTuple:
    """Return the unique root checkpoint leaf created by one run."""

    checkpoints = await _run_checkpoints(
        checkpointer,
        thread_id=thread_id,
        run_id=run_id,
    )
    completed_plan = _completed_plan_native_leaf(checkpoints, run_id=run_id)
    if completed_plan is not None:
        return completed_plan
    return _unique_leaf(
        checkpoints,
        missing_message="parentRunId has no checkpoint in this canonical thread",
        ambiguous_message="parentRunId resolves to ambiguous checkpoint branches",
        run_id=run_id,
    )


async def resolve_agui_native_run_head(
    checkpointer: object,
    *,
    thread_id: str,
    run_id: str,
    runtime_profile: str,
) -> CheckpointTuple:
    """Return the unique native checkpoint leaf for one indexed semantic run."""

    if not isinstance(checkpointer, BaseCheckpointSaver):
        raise TinkerFinLifecycleError(
            "native run lineage requires a concrete BaseCheckpointSaver"
        )
    checkpoints = await _run_checkpoints(
        cast(_CheckpointSaver, checkpointer),
        thread_id=thread_id,
        run_id=run_id,
    )
    native = [
        checkpoint
        for checkpoint in checkpoints
        if (marker := _checkpoint_lineage(checkpoint)) is not None
        and marker.role == NATIVE_CHECKPOINT_ROLE
    ]
    selected = _unique_leaf(
        native,
        missing_message="native run has no checkpoint in this canonical thread",
        ambiguous_message="native run resolves to ambiguous checkpoint branches",
        run_id=run_id,
    )
    _require_runtime_profile(selected, expected=runtime_profile)
    return selected


async def _read_snapshot(
    astream: Callable[..., object],
    checkpoint: CheckpointTuple,
) -> StateSnapshot:
    owner = getattr(astream, "__self__", None)
    role = _checkpoint_role(checkpoint)
    role_reader = getattr(owner, "_tinkerfin_lineage_state", None)
    if callable(role_reader):
        return await cast(Callable[..., Awaitable[StateSnapshot]], role_reader)(
            checkpoint.config,
            role,
            subgraphs=True,
        )
    if role != NATIVE_CHECKPOINT_ROLE:
        raise TinkerFinLifecycleError(
            "planning checkpoint requires a Plan-capable lineage reader"
        )
    reader = getattr(owner, "aget_state", None)
    if not callable(reader):
        raise TinkerFinLifecycleError(
            "parentRunId requires a graph with asynchronous state inspection"
        )
    return await cast(Callable[..., Awaitable[StateSnapshot]], reader)(
        checkpoint.config,
        subgraphs=True,
    )


def _interrupt_ids(snapshot: StateSnapshot) -> frozenset[str]:
    ids = frozenset(interrupt.id for interrupt in snapshot.interrupts)
    if len(ids) != len(snapshot.interrupts):
        raise TinkerFinLifecycleError(
            "parent checkpoint contains duplicate interrupt identities"
        )
    return ids


def _snapshot_namespace(snapshot: StateSnapshot) -> tuple[str, ...]:
    """Decode LangGraph's complete checkpoint namespace without truncating IDs."""

    value = snapshot.config.get("configurable", {}).get("checkpoint_ns", "")
    if not isinstance(value, str):
        raise TinkerFinLifecycleError("checkpoint namespace is not text")
    if not value:
        return ()
    components = tuple(value.split("|"))
    if any(not component for component in components):
        raise TinkerFinLifecycleError("checkpoint namespace has an empty component")
    return components


def _checkpoint_message_sequence(value: object) -> tuple[BaseMessage, ...]:
    """Validate one committed or pending LangGraph messages-channel value."""

    if isinstance(value, BaseMessage):
        return (value,)
    if not isinstance(value, list | tuple):
        raise TinkerFinLifecycleError(
            "checkpoint messages channel is not a message sequence"
        )
    raw_messages = cast(list[object] | tuple[object, ...], value)
    messages = tuple(raw_messages)
    if any(not isinstance(message, BaseMessage) for message in messages):
        raise TinkerFinLifecycleError(
            "checkpoint messages channel contains a non-LangChain value"
        )
    return tuple(cast(BaseMessage, message) for message in messages)


async def _dynamic_resume_messages(
    checkpointer: _CheckpointSaver,
    *,
    source: CheckpointTuple,
    snapshot: StateSnapshot,
    runtime_profile: DeepAgentsRuntimeProfile,
) -> _ResumeGraphEvidence:
    """Recover messages from Profile-owned dynamic subgraph checkpoint ancestry."""

    matcher_value = getattr(
        runtime_profile,
        "_matches_resume_subgraph_namespace",
        None,
    )
    if not callable(matcher_value):
        return _ResumeGraphEvidence(MappingProxyType({}), MappingProxyType({}))
    matcher = cast(
        Callable[[tuple[str, ...], frozenset[str]], bool],
        matcher_value,
    )
    pending_task_ids = frozenset(task.id for task in snapshot.tasks if task.interrupts)
    if not pending_task_ids:
        return _ResumeGraphEvidence(MappingProxyType({}), MappingProxyType({}))

    source_marker = _checkpoint_lineage(source)
    if source_marker is None:
        raise TinkerFinLifecycleError("resume source has no private lineage marker")
    thread_id = source_marker.thread_id
    namespaces: set[str] = set()
    listing_config: RunnableConfig = {
        "configurable": {
            "thread_id": thread_id,
        }
    }
    # Completed calls can leave extensive history in the same thread. Only the
    # distinct scopes belonging to current pending tasks occupy this working set.
    async for checkpoint in checkpointer.alist(listing_config):
        raw_namespace = checkpoint.config.get("configurable", {}).get(
            "checkpoint_ns",
            "",
        )
        if not isinstance(raw_namespace, str):
            raise TinkerFinLifecycleError("resume checkpoint namespace is not text")
        if not raw_namespace:
            continue
        namespace = tuple(raw_namespace.split("|"))
        if any(not component for component in namespace):
            raise TinkerFinLifecycleError(
                "resume checkpoint namespace has an empty component"
            )
        try:
            selected = matcher(namespace, pending_task_ids)
        except Exception as error:
            raise TinkerFinLifecycleError(
                "Runtime Profile rejected dynamic resume namespace inspection",
                cause=error,
            ) from error
        if not isinstance(selected, bool):
            raise TinkerFinLifecycleError(
                "Runtime Profile namespace matcher did not return bool"
            )
        if selected:
            namespaces.add(raw_namespace)
            if len(namespaces) > _MAX_RESUME_GRAPH_SCOPES:
                raise TinkerFinLifecycleError(
                    "resume graph scopes exceeded their safe bound",
                    diagnostic_context={
                        "maximum_graph_scopes": _MAX_RESUME_GRAPH_SCOPES
                    },
                )

    messages_by_graph_namespace: dict[tuple[str, ...], tuple[BaseMessage, ...]] = {}
    sources: dict[str, _InterruptSource] = {}
    for raw_namespace in sorted(namespaces):
        config: RunnableConfig = {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": raw_namespace,
            }
        }
        current = await checkpointer.aget_tuple(config)
        if current is None:
            raise TinkerFinLifecycleError(
                "dynamic resume checkpoint namespace has no head"
            )
        graph_namespace = tuple(raw_namespace.split("|"))
        # LangGraph 1.2.10 stores pending Interrupt batches under __interrupt__.
        # Read only the current head; historical completed calls do not own a resume.
        for task_id, channel, value in current.pending_writes or ():
            if channel != "__interrupt__":
                continue
            if not isinstance(value, list | tuple):
                raise TinkerFinLifecycleError(
                    "checkpoint interrupt batch is not a sequence"
                )
            for raw_interrupt in cast(list[object] | tuple[object, ...], value):
                pending_interrupt = NativeRuntimeInterrupt.model_validate(raw_interrupt)
                _record_interrupt_source(
                    sources,
                    pending_interrupt.id,
                    _InterruptSource(graph_namespace, current.config, task_id),
                )
        ancestry: list[CheckpointTuple] = []
        visited: set[tuple[str, str, str]] = set()
        for _depth in range(_MAX_RESUME_ANCESTRY_DEPTH):
            key = _checkpoint_ancestry_key(current)
            if key in visited:
                raise TinkerFinLifecycleError(
                    "dynamic resume checkpoint ancestry contains a cycle"
                )
            visited.add(key)
            if key[0] != thread_id or key[1] != raw_namespace:
                raise TinkerFinLifecycleError(
                    "dynamic resume checkpoint ancestry changed scope"
                )
            _require_runtime_profile(
                current,
                expected=runtime_profile.profile_id,
            )
            marker = _checkpoint_lineage(current)
            if marker is None or marker.thread != source_marker.thread:
                raise TinkerFinLifecycleError(
                    "dynamic resume checkpoint belongs to another thread"
                )
            ancestry.append(current)
            if current.parent_config is None:
                break
            parent = await checkpointer.aget_tuple(current.parent_config)
            if parent is None:
                raise TinkerFinLifecycleError(
                    "dynamic resume checkpoint ancestry is incomplete"
                )
            current = parent
        else:
            raise TinkerFinLifecycleError(
                "dynamic resume checkpoint ancestry exceeds the safe depth",
                diagnostic_context={
                    "maximum_depth": _MAX_RESUME_ANCESTRY_DEPTH,
                },
            )

        folded: list[BaseMessage] = []
        for checkpoint in reversed(ancestry):
            channel_values = checkpoint.checkpoint.get("channel_values")
            if isinstance(channel_values, Mapping) and "messages" in channel_values:
                folded = list(
                    _checkpoint_message_sequence(
                        cast(Mapping[object, object], channel_values)["messages"]
                    )
                )
            for _task_id, channel, value in checkpoint.pending_writes or ():
                if channel != "messages":
                    continue
                # LangGraph's public alias is invariant even though validated
                # BaseMessage lists are its canonical runtime input and output shape.
                merged: object = add_messages(
                    cast(Messages, list(folded)),
                    cast(Messages, list(_checkpoint_message_sequence(value))),
                )
                folded = list(_checkpoint_message_sequence(merged))
        if folded:
            messages_by_graph_namespace[graph_namespace] = tuple(folded)
    return _ResumeGraphEvidence(
        MappingProxyType(messages_by_graph_namespace), MappingProxyType(sources)
    )


async def _resume_context(
    snapshot: StateSnapshot,
    *,
    checkpointer: _CheckpointSaver,
    source: CheckpointTuple,
    runtime_profile: DeepAgentsRuntimeProfile,
) -> AgUiResumeContext:
    """Collect root, materialized, and dynamic subgraph resume correlation."""

    messages_by_graph_namespace: dict[tuple[str, ...], tuple[BaseMessage, ...]] = {}
    sources: dict[str, _InterruptSource] = {}
    interrupts_by_id: dict[str, NativeRuntimeInterrupt] = {}

    def visit(current: StateSnapshot) -> None:
        namespace = _snapshot_namespace(current)
        values = current.values
        if not isinstance(values, Mapping):
            raise TinkerFinLifecycleError("checkpoint values are not a mapping")
        raw_messages = cast(Mapping[object, object], values).get("messages", ())
        if not isinstance(raw_messages, (list, tuple)):
            raise TinkerFinLifecycleError("checkpoint messages are not a sequence")
        message_values = cast(list[object] | tuple[object, ...], raw_messages)
        resolved_messages = tuple(
            message for message in message_values if isinstance(message, BaseMessage)
        )
        if len(resolved_messages) != len(message_values):
            raise TinkerFinLifecycleError(
                "checkpoint messages contain a non-LangChain value"
            )
        previous_messages = messages_by_graph_namespace.get(namespace)
        if previous_messages is not None and previous_messages != resolved_messages:
            raise TinkerFinLifecycleError(
                "checkpoint repeats one namespace with conflicting messages"
            )
        messages_by_graph_namespace[namespace] = resolved_messages
        for raw_interrupt in current.interrupts:
            interrupt = NativeRuntimeInterrupt.model_validate(raw_interrupt)
            previous_interrupt = interrupts_by_id.get(interrupt.id)
            if previous_interrupt is not None and previous_interrupt != interrupt:
                raise TinkerFinLifecycleError(
                    "checkpoint repeats one interrupt ID with conflicting values"
                )
            interrupts_by_id[interrupt.id] = interrupt
            task_ids = {
                task.id
                for task in current.tasks
                if any(value.id == interrupt.id for value in task.interrupts)
            }
            if len(task_ids) > 1:
                raise TinkerFinLifecycleError(
                    "checkpoint interrupt has conflicting task owners"
                )
            _record_interrupt_source(
                sources,
                interrupt.id,
                _InterruptSource(namespace, current.config, next(iter(task_ids), None)),
            )
        for task in current.tasks:
            if isinstance(task.state, StateSnapshot):
                visit(task.state)

    visit(snapshot)
    dynamic_messages = await _dynamic_resume_messages(
        checkpointer,
        source=source,
        snapshot=snapshot,
        runtime_profile=runtime_profile,
    )
    for namespace, messages in dynamic_messages.messages.items():
        previous_messages = messages_by_graph_namespace.get(namespace)
        if previous_messages is not None and previous_messages != messages:
            raise TinkerFinLifecycleError(
                "checkpoint repeats one namespace with conflicting dynamic messages"
            )
        messages_by_graph_namespace[namespace] = messages
    for interrupt_id, interrupt_source in dynamic_messages.sources.items():
        if interrupt_id in interrupts_by_id:
            _record_interrupt_source(sources, interrupt_id, interrupt_source)
    if not interrupts_by_id:
        raise TinkerFinLifecycleError("checkpoint has no pending interrupt to resume")
    source_marker = _checkpoint_lineage(source)
    if source_marker is None:
        raise TinkerFinLifecycleError("resume source has no private lineage marker")
    cancellation_interrupt_ids: set[str] = set()
    for interrupt_id, owner in sources.items():
        checkpoint = await checkpointer.aget_tuple(owner.config)
        if checkpoint is None or owner.task_id is None:
            continue
        # Only the actual middleware's dedicated saver write can authorize this
        # interrupt. Inherited names, ordinary input, and another task's review
        # record cannot grant cancellation support to the current owner.
        try:
            proof = pending_tool_review(checkpoint, task_id=owner.task_id)
        except ValidationError:
            continue
        if (
            proof is not None
            and proof.graph_namespace == "|".join(owner.graph_namespace)
            and (owner_lineage := _checkpoint_lineage(checkpoint)) is not None
            and proof.run_id == owner_lineage.run_id
            and proof.task_id == owner.task_id
            and proof.interrupt_id == interrupt_id
        ):
            cancellation_interrupt_ids.add(interrupt_id)
    return AgUiResumeContext(
        interrupts=tuple(interrupts_by_id.values()),
        messages_by_graph_namespace=MappingProxyType(dict(messages_by_graph_namespace)),
        interrupt_graph_namespaces=MappingProxyType(
            {
                interrupt_id: owner.graph_namespace
                for interrupt_id, owner in sources.items()
            }
        ),
        cancellation_interrupt_ids=frozenset(cancellation_interrupt_ids),
        sources=MappingProxyType(sources),
    )


async def resolve_agui_resume_context(
    astream: Callable[..., object],
    *,
    identity: RunIdentity,
    parent_run_id: str | None,
    runtime_profile: DeepAgentsRuntimeProfile,
) -> AgUiResumeContext:
    """Resolve trusted resume correlation from the Profile-bound canonical head.

    Runtime Profile ownership is verified before Graph state is loaded. The returned
    context comes only from the configured checkpointer; client and host databases do
    not supply interrupt payloads or Tool correlation.

    Args:
        astream: Profile-bound Graph stream exposing the concrete checkpointer.
        identity: New resume Run identity in the existing checkpoint thread.
        parent_run_id: Optional interrupted Run selected explicitly by the host.
        runtime_profile: Exact Profile required by checkpoint lineage.

    Returns:
        Trusted pending interrupts and message correlation from the canonical source.

    Raises:
        TinkerFinLifecycleError: Lineage, Profile, checkpoint, or interrupt evidence is
            missing, ambiguous, or inconsistent.
    """

    checkpointer = _require_checkpointer(astream)
    marker = await _find_resume_intent(
        checkpointer, identity=identity, runtime_profile=runtime_profile
    )
    if marker is not None:
        if parent_run_id is not None and marker.parent_run_id != parent_run_id:
            raise TinkerFinLifecycleError(
                "durable resume marker belongs to another parentRunId"
            )
        await _resume_stage_and_source(
            checkpointer,
            head=await _required_thread_head(checkpointer, identity),
            identity=identity,
            marker=marker,
            runtime_profile=runtime_profile,
        )
        return _anchored_resume_context(marker)
    if parent_run_id is None:
        head = await _thread_head(checkpointer, thread_id=identity.thread_id)
        if head is None:
            raise TinkerFinLifecycleError(
                "resume has no checkpoint in this canonical thread"
            )
        checkpoint = head
    else:
        checkpoint = await _run_head(
            checkpointer,
            thread_id=identity.thread_id,
            run_id=parent_run_id,
        )
    _require_runtime_profile(checkpoint, expected=runtime_profile.profile_id)
    snapshot = await _read_snapshot(astream, checkpoint)
    failures = tuple(task.error for task in snapshot.tasks if task.error is not None)
    if failures:
        raise TinkerFinLifecycleError(
            "resume checkpoint contains failed task evidence",
            diagnostic_context={"failed_task_count": len(failures)},
        )
    return await _resume_context(
        snapshot,
        checkpointer=checkpointer,
        source=checkpoint,
        runtime_profile=runtime_profile,
    )


async def _required_thread_head(
    checkpointer: _CheckpointSaver, identity: RunIdentity
) -> CheckpointTuple:
    head = await _thread_head(checkpointer, thread_id=identity.thread_id)
    if head is None:
        raise TinkerFinLifecycleError("resume has no checkpoint in this thread")
    return head


def _anchor_config(anchor: ResumeAnchor) -> RunnableConfig:
    return {
        "configurable": {
            "thread_id": anchor.source.thread_id,
            "checkpoint_ns": anchor.graph_namespace,
            "checkpoint_id": anchor.checkpoint_id,
        }
    }


async def _approval_anchors(
    context: AgUiResumeContext, checkpointer: _CheckpointSaver
) -> tuple[ResumeAnchor, ...]:
    """Freeze only the original approval payloads and their matched Tool calls."""

    from tinkerfin_agui_adapter.hitl import (
        HitlRequest,
        match_hitl_tool_call_id_groups,
    )

    calls: dict[str, list[ToolCall]] = {}
    grouped: dict[tuple[str, ...], list[NativeRuntimeInterrupt]] = {}
    for interrupt in context.interrupts:
        namespace = context.interrupt_graph_namespaces[interrupt.id]
        grouped.setdefault(namespace, []).append(interrupt)
    for namespace, interrupts in grouped.items():
        try:
            reviews = [HitlRequest.model_validate(item.value) for item in interrupts]
        except ValidationError:
            # Runtime approvals such as Plan selection carry their own Schema and
            # do not correlate to a model Tool call. The binding validates the kind.
            continue
        messages = context.messages_by_graph_namespace[namespace]
        matched = match_hitl_tool_call_id_groups(
            [review.action_requests for review in reviews], messages
        )
        available = {
            call["id"]: call
            for message in messages
            if isinstance(message, AIMessage)
            for call in message.tool_calls
        }
        for interrupt, ids in zip(interrupts, matched, strict=True):
            calls[interrupt.id] = [available[call_id] for call_id in ids]

    anchors: list[ResumeAnchor] = []
    for interrupt in sorted(context.interrupts, key=lambda item: item.id):
        owner = context.sources[interrupt.id]
        checkpoint = await checkpointer.aget_tuple(owner.config)
        if checkpoint is None or owner.task_id is None:
            raise TinkerFinLifecycleError("approval has no durable task source")
        lineage = _checkpoint_lineage(checkpoint)
        if lineage is None:
            raise TinkerFinLifecycleError("approval source has no managed ownership")
        anchors.append(
            ResumeAnchor(
                source=lineage,
                graph_namespace="|".join(owner.graph_namespace),
                checkpoint_id=_checkpoint_id(checkpoint),
                task_id=owner.task_id,
                interrupt_id=interrupt.id,
                interrupt_json=json.dumps(
                    interrupt.model_dump(mode="json"),
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=True,
                    allow_nan=False,
                ),
                tool_calls_json=json.dumps(
                    calls.get(interrupt.id, []),
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=True,
                    allow_nan=False,
                ),
                cancellation_supported=(
                    interrupt.id in context.cancellation_interrupt_ids
                ),
            )
        )
    return tuple(anchors)


def _anchored_resume_context(marker: ResumeIntent) -> AgUiResumeContext:
    """Rebuild the original review without reading a child's current head."""

    messages: dict[tuple[str, ...], tuple[BaseMessage, ...]] = {}
    sources: dict[str, _InterruptSource] = {}
    interrupts: list[NativeRuntimeInterrupt] = []
    for anchor in marker.anchors:
        namespace = (
            tuple(anchor.graph_namespace.split("|")) if anchor.graph_namespace else ()
        )
        interrupt = NativeRuntimeInterrupt.model_validate_json(anchor.interrupt_json)
        if interrupt.id != anchor.interrupt_id:
            raise TinkerFinLifecycleError(
                "approval anchor has conflicting interrupt ID"
            )
        # AIMessage validates the stored Tool-call JSON at this private boundary.
        message = AIMessage.model_validate(
            {"content": "", "tool_calls": json.loads(anchor.tool_calls_json)}
        )
        messages[namespace] = (*messages.get(namespace, ()), message)
        interrupts.append(interrupt)
        sources[interrupt.id] = _InterruptSource(
            namespace, _anchor_config(anchor), anchor.task_id
        )
    return AgUiResumeContext(
        interrupts=tuple(interrupts),
        messages_by_graph_namespace=MappingProxyType(messages),
        interrupt_graph_namespaces=MappingProxyType(
            {key: owner.graph_namespace for key, owner in sources.items()}
        ),
        cancellation_interrupt_ids=frozenset(
            anchor.interrupt_id
            for anchor in marker.anchors
            if anchor.cancellation_supported
        ),
        sources=MappingProxyType(sources),
    )


async def _thread_head(
    checkpointer: _CheckpointSaver,
    *,
    thread_id: str,
) -> CheckpointTuple | None:
    config: RunnableConfig = {
        "configurable": {
            "thread_id": thread_id,
            "checkpoint_ns": "",
        }
    }
    return await checkpointer.aget_tuple(config)


async def resolve_agui_thread_head(
    checkpointer: object,
    *,
    thread_id: str,
    runtime_profile: str,
) -> AgUiThreadHead | None:
    """Resolve the canonical thread head without asking the wrong Graph to load it.

    Reading ``CheckpointTuple`` directly preserves pending sends until the owning
    Graph is known. Calling ``aget_state()`` on another Graph can otherwise discard
    unknown node sends while merely trying to determine the resume route.

    Args:
        checkpointer: Concrete saver shared by the native and Planning Graphs.
        thread_id: Canonical AG-UI and checkpoint thread identifier.

    Returns:
        The exact head config and owning Graph role, or ``None`` for a new thread.

    Raises:
        TinkerFinLifecycleError: If the saver or private lineage marker is invalid.
    """

    if not isinstance(checkpointer, BaseCheckpointSaver):
        raise TinkerFinLifecycleError(
            "AG-UI resume routing requires a concrete BaseCheckpointSaver"
        )
    checkpoint = await _thread_head(
        cast(_CheckpointSaver, checkpointer),
        thread_id=thread_id,
    )
    if checkpoint is None:
        return None
    _require_runtime_profile(checkpoint, expected=runtime_profile)
    marker = _checkpoint_lineage(checkpoint)
    if marker is None:
        raise TinkerFinLifecycleError("AG-UI thread head has no private lineage marker")
    if marker.thread_id != thread_id:
        raise TinkerFinLifecycleError(
            "AG-UI thread head lineage belongs to a different thread"
        )
    return AgUiThreadHead(config=checkpoint.config, role=marker.role)


def _committed_resume_marker_for_identity(
    checkpoint: CheckpointTuple,
    *,
    identity: RunIdentity,
) -> ResumeIntent | None:
    """Return one saver-owned committed intent for a semantic Run."""

    raw_marker = checkpoint.metadata.get(RESUME_METADATA_KEY)
    if raw_marker is None:
        return None
    try:
        if not isinstance(raw_marker, str):
            raise TypeError("resume metadata must be canonical JSON")
        marker = ResumeIntent.model_validate_json(raw_marker)
        if raw_marker != marker.canonical_json():
            raise ValueError("resume metadata is not canonical")
    except (TypeError, ValueError) as error:
        raise TinkerFinLifecycleError(
            "checkpoint contains invalid resume intent metadata", cause=error
        ) from error
    if marker.thread != identity.thread or marker.run_id != identity.run_id:
        return None
    return marker


def _resume_marker_for_identity(
    checkpoint: CheckpointTuple,
    *,
    identity: RunIdentity,
    runtime_profile: DeepAgentsRuntimeProfile,
) -> ResumeIntent | None:
    """Resolve one intent from committed metadata and Profile-owned pending writes."""

    from .agui_resume import parse_resume_marker

    raw_values: list[object] = []
    committed = _committed_resume_marker_for_identity(
        checkpoint,
        identity=identity,
    )
    if committed is not None:
        raw_values.append(committed)
    raw_values.extend(
        runtime_profile.pending_resume_values(
            checkpoint,
            channel_name=RESUME_METADATA_KEY,
        )
    )
    matches: list[ResumeIntent] = []
    for raw_value in raw_values:
        marker = parse_resume_marker(raw_value)
        if marker is None:
            raise TinkerFinLifecycleError(
                "checkpoint contains an invalid pending resume marker"
            )
        if marker.thread != identity.thread or marker.run_id != identity.run_id:
            continue
        if marker not in matches:
            matches.append(marker)
    if len(matches) > 1:
        raise TinkerFinLifecycleError(
            "checkpoint contains conflicting durable markers for one runId",
            context={"run_id": identity.run_id},
        )
    return matches[0] if matches else None


def _validate_resume_source(
    source: CheckpointTuple,
    *,
    marker: ResumeIntent,
    identity: RunIdentity,
    runtime_profile: DeepAgentsRuntimeProfile,
) -> None:
    """Prove prepared ownership without relabelling the interrupted source Run."""

    lineage = _checkpoint_lineage(source)
    source_ns = source.config.get("configurable", {}).get("checkpoint_ns", "")
    if (
        lineage is None
        or marker.thread != identity.thread
        or marker.run_id != identity.run_id
        or marker.runtime_profile != runtime_profile.profile_id
        or lineage.runtime_profile != runtime_profile.profile_id
        or marker.parent_run_id != lineage.run_id
        or marker.source_checkpoint_id != _checkpoint_id(source)
        or marker.source_checkpoint_ns != source_ns
        or marker.role != lineage.role
    ):
        raise TinkerFinLifecycleError(
            "durable resume intent has conflicting source ownership",
            context={"run_id": identity.run_id},
        )


async def _checkpoint_parent(
    checkpointer: _CheckpointSaver,
    checkpoint: CheckpointTuple,
) -> CheckpointTuple:
    """Load the exact durable parent required by resume-intent ancestry."""

    parent_config = checkpoint.parent_config
    if parent_config is None:
        raise TinkerFinLifecycleError("durable resume intent has no source checkpoint")
    parent = await checkpointer.aget_tuple(parent_config)
    if parent is None:
        raise TinkerFinLifecycleError(
            "durable resume intent source checkpoint is unavailable"
        )
    return parent


async def _find_resume_intent(
    checkpointer: _CheckpointSaver,
    *,
    identity: RunIdentity,
    runtime_profile: DeepAgentsRuntimeProfile,
) -> ResumeIntent | None:
    """Find a Run's immutable batch even when only its child checkpoint advanced."""

    found: ResumeIntent | None = None
    config: RunnableConfig = {"configurable": {"thread_id": identity.thread_id}}
    async for checkpoint in checkpointer.alist(config):
        marker = _resume_marker_for_identity(
            checkpoint, identity=identity, runtime_profile=runtime_profile
        )
        if marker is None:
            continue
        if found is not None and marker != found:
            raise TinkerFinLifecycleError("runId owns conflicting approval batches")
        found = marker
    return found


async def _resume_stage_and_source(
    checkpointer: _CheckpointSaver,
    *,
    head: CheckpointTuple,
    identity: RunIdentity,
    marker: ResumeIntent,
    runtime_profile: DeepAgentsRuntimeProfile,
) -> tuple[CheckpointTuple, CheckpointTuple]:
    """Verify exact storage, dispatch, and approval coordinates independently."""

    source = await checkpointer.aget_tuple(
        {
            "configurable": {
                "thread_id": identity.thread_id,
                "checkpoint_ns": marker.source_checkpoint_ns,
                "checkpoint_id": marker.source_checkpoint_id,
            }
        }
    )
    stage = await checkpointer.aget_tuple(
        {
            "configurable": {
                "thread_id": identity.thread_id,
                "checkpoint_ns": marker.storage_checkpoint_ns,
                "checkpoint_id": marker.storage_checkpoint_id,
            }
        }
    )
    if source is None or stage is None:
        raise TinkerFinLifecycleError("durable resume source checkpoint is unavailable")
    if any(
        channel == "__error__"
        for checkpoint in (source, head)
        for _, channel, _ in checkpoint.pending_writes or ()
    ):
        raise TinkerFinLifecycleError("resume checkpoint contains failed task evidence")
    _validate_resume_source(
        source, marker=marker, identity=identity, runtime_profile=runtime_profile
    )
    if (
        _resume_marker_for_identity(
            stage, identity=identity, runtime_profile=runtime_profile
        )
        != marker
    ):
        raise TinkerFinLifecycleError("durable resume lost its pending-write source")
    for anchor in marker.anchors:
        actual = await checkpointer.aget_tuple(_anchor_config(anchor))
        if actual is None or _checkpoint_lineage(actual) != anchor.source:
            raise TinkerFinLifecycleError("approval anchor lost its source ownership")
        if any(channel == "__error__" for _, channel, _ in actual.pending_writes or ()):
            raise TinkerFinLifecycleError(
                "resume checkpoint contains failed task evidence"
            )
    return stage, source


async def _missing_resume_groups(
    checkpointer: _CheckpointSaver, marker: ResumeIntent
) -> frozenset[str]:
    """Verify the exact original decision and its consuming invocation per task."""

    from ._resume_receipt import (
        resume_prefix_digest,
        resume_receipts,
        task_resume_values,
    )

    decisions: object = json.loads(marker.decisions_json)
    if not isinstance(decisions, dict):
        raise TinkerFinLifecycleError("approval decisions are not a JSON object")
    expected = cast(dict[str, object], decisions)
    if set(expected) != set(marker.native_interrupt_ids):
        raise TinkerFinLifecycleError(
            "approval decisions do not cover the stored batch"
        )
    missing: set[str] = set()
    for anchor in marker.anchors:
        checkpoint = await checkpointer.aget_tuple(_anchor_config(anchor))
        if checkpoint is None:
            raise TinkerFinLifecycleError("approval source checkpoint is unavailable")
        values = task_resume_values(checkpoint, anchor.task_id)
        if not values:
            missing.add(anchor.interrupt_id)
            continue
        receipt = resume_receipts(checkpoint, anchor.task_id, thread=marker.thread).get(
            0
        )
        digest = resume_prefix_digest([expected[anchor.interrupt_id]])
        if digest is None or resume_prefix_digest(values[:1]) != digest:
            raise TinkerFinLifecycleError("approval task consumed a different decision")
        if (
            receipt is None
            or receipt.prefix_digest != digest
            or receipt.owner is None
            or receipt.owner.thread != marker.thread
            or receipt.owner.run_id != marker.run_id
            or receipt.owner.runtime_profile != marker.runtime_profile
            or receipt.intent_digest != marker.digest
        ):
            raise TinkerFinLifecycleError(
                "approval task was consumed outside this resume intent"
            )
    return frozenset(missing)


async def _reject_inherited_scalar_resume(
    checkpointer: _CheckpointSaver, marker: ResumeIntent
) -> None:
    """Prevent another Native invocation's scalar from answering an original review.

    LangGraph 1.2.10 _scratchpad.get_null_resume inherits ancestor scalar writes.
    AG-UI sends only interrupt-ID maps, so it never owns these global values.
    Inspect both original sources and active ancestor scopes before continuation.
    """

    locations: set[tuple[str, str | None]] = {
        (marker.source_checkpoint_ns, marker.source_checkpoint_id),
        ("", None),
    }
    for anchor in marker.anchors:
        locations.add((anchor.graph_namespace, anchor.checkpoint_id))
        scopes = anchor.graph_namespace.split("|")
        locations.update(
            ("|".join(scopes[:index]), None) for index in range(1, len(scopes))
        )
    for scope, checkpoint_id in locations:
        config: RunnableConfig = {
            "configurable": {"thread_id": marker.thread_id, "checkpoint_ns": scope}
        }
        if checkpoint_id is not None:
            config["configurable"]["checkpoint_id"] = checkpoint_id
        checkpoint = await checkpointer.aget_tuple(config)
        if checkpoint is not None and any(
            task_id == "00000000-0000-0000-0000-000000000000"
            and channel == "__resume__"
            for task_id, channel, _ in checkpoint.pending_writes or ()
        ):
            raise TinkerFinLifecycleError(
                "resume source contains another invocation's global decision"
            )


async def _validate_resume_head(
    checkpointer: _CheckpointSaver,
    head: CheckpointTuple,
    *,
    source: CheckpointTuple,
    marker: ResumeIntent,
) -> None:
    """Permit current-frame continuation only along this request's dispatch ancestry.

    Passing an explicit checkpoint ID to LangGraph 1.2.10 requests replay and can
    execute already-completed siblings. Validate the current head, then invoke
    without that ID. Historical branching remains a separate ordinary-run feature.
    """

    current = head
    visited: set[tuple[str, str, str]] = set()
    target = _checkpoint_ancestry_key(source)
    for _ in range(_MAX_RESUME_ANCESTRY_DEPTH):
        key = _checkpoint_ancestry_key(current)
        if key == target:
            return
        if key in visited:
            raise TinkerFinLifecycleError(
                "durable resume ancestry contains a checkpoint cycle"
            )
        visited.add(key)
        lineage = _checkpoint_lineage(current)
        if (
            lineage is None
            or lineage.thread != marker.thread
            or lineage.run_id != marker.run_id
            or lineage.runtime_profile != marker.runtime_profile
        ):
            raise TinkerFinLifecycleError("resume current head belongs to another Run")
        if _committed_resume_marker_for_identity(current, identity=marker) != marker:
            raise TinkerFinLifecycleError(
                "resume current head no longer belongs to the original resume intent"
            )
        current = await _checkpoint_parent(checkpointer, current)
    raise TinkerFinLifecycleError(
        "durable resume ancestry exceeds the safe checkpoint depth"
    )


async def _durable_resume_progress(
    checkpointer: _CheckpointSaver,
    *,
    identity: RunIdentity,
    parent_run_id: str | None,
    resume: AgUiResumeBinding,
    runtime_profile: DeepAgentsRuntimeProfile,
) -> _DurableResumeProgress | None:
    """Recover one original batch and each task's durable consumption progress."""

    marker = await _find_resume_intent(
        checkpointer, identity=identity, runtime_profile=runtime_profile
    )
    if marker is None:
        return None
    if parent_run_id is not None and marker.parent_run_id != parent_run_id:
        raise TinkerFinLifecycleError(
            "durable resume marker belongs to another parentRunId"
        )
    expected = resume._marker(
        identity=identity,
        parent_run_id=marker.parent_run_id,
        runtime_profile=runtime_profile.profile_id,
        role=marker.role,
        source_checkpoint_id=marker.source_checkpoint_id,
        source_checkpoint_ns=marker.source_checkpoint_ns,
        anchors=marker.anchors,
        storage_checkpoint_id=marker.storage_checkpoint_id,
        storage_checkpoint_ns=marker.storage_checkpoint_ns,
    )
    if marker != expected:
        raise TinkerFinLifecycleError(
            "runId already owns a different durable resume marker"
        )
    head = await _required_thread_head(checkpointer, identity)
    stage, source = await _resume_stage_and_source(
        checkpointer,
        head=head,
        identity=identity,
        marker=marker,
        runtime_profile=runtime_profile,
    )
    await _validate_resume_head(checkpointer, head, source=source, marker=marker)
    await _reject_inherited_scalar_resume(checkpointer, marker)
    missing = await _missing_resume_groups(checkpointer, marker)
    return _DurableResumeProgress(
        head=head,
        stage=stage,
        source=source,
        marker=marker,
        phase="prepared" if missing else "accepted",
        missing_interrupt_ids=missing,
    )


async def agui_resume_marker_is_durable(
    checkpointer: object,
    *,
    identity: RunIdentity,
    runtime_profile: DeepAgentsRuntimeProfile,
) -> bool:
    """Prove whether one Run already owns a prepared or accepted resume marker.

    This probe intentionally reads the borrowed saver without constructing a Graph. It
    protects host claims when an async model, Sandbox, Definition, or Graph factory fails
    during retry. A marker whose source, Profile, or complete identity cannot be proven fails closed
    instead of being interpreted as no durable intent.

    Args:
        checkpointer: Borrowed saver selected before Definition construction.
        identity: Exact semantic resume Run identity to inspect.
        runtime_profile: Profile that owns pending-write and accepted-decision semantics.

    Returns:
        ``True`` for a prepared or accepted marker and ``False`` when no marker exists.

    Raises:
        TinkerFinLifecycleError: The saver, marker, Profile, or source ownership cannot be
            validated safely.
    """

    if not isinstance(checkpointer, BaseCheckpointSaver):
        raise TinkerFinLifecycleError(
            "AG-UI resume marker probing requires a concrete BaseCheckpointSaver"
        )
    saver = cast(_CheckpointSaver, checkpointer)
    head = await _thread_head(saver, thread_id=identity.thread_id)
    if head is None:
        return False
    marker = await _find_resume_intent(
        saver, identity=identity, runtime_profile=runtime_profile
    )
    if marker is None:
        return False
    _require_runtime_profile(head, expected=runtime_profile.profile_id)
    _stage, source = await _resume_stage_and_source(
        saver,
        head=head,
        identity=identity,
        marker=marker,
        runtime_profile=runtime_profile,
    )
    _validate_resume_source(
        source, marker=marker, identity=identity, runtime_profile=runtime_profile
    )
    return True


def _require_checkpointer(astream: Callable[..., object]) -> _CheckpointSaver:
    owner = getattr(astream, "__self__", None)
    checkpointer = getattr(owner, "checkpointer", None)
    if not isinstance(checkpointer, BaseCheckpointSaver):
        raise TinkerFinLifecycleError(
            "AG-UI branching and resume require a concrete BaseCheckpointSaver"
        )
    return cast(_CheckpointSaver, checkpointer)


async def _validate_branch_source(
    astream: Callable[..., object],
    checkpoint: CheckpointTuple,
    *,
    parent_run_id: str | None,
    runtime_profile: str,
    resume_interrupt_ids: frozenset[str] | None,
) -> None:
    """Prove a checkpoint is safe for the requested branch or exact resume.

    Runtime Profile ownership is checked before state inspection or Graph continuation.
    A resume must address exactly the pending interrupt set, while an ordinary branch
    requires a completed, non-interrupted snapshot with no failed task evidence.
    """

    _require_runtime_profile(checkpoint, expected=runtime_profile)
    snapshot = await _read_snapshot(astream, checkpoint)
    task_errors = tuple(task.error for task in snapshot.tasks if task.error is not None)
    interrupts = _interrupt_ids(snapshot)
    context = {} if parent_run_id is None else {"parent_run_id": parent_run_id}
    if task_errors:
        raise TinkerFinLifecycleError(
            "a failed checkpoint cannot be used as a branch or resume source",
            context=context,
            diagnostic_context={"failed_task_count": len(task_errors)},
        )
    if resume_interrupt_ids is not None:
        if interrupts != resume_interrupt_ids:
            raise TinkerFinLifecycleError(
                "a resume requires the source checkpoint's exact interrupts",
                context=context,
            )
        return
    if interrupts:
        raise TinkerFinLifecycleError(
            "an interrupted parent requires a resume for its exact interrupts",
            context=context,
        )
    if snapshot.next:
        raise TinkerFinLifecycleError(
            "an active parent run cannot be used as a branch source",
            context=context,
        )


async def bind_agui_lineage(
    astream: Callable[..., object],
    config: RunnableConfig,
    *,
    identity: RunIdentity,
    parent_run_id: str | None,
    runtime_profile: DeepAgentsRuntimeProfile,
    resume: AgUiResumeBinding | None,
) -> AgUiLineageResolution:
    """Resolve canonical branch lineage and durable resume progress.

    A parent must identify the unique completed root checkpoint leaf for an ordinary
    branch. An interrupted parent is valid only when the request carries a resume
    binding for the exact native interrupt set. A durable marker distinguishes a
    prepared intent from a continuation whose decision was already submitted.

    Args:
        astream: Bound graph stream whose owner exposes the saver and state reader.
        config: Canonical execution-thread configuration for this invocation.
        identity: Canonical public, Graph, checkpoint, and delivery identity.
        parent_run_id: Optional branch or resume source in the same thread.
        runtime_profile: Profile that must own every selected checkpoint.
        resume: Validated native resume binding, or ``None`` for an ordinary run.

    Returns:
        The selected checkpoint, effective parent lineage, and resume phase.

    Raises:
        TinkerFinLifecycleError: The parent is missing, ambiguous, active, failed,
            interrupted without a matching resume, or cannot be inspected safely.
    """

    updated = cast(RunnableConfig, dict(config))
    configurable = dict(updated.get("configurable", {}))
    if configurable.get("checkpoint_ns") not in (None, ""):
        raise TinkerFinLifecycleError(
            "AG-UI main runs require the root checkpoint scope"
        )
    if configurable.get("checkpoint_id") is not None:
        raise TinkerFinLifecycleError(
            "AG-UI checkpoint selection is owned by parentRunId"
        )
    configurable[RUN_ID_METADATA_KEY] = identity.run_id
    if configurable.get(NAMESPACE_METADATA_KEY) not in (None, identity.namespace):
        raise TinkerFinLifecycleError("Graph config belongs to another namespace")
    configurable[NAMESPACE_METADATA_KEY] = identity.namespace
    profile_id = runtime_profile.profile_id
    configured_profile = configurable.get(RUNTIME_PROFILE_METADATA_KEY)
    if configured_profile not in (None, profile_id):
        raise TinkerFinLifecycleError("Graph config belongs to another Runtime Profile")
    configurable[RUNTIME_PROFILE_METADATA_KEY] = profile_id
    # RedisSaver 0.5.2 indexes only its standard configurable run_id. Keep the
    # private metadata as the cross-saver authority and verify every listed result.
    configurable[_CHECKPOINTER_RUN_ID_KEY] = identity.run_id
    configurable[CHECKPOINT_ROLE_METADATA_KEY] = NATIVE_CHECKPOINT_ROLE
    configurable["checkpoint_ns"] = ""
    if parent_run_id is None and resume is None:
        updated["configurable"] = configurable
        updated = bind_checkpoint_run(
            updated, identity=identity, parent_run_id=None, runtime_profile=profile_id
        )
        return AgUiLineageResolution(
            updated,
            resume_phase="none",
            parent_run_id=None,
            checkpoint_role=NATIVE_CHECKPOINT_ROLE,
        )

    checkpointer = _require_checkpointer(astream)
    if resume is not None:
        progress = await _durable_resume_progress(
            checkpointer,
            identity=identity,
            parent_run_id=parent_run_id,
            resume=resume,
            runtime_profile=runtime_profile,
        )
        if progress is not None:
            _require_runtime_profile(progress.head, expected=profile_id)
            effective_parent = progress.marker.parent_run_id
            if effective_parent is not None:
                configurable[PARENT_RUN_ID_METADATA_KEY] = effective_parent
            updated["configurable"] = configurable
            updated = bind_checkpoint_run(
                updated,
                identity=identity,
                parent_run_id=effective_parent,
                runtime_profile=profile_id,
            )
            updated["configurable"] = {
                **updated.get("configurable", {}),
                RESUME_CONFIG_KEY: progress.marker,
            }
            return AgUiLineageResolution(
                updated,
                resume_phase=progress.phase,
                parent_run_id=effective_parent,
                checkpoint_role=cast(
                    LineageRole,
                    _checkpoint_role(progress.head),
                ),
                missing_interrupt_ids=progress.missing_interrupt_ids,
            )

    if parent_run_id is None:
        source = await _thread_head(
            checkpointer,
            thread_id=identity.thread_id,
        )
        if source is None:
            raise TinkerFinLifecycleError(
                "resume has no checkpoint in this canonical thread"
            )
    else:
        source = await _run_head(
            checkpointer,
            thread_id=identity.thread_id,
            run_id=parent_run_id,
        )
    await _validate_branch_source(
        astream,
        source,
        parent_run_id=parent_run_id,
        runtime_profile=profile_id,
        resume_interrupt_ids=(
            None if resume is None else frozenset(resume.native_interrupt_ids)
        ),
    )
    if resume is not None and not resume.native_interrupt_ids:
        raise TinkerFinLifecycleError(
            "resume binding contains no native interrupt identities"
        )

    if resume is not None:
        source_lineage = _checkpoint_lineage(source)
        if source_lineage is None:
            raise TinkerFinLifecycleError("resume source has no private lineage marker")
        effective_parent = source_lineage.run_id
        if parent_run_id is not None and effective_parent != parent_run_id:
            raise TinkerFinLifecycleError(
                "resume source conflicts with parentRunId",
                context={"parent_run_id": parent_run_id},
            )
        if effective_parent == identity.run_id:
            raise TinkerFinLifecycleError(
                "resume runId cannot own its interrupted source"
            )
        current = await _required_thread_head(checkpointer, identity)
        if _checkpoint_ancestry_key(current) != _checkpoint_ancestry_key(source):
            raise TinkerFinLifecycleError(
                "historical approval source is not the current dispatch checkpoint"
            )
        configurable["checkpoint_id"] = _checkpoint_id(source)
        configurable[PARENT_RUN_ID_METADATA_KEY] = effective_parent
        phase: Literal["none", "unstaged"] = "unstaged"
    else:
        effective_parent = parent_run_id
        phase = "none"
        if parent_run_id is not None:
            configurable["checkpoint_id"] = _checkpoint_id(source)
            configurable[PARENT_RUN_ID_METADATA_KEY] = parent_run_id
    updated["configurable"] = configurable
    updated = bind_checkpoint_run(
        updated,
        identity=identity,
        parent_run_id=effective_parent,
        runtime_profile=profile_id,
    )
    return AgUiLineageResolution(
        updated,
        resume_phase=phase,
        parent_run_id=effective_parent,
        checkpoint_role=cast(LineageRole, _checkpoint_role(source)),
        missing_interrupt_ids=(
            frozenset() if resume is None else frozenset(resume.native_interrupt_ids)
        ),
    )


async def stage_agui_resume_intent(
    astream: Callable[..., object],
    resolution: AgUiLineageResolution,
    *,
    identity: RunIdentity,
    runtime_profile: DeepAgentsRuntimeProfile,
    resume: AgUiResumeBinding,
) -> AgUiLineageResolution:
    """Commit and verify a private resume intent before decision submission.

    The selected Profile writes a private pending record through the borrowed saver without
    creating a Graph checkpoint or executing a node. This preserves root, Planning, and
    nested subgraph interrupt control while establishing a durable callback boundary.

    Args:
        astream: Profile-bound Graph stream exposing the interrupted saver.
        resolution: Verified unstaged lineage and exact source configuration.
        identity: Resume Run identity bound to the private intent.
        runtime_profile: Profile owning saver-specific private-write semantics.
        resume: Validated decisions used to derive the private marker.

    Returns:
        Resolution whose marker is proven saver-readable in the prepared phase.

    Raises:
        asyncio.CancelledError: Caller cancellation after the retained write settles.
        TinkerFinLifecycleError: The source, private writes, or durable evidence is
            missing, conflicting, or owned by another Profile.
    """

    if resolution.resume_phase != "unstaged":
        raise TinkerFinLifecycleError(
            "resume intent can be staged only from an interrupted source"
        )
    checkpointer = _require_checkpointer(astream)
    source = await checkpointer.aget_tuple(resolution.config)
    if source is None:
        raise TinkerFinLifecycleError("resume source checkpoint is unavailable")
    context = await _resume_context(
        await _read_snapshot(astream, source),
        checkpointer=checkpointer,
        source=source,
        runtime_profile=runtime_profile,
    )
    anchors = await _approval_anchors(context, checkpointer)
    if tuple(anchor.interrupt_id for anchor in anchors) != resume.native_interrupt_ids:
        raise TinkerFinLifecycleError("approval batch changed before preparation")
    available: list[ResumeAnchor] = []
    for anchor in anchors:
        actual = await checkpointer.aget_tuple(_anchor_config(anchor))
        if actual is None:
            raise TinkerFinLifecycleError("approval source checkpoint is unavailable")
        occupied = runtime_profile.pending_resume_values(
            actual, channel_name=RESUME_METADATA_KEY
        )
        for raw in occupied:
            previous = ResumeIntent.model_validate(raw)
            if await _missing_resume_groups(checkpointer, previous):
                raise TinkerFinLifecycleError(
                    "approval source belongs to another unfinished resume intent"
                )
        if any(
            task_id == anchor.task_id and channel == "__resume__"
            for task_id, channel, _ in actual.pending_writes or ()
        ):
            raise TinkerFinLifecycleError(
                "another review round requires a new checkpoint before AG-UI resume"
            )
        if not occupied:
            available.append(anchor)
    if not available:
        raise TinkerFinLifecycleError(
            "approval batch has no fresh checkpoint for durable preparation"
        )
    storage = min(
        available,
        key=lambda anchor: (
            anchor.graph_namespace,
            anchor.checkpoint_id,
            anchor.task_id,
        ),
    )
    marker = resume._marker(
        identity=identity,
        parent_run_id=resolution.parent_run_id,
        runtime_profile=runtime_profile.profile_id,
        role=resolution.checkpoint_role,
        source_checkpoint_id=_checkpoint_id(source),
        source_checkpoint_ns=source.config.get("configurable", {}).get(
            "checkpoint_ns", ""
        ),
        anchors=anchors,
        storage_checkpoint_id=storage.checkpoint_id,
        storage_checkpoint_ns=storage.graph_namespace,
    )
    _validate_resume_source(
        source, marker=marker, identity=identity, runtime_profile=runtime_profile
    )
    await _reject_inherited_scalar_resume(checkpointer, marker)
    writes = ((RESUME_METADATA_KEY, marker.model_dump(mode="json", by_alias=True)),)

    # The saver operation settles before cancellation can classify durable intent.
    try:
        await run_async_owned(
            lambda: runtime_profile.stage_resume_intent(
                checkpointer,
                _anchor_config(storage),
                writes,
            ),
            task_name="tinkerfin-resume-intent-stage",
        )
    except Exception as stage_error:
        raise TinkerFinLifecycleError(
            "Runtime Profile could not durably stage the resume intent",
            cause=stage_error,
        ) from stage_error
    progress = await _durable_resume_progress(
        checkpointer,
        identity=identity,
        parent_run_id=resolution.parent_run_id,
        resume=resume,
        runtime_profile=runtime_profile,
    )
    if progress is None or progress.phase != "prepared":
        raise TinkerFinLifecycleError(
            "resume intent was not durably prepared before decision submission"
        )
    if _checkpoint_id(progress.stage) != storage.checkpoint_id:
        raise TinkerFinLifecycleError(
            "Profile staged the resume intent on another checkpoint"
        )
    _require_runtime_profile(
        progress.stage,
        expected=runtime_profile.profile_id,
    )
    prepared_config: RunnableConfig = {
        **resolution.config,
        "configurable": {
            **resolution.config.get("configurable", {}),
            RESUME_CONFIG_KEY: marker,
        },
    }
    prepared_config["configurable"].pop("checkpoint_id", None)
    return AgUiLineageResolution(
        prepared_config,
        resume_phase="prepared",
        parent_run_id=progress.marker.parent_run_id,
        checkpoint_role=resolution.checkpoint_role,
        missing_interrupt_ids=progress.missing_interrupt_ids,
    )


__all__ = [
    "CHECKPOINT_ROLE_METADATA_KEY",
    "NATIVE_CHECKPOINT_ROLE",
    "PARENT_RUN_ID_METADATA_KEY",
    "PLANNING_CHECKPOINT_ROLE",
    "RUNTIME_PROFILE_METADATA_KEY",
    "RUN_ID_METADATA_KEY",
    "AgUiLineageResolution",
    "AgUiResumeContext",
    "AgUiThreadHead",
    "agui_resume_marker_is_durable",
    "bind_agui_lineage",
    "resolve_agui_native_run_head",
    "resolve_agui_resume_context",
    "resolve_agui_thread_head",
    "stage_agui_resume_intent",
]

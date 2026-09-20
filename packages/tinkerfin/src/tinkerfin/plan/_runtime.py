"""Request-local routing between standalone Planning and native Deep Agent graphs."""

from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import aclosing
from typing import Any, Protocol, TypeAlias, cast

from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import START
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command, StateSnapshot

from .._agui_lineage import (
    NATIVE_CHECKPOINT_ROLE,
    PLANNING_CHECKPOINT_ROLE,
    RUN_ID_METADATA_KEY,
    RUNTIME_PROFILE_METADATA_KEY,
    resolve_agui_native_run_head,
    resolve_agui_thread_head,
)
from .._agui_lineage_state import (
    LINEAGE_CONFIG_KEY,
    RESUME_CONFIG_KEY,
    LineageMarker,
    ResumeIntent,
)
from .._tasks import join_task
from ._config import PlanOptions
from ._content import PlanContentBinding
from ._handoff import (
    PLAN_HANDOFF_STATE_KEY,
    PlanHandoffStream,
)
from ._resume import validate_plan_resume_response
from ._state import (
    PLAN_CHECKPOINT_RUN_ID,
    PLAN_STATE_KEY,
    read_plan_state,
)
from ._workflow import PlanningWorkflowGraph
from .errors import PlanModeConfigurationError, PlanStateConflictError
from .models import (
    MarkdownPlanContent,
    PlanContentModel,
    PlanHandoffPhase,
    PlanState,
    PlanStatus,
)

_APPROVED_PLAN_MARKER = "<tinkerfin-approved-plan"
_CheckpointSaver: TypeAlias = (
    BaseCheckpointSaver[int] | BaseCheckpointSaver[float] | BaseCheckpointSaver[str]
)


class _GraphRuntime(Protocol):
    @property
    def checkpointer(self) -> object: ...

    def astream(
        self,
        *args: object,
        **kwargs: object,
    ) -> AsyncIterator[Mapping[str, object]]: ...

    async def aget_state(
        self,
        config: RunnableConfig,
        *,
        subgraphs: bool = False,
    ) -> StateSnapshot: ...

    async def aupdate_state(
        self,
        config: RunnableConfig,
        values: Mapping[str, object],
        as_node: str | None = None,
        task_id: str | None = None,
    ) -> RunnableConfig: ...


class _SignatureCallable(Protocol):
    def __call__(self, *args: object, **kwargs: object) -> object: ...


_COMPILED_ASTREAM = cast(
    _SignatureCallable,
    CompiledStateGraph.astream,  # pyright: ignore[reportUnknownMemberType,reportUnknownArgumentType]
)


def _config(bound: inspect.BoundArguments) -> RunnableConfig:
    value = bound.arguments.get("config")
    if value is None:
        return RunnableConfig()
    if not isinstance(value, Mapping):
        raise TypeError("config must be a mapping or None")
    return cast(RunnableConfig, dict(cast(Mapping[str, object], value)))


def _select_checkpoint(
    config: RunnableConfig,
    checkpoint_id: str,
) -> RunnableConfig:
    selected = cast(RunnableConfig, dict(config))
    configurable = dict(selected.get("configurable", {}))
    configurable["checkpoint_id"] = checkpoint_id
    selected["configurable"] = configurable
    return selected


def _native_checkpoint_id_for_plan(
    plan: PlanState[PlanContentModel] | None,
) -> str | None:
    handoff = None if plan is None else plan.handoff
    return (
        None
        if handoff is None
        else handoff.completed_checkpoint_id or handoff.native_checkpoint_id
    )


async def _native_snapshot_for_plan(
    native: _GraphRuntime,
    config: RunnableConfig,
    plan: PlanState[PlanContentModel] | None,
) -> tuple[StateSnapshot, RunnableConfig]:
    checkpoint_id = _native_checkpoint_id_for_plan(plan)
    selected = (
        config
        if checkpoint_id is None
        else _select_checkpoint(
            config,
            checkpoint_id,
        )
    )
    return await native.aget_state(selected), selected


async def _plan_checkpoint_channels(
    checkpointer: object,
    config: RunnableConfig,
) -> dict[str, object]:
    """Read the canonical Planning head independently of native checkpoint selection."""

    if not isinstance(checkpointer, BaseCheckpointSaver):
        return {}
    saver = cast(_CheckpointSaver, checkpointer)
    planning_config = cast(RunnableConfig, dict(config))
    configurable = dict(planning_config.get("configurable", {}))
    configurable.pop("checkpoint_id", None)
    planning_config["configurable"] = configurable
    async for checkpoint in saver.alist(
        planning_config,
        filter={"run_id": PLAN_CHECKPOINT_RUN_ID},
        limit=1,
    ):
        channels = checkpoint.checkpoint.get("channel_values")
        if not isinstance(channels, Mapping):
            return {}
        return dict(cast(Mapping[str, object], channels))
    return {}


async def _native_plan_overlay(
    native: _GraphRuntime,
    config: RunnableConfig,
    content: PlanContentBinding,
) -> PlanState[PlanContentModel] | None:
    state = await _plan_checkpoint_channels(native.checkpointer, config)
    if PLAN_STATE_KEY not in state:
        return None
    plan = read_plan_state(state, content)
    if plan.status is PlanStatus.APPROVED:
        handoff = plan.handoff
        if handoff is None or handoff.phase is PlanHandoffPhase.PENDING:
            raise PlanStateConflictError(
                "approved Plan has no native checkpoint evidence"
            )
        return plan
    return plan if plan.status is PlanStatus.CANCELLED else None


def _handoff_text(
    plan: PlanState[PlanContentModel],
    content_binding: PlanContentBinding,
) -> str:
    """Render the deterministic instruction used only for native model calls.

    The private state digest, rather than this prompt text, proves durable acceptance.
    Content is rendered according to the frozen media type and explicitly preserves
    later Tool-specific review instead of treating Plan approval as blanket execution
    permission.
    """

    confirmed = plan.confirmed_plan
    handoff = plan.handoff
    if confirmed is None or handoff is None:
        raise RuntimeError("approved Plan state requires a confirmed handoff")
    if confirmed.content_schema != content_binding.reference:
        raise RuntimeError("approved Plan content schema does not match the Definition")
    content = confirmed.content
    if confirmed.content_schema.media_type == "text/markdown":
        if not isinstance(content, MarkdownPlanContent):
            raise RuntimeError("Markdown Plan handoff requires MarkdownPlanContent")
        rendered = content.markdown
    else:
        rendered = json.dumps(
            content.model_dump(mode="json", by_alias=True, exclude_none=False),
            ensure_ascii=False,
            indent=2,
        )
    return (
        f'{_APPROVED_PLAN_MARKER} digest="{handoff.digest}" '
        f'content-type="{confirmed.content_schema.media_type}" '
        f'revision="{confirmed.revision}">\n'
        "Plan review is complete and approval has already been granted. Begin native "
        "Deep Agent execution now. Do not restate the Plan or request general Plan "
        "approval again. Tool-specific human review remains mandatory. Treat this "
        "approved Plan as the governing task contract and do not silently change its "
        "content.\n\n" + rendered + "\n</tinkerfin-approved-plan>"
    )


def _content_contains_handoff(content: object, digest: str) -> bool:
    marker = f'{_APPROVED_PLAN_MARKER} digest="{digest}"'
    if isinstance(content, str):
        return marker in content
    if isinstance(content, Sequence) and not isinstance(content, (str, bytes)):
        for block in cast(Sequence[object], content):
            if isinstance(block, str) and marker in block:
                return True
            if isinstance(block, Mapping):
                mapping = cast(Mapping[object, object], block)
                if marker in str(mapping.get("text", "")):
                    return True
    return False


def _handoff_user_message(
    state: Mapping[str, object],
    plan: PlanState[PlanContentModel],
) -> HumanMessage:
    """Copy the exact user request that the approved Plan will execute.

    The durable handoff digest lives in a private state channel. Keeping execution
    instructions out of this message preserves the caller's role, content, and ID in
    AG-UI snapshots, Trace, later conversation turns, and checkpoint replay.
    """

    handoff = plan.handoff
    if handoff is None:
        raise RuntimeError("approved Plan state requires handoff metadata")
    raw_messages = state.get("messages")
    if not isinstance(raw_messages, Sequence) or isinstance(raw_messages, (str, bytes)):
        raise TypeError("Plan handoff requires checkpoint messages")
    matches = [
        message
        for message in cast(Sequence[object], raw_messages)
        if isinstance(message, HumanMessage) and message.id == handoff.message_id
    ]
    if len(matches) != 1:
        raise RuntimeError("Plan handoff message is missing or duplicated")
    message = matches[0]
    if _content_contains_handoff(message.content, handoff.digest):
        raise PlanStateConflictError(
            "Planning checkpoint contains a public handoff marker"
        )
    return message.model_copy(deep=True)


def _overlay_plan(
    part: Mapping[str, object],
    plan: PlanState[PlanContentModel],
) -> Mapping[str, object]:
    if part.get("type") != "values" or part.get("ns") != ():
        return part
    data = part.get("data")
    if not isinstance(data, Mapping):
        raise TypeError("root values data must be a mapping")
    updated = dict(part)
    updated["data"] = {
        **cast(Mapping[str, object], data),
        PLAN_STATE_KEY: plan.model_dump(
            mode="json",
            by_alias=True,
            exclude_none=False,
        ),
    }
    return updated


def _checkpoint_id(snapshot: StateSnapshot) -> str:
    value = snapshot.config.get("configurable", {}).get("checkpoint_id")
    if not isinstance(value, str) or not value:
        raise PlanStateConflictError("native checkpoint has no stable checkpoint_id")
    return value


def _snapshot_handoff_message(
    snapshot: StateSnapshot,
    plan: PlanState[PlanContentModel],
) -> HumanMessage | None:
    """Verify whether a native checkpoint durably staged the exact handoff.

    The private digest proves acceptance without changing a public message. The same
    checkpoint must contain the original request ID exactly once and must never contain
    the legacy public marker shape.
    """

    handoff = plan.handoff
    if handoff is None:
        raise PlanStateConflictError("approved Plan has no handoff metadata")
    marker = snapshot.values.get(PLAN_HANDOFF_STATE_KEY)
    if marker is None:
        return None
    if not isinstance(marker, str) or marker != handoff.digest:
        raise PlanStateConflictError("native checkpoint handoff digest conflicts")
    raw_messages = snapshot.values.get("messages")
    if raw_messages is None:
        raise PlanStateConflictError("native checkpoint handoff has no user request")
    if not isinstance(raw_messages, Sequence) or isinstance(raw_messages, (str, bytes)):
        raise PlanStateConflictError("native checkpoint messages are not a sequence")
    matches = [
        message
        for message in cast(Sequence[object], raw_messages)
        if isinstance(message, HumanMessage) and message.id == handoff.message_id
    ]
    if not matches:
        raise PlanStateConflictError("native checkpoint handoff lost its user request")
    if len(matches) != 1:
        raise PlanStateConflictError("native checkpoint duplicates the Plan handoff")
    message = matches[0]
    if _content_contains_handoff(message.content, handoff.digest):
        raise PlanStateConflictError("native checkpoint exposes its private handoff")
    return message


def _is_resume_command(value: object) -> bool:
    return (
        isinstance(value, Command) and cast(Command[object], value).resume is not None
    )


def _plan_values_part(
    plan: PlanState[PlanContentModel],
    *,
    native_values: Mapping[str, object] | None = None,
) -> Mapping[str, object]:
    return {
        "type": "values",
        "ns": (),
        "data": {
            **({} if native_values is None else native_values),
            PLAN_STATE_KEY: plan.model_dump(
                mode="json",
                by_alias=True,
                exclude_none=False,
            ),
        },
        "interrupts": (),
    }


class PlanCapableGraphRuntime:
    """Route one request without wrapping or modifying the native Deep Agent graph.

    Ordinary default inputs and every Tool resume delegate directly to the native
    graph. Plan inputs and Plan resumes use the standalone Planning graph. An approved
    Planning run is synchronously handed to the same native graph in this request.
    The wrapper owns no external resource; both graphs borrow the Definition's saver,
    Store, cache, backend, and context.
    """

    __slots__ = (
        "_content",
        "_native",
        "_options",
        "_planning_factory",
        "_planning_graph_value",
        "_planning_task",
        "_prefer_plan",
        "_signature",
    )

    def __init__(
        self,
        *,
        options: PlanOptions,
        native: _GraphRuntime,
        planning_factory: Callable[[], Awaitable[PlanningWorkflowGraph[Any]]],
        prefer_plan: bool,
    ) -> None:
        """Bind borrowed native and lazily created Planning graphs to one router."""

        self._options = options
        self._content = options.content
        self._native = native
        self._planning_factory = planning_factory
        self._planning_graph_value: PlanningWorkflowGraph[Any] | None = None
        self._planning_task: asyncio.Task[PlanningWorkflowGraph[Any]] | None = None
        self._prefer_plan = prefer_plan
        self._signature = inspect.signature(native.astream)

    @property
    def checkpointer(self) -> object:
        """Expose the native graph checkpointer without taking ownership."""

        return self._native.checkpointer

    async def _tinkerfin_lineage_state(
        self,
        config: RunnableConfig,
        role: str,
        *,
        subgraphs: bool = False,
    ) -> StateSnapshot:
        """Read an exact native or Planning checkpoint for lineage validation."""

        if role == NATIVE_CHECKPOINT_ROLE:
            return await self._native.aget_state(config, subgraphs=subgraphs)
        if role == PLANNING_CHECKPOINT_ROLE:
            planning = await self._planning_graph()
            return await planning.aget_state(
                config,
                subgraphs=subgraphs,
            )
        raise PlanStateConflictError("checkpoint lineage has an unknown graph role")

    async def _planning_graph(self) -> PlanningWorkflowGraph[Any]:
        """Return the one lazily built Planning graph shared by this router.

        The retained task is the single construction owner. Caller cancellation waits
        for that task to settle without cancelling it, which prevents a reusable
        ``DeepAgentGraph`` from being left with an abandoned half-build. The Planning
        graph borrows the same checkpointer, Store, cache, backend, and context schema as
        the native graph and owns no closeable resource.

        Returns:
            The cached Planning graph for every invocation of this router.

        Raises:
            BaseException: The async factory fails or caller cancellation wins after the
                retained build settles.
        """

        current = self._planning_graph_value
        if current is not None:
            return current
        task = self._planning_task
        if task is None:

            async def build() -> PlanningWorkflowGraph[Any]:
                return await self._planning_factory()

            task = asyncio.create_task(
                build(),
                name="tinkerfin-planning-graph-build",
            )
            self._planning_task = task
        result = await join_task(task)
        assert result is not None
        if self._planning_graph_value is None:
            self._planning_graph_value = result
        return self._planning_graph_value

    def astream(
        self, *args: object, **kwargs: object
    ) -> AsyncIterator[Mapping[str, object]]:
        """Return the single request stream selected by input and pending origin."""

        return self._stream(*args, **kwargs)

    async def _stream(
        self,
        *args: object,
        **kwargs: object,
    ) -> AsyncIterator[Mapping[str, object]]:
        bound = self._signature.bind(*args, **kwargs)
        graph_input = bound.arguments.get("input")
        config = _config(bound)
        configurable = config.get("configurable", {})
        lineage_required = isinstance(
            configurable.get(LINEAGE_CONFIG_KEY), LineageMarker
        )
        runtime_profile = configurable.get(RUNTIME_PROFILE_METADATA_KEY)
        if lineage_required and (
            not isinstance(runtime_profile, str) or not runtime_profile
        ):
            raise PlanStateConflictError(
                "lineage routing requires a canonical Runtime Profile"
            )
        # The native and Planning graphs borrow one saver, so the Planning run ID is
        # authoritative before the Planning graph exists. A new default request can
        # therefore stay on the native fast path without constructing a second graph.
        checkpoint_state = await _plan_checkpoint_channels(
            self._native.checkpointer,
            config,
        )
        if (
            not self._prefer_plan
            and not _is_resume_command(graph_input)
            and PLAN_STATE_KEY not in checkpoint_state
        ):
            async for part in self._native.astream(*bound.args, **bound.kwargs):
                yield part
            return
        planning = await self._planning_graph()
        checkpoint_plan = (
            read_plan_state(checkpoint_state, self._content)
            if PLAN_STATE_KEY in checkpoint_state
            else None
        )

        use_planning = self._prefer_plan
        if graph_input is None and isinstance(
            configurable.get(RESUME_CONFIG_KEY), ResumeIntent
        ):
            # An accepted retry has no native Command. Restore its actual completed
            # Graph role so a rejected Plan still returns the Planning snapshot,
            # without making a new model call or interpreting the preferred mode.
            lineage = configurable.get(LINEAGE_CONFIG_KEY)
            if not isinstance(lineage, LineageMarker):
                raise PlanStateConflictError(
                    "resume retry requires typed run ownership"
                )
            head = await resolve_agui_thread_head(
                self.checkpointer,
                thread_id=lineage.thread_id,
                runtime_profile=lineage.runtime_profile,
            )
            if head is None:
                raise PlanStateConflictError("resume retry has no checkpoint")
            use_planning = head.role == PLANNING_CHECKPOINT_ROLE
        if _is_resume_command(graph_input):
            planning_snapshot: StateSnapshot | None = None
            if lineage_required:
                thread_id = configurable.get("thread_id")
                if not isinstance(thread_id, str):
                    raise PlanStateConflictError(
                        "resume routing requires a canonical AG-UI thread ID"
                    )
                head = await resolve_agui_thread_head(
                    self.checkpointer,
                    thread_id=thread_id,
                    runtime_profile=cast(str, runtime_profile),
                )
                if head is None:
                    raise PlanStateConflictError(
                        "resume input has no canonical thread checkpoint"
                    )
                planning_pending = False
                native_pending = False
                if head.role == PLANNING_CHECKPOINT_ROLE:
                    planning_snapshot = await planning.aget_state(head.config)
                    planning_values = cast(
                        Mapping[str, object], planning_snapshot.values
                    )
                    checkpoint_state = dict(planning_values)
                    checkpoint_plan = (
                        read_plan_state(checkpoint_state, self._content)
                        if PLAN_STATE_KEY in checkpoint_state
                        else None
                    )
                    planning_pending = bool(planning_snapshot.next)
                    if _native_checkpoint_id_for_plan(checkpoint_plan) is not None:
                        (
                            native_snapshot,
                            _native_config,
                        ) = await _native_snapshot_for_plan(
                            self._native,
                            config,
                            checkpoint_plan,
                        )
                        native_pending = bool(native_snapshot.next)
                else:
                    native_snapshot = await self._native.aget_state(head.config)
                    native_pending = bool(native_snapshot.next)
            else:
                planning_snapshot = await planning.aget_state(config)
                native_snapshot, _native_config = await _native_snapshot_for_plan(
                    self._native,
                    config,
                    checkpoint_plan,
                )
                planning_pending = bool(planning_snapshot.next)
                native_pending = bool(native_snapshot.next)
            if planning_pending and native_pending:
                raise PlanStateConflictError(
                    "Planning and native Graphs both contain pending work"
                )
            if planning_pending:
                if planning_snapshot is None or PLAN_STATE_KEY not in checkpoint_state:
                    raise PlanStateConflictError(
                        "pending Planning checkpoint has no Plan state"
                    )
                checkpoint_plan = read_plan_state(checkpoint_state, self._content)
                command = cast(Command[object], graph_input)
                raw_response = command.resume
                # Native Commands accept a scalar; AG-UI binds responses to the
                # pending interrupt ID. Validate both before checkpoint acceptance.
                if isinstance(raw_response, Mapping):
                    response_map = cast(Mapping[str, object], raw_response)
                    response: object = response_map
                    if len(planning_snapshot.interrupts) == 1:
                        interrupt_id = planning_snapshot.interrupts[0].id
                        response = response_map.get(interrupt_id, response_map)
                else:
                    response = raw_response
                validate_plan_resume_response(checkpoint_state, self._options, response)
                use_planning = True
            elif native_pending:
                use_planning = bool(
                    checkpoint_plan is not None
                    and checkpoint_plan.status is PlanStatus.APPROVED
                    and checkpoint_plan.handoff is not None
                    and checkpoint_plan.handoff.phase is PlanHandoffPhase.PENDING
                )
            elif checkpoint_plan is not None and checkpoint_plan.status in {
                PlanStatus.APPROVED,
                PlanStatus.AWAITING_INPUT,
                PlanStatus.CANCELLED,
            }:
                yield _plan_values_part(checkpoint_plan)
                return
            else:
                raise PlanStateConflictError(
                    "resume input has no pending Planning or native checkpoint"
                )

        if not use_planning:
            overlay = await _native_plan_overlay(
                self._native,
                config,
                self._content,
            )
            native_interrupted = False
            handoff = None if overlay is None else overlay.handoff
            if (
                overlay is not None
                and handoff is not None
                and handoff.phase is PlanHandoffPhase.ACCEPTED
            ):
                instruction = _handoff_text(overlay, self._content)
            else:
                instruction = None
            source = PlanHandoffStream(
                self._native.astream(*bound.args, **bound.kwargs), instruction
            )
            async with aclosing(source):
                async for part in source:
                    if part.get("type") == "values" and part.get("ns") == ():
                        native_interrupted = bool(part.get("interrupts", ()))
                    yield part if overlay is None else _overlay_plan(part, overlay)
            if (
                overlay is not None
                and overlay.handoff is not None
                and overlay.handoff.phase is PlanHandoffPhase.ACCEPTED
            ):
                snapshot = await self._native.aget_state(config)
                if not native_interrupted:
                    overlay = await planning.mark_handoff_phase(
                        config,
                        overlay,
                        phase=PlanHandoffPhase.COMPLETED,
                        native_checkpoint_id=overlay.handoff.native_checkpoint_id
                        or _checkpoint_id(snapshot),
                        completed_checkpoint_id=_checkpoint_id(snapshot),
                    )
                    yield _plan_values_part(
                        overlay,
                        native_values=cast(Mapping[str, object], snapshot.values),
                    )
            return

        final_state: Mapping[str, object] = checkpoint_state
        final_plan = checkpoint_plan
        if (
            not isinstance(graph_input, Command)
            or checkpoint_plan is None
            or checkpoint_plan.status not in {PlanStatus.APPROVED, PlanStatus.CANCELLED}
        ):
            interrupted = False
            planning_bound = self._signature.bind(*bound.args, **bound.kwargs)
            planning_input: object = cast(object, graph_input)
            if (
                graph_input is not None
                and checkpoint_plan is not None
                and checkpoint_plan.status is PlanStatus.AWAITING_INPUT
                and not isinstance(graph_input, Command)
            ):
                if not isinstance(graph_input, Mapping):
                    raise TypeError("continued Plan input must be a state mapping")
                # The Planning checkpoint uses one stable run owner. LangGraph treats
                # a completed owner as replay unless the next user turn explicitly
                # schedules the initializer, so continue through a state update and
                # deterministic node target rather than creating a second Plan graph.
                planning_input = Command(
                    update=cast(Mapping[str, object], graph_input),
                    goto="plan_lifecycle.before_agent",
                )
            planning_bound.arguments["input"] = planning_input
            async for part in planning.astream(
                *planning_bound.args,
                **planning_bound.kwargs,
            ):
                if part.get("type") == "values" and part.get("ns") == ():
                    data = part.get("data")
                    if not isinstance(data, Mapping):
                        raise TypeError("Planning root values data must be a mapping")
                    final_state = cast(Mapping[str, object], data)
                    final_plan = read_plan_state(final_state, self._content)
                    interrupted = bool(part.get("interrupts", ()))
                yield part
            if interrupted:
                return

        if final_plan is None or final_plan.status in {
            PlanStatus.AWAITING_INPUT,
            PlanStatus.CANCELLED,
        }:
            return
        if final_plan.status is not PlanStatus.APPROVED:
            raise RuntimeError(
                "Planning ended without an interrupt or approved handoff"
            )
        if final_plan.handoff is None:
            raise RuntimeError("approved Plan state requires handoff metadata")
        native_snapshot, native_config = await _native_snapshot_for_plan(
            self._native,
            config,
            final_plan,
        )
        existing_message = _snapshot_handoff_message(
            native_snapshot,
            final_plan,
        )
        accepted_this_call = False
        if final_plan.handoff.phase is PlanHandoffPhase.PENDING:
            if existing_message is None:
                if native_snapshot.next:
                    raise PlanStateConflictError(
                        "native Graph has pending work before Plan handoff"
                    )
                message = _handoff_user_message(final_state, final_plan)
                staged_config = await self._native.aupdate_state(
                    config,
                    {
                        "messages": [message],
                        PLAN_HANDOFF_STATE_KEY: final_plan.handoff.digest,
                    },
                    as_node=START,
                )
                native_checkpoint_id = cast(
                    str,
                    staged_config.get("configurable", {}).get("checkpoint_id"),
                )
                if not native_checkpoint_id:
                    raise PlanStateConflictError(
                        "native handoff update returned no checkpoint_id"
                    )
                # LangGraph 1.2.10 returns the saver checkpoint coordinates here,
                # not the invocation config. Keep callbacks, Run identity, tags,
                # metadata, and execution limits when selecting the staged work.
                native_config = _select_checkpoint(config, native_checkpoint_id)
                native_snapshot = await self._native.aget_state(native_config)
            else:
                native_checkpoint_id = _checkpoint_id(native_snapshot)
                native_config = _select_checkpoint(config, native_checkpoint_id)
            final_plan = await planning.mark_handoff_phase(
                config,
                final_plan,
                phase=PlanHandoffPhase.ACCEPTED,
                native_checkpoint_id=native_checkpoint_id,
            )
            accepted_this_call = True
        elif existing_message is None:
            raise PlanStateConflictError(
                "accepted Plan handoff is missing from the native checkpoint"
            )

        handoff = final_plan.handoff
        if handoff is None:
            raise PlanStateConflictError("approved Plan lost handoff metadata")
        if handoff.phase is PlanHandoffPhase.COMPLETED:
            yield _plan_values_part(
                final_plan,
                native_values=cast(Mapping[str, object], native_snapshot.values),
            )
            return
        if not native_snapshot.next and not accepted_this_call:
            final_plan = await planning.mark_handoff_phase(
                config,
                final_plan,
                phase=PlanHandoffPhase.COMPLETED,
                native_checkpoint_id=handoff.native_checkpoint_id
                or _checkpoint_id(native_snapshot),
                completed_checkpoint_id=_checkpoint_id(native_snapshot),
            )
            yield _plan_values_part(
                final_plan,
                native_values=cast(Mapping[str, object], native_snapshot.values),
            )
            return
        if native_snapshot.interrupts:
            yield _plan_values_part(
                final_plan,
                native_values=cast(Mapping[str, object], native_snapshot.values),
            )
            return

        native_bound = self._signature.bind(*bound.args, **bound.kwargs)
        native_bound.arguments["input"] = None
        native_bound.arguments["config"] = native_config
        durability = native_bound.arguments.get("durability")
        if durability not in (None, "sync"):
            raise PlanModeConfigurationError("Plan handoff requires durability='sync'")
        native_bound.arguments["durability"] = "sync"
        native_interrupted = False
        source = PlanHandoffStream(
            self._native.astream(*native_bound.args, **native_bound.kwargs),
            _handoff_text(final_plan, self._content),
        )
        async with aclosing(source):
            async for part in source:
                if part.get("type") == "values" and part.get("ns") == ():
                    native_interrupted = bool(part.get("interrupts", ()))
                yield _overlay_plan(part, final_plan)
        if lineage_required:
            thread_id = configurable.get("thread_id")
            semantic_run_id = configurable.get(RUN_ID_METADATA_KEY)
            if not isinstance(thread_id, str) or not isinstance(semantic_run_id, str):
                raise PlanStateConflictError(
                    "native completion requires canonical AG-UI lineage identifiers"
                )
            native_head = await resolve_agui_native_run_head(
                self.checkpointer,
                thread_id=thread_id,
                run_id=semantic_run_id,
                runtime_profile=cast(str, runtime_profile),
            )
            native_snapshot = await self._native.aget_state(native_head.config)
        else:
            native_snapshot = await self._native.aget_state(config)
        if not native_interrupted:
            handoff = final_plan.handoff
            if handoff is None:
                raise PlanStateConflictError("approved Plan lost handoff metadata")
            final_plan = await planning.mark_handoff_phase(
                config,
                final_plan,
                phase=PlanHandoffPhase.COMPLETED,
                native_checkpoint_id=handoff.native_checkpoint_id
                or _checkpoint_id(native_snapshot),
                completed_checkpoint_id=_checkpoint_id(native_snapshot),
            )
            yield _plan_values_part(
                final_plan,
                native_values=cast(Mapping[str, object], native_snapshot.values),
            )


setattr(
    PlanCapableGraphRuntime.astream,
    "__signature__",
    inspect.signature(_COMPILED_ASTREAM),
)


__all__ = ["PlanCapableGraphRuntime"]

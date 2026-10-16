"""Explicit Deep Agents integration profiles selected before a Runtime is created."""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Protocol, TypeAlias, cast, runtime_checkable

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver, CheckpointTuple
from langgraph.graph.state import CompiledStateGraph

from ._agui_lineage_state import RESUME_WRITE_OWNER
from ._v3_stream import graph_v3_stream
from .native_driver import (
    DeepAgentsV2StreamDriver,
    DeepAgentsV3StreamDriver,
    NativeStreamDriver,
    ReasoningExtractor,
)

_COMPILED_ASTREAM = cast(
    Callable[..., object],
    cast(
        object,
        CompiledStateGraph.astream,  # pyright: ignore[reportUnknownMemberType]
    ),
)
_COMPILED_ASTREAM_SIGNATURE = inspect.signature(_COMPILED_ASTREAM).replace(
    parameters=tuple(inspect.signature(_COMPILED_ASTREAM).parameters.values())[1:]
)

# LangGraph 1.2.10's PregelLoop._first() applies writes owned by this task before
# preparing interrupted tasks, while map_command() persists native decisions on the
# resume channel. These locked values belong to the v2 Profile rather than TinkerFin's
# internal protocol; the resume settlement and Redis saver contract tests guard them.

_CheckpointSaver: TypeAlias = (
    BaseCheckpointSaver[int] | BaseCheckpointSaver[float] | BaseCheckpointSaver[str]
)


@runtime_checkable
class DeepAgentsRuntimeProfile(Protocol):
    """Provide Native stream normalization and checkpoint resume semantics.

    Each Runtime retains one integration. Stream selection is explicit and never
    inferred from payloads. Agent construction is owned by TinkerFin and does not
    form part of this extension contract.
    """

    @property
    def profile_id(self) -> str:
        """Return the canonical host-visible integration identity."""

        ...

    @property
    def astream_signature(self) -> inspect.Signature:
        """Return the stable bound Graph stream signature before graph construction."""

        ...

    def graph_stream(self, graph: object) -> Callable[..., object]:
        """Return the concrete event source callable for one created Graph."""

        ...

    @property
    def stream_driver(self) -> NativeStreamDriver:
        """Return the immutable invocation and stream normalization Driver."""

        ...

    async def stage_resume_intent(
        self,
        checkpointer: _CheckpointSaver,
        config: RunnableConfig,
        writes: tuple[tuple[str, object], ...],
    ) -> None:
        """Persist a resume intent without changing interrupted Graph state.

        Args:
            checkpointer: Borrowed saver that owns the interrupted checkpoint.
            config: Exact checkpoint configuration selected by Runtime lineage.
            writes: Ordered private resume-intent values.

        Raises:
            Exception: The concrete Profile cannot durably establish this intent.
        """

        ...

    def pending_resume_values(
        self,
        checkpoint: CheckpointTuple,
        *,
        channel_name: str,
    ) -> tuple[object, ...]:
        """Return Profile-owned pending private values from one checkpoint.

        Args:
            checkpoint: Saver tuple at the selected approval checkpoint.
            channel_name: TinkerFin resume-intent channel to inspect.

        Returns:
            Immutable raw values that the Runtime validates as exact markers.
        """

        ...


class DeepAgentsV2RuntimeProfile:
    """Integrate native LangGraph v2 streams and checkpoint resume semantics.

    Invocation binding, checkpoint identity, resume-intent staging, and frame
    normalization are covered by the Native and lineage contract tests.

    Args:
        reasoning_extractors: Verified provider-specific reasoning extractors applied
            while the v2 Driver normalizes live messages. An empty tuple emits no
            reasoning observations.

    Raises:
        TypeError: A reasoning extractor does not implement ``ReasoningExtractor``.
        ValueError: Reasoning extractor names are invalid or duplicated.
    """

    def __init__(
        self,
        *,
        reasoning_extractors: tuple[ReasoningExtractor, ...] = (),
    ) -> None:
        """Create one request-independent v2 Driver from verified extractors."""

        self._stream_driver = DeepAgentsV2StreamDriver(
            reasoning_extractors=reasoning_extractors,
        )

    @property
    def profile_id(self) -> str:
        """Return the concrete third-party integration identity."""

        return "deepagents-v2"

    @property
    def astream_signature(self) -> inspect.Signature:
        """Return the locked bound LangGraph stream contract used by lazy resume."""

        return _COMPILED_ASTREAM_SIGNATURE

    def graph_stream(self, graph: object) -> Callable[..., object]:
        """Return the locked graph's v2 ``astream`` callable."""

        stream = getattr(graph, "astream", None)
        if not callable(stream):
            raise TypeError("Deep Agents v2 graph must expose astream")
        return stream

    @property
    def stream_driver(self) -> NativeStreamDriver:
        """Return the request-independent v2 stream Driver."""

        return self._stream_driver

    async def stage_resume_intent(
        self,
        checkpointer: _CheckpointSaver,
        config: RunnableConfig,
        writes: tuple[tuple[str, object], ...],
    ) -> None:
        """Persist an intent without changing interrupted Graph state or task writes.

        The dedicated owner is never a Graph task. The checkpoint stays owned by its
        source Run until execution creates a new checkpoint. Matching retries are
        idempotent; conflicting records fail before decision submission.

        Args:
            checkpointer: Borrowed saver that owns the exact interrupted checkpoint.
            config: Exact approval checkpoint selected for the complete batch.
            writes: Ordered private resume-intent channel values.

        Raises:
            TypeError: A write does not use a canonical channel name.
            ValueError: Existing Profile-owned writes conflict with this intent.
        """

        if any(
            not isinstance(channel, str) or not channel or channel != channel.strip()
            for channel, _value in writes
        ):
            raise TypeError("resume intent channels must be canonical text")
        expected = dict(writes)
        if len(expected) != len(writes):
            raise ValueError("resume intent channels must be unique")
        checkpoint = await checkpointer.aget_tuple(config)
        if checkpoint is None:
            raise ValueError("resume intent checkpoint is unavailable")
        existing: dict[str, object] = {}
        for task_id, channel, value in checkpoint.pending_writes or ():
            if task_id != RESUME_WRITE_OWNER:
                continue
            if channel not in expected:
                raise ValueError("checkpoint contains an unrelated resume-intent write")
            if channel in existing and existing[channel] != value:
                raise ValueError("checkpoint contains conflicting resume-intent writes")
            existing[channel] = value
        if any(expected[channel] != value for channel, value in existing.items()):
            raise ValueError("checkpoint contains a different resume intent")
        if existing == expected:
            return
        await checkpointer.aput_writes(config, writes, RESUME_WRITE_OWNER)

    def pending_resume_values(
        self,
        checkpoint: CheckpointTuple,
        *,
        channel_name: str,
    ) -> tuple[object, ...]:
        """Return pending records from the dedicated resume-intent owner.

        Args:
            checkpoint: Saver tuple at the selected approval checkpoint.
            channel_name: Private intent channel selected by TinkerFin.

        Returns:
            Immutable raw marker values pending at the locked v2 boundary.
        """

        return tuple(
            value
            for task_id, channel, value in checkpoint.pending_writes or ()
            if task_id == RESUME_WRITE_OWNER and channel == channel_name
        )

    def _matches_resume_subgraph_namespace(
        self,
        namespace: tuple[str, ...],
        pending_task_ids: frozenset[str],
    ) -> bool:
        """Identify dynamic v2 subgraphs owned by currently interrupted root tasks.

        Deep Agents 0.7.5 names an ordinary dynamic child ``tools:<task-id>`` and
        appends a decimal slot for parallel children. Nested children retain that first
        component. This private Profile hook keeps the upstream convention out of Core
        and does not add a required method to existing custom Runtime Profiles.
        """

        if not namespace or not pending_task_ids:
            return False
        first = namespace[0]
        for task_id in pending_task_ids:
            prefix = f"tools:{task_id}"
            if first == prefix:
                return True
            if first.startswith(f"{prefix}:") and first[len(prefix) + 1 :].isdecimal():
                return True
        return False


class DeepAgentsV3RuntimeProfile(DeepAgentsV2RuntimeProfile):
    """Integrate the locked experimental LangGraph v3 event-stream API.

    The installed LangGraph 1.2.10 implementation still drives its v3 mux from an
    internal v2 stream. Selecting this Profile nevertheless exercises the public
    ``astream_events(version="v3")`` contract and never falls back to the v2 Profile.

    Args:
        reasoning_extractors: Verified provider reasoning extractors applied after v3
            events enter TinkerFin's canonical Native boundary.
    """

    def __init__(
        self,
        *,
        reasoning_extractors: tuple[ReasoningExtractor, ...] = (),
    ) -> None:
        """Create one request-independent v3 Driver from verified extractors."""

        self._stream_driver = DeepAgentsV3StreamDriver(
            reasoning_extractors=reasoning_extractors,
        )

    @property
    def profile_id(self) -> str:
        """Return the explicit experimental integration identity."""

        return "deepagents-v3"

    def graph_stream(self, graph: object) -> Callable[..., object]:
        """Return a canonical source carried by public v3 protocol events."""

        return graph_v3_stream(graph)


__all__ = [
    "DeepAgentsRuntimeProfile",
    "DeepAgentsV2RuntimeProfile",
    "DeepAgentsV3RuntimeProfile",
]

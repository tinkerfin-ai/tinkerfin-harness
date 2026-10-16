"""Validate resource inheritance at the registered external Graph boundary."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, cast

from langchain_core.runnables.base import RunnableBindingBase
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph.state import CompiledStateGraph

_CHECKPOINT_COORDINATES = frozenset(
    {"thread_id", "checkpoint_ns", "checkpoint_id", "checkpoint_map"}
)


def _reserved_config_key(key: object) -> bool:
    return isinstance(key, str) and (
        key in _CHECKPOINT_COORDINATES or key.startswith(("__pregel_", "__tinkerfin_"))
    )


def _validate_config(config: Mapping[str, object]) -> None:
    # LangGraph 1.2.10 ensure_config clears inherited execution resources when a
    # thread_id key is supplied, even for the same value. Internal config keys can
    # also replace the inherited saver or Store without changing the coordinates.
    if any(_reserved_config_key(key) for key in config):
        raise ValueError(
            "subagent configuration must inherit Runtime resources and thread"
        )
    configurable = config.get("configurable")
    if configurable is None:
        return
    if not isinstance(configurable, Mapping):
        raise TypeError("subagent configurable must be a mapping")
    if any(
        _reserved_config_key(key) for key in cast(Mapping[object, object], configurable)
    ):
        raise ValueError(
            "subagent configuration must inherit Runtime resources and thread"
        )


def validate_subagent_resources(subagents: object) -> None:
    """Reject visible Graph settings that bypass Runtime namespace isolation.

    Ordinary compiled graphs inherit resources. Static Runnable bindings may add
    business configuration, tags, and metadata. Arbitrary Python tools and opaque
    Runnables still own any external resources they open themselves; inspecting a
    registration does not sandbox their code.

    Args:
        subagents: Optional sequence of upstream subagent specifications.

    Raises:
        TypeError: The specifications or configuration have invalid container types.
        ValueError: Visible settings override inherited storage or thread ownership.
    """

    if subagents is None:
        return
    if not isinstance(subagents, Sequence) or isinstance(subagents, (str, bytes)):
        raise TypeError("subagents must be a sequence")
    for spec in cast(Sequence[object], subagents):
        if not isinstance(spec, Mapping):
            raise TypeError("subagent specifications must be mappings")
        runnable = cast(Mapping[str, object], spec).get("runnable")
        visited: set[int] = set()
        while isinstance(runnable, RunnableBindingBase):
            binding = cast(RunnableBindingBase[Any, Any], runnable)
            if id(binding) in visited:
                raise ValueError("subagent bindings contain a cycle")
            visited.add(id(binding))
            if binding.config_factories:
                raise ValueError("subagent configuration must be fixed at build time")
            _validate_config(binding.config)
            runnable = binding.bound
        if isinstance(runnable, CompiledStateGraph):
            graph = cast(CompiledStateGraph[Any, Any, Any, Any], runnable)
            # Pregel 1.2.10 leaves BaseCheckpointSaver's version type unparameterized.
            saver = cast(BaseCheckpointSaver[Any] | bool | None, graph.checkpointer)  # pyright: ignore[reportUnknownMemberType]
            if saver is not None and saver is not True:
                raise ValueError(
                    "compiled subagents must inherit the Runtime checkpointer"
                )
            if graph.store is not None:
                raise ValueError("compiled subagents must inherit the Runtime Store")
            if graph.config is not None:
                _validate_config(graph.config)

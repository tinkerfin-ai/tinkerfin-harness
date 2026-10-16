"""Fixtures for exercising managed execution through the public Runtime API."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from tinkerfin import AgentRuntime, TinkerFin


@pytest.fixture
def definition_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[..., AgentRuntime[None]]:
    """Build a Runtime whose preparation returns a deterministic Graph or failure."""

    def create(
        graph: object,
        *,
        tinkerfin: TinkerFin | None = None,
    ) -> AgentRuntime[None]:
        def build(*_args: object, **_kwargs: object) -> object:
            if isinstance(graph, BaseException):
                raise graph
            return graph

        monkeypatch.setattr(
            "tinkerfin.deep_agent.create_agent_graph",
            build,
        )
        return (tinkerfin or TinkerFin().with_namespace("test")).build(
            model="provider:model",
            tools=[],
        )

    return create

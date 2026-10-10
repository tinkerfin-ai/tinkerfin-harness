from __future__ import annotations

from collections.abc import Mapping

import pytest

from tinkerfin_agui_adapter import (
    DeepAgentAgUiAdapter,
    RunIdentity,
)


def _identity(*, run_id: str = "run-1") -> RunIdentity:
    return RunIdentity(namespace="test", thread_id="thread-1", run_id=run_id)


def _ordinary_task_start(*, node: str, task_id: str) -> dict[str, object]:
    return {
        "type": "tasks",
        "ns": (),
        "data": {
            "id": task_id,
            "name": node,
            "input": {"value": 1},
            "triggers": (f"branch:to:{node}",),
        },
    }


def _generic_interrupt_part(
    *,
    namespace: tuple[str, ...],
    interrupt_id: str,
    value: object,
) -> dict[str, object]:
    return {
        "type": "values",
        "ns": namespace,
        "data": {"messages": [], "child_state": True},
        "interrupts": ({"id": interrupt_id, "value": value},),
    }


def test_nested_child_interrupt_propagates_through_ancestor_and_root_once() -> None:
    adapter = DeepAgentAgUiAdapter(identity=_identity())
    adapter.process(_ordinary_task_start(node="outer", task_id="outer-task"))
    outer_namespace = ("outer:outer-task",)
    adapter.process(
        {
            "type": "tasks",
            "ns": outer_namespace,
            "data": {
                "id": "inner-task",
                "name": "inner",
                "input": {"value": 1},
                "triggers": ("branch:to:inner",),
            },
        }
    )
    inner_namespace = (*outer_namespace, "inner:inner-task")
    value = {"kind": "nested-pause"}

    adapter.process(
        _generic_interrupt_part(
            namespace=inner_namespace,
            interrupt_id="nested-interrupt",
            value=value,
        )
    )
    adapter.process(
        _generic_interrupt_part(
            namespace=outer_namespace,
            interrupt_id="nested-interrupt",
            value=value,
        )
    )
    adapter.process(
        {
            "type": "values",
            "ns": (),
            "data": {"messages": [], "root_state": True},
            "interrupts": ({"id": "nested-interrupt", "value": value},),
        }
    )

    outcome = adapter.main_outcome()
    assert len(outcome.interrupts) == 1
    metadata = outcome.interrupts[0].metadata
    assert isinstance(metadata, Mapping)
    source = metadata.get("source")
    assert isinstance(source, Mapping)
    assert source["graphNamespace"] == list(inner_namespace)
    assert adapter.finish() == []


@pytest.mark.parametrize("shared_scope", [False, True])
def test_parallel_interrupt_batches_accumulate_until_stream_completion(
    shared_scope: bool,
) -> None:
    adapter = DeepAgentAgUiAdapter(identity=_identity())
    adapter.process(_ordinary_task_start(node="first", task_id="task-1"))
    if not shared_scope:
        adapter.process(_ordinary_task_start(node="second", task_id="task-2"))
    first = _generic_interrupt_part(
        namespace=("first:task-1",), interrupt_id="first", value={"kind": "first"}
    )
    second = _generic_interrupt_part(
        namespace=("first:task-1",) if shared_scope else ("second:task-2",),
        interrupt_id="second",
        value={"kind": "second"},
    )
    adapter.process(first)
    adapter.process(second)
    adapter.process({**first, "ns": (), "data": {"messages": []}})
    adapter.process({**second, "ns": (), "data": {"messages": []}})
    adapter.finish()
    assert [pending.id for pending in adapter.main_outcome().interrupts] == [
        "first",
        "second",
    ]

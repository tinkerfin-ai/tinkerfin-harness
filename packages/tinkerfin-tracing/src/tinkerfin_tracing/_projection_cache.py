"""Bound disposable business Projection states to one Tracer's registrations."""

from __future__ import annotations

import json
from collections import OrderedDict
from collections.abc import Iterator, Mapping
from dataclasses import dataclass

from .projection import CoreProjectionState, RegisteredTraceProjection
from .store import TraceThreadKey

_MAX_ENTRIES = 64
_MAX_BYTES = 16 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class _ProjectionScope:
    key: TraceThreadKey
    name: str
    head_run_id: str


@dataclass(frozen=True, slots=True)
class _ProjectionState:
    """Keep detached canonical state and the exact facts already folded into it."""

    scope: _ProjectionScope
    run_ids: frozenset[str]
    as_of_seq: int
    data: bytes

    def can_seed(
        self,
        scope: _ProjectionScope,
        *,
        run_ids: frozenset[str],
        as_of_seq: int,
        core_state: CoreProjectionState,
    ) -> bool:
        """Require a complete shared prefix, including late-bound ancestry."""

        if (
            self.scope.key != scope.key
            or self.scope.name != scope.name
            or self.as_of_seq > as_of_seq
            or self.scope.head_run_id not in run_ids
        ):
            return False
        if self.scope.head_run_id == scope.head_run_id:
            return self.run_ids == run_ids
        lineage: set[str] = set()
        current: str | None = self.scope.head_run_id
        while current is not None and current not in lineage:
            lineage.add(current)
            run = core_state.runs.get(current)
            current = None if run is None else run.parent_run_id
        if frozenset(lineage) != self.run_ids or not self.run_ids <= run_ids:
            return False
        # A parent queried after its child started lacks the child's earlier
        # facts. Starting after that parent's watermark would silently omit them.
        return all(
            (run := core_state.runs.get(run_id)) is not None
            and run.first_seq > self.as_of_seq
            for run_id in run_ids - self.run_ids
        )

    def retained_bytes(self) -> int:
        """Count canonical state plus every retained identity and lineage value."""

        metadata = (
            self.scope.key.namespace,
            self.scope.key.thread_id,
            self.scope.key.generation,
            self.scope.name,
            self.scope.head_run_id,
            sorted(self.run_ids),
            self.as_of_seq,
        )
        return len(self.data) + len(
            json.dumps(metadata, ensure_ascii=False, separators=(",", ":")).encode()
        )


class ProjectionRegistry(Mapping[str, RegisteredTraceProjection]):
    """Own registrations and bounded, immutable cross-request Projection seeds.

    Lookup, replacement, and eviction contain no suspension points or background
    tasks. Readers decode their own state, and a late query cannot replace a
    higher retained watermark. Active followers own separate states; this budget
    covers retained canonical bytes and metadata, not their memory or Python RSS.
    """

    def __init__(self, values: Mapping[str, RegisteredTraceProjection]) -> None:
        self._values = dict(values)
        self._states: OrderedDict[_ProjectionScope, tuple[_ProjectionState, int]] = (
            OrderedDict()
        )
        self._bytes = 0

    def __getitem__(self, name: str) -> RegisteredTraceProjection:
        return self._values[name]

    def __iter__(self) -> Iterator[str]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def seed(
        self,
        scope: _ProjectionScope,
        *,
        run_ids: frozenset[str],
        as_of_seq: int,
        core_state: CoreProjectionState,
    ) -> _ProjectionState | None:
        candidates = (
            state
            for state, _size in self._states.values()
            if state.can_seed(
                scope,
                run_ids=run_ids,
                as_of_seq=as_of_seq,
                core_state=core_state,
            )
        )
        selected = max(candidates, key=lambda state: state.as_of_seq, default=None)
        if selected is not None:
            self._states.move_to_end(selected.scope)
        return selected

    def remember(self, state: _ProjectionState) -> None:
        current = self._states.get(state.scope)
        if current is not None and current[0].as_of_seq > state.as_of_seq:
            return
        size = state.retained_bytes()
        if size > _MAX_BYTES:
            return
        if current is not None:
            self._bytes -= self._states.pop(state.scope)[1]
        while self._states and (
            len(self._states) >= _MAX_ENTRIES or self._bytes + size > _MAX_BYTES
        ):
            self._bytes -= self._states.popitem(last=False)[1][1]
        self._states[state.scope] = state, size
        self._bytes += size

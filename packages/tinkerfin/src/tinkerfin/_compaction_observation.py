"""Observe summary generation separately from checkpoint adoption.

An operation belongs to one managed run. Its private checkpoint marker proves
which generated summary was committed; callback parent IDs retain their native
meaning. No generated result is called applied until the borrowed saver returns.
"""

from __future__ import annotations

from collections.abc import Mapping
from contextvars import ContextVar
from typing import Literal
from uuid import uuid4

from langchain_core.messages import AnyMessage
from langgraph.config import get_config

from tinkerfin_contracts import ContextContributionObservation, ObservationBoundary

from ._call_observation import (
    _CURRENT_CALL_ID,
    _CURRENT_HUB,
    _callback_namespace,
    _control_flow_phase,
)
from ._observation import _message_record, _qualified_name, _source_value, _stamp

COMPACTION_MARKER = "_tinkerfin_compaction_id"
_CURRENT_COMPACTION: ContextVar[CompactionOperation | None] = ContextVar(
    "tinkerfin_compaction", default=None
)
_SUMMARY_OPERATION: ContextVar[str | None] = ContextVar(
    "tinkerfin_summary_operation", default=None
)


class CompactionOperation:
    """Own one summary's observations until commit or run settlement."""

    def __init__(
        self,
        origin: Literal["manual", "automatic", "tool"],
        *,
        tool_call_id: str | None = None,
    ) -> None:
        self.id = uuid4().hex
        self.hub = _CURRENT_HUB.get()
        self.origin: Literal["manual", "automatic", "tool"] = origin
        self.tool_call_id = tool_call_id
        self.parent_call_id = _CURRENT_CALL_ID.get()
        try:
            self.namespace = _callback_namespace(get_config().get("metadata"))
        except RuntimeError:
            self.namespace = ()
        self.original: list[AnyMessage] = []
        self.selected: list[AnyMessage] = []
        self.summary: str | None = None
        self.model_call_ids: list[str] = []
        self.started = False
        self.finished = False
        self.pending = False
        self.tool_error: BaseException | None = None
        self.payload: dict[str, object] = {}

    async def start(self) -> None:
        if self.started:
            return
        self.started = True
        if self.hub is not None:
            self.hub.compactions[self.id] = self
        await self._emit(
            "started",
            input={
                "origin": self.origin,
                "messages": [
                    _message_record(message).model_dump(mode="json")
                    for message in self.selected
                ],
            },
        )

    async def model_started(self, call_id: str) -> None:
        self.model_call_ids.append(call_id)
        await self._emit("generated", output={"status": "generating"})

    async def generated(self, summary: str) -> None:
        self.summary = summary
        await self._emit(
            "generated", output={"status": "generated", "summary": summary}
        )

    async def saving(self) -> None:
        await self._emit(
            "generated", output={"status": "saving", "summary": self.summary}
        )

    async def complete(self, payload: dict[str, object]) -> None:
        if self.finished:
            return
        await self.start()
        self.finished = True
        self.payload = payload
        await self._emit(
            "completed",
            output={
                **payload,
                **(
                    {"generated_summary": self.summary}
                    if self.summary is not None
                    else {}
                ),
            },
        )

    async def fail(self, error: BaseException) -> None:
        if self.finished:
            return
        await self.start()
        self.finished = True
        phase = _control_flow_phase(error) or "failed"
        await self._emit(phase, error=error if phase == "failed" else None)

    async def abandon(self) -> None:
        if self.started and not self.finished:
            self.finished = True
            await self._emit("abandoned")

    def mark_update(self, update: dict[str, object]) -> None:
        """Bind the native state update to this operation without changing its event."""
        if self.hub is not None and self.hub.enabled:
            update[COMPACTION_MARKER] = self.id
        self.pending = True
        self.payload = {
            "status": "compacted",
            "summary": self.summary,
            "compacted_messages": len(self.selected),
        }

    async def _emit(
        self,
        phase: Literal[
            "started",
            "generated",
            "completed",
            "failed",
            "cancelled",
            "interrupted",
            "abandoned",
        ],
        *,
        input: object = None,
        output: object = None,
        error: BaseException | None = None,
    ) -> None:
        if self.hub is None or not self.hub.enabled:
            return
        observed_at, monotonic_ns = _stamp()
        await self.hub.observe(
            ContextContributionObservation(
                identity=self.hub.context.identity,
                observed_at=observed_at,
                monotonic_ns=monotonic_ns,
                phase=phase,
                contribution_id=self.id,
                parent_call_id=self.parent_call_id,
                parent_tool_call_id=self.tool_call_id,
                compaction_origin=self.origin,
                model_call_ids=tuple(self.model_call_ids),
                graph_namespace=self.namespace,
                context_kind="compaction",
                name="context_compaction",
                input=None if input is None else _source_value(input),
                output=None if output is None else _source_value(output),
                error_type=None if error is None else _qualified_name(error),
                failure_origin=error is not None and self.hub.claim_error(error),
            )
        )
        await self.hub.force(ObservationBoundary.CALL_STARTED)


async def compaction_checkpointed(
    values: Mapping[str, object], new_versions: Mapping[str, object]
) -> None:
    """Publish adoption only for an operation explicitly included in this commit."""
    from deepagents.middleware.summarization import SUMMARIZATION_EVENT_KEY

    hub = _CURRENT_HUB.get()
    if (
        hub is None
        or COMPACTION_MARKER not in new_versions
        or SUMMARIZATION_EVENT_KEY not in new_versions
    ):
        return
    operation_id = values.get(COMPACTION_MARKER)
    if not isinstance(operation_id, str):
        return
    operation = hub.compactions.get(operation_id)
    if operation is not None and operation.pending:
        await operation.complete(operation.payload)

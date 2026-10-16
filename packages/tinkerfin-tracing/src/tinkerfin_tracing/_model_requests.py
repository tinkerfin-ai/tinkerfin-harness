"""Resolve retained model inputs within their original conversation scope."""

from __future__ import annotations

import base64
import binascii

from pydantic import Field, ValidationError

from tinkerfin_contracts import ThreadIdentity

from ._models import TraceModel
from .errors import InvalidTraceReference, TraceStoreProtocolError
from .facts import ModelCallFact, TraceEvent
from .model_requests import TraceModelRequest
from .store import TraceStore, TraceThreadKey


class _RequestReference(TraceModel, frozen=True):
    key: TraceThreadKey
    trace_seq: int = Field(ge=1)
    run_id: str = Field(min_length=1, max_length=1024)
    node_id: str = Field(min_length=1, max_length=2048)


def model_request_reference(event: TraceEvent) -> str:
    """Locate one recorded model input without repeating its payload in a graph."""
    fact = event.fact
    if not isinstance(fact, ModelCallFact) or fact.phase != "started":
        raise TraceStoreProtocolError("Model request reference requires its start fact")
    reference = _RequestReference(
        key=TraceThreadKey(
            namespace=fact.identity.namespace,
            thread_id=fact.identity.thread_id,
            generation=event.generation,
        ),
        trace_seq=event.trace_seq,
        run_id=fact.identity.run_id,
        node_id=fact.call_id,
    )
    return (
        base64.urlsafe_b64encode(reference.model_dump_json().encode())
        .decode()
        .rstrip("=")
    )


async def read_model_request(
    store: TraceStore, identity: ThreadIdentity, reference: str
) -> TraceModelRequest:
    """Read retained input only when its evidence belongs to the authorized scope."""
    if not isinstance(reference, str) or not reference or len(reference) > 16_384:
        raise InvalidTraceReference("Model request reference is invalid")
    try:
        token = _RequestReference.model_validate_json(
            base64.b64decode(
                reference + "=" * (-len(reference) % 4), altchars=b"-_", validate=True
            )
        )
    except (ValueError, binascii.Error, ValidationError) as error:
        raise InvalidTraceReference(
            "Model request reference is invalid", cause=error
        ) from error
    if token.key.thread != identity:
        raise InvalidTraceReference("Model request belongs to another conversation")
    events = await store.read_events(
        token.key, after_seq=token.trace_seq - 1, as_of_seq=token.trace_seq, limit=1
    )
    if len(events) != 1:
        raise InvalidTraceReference("Model request is unavailable")
    event = events[0]
    fact = event.fact
    if (
        event.trace_seq != token.trace_seq
        or event.generation != token.key.generation
        or fact.identity.thread != identity
        or fact.identity.run_id != token.run_id
        or not isinstance(fact, ModelCallFact)
        or fact.phase != "started"
        or fact.call_id != token.node_id
        or fact.request is None
    ):
        raise InvalidTraceReference(
            "Model request reference does not match retained evidence"
        )
    return TraceModelRequest(
        node_id=token.node_id,
        request=fact.request.value,
        request_omitted=fact.request.disposition == "omitted",
    )

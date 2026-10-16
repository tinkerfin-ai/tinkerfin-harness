"""Internal canonical fact preparation shared by built-in Trace writers."""

from __future__ import annotations

from uuid import uuid4

from .backend import TraceStoredFact
from .codec import CanonicalTracePayloadCodec
from .facts import TraceSemanticFact

PreparedTraceFact = TraceStoredFact


def prepare_trace_facts(
    facts: tuple[TraceSemanticFact, ...],
    *,
    codec: CanonicalTracePayloadCodec,
) -> tuple[PreparedTraceFact, ...]:
    """Encode each fact once and retain its exact Store evidence."""

    prepared: list[PreparedTraceFact] = []
    for fact in facts:
        stored_fact = fact.model_copy(deep=True)
        encoded = codec.encode_fact(stored_fact)
        prepared.append(
            TraceStoredFact(
                event_id=uuid4().hex,
                fact=stored_fact,
                canonical_payload=encoded.data,
                exact_byte_count=len(encoded.data),
                payload_digest=encoded.digest,
            )
        )
    return tuple(prepared)

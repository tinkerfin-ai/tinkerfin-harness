"""Deterministic semantic identities shared by Trace capture and indexing."""

from __future__ import annotations

import hashlib
import json


def scope_id(kind: str, namespace: tuple[str, ...], source_id: str) -> str:
    """Return one stable ID for a source value inside an exact graph scope."""

    encoded = json.dumps(
        list(namespace),
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()
    scope = hashlib.sha256(encoded).hexdigest()[:24]
    return f"{kind}:{scope}:{source_id}"


__all__ = ["scope_id"]

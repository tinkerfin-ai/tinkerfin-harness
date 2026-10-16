"""Unambiguous Sandbox resource keys for logical and standalone ownership."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import cast

from tinkerfin_contracts.identity import validate_namespace


@dataclass(frozen=True, slots=True)
class SandboxResourceIdentity:
    """Keep an application's key distinct from its optional logical namespace.

    ``None`` identifies standalone use and is different from every Runtime
    namespace. State and local caches use the canonical key; public details and
    events expose the original namespace and application key separately.
    """

    namespace: str | None
    key: str

    def canonical_key(self) -> str:
        """Encode both identity fields without interpreting business key syntax."""

        if self.namespace is not None:
            validate_namespace(self.namespace)
        if not isinstance(self.key, str):
            raise TypeError("key_resolver must return a string")
        if not self.key.strip():
            raise ValueError("key_resolver must return a non-blank string")
        self.key.encode("utf-8")
        return json.dumps(
            {"namespace": self.namespace, "key": self.key},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )

    @classmethod
    def from_key(cls, value: str) -> SandboxResourceIdentity:
        """Read the current canonical key without accepting an unscoped fallback."""

        decoded: object = json.loads(value)
        if not isinstance(decoded, dict):
            raise TypeError("Sandbox resource identity must be a JSON object")
        fields = cast(dict[str, object], decoded)
        if set(fields) != {"namespace", "key"}:
            raise ValueError("Sandbox resource identity has invalid fields")
        namespace, key = fields["namespace"], fields["key"]
        if namespace is not None and not isinstance(namespace, str):
            raise TypeError("Sandbox resource namespace must be text or None")
        if not isinstance(key, str):
            raise TypeError("Sandbox resource key must be text")
        identity = cls(namespace, key)
        if identity.canonical_key() != value:
            raise ValueError("Sandbox resource identity is not canonical")
        return identity

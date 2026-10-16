"""Structural extension boundaries for sources, codecs, and SSE renderers."""

from __future__ import annotations

from collections.abc import AsyncIterator, Coroutine
from typing import ClassVar, Protocol, TypeVar, runtime_checkable

from tinkerfin_contracts import RunIdentity

from .models import RecoverableMessage, RecoveryCheckpoint

SourceT_co = TypeVar("SourceT_co", covariant=True)
SourceT_contra = TypeVar("SourceT_contra", contravariant=True)
CodecInputT_co = TypeVar("CodecInputT_co", covariant=True)
ReplayT_co = TypeVar("ReplayT_co", covariant=True)
ReplayT_contra = TypeVar("ReplayT_contra", contravariant=True)
RecoverableSourceT = TypeVar("RecoverableSourceT")


@runtime_checkable
class MessageSource(Protocol[SourceT_co]):
    """Single-use asynchronous source whose close operation is idempotent."""

    def __aiter__(self) -> AsyncIterator[SourceT_co]:
        """Return the source's single-use asynchronous iterator."""

        ...

    def aclose(self) -> Coroutine[object, object, None]:
        """Return coroutine cleanup, including native async-generator close operations."""

        ...


@runtime_checkable
class ProfiledMessageSource(
    MessageSource[SourceT_co],
    Protocol[SourceT_co, ReplayT_co],
):
    """Object source with a complete immutable built-in codec profile.

    The live type is consumed by the selected codec. The replay type is produced by
    decoding committed bytes and determines the subscription's static item type.
    """

    @property
    def messaging_codec_profile(self) -> str:
        """Return the registered codec profile for durable serialization."""

        ...

    @property
    def messaging_identity(self) -> RunIdentity:
        """Return the immutable durable thread and run identity."""

        ...

    @property
    def messaging_source_type(self) -> type[SourceT_co]:
        """Return the live item type consumed by the profile codec."""

        ...

    @property
    def messaging_replay_type(self) -> type[ReplayT_co]:
        """Return the decoded item type yielded during replay."""

        ...


@runtime_checkable
class MessageCodecInputSource(
    Protocol[SourceT_contra, CodecInputT_co],
):
    """Transform a live item into the finite value accepted by its profile codec.

    This optional structural capability lets an upstream Runtime retain ownership of
    provider-specific normalization while Messaging remains protocol-neutral. The
    producer invokes the hook exactly once for an accepted item and settles its commit
    before pulling the next item, so a source may transfer a single-use sidecar safely.
    """

    @property
    def messaging_codec_input_type(self) -> type[CodecInputT_co]:
        """Return the exact finite type accepted by the inferred profile codec."""

        ...

    def messaging_codec_input(self, item: SourceT_contra) -> CodecInputT_co:
        """Return the already normalized codec input paired with ``item``."""

        ...


@runtime_checkable
class RecoverableSource(Protocol[RecoverableSourceT]):
    """Rebuild a single-use source from its last committed checkpoint."""

    async def open(
        self,
        checkpoint: RecoveryCheckpoint | None,
    ) -> MessageSource[RecoverableMessage[RecoverableSourceT]]:
        """Open a new source at the last durably committed checkpoint."""

        ...


@runtime_checkable
class MessageCodec(Protocol[SourceT_contra, ReplayT_co]):
    """Translate source values to and from one stable persisted byte schema."""

    codec_id: ClassVar[str]

    def encode(self, item: SourceT_contra) -> bytes:
        """Encode one live item into the profile's stable byte schema."""

        ...

    def decode(self, payload: bytes) -> ReplayT_co:
        """Decode one committed payload into its replay type."""

        ...


@runtime_checkable
class MessagePublicationPolicy(Protocol[SourceT_contra]):
    """Optional codec rules for external messages and protocol termination.

    Codecs without protocol lifecycle events need not implement this extension.
    Publications require a live run with at least one committed source message.
    A terminal source message closes publication in the same durable commit.
    """

    def validate_publication(
        self, item: SourceT_contra, *, identity: RunIdentity
    ) -> None:
        """Reject external messages that would violate the codec's protocol."""
        ...

    def starts_publication(
        self, item: SourceT_contra, *, identity: RunIdentity
    ) -> bool:
        """Identify the target run start that permits external publication."""
        ...

    def ends_publication(self, item: SourceT_contra, *, identity: RunIdentity) -> bool:
        """Identify the target run's terminal source message, excluding child runs."""
        ...


@runtime_checkable
class SseRenderer(Protocol[ReplayT_contra]):
    """Render one decoded payload with its durable sequence as an SSE frame."""

    def render(self, *, seq: int, payload: ReplayT_contra) -> bytes:
        """Render one decoded payload and durable sequence as an SSE frame."""

        ...

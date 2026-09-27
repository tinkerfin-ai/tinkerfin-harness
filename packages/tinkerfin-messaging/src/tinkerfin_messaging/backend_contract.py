"""Storage-oriented extension contract for durable Messaging backends."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol, runtime_checkable

from tinkerfin_contracts import RunIdentity

from .backend import FinalRunStatus, RunStatus
from .limits import MessagingLimits
from .models import MessageEnvelope, RecoveryCheckpoint
from .retention import MessagingRetentionPolicy

MessagingTransitionKind = Literal[
    "prepare_run",
    "append_message",
    "publish_message",
    "begin_settlement",
    "finish_run",
    "request_cancellation",
    "renew_producer_ownership",
    "reconcile_producer_ownership",
    "begin_generation_cleanup",
    "finish_generation_cleanup",
]
MessagingStreamDisposition = Literal[
    "active",
    "deleting",
    "deleted",
    "expiring",
    "expired",
]
MessagingCleanupReason = Literal["deleted", "expired"]
MessagingLeaseAction = Literal["none", "acquire", "renew", "release"]
MessagingRetentionAction = Literal["none", "clear", "start"]


@dataclass(frozen=True, slots=True)
class MessagingBackendSettings:
    """Describe immutable behavior shared by one Messaging backend deployment.

    Attributes:
        limits: Encoded message, checkpoint, and generation capacity limits.
        retention_policy: Terminal replay retention applied to every generation.
        producer_renew_interval_seconds: Delay between producer ownership renewals, or
            ``None`` when the backend has no expiring external ownership.
        producer_lease_seconds: Storage-clock ownership duration, or ``None`` when
            producer ownership cannot expire independently of the process.
        change_wait_timeout_seconds: Maximum duration of one backend change wait before
            the framework reloads authoritative state, or ``None`` for a natively
            notified wait with no periodic fallback.
    """

    limits: MessagingLimits
    retention_policy: MessagingRetentionPolicy
    producer_renew_interval_seconds: float | None
    producer_lease_seconds: float | None
    change_wait_timeout_seconds: float | None

    def __post_init__(self) -> None:
        """Validate policy types, finite durations, and lease ordering.

        Raises:
            TypeError: A policy or duration has the wrong type.
            ValueError: A duration is non-finite, non-positive, or inconsistently
                disables producer ownership.
        """

        if not isinstance(self.limits, MessagingLimits):
            raise TypeError("limits must be a MessagingLimits")
        if not isinstance(self.retention_policy, MessagingRetentionPolicy):
            raise TypeError("retention_policy must be a MessagingRetentionPolicy")
        wait_timeout = self.change_wait_timeout_seconds
        if wait_timeout is not None and (
            isinstance(wait_timeout, bool) or not isinstance(wait_timeout, int | float)
        ):
            raise TypeError("change_wait_timeout_seconds must be numeric or None")
        if wait_timeout is not None and (
            not math.isfinite(float(wait_timeout)) or wait_timeout <= 0
        ):
            raise ValueError("change_wait_timeout_seconds must be finite and positive")
        renew_interval = self.producer_renew_interval_seconds
        lease_duration = self.producer_lease_seconds
        if renew_interval is None or lease_duration is None:
            if renew_interval is not None or lease_duration is not None:
                raise ValueError(
                    "producer renewal and lease durations must both be set or disabled"
                )
            return
        for name, value in (
            ("producer_renew_interval_seconds", renew_interval),
            ("producer_lease_seconds", lease_duration),
        ):
            if isinstance(value, bool) or not isinstance(value, int | float):
                raise TypeError(f"{name} must be numeric or None")
            if not math.isfinite(float(value)) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
        if renew_interval >= lease_duration:
            raise ValueError(
                "producer_renew_interval_seconds must be shorter than "
                "producer_lease_seconds"
            )


@dataclass(frozen=True, slots=True)
class MessagingRunReference:
    """Identify one exact stream generation and optional producer ownership.

    Attributes:
        channel: Canonical logical channel containing the stream.
        identity: Exact thread and semantic run identity.
        generation: Positive stream generation selected by preparation.
        producer_token: Opaque producer ownership token, or ``None`` for an observer.
        producer_fence: Monotonic producer fence, or ``None`` for an observer.
    """

    channel: str
    identity: RunIdentity
    generation: int
    producer_token: str | None
    producer_fence: int | None


@dataclass(frozen=True, slots=True)
class StoredMessagingChannel:
    """Represent the durable configuration shared by one logical channel.

    Attributes:
        channel: Canonical logical channel name.
        codec_id: Stable codec required by every stream in the channel.
        limits: Capacity contract persisted for cross-worker consistency.
        retention_policy: Terminal replay policy persisted for consistency.
    """

    channel: str
    codec_id: str
    limits: MessagingLimits
    retention_policy: MessagingRetentionPolicy


@dataclass(frozen=True, slots=True)
class StoredMessagingStream:
    """Represent bounded metadata for one thread stream generation.

    Attributes:
        channel: Canonical logical channel containing the stream.
        thread_id: Canonical thread identifier shared by the generation's runs.
        generation: Positive replacement generation.
        disposition: Current readable, cleanup, deleted, or expired state.
        latest_sequence: Greatest committed message sequence, or zero when empty.
        payload_bytes: Total encoded payload bytes retained by the generation.
        next_producer_fence: Fence allocated to the next accepted producer owner.
        active_run_id: Current producer run identifier, or ``None`` when inactive.
        retention_expired: Whether the storage clock has reached the terminal deadline.
        message_sequence: Message-side change cursor used by followers.
        control_sequence: Run-control change cursor used by followers.
    """

    channel: str
    thread_id: str
    generation: int
    disposition: MessagingStreamDisposition
    latest_sequence: int
    payload_bytes: int
    next_producer_fence: int
    active_run_id: str | None
    retention_expired: bool
    message_sequence: int
    control_sequence: int


@dataclass(frozen=True, slots=True)
class StoredMessagingRun:
    """Represent one durable semantic run without exposing a database schema.

    Attributes:
        identity: Exact thread and semantic run identity.
        generation: Stream generation containing the run.
        start_sequence: Thread tail observed before the run began.
        end_sequence: Greatest sequence committed by the run.
        status: Current durable producer status.
        settlement_started: Whether the producer has atomically claimed settlement.
        cancellable: Whether the producer exposes a cancellation callback.
        recoverable: Whether an expired producer may reopen from its checkpoint.
        producer_token: Current opaque owner token, or ``None`` after settlement.
        producer_fence: Monotonic owner fence retained for stale-owner rejection.
        producer_lease_active: Whether storage still recognizes the current owner.
        producer_lease_remaining_seconds: Storage-clock duration until ownership may
            expire, or ``None`` when no finite lease controls the next observation.
        publication_closed: Whether a committed protocol terminal forbids publication.
        publication_ready: Whether the source committed the required run start.
        checkpoint: Last checkpoint committed atomically with a message.
        failure_class: Bounded trusted failure class name for remote diagnostics.
        failure_message: Bounded trusted failure message for remote diagnostics.
        local_failure: Original in-process failure when the backend can retain it safely.
        request_digest: Immutable command-content binding, or None for an ordinary
            object stream. Empty and nonempty bindings never attach to each other.
    """

    identity: RunIdentity
    generation: int
    start_sequence: int
    end_sequence: int
    status: RunStatus
    settlement_started: bool
    cancellable: bool
    recoverable: bool
    producer_token: str | None
    producer_fence: int
    producer_lease_active: bool
    checkpoint: RecoveryCheckpoint | None
    failure_class: str
    failure_message: str
    local_failure: BaseException | None = None
    producer_lease_remaining_seconds: float | None = None
    publication_closed: bool = False
    publication_ready: bool = False
    request_digest: str | None = None


@dataclass(frozen=True, slots=True)
class StoredMessageEvidence:
    """Pair one committed envelope with its idempotency and checkpoint evidence.

    Attributes:
        envelope: Immutable committed message returned during replay.
        signature: Stable digest covering run, codec, payload, and checkpoint.
        checkpoint: Recovery checkpoint committed with the message, when present.
    """

    envelope: MessageEnvelope
    signature: str
    checkpoint: RecoveryCheckpoint | None


@dataclass(frozen=True, slots=True)
class MessagingStateQuery:
    """Select the bounded durable state needed by one framework operation.

    Attributes:
        channel: Canonical logical channel to inspect.
        identity: Thread and optional target run identity.
        generation: Exact generation to inspect, or ``None`` for the current one.
        message_id: Optional idempotency key whose evidence must be loaded.
        include_active_run: Whether to include a different active run in the snapshot.
    """

    channel: str
    identity: RunIdentity
    generation: int | None = None
    message_id: str | None = None
    include_active_run: bool = False


@dataclass(frozen=True, slots=True)
class MessagingStateSnapshot:
    """Return one consistent bounded view for a Messaging transition or read.

    Every populated field must describe the same logical storage point. The snapshot
    must never combine metadata, run state, message evidence, lease state, or a storage
    timestamp that could not have coexisted. Transactional implementations obtain the
    values in one transaction; optimistic implementations must validate a revision and
    retry only a proven conflict before returning.

    Attributes:
        observed_at: Aware UTC timestamp allocated by the backend's storage clock.
        channel: Durable channel configuration, or ``None`` before first use.
        stream: Selected current or exact-generation stream, when present.
        target_run: Requested run state, when present in the selected generation.
        active_run: Different active run state requested for ownership classification.
        matching_message: Existing evidence for the requested message ID.
        tombstone_generation: Unavailable generation selected by the query, if any.
        tombstone_reason: Stable deleted or expired disposition for that generation.
    """

    observed_at: datetime
    channel: StoredMessagingChannel | None
    stream: StoredMessagingStream | None
    target_run: StoredMessagingRun | None
    active_run: StoredMessagingRun | None
    matching_message: StoredMessageEvidence | None
    tombstone_generation: int | None = None
    tombstone_reason: MessagingCleanupReason | None = None


@dataclass(frozen=True, slots=True)
class MessagingTransition:
    """Request one framework-defined atomic Messaging state transition.

    Attributes:
        kind: Exact lifecycle transition to resolve and commit.
        transition_id: Stable correlation identity for one framework operation. Append
            idempotency remains governed by ``message_id`` and complete message evidence.
        channel: Canonical logical channel containing the transition.
        identity: Exact thread and semantic run identity.
        settings: Immutable deployment settings enforced by the transition.
        run_reference: Exact generation and ownership evidence, when required.
        codec_id: Stable codec used by preparation or append.
        after_sequence: Optional exclusive replay cursor used by preparation.
        cancellable: Whether a new or recovered producer accepts cancellation.
        recoverable: Whether a lost producer can reopen from its checkpoint.
        request_digest: Optional command-content binding compared atomically during
            prepare, before ownership or attachment can be granted.
        message_id: Stable semantic message identity for append.
        payload: Encoded message bytes for append.
        checkpoint: Recovery position committed atomically with append.
        closes_publication: Seal external publication atomically with a source message.
        opens_publication: Permit external publication after the required source start.
        final_status: Terminal producer status for settlement.
        failure: Trusted local failure associated with a failed producer.
        cleanup_generation: Exact positive generation returned by cleanup sealing and
            required when finishing that same physical cleanup.
        cleanup_reason: Explicit deletion or retention expiry reason.
        cleanup_token: Opaque cleanup ownership value returned by the begin transition,
            or ``None`` when the backend does not require cleanup ownership. The
            framework returns this value unchanged to purge and finish operations.
    """

    kind: MessagingTransitionKind
    transition_id: str
    channel: str
    identity: RunIdentity
    settings: MessagingBackendSettings
    run_reference: MessagingRunReference | None = None
    codec_id: str | None = None
    after_sequence: int | None = None
    cancellable: bool = False
    recoverable: bool = False
    request_digest: str | None = None
    message_id: str | None = None
    payload: bytes | None = None
    checkpoint: RecoveryCheckpoint | None = None
    closes_publication: bool = False
    opens_publication: bool = True
    final_status: FinalRunStatus | None = None
    failure: BaseException | None = None
    cleanup_generation: int | None = None
    cleanup_reason: MessagingCleanupReason | None = None
    cleanup_token: str | None = None


@dataclass(frozen=True, slots=True)
class MessagingTransitionResult:
    """Return the observable value produced by one committed transition.

    Attributes:
        kind: Transition kind whose result is represented.
        run_reference: Prepared owner or observer reference, when produced.
        after_sequence: Validated exclusive replay cursor, when produced.
        is_producer_owner: Whether preparation granted producer ownership.
        checkpoint: Recovery checkpoint returned to a recovered producer.
        recovered: Whether preparation replaced an expired producer owner.
        envelope: Existing or newly committed message for append.
        cancellation_requested_by_transition: Whether this transition durably created
            the cancellation request.
        cancellation_preceded_settlement: Whether a cancellation request was already
            durable when the producer claimed settlement.
        producer_ownership_confirmed: Whether the current producer fence remains valid.
        run_status: Current or terminal durable run status, when requested.
        cleanup_generation: Exact generation selected for framework-owned physical
            cleanup. This must be present when ``cleanup_required`` is true and may be
            omitted when the backend completed cleanup inside the atomic transition.
        cleanup_reason: Authoritative reason that sealed or finalized the selected
            generation. This can differ from the requested reason when concurrent
            cleanup has already established the generation's terminal disposition.
        cleanup_token: Opaque bounded-lifetime ownership value required by this
            backend's purge and finish operations, or ``None`` when no such ownership
            is required. Callers must not inspect, persist, or modify it.
        cleanup_required: Whether records private to ``cleanup_generation`` still need
            bounded physical purging before cleanup can be finished.
    """

    kind: MessagingTransitionKind
    run_reference: MessagingRunReference | None = None
    after_sequence: int | None = None
    is_producer_owner: bool | None = None
    checkpoint: RecoveryCheckpoint | None = None
    recovered: bool = False
    envelope: MessageEnvelope | None = None
    cancellation_requested_by_transition: bool | None = None
    cancellation_preceded_settlement: bool | None = None
    producer_ownership_confirmed: bool | None = None
    run_status: RunStatus | None = None
    cleanup_generation: int | None = None
    cleanup_reason: MessagingCleanupReason | None = None
    cleanup_token: str | None = None
    cleanup_required: bool = False


@dataclass(frozen=True, slots=True)
class MessagingStorageEffect:
    """Describe complete durable replacements for one resolved transition.

    A Backend applies every non-empty replacement and requested lease, retention, or
    tombstone action atomically with the transition result. An effect is declarative:
    it does not transfer ownership of clients, transactions, or external resources.

    Attributes:
        result: Public transition result returned after the effect commits.
        channel: Optional channel configuration replacement.
        stream: Optional stream metadata replacement.
        runs: Run records to replace atomically in the selected generation.
        message: Optional message evidence to insert exactly once.
        lease_action: Producer lease mutation required by the transition.
        lease_run_id: Run whose lease must be acquired, renewed, or released.
        retention_action: Terminal replay deadline mutation required by the transition.
        tombstone_reason: Final unavailable disposition to retain for stale references.
    """

    result: MessagingTransitionResult
    channel: StoredMessagingChannel | None = None
    stream: StoredMessagingStream | None = None
    runs: tuple[StoredMessagingRun, ...] = ()
    message: StoredMessageEvidence | None = None
    lease_action: MessagingLeaseAction = "none"
    lease_run_id: str | None = None
    retention_action: MessagingRetentionAction = "none"
    tombstone_reason: MessagingCleanupReason | None = None


@dataclass(frozen=True, slots=True)
class CommittedMessageQuery:
    """Select one bounded ascending page from an exact stream generation.

    Attributes:
        channel: Canonical logical channel containing the generation.
        identity: Thread and run identity used for error context.
        generation: Exact positive generation that must supply every message.
        after_sequence: Exclusive non-negative sequence already delivered.
        through_sequence: Inclusive stable upper bound, or ``None`` for current tail.
        limit: Positive maximum number of messages returned.
        stop_at_run_terminal: Whether the page must stop at the selected run's terminal
            boundary instead of continuing through the thread tail.
    """

    channel: str
    identity: RunIdentity
    generation: int
    after_sequence: int
    through_sequence: int | None
    limit: int
    stop_at_run_terminal: bool = False


@dataclass(frozen=True, slots=True)
class CommittedMessagePage:
    """Return one generation-proven page and its observed change cursor.

    Attributes:
        generation: Exact generation that supplied the page.
        latest_sequence: Greatest committed sequence observed with the page.
        messages: Ascending immutable committed messages.
        change_cursor: Message and control cursor observed with the page.
        run_state: Run status, terminal boundary, and failure evidence observed in the
            same atomic page. This must be present when the query sets
            ``stop_at_run_terminal=True`` and may otherwise be ``None``.
    """

    generation: int
    latest_sequence: int
    messages: tuple[MessageEnvelope, ...]
    change_cursor: MessagingChangeCursor
    run_state: StoredMessagingRun | None = None


@dataclass(frozen=True, slots=True)
class MessagingChangeCursor:
    """Identify the message and control state already observed by a waiter.

    Attributes:
        message_sequence: Greatest committed message sequence already observed.
        control_sequence: Greatest run-control notification already observed.
    """

    message_sequence: int
    control_sequence: int


@dataclass(frozen=True, slots=True)
class MessagingChangeWait:
    """Wait for possible change to one exact stream generation.

    A wait is advisory rather than authoritative. After any return, including a timeout
    or spurious notification, the framework reloads durable state before deciding what
    happened.

    Attributes:
        channel: Canonical logical channel containing the generation.
        identity: Thread and run identity used for error context.
        generation: Exact positive generation being observed.
        after: Message and control cursor already observed by the caller.
        timeout_seconds: Finite positive maximum wait before a permitted spurious return,
            or ``None`` when native notification safely provides all progress.
    """

    channel: str
    identity: RunIdentity
    generation: int
    after: MessagingChangeCursor
    timeout_seconds: float | None


@dataclass(frozen=True, slots=True)
class StreamGenerationPurge:
    """Request one bounded physical cleanup step for a sealed generation.

    Attributes:
        channel: Canonical logical channel containing the generation.
        identity: Thread and run identity used for error context.
        generation: Exact generation previously sealed by a cleanup transition.
        maximum_records: Positive upper bound on records removed by this call.
        cleanup_token: Opaque ownership value returned when the generation was sealed,
            or ``None`` when the backend returned no cleanup token.
    """

    channel: str
    identity: RunIdentity
    generation: int
    maximum_records: int
    cleanup_token: str | None = None


@dataclass(frozen=True, slots=True)
class StreamGenerationPurgeResult:
    """Report progress of one bounded generation cleanup step.

    Attributes:
        removed_records: Exact number of generation-private records removed by this
            call. Retried records that were already absent do not count again.
        complete: Whether no generation-private records remain at the operation's
            atomic completion point.
    """

    removed_records: int
    complete: bool


@runtime_checkable
class MessagingBackend(Protocol):
    """Persist Messaging through six storage-oriented extension operations.

    Implementations borrow all clients and external resources from the host. They must
    preserve caller cancellation, use an authoritative storage clock, return consistent
    bounded snapshots, and atomically commit each framework-defined transition. All
    methods are natively asynchronous and borrow their client, pool, transaction factory,
    and other external resources; the host remains their shutdown owner. A backend must
    never close a borrowed resource or expose storage-specific values through envelopes,
    client-safe error messages, or transition results. Provider diagnostics belong only
    in ``diagnostic_context`` and ``cause`` on package exceptions.
    """

    @property
    def messaging_settings(self) -> MessagingBackendSettings:
        """Return immutable settings shared by cooperating backend instances.

        The value must remain equal for this Backend instance's lifetime. Workers that
        share a storage namespace must expose equal limits and retention settings;
        producer and wait durations are expressed in seconds.

        Returns:
            Capacity, retention, producer ownership, and change-wait settings.

        Raises:
            MessagingBackendError: Settings cannot be read safely.
        """

        ...

    async def prepare_messaging_storage(self) -> None:
        """Create and validate backend-owned storage structures idempotently.

        Concurrent calls must converge on the same current storage shape. The method
        may create indexes, collections, or tables owned by the Backend, but it must not
        clear a namespace, rewrite existing messages, or close host-owned resources.
        Caller cancellation must propagate after any in-flight provider operation has a
        safely classified outcome; a later call must be able to retry preparation.

        Returns:
            ``None`` after the one current storage shape is ready for use.

        Raises:
            MessagingBackendError: Storage cannot be prepared or its current shape is
                inconsistent with the Messaging contract.
        """

        ...

    async def commit_messaging_transition(
        self,
        transition: MessagingTransition,
    ) -> MessagingTransitionResult:
        """Resolve and atomically commit one framework-defined state transition.

        The Backend must load the evidence required by ``transition`` and apply the
        resulting mutation under one serialization boundary. Transactional stores may
        call ``resolve_messaging_transition()`` inside that boundary. A transition may
        be retried only after a proven optimistic conflict; ``message_id`` and complete
        message evidence, not ``transition_id`` alone, govern append idempotency.

        Cancellation and transport failure must never be translated into an unverified
        success. If the provider can complete after caller cancellation, the Backend
        must settle or otherwise classify that operation before releasing call-owned
        resources, then preserve the caller's cancellation.

        Args:
            transition: Complete lifecycle intent with stable correlation evidence.

        Returns:
            Observable result committed by the exact transition.

        Raises:
            MessagingError: Current durable state rejects the transition.
            MessagingBackendError: The backend cannot commit or classify the outcome
                safely.
            TypeError: The transition or one of its required fields has the wrong type.
            ValueError: An identifier, cursor, generation, or transition option is
                outside the documented contract.
        """

        ...

    async def load_messaging_state(
        self,
        query: MessagingStateQuery,
    ) -> MessagingStateSnapshot:
        """Load one storage-clock-consistent bounded Messaging state snapshot.

        Every returned field and ``observed_at`` must belong to one logical read point;
        separate unvalidated reads are not sufficient. An absent channel, stream,
        target run, active run, or matching message is represented by ``None``. An exact
        unavailable generation is represented by matching tombstone fields rather than
        data from a replacement generation. This operation does not transfer ownership
        of any cursor, session, or connection to the framework.

        Args:
            query: Exact channel, generation, run, and optional message evidence to load.

        Returns:
            Consistent state containing only evidence requested by the query.

        Raises:
            MessagingBackendError: State is unavailable, malformed, or inconsistent.
            TypeError: The query or one of its fields has the wrong type.
            ValueError: An identifier or exact generation is invalid.
        """

        ...

    async def read_committed_messages(
        self,
        query: CommittedMessageQuery,
    ) -> CommittedMessagePage:
        """Read one bounded ordered page from an exact generation.

        The messages, change cursor, latest sequence, and any requested run boundary
        must come from one consistent read. When ``stop_at_run_terminal`` is true,
        ``run_state`` is required and its ``end_sequence`` bounds returned messages.

        Args:
            query: Generation, cursor, stable upper bound, and page limit.

        Returns:
            At most ``limit`` ascending messages, the exact observed tail and change
            cursor, and the required run boundary when requested. The Backend must not
            prefetch an unbounded page or retain a cursor after returning.

        Raises:
            InvalidCursor: The requested cursor exceeds the generation tail.
            StreamDeleted: The exact generation was explicitly deleted.
            StreamExpired: The exact generation exceeded terminal retention.
            MessagingBackendError: Message evidence is unavailable or inconsistent.
            TypeError: The query or one of its fields has the wrong type.
            ValueError: A generation, cursor, boundary, or limit is invalid.
        """

        ...

    async def wait_for_messaging_change(self, wait: MessagingChangeWait) -> None:
        """Wait until an exact generation may differ from an observed cursor.

        Spurious timeout returns are permitted. Implementations must remain responsive
        to cancellation and release any pinned connection or subscription before the
        cancellation propagates. The wait must not buffer messages, advance the supplied
        cursor, or claim producer ownership.

        Args:
            wait: Exact generation, observed cursor, and finite wait budget.

        Returns:
            ``None`` when state may have changed or the wait budget elapsed.

        Raises:
            StreamDeleted: The exact generation was explicitly deleted.
            StreamExpired: The exact generation exceeded terminal retention.
            MessagingBackendError: Change evidence cannot be observed safely.
            TypeError: The wait value or one of its fields has the wrong type.
            ValueError: A generation, cursor, or timeout is invalid.
        """

        ...

    async def purge_stream_generation(
        self,
        purge: StreamGenerationPurge,
    ) -> StreamGenerationPurgeResult:
        """Remove one bounded batch of records from a logically sealed generation.

        The operation is idempotent and must never remove shared channel configuration,
        current generation control, or the stale-reference tombstone. The framework
        serializes calls carrying the same cleanup token; different cleanup attempts
        may overlap and must not affect another generation. Cancellation must release
        call-local resources and leave any external cleanup lease bounded by its own
        expiry so another backend instance can resume.

        Args:
            purge: Exact sealed generation and physical deletion bound.

        Returns:
            Exact records removed by this call and whether the sealed generation has no
            private records left. ``complete=True`` permits the framework to submit the
            matching finish transition; it does not authorize deleting shared control.

        Raises:
            StreamDeleteConflict: The generation is not logically sealed for cleanup.
            MessagingBackendError: Cleanup cannot be performed or classified safely.
            TypeError: The purge value, bound, or required cleanup token is invalid.
            ValueError: The generation or maximum record count is not positive.
        """

        ...


from ._messaging_transition import (  # noqa: E402, I001 - avoid import cycle
    resolve_messaging_transition as resolve_messaging_transition,
)


__all__ = [
    "CommittedMessagePage",
    "CommittedMessageQuery",
    "MessagingBackend",
    "MessagingBackendSettings",
    "MessagingChangeCursor",
    "MessagingChangeWait",
    "MessagingCleanupReason",
    "MessagingLeaseAction",
    "MessagingRetentionAction",
    "MessagingRunReference",
    "MessagingStateQuery",
    "MessagingStateSnapshot",
    "MessagingStorageEffect",
    "MessagingStreamDisposition",
    "MessagingTransition",
    "MessagingTransitionKind",
    "MessagingTransitionResult",
    "StoredMessageEvidence",
    "StoredMessagingChannel",
    "StoredMessagingRun",
    "StoredMessagingStream",
    "StreamGenerationPurge",
    "StreamGenerationPurgeResult",
    "resolve_messaging_transition",
]

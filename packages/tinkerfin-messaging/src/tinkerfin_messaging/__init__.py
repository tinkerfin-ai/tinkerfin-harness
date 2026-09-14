"""Replayable messaging with TinkerFin Native and AG-UI integration."""

from __future__ import annotations

from typing import TYPE_CHECKING, Never

from .backend import ActiveRunStatus as ActiveRunStatus
from .backend import FailedRunStatus as FailedRunStatus
from .backend import FinalRunStatus as FinalRunStatus
from .backend import MemoryBackend as MemoryBackend
from .backend import RunStatus as RunStatus
from .backend import is_active_run_status as is_active_run_status
from .backend import is_failed_run_status as is_failed_run_status
from .backend import is_final_run_status as is_final_run_status
from .errors import BackendOwnershipLost as BackendOwnershipLost
from .errors import CancellationUnsupported as CancellationUnsupported
from .errors import CodecMismatch as CodecMismatch
from .errors import InvalidCursor as InvalidCursor
from .errors import MessageIdConflict as MessageIdConflict
from .errors import MessagingBackendError as MessagingBackendError
from .errors import MessagingBackendProtocolError as MessagingBackendProtocolError
from .errors import MessagingBackendTimeout as MessagingBackendTimeout
from .errors import MessagingBackendUnavailable as MessagingBackendUnavailable
from .errors import MessagingClosed as MessagingClosed
from .errors import MessagingError as MessagingError
from .errors import MessagingErrorCode as MessagingErrorCode
from .errors import MessagingNotStarted as MessagingNotStarted
from .errors import MessagingQuotaExceeded as MessagingQuotaExceeded
from .errors import MessagingSettlementTimeout as MessagingSettlementTimeout
from .errors import PublicationRejected as PublicationRejected
from .errors import RecoveryUnsupported as RecoveryUnsupported
from .errors import RunAlreadyActive as RunAlreadyActive
from .errors import RunNotFound as RunNotFound
from .errors import RunProducerFailed as RunProducerFailed
from .errors import SourceProfileMismatch as SourceProfileMismatch
from .errors import SseRenderingUnsupported as SseRenderingUnsupported
from .errors import StreamDeleteConflict as StreamDeleteConflict
from .errors import StreamDeleted as StreamDeleted
from .errors import StreamExpired as StreamExpired
from .errors import UnexpectedMessagingBackendError as UnexpectedMessagingBackendError
from .limits import MessagingLimits as MessagingLimits
from .messaging import CancelCallback as CancelCallback
from .messaging import CancelContext as CancelContext
from .messaging import CommittedCallback as CommittedCallback
from .messaging import MessageChannel as MessageChannel
from .messaging import MessageSubscription as MessageSubscription
from .messaging import Messaging as Messaging
from .models import DecodedMessage as DecodedMessage
from .models import MessageEnvelope as MessageEnvelope
from .models import RecoverableMessage as RecoverableMessage
from .models import RecoveryCheckpoint as RecoveryCheckpoint
from .protocols import MessageCodec as MessageCodec
from .protocols import MessageCodecInputSource as MessageCodecInputSource
from .protocols import MessagePublicationPolicy as MessagePublicationPolicy
from .protocols import MessageSource as MessageSource
from .protocols import ProfiledMessageSource as ProfiledMessageSource
from .protocols import RecoverableSource as RecoverableSource
from .protocols import SseRenderer as SseRenderer
from .retention import MessagingRetentionPolicy as MessagingRetentionPolicy
from .sources import CancellableMessageSource as CancellableMessageSource
from .sources import DeferredMessageSource as DeferredMessageSource
from .sources import FiniteMessageSource as FiniteMessageSource
from .sources import MessageSourceBinding as MessageSourceBinding
from .sources import ProfiledDeferredMessageSource as ProfiledDeferredMessageSource
from .sources import map_source as map_source
from .sse import parse_sse_event_id as parse_sse_event_id

if TYPE_CHECKING:
    from ._agui_channel import AgUiChannel as AgUiChannel
    from .agui import AgUiCodec as AgUiCodec
    from .native import NativeStreamPart as NativeStreamPart
    from .native import NativeStreamPartCodec as NativeStreamPartCodec
    from .redis import RedisBackend as RedisBackend
    from .sqlalchemy import SqlAlchemyBackend as SqlAlchemyBackend

__all__ = [
    "ActiveRunStatus",
    "AgUiChannel",
    "AgUiCodec",
    "BackendOwnershipLost",
    "CancelCallback",
    "CancelContext",
    "CancellableMessageSource",
    "CancellationUnsupported",
    "CodecMismatch",
    "CommittedCallback",
    "DecodedMessage",
    "DeferredMessageSource",
    "FailedRunStatus",
    "FinalRunStatus",
    "FiniteMessageSource",
    "InvalidCursor",
    "MemoryBackend",
    "MessageChannel",
    "MessageCodec",
    "MessageCodecInputSource",
    "MessageEnvelope",
    "MessageIdConflict",
    "MessagePublicationPolicy",
    "MessageSource",
    "MessageSourceBinding",
    "MessageSubscription",
    "Messaging",
    "MessagingBackendError",
    "MessagingBackendProtocolError",
    "MessagingBackendTimeout",
    "MessagingBackendUnavailable",
    "MessagingClosed",
    "MessagingError",
    "MessagingErrorCode",
    "MessagingLimits",
    "MessagingNotStarted",
    "MessagingQuotaExceeded",
    "MessagingRetentionPolicy",
    "MessagingSettlementTimeout",
    "NativeStreamPart",
    "NativeStreamPartCodec",
    "ProfiledDeferredMessageSource",
    "ProfiledMessageSource",
    "PublicationRejected",
    "RecoverableMessage",
    "RecoverableSource",
    "RecoveryCheckpoint",
    "RecoveryUnsupported",
    "RedisBackend",
    "RunAlreadyActive",
    "RunNotFound",
    "RunProducerFailed",
    "RunStatus",
    "SourceProfileMismatch",
    "SqlAlchemyBackend",
    "SseRenderer",
    "SseRenderingUnsupported",
    "StreamDeleteConflict",
    "StreamDeleted",
    "StreamExpired",
    "UnexpectedMessagingBackendError",
    "is_active_run_status",
    "is_failed_run_status",
    "is_final_run_status",
    "map_source",
    "parse_sse_event_id",
]


def _raise_missing_extra(
    error: ModuleNotFoundError,
    *,
    symbol: str,
    extra: str,
    packages: tuple[str, ...],
) -> Never:
    missing = error.name
    if missing is None or not any(
        missing == package or missing.startswith(f"{package}.") for package in packages
    ):
        raise error
    raise ImportError(
        f"{symbol} requires optional dependencies from the {extra!r} extra; "
        f'install them with: pip install "tinkerfin-messaging[{extra}]"'
    ) from error


def __getattr__(name: str) -> object:
    """Load optional integrations only when their public symbol is requested."""

    if name == "AgUiChannel":
        try:
            from ._agui_channel import AgUiChannel
        except ModuleNotFoundError as error:
            _raise_missing_extra(error, symbol=name, extra="agui", packages=("ag_ui",))
        globals()[name] = AgUiChannel
        return AgUiChannel
    if name == "AgUiCodec":
        try:
            from .agui import AgUiCodec
        except ModuleNotFoundError as error:
            _raise_missing_extra(
                error,
                symbol=name,
                extra="agui",
                packages=("ag_ui",),
            )

        globals()["AgUiCodec"] = AgUiCodec
        return globals()[name]
    if name in {"NativeStreamPart", "NativeStreamPartCodec"}:
        try:
            from .native import NativeStreamPart, NativeStreamPartCodec
        except ModuleNotFoundError as error:
            _raise_missing_extra(
                error,
                symbol=name,
                extra="native",
                packages=("tinkerfin_native_stream",),
            )

        globals()["NativeStreamPart"] = NativeStreamPart
        globals()["NativeStreamPartCodec"] = NativeStreamPartCodec
        return globals()[name]
    if name == "RedisBackend":
        try:
            from .redis import RedisBackend
        except ModuleNotFoundError as error:
            _raise_missing_extra(
                error,
                symbol=name,
                extra="redis",
                packages=("redis",),
            )

        globals()[name] = RedisBackend
        return RedisBackend
    if name == "SqlAlchemyBackend":
        try:
            from .sqlalchemy import SqlAlchemyBackend
        except ModuleNotFoundError as error:
            _raise_missing_extra(
                error,
                symbol=name,
                extra="sqlalchemy",
                packages=("sqlalchemy", "tinkerfin_sqlalchemy"),
            )
        globals()[name] = SqlAlchemyBackend
        return SqlAlchemyBackend
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

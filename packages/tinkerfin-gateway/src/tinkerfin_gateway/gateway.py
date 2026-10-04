"""Compose authorized Runtime commands with durable Messaging ownership."""

from __future__ import annotations

from collections.abc import Collection
from datetime import datetime

from ag_ui.core import BaseEvent, RunErrorEvent, RunFinishedEvent, RunStartedEvent
from langgraph.typing import ContextT

from tinkerfin import AgentRuntime
from tinkerfin_contracts import RunIdentity
from tinkerfin_messaging import MessageSubscription, Messaging
from tinkerfin_notifications import Notifications, NotificationScope

from ._cleanup import _close_owned
from ._failures import _select_failure
from .commands import CompactRun, ResumeRun, RunCommand, StartRun, _freeze
from .lifecycle import (
    CommittedRunEvent,
    CommittedRunObserver,
    ResumeSettlement,
    RunAcceptance,
    RunPresentation,
    RunRegistration,
)
from .notifications import NotificationAuthorization, NotificationStream
from .run import GatewayRun


class Gateway:
    """Accept run commands and provide durable output and advisory notifications.

    Messaging and Notifications are borrowed, explicitly started host resources.
    The Gateway opens no server, owns no database, and does not cache Runtime
    objects. Hosts authorize identities, model/tool access, and business inputs.
    Command identity conflicts are checked atomically by Messaging for as long as
    the run record is retained. Deletion or retention expiry ends that guarantee.
    """

    def __init__(
        self,
        *,
        messaging: Messaging,
        notifications: Notifications,
        name: str = "gateway-runs",
    ) -> None:
        """Bind shared resources and the stable durable output channel name."""
        self._channel = messaging.agui_channel(name=name)
        self._notifications = notifications

    def run(self, identity: RunIdentity) -> GatewayRun:
        """Bind an authorized identity for replay, status, or cancellation.

        This does not create a Runtime, query storage, or execute a command.
        """
        if not isinstance(identity, RunIdentity):
            raise TypeError("identity must be a RunIdentity")
        return GatewayRun(self._channel, identity)

    async def notifications(
        self,
        *,
        scopes: Collection[NotificationScope],
        topics: Collection[str] | None = None,
        expires_at: datetime | None = None,
        authorize: NotificationAuthorization | None = None,
    ) -> NotificationStream:
        """Prepare a browser feed for scopes derived from host authorization.

        Never accept namespace or owner filters directly from an untrusted client.
        Supply a fixed session expiry and a short, application-owned authorization
        check for authenticated long-lived feeds. No request transaction should
        remain open while consuming the returned stream.
        """
        return await NotificationStream._open(
            self._notifications,
            scopes=scopes,
            topics=topics,
            expires_at=expires_at,
            authorize=authorize,
        )

    async def start(
        self,
        runtime: AgentRuntime[ContextT],
        command: StartRun,
        *,
        registration: RunRegistration | None = None,
        on_committed: CommittedRunObserver | None = None,
        presentation: RunPresentation | None = None,
    ) -> GatewayRun:
        """Accept a new conversation without requiring output consumption.

        The producer remains Messaging-owned after this method returns. Repeating
        the same command attaches to its retained run; changed content raises
        RunRequestConflict. Runtime and Messaging failures retain their public
        error families. Registration follows the contract of ``stream``.
        """
        if not isinstance(command, StartRun):
            raise TypeError("start requires StartRun")
        subscription = await self.stream(
            runtime,
            command,
            registration=registration,
            on_committed=on_committed,
            presentation=presentation,
        )
        await subscription.aclose()
        return self.run(runtime.run_identity(command.thread_id, command.run_id))

    async def resume(
        self,
        runtime: AgentRuntime[ContextT],
        command: ResumeRun,
        *,
        registration: RunRegistration | None = None,
        settlement: ResumeSettlement | None = None,
        on_committed: CommittedRunObserver | None = None,
        presentation: RunPresentation | None = None,
    ) -> GatewayRun:
        """Accept human decisions; settlement independently confirms saved state."""
        if not isinstance(command, ResumeRun):
            raise TypeError("resume requires ResumeRun")
        subscription = await self.stream(
            runtime,
            command,
            registration=registration,
            settlement=settlement,
            on_committed=on_committed,
            presentation=presentation,
        )
        await subscription.aclose()
        return self.run(runtime.run_identity(command.thread_id, command.run_id))

    async def compact(
        self,
        runtime: AgentRuntime[ContextT],
        command: CompactRun,
        *,
        registration: RunRegistration | None = None,
        on_committed: CommittedRunObserver | None = None,
        presentation: RunPresentation | None = None,
    ) -> GatewayRun:
        """Accept context compression under the same ownership and replay contract."""
        if not isinstance(command, CompactRun):
            raise TypeError("compact requires CompactRun")
        subscription = await self.stream(
            runtime,
            command,
            registration=registration,
            on_committed=on_committed,
            presentation=presentation,
        )
        await subscription.aclose()
        return self.run(runtime.run_identity(command.thread_id, command.run_id))

    async def stream(
        self,
        runtime: AgentRuntime[ContextT],
        command: RunCommand,
        *,
        after: int | None = None,
        registration: RunRegistration | None = None,
        settlement: ResumeSettlement | None = None,
        on_committed: CommittedRunObserver | None = None,
        presentation: RunPresentation | None = None,
    ) -> MessageSubscription[BaseEvent]:
        """Accept once and return the original typed admission subscription.

        Args:
            runtime: Host-authorized Runtime already bound to its namespace.
            command: Complete messages, decisions, or compression identity.
            after: Last applied sequence; omission starts at the admission tail.
            registration: Optional host reservation confirmed for new or existing
                admission, released only when this submission was not accepted.
            settlement: Confirmed saved/not-saved decisions, only for ResumeRun.
            on_committed: Producer-only main lifecycle observation after storage.
                Failures cannot undo committed output and are isolated by Messaging.
            presentation: Static additional start attributes and cancellation text.

        Returns:
            Caller-owned subscription, even when never consumed. Close it or use
            its async context. Closing detaches without cancelling execution.
            Admission does not wait for workspace readiness or Graph preparation.

        Raises:
            RunRequestConflict: Retained identity is bound to different content.
            MessagingError: Admission, cursor, or durable delivery is unavailable.
            TinkerFinError: Runtime admission cannot satisfy its contract.
            ValueError: Command, presentation, or settlement selection is invalid.
            BaseException: Required host registration or settlement fails.
        """
        try:
            if not isinstance(command, StartRun | ResumeRun | CompactRun):
                raise TypeError("command must be StartRun, ResumeRun, or CompactRun")
            command, digest = _freeze(command, runtime.namespace)
            identity = runtime.run_identity(command.thread_id, command.run_id)
            style = (
                None
                if presentation is None
                else RunPresentation.model_validate(
                    presentation.model_dump(mode="python")
                )
            )
            if settlement is not None and not isinstance(command, ResumeRun):
                raise ValueError("settlement requires a resume command")
            if isinstance(command, CompactRun):
                source = runtime.agui.open_compaction(
                    thread_id=command.thread_id, run_id=command.run_id
                )
            elif isinstance(command, StartRun):
                source = runtime.open_agui_run(
                    thread_id=command.thread_id,
                    run_id=command.run_id,
                    messages=command.messages,
                    parent_run_id=command.parent_run_id,
                    mode=command.mode,
                    config={"configurable": command.parameters},
                )
            else:
                source = runtime.open_agui_run(
                    thread_id=command.thread_id,
                    run_id=command.run_id,
                    resume=command.resume,
                    parent_run_id=command.parent_run_id,
                    mode=command.mode,
                    config={"configurable": command.parameters},
                    on_resume_saved=None if settlement is None else settlement.saved,
                    on_resume_not_saved=None
                    if settlement is None
                    else settlement.not_saved,
                )
        except BaseException as error:
            if registration is not None:
                try:
                    await _close_owned(registration.release())
                except BaseException as cleanup_error:  # noqa: BLE001 - preserve control and independent cleanup failures
                    raise _select_failure(error, cleanup_error)
            raise

        prepared_new = False

        async def confirm_new() -> None:
            nonlocal prepared_new
            prepared_new = True
            if registration is not None:
                await registration.confirm(RunAcceptance(identity, "new"))

        async def confirm_existing() -> None:
            if not prepared_new and registration is not None:
                await registration.confirm(RunAcceptance(identity, "existing"))

        async def observe(
            event: RunStartedEvent | RunFinishedEvent | RunErrorEvent,
        ) -> None:
            if on_committed is not None:
                await on_committed(CommittedRunEvent(identity, event, source.error))

        # Messaging owns the candidate after this call, including failed admission
        # and cancelled preflight. Its exact delivery decision protects registration
        # from being rolled back after an accepted source or replay attachment.
        return await self._channel.open_run(
            source,
            after=after,
            request_digest=digest,
            on_source_ready=confirm_new,
            on_subscribed=confirm_existing,
            on_delivery_not_started=None
            if registration is None
            else registration.release,
            transform_event=None
            if style is None
            else lambda event: style._apply(event, identity),
            on_run_started=observe,
            on_run_finished=observe,
        )


__all__ = ["Gateway"]

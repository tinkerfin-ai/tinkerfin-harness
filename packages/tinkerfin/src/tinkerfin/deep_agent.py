"""Deep Agents graph definitions and request-scoped runtimes."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import (
    AsyncGenerator,
    AsyncIterator,
    Awaitable,
    Callable,
    Iterator,
    Mapping,
    Sequence,
)
from dataclasses import replace
from functools import partial, wraps
from typing import (
    TYPE_CHECKING,
    Any,
    ParamSpec,
    Protocol,
    TypeVar,
    cast,
)

from deepagents.backends.protocol import BackendProtocol
from langchain.agents.middleware.types import InputAgentState
from langchain_core.callbacks import BaseCallbackManager
from langchain_core.runnables import Runnable, RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.types import Command, StateSnapshot
from langgraph.typing import ContextT

from tinkerfin_contracts import PreparedWorkspace, RunIdentity, Workspace
from tinkerfin_native_stream import NativeRuntimeInterrupt, NativeValidatedStreamPart

from ._agent_construction import create_agent_graph
from ._agent_preparation import prepare_agent_spec, validate_agent_spec
from ._agent_spec import AgentSpec
from ._agui_lineage_state import (
    RESUME_METADATA_KEY,
    bind_checkpoint_run,
)
from ._checkpoint import NamespaceCheckpointer
from ._failure_evidence import retain_failure, select_failure
from ._observation import RuntimeObservationHub, native_input_kind, source_context
from ._optional_dependencies import require_agui
from ._run_owner import in_native_owner, native_iterator_cleanup
from ._run_resources import RunResources
from ._state_schema import compose_deep_agent_base_schema
from ._store import NamespaceStore
from ._store_backend import async_store_backend
from ._tasks import OwnedOperationFailures, join_task, run_sync_owned
from .errors import TinkerFinLifecycleError
from .native_driver import NativeStreamDriver
from .plan._config import (
    AgentMode,
    resolve_agent_mode,
)

if TYPE_CHECKING:
    from .agui_resume import (
        AgUiResumeBinding,
        AgUiResumeCheckpointObserver,
        AgUiResumeNotSavedObserver,
        AgUiResumeRequest,
    )
    from .runtime import (
        AgentRuntime,
        AgUiEventStream,
        EventObserver,
        NativeGraphRunStream,
        PartObserver,
        TinkerFin,
    )

CreateP = ParamSpec("CreateP")
GraphT = TypeVar("GraphT")
AstreamT = TypeVar("AstreamT", bound=Callable[..., object])
_GraphInput = InputAgentState | Command[object] | None
_NativeAstream = Callable[..., AsyncIterator[Mapping[str, object]]]
_NativeAstreamFactory = Callable[[], _NativeAstream | Awaitable[_NativeAstream]]


class _PlanNativeGraph(Protocol):
    checkpointer: object

    def astream(
        self,
        *args: object,
        **kwargs: object,
    ) -> AsyncIterator[Mapping[str, object]]: ...

    async def aget_state(
        self,
        config: RunnableConfig,
        *,
        subgraphs: bool = False,
    ) -> StateSnapshot: ...

    async def aupdate_state(
        self,
        config: RunnableConfig,
        values: Mapping[str, object],
        as_node: str | None = None,
        task_id: str | None = None,
    ) -> RunnableConfig: ...


def _install_call_handler(
    bound: inspect.BoundArguments,
    observation: RuntimeObservationHub,
) -> None:
    """Append native sync/async callbacks without taking caller callback ownership."""

    if not observation.enabled:
        return
    raw_config = bound.arguments.get("config")
    if raw_config is None:
        config: dict[str, object] = {}
    elif isinstance(raw_config, Mapping):
        config = dict(cast(Mapping[str, object], raw_config))
    else:
        raise TypeError("bound Graph config must be a mapping or None")
    callbacks = config.get("callbacks")
    if callbacks is None:
        configured_callbacks: object = list(observation.call_handlers)
    elif isinstance(callbacks, list):
        configured_callbacks = [
            *cast(list[object], callbacks),
            *observation.call_handlers,
        ]
    elif isinstance(callbacks, BaseCallbackManager):
        manager = callbacks.copy()
        for call_handler in observation.call_handlers:
            manager.add_handler(call_handler, inherit=True)
        configured_callbacks = manager
    else:
        raise TypeError("config callbacks must be a callback list or manager")
    config["callbacks"] = configured_callbacks
    bound.arguments["config"] = config


async def _close_owned_iterator(
    source: object,
    *,
    primary: BaseException | None,
    task_name: str,
) -> None:
    """Close one request-owned iterator without hiding execution or cancellation."""

    close = getattr(source, "aclose", None)
    if not callable(close):
        return

    async def close_source() -> None:
        await cast(Callable[[], Awaitable[object]], close)()

    try:
        if in_native_owner():
            # The lifetime owner defers further controller cancellation during
            # cleanup. Keep generator-local ContextVar tokens in their own Task.
            with native_iterator_cleanup():
                await close_source()
        else:
            task = asyncio.create_task(close_source(), name=task_name)
            await join_task(task)
    except BaseException as close_error:
        current = asyncio.current_task()
        if isinstance(close_error, asyncio.CancelledError) and (
            current is not None and current.cancelling()
        ):
            if primary is not None and not isinstance(primary, GeneratorExit):
                retain_failure(
                    close_error, primary, label="Iterator processing also failed"
                )
            raise
        if isinstance(primary, GeneratorExit):
            # GeneratorExit is the close signal, not the execution failure that owns
            # cleanup evidence. Surface the close failure to the enclosing Runtime so
            # it can attach that evidence to its actual processing error.
            raise close_error.with_traceback(close_error.__traceback__)
        if primary is None:
            raise
        if isinstance(close_error, asyncio.CancelledError):
            # A source may raise cancellation itself without its consumer being
            # cancelled. Keep the execution failure in that case; see the AG-UI
            # conversion-error regression in test_stateless_agui.
            retain_failure(
                primary, close_error, label="Owned iterator cleanup also failed"
            )
        else:
            selected = select_failure(
                primary, close_error, label="Owned iterator cleanup also failed"
            )
            if selected is not primary:
                raise selected


class _StreamClaim:
    __slots__ = ("_claimed",)

    def __init__(self) -> None:
        self._claimed = False

    def ensure_available(self) -> None:
        if self._claimed:
            raise TinkerFinLifecycleError(
                "a TinkerFin run can create only one object stream"
            )

    def claim(self) -> None:
        self.ensure_available()
        self._claimed = True


class _AsyncAstreamResolver:
    """Resolve one asynchronous Graph stream without spawning an orphan task.

    The first caller owns construction in its current cancellation scope. Concurrent
    callers await the same protected Future, while process-control exceptions propagate
    directly through the initiating call instead of escaping from a child task.
    """

    __slots__ = ("_factory", "_future", "_owner")

    def __init__(self, factory: _NativeAstreamFactory) -> None:
        self._factory = factory
        self._future: asyncio.Future[_NativeAstream] | None = None
        self._owner: asyncio.Task[object] | None = None

    async def resolve(self) -> _NativeAstream:
        """Return the one callable or propagate its retained construction outcome."""

        future = self._future
        current = cast(asyncio.Task[object] | None, asyncio.current_task())
        if current is None:  # pragma: no cover - async methods execute in a Task
            raise TinkerFinLifecycleError("Graph construction requires an asyncio task")
        if future is not None:
            if future.done():
                return future.result()
            if self._owner is current:
                raise TinkerFinLifecycleError("recursive Graph construction is invalid")
            return await asyncio.shield(future)

        future = asyncio.get_running_loop().create_future()
        self._future = future
        self._owner = current
        try:
            value = self._factory()
            if inspect.isawaitable(value):
                value = await value
            if not callable(value):
                raise TypeError("Graph factory must resolve to a callable astream")
            resolved = cast(_NativeAstream, value)
            future.set_result(resolved)
            return resolved
        except BaseException as error:
            future.set_exception(error)
            # The initiating caller re-raises the same outcome. Mark the Future as
            # observed so a request with no concurrent waiter cannot emit a warning.
            try:
                future.exception()
            except asyncio.CancelledError:
                pass
            raise
        finally:
            self._owner = None


class _ResumeInitializationGuard:
    """Settle one host claim only while the resume marker is not durable.

    The guard is request-scoped and borrowed by the native stream's retained close
    task. Marking the guard prepared permanently fences release for that request;
    prepared and accepted retries must keep the host claim until the durable checkpoint
    callback completes. A pre-marker close, failure, or cancellation invokes the host
    callback at most once and waits for it independently of caller cancellation.
    """

    __slots__ = (
        "_callback",
        "_marker_probe",
        "_marker_readable",
        "_stage_started",
    )

    def __init__(
        self,
        callback: AgUiResumeNotSavedObserver | None,
        marker_probe: Callable[[], Awaitable[bool]] | None,
    ) -> None:
        self._callback = callback
        self._marker_probe = marker_probe
        self._marker_readable = False
        self._stage_started = False

    def mark_stage_started(self) -> None:
        """Require a saver probe before releasing after a stage attempt."""

        self._stage_started = True

    def mark_marker_readable(self) -> None:
        """Prevent a durable prepared or accepted resume from being released."""

        self._marker_readable = True

    async def settle_unprepared(self) -> None:
        """Invoke and consume the host callback only before marker durability."""

        if self._marker_readable:
            return
        probe = self._marker_probe
        if probe is not None and await probe():
            self._marker_readable = True
            return
        callback = self._callback
        self._callback = None
        if callback is None:
            return

        async def settle() -> None:
            await callback()

        task = asyncio.create_task(
            settle(),
            name="tinkerfin-resume-initialization-settlement",
        )
        await join_task(task)


def _wrap_native_astream(
    astream: AstreamT,
    *,
    tinkerfin: AgentRuntime[Any],
    identity: RunIdentity,
    mode: AgentMode,
    private_state_keys: frozenset[str],
    on_part: PartObserver[Mapping[str, object]] | None,
    compaction: bool = False,
) -> AstreamT:
    """Bind one Definition stream to its Profile, identity, and Observer lifecycle.

    Invocation validation happens before the single-use claim and before coordinator or
    Observer side effects. The wrapper preserves the installed callable signature while
    returning a TinkerFin-owned stream that keeps raw objects public and canonical
    frames private to downstream integrations.
    """

    signature = inspect.signature(astream)
    deferred_resolver = getattr(astream, "_tinkerfin_resolve_astream", None)
    claim = _StreamClaim()

    @wraps(astream)
    def wrapped(*args: object, **kwargs: object) -> NativeGraphRunStream:
        bound = tinkerfin._bind_native_invocation(
            signature,
            args,
            kwargs,
            identity=identity,
        )
        claim.claim()
        graph_input = bound.arguments.get("input")
        raw_config = bound.arguments.get("config", {})
        input_kind = "compaction" if compaction else native_input_kind(graph_input)
        context = source_context(
            identity=identity,
            runtime_profile=tinkerfin._runtime_profile.profile_id,
            input_kind=input_kind,
            parent_run_id=None,
            mode=mode,
            graph_input=cast(object, graph_input),
            config=raw_config,
            private_state_keys=private_state_keys,
            call_tracking_enabled=True,
        )
        observation = tinkerfin._observation_hub(context)
        _install_call_handler(bound, observation)
        bound.arguments["config"] = bind_checkpoint_run(
            cast(RunnableConfig, bound.arguments.get("config", {})),
            identity=identity,
            parent_run_id=None,
            runtime_profile=tinkerfin._runtime_profile.profile_id,
        )

        def source() -> AsyncIterator[Mapping[str, object]]:
            return cast(
                AsyncIterator[Mapping[str, object]],
                astream(*bound.args, **bound.kwargs),
            )

        return tinkerfin._run_native(
            source,
            identity=identity,
            on_part=on_part,
            observation=observation,
            source_preflight=(
                cast(Callable[[], Awaitable[object]], deferred_resolver)
                if callable(deferred_resolver)
                else None
            ),
        )

    return cast(AstreamT, wrapped)


def _create_graph_agui_stream(
    *,
    native_astream_factory: _NativeAstreamFactory,
    bound: inspect.BoundArguments,
    claim: _StreamClaim,
    tinkerfin: AgentRuntime[Any],
    identity: RunIdentity,
    parent_run_id: str | None,
    mode: AgentMode,
    on_part: PartObserver[Mapping[str, object]] | None,
    timeout: float | None,
    settlement_timeout: float | None,
    expose_reasoning_events: bool,
    expose_subagent_events: bool,
    private_state_keys: frozenset[str],
    checkpointer: object | None,
    resume: AgUiResumeBinding | None,
    on_resume_checkpointed: AgUiResumeCheckpointObserver | None,
    on_resume_not_saved: (AgUiResumeNotSavedObserver | None),
    on_event: EventObserver | None,
    compaction: bool = False,
) -> AgUiEventStream:
    """Create one lazy AG-UI stream from an already bound Graph call."""

    require_agui()
    from ._agui_lineage import (
        agui_resume_marker_is_durable,
        bind_agui_lineage,
        stage_agui_resume_intent,
    )

    claim.ensure_available()
    if resume is not None:
        durability = bound.arguments.get("durability")
        if durability not in (None, "sync"):
            raise TinkerFinLifecycleError("AG-UI resume requires durability='sync'")
    raw_config = bound.arguments.get("config", {})
    raw_input = (
        bound.arguments.get("input")
        if resume is None
        else resume.model_dump(mode="json", by_alias=True)
    )
    input_kind = (
        resume.mode
        if resume is not None
        else (
            "compaction"
            if compaction
            else "branch"
            if parent_run_id is not None
            else "ordinary"
        )
    )
    context = source_context(
        identity=identity,
        runtime_profile=tinkerfin._runtime_profile.profile_id,
        input_kind=input_kind,
        parent_run_id=parent_run_id,
        mode=mode,
        graph_input=raw_input,
        config=raw_config,
        private_state_keys=private_state_keys,
        call_tracking_enabled=True,
        resume=() if resume is None else resume._observation_summaries(),
    )
    observation = tinkerfin._observation_hub(context)
    _install_call_handler(bound, observation)
    resolver = _AsyncAstreamResolver(native_astream_factory)
    resolve_native_astream = resolver.resolve

    async def resume_marker_is_readable() -> bool:
        if resume is None:
            return False
        if checkpointer is not None:
            return await agui_resume_marker_is_durable(
                checkpointer,
                identity=identity,
                runtime_profile=tinkerfin._runtime_profile,
            )
        native_astream = await resolve_native_astream()
        raw_config = bound.arguments.get("config")
        if not isinstance(raw_config, Mapping):
            raise TypeError("bound Graph config must be a mapping")
        resolution = await bind_agui_lineage(
            native_astream,
            cast(RunnableConfig, raw_config),
            identity=identity,
            parent_run_id=parent_run_id,
            runtime_profile=tinkerfin._runtime_profile,
            resume=resume,
        )
        return resolution.resume_phase in {"prepared", "accepted"}

    resume_guard = _ResumeInitializationGuard(
        on_resume_not_saved,
        resume_marker_is_readable if resume is not None else None,
    )

    async def record_resume_checkpoint(effective_parent_run_id: str | None) -> None:
        assert resume is not None
        checkpoint = resume._checkpoint(
            identity=identity,
            parent_run_id=effective_parent_run_id,
        )
        await observation.resume_checkpointed(
            marker_id=checkpoint.marker_id,
            native_interrupt_ids=checkpoint.native_interrupt_ids,
        )
        if on_resume_checkpointed is not None:
            await on_resume_checkpointed(checkpoint)

    async def source(
        native_astream: Callable[..., AsyncIterator[Mapping[str, object]]],
    ) -> AsyncIterator[Mapping[str, object]]:
        raw_config = bound.arguments.get("config")
        if not isinstance(raw_config, Mapping):
            raise TypeError("bound Graph config must be a mapping")
        resolution = await bind_agui_lineage(
            native_astream,
            cast(RunnableConfig, raw_config),
            identity=identity,
            parent_run_id=parent_run_id,
            runtime_profile=tinkerfin._runtime_profile,
            resume=resume,
        )
        if resume is not None:
            if resolution.resume_phase == "unstaged":
                resume_guard.mark_stage_started()
                resolution = await stage_agui_resume_intent(
                    native_astream,
                    resolution,
                    identity=identity,
                    runtime_profile=tinkerfin._runtime_profile,
                    resume=resume,
                )
            if resolution.resume_phase not in {"prepared", "accepted"}:
                raise TinkerFinLifecycleError(
                    "resume did not resolve to a durable intent"
                )
            # Both phases are proven saver-readable by bind/stage. From this point a
            # retry must preserve the claim and re-deliver the exact checkpoint callback.
            resume_guard.mark_marker_readable()
            bound.arguments["config"] = resolution.config
            bound.arguments["input"] = resume._invocation_command(
                resolution.missing_interrupt_ids
            )
            bound.arguments["durability"] = "sync"
            await record_resume_checkpoint(resolution.parent_run_id)
        else:
            bound.arguments["config"] = resolution.config
            raw_input = bound.arguments.get("input")
            if not isinstance(raw_input, Mapping):
                raise TypeError("ordinary AG-UI Graph input must be a mapping")
            bound.arguments["input"] = {
                **cast(Mapping[str, object], raw_input),
            }
        native = native_astream(*bound.args, **bound.kwargs)
        if not isinstance(native, AsyncIterator):
            raise TypeError("source_factory must return an async iterator")
        primary: BaseException | None = None
        try:
            async for part in native:
                yield part
        except BaseException as error:
            primary = error
            raise
        finally:
            await _close_owned_iterator(
                native,
                primary=primary,
                task_name="tinkerfin-deep-agent-native-close",
            )

    source_created = False

    async def deferred_source() -> AsyncIterator[Mapping[str, object]]:
        native_astream = await resolve_native_astream()
        opened = source(native_astream)
        primary: BaseException | None = None
        try:
            async for part in opened:
                yield part
        except BaseException as error:
            primary = error
            raise
        finally:
            # ``async for`` does not close a nested iterator when this outer source is
            # closed. The request owns that iterator and must settle it before Runtime
            # cancellation or terminal delivery can complete.
            await _close_owned_iterator(
                opened,
                primary=primary,
                task_name="tinkerfin-deep-agent-source-close",
            )

    def source_factory() -> AsyncIterator[Mapping[str, object]]:
        nonlocal source_created
        if source_created:
            raise TinkerFinLifecycleError("resume Graph source was already constructed")
        source_created = True
        return deferred_source()

    stream = tinkerfin._run_agui(
        source_factory,
        identity=identity,
        parent_run_id=parent_run_id,
        on_part=on_part,
        timeout=timeout,
        settlement_timeout=settlement_timeout,
        expose_reasoning_events=expose_reasoning_events,
        expose_subagent_events=expose_subagent_events,
        prior_tool_call_ids=(
            frozenset() if resume is None else frozenset(resume.prior_tool_call_ids)
        ),
        private_state_keys=private_state_keys,
        on_event=on_event,
        observation=observation,
        on_settle=(resume_guard.settle_unprepared if resume is not None else None),
        source_preflight=resolve_native_astream,
    )
    claim.claim()
    return stream


def _wrap_agui_astream(
    astream: AstreamT,
    *,
    tinkerfin: AgentRuntime[Any],
    identity: RunIdentity,
    parent_run_id: str | None,
    mode: AgentMode,
    on_part: PartObserver[Mapping[str, object]] | None,
    timeout: float | None,
    settlement_timeout: float | None,
    expose_reasoning_events: bool,
    expose_subagent_events: bool,
    private_state_keys: frozenset[str],
    on_event: EventObserver | None,
    compaction: bool = False,
) -> AstreamT:
    """Preserve the Graph input signature for an ordinary AG-UI run."""

    signature = inspect.signature(astream)
    claim = _StreamClaim()
    native_astream = cast(_NativeAstream, astream)
    deferred_resolver = getattr(astream, "_tinkerfin_resolve_astream", None)
    native_astream_factory = (
        cast(_NativeAstreamFactory, deferred_resolver)
        if callable(deferred_resolver)
        else lambda: native_astream
    )

    @wraps(astream)
    def wrapped(*args: object, **kwargs: object) -> AgUiEventStream:
        graph_input = args[0] if args else kwargs.get("input")
        if graph_input is None or isinstance(graph_input, Command):
            raise ValueError(
                "ordinary AG-UI runs require Graph state input; use resume= for resume"
            )
        bound = tinkerfin._bind_native_invocation(
            signature,
            args,
            kwargs,
            identity=identity,
        )
        return _create_graph_agui_stream(
            native_astream_factory=native_astream_factory,
            bound=bound,
            claim=claim,
            tinkerfin=tinkerfin,
            identity=identity,
            parent_run_id=parent_run_id,
            mode=mode,
            on_part=on_part,
            timeout=timeout,
            settlement_timeout=settlement_timeout,
            expose_reasoning_events=expose_reasoning_events,
            expose_subagent_events=expose_subagent_events,
            private_state_keys=private_state_keys,
            checkpointer=None,
            resume=None,
            on_resume_checkpointed=None,
            on_resume_not_saved=None,
            on_event=on_event,
            compaction=compaction,
        )

    return cast(AstreamT, wrapped)


def _resume_astream_signature(signature: inspect.Signature) -> inspect.Signature:
    """Remove Graph input and make config keyword-only for a resume Runtime."""

    parameters = list(signature.parameters.values())
    if parameters and parameters[0].name == "self":
        parameters.pop(0)
    if not parameters or parameters[0].name != "input":
        raise TypeError("Graph astream must expose its input parameter")
    parameters.pop(0)
    parameters = [
        (
            parameter.replace(kind=inspect.Parameter.KEYWORD_ONLY)
            if parameter.name == "config"
            else parameter
        )
        for parameter in parameters
    ]
    return signature.replace(parameters=parameters)


def _flatten_bound_options(
    bound: inspect.BoundArguments,
    signature: inspect.Signature,
) -> dict[str, object]:
    """Restore keyword options after validating the public resume signature."""

    options: dict[str, object] = {}
    for name, value in bound.arguments.items():
        parameter = signature.parameters[name]
        if parameter.kind is inspect.Parameter.VAR_KEYWORD:
            options.update(cast(Mapping[str, object], value))
        else:
            options[name] = value
    return options


def _wrap_agui_resume_astream(
    astream_factory: _NativeAstreamFactory | None,
    *,
    tinkerfin: AgentRuntime[Any],
    identity: RunIdentity,
    parent_run_id: str | None,
    mode: AgentMode,
    on_part: PartObserver[Mapping[str, object]] | None,
    timeout: float | None,
    settlement_timeout: float | None,
    expose_reasoning_events: bool,
    expose_subagent_events: bool,
    private_state_keys: frozenset[str],
    checkpointer: object | None,
    resume: AgUiResumeBinding,
    on_resume_checkpointed: AgUiResumeCheckpointObserver | None,
    on_resume_not_saved: (AgUiResumeNotSavedObserver | None),
    on_event: EventObserver | None,
) -> Callable[..., AgUiEventStream]:
    """Bind native resume input internally and expose only Graph options."""

    native_signature = tinkerfin._runtime_profile.astream_signature
    public_signature = _resume_astream_signature(native_signature)
    claim = _StreamClaim()

    def wrapped(*args: object, **kwargs: object) -> AgUiEventStream:
        public_bound = public_signature.bind(*args, **kwargs)
        options = _flatten_bound_options(public_bound, public_signature)
        bound = tinkerfin._bind_native_invocation(
            native_signature,
            (None,),
            options,
            identity=identity,
        )
        if resume.mode == "abandon":
            claim.ensure_available()

            async def empty_source() -> AsyncIterator[Mapping[str, object]]:
                if False:  # pragma: no cover - supplies the async iterator shape
                    yield {}

            context = source_context(
                identity=identity,
                runtime_profile=tinkerfin._runtime_profile.profile_id,
                input_kind="abandon",
                parent_run_id=parent_run_id,
                mode=mode,
                graph_input=resume.model_dump(mode="json", by_alias=True),
                config=bound.arguments.get("config", {}),
                private_state_keys=private_state_keys,
                call_tracking_enabled=True,
                resume=resume._observation_summaries(),
            )
            observation = tinkerfin._observation_hub(context)

            stream = tinkerfin._run_agui(
                empty_source,
                identity=identity,
                parent_run_id=parent_run_id,
                on_part=on_part,
                timeout=timeout,
                settlement_timeout=settlement_timeout,
                expose_reasoning_events=expose_reasoning_events,
                expose_subagent_events=expose_subagent_events,
                prior_tool_call_ids=frozenset(),
                private_state_keys=private_state_keys,
                on_event=on_event,
                observation=observation,
            )
            stream._resume_abandoned = True
            claim.claim()
            return stream
        if astream_factory is None:  # pragma: no cover - abandonment is handled above
            raise RuntimeError("resume Runtime has no Graph stream")
        resolved_astream_factory = astream_factory
        return _create_graph_agui_stream(
            native_astream_factory=resolved_astream_factory,
            bound=bound,
            claim=claim,
            tinkerfin=tinkerfin,
            identity=identity,
            parent_run_id=parent_run_id,
            mode=mode,
            on_part=on_part,
            timeout=timeout,
            settlement_timeout=settlement_timeout,
            expose_reasoning_events=expose_reasoning_events,
            expose_subagent_events=expose_subagent_events,
            private_state_keys=private_state_keys,
            checkpointer=checkpointer,
            resume=resume,
            on_resume_checkpointed=on_resume_checkpointed,
            on_resume_not_saved=on_resume_not_saved,
            on_event=on_event,
        )

    setattr(wrapped, "__signature__", public_signature)
    return wrapped


class DeepAgentGraph(Runnable[_GraphInput, Mapping[str, object]]):
    """Expose one complete async Plan/native Graph as a reusable Runnable.

    This direct Graph owns no request identity, Runtime Observation, Trace, AG-UI,
    Messaging, or business lifecycle. The selected Runtime Profile still binds and
    validates its concrete upstream stream shape so Plan routing never embeds a hidden
    v2 parser in Core. Callers use native LangGraph ``Command`` values for direct resume;
    managed AG-UI resume remains an ``open_agui_run`` responsibility.
    """

    __slots__ = ("_astream", "_driver", "_signature")

    def __init__(
        self,
        *,
        astream: Callable[..., AsyncIterator[Mapping[str, object]]],
        driver: NativeStreamDriver,
    ) -> None:
        """Bind one fresh effective Graph to its immutable Profile Driver.

        Args:
            astream: Effective native or Plan-capable upstream stream callable.
            driver: Selected Profile Driver used only for direct invocation binding and
                part validation. It does not create managed Runtime observations here.

        Raises:
            TypeError: The stream or Driver does not implement the required boundary.
        """

        if not callable(astream):
            raise TypeError("astream must be callable")
        if not isinstance(driver, NativeStreamDriver):
            raise TypeError("driver must implement NativeStreamDriver")
        self._astream = astream
        self._driver = driver
        self._signature = inspect.signature(astream)

    def invoke(
        self,
        input: _GraphInput,
        config: RunnableConfig | None = None,
        **kwargs: Any,
    ) -> Mapping[str, object]:
        """Reject synchronous execution for an async resource Graph.

        Args:
            input: Ordinary state, native resume command, or ``None``.
            config: Optional LangGraph configuration, unused by the rejected call.
            **kwargs: Additional upstream options, unused by the rejected call.

        Raises:
            NotImplementedError: Always. Use :meth:`ainvoke` so async checkpointers,
                Stores, tools, and cancellation remain native.
        """

        del input, config, kwargs
        raise NotImplementedError("DeepAgentGraph supports async execution only")

    def stream(
        self,
        input: _GraphInput,
        config: RunnableConfig | None = None,
        **kwargs: Any | None,
    ) -> Iterator[Mapping[str, object]]:
        """Reject synchronous streaming before any Graph or worker is started."""

        del input, config, kwargs
        raise NotImplementedError("DeepAgentGraph supports async execution only")

    def batch(
        self,
        inputs: list[_GraphInput],
        config: RunnableConfig | list[RunnableConfig] | None = None,
        *,
        return_exceptions: bool = False,
        **kwargs: Any | None,
    ) -> list[Mapping[str, object]]:
        """Reject synchronous batching before LangChain creates worker threads."""

        del inputs, config, return_exceptions, kwargs
        raise NotImplementedError("DeepAgentGraph supports async execution only")

    def batch_as_completed(
        self,
        inputs: Sequence[_GraphInput],
        config: RunnableConfig | Sequence[RunnableConfig] | None = None,
        *,
        return_exceptions: bool = False,
        **kwargs: Any | None,
    ) -> Iterator[tuple[int, Mapping[str, object]]]:
        """Reject synchronous concurrent batching without allocating an executor."""

        del inputs, config, return_exceptions, kwargs
        raise NotImplementedError("DeepAgentGraph supports async execution only")

    async def ainvoke(
        self,
        input: _GraphInput,
        config: RunnableConfig | None = None,
        **kwargs: Any,
    ) -> Mapping[str, object]:
        """Run to one final root state while preserving native interrupt objects.

        Args:
            input: Ordinary state, native resume command, or ``None``.
            config: Optional LangGraph configuration.
            **kwargs: Additional Profile-supported async Graph options.

        Returns:
            A defensive root state mapping. Interrupted output includes the current
            LangGraph ``__interrupt__`` list, matching ``CompiledStateGraph.ainvoke``.

        Raises:
            TinkerFinLifecycleError: No root values boundary is emitted.
            BaseException: Profile binding, Graph execution, validation, or cleanup fails.
        """

        options = dict(kwargs)
        if callable(getattr(self._driver, "bind_graph_invocation", None)):
            options["stream_mode"] = ("values",)
        state: dict[str, object] | None = None
        pending: dict[str, NativeRuntimeInterrupt] = {}
        source = self._iterate(input, config, options)
        primary: BaseException | None = None
        try:
            async for _part, canonical in source:
                if canonical.type != "values" or canonical.ns != ():
                    continue
                data = canonical.data
                if not isinstance(data, Mapping):
                    raise TinkerFinLifecycleError(
                        "direct Graph root values data must be a mapping"
                    )
                state = dict(cast(Mapping[str, object], data))
                pending.update((item.id, item) for item in canonical.interrupts)
        except BaseException as error:
            primary = error
            raise
        finally:
            await _close_owned_iterator(
                source, primary=primary, task_name="tinkerfin-direct-invoke-close"
            )
        if state is None:
            raise TinkerFinLifecycleError(
                "direct Graph completed without a root values boundary"
            )
        if pending:
            state["__interrupt__"] = list(pending.values())
        return state

    async def astream(
        self,
        input: _GraphInput,
        config: RunnableConfig | None = None,
        **kwargs: Any,
    ) -> AsyncGenerator[Mapping[str, object], None]:
        """Stream validated Profile objects without managed Runtime side effects.

        Profile-required semantic modes remain available to the internal Plan router. If
        the caller explicitly requests a subset, those private routing modes are consumed
        but not yielded. Omitting ``stream_mode`` exposes the Profile's complete canonical
        object stream, matching the managed native Runtime.

        Args:
            input: Ordinary state, native resume command, or ``None``.
            config: Optional LangGraph configuration.
            **kwargs: Profile-supported Graph stream options.

        Yields:
            Original upstream object mappings after Profile validation.

        Raises:
            TypeError: Stream mode or upstream iterator shape is invalid.
            ValueError: A direct Graph option conflicts with the selected Profile.
            BaseException: Graph execution, validation, cancellation, or cleanup fails.
        """

        requested = self._requested_modes(kwargs.get("stream_mode"))
        source = self._iterate(input, config, kwargs)
        primary: BaseException | None = None
        try:
            async for part, canonical in source:
                if requested is None or canonical.type in requested:
                    yield part
        except BaseException as error:
            primary = error
            raise
        finally:
            # Closing this public generator must join the inner iterator now;
            # async-for alone leaves its finally to a later generator finalizer.
            await _close_owned_iterator(
                source, primary=primary, task_name="tinkerfin-direct-stream-close"
            )

    async def _iterate(
        self,
        input: _GraphInput,
        config: RunnableConfig | None,
        options: Mapping[str, object],
    ) -> AsyncIterator[tuple[Mapping[str, object], NativeValidatedStreamPart]]:
        """Yield each raw object with its single validated Profile representation."""

        direct_binder = getattr(self._driver, "bind_graph_invocation", None)
        if callable(direct_binder):
            bound = cast(
                Callable[
                    [inspect.Signature, tuple[object, ...], Mapping[str, object]],
                    inspect.BoundArguments,
                ],
                direct_binder,
            )(
                self._signature,
                (input, config),
                options,
            )
        else:
            # Existing custom Profiles remain valid. They receive exactly the public
            # arguments their own stream signature declares; only Drivers that opt in
            # may add private modes needed by a composed direct Graph.
            bound = self._signature.bind(input, config, **dict(options))
        source = self._astream(*bound.args, **bound.kwargs)
        if not isinstance(source, AsyncIterator):
            raise TypeError("source_factory must return an async iterator")
        failures = OwnedOperationFailures()
        primary: BaseException | None = None
        try:
            while True:
                try:
                    with failures.capture():
                        part = await anext(source)
                except StopAsyncIteration:
                    break
                canonical = self._driver.validate(part)
                if not isinstance(part, Mapping):
                    raise TypeError("direct Graph stream parts must be mappings")
                yield part, canonical
        except BaseException as error:
            primary = error
            raise
        finally:
            try:
                with failures.capture():
                    await _close_owned_iterator(
                        source,
                        primary=primary,
                        task_name="tinkerfin-direct-graph-close",
                    )
            except BaseException as cleanup_error:
                failures.settle(cleanup_error)
                raise
            failures.settle(primary)

    @staticmethod
    def _requested_modes(value: object) -> frozenset[str] | None:
        """Return an explicit caller mode filter without validating Profile support."""

        if value is None:
            return None
        if isinstance(value, str):
            return frozenset({value})
        if isinstance(value, Sequence) and not isinstance(value, bytes):
            modes: tuple[object, ...] = tuple(cast(Sequence[object], value))
            if all(isinstance(mode, str) for mode in modes):
                return frozenset(cast(str, mode) for mode in modes)
        return None


class _AgentDefinition:
    """Keep immutable agent configuration and build a fresh graph per run."""

    def __init__(
        self,
        *,
        tinkerfin: AgentRuntime[Any],
        spec: AgentSpec[Any],
        private_state_keys: frozenset[str],
    ) -> None:
        self._tinkerfin = tinkerfin
        self._spec = spec.snapshot()
        self._checkpointer = spec.checkpointer
        self._plan_options = tinkerfin._plan_options
        self._private_state_keys = private_state_keys

    def _deferred_astream(self, mode: AgentMode) -> _NativeAstream:
        """Return one signature-preserving stream that builds its Graph asynchronously."""

        async def build_astream() -> _NativeAstream:
            graph = await self.create_graph(mode=mode)
            return graph._astream

        resolver = _AsyncAstreamResolver(build_astream)
        resolve_astream = resolver.resolve

        def deferred(
            *args: object, **kwargs: object
        ) -> AsyncIterator[Mapping[str, object]]:
            async def source() -> AsyncIterator[Mapping[str, object]]:
                native_astream = await resolve_astream()
                native = native_astream(*args, **kwargs)
                if not isinstance(native, AsyncIterator):
                    raise TypeError("source_factory must return an async iterator")
                primary: BaseException | None = None
                try:
                    async for part in native:
                        yield part
                except BaseException as error:
                    primary = error
                    raise
                finally:
                    await _close_owned_iterator(
                        native,
                        primary=primary,
                        task_name="tinkerfin-deferred-graph-close",
                    )

            return source()

        setattr(
            deferred,
            "__signature__",
            self._tinkerfin._runtime_profile.astream_signature,
        )
        setattr(deferred, "_tinkerfin_resolve_astream", resolve_astream)
        return deferred

    async def create_graph(
        self,
        *,
        mode: AgentMode | None = None,
    ) -> DeepAgentGraph:
        """Create one complete reusable async native or Plan-capable Graph.

        TinkerFin assembles the native graph in a bounded worker.
        Plan capability adds one lazily built Planning Graph to the same reusable router;
        it never creates a second native Graph. The returned Runnable owns no borrowed
        model, checkpointer, Store, or backend resource and never closes them.

        Args:
            mode: Default route for new input. Resume always follows durable checkpoint
                role evidence rather than this preference.

        Returns:
            A reusable async Graph containing the Definition's complete native and
            optional Plan behavior.

        Raises:
            TypeError: Graph configuration violates the selected integration.
            ValueError: The requested mode is unavailable on this Definition.
            BaseException: Agent or Planning Graph construction fails.
        """

        if isinstance(self._spec.backend, Workspace):
            raise ValueError("Workspace requires managed Runtime execution")  # noqa: TRY004 - the declaration is valid but requires a managed resource scope
        spec = self._spec.snapshot()
        if isinstance(spec.backend, BackendProtocol):
            spec = replace(spec, backend=async_store_backend(spec.backend))
        return await self._build_graph(mode=mode, spec=spec)

    async def _create_run_graph(
        self,
        identity: RunIdentity,
        resources: RunResources,
        *,
        mode: AgentMode,
        compaction: bool = False,
    ) -> DeepAgentGraph:
        prepared = await prepare_agent_spec(self._spec.snapshot(), identity, resources)
        return await self._build_graph(
            mode=mode,
            spec=prepared.spec,
            workspace=prepared.workspace,
            compaction=compaction,
        )

    async def _build_graph(
        self,
        *,
        mode: AgentMode | None,
        spec: AgentSpec[Any],
        workspace: PreparedWorkspace[object, BackendProtocol] | None = None,
        compaction: bool = False,
    ) -> DeepAgentGraph:
        resolved_mode = resolve_agent_mode(mode, options=self._plan_options)
        native = await run_sync_owned(
            partial(create_agent_graph, spec, workspace=workspace)
        )
        native_astream = self._tinkerfin._runtime_profile.graph_stream(native)
        if not callable(native_astream):
            raise TypeError("Runtime Profile Graph must expose a callable astream")
        effective = cast(_NativeAstream, native_astream)
        # LangGraph 1.2.11 omits Command's generic in astream. Keep the bound
        # method itself so checkpoint inspection can access its graph owner.
        inspect_astream = cast(
            _NativeAstream,
            native.astream,  # pyright: ignore[reportUnknownMemberType]
        )
        if self._plan_options is not None:
            from .plan._runtime import PlanCapableGraphRuntime
            from .plan._workflow import PlanningWorkflowGraph, create_planning_graph

            options = self._plan_options

            async def planning_graph() -> PlanningWorkflowGraph[Any]:
                return await run_sync_owned(
                    partial(
                        create_planning_graph,
                        spec,
                        options=options,
                        workspace=workspace,
                    )
                )

            runtime = PlanCapableGraphRuntime(
                options=options,
                native=cast(_PlanNativeGraph, native),
                planning_factory=planning_graph,
                prefer_plan=resolved_mode == "plan",
            )
            effective = runtime.astream
            inspect_astream = runtime.astream
        if compaction:
            from ._compaction import create_compaction_stream

            effective = await run_sync_owned(
                partial(
                    create_compaction_stream,
                    native=native,
                    inspect_astream=inspect_astream,
                    spec=spec,
                    plan_enabled=self._plan_options is not None,
                    runtime_profile=self._tinkerfin._runtime_profile,
                )
            )
        return DeepAgentGraph(
            astream=effective,
            driver=self._tinkerfin._runtime_profile.stream_driver,
        )

    def _resume_checkpointer(self) -> object | None:
        """Return the effective borrowed saver without transferring ownership."""

        return self._checkpointer

    async def _open_native_run(
        self,
        *,
        identity: RunIdentity,
        resources: RunResources,
        input: _GraphInput,
        mode: AgentMode | None,
        config: RunnableConfig | None,
        context: object | None,
        on_part: PartObserver[Mapping[str, object]] | None,
        stream_options: Mapping[str, object],
        compaction: bool = False,
    ) -> NativeGraphRunStream:
        """Build once and return one managed native stream for the common facade."""

        resolved_mode = resolve_agent_mode(mode, options=self._plan_options)
        graph = await self._create_run_graph(
            identity, resources, mode=resolved_mode, compaction=compaction
        )
        astream = _wrap_native_astream(
            graph._astream,
            tinkerfin=self._tinkerfin,
            identity=identity,
            mode=resolved_mode,
            private_state_keys=self._private_state_keys,
            on_part=on_part,
            compaction=compaction,
        )
        return cast(
            "NativeGraphRunStream",
            astream(
                input,
                config,
                context=context,
                **dict(stream_options),
            ),
        )

    async def _open_agui_run(
        self,
        *,
        identity: RunIdentity,
        resources: RunResources,
        input: InputAgentState | None,
        resume_request: AgUiResumeRequest | None,
        parent_run_id: str | None,
        mode: AgentMode | None,
        config: RunnableConfig | None,
        context: object | None,
        on_resume_checkpointed: AgUiResumeCheckpointObserver | None,
        on_resume_not_saved: (AgUiResumeNotSavedObserver | None),
        timeout: float | None,
        settlement_timeout: float | None,
        expose_reasoning_events: bool,
        expose_subagent_events: bool,
        on_part: PartObserver[Mapping[str, object]] | None,
        on_event: EventObserver | None,
        stream_options: Mapping[str, object],
        compaction: bool = False,
    ) -> AgUiEventStream:
        """Build once and return one managed AG-UI stream for the common facade."""

        resolved_mode = resolve_agent_mode(mode, options=self._plan_options)
        graph = await self._create_run_graph(
            identity, resources, mode=resolved_mode, compaction=compaction
        )
        if resume_request is None:
            if input is None:
                raise ValueError("ordinary AG-UI run requires input")
            astream = _wrap_agui_astream(
                graph._astream,
                tinkerfin=self._tinkerfin,
                identity=identity,
                parent_run_id=parent_run_id,
                mode=resolved_mode,
                on_part=on_part,
                timeout=timeout,
                settlement_timeout=settlement_timeout,
                expose_reasoning_events=expose_reasoning_events,
                expose_subagent_events=expose_subagent_events,
                private_state_keys=self._private_state_keys,
                on_event=on_event,
                compaction=compaction,
            )
            return cast(
                "AgUiEventStream",
                astream(
                    input,
                    config,
                    context=context,
                    **dict(stream_options),
                ),
            )

        binding = await self._prepare_agui_resume_from_astream(
            graph._astream,
            identity=identity,
            request=resume_request,
            parent_run_id=parent_run_id,
        )
        astream = _wrap_agui_resume_astream(
            (None if binding.mode == "abandon" else lambda: graph._astream),
            tinkerfin=self._tinkerfin,
            identity=identity,
            parent_run_id=parent_run_id,
            mode=resolved_mode,
            on_part=on_part,
            timeout=timeout,
            settlement_timeout=settlement_timeout,
            expose_reasoning_events=expose_reasoning_events,
            expose_subagent_events=expose_subagent_events,
            private_state_keys=self._private_state_keys,
            checkpointer=self._checkpointer,
            resume=binding,
            on_resume_checkpointed=on_resume_checkpointed,
            on_resume_not_saved=on_resume_not_saved,
            on_event=on_event,
        )
        return astream(
            config=config,
            context=context,
            **dict(stream_options),
        )

    async def _prepare_agui_resume_from_astream(
        self,
        astream: Callable[..., object],
        *,
        identity: RunIdentity,
        request: AgUiResumeRequest,
        parent_run_id: str | None,
    ) -> AgUiResumeBinding:
        """Resolve one public resume request against an already built Graph."""

        require_agui()
        from tinkerfin_agui_adapter import AgUiLifecycleEventFactory

        from ._agui_lineage import _require_checkpointer, resolve_agui_resume_context
        from .agui_resume import AgUiResumeBinding as AgUiResumeBindingType
        from .agui_resume import AgUiResumeRequest as AgUiResumeRequestType

        if not isinstance(identity, RunIdentity):
            raise TypeError("identity must be a RunIdentity")
        if not isinstance(request, AgUiResumeRequestType):
            raise TypeError("request must be an AgUiResumeRequest")
        AgUiLifecycleEventFactory.validate_parent_run_id(
            parent_run_id,
            identity=identity,
        )
        context = await resolve_agui_resume_context(
            astream,
            identity=identity,
            parent_run_id=parent_run_id,
            runtime_profile=self._tinkerfin._runtime_profile,
        )
        binding = AgUiResumeBindingType.from_native(
            request=request,
            interrupts=context.interrupts,
            messages_by_graph_namespace=context.messages_by_graph_namespace,
            interrupt_graph_namespaces=context.interrupt_graph_namespaces,
        )
        if not binding._cancelled_native_groups() <= context.cancellation_interrupt_ids:
            raise TinkerFinLifecycleError(
                "mixed tool cancellation requires TinkerFin tool review support "
                "in each interrupted Graph receiving a cancellation"
            )
        if self._plan_options is not None and binding.mode == "resume":
            from .plan._resume import validate_plan_resume_response

            responses = binding._native_decisions()
            for pending in context.interrupts:
                value = pending.value
                if not isinstance(value, dict) or value.get("kind") not in {
                    "tinkerfin:plan_clarification",
                    "tinkerfin:plan_review",
                }:
                    continue
                # Preserve the original card on semantic validation failure.
                # source() must not stage a durable intent until this completes.
                source = context.sources[pending.id]
                checkpoint = await _require_checkpointer(astream).aget_tuple(
                    source.config
                )
                if checkpoint is None:
                    raise TinkerFinLifecycleError(
                        "Plan response source checkpoint is missing"
                    )
                validate_plan_resume_response(
                    checkpoint.checkpoint["channel_values"],
                    self._plan_options,
                    responses[pending.id],
                )
        return binding


def bind_agent(builder: TinkerFin, spec: AgentSpec[ContextT]) -> AgentRuntime[ContextT]:
    """Bind declared resources to one namespace without constructing a graph."""
    from deepagents.middleware.summarization import (
        SUMMARIZATION_EVENT_KEY,
        SUMMARIZATION_SESSION_ID_KEY,
    )

    from ._compaction_observation import COMPACTION_MARKER
    from ._hitl_state import TOOL_REVIEW_CHANNEL
    from .runtime import AgentRuntime

    namespace = builder._require_namespace()
    validate_agent_spec(spec)
    checkpointer = (
        None
        if spec.checkpointer is None
        else NamespaceCheckpointer(
            cast(BaseCheckpointSaver[Any], spec.checkpointer), namespace
        )
    )
    store = None if spec.store is None else NamespaceStore(spec.store, namespace)
    state = compose_deep_agent_base_schema(spec.state_schema)
    private_keys = frozenset(
        {
            RESUME_METADATA_KEY,
            TOOL_REVIEW_CHANNEL,
            SUMMARIZATION_EVENT_KEY,
            SUMMARIZATION_SESSION_ID_KEY,
            COMPACTION_MARKER,
        }
    )
    middleware = tuple(spec.middleware)
    if builder._plan_options is not None:
        from .plan._handoff import create_plan_handoff_middleware
        from .plan._state import PLAN_PRIVATE_STATE_KEYS

        middleware = (*middleware, create_plan_handoff_middleware())
        private_keys |= PLAN_PRIVATE_STATE_KEYS
    spec = replace(
        spec,
        checkpointer=checkpointer,
        store=store,
        state_schema=state,
        middleware=middleware,
        attachments=builder._attachments,
    )
    runtime: AgentRuntime[ContextT] = AgentRuntime[ContextT]._create(builder)
    runtime._definition = _AgentDefinition(
        tinkerfin=runtime,
        spec=spec,
        private_state_keys=private_keys,
    )
    return runtime


async def create_graph(
    runtime: AgentRuntime[ContextT], *, mode: AgentMode | None = None
) -> DeepAgentGraph:
    """Build a reusable Graph from a Runtime's namespace-bound configuration.

    Args:
        runtime: Runtime supplying agent configuration and borrowed resources.
        mode: Optional default execution or Plan route.

    Returns:
        A reusable asynchronous Graph. The caller owns its execution lifecycle.

    Raises:
        TypeError: The runtime has an invalid type.
        BaseException: Graph preparation fails or is cancelled.
    """
    from .runtime import AgentRuntime

    if not isinstance(runtime, AgentRuntime):
        raise TypeError("runtime must be an AgentRuntime")
    failures = OwnedOperationFailures()
    try:
        with failures.capture():
            graph = await runtime._definition.create_graph(mode=mode)
    except BaseException as error:
        failures.settle(error)
        raise
    failures.settle(None)
    return graph


__all__ = ["DeepAgentGraph", "create_graph"]

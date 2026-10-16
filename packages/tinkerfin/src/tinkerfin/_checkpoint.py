"""Translate logical checkpoint threads without owning the borrowed saver."""

from __future__ import annotations

import hashlib
import json
import sys
from base64 import b64decode, b64encode
from collections.abc import (
    AsyncGenerator,
    Awaitable,
    Callable,
    Collection,
    Iterator,
    Sequence,
)
from typing import TYPE_CHECKING, Any, TypeVar, cast

from langchain_core.runnables import RunnableConfig
from langchain_core.runnables.utils import ConfigurableFieldSpec
from langgraph.checkpoint.base import (
    BaseCheckpointSaver,
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
    get_checkpoint_metadata,
)
from pydantic import ValidationError

from tinkerfin_contracts import ThreadIdentity
from tinkerfin_contracts.identity import validate_namespace

from ._agui_lineage_state import (
    LINEAGE_CONFIG_KEY,
    LINEAGE_METADATA_KEY,
    RESUME_CONFIG_KEY,
    RESUME_METADATA_KEY,
    LineageMarker,
    ResumeIntent,
)
from ._tasks import run_async_owned
from .errors import TinkerFinLifecycleError

if TYPE_CHECKING:
    from langgraph.checkpoint.redis.aio import AsyncRedisSaver

V = TypeVar("V", int, float, str)
THREAD_IDENTITY_KEY = "_tinkerfin_thread_identity"


class _ThreadMetadata(CheckpointMetadata, total=False):
    _tinkerfin_thread_identity: str
    _tinkerfin_lineage: str
    _tinkerfin_resume: str


class _RedisMetadata(CheckpointMetadata, total=False):
    _tinkerfin_metadata: str


def _identity_json(identity: ThreadIdentity) -> str:
    # Canonical ASCII JSON gives every Unicode identity one stable hash input.
    return json.dumps(
        identity.model_dump(mode="json", by_alias=True),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )


def _redis_saver(saver: BaseCheckpointSaver[V]) -> AsyncRedisSaver | None:
    # Loading a Memory-only Runtime must not import Redis or open connections.
    if "langgraph.checkpoint.redis.aio" not in sys.modules:
        return None
    from langgraph.checkpoint.redis.aio import AsyncRedisSaver

    return saver if isinstance(saver, AsyncRedisSaver) else None


def _physical_thread(identity: ThreadIdentity, saver: BaseCheckpointSaver[V]) -> str:
    canonical = _identity_json(identity)
    if redis := _redis_saver(saver):
        # Redis 0.5.2's write registry is shared across configured key prefixes.
        # Bind both stable prefixes here so every read/write/registry/delete key
        # stays in one storage domain, including after a connection is rebuilt.
        canonical = json.dumps(
            {
                "identity": canonical,
                "checkpointPrefix": redis._checkpoint_prefix,
                "writePrefix": redis._checkpoint_write_prefix,
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()


def _owned_metadata_key(key: str) -> bool:
    # BaseRedisSaver._dump_metadata strips NUL from the entire serialized JSON,
    # including keys. An ordinary alias must not turn into an ownership field.
    return key.replace("\0", "") in {
        THREAD_IDENTITY_KEY,
        LINEAGE_METADATA_KEY,
        RESUME_METADATA_KEY,
    }


def _encode_metadata(canonical_json: str) -> str:
    # ASCII encoding also protects escaped NUL inside the nested JSON string.
    return b64encode(canonical_json.encode("ascii")).decode("ascii")


def _decode_metadata(value: object) -> str:
    if not isinstance(value, str):
        raise TinkerFinLifecycleError("checkpoint ownership metadata is not text")
    try:
        decoded = b64decode(value, validate=True).decode("ascii")
        if _encode_metadata(decoded) != value:
            raise ValueError("checkpoint ownership encoding is not canonical")
    except ValueError as error:
        raise TinkerFinLifecycleError(
            "checkpoint ownership metadata is invalid", cause=error
        ) from error
    return decoded


def _encode_graph_scope(value: str) -> str:
    return b64encode(value.encode("utf-8")).decode("ascii")


def _decode_graph_scope(value: object) -> str:
    if not isinstance(value, str):
        raise TinkerFinLifecycleError("checkpoint graph scope is not text")
    try:
        decoded = b64decode(value, validate=True).decode("utf-8")
        if _encode_graph_scope(decoded) != value:
            raise ValueError("checkpoint graph scope encoding is not canonical")
    except ValueError as error:
        raise TinkerFinLifecycleError(
            "checkpoint graph scope is invalid", cause=error
        ) from error
    return decoded


def _redis_metadata(metadata: CheckpointMetadata) -> _RedisMetadata:
    # Redis scrubs serialized JSON, so even literal backslash-u sequences in
    # ordinary application metadata require protection. Keep its standard index
    # selectors while the complete logical metadata travels as one ASCII value.
    canonical = json.dumps(
        metadata, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    encoded: _RedisMetadata = {"_tinkerfin_metadata": _encode_metadata(canonical)}
    if "source" in metadata:
        encoded["source"] = metadata["source"]
    if "step" in metadata:
        encoded["step"] = metadata["step"]
    return encoded


def _read_redis_metadata(metadata: CheckpointMetadata) -> _ThreadMetadata:
    try:
        decoded: object = json.loads(
            _decode_metadata(metadata.get("_tinkerfin_metadata"))
        )
    except ValueError as error:
        raise TinkerFinLifecycleError(
            "checkpoint metadata is invalid", cause=error
        ) from error
    if not isinstance(decoded, dict):
        raise TinkerFinLifecycleError("checkpoint metadata is not a mapping")
    # Redis metadata is an external JSON object; ownership fields are validated
    # immediately after this physical representation is decoded.
    return cast(_ThreadMetadata, decoded)


class NamespaceCheckpointer(BaseCheckpointSaver[V]):
    """Restrict asynchronous checkpoint operations to one logical namespace.

    Physical thread keys include namespace and thread identity; run IDs do not
    split a conversation. Every returned checkpoint must contain matching canonical
    identity evidence. Graph namespaces, parent chains, delta metadata, serializers,
    and pending writes retain their upstream meaning. The saver remains borrowed.

    History requires an explicit thread. Global run deletion, thread copying, and
    pruning are intentionally unavailable: the generic upstream methods cannot
    safely preserve scoped identity and DeltaChannel ancestry on every saver.
    """

    def __init__(self, saver: BaseCheckpointSaver[V], namespace: str) -> None:
        """Bind a borrowed saver without opening connections or performing I/O."""

        super().__init__(serde=saver.serde)
        self._saver = saver
        self._namespace = validate_namespace(namespace)

    @property
    def config_specs(self) -> list[ConfigurableFieldSpec]:
        """Preserve the borrowed saver's declared configuration fields."""

        # BaseCheckpointSaver 4.2 declares this return as an unparameterized list.
        return cast(list[ConfigurableFieldSpec], self._saver.config_specs)  # pyright: ignore[reportUnknownMemberType]

    def with_allowlist(
        self, extra_allowlist: Collection[tuple[str, ...]]
    ) -> NamespaceCheckpointer[V]:
        """Apply serializer permissions to the saver that performs actual I/O."""

        return NamespaceCheckpointer(
            self._saver.with_allowlist(extra_allowlist), self._namespace
        )

    def get_next_version(self, current: V | None, channel: None) -> V:
        """Preserve the borrowed saver's pure channel-version sequence."""

        return self._saver.get_next_version(current, channel)

    def _identity(self, config: RunnableConfig) -> ThreadIdentity:
        if not isinstance(config.get("configurable", {}).get("checkpoint_ns", ""), str):
            raise TypeError("checkpoint graph scope must be text")
        raw_thread = config.get("configurable", {}).get("thread_id")
        if raw_thread is None:
            raise ValueError("checkpoint operations require a logical thread_id")
        if not isinstance(raw_thread, str):
            raise TypeError("checkpoint thread_id must be a string")
        identity = ThreadIdentity(namespace=self._namespace, thread_id=raw_thread)
        lineage = config.get("configurable", {}).get(LINEAGE_CONFIG_KEY)
        if lineage is not None and (
            not isinstance(lineage, LineageMarker) or lineage.thread != identity
        ):
            raise TinkerFinLifecycleError(
                "checkpoint invocation has conflicting run ownership"
            )
        return identity

    @staticmethod
    def _replace_thread(config: RunnableConfig, thread_id: str) -> RunnableConfig:
        return {
            **config,
            "configurable": {**config.get("configurable", {}), "thread_id": thread_id},
        }

    def _physical(self, config: RunnableConfig) -> RunnableConfig:
        physical = self._replace_thread(
            config, _physical_thread(self._identity(config), self._saver)
        )
        options = dict(physical.get("configurable", {}))
        options = {
            key: value for key, value in options.items() if not _owned_metadata_key(key)
        }
        options.pop(LINEAGE_CONFIG_KEY, None)
        options.pop(RESUME_CONFIG_KEY, None)
        if _redis_saver(self._saver) is not None:
            scope = options.get("checkpoint_ns", "")
            if not isinstance(scope, str):
                raise TypeError("checkpoint graph scope must be text")
            options["checkpoint_ns"] = _encode_graph_scope(scope)
        physical["configurable"] = options
        if "metadata" in physical:
            physical["metadata"] = {
                key: value
                for key, value in physical["metadata"].items()
                if not _owned_metadata_key(key)
            }
        return physical

    def _logical_config(
        self, config: RunnableConfig, identity: ThreadIdentity
    ) -> RunnableConfig:
        if config.get("configurable", {}).get("thread_id") != _physical_thread(
            identity, self._saver
        ):
            raise TinkerFinLifecycleError(
                "checkpoint configuration has conflicting thread identity"
            )
        logical = self._replace_thread(config, identity.thread_id)
        # A saver may echo its entire input config. Transient ownership must never
        # be restored over the next invocation's typed run binding.
        options = dict(logical.get("configurable", {}))
        options.pop(LINEAGE_CONFIG_KEY, None)
        options.pop(RESUME_CONFIG_KEY, None)
        if _redis_saver(self._saver) is not None:
            options["checkpoint_ns"] = _decode_graph_scope(
                options.get("checkpoint_ns", "")
            )
        logical["configurable"] = options
        return logical

    def _logical(
        self, saved: CheckpointTuple, identity: ThreadIdentity
    ) -> CheckpointTuple:
        if (
            saved.config.get("configurable", {}).get("checkpoint_id")
            != saved.checkpoint["id"]
        ):
            raise TinkerFinLifecycleError(
                "checkpoint tuple has conflicting checkpoint coordinates"
            )
        metadata: _ThreadMetadata = (
            _read_redis_metadata(saved.metadata)
            if _redis_saver(self._saver) is not None
            else {**saved.metadata}
        )
        for key in (
            "_tinkerfin_thread_identity",
            "_tinkerfin_lineage",
            "_tinkerfin_resume",
        ):
            if key in metadata:
                metadata[key] = _decode_metadata(metadata.get(key))
        proof = metadata.get(THREAD_IDENTITY_KEY)
        if not isinstance(proof, str):
            raise TinkerFinLifecycleError(
                "checkpoint lacks canonical thread identity evidence"
            )
        try:
            persisted = ThreadIdentity.model_validate_json(proof)
        except ValidationError as error:
            raise TinkerFinLifecycleError(
                "checkpoint thread identity is invalid", cause=error
            ) from error
        if persisted != identity or proof != _identity_json(persisted):
            raise TinkerFinLifecycleError(
                "checkpoint belongs to another logical thread"
            )
        raw_lineage = metadata.get(LINEAGE_METADATA_KEY)
        lineage: LineageMarker | None = None
        if raw_lineage is not None:
            try:
                if not isinstance(raw_lineage, str):
                    raise TypeError("lineage metadata must be a string")
                lineage = LineageMarker.model_validate_json(raw_lineage)
                if (
                    lineage.thread != identity
                    or raw_lineage != lineage.canonical_json()
                ):
                    raise ValueError("lineage metadata conflicts with thread ownership")
            except (TypeError, ValueError) as error:
                raise TinkerFinLifecycleError(
                    "checkpoint has invalid run ownership evidence", cause=error
                ) from error
        raw_resume = metadata.get(RESUME_METADATA_KEY)
        if raw_resume is not None:
            try:
                if not isinstance(raw_resume, str):
                    raise TypeError("resume metadata must be a string")
                intent = ResumeIntent.model_validate_json(raw_resume)
                self._validate_intent(intent, lineage)
                if raw_resume != intent.canonical_json():
                    raise ValueError("resume metadata is not canonical")
            except (TypeError, ValueError) as error:
                raise TinkerFinLifecycleError(
                    "checkpoint has invalid resume ownership evidence", cause=error
                ) from error
        return CheckpointTuple(
            config=self._logical_config(saved.config, identity),
            checkpoint=saved.checkpoint,
            metadata=metadata,
            parent_config=(
                None
                if saved.parent_config is None
                else self._logical_config(saved.parent_config, identity)
            ),
            pending_writes=saved.pending_writes,
        )

    @staticmethod
    def _validate_intent(intent: ResumeIntent, lineage: LineageMarker | None) -> None:
        if (
            lineage is None
            or intent.thread != lineage.thread
            or intent.run_id != lineage.run_id
            or intent.parent_run_id != lineage.parent_run_id
            or intent.runtime_profile != lineage.runtime_profile
        ):
            raise ValueError("resume intent conflicts with checkpoint run ownership")

    async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        """Read one logical checkpoint and verify its persisted identity."""

        identity = self._identity(config)
        saved = await self._saver.aget_tuple(self._physical(config))
        if saved is None:
            return None
        logical = self._logical(saved, identity)
        requested = config.get("configurable", {})
        returned = logical.config.get("configurable", {})
        if returned.get("checkpoint_ns", "") != requested.get("checkpoint_ns", "") or (
            requested.get("checkpoint_id") not in (None, "")
            and requested["checkpoint_id"] != logical.checkpoint["id"]
        ):
            raise TinkerFinLifecycleError(
                "checkpoint lookup returned another requested location"
            )
        return logical

    async def alist(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> AsyncGenerator[CheckpointTuple, None]:
        """List verified history for one thread, applying selection before the limit.

        The underlying query is indexed by the physical thread; exact selection
        is enforced on its stream. Redis history uses bounded ordered pages and
        fails explicitly if the complete result count cannot be verified.
        Delta history uses BaseCheckpointSaver.aget_delta_channel_history, which
        traverses this view's verified aget_tuple rather than bypassing identity.

        Args:
            config: Logical thread and optional Graph scope or checkpoint selection.
            filter: Exact metadata values required on every returned checkpoint.
            before: Exclusive checkpoint cursor in the same logical thread.
            limit: Maximum results after selection, or None for all matching history.

        Yields:
            Verified checkpoints with logical thread and parent coordinates.

        Raises:
            ValueError: The thread, cursor, or limit is invalid.
            TypeError: The thread or Graph scope has an invalid type.
            TinkerFinLifecycleError: Persisted ownership cannot be verified.
        """

        if config is None:
            raise ValueError("checkpoint history requires a logical thread_id")
        if limit is not None and limit < 0:
            raise ValueError("checkpoint history limit must not be negative")
        identity = self._identity(config)
        if limit == 0:
            return
        selected = config.get("configurable", {})
        before_id: str | None = None
        if before is not None:
            before_options = before.get("configurable", {})
            if (
                before_options.get("thread_id", identity.thread_id)
                != identity.thread_id
            ):
                raise ValueError("checkpoint history cursor belongs to another thread")
            raw_before = before_options.get("checkpoint_id")
            if not isinstance(raw_before, str):
                raise ValueError("checkpoint history cursor requires checkpoint_id")
            before_id = raw_before
        count = 0
        # An upstream configurable run_id may be an index selector (Redis) or be
        # ignored (Memory). Query only the physical thread and enforce our explicit
        # filters below so selection is consistent across borrowed savers.
        physical = _physical_thread(identity, self._saver)
        if redis_saver := _redis_saver(self._saver):
            from ._checkpoint_redis import checkpoint_history

            rows = checkpoint_history(redis_saver, physical)
        else:
            rows = self._saver.alist({"configurable": {"thread_id": physical}})
        try:
            async for saved in rows:
                row = self._logical(saved, identity)
                options = row.config.get("configurable", {})
                if (
                    "checkpoint_ns" in selected
                    and options.get("checkpoint_ns", "") != selected["checkpoint_ns"]
                ):
                    continue
                if (
                    "checkpoint_id" in selected
                    and options.get("checkpoint_id") != selected["checkpoint_id"]
                ):
                    continue
                if before_id is not None and row.checkpoint["id"] >= before_id:
                    continue
                if filter and any(
                    row.metadata.get(key) != value for key, value in filter.items()
                ):
                    continue
                yield row
                count += 1
                if limit is not None and count >= limit:
                    break
        finally:
            # Savers can hold a query cursor or connection until iteration closes.
            # AsyncIterator's base protocol omits aclose, so inspect that optional
            # upstream resource boundary while preserving cancellation.
            close = getattr(rows, "aclose", None)
            if close is not None:
                if not callable(close):
                    raise TypeError("checkpoint history aclose must be callable")
                await run_async_owned(
                    cast(Callable[[], Awaitable[object]], close),
                    task_name="tinkerfin-checkpoint-history-close",
                )

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        """Save a checkpoint with immutable evidence of its logical thread."""

        identity = self._identity(config)
        owned_metadata = cast(
            _ThreadMetadata,
            {
                key: value
                for key, value in metadata.items()
                if not _owned_metadata_key(key)
            },
        )
        owned_metadata["_tinkerfin_thread_identity"] = _encode_metadata(
            _identity_json(identity)
        )
        # Caller metadata and returned subagent state cannot assign checkpoint
        # ownership. Only the typed value issued by the managed boundary can do so.
        lineage = config.get("configurable", {}).get(LINEAGE_CONFIG_KEY)
        if lineage is not None:
            if not isinstance(lineage, LineageMarker) or lineage.thread != identity:
                raise TinkerFinLifecycleError(
                    "checkpoint invocation has conflicting run ownership"
                )
            owned_metadata["_tinkerfin_lineage"] = _encode_metadata(
                lineage.canonical_json()
            )
        intent = config.get("configurable", {}).get(RESUME_CONFIG_KEY)
        if intent is not None:
            if not isinstance(intent, ResumeIntent):
                raise TypeError("checkpoint resume intent must be a typed record")
            self._validate_intent(intent, lineage)
            owned_metadata["_tinkerfin_resume"] = _encode_metadata(
                intent.canonical_json()
            )
        physical = self._physical(config)
        # BaseCheckpointSaver's scalar configuration metadata is part of history
        # selection. Redis 0.5.2 does not merge it in aput; apply the base contract
        # consistently before delegation, after protecting our identity evidence.
        merged = get_checkpoint_metadata(physical, owned_metadata)
        saved = await self._saver.aput(
            physical,
            checkpoint,
            _redis_metadata(merged)
            if _redis_saver(self._saver) is not None
            else merged,
            new_versions,
        )
        from ._compaction_observation import compaction_checkpointed

        await compaction_checkpointed(checkpoint["channel_values"], new_versions)
        return self._logical_config(saved, identity)

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        """Keep pending task writes and approval ownership in the physical thread."""

        physical = self._physical(config)
        resumes = [value for channel, value in writes if channel == "__resume__"]
        if resumes:
            if len(resumes) != 1:
                raise TinkerFinLifecycleError("task has conflicting resume submissions")
            values = resumes[0]

            async def save_resume() -> None:
                if task_id != "00000000-0000-0000-0000-000000000000":
                    await self._reserve_resume_prefix(
                        config,
                        cast(list[object], values)
                        if isinstance(values, list)
                        else [values],
                        task_id,
                    )
                await self._saver.aput_writes(physical, writes, task_id, task_path)

            # LangGraph 1.2.10 AsyncBackgroundExecutor suppresses saver failures
            # while unwinding cancellation. Join the complete reservation/write
            # operation and retain its failure in the owning Runtime's settlement.
            await run_async_owned(save_resume, task_name="tinkerfin-checkpoint-resume")
        else:
            await self._saver.aput_writes(physical, writes, task_id, task_path)

    async def _reserve_resume_prefix(
        self, config: RunnableConfig, values: list[object], task_id: str
    ) -> None:
        # LangGraph 1.2.10 _scratchpad restores the whole list on task retries.
        # Preserve each persisted prefix's original owner, reserving only new
        # entries before the native write. Records grow only with that task's
        # existing native list and are deleted with its checkpoint thread.
        from ._resume_receipt import (
            RESUME_RECEIPT_CHANNEL,
            ResumeReceipt,
            receipt_owner,
            resume_prefix_digest,
            resume_receipts,
            task_resume_values,
        )

        checkpoint = await self.aget_tuple(config)
        options = config.get("configurable", {})
        intent = options.get(RESUME_CONFIG_KEY)
        owner = options.get(LINEAGE_CONFIG_KEY)
        if owner is not None and not isinstance(owner, LineageMarker):
            raise TypeError("resume ownership requires a typed run identity")
        if intent is not None and not isinstance(intent, ResumeIntent):
            raise TypeError("resume ownership requires a typed intent")
        if checkpoint is None:
            if intent is not None:
                raise TinkerFinLifecycleError("approval task has no source checkpoint")
            return
        previous = task_resume_values(checkpoint, task_id)
        thread = self._identity(config)
        receipts = resume_receipts(checkpoint, task_id, thread=thread)
        current_intent = None if intent is None else intent.digest
        for index, receipt in receipts.items():
            if index < len(previous):
                if index >= len(values):
                    raise TinkerFinLifecycleError(
                        "resume cannot truncate a reserved prefix"
                    )
                if receipt.prefix_digest != resume_prefix_digest(previous[: index + 1]):
                    raise TinkerFinLifecycleError(
                        "native resume lost its reserved prefix"
                    )
                continue
            if (
                receipt.owner != owner
                or receipt.intent_digest != current_intent
                or index >= len(values)
                or receipt.prefix_digest != resume_prefix_digest(values[: index + 1])
            ):
                raise TinkerFinLifecycleError(
                    "task has an unfinished resume reservation"
                )
        for index, _value in enumerate(values):
            digest = resume_prefix_digest(values[: index + 1])
            if index < len(previous):
                # Native callers may use arbitrary non-JSON values. Never infer
                # their old ownership from the current writer or serializer bytes.
                existing = receipts.get(index)
                if existing is not None and existing.prefix_digest != digest:
                    raise TinkerFinLifecycleError(
                        "resume cannot replace a reserved prefix"
                    )
                continue
            if digest is None:
                continue
            proposed = ResumeReceipt(
                thread=thread,
                graph_namespace=options.get("checkpoint_ns", ""),
                checkpoint_id=checkpoint.checkpoint["id"],
                task_id=task_id,
                index=index,
                prefix_digest=digest,
                owner=owner,
                intent_digest=current_intent,
            )
            if index in receipts:
                continue
            await self._saver.aput_writes(
                self._physical(config),
                [(RESUME_RECEIPT_CHANNEL, proposed.model_dump(mode="json"))],
                receipt_owner(task_id, index),
            )
            verified = await self.aget_tuple(config)
            if (
                verified is None
                or resume_receipts(verified, task_id, thread=thread).get(index)
                != proposed
            ):
                raise TinkerFinLifecycleError(
                    "resume reservation was not durably saved"
                )

    async def adelete_thread(self, thread_id: str) -> None:
        """Delete only the requested thread in this view's namespace."""

        identity = ThreadIdentity(namespace=self._namespace, thread_id=thread_id)
        physical = _physical_thread(identity, self._saver)
        if redis_saver := _redis_saver(self._saver):
            from ._checkpoint_redis import delete_checkpoint_thread

            await delete_checkpoint_thread(redis_saver, physical)
        else:
            await self._saver.adelete_thread(physical)

    def get_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        """Reject synchronous reads before accessing the borrowed saver."""

        del config
        raise NotImplementedError("Runtime checkpoints support asynchronous I/O only")

    def list(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> Iterator[CheckpointTuple]:
        """Reject synchronous history before accessing the borrowed saver."""

        del config, filter, before, limit
        raise NotImplementedError("Runtime checkpoints support asynchronous I/O only")

    def put(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        """Reject synchronous writes before accessing the borrowed saver."""

        del config, checkpoint, metadata, new_versions
        raise NotImplementedError("Runtime checkpoints support asynchronous I/O only")

    def put_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        """Reject synchronous task writes before accessing the borrowed saver."""

        del config, writes, task_id, task_path
        raise NotImplementedError("Runtime checkpoints support asynchronous I/O only")

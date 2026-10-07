"""Persistent files shared safely by agents and application editors."""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime
from uuid import uuid4

from deepagents.backends.protocol import (
    DeleteResult,
    EditResult,
    FileData,
    FileUploadResponse,
    WriteResult,
)
from deepagents.backends.store import StoreBackend
from deepagents.backends.utils import (
    create_file_data,
    file_data_to_string,
    perform_string_replacement,
)
from langgraph.store.base import BaseStore, Item

from tinkerfin_contracts.storage import ConditionalStore, DocumentSnapshot, JsonValue

from ._store import NamespaceStore
from ._store_backend import _AsyncStoreBackend
from .errors import TinkerFinError, TinkerFinErrorCode

__all__ = ["FileConflict", "PersistentFile", "PersistentFiles"]


class FileConflict(TinkerFinError):
    """A file changed after it was read, or a create target already exists."""

    code = TinkerFinErrorCode.FILE_CONFLICT


@dataclass(frozen=True, slots=True)
class PersistentFile:
    """One file snapshot; its etag guards a subsequent update or deletion."""

    path: str
    content: bytes
    etag: str
    updated_at: datetime


def _path(path: str) -> str:
    if not path.startswith("/") or "\x00" in path or "\\" in path:
        raise ValueError("File paths must be absolute virtual paths")
    if any(part in {"", ".", ".."} for part in path[1:].split("/")):
        raise ValueError("File paths must identify one canonical file")
    return path


def _etag(item: Item | DocumentSnapshot) -> str:
    # Include every stored field: ordinary Store writes also invalidate editors.
    return hashlib.sha256(
        json.dumps(
            item.value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
    ).hexdigest()


class PersistentFiles:
    """Bind a file collection to a borrowed, isolated Store.

    Obtain this through ``TinkerFin.with_namespace(...).files(...)``. The host
    retains ownership of the Store. Updates and deletions require the etag of a
    previously read snapshot; conflicts never retry or overwrite implicitly.
    The backend property supplies the same collection to agent filesystem tools.
    """

    def __init__(self, store: BaseStore, namespace: tuple[str, ...]) -> None:
        """Bind a collection without opening or taking ownership of resources.

        Args:
            store: Borrowed Store supporting conditional writes and exact queries.
            namespace: Nonempty relative collection labels without padding.

        Raises:
            ValueError: The collection namespace is empty or padded.
            NotImplementedError: The Store lacks the required document operations.
        """
        if not namespace or any(not part or part != part.strip() for part in namespace):
            raise ValueError("A nonempty canonical file namespace is required")
        if not isinstance(store, ConditionalStore):
            raise NotImplementedError(
                "Persistent files require conditional writes and exact queries"
            )
        self._store = store
        self._namespace = namespace
        self._codec = StoreBackend(namespace=lambda _: namespace, store=store)

    @property
    def backend(self) -> StoreBackend:
        """Return a runtime route using the same collection and isolation owner.

        This route borrows the executing Runtime's Store. Binding it to another
        namespace is rejected rather than reading the wrong owner's files.

        Returns:
            An asynchronous file route for the bound collection.

        Raises:
            TypeError: The collection was not bound through a TinkerFin namespace.
        """
        if not isinstance(self._store, NamespaceStore):
            raise TypeError("Runtime file routes require a TinkerFin namespace")
        return _PersistentFileBackend(self._namespace, self._store.namespace)

    def _file_data(self, item: Item | DocumentSnapshot) -> FileData:
        # The codec's Item model stays within this Deep Agents integration boundary.
        source = (
            item
            if isinstance(item, Item)
            else Item(
                value=item.value,
                key=item.key,
                namespace=self._namespace,
                created_at=item.created_at,
                updated_at=item.updated_at,
            )
        )
        return self._codec._convert_store_item_to_file_data(source)

    def _snapshot(self, item: Item | DocumentSnapshot) -> PersistentFile:
        file = self._file_data(item)
        text = file_data_to_string(file)
        content = (
            base64.b64decode(text, validate=True)
            if file["encoding"] == "base64"
            else text.encode("utf-8")
        )
        return PersistentFile(item.key, content, _etag(item), item.updated_at)

    async def list(self, *, limit: int = 100, offset: int = 0) -> list[PersistentFile]:
        """Read one exact collection page, ordered by update time then path.

        Args:
            limit: Maximum files returned, from 1 through 100.
            offset: Nonnegative number of collection files to skip.

        Returns:
            Snapshots with independent content and conditions for later writes.

        Raises:
            ValueError: The page is invalid or a stored file cannot be decoded.
        """
        if not 1 <= limit <= 100 or offset < 0:
            raise ValueError("Invalid file page")
        assert isinstance(self._store, ConditionalStore)
        items = await self._store.asearch_exact(
            self._namespace, limit=limit, offset=offset
        )
        return [self._snapshot(item) for item in items]

    async def _documents(self) -> AsyncIterator[DocumentSnapshot]:
        assert isinstance(self._store, ConditionalStore)
        offset = 0
        while True:
            page = await self._store.asearch_exact(
                self._namespace, limit=100, offset=offset
            )
            for item in page:
                yield item
            if len(page) < 100:
                return
            offset += len(page)

    async def read(self, path: str) -> PersistentFile:
        """Read one file and the condition for a later edit or deletion.

        Args:
            path: Canonical absolute virtual file path.

        Returns:
            File bytes, etag and last update time.

        Raises:
            FileNotFoundError: No file exists at this path.
            ValueError: The path is invalid or stored content cannot be decoded.
        """
        item = await self._store.aget(self._namespace, _path(path))
        if item is None:
            raise FileNotFoundError(path)
        return self._snapshot(item)

    async def create(self, path: str, content: bytes) -> PersistentFile:
        """Create a file only while its path is absent.

        Args:
            path: Canonical absolute virtual file path.
            content: Original file bytes, including binary content.

        Returns:
            The committed snapshot, even if another writer subsequently changes it.

        Raises:
            FileConflict: The target exists or is created by another writer.
            ValueError: The file path is invalid.
        """
        return await self._write(_path(path), content, None)

    async def update(
        self, path: str, content: bytes, *, expected_etag: str
    ) -> PersistentFile:
        """Replace one unchanged file without replaying a stale editor's draft.

        Args:
            path: Canonical absolute virtual file path.
            content: Replacement file bytes.
            expected_etag: Condition from the last read or committed snapshot.

        Returns:
            The committed replacement, with a fresh etag even for equal bytes.

        Raises:
            FileConflict: The file changed, disappeared or was recreated.
            ValueError: The path is invalid or stored content cannot be decoded.
        """
        path = _path(path)
        current = await self._expected(path, expected_etag)
        return await self._write(path, content, current)

    async def delete(self, path: str, *, expected_etag: str) -> None:
        """Delete exactly one unchanged file, retaining recreated files.

        Args:
            path: Canonical absolute virtual file path, never a directory.
            expected_etag: Condition from a previous read or committed snapshot.

        Raises:
            FileConflict: The file changed, disappeared or was recreated.
            ValueError: The file path is invalid.
        """
        path = _path(path)
        current = await self._expected(path, expected_etag)
        assert isinstance(self._store, ConditionalStore)
        if not await self._store.acompare_and_set(
            self._namespace, path, expected=current.value, value=None
        ):
            raise FileConflict("File changed; read it again before deleting")

    async def _expected(self, path: str, expected_etag: str) -> Item:
        current = await self._store.aget(self._namespace, path)
        if current is None or _etag(current) != expected_etag:
            raise FileConflict("File changed; read it again before saving")
        return current

    async def _write(
        self, path: str, content: bytes, current: Item | None
    ) -> PersistentFile:
        try:
            file = create_file_data(content.decode("utf-8"), encoding="utf-8")
        except UnicodeDecodeError:
            file = create_file_data(
                base64.b64encode(content).decode("ascii"), encoding="base64"
            )
        value: dict[str, JsonValue] = dict(
            self._codec._convert_file_data_to_store_value(file)
        )
        # A fresh mutation identity also distinguishes delete-and-recreate and
        # equal-content replacements without depending on clock granularity.
        value["mutation_id"] = uuid4().hex
        if current is not None and isinstance(current.value.get("created_at"), str):
            value["created_at"] = current.value["created_at"]
        assert isinstance(self._store, ConditionalStore)
        if not await self._store.acompare_and_set(
            self._namespace,
            path,
            expected=None if current is None else current.value,
            value=value,
        ):
            raise FileConflict("File changed; read it again before saving")
        # Return our committed snapshot, never another writer's subsequent bytes.
        now = datetime.fromisoformat(str(value["modified_at"]))
        return self._snapshot(
            Item(
                value=value,
                key=path,
                namespace=self._namespace,
                created_at=now,
                updated_at=now,
            )
        )


class _PersistentFileBackend(_AsyncStoreBackend):
    """Use conditional writes for a runtime's persistent file route."""

    def __init__(self, namespace: tuple[str, ...], owner: str) -> None:
        super().__init__(namespace=lambda _: namespace)
        self._owner = owner

    def _get_store(self) -> BaseStore:
        store = super()._get_store()
        if not isinstance(store, NamespaceStore) or store.namespace != self._owner:
            raise ValueError("Persistent file route and Runtime namespaces differ")
        return store

    def _files_service(self) -> PersistentFiles:
        return PersistentFiles(self._get_store(), self._get_namespace())

    async def _files(self) -> dict[str, FileData]:
        service = self._files_service()
        files: dict[str, FileData] = {}
        async for item in service._documents():
            try:
                files[item.key] = service._file_data(item)
            except ValueError:
                # As StoreBackend does, omit non-file documents from file searches.
                continue
        return files

    async def awrite(self, file_path: str, content: str) -> WriteResult:
        files = self._files_service()
        try:
            try:
                snapshot = await files.read(file_path)
            except FileNotFoundError:
                await files.create(file_path, content.encode("utf-8"))
            else:
                await files.update(
                    file_path, content.encode("utf-8"), expected_etag=snapshot.etag
                )
            return WriteResult(path=file_path)
        except FileConflict as error:
            return WriteResult(error=error.message)

    async def aedit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> EditResult:
        files = self._files_service()
        try:
            snapshot = await files.read(file_path)
            result = perform_string_replacement(
                snapshot.content.decode("utf-8"), old_string, new_string, replace_all
            )
            if isinstance(result, str):
                return EditResult(error=result)
            content, occurrences = result
            await files.update(
                file_path, content.encode("utf-8"), expected_etag=snapshot.etag
            )
            return EditResult(path=file_path, occurrences=occurrences)
        except (FileConflict, FileNotFoundError, UnicodeDecodeError) as error:
            return EditResult(error=str(error))

    async def adelete(self, file_path: str) -> DeleteResult:
        files = self._files_service()
        base = file_path.rstrip("/")
        if base:
            _path(base)
        elif file_path != "/":
            raise ValueError("File paths must be absolute virtual paths")
        prefix = base + "/"
        # Deep Agents 0.7.19 BackendProtocol.delete includes recursive directories.
        # Each file is conditional; a stale snapshot stops deletion without replay.
        # Keep only paths and conditions, not another copy of every file's bytes.
        targets = [
            (item.key, _etag(item))
            async for item in files._documents()
            if item.key == base or item.key.startswith(prefix)
        ]
        if not targets:
            return DeleteResult(error=f"File '{file_path}' not found")
        try:
            for path, etag in sorted(targets):
                await files.delete(path, expected_etag=etag)
        except (FileConflict, FileNotFoundError) as error:
            return DeleteResult(
                error=f"Deletion incomplete; some files may already be deleted. Read remaining files before retrying: {error}"
            )
        # New descendants belong to their writer, not the stale delete snapshot.
        # Report observed survivors rather than deleting them or declaring success.
        async for item in files._documents():
            if item.key == base or item.key.startswith(prefix):
                return DeleteResult(
                    error="Deletion incomplete; new or changed files remain. Some files may already be deleted. Read remaining files before retrying"
                )
        return DeleteResult(path=file_path)

    async def aupload_files(
        self, files: list[tuple[str, bytes]]
    ) -> list[FileUploadResponse]:
        service = self._files_service()
        results: list[FileUploadResponse] = []
        for path, content in files:
            try:
                try:
                    snapshot = await service.read(path)
                except FileNotFoundError:
                    await service.create(path, content)
                else:
                    await service.update(path, content, expected_etag=snapshot.etag)
                results.append(FileUploadResponse(path=path, error=None))
            except FileConflict as error:
                results.append(FileUploadResponse(path=path, error=error.message))
        return results

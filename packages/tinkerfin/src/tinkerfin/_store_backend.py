"""Use asynchronous Store I/O for Deep Agents' persistent file operations."""

from __future__ import annotations

import base64
from copy import copy

from deepagents.backends import CompositeBackend, StoreBackend
from deepagents.backends.protocol import (
    BackendProtocol,
    FileData,
    FileDownloadResponse,
    FileInfo,
    FileUploadResponse,
    GlobResult,
    GrepResult,
    LsResult,
)
from deepagents.backends.utils import (
    _glob_search_files,
    create_file_data,
    file_data_to_string,
    grep_matches_from_files,
)


class _AsyncStoreBackend(StoreBackend):
    """Complete the built-in file adapter without enabling synchronous Store access.

    Deep Agents 0.7.5 StoreBackend implements async read/write/edit/delete, but
    inherits thread-based wrappers for listing, search, and file transfer from
    BackendProtocol. Those wrappers call synchronous BaseStore operations. Here
    all I/O uses the borrowed Store's async methods; file encoding, namespace
    resolution, filtering, and result shapes retain the locked adapter contract.
    """

    async def _files(self) -> dict[str, FileData]:
        items = await self._asearch_store_paginated(
            self._get_store(), self._get_namespace()
        )
        files: dict[str, FileData] = {}
        for item in items:
            try:
                files[item.key] = self._convert_store_item_to_file_data(item)
            except ValueError:
                # Listing and search skip non-file items, as StoreBackend does.
                continue
        return files

    async def als(self, path: str) -> LsResult:
        """List files using asynchronous Store pagination."""

        prefix = path if path.endswith("/") else path + "/"
        entries: list[FileInfo] = []
        directories: set[str] = set()
        for key, file in (await self._files()).items():
            if not key.startswith(prefix):
                continue
            relative = key[len(prefix) :]
            if "/" in relative:
                directories.add(prefix + relative.split("/")[0] + "/")
            else:
                entries.append(_file_info(key, file))
        entries.extend(
            FileInfo(path=path, is_dir=True, size=0, modified_at="")
            for path in directories
        )
        return LsResult(entries=sorted(entries, key=lambda item: item["path"]))

    async def agrep(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
        *,
        max_count: int | None = None,
    ) -> GrepResult:
        """Search the current Store namespace for literal text."""

        return grep_matches_from_files(
            await self._files(), pattern, path, glob, max_count=max_count
        )

    async def aglob(self, pattern: str, path: str | None = None) -> GlobResult:
        """Find matching file paths without invoking a synchronous Store."""

        files = await self._files()
        matches = _glob_search_files(files, pattern, path)
        return GlobResult(
            matches=[]
            if matches == "No files found"
            else [_file_info(key, files[key]) for key in matches.split("\n")]
        )

    async def aupload_files(
        self, files: list[tuple[str, bytes]]
    ) -> list[FileUploadResponse]:
        """Persist file contents in input order with the adapter's text/binary encoding."""

        store, namespace = self._get_store(), self._get_namespace()
        responses: list[FileUploadResponse] = []
        for path, content in files:
            try:
                text = content.decode("utf-8")
                file = create_file_data(text, encoding="utf-8")
            except UnicodeDecodeError:
                file = create_file_data(
                    base64.standard_b64encode(content).decode("ascii"),
                    encoding="base64",
                )
            await store.aput(
                namespace, path, self._convert_file_data_to_store_value(file)
            )
            responses.append(FileUploadResponse(path=path, error=None))
        return responses

    async def adownload_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        """Read file bytes in input order, retaining missing-file results."""

        store, namespace = self._get_store(), self._get_namespace()
        responses: list[FileDownloadResponse] = []
        for path in paths:
            item = await store.aget(namespace, path)
            if item is None:
                responses.append(
                    FileDownloadResponse(
                        path=path, content=None, error="file_not_found"
                    )
                )
                continue
            file = self._convert_store_item_to_file_data(item)
            text = file_data_to_string(file)
            content = (
                base64.standard_b64decode(text)
                if file["encoding"] == "base64"
                else text.encode("utf-8")
            )
            responses.append(
                FileDownloadResponse(path=path, content=content, error=None)
            )
        return responses


def _file_info(path: str, file: FileData) -> FileInfo:
    content = file_data_to_string(file)
    size = len(
        base64.standard_b64decode(content)
        if file["encoding"] == "base64"
        else content.encode("utf-8")
    )
    return FileInfo(
        path=path,
        is_dir=False,
        size=size,
        modified_at=file.get("modified_at", ""),
    )


def async_store_backend(backend: BackendProtocol) -> BackendProtocol:
    """Derive built-in Store routes while preserving borrowed and custom backends.

    Only the exact locked StoreBackend needs completion. Custom implementations
    remain responsible for their async operations. Composite copies retain their
    own behavior and route order without mutating the provider's shared object.
    """

    derived: dict[int, BackendProtocol] = {}

    def adapt(current: BackendProtocol) -> BackendProtocol:
        if id(current) in derived:
            return derived[id(current)]
        if type(current) is StoreBackend:
            result = _AsyncStoreBackend(namespace=current._namespace)
            derived[id(current)] = result
            return result
        if isinstance(current, CompositeBackend):
            composite = copy(current)
            derived[id(current)] = composite
            composite.default = adapt(current.default)
            composite.routes = {
                path: adapt(route) for path, route in current.routes.items()
            }
            composite.sorted_routes = [
                (path, composite.routes[path]) for path, _ in current.sorted_routes
            ]
            return composite
        return current

    return adapt(backend)

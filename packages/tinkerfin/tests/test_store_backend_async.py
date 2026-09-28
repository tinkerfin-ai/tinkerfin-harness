"""The locked file adapter uses async Store operations and preserves file results."""

import asyncio
from collections.abc import Iterable

import pytest
from langgraph.store.base import Op, Result
from langgraph.store.memory import InMemoryStore
from test_runtime_store import _runtime_store

from tinkerfin._store_backend import _AsyncStoreBackend


@pytest.mark.parametrize(
    "content", [b"", b"ASCII\n", "中文😀\n".encode(), b"\x00\xff\x80"]
)
async def test_listing_and_glob_report_original_file_byte_length(
    content: bytes,
) -> None:
    backend = _AsyncStoreBackend(namespace=lambda _: ("files",), store=InMemoryStore())
    assert (await backend.aupload_files([("/sample", content)]))[0].error is None
    listing = await backend.als("/")
    glob = await backend.aglob("*", "/")
    assert listing.entries is not None and glob.matches is not None
    assert listing.entries[0].get("size") == len(content)
    assert glob.matches[0].get("size") == len(content)
    assert (await backend.adownload_files(["/sample"]))[0].content == content


async def test_file_operations_isolate_namespaces_and_preserve_text_and_bytes() -> None:
    shared = InMemoryStore()
    alpha = _AsyncStoreBackend(
        namespace=lambda _: ("files",), store=await _runtime_store(shared, "alpha")
    )
    beta = _AsyncStoreBackend(
        namespace=lambda _: ("files",), store=await _runtime_store(shared, "beta")
    )
    for backend, owner in ((alpha, "alpha"), (beta, "beta")):
        uploaded = await backend.aupload_files(
            [("/report.txt", owner.encode()), ("/sub/image.png", b"\x00\xff\x80")]
        )
        assert [item.path for item in uploaded] == ["/report.txt", "/sub/image.png"]
        assert all(item.error is None for item in uploaded)
        listed = await backend.als("/")
        assert listed.error is None
        assert listed.entries is not None
        assert [item["path"] for item in listed.entries] == ["/report.txt", "/sub/"]
        matched = await backend.aglob("**/*.txt", "/")
        assert matched.error is None
        assert matched.matches is not None
        assert [item["path"] for item in matched.matches] == ["/report.txt"]
        found = await backend.agrep(owner, "/", max_count=1)
        assert found.error is None and found.matches
        assert found.matches[0]["path"] == "/report.txt"
        downloaded = await backend.adownload_files(
            ["/sub/image.png", "/report.txt", "/missing.txt"]
        )
        assert [item.content for item in downloaded] == [
            b"\x00\xff\x80",
            owner.encode(),
            None,
        ]
        assert downloaded[-1].error == "file_not_found"
        assert (await backend.awrite("/draft.txt", "draft")).error is None
        assert (await backend.aedit("/draft.txt", "draft", "final")).error is None
        read = await backend.aread("/draft.txt")
        assert read.file_data is not None and read.file_data["content"] == "final"
        assert (await backend.adelete("/draft.txt")).error is None
    await alpha.adelete("/report.txt")
    assert (await alpha.adownload_files(["/report.txt"]))[0].error == "file_not_found"
    assert (await beta.adownload_files(["/report.txt"]))[0].content == b"beta"


async def test_cancelled_file_io_stops_before_writing_the_next_file() -> None:
    class GatedStore(InMemoryStore):
        def __init__(self) -> None:
            super().__init__()
            self.entered = asyncio.Event()
            self.blocked = False
            self.calls = 0

        async def abatch(self, ops: Iterable[Op]) -> list[Result]:
            self.calls += 1
            if self.blocked:
                self.entered.set()
                await asyncio.Event().wait()
            return await super().abatch(ops)

    shared = GatedStore()
    backend = _AsyncStoreBackend(
        namespace=lambda _: ("files",), store=await _runtime_store(shared, "alpha")
    )
    shared.blocked = True
    transfer = asyncio.create_task(
        backend.aupload_files([("/first.txt", b"first"), ("/second.txt", b"second")])
    )
    await shared.entered.wait()
    transfer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await transfer
    assert shared.calls == 1
    shared.blocked = False
    assert await shared.asearch(()) == []

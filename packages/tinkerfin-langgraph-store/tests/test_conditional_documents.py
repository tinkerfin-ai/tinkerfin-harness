"""Conditional writes reject stale snapshots across independent Store objects."""

import asyncio
import json
import sys

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from tinkerfin_langgraph_store import SqlAlchemyStore


async def test_condition_ignores_nested_object_order_but_preserves_json_types(
    store: SqlAlchemyStore,
) -> None:
    scope = ("condition",)
    value = {"a": 1, "nested": {"x": True, "array": [1, 2]}}
    await store.aput(scope, "note", value)
    assert not await store.acompare_and_set(
        scope, "note", expected={"a": 1.0, "nested": value["nested"]}, value=None
    )
    assert not await store.acompare_and_set(
        scope,
        "note",
        expected={"a": 1, "nested": {"x": 1, "array": [1, 2]}},
        value=None,
    )
    assert not await store.acompare_and_set(
        scope,
        "note",
        expected={"a": 1, "nested": {"x": True, "array": [2, 1]}},
        value=None,
    )
    assert await store.acompare_and_set(
        scope,
        "note",
        expected={"nested": {"array": [1, 2], "x": True}, "a": 1},
        value=None,
    )


async def test_exact_document_pages_exclude_child_namespaces_before_paging(
    store: SqlAlchemyStore,
) -> None:
    await store.aput(("files",), "parent", {"content": "parent"})
    await store.aput(("files", "private"), "child", {"content": "secret"})
    page = await store.asearch_exact(("files",), limit=1, offset=0)
    assert [item.key for item in page] == ["parent"]
    assert await store.asearch_exact(("files",), limit=1, offset=1) == []


async def test_conditional_create_update_delete(store: SqlAlchemyStore) -> None:
    scope = ("project", "memory")
    assert await store.acompare_and_set(
        scope, "note", expected=None, value={"text": "a"}
    )
    assert not await store.acompare_and_set(
        scope, "note", expected=None, value={"text": "b"}
    )
    assert not await store.acompare_and_set(
        scope, "note", expected={"text": "b"}, value=None
    )
    assert await store.acompare_and_set(
        scope, "note", expected={"text": "a"}, value={"text": "c"}
    )
    item = await store.aget(scope, "note")
    assert item is not None and item.value == {"text": "c"}
    assert await store.acompare_and_set(
        scope, "note", expected={"text": "c"}, value=None
    )
    assert await store.aget(scope, "note") is None


async def test_independent_writers_cannot_both_replace_the_same_snapshot(
    store: SqlAlchemyStore,
    engine: AsyncEngine,
) -> None:
    scope = ("project", "memory")
    await store.aput(scope, "note", {"text": "base"})
    async with SqlAlchemyStore(engine) as other:
        results = await asyncio.gather(
            store.acompare_and_set(
                scope, "note", expected={"text": "base"}, value={"text": "one"}
            ),
            other.acompare_and_set(
                scope, "note", expected={"text": "base"}, value={"text": "two"}
            ),
        )
    assert sorted(results) == [False, True]
    current = await store.aget(scope, "note")
    assert current is not None and current.value["text"] in {"one", "two"}


async def test_condition_distinguishes_boolean_and_integer(
    store: SqlAlchemyStore,
) -> None:
    await store.aput(("scope",), "key", {"value": True})
    assert not await store.acompare_and_set(
        ("scope",), "key", expected={"value": 1}, value=None
    )


_PROCESS_WRITER = """
import asyncio, json, sys
from sqlalchemy.ext.asyncio import create_async_engine
from tinkerfin_langgraph_store import SqlAlchemyStore
settings = json.loads(sys.stdin.readline())
print('ready', flush=True)
assert sys.stdin.readline().strip() == 'write'
async def run():
    options = {'connect_args': {'server_settings': {'search_path': settings['schema']}}} if settings.get('schema') else {}
    engine = create_async_engine(settings['url'], **options)
    try:
        async with SqlAlchemyStore(engine) as store:
            changed = await store.acompare_and_set(
                ('process',), 'note', expected={'text': 'base'},
                value={'text': settings['text']},
            )
            print(json.dumps(changed), flush=True)
    finally:
        await engine.dispose()
asyncio.run(run())
"""


async def test_processes_cannot_overwrite_the_same_snapshot(
    store: SqlAlchemyStore,
    engine: AsyncEngine,
) -> None:
    """Independent interpreters share only the database write condition."""
    await store.aput(("process",), "note", {"text": "base"})
    schema = None
    if engine.dialect.name == "postgresql":
        async with engine.connect() as connection:
            schema = await connection.scalar(text("SELECT current_schema()"))
    processes: list[asyncio.subprocess.Process] = []
    try:
        for replacement in ("one", "two"):
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-c",
                _PROCESS_WRITER,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            processes.append(process)
            assert process.stdin is not None
            process.stdin.write(
                (
                    json.dumps(
                        {
                            "url": engine.url.render_as_string(hide_password=False),
                            "text": replacement,
                            "schema": schema,
                        }
                    )
                    + "\n"
                ).encode()
            )
            await process.stdin.drain()
        for process in processes:
            assert process.stdout is not None
            assert await process.stdout.readline() == b"ready\n"
        outputs = await asyncio.gather(
            *(process.communicate(b"write\n") for process in processes)
        )
        assert all(process.returncode == 0 for process in processes), [
            stderr.decode() for _, stderr in outputs
        ]
        assert sorted(json.loads(stdout) for stdout, _ in outputs) == [False, True]
        saved = await store.aget(("process",), "note")
        assert saved is not None and saved.value["text"] in {"one", "two"}
    finally:
        for process in processes:
            if process.returncode is None:
                process.terminate()
                await process.wait()

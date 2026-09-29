"""离线资料更新的完整性、确定性与失败后保留当前文件"""

import asyncio
import json
from unittest.mock import AsyncMock

import httpx
import pytest

from tinkerfin_studio.models.capability_data import parse_source, read_catalog
from tinkerfin_studio.models.update_capabilities import download_source, update_catalog


def source():
    return json.dumps(
        {
            "fixture": {
                "models": {
                    "vision": {"modalities": {"input": ["text", "image"]}},
                    "text": {"modalities": {"input": ["text"]}},
                    "unknown": {},
                }
            }
        }
    ).encode()


def test_update_preserves_three_states_and_is_deterministic(tmp_path):
    target = tmp_path / "catalog.json"
    diff = update_catalog(source(), target)
    assert '+    "vision": "supported"' in diff
    first = target.read_bytes()
    assert update_catalog(source(), target) == ""
    assert target.read_bytes() == first
    catalog = read_catalog(first)
    assert catalog.providers["fixture"] == {
        "vision": "supported",
        "text": "unsupported",
        "unknown": "unknown",
    }


@pytest.mark.parametrize(
    "invalid",
    [
        b'{"same":{},"same":{}}',
        b"{}",
        b"not json",
        b'{"p":{"models":{"m":{"modalities":{"input":"image"}}}}}',
        b'{"p":{"models":{"m":{"modalities":{"input":[123]}}}}}',
    ],
)
def test_invalid_source_never_overwrites_current_catalog(tmp_path, invalid):
    target = tmp_path / "catalog.json"
    update_catalog(source(), target)
    before = target.read_bytes()
    with pytest.raises(ValueError):
        update_catalog(invalid, target)
    assert target.read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["catalog.json"]


def test_catalog_corruption_is_an_error():
    data = parse_source(source()).model_dump()
    data["providers"]["fixture"]["vision"] = "unsupported"
    with pytest.raises(ValueError, match="校验和"):
        read_catalog(json.dumps(data).encode())


async def test_download_http_errors_and_size_limits(monkeypatch):
    from tinkerfin_studio.models import update_capabilities

    monkeypatch.setattr(update_capabilities, "MAX_SOURCE_BYTES", 3)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=b"large")
        )
    ) as client:
        with pytest.raises(ValueError, match="20 MiB"):
            await download_source(client)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(503))
    ) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await download_source(client)


async def test_download_failure_closes_response_and_preserves_cancellation():
    class InterruptedStream(httpx.AsyncByteStream):
        def __init__(self, error):
            self.error = error
            self.closed = AsyncMock()

        async def __aiter__(self):
            yield b"{"
            raise self.error

        async def aclose(self):
            await self.closed()

    for error in (httpx.ReadTimeout("controlled timeout"), asyncio.CancelledError()):
        stream = InterruptedStream(error)
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, stream=stream)
            )
        ) as client:
            with pytest.raises(type(error)):
                await download_source(client)
        stream.closed.assert_awaited_once()

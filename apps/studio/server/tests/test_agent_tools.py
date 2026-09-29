"""网页搜索 Tool 的模型可见输入和安全结果"""

from __future__ import annotations

import asyncio

import pytest
from langchain_core.messages import ToolMessage

from tinkerfin_studio.agent import tools as tools_module
from tinkerfin_studio.agent.tools import build_web_search_tool
from tinkerfin_studio.services.http import SearchItem, SearchResult
from tinkerfin_studio.services.schemas import SearchConfig
from tinkerfin_studio.services.service import ResolvedService


def configured() -> ResolvedService:
    return ResolvedService("search", SearchConfig(), "fingerprint", "secret")


async def test_search_tool_passes_only_model_visible_query_and_returns_standard_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def search(service, request):
        assert service.api_key == "secret"
        assert (request.query, request.max_results, request.topic) == (
            "current news",
            3,
            "news",
        )
        return SearchResult(
            results=[
                SearchItem.model_validate(
                    {
                        "title": "Result",
                        "url": "https://example.com/result",
                        "content": "Summary",
                        "score": 0.9,
                    }
                )
            ]
        )

    monkeypatch.setattr(tools_module, "search_web", search)
    result = await build_web_search_tool(configured()).ainvoke(
        {
            "name": "web_search",
            "args": {"query": "current news", "max_results": 3, "topic": "news"},
            "id": "call-1",
            "type": "tool_call",
        }
    )
    assert isinstance(result, ToolMessage)
    assert isinstance(result.content, str)
    assert result.content.startswith('{"results":')
    assert result.artifact["results"][0]["title"] == "Result"
    assert "secret" not in result.content


async def test_search_tool_reports_missing_service_and_safe_supplier_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    missing = await build_web_search_tool(None).ainvoke({"query": "current news"})
    assert "not configured" in missing

    async def fail(service, request):
        raise ValueError("upstream secret response")

    monkeypatch.setattr(tools_module, "search_web", fail)
    failed = await build_web_search_tool(configured()).ainvoke(
        {"query": "current news"}
    )
    assert "Web search failed" in failed
    assert "upstream secret response" not in failed


async def test_search_tool_preserves_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def cancel(service, request):
        raise asyncio.CancelledError

    monkeypatch.setattr(tools_module, "search_web", cancel)
    with pytest.raises(asyncio.CancelledError):
        await build_web_search_tool(configured()).ainvoke({"query": "current news"})

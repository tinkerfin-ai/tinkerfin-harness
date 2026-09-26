"""链路查询 HTTP 参数与错误包络"""

from httpx import ASGITransport, AsyncClient

from tinkerfin_studio.api.dependencies import get_conversation_history_service
from tinkerfin_studio.application import create_application
from tinkerfin_studio.conversation.trace_responses import ConversationTraceUpdate


def test_conversation_schema_distinguishes_model_details_from_execution_requests():
    """响应的实际字段与公开模式一致，链路查询仍提供完整模型请求"""
    schemas = create_application(lifespan=None).openapi()["components"]["schemas"]
    model = schemas["ConversationModelNode"]
    assert "request" not in model["properties"]
    assert model["additionalProperties"] is False
    assert "request" in schemas["ConversationExecutionNode"]["properties"]
    assert "request" in schemas["AgUiTraceGraphNode"]["properties"]
    page = schemas["ConversationGraphQueryPage"]
    assert {"generation", "headRunId"} <= set(page["required"])
    update = ConversationTraceUpdate.model_json_schema(by_alias=True)
    assert "hasEvents" in update["required"]
    assert {"events", "facts", "projections", "summary"}.isdisjoint(
        update["properties"]
    )


async def test_trace_graph_query_rejects_noncanonical_namespace() -> None:
    """非法图命名空间必须在进入业务查询前返回统一校验错误"""

    application = create_application(lifespan=None)
    application.dependency_overrides[get_conversation_history_service] = object

    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://test",
    ) as client:
        response = await client.get(
            "/api/conversation/thread-1/trace/graph",
            params={"graph_namespace": "tools| invalid"},
        )

    assert response.status_code == 422
    assert response.json() == {
        "code": 422,
        "message": "请求参数校验失败",
        "data": None,
    }


async def test_trace_graph_routes_reject_limits_above_complete_view_boundary() -> None:
    """完整链路查询最多接收一千个节点"""

    application = create_application(lifespan=None)
    application.dependency_overrides[get_conversation_history_service] = object

    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://test",
    ) as client:
        query_response = await client.get(
            "/api/conversation/thread-1/trace/graph",
            params={"limit": 1001},
        )
        follow_response = await client.get(
            "/api/conversation/thread-1/trace/graph/follow",
            params={"limit": 1001},
        )

    assert query_response.status_code == 422
    assert follow_response.status_code == 422


def test_trace_graph_routes_publish_the_complete_view_limit() -> None:
    """查询和实时订阅对外发布同一个节点上限"""

    schema = create_application(lifespan=None).openapi()
    for path in (
        "/api/conversation/{thread_id}/trace/graph",
        "/api/conversation/{thread_id}/trace/graph/follow",
    ):
        parameters = schema["paths"][path]["get"]["parameters"]
        limit = next(
            parameter for parameter in parameters if parameter["name"] == "limit"
        )
        assert limit["schema"]["maximum"] == 1000


def test_trace_graph_routes_publish_only_the_current_filter_contract() -> None:
    """链路路由只公开平级事件与精确关系筛选参数"""

    schema = create_application(lifespan=None).openapi()
    expected_common = {
        "thread_id",
        "kind",
        "status",
        "modelCallId",
        "agent",
        "provider",
        "model",
        "graph_namespace",
        "query",
        "startedAfter",
        "startedBefore",
        "limit",
        "authorization",
    }
    query_parameters = {
        parameter["name"]
        for parameter in schema["paths"]["/api/conversation/{thread_id}/trace/graph"][
            "get"
        ]["parameters"]
    }
    follow_parameters = {
        parameter["name"]
        for parameter in schema["paths"][
            "/api/conversation/{thread_id}/trace/graph/follow"
        ]["get"]["parameters"]
    }

    assert query_parameters == expected_common | {"cursor"}
    assert follow_parameters == expected_common
    assert not {
        "parentId",
        "middleware",
        "skill",
        "includeTechnicalNodes",
        "includeAncestorNodes",
    } & (query_parameters | follow_parameters)

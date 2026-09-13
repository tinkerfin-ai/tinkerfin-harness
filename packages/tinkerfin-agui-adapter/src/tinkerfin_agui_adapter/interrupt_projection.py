"""Project resolved native human-input requests to the public AG-UI contract."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from ag_ui.core import Interrupt
from pydantic import JsonValue, ValidationError

from .errors import HitlCorrelationError
from .hitl import HitlRequest
from .ids import ScopedIdCodec
from .models import AgentRuntimeInterrupt, JsonObject
from .reasoning import normalize_operational_data, sanitize_public_data
from .runtime_interrupts import parse_runtime_interrupt, prepare_runtime_ag_ui_interrupt
from .tool_review import TOOL_REVIEW_SCHEMA, ToolReviewInterruptMetadata


def project_interrupt(
    interrupt: AgentRuntimeInterrupt,
    *,
    tool_call_ids: Sequence[str] = (),
    source: Mapping[str, JsonValue] | None = None,
) -> tuple[Interrupt, ...]:
    """Export a native approval or human-input request using resolved Tool IDs.

    Args:
        interrupt: Native interrupt with its complete retained request payload.
        tool_call_ids: Already correlated, scoped Tool IDs in native action order.
            Required for Tool review and empty for other human-input requests.
        source: Verified public provenance, if retained by the caller. Missing
            provenance is not inferred from partial graph information.

    Returns:
        Public interrupts in action order. A multi-action Tool review produces
        one independently addressable interrupt per action.

    Raises:
        HitlCorrelationError: The request or its ordered Tool assignment is invalid.
        ValueError: A declared runtime request violates its response contract.
    """

    provenance = {} if source is None else dict(source)
    source_namespace: tuple[str, ...] | None = None
    if "graphNamespace" in provenance:
        raw_namespace = provenance["graphNamespace"]
        if not isinstance(raw_namespace, list) or any(
            not isinstance(segment, str) or not segment for segment in raw_namespace
        ):
            raise HitlCorrelationError(
                "source graphNamespace must contain graph scope segments"
            )
        source_namespace = tuple(
            segment for segment in raw_namespace if isinstance(segment, str)
        )
    runtime = parse_runtime_interrupt(interrupt.value)
    if runtime is not None:
        if tool_call_ids:
            raise HitlCorrelationError("runtime input cannot reference Tool actions")
        return (prepare_runtime_ag_ui_interrupt(interrupt, runtime, source=provenance),)
    try:
        request = HitlRequest.model_validate(interrupt.value)
    except ValidationError as error:
        if tool_call_ids or (
            isinstance(interrupt.value, dict)
            and (
                "action_requests" in interrupt.value
                or "review_configs" in interrupt.value
            )
        ):
            raise HitlCorrelationError("invalid native Tool review request") from error
        return (
            Interrupt(
                id=interrupt.id,
                reason="langgraph:interrupt",
                metadata={
                    "langgraphValue": sanitize_public_data(interrupt.value),
                    "source": provenance,
                },
            ),
        )
    if len(tool_call_ids) != len(request.action_requests) or len(
        set(tool_call_ids)
    ) != len(tool_call_ids):
        raise HitlCorrelationError("Tool IDs must uniquely match native action order")
    namespaces: set[tuple[str, ...]] = set()
    for tool_id in tool_call_ids:
        try:
            kind, namespace, _raw_id = ScopedIdCodec().decode(tool_id)
        except (TypeError, ValueError) as error:
            raise HitlCorrelationError(
                "review IDs must identify scoped Tool calls"
            ) from error
        if kind != "tool":
            raise HitlCorrelationError("review IDs must identify scoped Tool calls")
        namespaces.add(namespace)
    if len(namespaces) != 1:
        raise HitlCorrelationError("one native review must belong to one graph scope")
    if source_namespace is not None and source_namespace not in namespaces:
        raise HitlCorrelationError(
            "review Tool IDs disagree with the source graph scope"
        )
    public_value = sanitize_public_data(interrupt.value)
    if not isinstance(public_value, dict):
        raise HitlCorrelationError("Tool review must retain a JSON object")
    actions = public_value.get("action_requests")
    if not isinstance(actions, list) or len(actions) != len(request.action_requests):
        raise HitlCorrelationError("Tool review actions are incomplete")
    for public_action, action in zip(actions, request.action_requests, strict=True):
        if not isinstance(public_action, dict):
            raise HitlCorrelationError("Tool review actions must be objects")
        public_action["args"] = normalize_operational_data(action.args.root)
    result: list[Interrupt] = []
    for index, (action, review, tool_id) in enumerate(
        zip(
            request.action_requests,
            request.review_configs,
            tool_call_ids,
            strict=True,
        )
    ):
        metadata = ToolReviewInterruptMetadata(
            schema=TOOL_REVIEW_SCHEMA,
            nativeInterruptId=interrupt.id,
            actionIndex=index,
            toolName=action.name,
            allowedDecisions=tuple(review.allowed_decisions),
            originalArgs=action.args,
        )
        result.append(
            Interrupt(
                id=f"{interrupt.id}#{index}" if len(actions) > 1 else interrupt.id,
                reason="tool_call",
                message=action.description or f"Approve tool {action.name}",
                tool_call_id=tool_id,
                response_schema=_build_response_schema(
                    review.allowed_decisions,
                    action_name=action.name,
                    args_schema=None
                    if review.args_schema is None
                    else review.args_schema.root,
                ),
                metadata={
                    "langgraphValue": public_value,
                    "source": provenance,
                    "deepagents": metadata.model_dump(mode="json", by_alias=True),
                },
            )
        )
    return tuple(result)


def _build_response_schema(
    allowed_decisions: Sequence[str],
    *,
    action_name: str,
    args_schema: dict[str, JsonValue] | None,
) -> dict[str, JsonValue]:
    """Build an AG-UI resume payload schema that fixes the original Tool identity."""

    variants: list[dict[str, object]] = []
    for decision_type in allowed_decisions:
        properties: dict[str, object] = {
            "type": {"const": decision_type},
        }
        required = ["type"]
        if decision_type == "edit":
            properties["edited_action"] = {
                "type": "object",
                "required": ["name", "args"],
                "properties": {
                    "name": {"const": action_name},
                    "args": (
                        {"type": "object"} if args_schema is None else args_schema
                    ),
                },
                "additionalProperties": False,
            }
            required.append("edited_action")
        elif decision_type == "reject":
            properties["message"] = {"type": "string"}
        elif decision_type == "respond":
            properties["message"] = {"type": "string"}
            required.append("message")
        variants.append(
            {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            }
        )
    return JsonObject.model_validate({"oneOf": variants}).root


__all__ = ["project_interrupt"]

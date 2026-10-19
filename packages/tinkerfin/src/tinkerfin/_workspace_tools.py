"""Expose prepared tools through the cloud agent's ordinary tool lifecycle."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any, TypeGuard, cast

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as SchemaValidationError
from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool
from langchain_core.tools.base import TOOL_MESSAGE_BLOCK_TYPES
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import BaseModel, Field, JsonValue, TypeAdapter

from tinkerfin_contracts import (
    ToolDescriptor,
    WorkspaceTool,
    current_workspace_operation,
)

from ._agent_spec import AgentMiddlewareType, ToolDefinition

_arguments = TypeAdapter(dict[str, JsonValue])
_RESERVED = frozenset(
    {
        "ls",
        "read_file",
        "write_file",
        "edit_file",
        "delete",
        "glob",
        "grep",
        "execute",
        "task",
        "write_todos",
        "compact_conversation",
        "start_async_task",
        "check_async_task",
        "update_async_task",
        "cancel_async_task",
        "list_async_tasks",
    }
)


def tool_descriptor(definition: ToolDefinition) -> ToolDescriptor | None:
    if isinstance(definition, BaseTool):
        schema = definition.tool_call_schema
        if isinstance(schema, dict):
            declared = schema
        elif issubclass(schema, BaseModel):
            declared = schema.model_json_schema()
        else:
            # LangChain's public schema type also accepts pydantic.v1 models.
            declared = schema.schema()
        return ToolDescriptor(
            name=definition.name,
            description=definition.description,
            input_schema=_arguments.validate_python(declared),
        )
    declaration = convert_to_openai_tool(definition)
    function = declaration.get("function")
    if not isinstance(function, dict):
        return None
    value = cast(Mapping[str, object], function)
    return ToolDescriptor(
        name=str(value["name"]),
        description=str(value.get("description", "")),
        input_schema=_arguments.validate_python(value.get("parameters", {})),
    )


def model_tool_declarations(
    tools: Sequence[BaseTool | dict[str, Any]],
) -> list[BaseTool | dict[str, Any]]:
    """Preserve self-contained schemas at LangChain's model-binding boundary."""
    result: list[BaseTool | dict[str, Any]] = []
    for tool in tools:
        if isinstance(tool, BaseTool):
            descriptor = tool_descriptor(tool)
            assert descriptor is not None
            # LangChain Core 1.6.4 dereference_refs does not decode URI fragments
            # or JSON Pointer escapes, merges $ref siblings, and visits literal
            # defaults. Its public function-schema input preserves the complete
            # JSON Schema. Only model declarations use that input; ToolNode keeps
            # the original registered tool and its execution validation.
            result.append(
                {
                    "type": "function",
                    "function": {
                        "name": descriptor.name,
                        "description": descriptor.description,
                        "parameters": descriptor.input_schema,
                    },
                }
            )
        else:
            result.append(tool)
    return result


def _content_blocks(value: JsonValue) -> TypeGuard[list[str | dict[str, JsonValue]]]:
    # Match LangChain Core's declared tool content types so media and attachment
    # references remain blocks while ordinary domain arrays remain JSON text.
    return isinstance(value, list) and all(
        isinstance(item, str)
        or isinstance(item, dict)
        and item.get("type") in TOOL_MESSAGE_BLOCK_TYPES
        for item in value
    )


class _WorkspaceTool(BaseTool):
    """Let ToolNode own tracing and correlation while the provider owns execution."""

    prepared: WorkspaceTool = Field(exclude=True)

    def _run(self, *args: Any, **kwargs: Any) -> ToolMessage:
        raise NotImplementedError("Workspace tools require asynchronous execution")

    async def _arun(self, **kwargs: Any) -> ToolMessage:
        arguments = _arguments.validate_python(kwargs)
        json.dumps(arguments, allow_nan=False)
        scope = current_workspace_operation()
        if scope.tool_call_id is None:
            raise ValueError("workspace tools require a real model tool call")
        # LangChain 1.4.2 accepts dict args_schema without validating its payload.
        # Invalid inputs remain tool feedback and never reach the provider.
        try:
            validator = Draft202012Validator(self.prepared.descriptor.input_schema)
            # jsonschema's bundled stubs omit the validator call signature.
            validator.validate(arguments)  # pyright: ignore[reportUnknownMemberType]
        except SchemaValidationError as error:
            return ToolMessage(
                content="Invalid tool arguments: " + error.message,
                tool_call_id=scope.tool_call_id,
                name=self.name,
                status="error",
            )
        result = await self.prepared.invoke(arguments)
        return ToolMessage(
            content=result.value
            if isinstance(result.value, str) or _content_blocks(result.value)
            else json.dumps(result.value, ensure_ascii=False, allow_nan=False),
            tool_call_id=scope.tool_call_id,
            name=self.name,
            status="error" if result.is_error else "success",
        )


def merge_workspace_tools(
    declared: Sequence[ToolDefinition],
    prepared: Sequence[WorkspaceTool],
    middleware: Sequence[AgentMiddlewareType],
    *,
    reserved: Sequence[str] = (),
) -> list[ToolDefinition]:
    """Reject shadowing before registering provider tools on one agent role."""
    names = {*_RESERVED, *reserved}
    for definition in (
        *declared,
        *(tool for item in middleware if hasattr(item, "tools") for tool in item.tools),
    ):
        if isinstance(definition, BaseTool):
            names.add(definition.name)
        elif isinstance(definition, dict):
            value = cast(Mapping[str, object], definition)
            name = value.get("name")
            function = value.get("function")
            if isinstance(function, dict):
                name = cast(Mapping[str, object], function).get("name")
            if isinstance(name, str):
                names.add(name)
        else:
            name = getattr(definition, "__name__", None)
            if isinstance(name, str):
                names.add(name)
    result = list(declared)
    for tool in prepared:
        descriptor = ToolDescriptor.model_validate(tool.descriptor.model_dump())
        if descriptor.name in names:
            raise ValueError(
                f"workspace tool {descriptor.name!r} conflicts with an agent tool"
            )
        names.add(descriptor.name)
        result.append(
            _WorkspaceTool(
                name=descriptor.name,
                description=descriptor.description,
                args_schema=descriptor.input_schema,
                prepared=WorkspaceTool(descriptor, tool.invoke),
            )
        )
    return result

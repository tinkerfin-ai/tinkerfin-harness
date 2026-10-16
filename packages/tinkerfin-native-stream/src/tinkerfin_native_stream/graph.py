"""Prove physical graph origins from locked Native task and execution evidence."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import TypeGuard, cast

from langchain_core.messages import AIMessage, AIMessageChunk

from tinkerfin_contracts import (
    GraphOrigin,
    GraphTaskReference,
    SubagentRequestReference,
    subagent_request_id,
)

from .errors import NativeStreamContractError
from .json import to_json_value
from .stream import (
    NativeExtraStreamPart,
    NativeMessageStreamPart,
    NativeTasksStreamPart,
    NativeTaskStartPayload,
    NativeUpdatesStreamPart,
    NativeValidatedStreamPart,
    NativeValuesStreamPart,
)

_COUNTER = re.compile(r"[1-9][0-9]*\Z", re.ASCII)


def _is_mapping(value: object) -> TypeGuard[Mapping[object, object]]:
    return isinstance(value, Mapping)


def _is_tuple(value: object) -> TypeGuard[tuple[object, ...]]:
    return isinstance(value, tuple)


def _is_array(value: object) -> TypeGuard[tuple[object, ...] | list[object]]:
    return isinstance(value, (tuple, list))


class NativeGraphScopeRegistry:
    """Own proven graph and delegation relationships for one stream request.

    LangGraph 1.2.11 ``PregelLoop.__init__`` appends a standalone positive counter
    to repeated child calls. ``map_debug_tasks`` filters internal metadata, so owned
    functional tasks require registered execution evidence for their complete scopes.
    Neither physical form creates a new logical delegation.
    """

    def __init__(self) -> None:
        """Start with the root scope and retain evidence only for this request."""

        self._origins: dict[tuple[str, ...], GraphOrigin] = {(): GraphOrigin()}
        self._tasks: dict[tuple[tuple[str, ...], str], tuple[str, ...]] = {}
        self._requests: dict[str, SubagentRequestReference] = {}
        self._executed_requests: set[str] = set()
        self._internal_nodes: set[tuple[tuple[str, ...], str]] = set()
        self._internal_tasks: set[tuple[tuple[str, ...], str]] = set()
        self._executions: dict[
            tuple[tuple[str, ...], str], SubagentRequestReference
        ] = {}
        self._task_paths: dict[tuple[tuple[str, ...], int, str], tuple[str, ...]] = {}
        self._internal_results: dict[tuple[tuple[str, ...], str], set[str]] = {}
        self._private_invocations: dict[tuple[tuple[str, ...], str, str], str] = {}
        self._tool_proposals: dict[tuple[tuple[str, ...], str], str] = {}

    def copy(self) -> NativeGraphScopeRegistry:
        """Stage correlation changes without mutating a rejected part's state."""

        result = NativeGraphScopeRegistry()
        result._origins = dict(self._origins)
        result._tasks = dict(self._tasks)
        result._requests = dict(self._requests)
        result._executed_requests = set(self._executed_requests)
        result._internal_nodes = set(self._internal_nodes)
        result._internal_tasks = set(self._internal_tasks)
        result._executions = dict(self._executions)
        result._task_paths = dict(self._task_paths)
        result._internal_results = {
            key: set(values) for key, values in self._internal_results.items()
        }
        result._private_invocations = dict(self._private_invocations)
        result._tool_proposals = dict(self._tool_proposals)
        return result

    def validate_model_output(
        self,
        namespace: tuple[str, ...],
        *,
        message_ids: tuple[str, ...],
        tool_call_ids: tuple[str, ...],
    ) -> None:
        """Reject conflicting proposals before model facts can alter retained links.

        Native checkpoint messages retain the original proposal message identity.
        Replaying that message is valid; a different assistant message in the same
        physical graph cannot reuse its Tool ID for another request.
        """

        for tool_id in tool_call_ids:
            prior = self._tool_proposals.get((namespace, tool_id))
            if prior is not None and prior not in message_ids:
                raise NativeStreamContractError(
                    "new Tool proposals require unique IDs within their graph scope"
                )

    def _record_proposals(self, part: NativeValidatedStreamPart) -> None:
        messages: list[object] = []
        if isinstance(part, NativeMessageStreamPart):
            messages.append(part.data.message)
        elif isinstance(part, NativeValuesStreamPart):
            supplied = part.data.get("messages")
            if _is_array(supplied):
                messages.extend(supplied)
        staged = dict(self._tool_proposals)
        for message in messages:
            if not isinstance(message, AIMessage) or not message.id:
                continue
            calls = (
                message.tool_call_chunks
                if isinstance(message, AIMessageChunk)
                else message.tool_calls
            )
            for call in calls:
                tool_id = call.get("id")
                if not isinstance(tool_id, str) or not tool_id:
                    continue
                key = (part.ns, tool_id)
                prior = staged.get(key)
                if prior is not None and prior != message.id:
                    raise NativeStreamContractError(
                        "new Tool proposals require unique IDs within their graph scope"
                    )
                staged[key] = message.id
        self._tool_proposals = staged

    def callback_origin(self, namespace: tuple[str, ...]) -> GraphOrigin:
        """Register ancestry carried by trusted locked callback checkpoint metadata.

        Callback metadata is produced inside the executing Graph, before its Native
        parts are necessarily pulled. It may prove ordinary intermediate tasks, but
        never establishes a delegate without an already observed parent Tool request.
        """

        for length in range(1, len(namespace) + 1):
            scope = namespace[:length]
            if scope in self._origins:
                continue
            if _COUNTER.fullmatch(scope[-1]):
                self.resolve(scope)
                continue
            node, separator, task_id = scope[-1].partition(":")
            if not separator or not node or not task_id:
                raise ValueError("callback checkpoint namespace has no task identity")
            parent = self.resolve(scope[:-1])
            task = GraphTaskReference(
                graph_namespace=scope[:-1], task_id=task_id, node_name=node
            )
            self._register_scope(
                scope,
                GraphOrigin(parent_task=task, subagent_request=parent.subagent_request),
            )
            self._tasks[(scope[:-1], task_id)] = scope
        return self.resolve(namespace)

    def execution_request(
        self, namespace: tuple[str, ...], graph_task_id: str
    ) -> SubagentRequestReference | None:
        """Return the logical request of an explicitly registered Tool execution."""

        request = self._executions.get((namespace, graph_task_id))
        return None if request is None else self._requests[request.id]

    def delegation_execution(
        self, namespace: tuple[str, ...]
    ) -> SubagentRequestReference | None:
        """Return a request only at its proven delegated execution scope.

        Ordinary nested graphs inherit ownership but can use their own agent names.
        Native task scope and callback scope are both explicit execution records.
        """

        origin = self.resolve(namespace)
        task, request = origin.parent_task, origin.subagent_request
        if task is None or request is None:
            return None
        executions = (
            self.execution_request(namespace[:-1], task.task_id),
            self.execution_request(task.graph_namespace, task.task_id),
        )
        return (
            request
            if any(
                execution is not None and execution.id == request.id
                for execution in executions
            )
            else None
        )

    def adopt_origin(self, namespace: tuple[str, ...], origin: GraphOrigin) -> None:
        """Consume the same validated origin already used by Runtime observers.

        Private durable task parts need not be published to protocol consumers. Their
        source frame still proves the exact opening task and original parent request.
        """

        if not namespace:
            if origin.parent_task is not None or origin.subagent_request is not None:
                raise ValueError("root graph cannot have an opening task or delegate")
            return
        task = origin.parent_task
        if task is None:
            raise ValueError("non-root frame requires its proven opening task")
        base = namespace[:-1] if _COUNTER.fullmatch(namespace[-1]) else namespace
        if not base or base[-1] != f"{task.node_name}:{task.task_id}":
            raise ValueError("frame scope conflicts with its opening task")
        if (
            len(task.graph_namespace) >= len(base)
            or base[: len(task.graph_namespace)] != task.graph_namespace
        ):
            raise ValueError(
                "frame opening task must belong to a proven ancestor scope"
            )
        self.resolve(task.graph_namespace)
        enclosing = self.resolve(base[:-1])
        previous = self._origins.get(base)
        expected = (
            enclosing.subagent_request
            if previous is None
            else previous.subagent_request
        )
        request = origin.subagent_request
        if (None if expected is None else expected.id) != (
            None if request is None else request.id
        ):
            raise ValueError(
                "frame cannot remove or replace its proven delegation owner"
            )
        for scope in (base, namespace):
            recorded = self._origins.get(scope)
            if recorded is not None and recorded.parent_task != task:
                raise ValueError(
                    "frame opening task conflicts with the proven graph scope"
                )
        task_scope = self._tasks.get((task.graph_namespace, task.task_id))
        if task_scope is not None and task_scope != base:
            raise ValueError(
                "frame opening task already belongs to another graph scope"
            )
        if request is not None:
            anchor = (*request.parent_graph_namespace, f"tools:{request.graph_task_id}")
            if base[: len(anchor)] != anchor:
                raise ValueError(
                    "frame delegation does not own its physical graph scope"
                )
            known = self._requests.get(request.id)
            if known is None or (
                known.parent_graph_namespace != request.parent_graph_namespace
                or known.parent_tool_call_id != request.parent_tool_call_id
                or known.graph_task_id != request.graph_task_id
            ):
                raise ValueError("frame delegation has no matching parent request")
            if request.id in self._executed_requests and known != request:
                raise ValueError(
                    "recorded delegation changed after its execution was observed"
                )
            self._requests[request.id] = request
            self._executed_requests.add(request.id)
        self._register_scope(base, origin, allow_request=True)
        self._tasks[(task.graph_namespace, task.task_id)] = base
        if base != namespace:
            self._register_scope(namespace, origin, allow_request=True)

    def resolve(self, namespace: tuple[str, ...]) -> GraphOrigin:
        """Resolve an exact scope or one canonical repeat of a proven task scope."""

        known = self._origins.get(namespace)
        if known is not None:
            return self._current(known)
        if namespace and _COUNTER.fullmatch(namespace[-1]):
            base = self._origins.get(namespace[:-1])
            if base is not None and namespace[:-1] in self._tasks.values():
                resolved = self._current(base)
                self._origins[namespace] = resolved
                return resolved
        raise NativeStreamContractError(
            "subgraph stream arrived before its native task-start correlation: "
            f"namespace={namespace!r}"
        )

    def _current(self, origin: GraphOrigin) -> GraphOrigin:
        request = origin.subagent_request
        if request is None:
            return origin
        current = self._requests[request.id]
        return (
            origin
            if current == request
            else origin.model_copy(update={"subagent_request": current})
        )

    def register_delegation(
        self,
        *,
        parent_namespace: tuple[str, ...],
        graph_task_id: str,
        tool_call_id: str,
        agent_name: str,
        description: str,
        task_namespace: tuple[str, ...] | None = None,
        executed: bool = False,
    ) -> SubagentRequestReference:
        """Register a real parent request before any of its child work is observed."""

        self.resolve(parent_namespace)
        anchor = task_namespace or (*parent_namespace, f"tools:{graph_task_id}")
        request = SubagentRequestReference(
            id=subagent_request_id(anchor),
            parent_graph_namespace=parent_namespace,
            parent_tool_call_id=tool_call_id,
            graph_task_id=graph_task_id,
            agent_name=agent_name,
            description=description,
        )
        previous = self._requests.get(request.id)
        if previous is not None:
            if (
                previous.parent_graph_namespace != parent_namespace
                or previous.parent_tool_call_id != tool_call_id
                or previous.graph_task_id != graph_task_id
            ):
                raise ValueError("delegation task changed its owning Tool request")
            if previous != request:
                if request.id in self._executed_requests and not executed:
                    request = previous
                elif request.id in self._executed_requests:
                    raise ValueError(
                        "delegation request changed after execution started"
                    )
                elif not executed:
                    raise ValueError(
                        "conflicting task start: delegation arguments changed"
                    )
        self._requests[request.id] = request
        if executed:
            self._executed_requests.add(request.id)
        self._executions[(parent_namespace, graph_task_id)] = request
        task = GraphTaskReference(
            graph_namespace=parent_namespace, task_id=graph_task_id, node_name="tools"
        )
        origin = GraphOrigin(parent_task=task, subagent_request=request)
        self._register_scope(anchor, origin, allow_request=True)
        self._tasks.setdefault((parent_namespace, graph_task_id), anchor)
        return request

    def register_retry(
        self, request: SubagentRequestReference, *, node_name: str
    ) -> None:
        """Classify one framework-owned durable attempt node under its parent task."""

        anchor = (*request.parent_graph_namespace, f"tools:{request.graph_task_id}")
        self._internal_nodes.add((anchor, node_name))

    def register_private_invocation(
        self,
        request: SubagentRequestReference,
        *,
        node_name: str,
        arguments: tuple[int | str, ...],
    ) -> None:
        """Register exact functional input before its Native start can be emitted.

        LangGraph 1.2.11 emits functional input as ``(args, kwargs)`` before running
        the task body. This evidence marks only that invocation private; the body
        or a retained started record must still prove its complete physical scope.

        Args:
            request: Original delegation already registered for this stream.
            node_name: Owned functional task name under that delegation.
            arguments: Exact integer and text arguments supplied to that invocation.

        Raises:
            TypeError: Arguments contain values outside the owned task contract.
            ValueError: The task owner is missing or the same input has another owner.
        """

        anchor = (*request.parent_graph_namespace, f"tools:{request.graph_task_id}")
        if (anchor, node_name) not in self._internal_nodes:
            raise ValueError("private invocation requires its registered task owner")
        if any(type(argument) not in (int, str) for argument in arguments):
            raise TypeError("private invocation arguments must be integers or text")
        keyword_arguments: dict[str, object] = {}
        encoded = json.dumps(
            to_json_value((arguments, keyword_arguments)),
            sort_keys=True,
            separators=(",", ":"),
        )
        key = (request.parent_graph_namespace, node_name, encoded)
        prior = self._private_invocations.get(key)
        if prior is not None and prior != request.id:
            raise ValueError("private invocation input has conflicting owners")
        self._private_invocations[key] = request.id

    def _accept_private_start(
        self, namespace: tuple[str, ...], data: Mapping[object, object]
    ) -> bool:
        task_id, name = data.get("id"), data.get("name")
        if (
            not isinstance(task_id, str)
            or not isinstance(name, str)
            or "input" not in data
        ):
            return False
        if not any(key[:2] == (namespace, name) for key in self._private_invocations):
            return False
        value = data["input"]
        if (
            not _is_tuple(value)
            or len(value) != 2
            or not _is_tuple(value[0])
            or value[1] != {}
            or any(type(argument) not in (int, str) for argument in value[0])
        ):
            return False
        encoded = json.dumps(
            to_json_value(data["input"]), sort_keys=True, separators=(",", ":")
        )
        if (namespace, name, encoded) not in self._private_invocations:
            return False
        self._internal_tasks.add((namespace, task_id))
        return True

    def register_execution(
        self,
        namespace: tuple[str, ...],
        *,
        parent_task: GraphTaskReference,
        request: SubagentRequestReference,
    ) -> None:
        """Bind actual attempt execution evidence to its original logical request."""

        if self._requests.get(request.id) != request:
            raise ValueError("child execution requires its registered parent request")
        if (
            not namespace
            or namespace[-1] != f"{parent_task.node_name}:{parent_task.task_id}"
        ):
            raise ValueError("child execution namespace does not match its actual task")
        self._register_scope(
            namespace, GraphOrigin(parent_task=parent_task, subagent_request=request)
        )
        self._tasks[(parent_task.graph_namespace, parent_task.task_id)] = namespace
        self._executions[(namespace[:-1], parent_task.task_id)] = request
        if (namespace[:-1], parent_task.node_name) in self._internal_nodes:
            self._internal_tasks.add((parent_task.graph_namespace, parent_task.task_id))

    def accept(self, part: NativeValidatedStreamPart) -> GraphOrigin:
        """Validate one part and register task declarations before its descendants."""

        source = self.resolve(part.ns)
        if source.subagent_request is not None:
            self._executed_requests.add(source.subagent_request.id)
        if isinstance(part, NativeExtraStreamPart) and part.type == "debug":
            debug_data = part.data
            if _is_mapping(debug_data) and debug_data.get("type") == "task":
                debug_payload = debug_data.get("payload")
                if _is_mapping(debug_payload):
                    self._accept_private_start(part.ns, debug_payload)
        if isinstance(part, NativeMessageStreamPart):
            request = self.delegation_execution(part.ns)
            agent_name = part.data.metadata.lc_agent_name
            if request is not None and agent_name and request.agent_name != agent_name:
                raise NativeStreamContractError(
                    "task subagent_type does not match streamed lc_agent_name"
                )
        self._record_proposals(part)
        if not isinstance(part, NativeTasksStreamPart) or not isinstance(
            part.data, NativeTaskStartPayload
        ):
            return source
        payload = part.data
        if self._accept_private_start(
            part.ns, {"id": payload.id, "name": payload.name, "input": payload.input}
        ):
            return source
        task = GraphTaskReference(
            graph_namespace=part.ns, task_id=payload.id, node_name=payload.name
        )
        base = self._tasks.get(
            (part.ns, payload.id), (*part.ns, f"{payload.name}:{payload.id}")
        )
        known = self._origins.get(base)
        if (
            known is not None
            and known.parent_task is not None
            and (known.parent_task.node_name != payload.name)
        ):
            raise ValueError("conflicting task start: task name changed")
        metadata = None if payload.metadata is None else payload.metadata.model_extra
        raw = None if metadata is None else metadata.get("langgraph_checkpoint_ns")
        if raw is not None:
            if not isinstance(raw, str) or not raw:
                raise ValueError("task checkpoint namespace must be canonical text")
            base = tuple(raw.split("|"))
            if any(not value or value != value.strip() for value in base):
                raise ValueError("task checkpoint namespace has invalid components")
            if base[-1] != f"{payload.name}:{payload.id}":
                raise ValueError(
                    "task checkpoint namespace conflicts with its identity"
                )
        if base[:-1] != part.ns and "__pregel_push" not in payload.triggers:
            raise ValueError("task metadata changed its declared parent graph scope")
        path = None if metadata is None else metadata.get("langgraph_path")
        step = None if metadata is None else metadata.get("langgraph_step")
        if base[:-1] != part.ns and (part.ns, payload.id) not in self._tasks:
            parent_path = (
                path[1]
                if _is_array(path)
                and len(path) == 4
                and path[0] == "__pregel_push"
                and path[-1] is True
                else None
            )
            if not _is_array(parent_path) or type(step) is not int:
                raise ValueError("nested functional task has no proven caller path")
            caller = self._task_paths.get(
                (part.ns, step, json.dumps(parent_path, separators=(",", ":")))
            )
            if caller != base[:-1]:
                raise ValueError(
                    "functional task metadata conflicts with its caller scope"
                )
        enclosing = self.resolve(base[:-1])
        origin = GraphOrigin(
            parent_task=task, subagent_request=enclosing.subagent_request
        )
        self._register_scope(base, origin)
        key = (part.ns, payload.id)
        previous = self._tasks.get(key)
        if previous is not None and previous != base:
            raise ValueError("Native task changed its complete execution namespace")
        self._tasks[key] = base
        if _is_array(path) and type(step) is int:
            path_key = (part.ns, step, json.dumps(path, separators=(",", ":")))
            prior_path = self._task_paths.get(path_key)
            if prior_path is not None and prior_path != base:
                raise ValueError("Native task path has conflicting execution identity")
            self._task_paths[path_key] = base
        if (base[:-1], payload.name) in self._internal_nodes:
            self._internal_tasks.add(key)
        tool_input = payload.input
        if payload.name == "tools" and isinstance(tool_input, list):
            calls = [
                value
                for value in cast(list[object], tool_input)
                if _is_mapping(value) and value.get("name") == "task"
            ]
            if len(calls) > 1:
                raise NativeStreamContractError(
                    "each delegated request requires its own Native task identity"
                )
            for call in calls:
                args = call.get("args")
                tool_id = call.get("id")
                if not _is_mapping(args) or not isinstance(tool_id, str):
                    raise TypeError(
                        "delegation requires complete Tool identity and arguments"
                    )
                name, description = args.get("subagent_type"), args.get("description")
                if not isinstance(name, str) or not isinstance(description, str):
                    raise TypeError(
                        "delegation requires its agent type and description"
                    )
                self.register_delegation(
                    parent_namespace=part.ns,
                    graph_task_id=payload.id,
                    tool_call_id=tool_id,
                    agent_name=name,
                    description=description,
                    task_namespace=base,
                )
        return source

    def is_internal(self, part: NativeValidatedStreamPart) -> bool:
        """Return whether a task part carries a registered private attempt record."""

        return (
            isinstance(part, NativeTasksStreamPart)
            and (part.ns, part.data.id) in self._internal_tasks
        )

    def record_internal_result(
        self, namespace: tuple[str, ...], task_id: str, reference: Mapping[str, str]
    ) -> None:
        """Register an exact private RETURN before it enters an updates stream."""

        key = (namespace, task_id)
        if key not in self._internal_tasks:
            raise ValueError("private result requires a proven internal task")
        if set(reference) != {"attempt_key", "record_digest"} or any(
            not isinstance(value, str) or not value for value in reference.values()
        ):
            raise ValueError("private result reference has an invalid shape")
        scope = self._tasks[key]
        task = self._origins[scope].parent_task
        assert task is not None
        self._internal_results.setdefault((namespace, task.node_name), set()).add(
            json.dumps(dict(reference), sort_keys=True, separators=(",", ":"))
        )

    def public_part(
        self, part: NativeValidatedStreamPart
    ) -> NativeValidatedStreamPart | None:
        """Remove only registered execution records from Native transport surfaces.

        Public business values are not filtered by key names. Updates require an
        exact owned RETURN value; checkpoint/debug records require the full task ID
        in the same graph scope. Child model, Tool, and state parts are untouched.
        """

        if self.is_internal(part):
            return None
        if not self._internal_tasks:
            return part
        if isinstance(part, NativeUpdatesStreamPart) and isinstance(part.data, Mapping):
            public: dict[str, object] = dict(part.data)
            for name, result in tuple(public.items()):
                if not isinstance(name, str):
                    continue
                registered = self._internal_results.get((part.ns, name))
                if not registered:
                    continue
                value = result
                if _is_mapping(value) and set(value) == {"__return__"}:
                    value = value["__return__"]
                if (
                    _is_mapping(value)
                    and set(value) == {"attempt_key", "record_digest"}
                    and json.dumps(dict(value), sort_keys=True, separators=(",", ":"))
                    in registered
                ):
                    public.pop(name)
            if not public:
                return None
            return (
                part
                if public == part.data
                else part.model_copy(update={"data": public})
            )
        if not isinstance(part, NativeExtraStreamPart) or part.type not in {
            "debug",
            "checkpoints",
        }:
            return part
        if not _is_mapping(part.data):
            return part
        data = dict(part.data)
        if part.type == "debug":
            payload = data.get("payload")
            if not _is_mapping(payload):
                return part
            if data.get("type") in {"task", "task_result"}:
                task_id = payload.get("id")
                if (
                    isinstance(task_id, str)
                    and (part.ns, task_id) in self._internal_tasks
                ):
                    return None
                return part
            if data.get("type") != "checkpoint":
                return part
            data["payload"] = self._public_checkpoint(part.ns, payload)
        else:
            data = self._public_checkpoint(part.ns, data)
        return part if data == part.data else part.model_copy(update={"data": data})

    def _public_checkpoint(
        self, namespace: tuple[str, ...], value: Mapping[object, object]
    ) -> dict[object, object]:
        public = dict(value)
        tasks = public.get("tasks")
        if _is_array(tasks):
            public["tasks"] = [
                task
                for task in tasks
                if not (
                    _is_mapping(task)
                    and isinstance(task.get("id"), str)
                    and (namespace, task["id"]) in self._internal_tasks
                )
            ]
        return public

    def declarations(
        self, part: NativeValidatedStreamPart
    ) -> tuple[SubagentRequestReference, ...]:
        """Return only delegations declared by this exact Native task start."""

        if (
            not isinstance(part, NativeTasksStreamPart)
            or not isinstance(part.data, NativeTaskStartPayload)
            or part.data.name != "tools"
        ):
            return ()
        return tuple(
            request
            for request in self._requests.values()
            if request.parent_graph_namespace == part.ns
            and request.graph_task_id == part.data.id
        )

    def adopt_declarations(
        self,
        part: NativeValidatedStreamPart,
        requests: tuple[SubagentRequestReference, ...],
    ) -> None:
        """Validate recorded declarations against their exact parent task input."""

        declared = {request.id: request for request in requests}
        expected = {request.id: request for request in self.declarations(part)}
        if len(declared) != len(requests) or declared.keys() != expected.keys():
            raise ValueError("recorded delegations disagree with the Native task")
        for request in requests:
            known = expected[request.id]
            if request.id in self._executed_requests and known != request:
                raise ValueError(
                    "recorded delegation changed after its execution was observed"
                )
            if (
                request.parent_graph_namespace != known.parent_graph_namespace
                or request.parent_tool_call_id != known.parent_tool_call_id
                or request.graph_task_id != known.graph_task_id
            ):
                raise ValueError("recorded delegation changed its parent Tool identity")
        for request in requests:
            if request != expected[request.id]:
                self._executed_requests.add(request.id)
            self._requests[request.id] = request

    def _register_scope(
        self,
        namespace: tuple[str, ...],
        origin: GraphOrigin,
        *,
        allow_request: bool = False,
    ) -> None:
        previous = self._origins.get(namespace)
        if previous is not None and previous != origin:
            same_task = previous.parent_task == origin.parent_task
            inherited = previous.subagent_request
            requested = origin.subagent_request
            if (
                same_task
                and inherited is not None
                and (
                    requested is None
                    or (
                        previous.parent_task is not None
                        and inherited.graph_task_id == previous.parent_task.task_id
                    )
                )
                and not allow_request
            ):
                return
            if not same_task or not (
                allow_request
                or inherited is not None
                and requested is not None
                and inherited.id == requested.id
            ):
                raise ValueError("physical graph scope has conflicting parent evidence")
        self._origins[namespace] = origin


__all__ = ["NativeGraphScopeRegistry"]

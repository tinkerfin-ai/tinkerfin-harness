"""Concrete remote operations independent of Agent instructions or protocols."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from tinkerfin_contracts import RunIdentity, SkillSource, ToolDescriptor, ToolResult
from tinkerfin_contracts.workspace_tools import OperationPurpose

Identifier = Annotated[str, Field(min_length=1, max_length=1024)]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class Boundary(BaseModel):
    """Reject undeclared and non-finite transport input."""

    model_config = ConfigDict(
        extra="forbid", frozen=True, strict=True, allow_inf_nan=False
    )


class WorkspaceRequest(Boundary):
    """Give the host a verified endpoint and an opaque authorization to resolve.

    The host interprets authorization and owns account/project/directory policy.
    Workspace scope survives approval runs; session identifies only this borrow.
    """

    identity: RunIdentity
    target: Identifier
    authorization: str = Field(min_length=1, max_length=16_384, repr=False)
    workspace_scope_id: Digest
    session_id: Digest


class WorkspaceDescription(Boundary):
    """Describe prepared capabilities without transferring executable objects."""

    filesystem_instructions: str | None = None
    tool_descriptions: dict[str, str] = Field(default_factory=dict)
    tools: tuple[ToolDescriptor, ...] = ()
    skill_sources: tuple[SkillSource, ...] = ()
    artifacts_root: str


class ResolvePath(Boundary):
    """Resolve a path under the workspace's read or write access policy."""

    kind: Literal["file.resolve"] = "file.resolve"
    path: str | None
    operation: Literal["read", "write"]


class ListFiles(Boundary):
    """Request directory entries from the prepared workspace backend."""

    kind: Literal["file.ls"] = "file.ls"
    path: str


class ReadFile(Boundary):
    """Read file data, applying a zero-based offset and line-count limit to text."""

    kind: Literal["file.read"] = "file.read"
    path: str
    offset: int = Field(default=0, ge=0)
    limit: int = 2000


class WriteFile(Boundary):
    """Create or overwrite a file with text under the backend's write policy."""

    kind: Literal["file.write"] = "file.write"
    path: str
    content: str


class EditFile(Boundary):
    """Replace matching text, requiring a unique match unless replace_all is set."""

    kind: Literal["file.edit"] = "file.edit"
    path: str
    old: str
    new: str
    replace_all: bool = False


class DeleteFile(Boundary):
    """Request deletion subject to the prepared workspace's write policy."""

    kind: Literal["file.delete"] = "file.delete"
    path: str


class GlobFiles(Boundary):
    """Find matching paths within the backend's allowed search directory."""

    kind: Literal["file.glob"] = "file.glob"
    pattern: str
    path: str | None = None


class GrepFiles(Boundary):
    """Search file contents with optional path, filename, and match limits."""

    kind: Literal["file.grep"] = "file.grep"
    pattern: str
    path: str | None = None
    glob: str | None = None
    max_count: int | None = Field(default=None, ge=0)


class BinaryFile(Boundary):
    """Bind immutable bytes to one path before upload admission."""

    path: str
    size: int = Field(ge=0)
    digest: Digest


class DownloadedFile(Boundary):
    """Describe a downloaded file whose bytes travel in separate chunks."""

    path: str
    size: int = Field(ge=0)
    digest: Digest | None
    error: str | None


class UploadFiles(Boundary):
    """Declare upload destinations and digests before transferring file bytes."""

    kind: Literal["file.upload"] = "file.upload"
    files: tuple[BinaryFile, ...] = Field(min_length=1, max_length=64)


class DownloadFiles(Boundary):
    """Request file bytes for transfer in independently acknowledged chunks."""

    kind: Literal["file.download"] = "file.download"
    paths: tuple[str, ...] = Field(min_length=1, max_length=64)


class ExecuteCommand(Boundary):
    """Run a workspace command with an optional timeout measured in seconds."""

    kind: Literal["command.execute"] = "command.execute"
    command: str
    timeout: int | None = Field(default=None, gt=0)


class CallTool(Boundary):
    """Invoke a prepared tool only if its declaration matches the supplied digest."""

    kind: Literal["tool.call"] = "tool.call"
    name: Identifier
    arguments: dict[str, JsonValue]
    descriptor_digest: Digest


Operation = Annotated[
    ResolvePath
    | ListFiles
    | ReadFile
    | WriteFile
    | EditFile
    | DeleteFile
    | GlobFiles
    | GrepFiles
    | UploadFiles
    | DownloadFiles
    | ExecuteCommand
    | CallTool,
    Field(discriminator="kind"),
]


class OperationRequest(Boundary):
    """Bind one stable invocation step independently from its delivery session."""

    operation_id: Digest
    origin: RunIdentity
    workspace_scope_id: Digest
    invocation_id: Identifier
    steps: tuple[str, ...]
    graph_namespace: tuple[str, ...]
    purpose: OperationPurpose
    tool_call_id: str | None = None
    observation_session: Digest | None = None
    operation: Operation


class OpenWorkspace(Boundary):
    """Ask the worker to authorize and prepare one workspace session."""

    kind: Literal["workspace.open"] = "workspace.open"
    request: WorkspaceRequest


class CloseWorkspace(Boundary):
    """Release one borrowed workspace session without deleting its files."""

    kind: Literal["workspace.close"] = "workspace.close"
    session_id: Digest


class Dispatch(Boundary):
    """Deliver a stable operation to an already prepared workspace session."""

    kind: Literal["operation.invoke"] = "operation.invoke"
    session_id: Digest
    request: OperationRequest


class CancelOperation(Boundary):
    """Request cancellation without undoing effects already performed."""

    kind: Literal["operation.cancel"] = "operation.cancel"
    operation_id: Digest


Command = Annotated[
    OpenWorkspace | CloseWorkspace | Dispatch | CancelOperation,
    Field(discriminator="kind"),
]


class CommandEnvelope(Boundary):
    """Bind a command to its durable delivery and deduplication identifier."""

    command_id: Digest
    command: Command


class BinaryChunk(Boundary):
    """One immutable, contiguous piece of an operation-owned binary transfer."""

    operation_id: Digest
    channel: Literal["file", "result"] = "file"
    file_index: int = Field(ge=0, le=63)
    offset: int = Field(ge=0)
    data: str
    digest: Digest


class ResultTransfer(Boundary):
    """Identify a bounded JSON result transferred separately from its terminal."""

    size: int = Field(gt=0)
    digest: Digest


class OperationEvent(Boundary):
    """Persist progress before acknowledgment and expose exactly one terminal."""

    operation_id: Digest
    sequence: int = Field(ge=1)
    kind: Literal[
        "started", "output", "binary", "completed", "failed", "cancelled", "uncertain"
    ]
    output: JsonValue | None = None
    chunk: BinaryChunk | None = None
    result: ToolResult | None = None
    result_transfer: ResultTransfer | None = None
    message: str | None = Field(default=None, max_length=2048)

    @model_validator(mode="after")
    def payload_matches_kind(self) -> OperationEvent:
        """Separate streamed output, binary transfer, and terminal results."""
        if (self.kind == "binary") != (self.chunk is not None):
            raise ValueError("binary event requires exactly one chunk")
        if self.chunk is not None and self.chunk.operation_id != self.operation_id:
            raise ValueError("binary chunk belongs to another operation")
        if (
            self.result is not None or self.result_transfer is not None
        ) and self.kind != "completed":
            raise ValueError("only completed operations carry a result")
        if self.kind == "completed" and (self.result is None) == (
            self.result_transfer is None
        ):
            raise ValueError("completed operation requires a result")
        if self.output is not None and self.kind != "output":
            raise ValueError("only output events carry progress")
        return self


class WorkspaceEvent(Boundary):
    """Settle one workspace control request without inventing a tool execution."""

    command_id: Digest
    session_id: Digest
    description: WorkspaceDescription | None = None
    closed: bool = False
    error: str | None = Field(default=None, max_length=2048)

    @model_validator(mode="after")
    def exactly_one_outcome(self) -> WorkspaceEvent:
        """Require one accepted, closed, or rejected workspace outcome."""
        if (
            sum((self.description is not None, self.closed, self.error is not None))
            != 1
        ):
            raise ValueError("workspace reply must carry exactly one outcome")
        return self


class Receipt(Boundary):
    """Acknowledge the immutable payload identified by a key and SHA-256 digest."""

    key: Identifier
    digest: Digest


class WorkerFrame(Boundary):
    """Carry bounded worker results and delivery receipts for one endpoint."""

    namespace: Identifier
    target: Identifier
    commands_received: tuple[Receipt, ...] = Field(default=(), max_length=8)
    chunks_received: tuple[Receipt, ...] = Field(default=(), max_length=8)
    events: tuple[OperationEvent, ...] = Field(default=(), max_length=8)
    workspaces: tuple[WorkspaceEvent, ...] = Field(default=(), max_length=8)


class CoordinatorFrame(Boundary):
    """Deliver work and receipt confirmations, or revoke the endpoint's access."""

    namespace: Identifier
    target: Identifier
    revoked: bool = False
    commands: tuple[CommandEnvelope, ...] = Field(default=(), max_length=8)
    chunks: tuple[BinaryChunk, ...] = Field(default=(), max_length=8)
    events_received: tuple[Receipt, ...] = Field(default=(), max_length=8)
    workspaces_received: tuple[Receipt, ...] = Field(default=(), max_length=8)
    commands_confirmed: tuple[Receipt, ...] = Field(default=(), max_length=8)
    chunks_confirmed: tuple[Receipt, ...] = Field(default=(), max_length=8)

    @model_validator(mode="after")
    def revoked_has_no_work(self) -> CoordinatorFrame:
        """A revoked endpoint cannot send queued operations before cancellation."""
        if self.revoked and (self.commands or self.chunks):
            raise ValueError("a revoked endpoint cannot deliver new work")
        return self


TERMINAL_EVENTS = frozenset({"completed", "failed", "cancelled", "uncertain"})

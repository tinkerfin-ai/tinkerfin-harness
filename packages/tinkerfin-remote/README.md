# tinkerfin-remote

Keep the Agent in the cloud and execute concrete tools in an authenticated remote workspace. The package provides durable delivery, bounded file transfers, cancellation and replay without repeating accepted side effects.

## Installation

```bash
pip install tinkerfin-remote
```

Install an asynchronous SQLAlchemy driver for each host's database. To execute native macOS tools, also install `tinkerfin-sandbox` and its sandbox runtime.

## Cloud host

Create one `RemoteTools` context for the application lifetime. Bind both namespace and target from authenticated host state, then pass the workspace declaration to the normal cloud Runtime:

```python
from tinkerfin_remote import RemoteTools

async with RemoteTools(engine=engine) as remote:
    endpoint = remote.endpoint(
        namespace=authenticated_namespace, target=authenticated_target
    )
    workspace = endpoint.workspace(authorization=authorized_workspace_token)
    runtime = configured.build(model=model, backend=workspace)
    # The authenticated socket route owns this independent delivery attachment.
    await endpoint.serve(send=socket.send_text, receive=socket.receive_text)
```

The existing cloud Gateway executes `runtime`. The socket route runs independently; opening a workspace does not create another Agent. The host must authenticate the socket, fence replaced connections and interpret its authorization token on the remote host. `await endpoint.revoke()` permanently revokes that target and saves cancellation intent, including while offline. A new pairing uses a new target.

Workspace preparation waits up to 30 seconds for authorized capabilities. Preparation failures expose `workspace_failure` for the normal Runtime status mapping. New work is not silently queued indefinitely while a device is unavailable.

## Remote host

The application resolves authorized directories and owns workspace managers. The worker owns prepared borrows and accepted operations:

```python
from contextlib import asynccontextmanager
from langchain_core.tools import tool
from tinkerfin_sandbox import LocalSession, LocalWorkspace
from tinkerfin_remote import ToolContextArg, ToolExecutor, ToolWorker


@tool
async def working_directory(runtime: ToolContextArg[LocalSession]) -> str:
    """Return the authorized workspace's working directory."""
    return str(runtime.workspace.directory)


@asynccontextmanager
async def open_workspace(request):
    binding = await authorize(request.authorization, request.identity)
    async with LocalWorkspace(binding.directory, access=binding.access) as local:
        yield ToolExecutor(workspace=local, tools=[working_directory])


async with ToolWorker(
    engine=local_engine,
    namespace=authenticated_namespace,
    target=authenticated_target,
    open_workspace=open_workspace,
) as worker:
    await worker.serve(send=socket.send, receive=socket.recv)
```

Reconnect by calling `serve` again on the same worker. Detaching delivery does not stop accepted work. `wait_idle()` joins accepted operations; closing the worker cancels and joins owned execution before releasing workspace managers. Both engines remain owned by the application.

For a graceful restart or maintenance, pause new work while keeping delivery
attached so accepted uploads, cancellation and results can finish:

```python
async with worker.maintenance():
    await hand_over_to_replacement()
```

Entering the context waits for accepted execution and workspace preparation.
New work receives a durable rejection; an existing request keeps its original
outcome when replayed. Leaving or cancelling the context resumes admission unless
the worker has closed. One maintenance owner is allowed at a time. The host owns
the replacement or maintenance action; prepared workspaces remain borrowed until
their normal owner closes them.

Configure driver and database deadlines on both borrowed engines. Cancellation and shutdown wait for already accepted SQL to settle, including cursor cleanup and commit acknowledgment. A driver receive timeout bounds an inactive network read; it is not a total statement deadline. Close `RemoteTools` and `ToolWorker` before disposing their engines; their close methods reject new ledger calls and join accepted ones.

Tools receive a real `ToolContextArg` with workspace access and `await runtime.emit_output(value)`. Return JSON content or ordinary LangChain tool messages. Graph state, model access, Store injection and Graph commands do not cross this boundary. Declare path arguments using `tinkerfin_contracts.FilePath` so the cloud resolves targets before approval and the worker checks them again before execution.

Direct `RemoteSession` file calls also accept relative and `~/` POSIX paths, resolved by the selected workspace under its current permissions. Absolute targets are rechecked locally: trailing separators, repeated interior separators and `.` components are accepted, while `..` components and symlink redirection are rejected. Upload and download responses preserve each requested path and its input position. Write, edit, delete and directory results retain the backend's absolute paths.

```python
from typing import Annotated
from tinkerfin_contracts import FilePath


@tool
async def read_note(
    path: Annotated[str, FilePath("read")], runtime: ToolContextArg[LocalSession]
) -> str:
    """Read an approved text file from the authorized workspace."""
    result = await runtime.workspace.aread(path)
    if result.error is not None or result.file_data is None:
        raise ValueError("The file could not be read")
    return result.file_data["content"]
```

Register `read_note` in the executor's `tools`. Prepared tools and skills become available to the main Agent and ordinary cloud subagents that inherit this workspace. The selected workspace does not trigger an additional Agent.

Stable invocation identities prevent automatic repetition after disconnect or restart. An uncertain result means an operation may have taken effect; it is never automatically rerun. Cancellation stops owned work but does not undo effects or guarantee that unrelated processes spawned outside its ownership have stopped.

An operation failure includes its original `operation_id` in `RemoteError.context`.
Use the authenticated endpoint to read its current receipt without dispatching work:

```python
operation_id = failure.context.get("operation_id")
if isinstance(operation_id, str):
    status = await endpoint.get_operation(operation_id)
```

The query returns `OperationStatus` with lifecycle state, cancellation intent and
any retained inline result. It does not download transferred results. `None` means
no record is retained by this endpoint; neither absence nor an expired result
proves that an operation did not execute. Keep the original identifier when
checking again, and never replace an uncertain operation automatically.

Closed, acknowledged sessions automatically release bulk transfer and progress payloads. Small results, including resolved paths, remain replayable across approval and Runtime reconstruction (16 KiB by default). Replaying an expired large result reports `UNCERTAIN` and never executes its operation again. The operation limit counts active work; retained storage also bounds compact identity and receipt evidence. Reaching that final evidence capacity remains an explicit error.

If cancellation occurs while delivery is detached, the cloud saves stop intent and returns cancellation with an unconfirmed-termination note. The worker receives the intent when delivery reconnects; the cloud does not report the offline process as already stopped.

See the [TinkerFin documentation](https://tinkerfin-ai.github.io/tinkerfin-harness/en/) and [Apache-2.0 license](https://github.com/tinkerfin-ai/tinkerfin-harness/blob/main/LICENSE).

# Rooted files and commands

[Sandbox lifecycle](lifecycle.md) · [中文](../../cn/sandbox/rooted-filesystem.md)

An isolated workspace presents its project files at file-tool `/`. The owner key
selects a physical Sandbox; `workspace_key` selects the project within it:

```python
project = manager.workspace("users/7", workspace_key="project-a")
```

## Why use a rooted workspace

- File tools reject paths and links that escape the project's files;
- Shell commands, transfers, and output capture share the same project isolation;
- project files, HOME, caches, and dependencies persist across runs;
- the agent uses virtual paths without knowing the physical storage layout.

Each run has private processes, `/tmp`, `/proc`, `/dev`, and networking. Shared base
tools are read-only. Processes and network activity stop when the run or `open()`
context ends, including cancellation and errors. Concurrent runs in the same project
share files; writes are not transactional.

For raw command Sandboxes opened with `manager.get()`, `workspace_root="/workspace"`
maps file-tool `/` to that directory, and `reset()` clears its children. This option
only limits file tools; raw Shell commands can access other Sandbox paths. It does
not configure isolated workspaces, whose virtual file root is always `/`.

## Common asynchronous file operations

```python
async with project.open() as backend:
    await backend.awrite("/notes.txt", "hello")
    result = await backend.aread("/notes.txt")
    await backend.aedit("/notes.txt", "hello", "hello world")
    entries = await backend.als("/")
    matches = await backend.aglob("**/*.txt", "/")
    hits = await backend.agrep("hello", "/", glob="*.txt")
    await backend.adelete("/notes.txt")
```

### Method parameters

| Method | Parameters | Use |
| --- | --- | --- |
| `aread()` | `file_path`, `offset=0`, `limit=2000` | Read text by line range |
| `awrite()` | `file_path`, `content` | Write complete text |
| `aedit()` | path, old text, new text, `replace_all=False` | Exact replacement |
| `adelete()` | `file_path` | Delete a path, never the virtual root |
| `als()` | `path` | List a directory |
| `aglob()` | `pattern`, `path=None` | Find paths |
| `agrep()` | pattern, optional path/glob/count | Search text |

Results may contain both normal data and an error description. In search and batch operations, inspect the error field as well as the returned entries.

## Inspect existing files

Use the project declaration directly when viewing files must not start an execution
environment. The manager must remain open throughout these calls:

```python
project = manager.workspace("users/7", workspace_key="project-a")
page = await project.list_directory("/", limit=100)
while True:
    for entry in page.entries:
        print(entry.path, entry.kind, entry.size_bytes)
    if page.next_cursor is None:
        break
    page = await project.list_directory("/", limit=100, cursor=page.next_cursor)

info = await project.get_file_info("/notes.txt")
preview = await project.read_text("/notes.txt", max_bytes=100 * 1024, max_lines=200)
print(preview.text, preview.truncated)
```

These reads do not create, recreate, initialize, or resume resources. The host chooses
authorized owner and project identities; the framework confines traversal, enforces
read limits, and closes query resources. Handle an uninitialized workspace as an
absence of files. Paused or unavailable resources remain explicit errors. Paths are
relative to virtual `/`; symbolic links are never followed. See the
[API reference](api-reference.md) for result fields, limits, and errors.

## Watch file changes

For an existing running project, enter the subscription before reading its initial
file state. Watching does not open a command run or keep the Sandbox from pausing:

```python
async with project.watch() as changes:
    print(await project.list_directory("/"))
    async for change in changes:
        print(await project.list_directory("/"))
```

The iterator yields `WorkspaceChange.FILES_CHANGED` from `tinkerfin_sandbox` or
`ResyncRequired` from `tinkerfin_notifications`. Both concern the whole project file
root; use the file APIs to read current state. Hints can be combined and do not contain
file content, individual operations, or a replay history. Subscribe before reading an
initial snapshot, and repeat authoritative reads after a resync.

File tools, uploads, and ordinary Shell or Python writes, truncation, renames, and
deletions are observed. HOME, caches, and dependency directories are excluded, as are
memory-mapped writes and files hidden by new mounts. Directory changes can require
resynchronization while observation is reestablished.

Watching never creates or resumes a Sandbox or project. Pause, deletion, replacement,
or source disconnection ends the selected watch with a disconnected resync. Check
availability before subscribing again. Exiting or cancelling the context releases its
subscription without affecting project files or other listeners.

## Run a command

```python
async with project.open() as backend:
    result = await backend.aexecute(
        "python -m pytest",
        timeout=300,
    )
```

`timeout=None` uses the backend default. Commands start in the project's files
directory. Use relative paths in commands or `backend.to_shell_path(file_path)` to
convert a virtual file path. A command timeout or cancellation ends the current run;
already written project files are retained.

Public HTTP on port 80 and HTTPS on port 443 use the managed proxy. This supports
`pip`, `npm`, HTTPS Git, and browser requests without application-managed proxy setup.
Private destinations, SSH Git, and other destination ports are unavailable. A server
started in one run does not remain running for later runs.

For large output, pass `OpenSandboxConfig(enable_capture_offload=True)` to the client
when creating the manager, then capture command output:

```python
async with project.open() as backend:
    result = await backend.aexecute_with_offload(
        "python -m pytest -vv",
        "/captures/tests.txt",
        max_inline_bytes=32_000,
        max_capture_bytes=5_000_000,
        timeout=300,
    )
```

| Parameter | Purpose |
| --- | --- |
| `capture_path` | Virtual destination for full output |
| `max_inline_bytes` | Maximum output bytes returned inline |
| `max_capture_bytes` | Optional complete capture limit |
| `timeout` | Command timeout in seconds |

## Upload and download

```python
async with project.open() as backend:
    uploads = await backend.aupload_files([("/input/data.csv", csv_bytes)])
    downloads = await backend.adownload_files(["/output/report.json"])
```

Responses preserve input order. A confirmed invalid path affects only that item. Transport failures and uncertain results propagate instead of retrying a write that may already have happened.

In isolated projects, `adownload_files()` allows up to 64 MiB per file; use
`aread_bytes(..., max_bytes=...)` to choose another explicit limit. Command responses
are bounded to 32 MiB. Exceeding that bound ends the Run; use output offload for large output.

Isolated project transfers use the same run boundary as commands. Rooted transfers
on raw command Sandboxes require Python 3, Linux procfs, and shared process visibility
between command and filesystem services.

## Connect an AgentRuntime

```python
from deepagents import FilesystemPermission

from tinkerfin import TinkerFin

permissions = [
    FilesystemPermission(
        operations=["write"],
        paths=["/policies/private/**"],
        mode="deny",
    )
]

runtime = (
    TinkerFin(checkpointer=checkpointer)
    .with_namespace("projects/project-a")
    .build(
        model=model,
        backend=project,
        permissions=permissions,
    )
)
```

The Runtime prepares isolated project access and its filesystem middleware together
when the run starts. Permission rules that interrupt instead of deny require a checkpointer.
Use `build_rooted_filesystem_middleware()` only in a caller-managed Deep Agents Graph.

Next: [Persistent state and extensions](persistence-and-extensions.md).

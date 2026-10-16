"""Model-visible file and shell path contract for rooted OpenSandbox backends."""

from collections.abc import Sequence

from deepagents.backends.protocol import BackendProtocol
from deepagents.middleware.filesystem import FilesystemMiddleware, FilesystemPermission

ROOTED_FILESYSTEM_SYSTEM_PROMPT = """## Workspace path contract

File tools and shell commands use different path syntax for the same workspace:

- File tools (`ls`, `read_file`, `write_file`, `edit_file`, `delete`, `glob`, and
  `grep`) require virtual absolute paths rooted at `/`, such as
  `/scripts/helloworld.py`.
- `execute` starts in the workspace root. Refer to workspace files with relative
  shell paths, such as `python3 scripts/helloworld.py`.
- Never pass a file-tool virtual path directly to `execute`. In the shell,
  `/scripts/helloworld.py` is a real container path, not the workspace file.
- Do not guess physical workspace paths such as `/home/user`, `/root`, or
  `/workspace`. Keep physical paths out of user-facing responses and report the
  virtual file-tool path instead.
- Shell absolute paths retain their normal real-container meaning for system
  resources outside the workspace.
"""

ROOTED_EXECUTE_TOOL_DESCRIPTION = """Executes a shell command in an isolated sandbox from the workspace root and returns combined stdout/stderr with the exit code (truncated if very large).

Usage:
- Use workspace-relative shell paths for files created by file tools, for example `python3 scripts/helloworld.py` for the file-tool path `/scripts/helloworld.py`.
- A leading slash has normal shell meaning and addresses real container paths; it is not mapped through the file-tool virtual root.
- Do not guess `/home/user`, `/root`, `/workspace`, or another physical workspace path.
- Quote paths containing spaces (e.g. `python3 "scripts/path with spaces/app.py"`).
- Chain commands with `;` or `&&` (use `&&` when a command depends on the previous); do not use newlines except inside quoted strings.
- Use the optional timeout to override the default (0 disables it on backends that support that).
- Avoid shell `find` and `grep`; use the `glob` and `grep` tools to search. Use `read_file` rather than `cat`, `head`, or `tail`.

Only available on backends implementing SandboxBackendProtocol; otherwise it returns an error."""


def build_rooted_filesystem_middleware(
    backend: BackendProtocol,
    *,
    permissions: Sequence[FilesystemPermission] | None = None,
) -> FilesystemMiddleware:
    """Build permission-aware middleware for virtual file and Shell paths.

    Args:
        backend: Final backend used by the Deep Agents filesystem tools.
        permissions: Route-scoped filesystem rules also supplied to
            ``create_deep_agent``. Deep Agents rejects rules for executable default
            backend paths because Shell commands can bypass file-tool enforcement.

    Returns:
        Filesystem middleware configured for rooted OpenSandbox path semantics.

    Raises:
        NotImplementedError: The backend supports Shell execution and a permission
            path is not scoped to a non-Shell ``CompositeBackend`` route.
    """
    return FilesystemMiddleware(
        backend=backend,
        system_prompt=ROOTED_FILESYSTEM_SYSTEM_PROMPT,
        custom_tool_descriptions={
            "execute": ROOTED_EXECUTE_TOOL_DESCRIPTION,
        },
        _permissions=list(permissions or ()),
    )

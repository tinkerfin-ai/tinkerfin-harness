"""Immutable results of bounded queries against existing project files."""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal


@dataclass(frozen=True, slots=True)
class WorkspaceFileInfo:
    """Describe one virtual path without revealing its physical location.

    ``modified_at`` is UTC. ``size_bytes`` is absent for directories and special
    entries. ``etag`` is an opaque metadata change token, not a content digest or
    a transactional snapshot. Symbolic links are listed but never followed.
    """

    path: str
    name: str
    kind: Literal["file", "directory", "symlink", "other"]
    size_bytes: int | None
    modified_at: datetime
    etag: str


@dataclass(frozen=True, slots=True)
class WorkspaceDirectoryPage:
    """Return a bounded directory page, ordered by directories then exact name.

    ``next_cursor`` belongs to this directory observation. A namespace change
    invalidates it; restart at the first page after a file-changed failure.
    """

    path: str
    entries: tuple[WorkspaceFileInfo, ...]
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class WorkspaceText:
    """Return a UTF-8 text prefix and metadata from the same opened regular file.

    ``truncated`` indicates that either caller-supplied byte or line limits hid
    additional content. A split UTF-8 character is never included in ``text``.
    Concurrent changes detected during the read fail instead of returning a
    mixture; changes after completion are reported by workspace observation.
    """

    file: WorkspaceFileInfo
    text: str
    truncated: bool

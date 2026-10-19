"""Validated requests consumed inside the native file-operation sandbox."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class FileRequest(BaseModel):
    """Carry one bounded file operation through a private standard-input pipe."""

    model_config = ConfigDict(extra="forbid", strict=True)

    operation: Literal[
        "ls",
        "read",
        "write",
        "edit",
        "delete",
        "glob",
        "grep",
        "upload",
        "download",
        "resolve",
    ]
    root: str
    user_home: str
    path: str = "."
    content: str = ""
    pattern: str = ""
    old: str = ""
    new: str = ""
    offset: int = 0
    limit: int = 2000
    replace_all: bool = False
    include: str | None = None
    max_count: int | None = None
    max_file_bytes: int = Field(gt=0)

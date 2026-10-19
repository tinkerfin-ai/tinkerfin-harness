"""Host-authorized access and bounded local execution settings."""

import re
import socket
from dataclasses import dataclass
from ipaddress import ip_address
from pathlib import Path
from typing import Literal


@dataclass(frozen=True, slots=True)
class LocalAccess:
    """Select filesystem reach and network access for one local workspace.

    ``workspace`` permits the working directory and explicit extra grants.
    ``user`` permits native user paths subject to protected and read-only paths
    and operating-system permissions. Approval remains the host's tool policy.
    Each isolated root permits its bound working directory, if beneath it,
    and reads of explicit ``read_directories`` strictly beneath that root.
    Roots can be outside the working directory; other descendants, including
    future siblings, remain inaccessible. A read grant to a root or its
    ancestor does not expose the tree. Only the working directory can receive
    writes or Unix connections inside an isolated tree, subject to the other
    grants. Nested roots each retain these restrictions. Roots and their read
    exceptions must be existing directories; a root cannot be ``/`` or the
    working directory itself.
    Protected paths and inaccessible isolated directories also deny connections
    to Unix sockets within them, including connections through path aliases.
    Protected paths remain inaccessible even when explicit read, write or Unix
    grants overlap them. Extra grants never permit writes to the application
    runtime. ``normal`` uses native networking and ordinary macOS GUI
    and Unix-socket services without requiring Node or SRT. In user mode,
    credential directories are ordinary user paths unless explicitly protected.
    ``restricted`` permits only allowed_domains; an empty tuple denies outbound
    access. Restricted grants exclude IP literals and localhost names. Hostname
    resolution rejects private destinations in restricted mode; it is not an
    independent IP firewall. Operating-system permissions still apply.
    ``unix_socket_paths`` permits restricted Unix binding and connections at
    specific socket files or beneath directories. Socket files may be created
    later. These grants do not add filesystem access, allow TCP loopback, or
    override protected paths and isolated roots. The filesystem root is not a
    valid Unix grant. Normal mode already permits ordinary Unix services.
    In restricted mode, ``graphics`` adds private IPC and read-only system
    queries for supported multiprocess graphical programs. Normal mode already
    permits these services and does not consult this flag. It is not a blanket
    prohibition of windows when disabled or an operating-system authorization.
    Read-only paths prevent writes inside otherwise writable grants without
    granting additional read access.
    """

    filesystem: Literal["workspace", "user"] = "workspace"
    network: Literal["normal", "restricted"] = "normal"
    graphics: bool = False
    isolated_roots: tuple[Path, ...] = ()
    read_directories: tuple[Path, ...] = ()
    write_directories: tuple[Path, ...] = ()
    protected_paths: tuple[Path, ...] = ()
    read_only_paths: tuple[Path, ...] = ()
    allowed_domains: tuple[str, ...] = ()
    unix_socket_paths: tuple[Path, ...] = ()

    def __post_init__(self) -> None:
        """Reject invalid filesystem reach and private-address network grants."""
        if self.filesystem not in {"workspace", "user"}:
            raise ValueError("filesystem must be workspace or user")
        if not isinstance(self.graphics, bool):
            raise TypeError("graphics must be a boolean")
        if self.network not in {"normal", "restricted"}:
            raise ValueError("network must be normal or restricted")
        if self.network == "normal" and self.allowed_domains:
            raise ValueError("allowed_domains requires restricted network access")
        if Path("/") in self.unix_socket_paths:
            raise ValueError("unix_socket_paths cannot grant the filesystem root")
        if len(self.isolated_roots) > 32:
            raise ValueError("isolated_roots permits at most 32 entries")
        if len(self.allowed_domains) > 128:
            raise ValueError("allowed_domains permits at most 128 entries")
        for value in self.allowed_domains:
            match = re.fullmatch(
                r"(?P<wildcard>\*\.)?(?P<host>[a-zA-Z0-9](?:[a-zA-Z0-9.-]{0,251}[a-zA-Z0-9])?)(?::(?P<port>[0-9]{1,5}))?",
                value,
            )
            if match is None:
                raise ValueError(
                    "allowed_domains requires domain names or subdomain patterns"
                )
            host = match["host"].lower().rstrip(".")
            port = match["port"]
            if (
                host == "localhost"
                or host.endswith(".localhost")
                or ".." in host
                or (match["wildcard"] and "." not in host)
                or (port is not None and not 1 <= int(port) <= 65535)
            ):
                raise ValueError("allowed_domains requires external domain names")
            try:
                ip_address(host)
            except ValueError:
                try:
                    # URL parsers can normalize shortened, hexadecimal and
                    # octal IPv4 spellings before the proxy sees the host.
                    socket.inet_aton(host)
                except OSError:
                    continue
            raise ValueError("IP address grants bypass private-address protection")


@dataclass(frozen=True, slots=True)
class LocalLimits:
    """Bound command output, file transfers, and command lifetime.

    Byte limits apply before decoding. Command timeout is in seconds and must
    be positive. File operations also use this timeout and never spawn a
    synchronous driver in the agent event loop.
    """

    output_bytes: int = 4 * 1024 * 1024
    file_bytes: int = 64 * 1024 * 1024
    command_timeout: float = 3600
    processes: int = 16

    def __post_init__(self) -> None:
        """Reject unbounded or invalid limits before opening resources."""
        import math

        if self.output_bytes < 1024 or self.file_bytes < 1:
            raise ValueError("byte limits must be positive; output requires 1024 bytes")
        if not math.isfinite(self.command_timeout) or self.command_timeout <= 0:
            raise ValueError("command_timeout must be finite and positive")
        if isinstance(self.processes, bool) or not 1 <= self.processes <= 64:
            raise ValueError("processes must be between 1 and 64")

"""Safe failures from native local execution."""

from collections.abc import Mapping
from enum import StrEnum

from ..errors import SandboxError


class LocalErrorCode(StrEnum):
    """Stable categories for local workspace failures."""

    UNAVAILABLE = "sandbox.local.unavailable"
    CLOSED = "sandbox.local.closed"
    LIMIT = "sandbox.local.limit"
    TIMEOUT = "sandbox.local.timeout"
    PROTOCOL = "sandbox.local.protocol"
    CLEANUP = "sandbox.local.cleanup"


class LocalError(SandboxError):
    """Report a safe local failure while retaining private diagnostic evidence.

    Args:
        code: Stable category for callers and product messages.
        message: Client-safe explanation without command or file contents.
        context: Non-sensitive facts safe for serialization.
        cause: Original failure, for trusted diagnostics only.
    """

    def __init__(
        self,
        code: LocalErrorCode,
        message: str,
        *,
        context: Mapping[str, str | int] | None = None,
        cause: BaseException | None = None,
    ) -> None:
        """Copy public context while retaining the original private failure."""
        super().__init__(message, context=context, cause=cause)
        self.code = code

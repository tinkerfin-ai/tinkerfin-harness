"""Stable OpenSandbox configuration and query models.

SDK models appear only at conversion boundaries and never leak through public query
results. Every model is frozen so callers cannot mutate cached snapshots in place.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import PurePosixPath
from typing import Literal, Self

from opensandbox.models.sandboxes import SandboxInfo, Volume
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from tinkerfin_contracts.identity import validate_namespace

DEFAULT_SANDBOX_IMAGE = (
    "ghcr.io/tinkerfin-ai/sandbox-runtime@"
    "sha256:c633c3bf0cf8a4bfde2372bb81cfb9e567ccc90f472e0f2772949123cc4c930d"
)
_RESERVED_METADATA_PREFIX = "tinkerfin.ai/"

OpenSandboxPurpose = Literal["commands", "workspaces"]
OpenSandboxUnavailableReason = Literal["not_found", "unreachable"]
_AccessState = Literal[
    "running", "draining", "pausing", "paused", "resuming", "uncertain"
]


class OpenSandboxDiagnosticContent(BaseModel):
    """Best-effort diagnostic text or an expiring download reference.

    Content and URLs are for trusted diagnostics and are never fetched or logged
    automatically. ``warnings`` describe retention gaps or unavailable sources;
    ``truncated`` describes a deliberate limit on the returned payload.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    sandbox_id: str
    kind: Literal["logs", "events"]
    scope: str
    delivery: Literal["inline", "url"]
    content_type: str = Field(description="Media type of the diagnostic text")
    truncated: bool
    content: str | None = Field(default=None, repr=False)
    content_url: str | None = Field(default=None, repr=False)
    content_length: int | None = Field(
        default=None, ge=0, description="Payload size in bytes, when known"
    )
    expires_at: datetime | None = None
    warnings: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _validate_delivery(self) -> Self:
        if self.delivery == "inline":
            if (
                self.content is None
                or self.content_url is not None
                or self.expires_at is not None
            ):
                raise ValueError(
                    "Inline diagnostics require text without a download reference"
                )
        elif (
            self.content_url is None
            or self.expires_at is None
            or self.content is not None
        ):
            raise ValueError(
                "URL diagnostics require a download reference and expiration without inline text"
            )
        return self


def _normalize_workspace_root(value: str | None) -> str | None:
    """Normalize an in-sandbox absolute POSIX path for use as a virtual root."""
    if value is None:
        return None
    if "\x00" in value:
        raise ValueError("workspace_root must not contain null bytes")
    if value.startswith("//"):
        raise ValueError("workspace_root must not use a double-slash root")

    path = PurePosixPath(value)
    if not path.is_absolute():
        raise ValueError("workspace_root must be an absolute POSIX path")
    if ".." in path.parts:
        raise ValueError("workspace_root must not contain parent-directory segments")

    normalized = str(path)
    if normalized == "/":
        raise ValueError("workspace_root must not be the sandbox root directory")
    return normalized


class OpenSandboxConfig(BaseModel):
    """Configure OpenSandbox creation, connection, execution, and warm pooling.

    This external configuration boundary validates time and capacity limits. Time
    fields use ``timedelta`` except ``command_timeout``, which uses integer seconds
    for the Deep Agents protocol. A zero command timeout disables the SDK limit.
    Set ``ttl=None`` to require explicit remote cleanup; this does not change
    State ownership at manager close or provide persistent file storage.
    """

    image: str = Field(
        default=DEFAULT_SANDBOX_IMAGE,
        description="Container image reference used to create a sandbox.",
    )
    entrypoint: list[str] = Field(
        default_factory=lambda: ["/opt/sandbox-runtime/bin/entrypoint.sh"],
        description="Entrypoint command and arguments passed to a new sandbox.",
    )
    env: dict[str, str] = Field(
        default_factory=dict,
        description="Environment variables injected into a new sandbox.",
    )
    metadata: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Non-sensitive host metadata attached at creation; keys under "
            "'tinkerfin.ai/' are reserved for lifecycle recovery"
        ),
    )
    resource: dict[str, str] = Field(
        default_factory=lambda: {"cpu": "1", "memory": "2Gi"},
        description="CPU, memory, and other resources requested for a new sandbox.",
    )
    volumes: tuple[Volume, ...] = Field(
        default_factory=tuple,
        description=(
            "OpenSandbox volumes mounted at creation, including PVC, Host, and OSSFS "
            "volumes. Definitions are deep-copied so later caller mutations cannot "
            "change creation parameters."
        ),
    )
    ttl: timedelta | None = Field(
        default=timedelta(hours=2),
        gt=timedelta(0),
        description=(
            "Positive remote sandbox lifetime from creation or renewal. None "
            "requires explicit cleanup and disables automatic expiry and renewal."
        ),
    )
    lifecycle_request_timeout: timedelta = Field(
        default=timedelta(minutes=10),
        gt=timedelta(0),
        description=(
            "Per-request control-plane timeout used when the SDK request_timeout is "
            "unset. It must cover cold image pulls and container creation."
        ),
    )
    ready_timeout: timedelta = Field(
        default=timedelta(minutes=5),
        gt=timedelta(0),
        description="Maximum time to wait for a newly created sandbox to become ready.",
    )
    connect_timeout: timedelta = Field(
        default=timedelta(seconds=30),
        gt=timedelta(0),
        description="Maximum time to reconnect to an existing sandbox.",
    )
    command_timeout: int = Field(
        default=60 * 60,
        ge=0,
        strict=True,
        description="Default command timeout in seconds; zero disables the limit.",
    )
    workspace_root: str | None = Field(
        default="/workspace",
        description=(
            "Absolute in-sandbox directory exposed as the Deep Agents file-tool "
            "virtual root and default shell working directory. Absolute shell paths "
            "bypass virtual-root mapping. None disables directory initialization, "
            "the default working directory, and application-level rooted views."
        ),
    )
    health_command: str = Field(
        default="printf ok",
        description="Lightweight shell command used to verify command execution.",
    )
    warm_pool_size: int = Field(
        default=1,
        ge=0,
        strict=True,
        description="Target number of unbound warm sandboxes maintained by the manager.",
    )
    command_env: dict[str, str] = Field(
        default_factory=dict,
        description="Environment variables injected by the SDK for every shell command.",
    )
    enable_capture_offload: bool = Field(
        default=False,
        description="Whether Deep Agents may offload large command output to sandbox files.",
    )

    model_config = ConfigDict(frozen=True)

    @field_validator("image", "health_command")
    @classmethod
    def _must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("value must not be blank")
        return value

    @field_validator("workspace_root")
    @classmethod
    def _normalize_workspace_root(cls, value: str | None) -> str | None:
        """Normalize an in-sandbox absolute POSIX path as the virtual root."""
        return _normalize_workspace_root(value)

    @field_validator("metadata")
    @classmethod
    def _reject_reserved_metadata(cls, value: dict[str, str]) -> dict[str, str]:
        """Reject host overrides of metadata reserved for recovery and ownership."""
        reserved = sorted(
            key for key in value if key.startswith(_RESERVED_METADATA_PREFIX)
        )
        if reserved:
            raise ValueError(
                f"metadata keys under {_RESERVED_METADATA_PREFIX!r} are reserved"
            )
        return value

    @field_validator("volumes")
    @classmethod
    def _copy_volumes(cls, value: tuple[Volume, ...]) -> tuple[Volume, ...]:
        """Freeze snapshots of mutable SDK volume models supplied by the caller."""
        return tuple(volume.model_copy(deep=True) for volume in value)


class OpenSandboxStatusInfo(BaseModel):
    """Lifecycle status snapshot independent of SDK type and enum evolution."""

    state: str = Field(description="Current lifecycle state reported by OpenSandbox.")
    reason: str | None = Field(
        default=None,
        description="Machine-readable reason for the latest state transition.",
    )
    message: str | None = Field(
        default=None,
        description="Status message reported by OpenSandbox.",
    )
    last_transition_at: datetime | None = Field(
        default=None,
        description="UTC timestamp of the latest state transition.",
    )

    model_config = ConfigDict(frozen=True)


class OpenSandboxPlatformInfo(BaseModel):
    """Runtime platform snapshot independent of SDK types."""

    os: str = Field(description="Sandbox operating system.")
    arch: str = Field(description="Sandbox CPU architecture.")

    model_config = ConfigDict(frozen=True)


class OpenSandboxRuntimeInfo(BaseModel):
    """Remote sandbox snapshot without user ownership data.

    ``available`` reports whether control-plane details are readable, while
    ``healthy`` reports whether the data-plane health command succeeds. Neither
    substitutes for the other. Unavailable snapshots omit underlying exception text
    so network locations, credentials, and unstable SDK messages are not exposed.
    """

    sandbox_id: str = Field(
        description="Unique sandbox identifier assigned by OpenSandbox."
    )
    available: bool = Field(
        description="Whether remote sandbox details were read successfully."
    )
    healthy: bool = Field(
        description="Whether the sandbox completed its health command successfully."
    )
    status: OpenSandboxStatusInfo | None = Field(
        default=None,
        description="Lifecycle status when remote details are available.",
    )
    created_at: datetime | None = Field(
        default=None,
        description="Remote sandbox creation time.",
    )
    expires_at: datetime | None = Field(
        default=None,
        description=(
            "Scheduled automatic termination time. None means manual cleanup when "
            "available, or unknown expiry when remote details are unavailable."
        ),
    )
    image: str | None = Field(
        default=None,
        description="Remote sandbox image reference without registry credentials.",
    )
    platform: OpenSandboxPlatformInfo | None = Field(
        default=None,
        description="Operating system and CPU architecture used by the remote sandbox.",
    )
    metadata: dict[str, str] = Field(
        default_factory=dict,
        description="Non-sensitive business metadata attached to the remote sandbox.",
    )
    unavailable_reason: OpenSandboxUnavailableReason | None = Field(
        default=None,
        description="Stable reason code when remote details cannot be read.",
    )

    model_config = ConfigDict(frozen=True)

    @classmethod
    def from_sdk(cls, info: SandboxInfo, *, healthy: bool) -> Self:
        """Copy available SDK details into a stable query model.

        Args:
            info: Validated details returned by OpenSandbox.
            healthy: Result of the independent health-command probe.

        Returns:
            A runtime snapshot that no longer references mutable SDK containers.
        """
        status = OpenSandboxStatusInfo(
            state=info.status.state,
            reason=info.status.reason,
            message=info.status.message,
            last_transition_at=info.status.last_transition_at,
        )
        platform = None
        if info.platform is not None:
            platform = OpenSandboxPlatformInfo(
                os=info.platform.os,
                arch=info.platform.arch,
            )
        return cls(
            sandbox_id=info.id,
            available=True,
            healthy=healthy,
            status=status,
            created_at=info.created_at,
            expires_at=info.expires_at,
            image=info.image.image if info.image is not None else None,
            platform=platform,
            metadata=dict(info.metadata or {}),
        )

    @classmethod
    def unavailable(
        cls,
        sandbox_id: str,
        reason: OpenSandboxUnavailableReason,
    ) -> Self:
        """Create an unavailable snapshot without underlying exception text.

        Args:
            sandbox_id: OpenSandbox identifier being queried.
            reason: Stable public reason code for unavailability.

        Returns:
            A minimal snapshot with ``available`` and ``healthy`` both false.
        """
        return cls(
            sandbox_id=sandbox_id,
            available=False,
            healthy=False,
            unavailable_reason=reason,
        )


class OpenSandboxDetails(OpenSandboxRuntimeInfo):
    """Stable sandbox details enriched with ownership and local handle state.

    ``cached`` only reports whether an open in-memory handle exists. It does not mean
    remote details or health results came from a cache.
    """

    owner_key: str = Field(
        description="User ID or other business key that owns the sandbox."
    )
    namespace: str | None = Field(
        default=None,
        description="Logical resource scope; None identifies standalone Sandbox use",
    )
    cached: bool = Field(
        description="Whether the manager currently holds an in-memory handle."
    )
    access_state: _AccessState | None = Field(
        default=None,
        description="Framework admission state, separate from remote runtime status; None when not queried",
    )

    @field_validator("namespace")
    @classmethod
    def validate_resource_namespace(cls, value: str | None) -> str | None:
        """Use the shared Runtime identity rules for logical resource scopes."""

        return None if value is None else validate_namespace(value)

    @classmethod
    def from_runtime(
        cls,
        runtime: OpenSandboxRuntimeInfo,
        *,
        owner_key: str,
        cached: bool,
        namespace: str | None = None,
        access_state: _AccessState | None = None,
    ) -> Self:
        """Add ownership and local handle state to a runtime snapshot.

        Args:
            runtime: Runtime snapshot without business ownership.
            owner_key: User ID or another stable business key.
            cached: Whether an open in-memory handle exists at query time.
            namespace: Logical resource scope, or None for standalone use.
            access_state: Authoritative framework admission state when queried.

        Returns:
            User sandbox details preserving every runtime field.
        """
        return cls(
            **runtime.model_dump(),
            owner_key=owner_key,
            namespace=namespace,
            cached=cached,
            access_state=access_state,
        )

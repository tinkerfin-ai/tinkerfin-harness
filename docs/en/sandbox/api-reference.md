# Sandbox usage reference

[Sandbox basics](index.md) · [中文](../../cn/sandbox/api-reference.md)

## Configuration and connection

### `OpenSandboxConfig`

| Field | Default | Purpose |
| --- | --- | --- |
| `image` | Version-pinned TinkerFin image | Image for new Sandboxes |
| `entrypoint` | `/opt/sandbox-runtime/bin/entrypoint.sh` | Container entry command |
| `env` | `{}` | Sandbox environment variables |
| `metadata` | `{}` | Application metadata; reserved ownership fields are rejected |
| `resource` | `cpu=1, memory=2Gi` | Resource request |
| `volumes` | `()` | OpenSandbox volumes |
| `ttl` | 2 hours | Positive lifetime from creation or renewal; `None` requires explicit cleanup |
| `lifecycle_request_timeout` | 10 minutes | Per-request control-plane timeout when the SDK timeout is implicit |
| `ready_timeout` | 5 minutes | Wait for a new Sandbox to become ready |
| `connect_timeout` | 30 seconds | Data-plane connection limit |
| `command_timeout` | 3600 seconds | Default non-negative command timeout |
| `workspace_root` | `/workspace` | Rooted file-tool directory; `None` disables rooted view |
| `health_command` | `printf ok` | Data-plane health command |
| `warm_pool_size` | `1` | Non-negative warm capacity |
| `command_env` | `{}` | Environment added to each Shell command |
| `enable_capture_offload` | `False` | Allow large command output to be saved to a file |

The default image pins an immutable [TinkerFin Sandbox Runtime](https://github.com/tinkerfin-ai/sandbox-runtime) digest and includes Playwright with headless Chromium. Image configuration affects newly created remote instances; reconnecting does not update an existing runtime.

`ttl=None` creates instances without automatic expiry and skips remote renewal.
It retains health checks and State ownership rules. Connecting does not change an
existing expiry. See [remote lifetime](lifecycle.md#remote-lifetime) for close and
storage behavior.

### `OpenSandboxClient`

| Parameter | Default | Purpose |
| --- | --- | --- |
| `connection_config` | required, may be `None` | Explicit OpenSandbox connection or SDK environment configuration |
| `config` | `None` | TinkerFin Sandbox settings |
| `initializers` | `()` | Idempotent callbacks run in order after creation or connection |

The client uses OpenSandbox SDK 0.1.16 and official Server 0.2.3. All methods below
are asynchronous; client methods accept remote IDs, while manager methods accept
application keys.

| Method | Behavior |
| --- | --- |
| `create(metadata=None)` | Create an initialized backend and transfer ownership to the caller |
| `connect(sandbox_id)` | Connect and initialize an existing instance |
| `inspect(sandbox_id)` | Read details and execution health through a temporary connection; unreadable details return an unavailable snapshot |
| `get_runtime_info(sandbox_id)` | Read control-plane details without endpoint discovery, initialization, health checks, or renewal |
| `pause(sandbox_id)`, `resume(sandbox_id)` | Submit the official control-plane state change; manager methods coordinate holders and readiness |
| `get_diagnostic_logs(sandbox_id, scope="container")` | Read provider log diagnostics |
| `get_diagnostic_events(sandbox_id, scope="runtime")` | Read provider event diagnostics |
| `destroy(sandbox_id)` | Idempotently destroy the instance and close its temporary connection |
| `aclose()` | Finish owned work and close only the transport created by the client |

`get_runtime_info()` raises a backend error when the read fails. Its `healthy=False`
means no health probe was performed, rather than a failed probe. The low-level
pause/resume methods do not connect or initialize a backend. Use the manager for
ordinary pause/resume operations.

Implicit SDK transport retries are disabled. An explicit `ConnectionConfig.retry_policy`
is retained, but an enabled policy that replays POST/PATCH response failures is rejected
with `ValueError`. Caller-supplied transports keep their original policy and ownership.
Caller configuration, headers, and environment are not modified.

Initializers receive an `OpenSandboxBackend` and may return an awaitable or `None`.
Use native asynchronous callbacks for I/O. Synchronous callbacks run on the event
loop and must be non-blocking; the client does not move them into a thread. Connect
and initialization share `connect_timeout`, shortened to the recovery deadline when
called by a manager. Timeout and cancellation are cooperative
and cannot interrupt blocking synchronous work. Initialization failures raise
`OpenSandboxInitializationError` and do not authorize recovery retries or recreation.

## Manager

See [Sandbox lifecycle](lifecycle.md) for constructor parameters and operations.
`workspace(key)` returns the lazy declaration used by `TinkerFin.build(backend=...)`.
`build_rooted_filesystem_middleware(backend, ...)` is the standalone integration for
caller-managed Deep Agents Graphs.

`pause(key, timeout=30.0)` returns `None` after all registered holders finish work and
the remote pause is confirmed. `resume(key, timeout=30.0)` returns a ready backend for
the same instance. Both use a positive finite work budget in seconds. Scoped diagnostics
are available through `get_diagnostic_logs(key, scope="container")` and
`get_diagnostic_events(key, scope="runtime")` without waking the instance.

`await manager.check_ready()` returns `None` only when configured warm capacity is
verified. It raises `OpenSandboxWarmPoolUnavailableError` for startup or background
capacity failure and `OpenSandboxManagerClosedError` after shutdown.

Optional lifecycle notification contracts:

| API | Purpose |
| --- | --- |
| `OpenSandboxLifecycleObserver.on_sandbox_event(event)` | Borrowed asynchronous observer; cannot reenter its manager |
| `OpenSandboxNotificationOptions(max_pending_events=128, timeout=1.0)` | Independent pending limit and per-call seconds for each observer |
| `OpenSandboxLifecycleEvent` | Immutable event identity, type, owner, UTC time, reason, effects, and trusted diagnostics |
| `OpenSandboxLifecycleEventType` | User Sandbox changes, explicit workspace reset, and separate warm-capacity changes |
| `OpenSandboxLifecycleReason` | Enumerated connection, health, initialization, State, and explicit-operation causes |

Configure `observers=()` and `notification_options=None` on the manager. See
[Lifecycle notifications](lifecycle.md#lifecycle-notifications) for event and delivery
semantics. `diagnostic_context` is trusted-only and is not a client response.

## Backends and handles

| API | Use |
| --- | --- |
| `OpenSandboxBackend` | One connected asynchronous OpenSandbox data plane |
| `OpenSandboxHandle` | Stable borrowed handle across remote replacement |
| `RootedOpenSandboxBackend` | Maps virtual `/` into the configured workspace root |
| `build_rooted_filesystem_middleware(...)` | Builds matching middleware without a manager |

For a custom client that constructs these values directly, `OpenSandboxBackend` accepts the native `sandbox`, `default_timeout=60`, optional `command_env`, optional `working_directory`, `health_command="printf ok"`, and `enable_capture_offload=False`. `OpenSandboxHandle` accepts a backend; `RootedOpenSandboxBackend` accepts a handle and `root="/workspace"`.

Common asynchronous methods:

| Category | Methods |
| --- | --- |
| Shell | `aexecute(command, timeout=None)` |
| Files | `aread`, `awrite`, `aedit`, `adelete`, `als`, `aglob`, `agrep` |
| Transfer | `aupload_files`, `adownload_files` |
| Large output | `aexecute_with_offload` |
| Lifecycle | `arenew(timeout)`, `aget_runtime_info()`, `akill()`, `aclose()` |

`RootedOpenSandboxBackend.to_shell_path(file_path)` converts a virtual path for Shell. Prefer middleware-managed mapping in ordinary applications.

Synchronous remote methods fail explicitly; use the asynchronous forms.

## State implementations

| API | Use |
| --- | --- |
| `OpenSandboxState` | Custom binding, lease, warm-pool, availability, holder coordination, and cleanup protocol |
| `InMemoryOpenSandboxState(namespace="")` | Current-process state |
| `SQLAlchemyOpenSandboxState(...)` | Shared SQLite, MySQL, or PostgreSQL state through a borrowed `engine` |
| `get_sqlalchemy_opensandbox_state_schema(dialect=...)` | Generate complete schema DDL |
| `SQLAlchemyOpenSandboxStateSchema` | Immutable dialect, table names, and DDL |

`OpenSandboxInitializer` receives a ready backend after creation or connection and
returns `Awaitable[None] | None`; synchronous callbacks must be non-blocking.

### Immutable claims and bindings

| Type | Fields |
| --- | --- |
| `OpenSandboxBinding` | `sandbox_id`, `generation` |
| `OpenSandboxOwnerClaim` | owner key, digest, token, generation, optional binding |
| `OpenSandboxWarmClaim` | slot, token, generation |
| `OpenSandboxReadyWarmClaim` | warm claim fields plus the published Sandbox ID |
| `OpenSandboxCleanupClaim` | sandbox ID, token, generation |

These types mainly support custom `OpenSandboxState` implementations.

### Availability and registered holders

| Type | Fields |
| --- | --- |
| `OpenSandboxAvailability` | `owner_digest`, `sandbox_id`, `binding_generation`, `sequence`, `phase`, `connection_generation` |
| `OpenSandboxAvailabilityPhase` | `running`, `draining`, `pausing`, `paused`, `resuming`, `uncertain` |
| `OpenSandboxHolderUpdate` | `holder_id`, `owner_digest`, `sandbox_id`, `binding_generation`, `acknowledged_sequence`, `availability` |

Custom State implementations also provide these asynchronous operations:

| Method | Required behavior |
| --- | --- |
| `register_holder(claim, holder_id)` | Atomically register a manager for the current running binding before publishing its handle |
| `read_availability(owner_key)` | Read the current intent without waiting for an owner claim; `None` means no binding |
| `get_holder_updates(holder_id)` | Return a batch of holder registrations and their current availability |
| `change_availability(claim, expected, phase=..., refresh_connection=False)` | Change only the exact fenced snapshot; entering `pausing` requires all current drain acknowledgements |
| `acknowledge_idle(holder_id, availability)` | Record explicit closed admission and settled work for the exact drain |
| `holders_are_idle(claim, availability)` | Require every registered holder's acknowledgement for that drain |
| `unregister_holder(holder_id, availability)` | Release the exact binding registration only after admission closes and work settles |

`sequence` advances on intent changes; `connection_generation` advances when holders
must reconnect. Both belong to one `binding_generation`. Stale bindings and sequences
cannot acknowledge or mutate newer ones. Holder IDs identify a manager lifetime, are
non-empty, and contain at most 36 characters. Worker expiry, missing heartbeats, and
State shutdown never substitute for an idle acknowledgement. Storage failures raise
State errors rather than granting admission.

## Runtime information

| Model | Main fields |
| --- | --- |
| `OpenSandboxStatusInfo` | `state`, optional reason/message/last transition time |
| `OpenSandboxPlatformInfo` | `os`, `arch` |
| `OpenSandboxRuntimeInfo` | ID, availability, health, status, times, image, platform, metadata, unavailable reason |
| `OpenSandboxDetails` | RuntimeInfo plus `owner_key`, `cached`, and optional `access_state` |
| `OpenSandboxUnavailableReason` | `not_found` or `unreachable` |

`access_state` is the framework's shared coordination phase: `running`, `draining`,
`pausing`, `paused`, `resuming`, or `uncertain`. `None` means no coordination snapshot
was supplied. `manager.get_details()` reads that State alongside provider details;
remote `status.state="Running"` can coexist with `access_state="draining"` or an
unconfirmed lifecycle request. `cached` describes local handle presence and does not
prove that the handle is currently accepting work.

### Diagnostic content

`OpenSandboxDiagnosticContent` is immutable. It contains `sandbox_id`, `kind`
(`logs` or `events`), `scope`, `delivery` (`inline` or `url`), `content_type`,
`truncated`, and `warnings`. Inline delivery contains `content`; URL delivery contains
`content_url` and `expires_at`. `content_length`, when present, is measured in bytes.
Warnings describe missing sources or retention gaps and are an empty tuple when absent.

Docker supports `container`/`all` log scopes and `runtime`/`all` event scopes. Event
content is a current state summary, not a complete event history. Returned URLs are
not fetched automatically; diagnostic content and references are intended for trusted
operators under host-controlled access.

## Errors

| Error | Meaning |
| --- | --- |
| `OpenSandboxStateError` | Base state failure |
| `OpenSandboxStateOwnershipError` | Claim expired, was replaced, or belongs to another worker |
| `OpenSandboxStateConfigurationError` | Unsupported state, database, or schema configuration |
| `OpenSandboxDestroyError` | Remote destruction could not settle reliably |
| `OpenSandboxInitializationError` | Workspace setup or an initializer failed; not eligible for recovery retries |
| `OpenSandboxBackendUnavailableError` | Existing instance recovery failed or a provider rejected access |
| `OpenSandboxBackendTimeoutError` | A connection, pause, resume, or other backend work budget expired |
| `OpenSandboxPausedError` | Data-plane access requires explicit resume |
| `OpenSandboxBusyError` | A pending pause is waiting for admitted operations to finish |
| `OpenSandboxLifecycleUncertainError` | A lifecycle request has no confirmed outcome; access cannot safely reopen |
| `OpenSandboxResetError` | No safe workspace root or reset failed |
| `OpenSandboxHandleOwnershipError` | Backend ownership is no longer valid |
| `OpenSandboxManagerClosedError` | A closed manager was used |
| `OpenSandboxObserverReentryError` | A lifecycle observer tried to operate or close its own manager |
| `OpenSandboxSettlementTimeoutError` | Caller wait for protected close expired |

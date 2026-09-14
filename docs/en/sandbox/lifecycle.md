# Sandbox lifecycle

[Sandbox basics](index.md) · [中文](../../cn/sandbox/lifecycle.md)

`OpenSandboxManager` keeps one stable handle for each business key. The same handle remains usable when its remote Sandbox reconnects or is replaced.

## Manager configuration

```python
manager = OpenSandboxManager(
    client=client,
    key_resolver=lambda key: str(key),
    state=None,
    warm_pool_size=None,
    fail_on_startup_warmup_error=False,
    settlement_timeout=None,
    recovery_policy=None,
    observers=(),
    notification_options=None,
)
```

| Parameter | Default | Purpose |
| --- | --- | --- |
| `client` | required | Async creation, connection, inspection, and destruction |
| `key_resolver` | `None` | Converts custom keys to stable non-empty strings; string keys work directly |
| `state` | `None` | Binding and lease state; defaults to in-memory |
| `warm_pool_size` | `None` | Overrides the configured warm capacity |
| `fail_on_startup_warmup_error` | `False` | Makes `start()` fail if initial warmup fails |
| `settlement_timeout` | `None` | Maximum caller wait for manager close |
| `recovery_policy` | `None` | Existing-instance retries; defaults preserve the instance on failure |
| `observers` | `()` | Borrowed asynchronous lifecycle observers |
| `notification_options` | `None` | Per-observer pending capacity and callback timeout |

Warm-pool sizes are strict integers. Command and lifecycle timeouts are finite numeric
values; booleans are rejected before State startup or task creation.

## Common operations

| Method | Behavior | Does the remote ID normally change? |
| --- | --- | --- |
| `get(key)` | Create an initial Sandbox or recover the existing binding | Only with explicit recreation policy |
| `reconnect(key)` | Reconnect an existing binding; fail if none exists | No |
| `recreate(key)` | Commit a replacement and retire the old instance | Yes |
| `reset(key)` | Clear workspace-root contents | No |
| `pause(key, timeout=30.0)` | Drain registered holders and confirm remote pause | No |
| `resume(key, timeout=30.0)` | Resume and return a ready backend for the original instance | No |
| `destroy(key)` | Destroy known instances and remove the binding | Binding is removed |
| `delete(key)` | Alias for `destroy()` | Binding is removed |
| `is_healthy(key)` | Probe the current instance | No |
| `get_details(key)` | Return runtime and owner information | No |
| `get_diagnostic_logs(key, scope="container")` | Read scoped provider logs through the control plane | No |
| `get_diagnostic_events(key, scope="runtime")` | Read scoped provider event diagnostics | No |
| `check_ready()` | Raise unless configured warm capacity is verified | No |

```python
backend = await manager.get(project_key)

details = await manager.get_details(project_key)
```

Most requests only need `get()`. Recreating on every request defeats reuse and warm capacity.

## Pause and resume

```python
backend = await manager.get(project_key)
await manager.pause(project_key, timeout=30.0)
backend = await manager.resume(project_key, timeout=30.0)
```

`pause()` preserves the remote ID, files, and existing handles. It waits for work
across managers sharing the same State to finish before pausing the instance.
A lost worker or an unresolved remote operation can prevent pause from completing.

The default `timeout` is 30 seconds and must be finite and positive. Cleanup may
extend the total wait. An unconfirmed remote result keeps access closed until the
outcome can be established; do not assume that a timeout means the instance paused.

Instances paused through the manager require explicit `resume()`; `get()`,
`reconnect()`, and `reset()` do not wake them. Resume returns a ready backend for
the original instance. Connection initializers run again, so they must be idempotent.
A failed connection refresh leaves the affected handle unavailable.

Official OpenSandbox Server 0.2.3 uses Docker pause/unpause. `resume()` cannot start a
container stopped through Docker or recover an expired instance. Pause does not freeze
or extend the remote TTL; a paused instance with a finite lifetime can expire.

## Diagnostics

```python
logs = await manager.get_diagnostic_logs(project_key, scope="container")
events = await manager.get_diagnostic_events(project_key, scope="runtime")
```

Both queries use the existing binding and the control plane. They do not create,
connect, initialize, renew, or wake a Sandbox. A missing binding raises a backend error.
Docker log scopes are `container` and `all`; event scopes are `runtime` and `all`.
Docker events describe current runtime state, not a complete historical event stream.

The immutable `OpenSandboxDiagnosticContent` result distinguishes inline `content`
from `content_url` with an expiration. `content_type`, optional byte length,
`truncated`, and `warnings` describe the returned content and source limitations.
The framework does not fetch URLs automatically. Hosts control who can read diagnostic
text and references and how that operational data is retained.

## Remote lifetime

`OpenSandboxConfig.ttl` defaults to two hours from creation or renewal. Supply a
positive `timedelta` for automatic expiry, or `None` when a workspace should live
until explicit cleanup:

```python
config = OpenSandboxConfig(ttl=None)
```

New instances created with `ttl=None` have no scheduled expiry.
Connecting to an existing instance does not remove its
scheduled expiry. Non-expiring instances keep consuming resources until destroyed;
use `destroy(key)` when they are no longer needed.

Use persistent State when bindings must survive a normal manager close. Default
in-memory State still destroys owned instances at close. Manual cleanup does not
persist or back up files: external deletion and storage failure can still lose
them. Configure volumes and a backup policy for files that must survive instance loss.

## Start and close

`async with manager` calls `start()` and `aclose()` for you. Manual control looks like this:

```python
await manager.start()
try:
    backend = await manager.get(key)
finally:
    await manager.aclose()
```

`start()` is idempotent. A closed manager cannot be restarted.

Include `await manager.check_ready()` in application readiness checks. It raises
`OpenSandboxWarmPoolUnavailableError` when configured warm capacity is unavailable.
Set `fail_on_startup_warmup_error=True` to fail startup when that capacity cannot be
verified. Background warm-capacity failures do not invalidate an instance already
in use by a request.

Close waits for active creation, replacement, reset, pause/resume coordination, and cleanup to settle safely. A finite `settlement_timeout` only limits this caller's wait. It raises `OpenSandboxSettlementTimeoutError` without cancelling owned cleanup; call `aclose()` later to continue waiting.

The default in-memory State owns remote instances for the manager lifetime and
destroys them during close. Persistent State retains remote bindings for other
workers. Preserving an instance after a failed recovery does not change these close
semantics or extend its TTL.

## Health checks and replacement

`OpenSandboxConfig.health_command` probes the data plane and defaults to `printf ok`.
By default, `get()` attempts to recover the same instance up to three times, including
the initial check, with a 30-second work budget. Retry delays start at 0.5 seconds, double after
each retry, and stop at 2 seconds. Exhaustion raises `OpenSandboxBackendUnavailableError`
and preserves the instance and its binding. Confirmed absence skips futile retries.

For disposable workspaces, opt into recreation:

```python
from tinkerfin_sandbox import OpenSandboxManager, OpenSandboxRecoveryPolicy

manager = OpenSandboxManager(
    client=client,
    key_resolver=resolve_owner,
    recovery_policy=OpenSandboxRecoveryPolicy(on_failure="recreate"),
)
```

The policy also accepts `max_attempts`, `initial_delay`, `max_delay`, and `timeout`.
Only recognized connection or health failures qualify. Authentication, permission,
protocol, initialization, and State failures are reported without retries or
recreation. If the total budget expires while a connection or initializer is still
unresolved, the binding is preserved. `reconnect()` and `reset()` always retain remote
identity, regardless of the policy. Commands, file writes, and resets are never replayed.

The work budget covers connection, health checks, and retry delays. Native connection
and initialization use the earlier client or recovery deadline. Necessary cancellation
and resource settlement finish before the owner claim is released, so elapsed call
time can exceed the work budget. The next recovery cannot overlap an initializer
still settling from the previous call. Blocking or cancellation-suppressing callbacks
cannot be forcibly stopped.

Recreation commits a new instance and then retires the authoritative old instance;
it does not copy files. Persistent State stores bindings, not container contents.
Preserving a binding cannot recover files already lost to external deletion or TTL
expiry. Configure volumes or another storage policy when files must survive those events.

In-flight operations finish against the backend they acquired. The old instance is not closed underneath them, and replacement waits for safe retirement before returning.

## Lifecycle notifications

Pass observers only when the host needs lifecycle changes. Ordinary `get()` calls
need no notification configuration:

```python
from tinkerfin_sandbox import OpenSandboxLifecycleEvent, OpenSandboxManager


class SandboxEvents:
    async def on_sandbox_event(self, event: OpenSandboxLifecycleEvent) -> None:
        await record_status(event.owner_key, event.type, event.reason)


manager = OpenSandboxManager(
    client=client,
    key_resolver=resolve_owner,
    observers=[SandboxEvents()],
)
```

| Event | Confirmed fact |
| --- | --- |
| `unavailable` | An existing access or check found the owner's Sandbox unusable |
| `recovering` | A known outage is being reconnected, or replacement is starting |
| `recovered` | The same instance is usable again |
| `replaced` | A different instance is bound and its verified handle is published |
| `recovery_failed` | Recovery or requested replacement failed; the operation still reports its error |
| `workspace_reset` | An explicit reset finished clearing the configured workspace |
| `destroyed` | Explicit destruction and binding removal completed |
| `paused` | Explicit pause was confirmed for the bound instance |
| `resumed` | Explicit resume returned a ready local handle for the same instance |
| `warm_capacity_degraded` / `warm_capacity_restored` | Verified unbound capacity became unavailable or available |

Events contain a unique `event_id`, `type`, host-resolved `owner_key`, UTC
`occurred_at`, and an `OpenSandboxLifecycleReason`. The derived `recovered` and
`replaced` properties describe availability and remote identity.
Fields are immutable. Remote identifiers appear only in `diagnostic_context`,
which is reserved for trusted observers and must not be forwarded to browsers.
Events contain no provider exceptions, commands, credentials, or file contents.
Choose owner keys appropriate for the notification audience.

`workspace_may_have_changed` flags lifecycle evidence of possible workspace changes:
confirmed remote absence, connection initializers and their partial effects,
replacement, explicit reset, or destruction. The flag is carried through recovery
success or failure, even when the remote ID is retained. `False` only means this
event supplies no such evidence; it does not certify file integrity, prove that no
independent writes occurred, or imply that initializer effects were rolled back.
New absence or workspace-effect evidence can update an existing outage notification.

Repeated failures within one unresolved outage are deduplicated by this manager.
Healthy checks produce no event unless they restore a previously observed outage.
Temporary inspection can discover a failure but does not announce recovery until a
usable managed handle has been published.
First creation does not report replacement; a cancelled or uncertain binding commit
does not report success before the verified handle is published. Explicit repeated
destruction produces one event after the first confirmed removal. Normal manager
close produces no user failure or destruction event. Warm events have
`owner_key=None` to distinguish them from user Sandbox events.

External changes are discovered by `get()`, `is_healthy()`, `get_details()`, or warm
maintenance. Notifications are local and best effort; they do not add remote user
Sandbox polling or guarantee durable or cross-process delivery. Consumers own any
persistent notification records.

Each observer has an independent ordered queue. The defaults are 128 pending events
plus one active callback and a one-second callback timeout. Configure these with
`OpenSandboxNotificationOptions(max_pending_events=128, timeout=1.0)` via
`notification_options`. A full queue drops the new event. Callback errors, timeouts,
and cancellation are isolated from Sandbox operations and other observers. Closing
drains accepted notifications concurrently across observers, potentially taking up
to `(max_pending_events + 1) * timeout` after resource settlement for cooperative
callbacks. The manager never closes borrowed observers.

Observers must use non-blocking asynchronous work and propagate cancellation. A
callback or a task it starts must not call this manager's resource operations,
readiness/start methods, or close; such calls raise `OpenSandboxObserverReentryError`.
Queue follow-up work to an independently owned host worker when it needs to operate
the manager. Blocking code and callbacks that suppress cancellation cannot be
forcibly stopped by the timeout.

## Cancellation safety

After creation, health checking, replacement, reset, destroy, or close begins, the manager retains cleanup responsibility even if the requesting task is cancelled. A caller receiving cancellation does not mean remote cleanup has finished.

Concurrent `OpenSandboxClient.destroy()` calls for the same Sandbox share one result.
Caller cancellation waits for destruction and local cleanup before propagating. Once
remote destruction succeeds, an SDK close failure does not reverse that result.
Client close waits for accepted work before closing its owned transport. Concurrent
close callers share the result; cancelling a waiter does not stop cleanup, and failed
close can be retried. A caller-supplied transport remains caller-owned.

## Inspect details

```python
details = await manager.get_details(key)
if details is not None:
    print(details.sandbox_id)
    print(details.available, details.healthy)
    print(details.owner_key, details.cached)
    print(details.access_state)
```

`None` means no known binding. When `available=False`, `unavailable_reason` is `not_found` or `unreachable`.
An available snapshot with `expires_at=None` identifies a manual-cleanup instance.
When details are unavailable, a null expiry is unknown.

`access_state` reports the framework's shared coordination phase independently of the
provider's `status.state`. A remote instance can still report `Running` while the
framework is `draining`, `pausing`, or awaiting confirmation. `None` means a coordination
snapshot was not supplied. During suspension, details use only the control plane;
`healthy=False` then means no data-plane probe was performed. A cached handle may remain
closed to work while its connection is being refreshed.

Next: [Rooted files and commands](rooted-filesystem.md).

# TinkerFin Contracts

`tinkerfin-contracts` provides protocol-neutral run identities and Runtime observation
interfaces so integrations can share execution data through one common contract.

## Installation

```bash
pip install tinkerfin-contracts
```

## Run identity

```python
from tinkerfin_contracts import RunIdentity, ThreadIdentity

identity = RunIdentity(namespace="company-a", thread_id="thread-1", run_id="run-1")
assert identity.thread == ThreadIdentity(namespace="company-a", thread_id="thread-1")
```

The namespace is required and limited to 128 Unicode characters; thread and run IDs
are limited to 1,024 characters each. All identifiers are non-empty, immutable,
case-sensitive UTF-8 strings without surrounding whitespace. Hosts choose namespace
ownership and enforce authorization. JSON uses flat `namespace`, `threadId`, and
`runId` fields; the derived `thread` property is not serialized.

## Runtime observers

`RuntimeObserver.open_run()` creates one request-scoped
`RunObservationSession`. The session receives validated run and Native observation
models, owns any work it starts, exposes asynchronous failure notification, and closes
idempotently at the Runtime boundary.

`RunSourceContext` carries the canonical identity, ordinary/branch/resume/abandon input
kind, parent lineage, Runtime mode, finite input/config snapshots, public resume
summaries, and Runtime-owned private state keys.

The observation union contains:

- Run start, input, resume-checkpoint, Observer-failure, terminal, and close values;
- validated Native message, task, root/subgraph state, interrupt, and declared extra-mode
  values;
- stable full graph namespace, IDs, UTC time, and monotonic clock evidence.

`identity.namespace` identifies the application-defined isolation scope.
`graph_namespace` identifies a position within the execution graph; `()` is the root.
Observation JSON uses `graphNamespace` for that position.

Runtime observers receive validated models rather than captured Python `repr`.
JSON fields reject non-finite numbers during Python and JSON validation. Model fields
cannot be reassigned, but nested dictionaries and lists remain mutable. Treat received
evidence as read-only; the Runtime supplies an independent copy to each observer.
`Command(resume=...)` remains invocation input and is represented only through the
protocol-neutral Run source summary and checkpoint Observation.

## Document collections

`tinkerfin_contracts.storage.ConditionalStore` lets consumers share a collection
without depending on a provider's query models. `asearch_exact(namespace,
limit=100, offset=0)` returns independent `DocumentSnapshot` values containing
the key, finite JSON value and UTC creation/update times. Descendant namespaces
are excluded before pagination; pages sort by update time descending and then key.

`acompare_and_set(namespace, key, expected=observed, value=replacement)` compares
the complete JSON value and commits one document atomically across processes.
Object member order does not affect the condition; array order and scalar types
do. `expected=None` requires absence and `value=None` deletes. A conflict returns
`False` without changing data. Consumers retain their draft and reread before
retrying. Providers define validation and resource failures; these operations
use borrowed resources and do not transfer ownership to consumers.

## License

Apache License 2.0. See
[LICENSE](https://github.com/tinkerfin-ai/tinkerfin-harness/blob/main/LICENSE).

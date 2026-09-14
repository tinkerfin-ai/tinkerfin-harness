"""Distribution and import-boundary contracts for the messaging package."""

from __future__ import annotations

import inspect
import json
import subprocess
import sys
from dataclasses import FrozenInstanceError, fields
from importlib.metadata import PackageNotFoundError, distribution
from importlib.resources import files
from pathlib import Path
from typing import get_args, get_origin, get_type_hints

import pytest
from ag_ui.core import BaseEvent

import tinkerfin_messaging
import tinkerfin_messaging.backend as backend_module
import tinkerfin_messaging.backend_contract as backend_contract
import tinkerfin_messaging.messaging as messaging_module
import tinkerfin_messaging.redis as redis_module
from tinkerfin_contracts import RunIdentity


@pytest.mark.parametrize(
    ("method_name", "documents_args"),
    [
        ("prepare_messaging_storage", False),
        ("commit_messaging_transition", True),
        ("load_messaging_state", True),
        ("read_committed_messages", True),
        ("wait_for_messaging_change", True),
        ("purge_stream_generation", True),
    ],
)
def test_backend_protocol_documents_storage_extension_contracts(
    method_name: str,
    documents_args: bool,
) -> None:
    """Keep atomicity, results, failures, and ownership next to each operation."""

    method = getattr(backend_contract.MessagingBackend, method_name)
    documentation = inspect.getdoc(method)

    assert documentation is not None
    assert ("Args:" in documentation) is documents_args
    assert "Returns:" in documentation
    assert "Raises:" in documentation


def test_backend_protocol_exposes_exactly_six_descriptive_operations() -> None:
    operations = {
        name
        for name, value in inspect.getmembers(backend_contract.MessagingBackend)
        if inspect.isfunction(value) and not name.startswith("_")
    }

    assert operations == {
        "commit_messaging_transition",
        "load_messaging_state",
        "prepare_messaging_storage",
        "purge_stream_generation",
        "read_committed_messages",
        "wait_for_messaging_change",
    }
    settings_doc = inspect.getdoc(
        inspect.getattr_static(
            backend_contract.MessagingBackend,
            "messaging_settings",
        )
    )
    assert settings_doc is not None
    assert "Returns:" in settings_doc
    assert "Raises:" in settings_doc


def test_removed_backend_handle_shapes_are_not_importable() -> None:
    assert not hasattr(backend_module, "BackendRunHandle")
    assert not hasattr(backend_module, "PreparedRun")
    assert not hasattr(messaging_module, "PreparedRun")
    assert not hasattr(redis_module, "BackendRunHandle")


@pytest.mark.parametrize(
    "type_name",
    [
        "CommittedMessagePage",
        "CommittedMessageQuery",
        "MessagingBackendSettings",
        "MessagingChangeCursor",
        "MessagingChangeWait",
        "MessagingRunReference",
        "MessagingStateQuery",
        "MessagingStateSnapshot",
        "MessagingStorageEffect",
        "MessagingTransition",
        "MessagingTransitionResult",
        "StoredMessageEvidence",
        "StoredMessagingChannel",
        "StoredMessagingRun",
        "StoredMessagingStream",
        "StreamGenerationPurge",
        "StreamGenerationPurgeResult",
    ],
)
def test_backend_extension_values_document_every_attribute(type_name: str) -> None:
    extension_type = getattr(backend_contract, type_name)
    documentation = inspect.getdoc(extension_type)

    assert documentation is not None
    assert "Attributes:" in documentation
    for value_field in fields(extension_type):
        assert f"{value_field.name}:" in documentation


@pytest.mark.parametrize(
    ("method_name", "required_terms"),
    [
        ("prepare_messaging_storage", ("idempotent", "cancellation", "host-owned")),
        ("commit_messaging_transition", ("atomically", "Cancellation", "idempotency")),
        ("load_messaging_state", ("observed_at", "logical read point", "tombstone")),
        ("read_committed_messages", ("stop_at_run_terminal", "limit", "prefetch")),
        ("wait_for_messaging_change", ("cancellation", "pinned connection", "cursor")),
        ("purge_stream_generation", ("bounded", "cleanup token", "Cancellation")),
    ],
)
def test_backend_operations_document_non_obvious_extension_requirements(
    method_name: str,
    required_terms: tuple[str, ...],
) -> None:
    documentation = inspect.getdoc(
        getattr(backend_contract.MessagingBackend, method_name)
    )

    assert documentation is not None
    for term in required_terms:
        assert term in documentation


@pytest.mark.parametrize(
    "function_name",
    ["resolve_messaging_transition"],
)
def test_backend_extension_functions_document_complete_contracts(
    function_name: str,
) -> None:
    documentation = inspect.getdoc(getattr(backend_contract, function_name))

    assert documentation is not None
    assert "Args:" in documentation
    assert "Returns:" in documentation
    assert "Raises:" in documentation


def test_public_namespace_exposes_the_default_tinkerfin_facade() -> None:
    expected = {
        "AgUiCodec",
        "ActiveRunStatus",
        "BackendOwnershipLost",
        "CancelCallback",
        "CancelContext",
        "CancellableMessageSource",
        "CancellationUnsupported",
        "CodecMismatch",
        "CommittedCallback",
        "DecodedMessage",
        "DeferredMessageSource",
        "FailedRunStatus",
        "FinalRunStatus",
        "FiniteMessageSource",
        "InvalidCursor",
        "MemoryBackend",
        "MessageChannel",
        "MessageCodec",
        "MessageCodecInputSource",
        "MessageEnvelope",
        "MessageIdConflict",
        "MessagePublicationPolicy",
        "MessageSource",
        "MessageSourceBinding",
        "MessageSubscription",
        "Messaging",
        "MessagingBackendError",
        "MessagingBackendProtocolError",
        "MessagingBackendTimeout",
        "MessagingBackendUnavailable",
        "MessagingClosed",
        "MessagingError",
        "MessagingErrorCode",
        "MessagingLimits",
        "MessagingNotStarted",
        "MessagingQuotaExceeded",
        "MessagingRetentionPolicy",
        "MessagingSettlementTimeout",
        "NativeStreamPart",
        "NativeStreamPartCodec",
        "ProfiledDeferredMessageSource",
        "ProfiledMessageSource",
        "PublicationRejected",
        "RecoverableMessage",
        "RecoverableSource",
        "RecoveryCheckpoint",
        "RecoveryUnsupported",
        "RedisBackend",
        "RunAlreadyActive",
        "RunNotFound",
        "RunProducerFailed",
        "RunStatus",
        "SqlAlchemyBackend",
        "SourceProfileMismatch",
        "SseRenderer",
        "SseRenderingUnsupported",
        "StreamDeleteConflict",
        "StreamDeleted",
        "StreamExpired",
        "UnexpectedMessagingBackendError",
        "create_agui_run_source",
        "is_active_run_status",
        "is_failed_run_status",
        "is_final_run_status",
        "map_source",
        "parse_sse_event_id",
    }

    assert set(tinkerfin_messaging.__all__) == expected
    assert all(hasattr(tinkerfin_messaging, name) for name in expected)


def test_backend_extension_contract_is_exposed_only_from_its_module() -> None:
    extension_names = set(backend_contract.__all__)

    assert "MessagingBackend" in extension_names
    assert "resolve_messaging_transition" in extension_names
    assert extension_names.isdisjoint(tinkerfin_messaging.__all__)
    assert all(not hasattr(tinkerfin_messaging, name) for name in extension_names)
    assert all(hasattr(backend_contract, name) for name in extension_names)


def test_agui_run_source_returns_the_public_profiled_source_contract() -> None:
    from tinkerfin_messaging.agui import create_agui_run_source

    return_type = get_type_hints(create_agui_run_source)["return"]

    assert get_origin(return_type) is tinkerfin_messaging.ProfiledMessageSource


def test_agui_run_source_requires_a_profiled_event_source() -> None:
    from tinkerfin_messaging.agui import create_agui_run_source

    source_type = get_type_hints(create_agui_run_source)["source"]
    assert get_origin(source_type) is tinkerfin_messaging.ProfiledMessageSource
    assert get_args(source_type) == (BaseEvent, BaseEvent)


def test_cancel_context_is_an_immutable_public_value() -> None:
    context = tinkerfin_messaging.CancelContext(
        channel="events",
        identity=RunIdentity(
            namespace="test", thread_id="conversation-1", run_id="run-1"
        ),
    )

    assert (context.channel, context.identity) == (
        "events",
        RunIdentity(namespace="test", thread_id="conversation-1", run_id="run-1"),
    )
    with pytest.raises(FrozenInstanceError):
        context.__setattr__(
            "identity",
            RunIdentity(
                namespace="test", thread_id="conversation-1", run_id="replacement"
            ),
        )


def test_message_subscription_requires_a_channel_factory() -> None:
    with pytest.raises(TypeError, match=r"MessageChannel\.wrap\(\) or follow\(\)"):
        tinkerfin_messaging.MessageSubscription()


def test_base_import_does_not_load_optional_integrations() -> None:
    repository_root = Path(__file__).resolve().parents[3]
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import json, sys, tinkerfin_messaging; "
                "print(json.dumps(sorted(name for name in sys.modules "
                "if any(name == prefix or name.startswith(prefix + '.') "
                "for prefix in ('ag_ui', 'deepagents', 'langchain', 'langgraph', "
                "'redis', 'tinkerfin', 'tinkerfin_native_stream')))))"
            ),
        ],
        cwd=repository_root,
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(result.stdout) == []


def test_distribution_declares_only_protocol_neutral_core_dependencies() -> None:
    try:
        metadata = distribution("tinkerfin-messaging")
    except PackageNotFoundError:
        pytest.fail("tinkerfin-messaging distribution is not installed")

    requirements = set(metadata.requires or ())
    assert "pydantic<3,>=2" in requirements
    assert "tinkerfin-contracts==0.1.0" in requirements
    assert not any(
        "extra ==" not in value and "ag-ui-protocol" in value for value in requirements
    )
    assert not any(
        "extra ==" not in value and value.startswith("tinkerfin<")
        for value in requirements
    )
    assert files("tinkerfin_messaging").joinpath("py.typed").is_file()


def test_distribution_declares_redis_agui_and_native_extras() -> None:
    requirements = set(distribution("tinkerfin-messaging").requires or ())

    assert 'redis<9,>=6; extra == "redis"' in requirements
    assert 'ag-ui-protocol==0.1.19; extra == "agui"' in requirements
    assert 'tinkerfin-native-stream==0.1.0; extra == "native"' in requirements
    assert not any(
        'extra == "native"' in value and value.startswith("tinkerfin<")
        for value in requirements
    )


@pytest.mark.parametrize(
    ("symbol", "blocked_packages", "extra"),
    (
        ("AgUiCodec", ("ag_ui",), "agui"),
        ("create_agui_run_source", ("ag_ui",), "agui"),
        ("NativeStreamPartCodec", ("tinkerfin_native_stream",), "native"),
        ("RedisBackend", ("redis",), "redis"),
        ("SqlAlchemyBackend", ("sqlalchemy",), "sqlalchemy"),
        ("SqlAlchemyBackend", ("tinkerfin_sqlalchemy",), "sqlalchemy"),
    ),
)
def test_missing_optional_dependency_reports_the_install_command(
    symbol: str,
    blocked_packages: tuple[str, ...],
    extra: str,
) -> None:
    repository_root = Path(__file__).resolve().parents[3]
    script = f"""
import importlib.abc
import sys

blocked = {blocked_packages!r}

class Blocker(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if any(fullname == name or fullname.startswith(name + '.') for name in blocked):
            error = ModuleNotFoundError(f'blocked optional dependency: {{fullname}}')
            error.name = fullname
            raise error
        return None

sys.meta_path.insert(0, Blocker())
import tinkerfin_messaging

try:
    getattr(tinkerfin_messaging, {symbol!r})
except ImportError as error:
    print(str(error))
else:
    raise SystemExit('optional import unexpectedly succeeded')
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=repository_root,
        check=True,
        capture_output=True,
        text=True,
    )

    assert f'pip install "tinkerfin-messaging[{extra}]"' in result.stdout


def test_core_import_does_not_load_or_depend_on_messaging() -> None:
    repository_root = Path(__file__).resolve().parents[3]
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import json, sys, tinkerfin; "
                "print(json.dumps('tinkerfin_messaging' in sys.modules))"
            ),
        ],
        cwd=repository_root,
        check=True,
        capture_output=True,
        text=True,
    )

    assert result.stdout.strip() == "false"
    core_requirements = distribution("tinkerfin").requires or ()
    assert not any(
        requirement.startswith("tinkerfin-messaging")
        for requirement in core_requirements
    )

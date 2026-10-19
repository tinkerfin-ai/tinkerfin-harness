"""Providers remain independently selectable through the public package."""

import subprocess
import sys

import pytest

from tinkerfin_sandbox import (
    LocalError,
    LocalErrorCode,
    OpenSandboxError,
    SandboxError,
)


@pytest.mark.parametrize(
    ("statement", "absent"),
    [
        ("import tinkerfin_sandbox", ("tinkerfin_sandbox._native", "opensandbox")),
        ("from tinkerfin_sandbox import LocalWorkspace", ("opensandbox",)),
        (
            "from tinkerfin_sandbox import OpenSandboxManager",
            ("tinkerfin_sandbox._native",),
        ),
    ],
)
def test_provider_imports_do_not_load_another_provider(
    statement: str, absent: tuple[str, ...]
) -> None:
    source = (
        f"import sys\n{statement}\n"
        f"assert not any(name in sys.modules for name in {absent!r})\n"
    )
    subprocess.run([sys.executable, "-B", "-c", source], check=True)


def test_shared_failure_family_retains_safe_context_and_private_cause() -> None:
    cause = OSError("private path")
    context = {"attempts": 1}
    error = LocalError(
        LocalErrorCode.CLEANUP, "Cleanup failed", context=context, cause=cause
    )
    context["attempts"] = 2
    assert isinstance(error, SandboxError)
    assert isinstance(OpenSandboxError("Unavailable"), SandboxError)
    assert error.code == "sandbox.local.cleanup"
    assert error.context == {"attempts": 1}
    assert error.cause is cause and error.__cause__ is cause

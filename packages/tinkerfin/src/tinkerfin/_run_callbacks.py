"""Identify close requests made inside a still-active run callback."""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar, Token


class _CallbackCall:
    """Expire callback ancestry when the original invocation returns."""

    def __init__(self, owner: object | None) -> None:
        self.owner = owner
        self.active = True


_RUN_OWNER: ContextVar[object | None] = ContextVar("tinkerfin_run_owner", default=None)
_CALLBACK_CALL: ContextVar[_CallbackCall | None] = ContextVar(
    "tinkerfin_run_callback", default=None
)


def bind_run(owner: object) -> Token[object | None]:
    """Associate callbacks in this asynchronous context with their owning run."""
    return _RUN_OWNER.set(owner)


def reset_run(token: Token[object | None]) -> None:
    """Restore the enclosing run after an operation returns."""
    _RUN_OWNER.reset(token)


def in_run_callback(owner: object) -> bool:
    """Identify only callbacks that are still being awaited by this run."""
    call = _CALLBACK_CALL.get()
    return call is not None and call.active and call.owner is owner


@contextmanager
def callback_scope() -> Generator[None, None, None]:
    """Keep immediate callback descendants distinct from delayed external closers.

    Child tasks inherit the same call record. Its active flag expires when the
    actual callback returns, so a delayed child cannot leave a close request with
    no operation left to settle it.
    """

    call = _CallbackCall(_RUN_OWNER.get())
    token = _CALLBACK_CALL.set(call)
    try:
        yield
    finally:
        call.active = False
        _CALLBACK_CALL.reset(token)

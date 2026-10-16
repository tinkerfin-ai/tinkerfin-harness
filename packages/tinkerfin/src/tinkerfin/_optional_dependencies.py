"""Precise lazy dependency gates for optional Runtime integrations."""

from __future__ import annotations

import importlib

_AGUI_INSTALL_COMMAND = 'pip install "tinkerfin[agui]"'


def require_agui() -> None:
    """Require the complete AG-UI extra without affecting Core imports.

    Raises:
        ModuleNotFoundError: AG-UI Protocol, the Adapter, or one of their declared
            dependencies is unavailable.
    """

    try:
        importlib.import_module("ag_ui.core")
        importlib.import_module("tinkerfin_agui_adapter")
    except ModuleNotFoundError as error:
        missing = error.name or "unknown"
        translated = ModuleNotFoundError(
            "TinkerFin AG-UI support is not installed; run "
            f"{_AGUI_INSTALL_COMMAND} (missing module: {missing})",
            name=missing,
        )
        raise translated from error


def require_tracing() -> None:
    """Require the optional recorded-history integration without owning its Store."""
    try:
        importlib.import_module("tinkerfin_tracing")
    except ModuleNotFoundError as error:
        missing = error.name or "unknown"
        raise ModuleNotFoundError(
            'TinkerFin history support is not installed; run pip install "tinkerfin[agui,tracing]" '
            f"(missing module: {missing})",
            name=missing,
        ) from error


__all__ = ["require_agui", "require_tracing"]

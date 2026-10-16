"""Public Plan Mode failures."""

from ..errors import TinkerFinError, TinkerFinErrorCode


class PlanModeConfigurationError(TinkerFinError, ValueError):
    """Plan Mode cannot be built from the supplied Deep Agent configuration."""

    code = TinkerFinErrorCode.PLAN_MODE_CONFIGURATION


class PlanStructuredOutputError(TinkerFinError, RuntimeError):
    """A model result cannot satisfy the configured Plan response contract."""

    code = TinkerFinErrorCode.PLAN_STRUCTURED_OUTPUT


class PlanClarificationResponseError(TinkerFinError, ValueError):
    """A clarification response cannot resolve its checkpointed question form."""

    code = TinkerFinErrorCode.PLAN_CLARIFICATION_RESPONSE_INVALID


class PlanStateConflictError(TinkerFinError, RuntimeError):
    """Planning and native checkpoints cannot identify one safe next action."""

    code = TinkerFinErrorCode.PLAN_STATE_CONFLICT


__all__ = [
    "PlanClarificationResponseError",
    "PlanModeConfigurationError",
    "PlanStateConflictError",
    "PlanStructuredOutputError",
]

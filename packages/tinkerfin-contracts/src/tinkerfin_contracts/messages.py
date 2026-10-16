"""Message provenance independent of model roles and transport protocols."""

from typing import Literal

from pydantic import Field

from ._json import FiniteJsonValue
from ._models import ContractModel


class MessageSource(ContractModel):
    """Distinguish a person's request from host-provided conversation context.

    On a user-role message, an absent source identifies a person's request.
    Context may use the same role, but must not become a new request for planning
    or Turn association.
    ``name`` identifies the producer; ``metadata`` contains host-authorized public
    provenance, never credentials. Hosts own the content and its authorization.
    Native LangChain messages retain this value in ``additional_kwargs`` under
    ``tinkerfin_source``; AG-UI and Trace messages expose the ``source`` field.
    """

    kind: Literal["user", "context"]
    name: str | None = Field(default=None, min_length=1, max_length=128)
    metadata: dict[str, FiniteJsonValue] = Field(default_factory=dict)


__all__ = ["MessageSource"]

"""正常联网与受限联网只有一个当前输入契约"""

import pytest
from pydantic import ValidationError

from tinkerfin_desktop.configuration import WorkspacePermissions


def test_network_policy_is_explicit_in_serialized_configuration():
    normal = WorkspacePermissions(network="normal")
    assert normal.allowed_domains == ()
    assert WorkspacePermissions.model_validate_json(normal.model_dump_json()) == normal
    assert WorkspacePermissions(
        network="restricted", allowed_domains=("example.com",)
    ).allowed_domains == ("example.com",)
    for value in (
        {"allowed_domains": []},
        {"network": "normal", "allowed_domains": ["example.com"]},
        {"network": "restricted", "allowed_domains": ["https://example.com"]},
        {"network": "restricted", "allowed_domains": ["127.0.0.1"]},
        {"network": "other", "allowed_domains": []},
    ):
        with pytest.raises(ValidationError):
            WorkspacePermissions.model_validate(value)

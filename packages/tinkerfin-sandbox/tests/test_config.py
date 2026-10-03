"""Default Sandbox runtime configuration contracts."""

import pytest
from opensandbox.models.sandboxes import PVC, Volume
from pydantic import ValidationError

from tinkerfin_sandbox.models import OpenSandboxConfig


def test_default_sandbox_uses_immutable_standard_runtime() -> None:
    config = OpenSandboxConfig()

    assert config.image == (
        "ghcr.io/tinkerfin-ai/sandbox-runtime@"
        "sha256:fd62b46229630554e8cd21071cdddbc7d79c490109fe0a78580bba7d07942584"
    )
    assert config.entrypoint == ["/opt/sandbox-runtime/bin/entrypoint.sh"]
    assert config.env == {}
    assert config.workspace_root == "/workspace"


def test_workspace_root_normalizes_absolute_posix_path() -> None:
    config = OpenSandboxConfig(workspace_root="/workspace/./projects/")

    assert config.workspace_root == "/workspace/projects"


@pytest.mark.parametrize(
    "workspace_root",
    [
        "workspace",
        "/",
        "//workspace",
        "/workspace/../etc",
        "/workspace\x00private",
    ],
)
def test_workspace_root_rejects_unsafe_boundaries(workspace_root: str) -> None:
    with pytest.raises(ValidationError):
        OpenSandboxConfig(workspace_root=workspace_root)


def test_config_accepts_pvc_volume_mounts_without_sharing_mutable_input() -> None:
    volume = Volume(
        name="workspace-data",
        pvc=PVC(
            claimName="tinkerfin-harnesss",
            createIfNotExists=False,
        ),
        mountPath="/workspace/data",
    )

    config = OpenSandboxConfig.model_validate({"volumes": [volume]})
    volume.mount_path = "/mutated"

    assert isinstance(config.volumes, tuple)
    assert config.volumes[0].mount_path == "/workspace/data"
    assert config.volumes[0].pvc is not None
    assert config.volumes[0].pvc.claim_name == "tinkerfin-harnesss"


def test_config_rejects_framework_reserved_metadata_keys() -> None:
    with pytest.raises(ValidationError, match="tinkerfin.ai/"):
        OpenSandboxConfig(
            metadata={
                "team": "agents",
                "tinkerfin.ai/owner": "forged-owner",
            }
        )


@pytest.mark.parametrize("field", ("command_timeout", "warm_pool_size"))
def test_integer_capacity_and_timeout_fields_reject_booleans(field: str) -> None:
    with pytest.raises(ValidationError):
        OpenSandboxConfig.model_validate({field: True})

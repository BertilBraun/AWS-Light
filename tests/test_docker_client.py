from __future__ import annotations

from aws_light.compute.docker_client import _format_volume_bindings


def test_format_volume_bindings_expands_mount_paths_for_docker_sdk() -> None:
    assert _format_volume_bindings(
        {"aws-light-db-app-db-data": "/var/lib/postgresql/data"}
    ) == {
        "aws-light-db-app-db-data": {
            "bind": "/var/lib/postgresql/data",
            "mode": "rw",
        }
    }


def test_format_volume_bindings_preserves_missing_volumes() -> None:
    assert _format_volume_bindings(None) is None

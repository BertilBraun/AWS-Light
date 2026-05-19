from pathlib import Path

import yaml

from aws_light.config import Settings


ROOT = Path(__file__).resolve().parents[1]


def test_settings_do_not_expose_legacy_shared_workload_network() -> None:
    assert "docker_network" not in Settings.model_fields


def test_compose_does_not_require_legacy_shared_workload_network() -> None:
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())

    assert "data" not in compose["networks"]
    assert compose["services"]["proxy"]["networks"] == ["internal"]
    assert compose["services"]["health-checker"]["networks"] == ["internal"]

from __future__ import annotations

import importlib.util
from pathlib import Path

from aws_light.iac.parser import parse_manifests
from aws_light.models.manifest import ServiceManifest

ROOT = Path(__file__).parents[1]


def _load_module(module_path: Path, name: str):  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location(name, module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_internal_call_manifest_declares_target_owned_policy() -> None:
    manifests = parse_manifests((ROOT / "examples" / "internal-call.yaml").read_text())
    services = {
        manifest.metadata.name: manifest
        for manifest in manifests
        if isinstance(manifest, ServiceManifest)
    }

    assert set(services) == {"internal-backend", "internal-frontend"}
    backend = services["internal-backend"]
    frontend = services["internal-frontend"]

    assert backend.spec.image == "aws-light/internal-backend:latest"
    assert backend.spec.ingress.external is False
    assert backend.spec.ingress.internal.enabled is True
    assert backend.spec.ingress.internal.allow_from == ["internal-frontend"]
    assert frontend.spec.image == "aws-light/internal-frontend:latest"
    assert frontend.spec.ingress.external is True
    assert frontend.spec.ingress.internal.enabled is False


def test_internal_frontend_builds_backend_proxy_request(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    frontend = _load_module(
        ROOT / "examples" / "internal-frontend" / "main.py",
        "internal_frontend_example",
    )
    monkeypatch.setenv("AWS_LIGHT_PROXY_URL", "http://proxy:8080")
    monkeypatch.setenv("AWS_LIGHT_SERVICE_TOKEN", "frontend-token")

    assert frontend.backend_url("/message") == "http://proxy:8080/message"
    assert frontend.backend_headers() == {
        "Host": "internal-backend.localhost",
        "X-AWS-Light-Service-Token": "frontend-token",
    }

from __future__ import annotations

import importlib.util
from pathlib import Path

from aws_light.iac.parser import parse_manifests
from aws_light.models.manifest import BucketManifest, DatabaseManifest, ServiceManifest

ROOT = Path(__file__).parents[1]


def _load_combined_service():  # type: ignore[no-untyped-def]
    module_path = ROOT / "examples" / "combined-service" / "main.py"
    spec = importlib.util.spec_from_file_location("combined_service_example", module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_combined_stack_manifest_uses_all_platform_features() -> None:
    manifests = parse_manifests((ROOT / "examples" / "combined-stack.yaml").read_text())
    buckets = [manifest for manifest in manifests if isinstance(manifest, BucketManifest)]
    databases = [manifest for manifest in manifests if isinstance(manifest, DatabaseManifest)]
    services = {
        manifest.metadata.name: manifest
        for manifest in manifests
        if isinstance(manifest, ServiceManifest)
    }

    assert [bucket.metadata.name for bucket in buckets] == ["combined-objects"]
    assert [database.metadata.name for database in databases] == ["combined-db"]
    assert set(services) == {"combined-service", "cpu-service", "flaky-service"}

    combined = services["combined-service"]
    assert combined.spec.ingress.external is True
    assert combined.spec.resources.buckets[0].name == "combined-objects"
    assert combined.spec.resources.buckets[0].access == ["read", "write"]
    assert combined.spec.resources.databases[0].name == "combined-db"
    assert combined.spec.resources.databases[0].access == ["connect"]
    assert combined.spec.secret_refs == ["combined-api-token"]

    cpu = services["cpu-service"]
    assert cpu.spec.replicas == 3
    assert cpu.spec.ingress.external is False
    assert cpu.spec.ingress.internal.enabled is True
    assert cpu.spec.ingress.internal.allow_from == ["combined-service"]

    flaky = services["flaky-service"]
    assert flaky.spec.ingress.external is False
    assert flaky.spec.ingress.internal.enabled is True
    assert flaky.spec.ingress.internal.allow_from == ["combined-service"]
    assert flaky.spec.env["REQUEST_FAILURE_RATE"] == "0.35"


def test_combined_service_builds_internal_proxy_requests(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    combined = _load_combined_service()
    monkeypatch.setenv("AWS_LIGHT_PROXY_URL", "http://proxy:8080")
    monkeypatch.setenv("AWS_LIGHT_SERVICE_TOKEN", "combined-token")

    assert combined.service_url("cpu-service", "/?work_ms=25") == "http://proxy:8080/?work_ms=25"
    assert combined.service_headers("flaky-service") == {
        "Host": "flaky-service.localhost",
        "X-AWS-Light-Service-Token": "combined-token",
    }


def test_combined_service_requires_demo_token(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    combined = _load_combined_service()
    monkeypatch.setenv("COMBINED_API_TOKEN", "demo-secret")

    assert combined.authorized("demo-secret") is True
    assert combined.authorized("wrong") is False

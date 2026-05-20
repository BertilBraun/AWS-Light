from __future__ import annotations

import asyncio
import subprocess
import time
from collections.abc import Callable

import httpx
import pytest

from tests.integration.conftest import PROXY_URL, ROOT

pytestmark = pytest.mark.integration


def _wait_for_service(
    client: httpx.Client,
    headers: dict[str, str],
    name: str,
    *,
    replicas: int,
    absent_container_id: str = "",
    timeout: int = 90,
) -> dict:
    deadline = time.monotonic() + timeout
    last_payload: dict = {}
    while time.monotonic() < deadline:
        try:
            response = client.get(f"/api/v1/services/{name}", headers=headers)
        except httpx.HTTPError:
            time.sleep(1)
            continue
        if response.status_code == 200:
            last_payload = response.json()
            running = [
                replica
                for replica in last_payload.get("replicas", [])
                if replica.get("status") == "running"
            ]
            running_ids = {replica.get("container_id") for replica in running}
            if len(running) >= replicas and absent_container_id not in running_ids:
                return last_payload
        time.sleep(2)
    raise AssertionError(f"{name} did not reach {replicas} running replicas: {last_payload}")


def _wait_for_proxy(
    host: str,
    timeout: int = 60,
    *,
    path: str = "/",
    expected_status: int = 200,
) -> httpx.Response:
    deadline = time.monotonic() + timeout
    last_response: httpx.Response | None = None
    while time.monotonic() < deadline:
        try:
            response = httpx.get(
                f"{PROXY_URL}{path}", headers={"Host": host}, timeout=10
            )
            last_response = response
            if response.status_code == expected_status:
                return response
        except httpx.HTTPError:
            pass
        time.sleep(1)
    detail = last_response.text if last_response is not None else "no response"
    raise AssertionError(f"proxy did not serve host {host} with {expected_status}: {detail}")


def _wait_until(description: str, predicate: Callable[[], bool], timeout: int = 90) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(2)
    raise AssertionError(f"Timed out waiting for {description}")


def _apply_manifest(client: httpx.Client, headers: dict[str, str], relative_path: str) -> None:
    manifest = (ROOT / relative_path).read_text()
    response = client.post(
        "/api/v1/manifests/apply",
        json={"yaml_text": manifest},
        headers=headers,
    )
    response.raise_for_status()


async def _send_proxy_load(
    host: str,
    path: str,
    *,
    requests: int = 600,
    concurrency: int = 100,
) -> None:
    semaphore = asyncio.Semaphore(concurrency)
    limits = httpx.Limits(
        max_connections=concurrency,
        max_keepalive_connections=concurrency,
    )

    async def request_once(client: httpx.AsyncClient) -> None:
        async with semaphore:
            try:
                await client.get(
                    f"{PROXY_URL}{path}",
                    headers={"Host": host},
                )
            except httpx.HTTPError:
                return

    async with httpx.AsyncClient(timeout=30, limits=limits) as client:
        await asyncio.gather(*(request_once(client) for _ in range(requests)))


def test_core_flow_proxy_health_and_reconcile(
    api_client: httpx.Client,
    admin_headers: dict[str, str],
) -> None:
    _apply_manifest(api_client, admin_headers, "examples/secret-service.yaml")

    service = _wait_for_service(api_client, admin_headers, "secret-service", replicas=1)
    _wait_for_proxy("secret-service.localhost")

    first_replica = service["replicas"][0]
    subprocess.run(
        ["docker", "rm", "-f", first_replica["container_id"]],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )

    recovered = _wait_for_service(
        api_client,
        admin_headers,
        "secret-service",
        replicas=1,
        absent_container_id=first_replica["container_id"],
    )
    recovered_ids = {replica["container_id"] for replica in recovered["replicas"]}
    assert first_replica["container_id"] not in recovered_ids
    _wait_for_proxy("secret-service.localhost")


def test_storage_and_presigned_url_flow(
    api_client: httpx.Client,
    admin_headers: dict[str, str],
) -> None:
    bucket_name = f"it-bucket-{int(time.time())}"
    create_response = api_client.post(
        "/api/v1/storage/buckets",
        json={"name": bucket_name},
        headers=admin_headers,
    )
    create_response.raise_for_status()

    put_response = api_client.put(
        f"/api/v1/storage/buckets/{bucket_name}/objects/hello.txt",
        content=b"integration object",
        headers={**admin_headers, "content-type": "text/plain"},
    )
    put_response.raise_for_status()

    presign_response = api_client.post(
        f"/api/v1/storage/buckets/{bucket_name}/objects/hello.txt/presign",
        json={"ttl_seconds": 60},
        headers=admin_headers,
    )
    presign_response.raise_for_status()

    object_response = httpx.get(presign_response.json()["url"], timeout=10)
    assert object_response.status_code == 200
    assert object_response.content == b"integration object"


def test_security_boundaries(api_client: httpx.Client, admin_headers: dict[str, str]) -> None:
    assert api_client.get("/api/v1/platform/services").status_code in {401, 403}
    assert api_client.get("/api/v1/services").status_code in {401, 403}

    create_viewer = api_client.post(
        "/api/v1/users",
        json={"username": "integration-viewer", "password": "pass", "role": "viewer"},
        headers=admin_headers,
    )
    if create_viewer.status_code not in {201, 409}:
        create_viewer.raise_for_status()

    login = api_client.post(
        "/api/v1/auth/login",
        json={"username": "integration-viewer", "password": "pass"},
    )
    login.raise_for_status()
    viewer_headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    assert api_client.get("/api/v1/platform/config", headers=viewer_headers).status_code == 200
    assert api_client.get("/api/v1/platform/services", headers=viewer_headers).status_code == 403
    assert (
        api_client.post(
            "/api/v1/storage/buckets",
            json={"name": "viewer-denied"},
            headers=viewer_headers,
        ).status_code
        == 403
    )


def test_combined_stack_exercises_resources_policy_and_topology(
    api_client: httpx.Client,
    admin_headers: dict[str, str],
) -> None:
    _apply_manifest(api_client, admin_headers, "examples/combined-stack.yaml")

    _wait_for_service(api_client, admin_headers, "combined-service", replicas=1)
    _wait_for_service(api_client, admin_headers, "cpu-service", replicas=3)
    _wait_for_service(api_client, admin_headers, "flaky-service", replicas=2)

    response = _wait_for_proxy(
        "combined-service.localhost",
        path="/?demo_token=demo-token",
        expected_status=200,
        timeout=120,
    )
    payload = response.json()
    assert payload["service"] == "combined-service"
    assert payload["storage"]["bucket"] == "combined-objects"
    assert payload["database"]["database"] == "combined_db"
    assert len(payload["cpu"]) == 3
    assert {result["status"] for result in payload["cpu"]} == {200}
    assert payload["flaky"]["status"] in {200, 500}

    denied = httpx.get(
        PROXY_URL,
        headers={"Host": "cpu-service.localhost"},
        timeout=10,
    )
    assert denied.status_code == 403
    assert denied.json()["error"] == "external ingress denied"

    topology = api_client.get(
        "/api/v1/platform/topology", headers=admin_headers
    )
    topology.raise_for_status()
    graph = topology.json()
    node_ids = {node["id"] for node in graph["nodes"]}
    edge_pairs = {
        (edge["source"], edge["target"], edge["label"]) for edge in graph["edges"]
    }
    assert "service:combined-service" in node_ids
    assert "bucket:combined-objects" in node_ids
    assert "database:combined-db" in node_ids
    assert (
        "service:combined-service",
        "bucket:combined-objects",
        "binds bucket",
    ) in edge_pairs
    assert (
        "service:combined-service",
        "database:combined-db",
        "binds database",
    ) in edge_pairs
    assert (
        "service:combined-service",
        "service:cpu-service",
        "allowed internal ingress",
    ) in edge_pairs


def test_internal_call_example_enforces_ingress_policy(
    api_client: httpx.Client,
    admin_headers: dict[str, str],
) -> None:
    _apply_manifest(api_client, admin_headers, "examples/internal-call.yaml")

    _wait_for_service(api_client, admin_headers, "internal-backend", replicas=1)
    _wait_for_service(api_client, admin_headers, "internal-frontend", replicas=1)

    frontend = _wait_for_proxy("internal-frontend.localhost", expected_status=200)
    payload = frontend.json()
    assert payload["service"] == "internal-frontend"
    assert payload["backend_status"] == 200
    assert payload["backend"]["service"] == "internal-backend"

    backend = httpx.get(
        PROXY_URL,
        headers={"Host": "internal-backend.localhost"},
        timeout=10,
    )
    assert backend.status_code == 403
    assert backend.json()["error"] == "external ingress denied"


def test_autoscaler_scales_cpu_service_and_orchestrator_reconciles(
    api_client: httpx.Client,
    admin_headers: dict[str, str],
) -> None:
    manifest = """
apiVersion: aws-light/v1
kind: Service
metadata:
  name: it-cpu-service
spec:
  image: aws-light/cpu-service:latest
  replicas: 1
  minReplicas: 1
  maxReplicas: 3
  cpuRequest: 0.1
  memoryRequestMb: 128
  port: 8000
  healthCheckPath: /health
  ingress:
    external: true
    internal: false
"""
    response = api_client.post(
        "/api/v1/manifests/apply",
        json={"yaml_text": manifest},
        headers=admin_headers,
    )
    response.raise_for_status()
    _wait_for_service(api_client, admin_headers, "it-cpu-service", replicas=1)
    _wait_for_proxy("it-cpu-service.localhost", path="/?work_ms=1")

    def scaled_and_reconciled() -> bool:
        asyncio.run(
            _send_proxy_load(
                "it-cpu-service.localhost",
                "/?work_ms=10",
                requests=600,
                concurrency=100,
            )
        )
        try:
            service_response = api_client.get(
                "/api/v1/services/it-cpu-service", headers=admin_headers
            )
            service_response.raise_for_status()
        except httpx.HTTPError:
            return False
        service = service_response.json()
        running = [
            replica
            for replica in service["replicas"]
            if replica["status"] == "running"
        ]
        return service["spec"]["replicas"] > 1 and len(running) > 1

    _wait_until(
        "autoscaler to increase replicas and orchestrator to create them",
        scaled_and_reconciled,
        timeout=150,
    )

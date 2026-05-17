from __future__ import annotations

import subprocess
import time

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
        response = client.get(f"/api/v1/services/{name}", headers=headers)
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


def _wait_for_proxy(host: str, timeout: int = 60) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            response = httpx.get(PROXY_URL, headers={"Host": host}, timeout=3)
            if response.status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(1)
    raise AssertionError(f"proxy did not serve host {host}")


def test_core_flow_proxy_health_and_reconcile(
    api_client: httpx.Client,
    admin_headers: dict[str, str],
) -> None:
    manifest = (ROOT / "examples" / "hello-service.yaml").read_text()
    apply_response = api_client.post(
        "/api/v1/manifests/apply",
        json={"yaml_text": manifest},
        headers=admin_headers,
    )
    apply_response.raise_for_status()

    service = _wait_for_service(api_client, admin_headers, "hello-service", replicas=2)
    _wait_for_proxy("hello-service.localhost")

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
        "hello-service",
        replicas=2,
        absent_container_id=first_replica["container_id"],
    )
    recovered_ids = {replica["container_id"] for replica in recovered["replicas"]}
    assert first_replica["container_id"] not in recovered_ids
    _wait_for_proxy("hello-service.localhost")


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

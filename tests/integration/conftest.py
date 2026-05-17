from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).parents[2]
API_URL = "http://localhost:8000"
PROXY_URL = "http://localhost:8080"


def _integration_enabled() -> bool:
    return os.environ.get("AWS_LIGHT_INTEGRATION") == "1"


def _run(command: list[str], timeout: int = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=ROOT,
        check=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=timeout,
    )


def _wait_for_api(timeout: int = 90) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            response = httpx.get(f"{API_URL}/healthz", timeout=2)
            if response.status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(1)
    raise AssertionError("control-plane did not become healthy")


@pytest.fixture(scope="session")
def compose_stack() -> Iterator[None]:
    if not _integration_enabled():
        pytest.skip("Set AWS_LIGHT_INTEGRATION=1 to run Docker Compose integration tests")

    _run(["docker", "build", "-t", "aws-light/hello-service:latest", "examples/hello-service"])
    _run(["docker", "compose", "up", "-d", "--build"], timeout=300)
    _wait_for_api()
    yield


@pytest.fixture()
def api_client(compose_stack: None) -> Iterator[httpx.Client]:
    with httpx.Client(base_url=API_URL, timeout=10) as client:
        yield client


@pytest.fixture()
def admin_headers(api_client: httpx.Client) -> dict[str, str]:
    response = api_client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "admin"},
    )
    response.raise_for_status()
    return {"Authorization": f"Bearer {response.json()['access_token']}"}

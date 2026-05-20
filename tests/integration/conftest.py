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


def _run_optional(command: list[str], timeout: int = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=ROOT,
        check=False,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=timeout,
    )


def _docker_names(command: list[str], prefixes: tuple[str, ...]) -> list[str]:
    result = _run_optional(command)
    if result.returncode != 0:
        return []
    return [
        name.strip()
        for name in result.stdout.splitlines()
        if name.strip().startswith(prefixes)
    ]


def _reset_dynamic_docker_resources() -> None:
    # The integration suite owns these locally generated resources while
    # AWS_LIGHT_INTEGRATION=1 is set. Database volumes live outside Compose.
    containers = _docker_names(
        ["docker", "container", "ls", "-a", "--format", "{{.Names}}"],
        ("aws-light-",),
    )
    if containers:
        _run_optional(["docker", "rm", "-f", *containers], timeout=180)

    networks = _docker_names(
        ["docker", "network", "ls", "--format", "{{.Name}}"],
        ("aws-light-svc-", "aws-light-db-"),
    )
    for network in networks:
        _run_optional(["docker", "network", "rm", network])

    volumes = [
        name
        for name in _docker_names(
            ["docker", "volume", "ls", "--format", "{{.Name}}"],
            ("aws-light-db-",),
        )
        if name.endswith("-data")
    ]
    for volume in volumes:
        _run_optional(["docker", "volume", "rm", "-f", volume])


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

    _run(["docker", "compose", "down", "-v", "--remove-orphans"], timeout=180)
    _reset_dynamic_docker_resources()
    for image_name, build_path in (
        ("aws-light/secret-service:latest", "examples/secret-service"),
        ("aws-light/cpu-service:latest", "examples/cpu-service"),
        ("aws-light/flaky-service:latest", "examples/flaky-service"),
        ("aws-light/combined-service:latest", "examples/combined-service"),
        ("aws-light/internal-backend:latest", "examples/internal-backend"),
        ("aws-light/internal-frontend:latest", "examples/internal-frontend"),
        ("aws-light/database-service:latest", "examples/database-service"),
    ):
        _run(["docker", "build", "-t", image_name, build_path])
    _run(["docker", "compose", "up", "-d", "--build"], timeout=300)
    _wait_for_api()
    try:
        yield
    finally:
        if os.environ.get("AWS_LIGHT_INTEGRATION_KEEP_STACK") != "1":
            _run(["docker", "compose", "down", "-v", "--remove-orphans"], timeout=180)
            _reset_dynamic_docker_resources()


@pytest.fixture()
def api_client(compose_stack: None) -> Iterator[httpx.Client]:
    limits = httpx.Limits(max_keepalive_connections=0)
    with httpx.Client(base_url=API_URL, timeout=10, limits=limits) as client:
        yield client


@pytest.fixture()
def admin_headers(api_client: httpx.Client) -> dict[str, str]:
    deadline = time.monotonic() + 30
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            response = api_client.post(
                "/api/v1/auth/login",
                json={"username": "admin", "password": "admin"},
            )
            response.raise_for_status()
            return {"Authorization": f"Bearer {response.json()['access_token']}"}
        except (httpx.HTTPError, httpx.HTTPStatusError) as error:
            last_error = error
            time.sleep(1)
    raise AssertionError(f"Could not log in to integration stack: {last_error}")

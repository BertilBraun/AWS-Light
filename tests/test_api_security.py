from __future__ import annotations

from fastapi.testclient import TestClient


def _get_token(client: TestClient, username: str = "admin", password: str = "admin") -> str:
    response = client.post("/api/v1/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200
    return response.json()["access_token"]


def _create_user(client: TestClient, username: str, password: str, role: str) -> str:
    admin_token = _get_token(client)
    response = client.post(
        "/api/v1/users",
        json={"username": username, "password": password, "role": role},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert response.status_code == 201
    return _get_token(client, username, password)


def test_anonymous_requests_cannot_access_admin_surfaces(client: TestClient) -> None:
    protected_paths = [
        "/api/v1/services",
        "/api/v1/services/secret-service/logs",
        "/api/v1/platform/config",
        "/api/v1/platform/services",
        "/api/v1/storage/buckets",
        "/api/v1/secrets",
        "/api/v1/deployments",
    ]

    for path in protected_paths:
        response = client.get(path)
        assert response.status_code in {401, 403}, path


def test_viewer_can_read_platform_config_but_not_platform_internals(
    client: TestClient,
) -> None:
    viewer_token = _create_user(client, "viewer-security", "pass", "viewer")
    headers = {"Authorization": f"Bearer {viewer_token}"}

    config_response = client.get("/api/v1/platform/config", headers=headers)
    assert config_response.status_code == 200

    service_response = client.get("/api/v1/platform/services", headers=headers)
    assert service_response.status_code == 403

    logs_response = client.get("/api/v1/platform/services/control-plane/logs", headers=headers)
    assert logs_response.status_code == 403


def test_viewer_cannot_access_service_logs(client: TestClient) -> None:
    viewer_token = _create_user(client, "viewer-logs", "pass", "viewer")
    response = client.get(
        "/api/v1/services/secret-service/logs",
        headers={"Authorization": f"Bearer {viewer_token}"},
    )
    assert response.status_code == 403


def test_viewer_cannot_mutate_storage_or_iac(client: TestClient) -> None:
    viewer_token = _create_user(client, "viewer-mutate", "pass", "viewer")
    headers = {"Authorization": f"Bearer {viewer_token}"}

    bucket_response = client.post(
        "/api/v1/storage/buckets",
        json={"name": "viewer-bucket"},
        headers=headers,
    )
    assert bucket_response.status_code == 403

    apply_response = client.post(
        "/api/v1/manifests/apply",
        json={"yaml_text": ""},
        headers=headers,
    )
    assert apply_response.status_code == 403

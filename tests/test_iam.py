from __future__ import annotations

from fastapi.testclient import TestClient


def test_login_with_default_admin_credentials_returns_token(client: TestClient) -> None:
    response = client.post("/api/v1/auth/login", json={"username": "admin", "password": "admin"})
    assert response.status_code == 200
    data = response.json()
    assert "access_token" in data
    assert data["token_type"] == "bearer"


def test_login_with_wrong_password_returns_401(client: TestClient) -> None:
    response = client.post("/api/v1/auth/login", json={"username": "admin", "password": "wrong"})
    assert response.status_code == 401


def test_login_with_unknown_user_returns_401(client: TestClient) -> None:
    response = client.post("/api/v1/auth/login", json={"username": "nobody", "password": "x"})
    assert response.status_code == 401


def _get_token(client: TestClient, username: str = "admin", password: str = "admin") -> str:
    response = client.post("/api/v1/auth/login", json={"username": username, "password": password})
    return response.json()["access_token"]


def test_get_me_returns_current_user(client: TestClient) -> None:
    token = _get_token(client)
    response = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    assert response.json()["username"] == "admin"
    assert response.json()["role"] == "admin"


def test_get_me_without_token_returns_401(client: TestClient) -> None:
    response = client.get("/api/v1/auth/me")
    assert response.status_code == 401


def test_list_users_as_admin_returns_users(client: TestClient) -> None:
    token = _get_token(client)
    response = client.get("/api/v1/users", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    usernames = [user["username"] for user in response.json()]
    assert "admin" in usernames


def test_create_user_as_admin_succeeds(client: TestClient) -> None:
    token = _get_token(client)
    headers = {"Authorization": f"Bearer {token}"}
    response = client.post(
        "/api/v1/users",
        json={"username": "alice", "password": "secret", "role": "developer"},
        headers=headers,
    )
    assert response.status_code == 201
    assert response.json()["username"] == "alice"
    assert response.json()["role"] == "developer"


def test_created_user_can_login(client: TestClient) -> None:
    token = _get_token(client)
    headers = {"Authorization": f"Bearer {token}"}
    client.post(
        "/api/v1/users",
        json={"username": "bob", "password": "bobpass", "role": "viewer"},
        headers=headers,
    )
    response = client.post("/api/v1/auth/login", json={"username": "bob", "password": "bobpass"})
    assert response.status_code == 200


def test_viewer_cannot_list_users(client: TestClient) -> None:
    admin_token = _get_token(client)
    client.post(
        "/api/v1/users",
        json={"username": "viewer1", "password": "pass", "role": "viewer"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    viewer_token = _get_token(client, "viewer1", "pass")
    response = client.get("/api/v1/users", headers={"Authorization": f"Bearer {viewer_token}"})
    assert response.status_code == 403


def test_create_duplicate_user_returns_409(client: TestClient) -> None:
    token = _get_token(client)
    headers = {"Authorization": f"Bearer {token}"}
    client.post(
        "/api/v1/users",
        json={"username": "dup", "password": "p", "role": "viewer"},
        headers=headers,
    )
    response = client.post(
        "/api/v1/users",
        json={"username": "dup", "password": "p", "role": "viewer"},
        headers=headers,
    )
    assert response.status_code == 409


def test_delete_user_as_admin_succeeds(client: TestClient) -> None:
    token = _get_token(client)
    headers = {"Authorization": f"Bearer {token}"}
    client.post(
        "/api/v1/users",
        json={"username": "tobedeleted", "password": "p", "role": "viewer"},
        headers=headers,
    )
    response = client.delete("/api/v1/users/tobedeleted", headers=headers)
    assert response.status_code == 204


def test_admin_cannot_delete_own_account(client: TestClient) -> None:
    token = _get_token(client)
    response = client.delete("/api/v1/users/admin", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 400

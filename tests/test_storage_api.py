from __future__ import annotations

from fastapi.testclient import TestClient

import aws_light.dependencies as deps
from aws_light.storage.presigned import PresignedUrlService
from aws_light.storage.storage_service import StorageService


def _auth_headers(client: TestClient) -> dict[str, str]:
    response = client.post("/api/v1/auth/login", json={"username": "admin", "password": "admin"})
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def test_bucket_object_presign_flow(client: TestClient, tmp_path) -> None:  # type: ignore[no-untyped-def]
    deps._storage_service = StorageService(tmp_path / "storage")
    deps._presigned_service = PresignedUrlService(
        secret_key="test-secret",
        base_url="http://testserver",
    )
    headers = _auth_headers(client)

    create_response = client.post(
        "/api/v1/storage/buckets",
        json={"name": "integration-bucket"},
        headers=headers,
    )
    assert create_response.status_code == 201

    put_response = client.put(
        "/api/v1/storage/buckets/integration-bucket/objects/hello.txt",
        content=b"hello bucket",
        headers={**headers, "content-type": "text/plain"},
    )
    assert put_response.status_code == 201
    assert put_response.json()["key"] == "hello.txt"

    list_response = client.get(
        "/api/v1/storage/buckets/integration-bucket/objects",
        headers=headers,
    )
    assert list_response.status_code == 200
    assert [item["key"] for item in list_response.json()] == ["hello.txt"]

    presign_response = client.post(
        "/api/v1/storage/buckets/integration-bucket/objects/hello.txt/presign",
        json={"ttl_seconds": 60},
        headers=headers,
    )
    assert presign_response.status_code == 200

    object_response = client.get(presign_response.json()["url"].removeprefix("http://testserver"))
    assert object_response.status_code == 200
    assert object_response.content == b"hello bucket"

    delete_response = client.delete(
        "/api/v1/storage/buckets/integration-bucket/objects/hello.txt",
        headers=headers,
    )
    assert delete_response.status_code == 204

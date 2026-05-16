from __future__ import annotations

from pathlib import Path

import pytest

from aws_light.storage.presigned import PresignedUrlService
from aws_light.storage.storage_service import (
    BucketNotFoundError,
    ObjectNotFoundError,
    StorageService,
)


@pytest.fixture()
def storage(tmp_path: Path) -> StorageService:
    return StorageService(storage_root=tmp_path / "storage")


def test_create_and_list_bucket(storage: StorageService) -> None:
    storage.create_bucket("my-bucket")
    buckets = storage.list_buckets()
    assert any(bucket.name == "my-bucket" for bucket in buckets)


def test_put_and_get_object(storage: StorageService) -> None:
    storage.create_bucket("test-bucket")
    storage.put_object("test-bucket", "file.txt", b"hello world", "text/plain")
    data = storage.get_object("test-bucket", "file.txt")
    assert data == b"hello world"


def test_get_object_from_missing_bucket_raises(storage: StorageService) -> None:
    with pytest.raises(BucketNotFoundError):
        storage.get_object("no-bucket", "key")


def test_get_missing_object_raises(storage: StorageService) -> None:
    storage.create_bucket("b")
    with pytest.raises(ObjectNotFoundError):
        storage.get_object("b", "missing-key")


def test_delete_object_removes_it(storage: StorageService) -> None:
    storage.create_bucket("b")
    storage.put_object("b", "to-delete.txt", b"data", "text/plain")
    storage.delete_object("b", "to-delete.txt")
    with pytest.raises(ObjectNotFoundError):
        storage.get_object("b", "to-delete.txt")


def test_list_objects_returns_metadata(storage: StorageService) -> None:
    storage.create_bucket("b")
    storage.put_object("b", "a.txt", b"aaa", "text/plain")
    storage.put_object("b", "b.txt", b"bb", "text/plain")
    objects = storage.list_objects("b")
    keys = {obj.key for obj in objects}
    assert keys == {"a.txt", "b.txt"}


def test_list_objects_with_prefix_filters(storage: StorageService) -> None:
    storage.create_bucket("b")
    storage.put_object("b", "logs/2024.txt", b"x", "text/plain")
    storage.put_object("b", "models/v1.bin", b"y", "application/octet-stream")
    objects = storage.list_objects("b", prefix="logs/")
    assert len(objects) == 1
    assert objects[0].key == "logs/2024.txt"


def test_presigned_url_validates_correctly() -> None:
    service = PresignedUrlService(secret_key="test-secret", base_url="http://localhost:8000")
    url = service.generate_presigned_get("my-bucket", "file.txt", ttl_seconds=3600)
    from urllib.parse import parse_qs, urlparse

    parsed = urlparse(url)
    params = {key: values[0] for key, values in parse_qs(parsed.query).items()}
    assert service.validate_presigned_url(
        params["bucket"], params["key"], params["expires"], params["signature"]
    )


def test_presigned_url_rejects_tampered_signature() -> None:
    service = PresignedUrlService(secret_key="test-secret", base_url="http://localhost:8000")
    url = service.generate_presigned_get("my-bucket", "file.txt", ttl_seconds=3600)
    from urllib.parse import parse_qs, urlparse

    parsed = urlparse(url)
    params = {key: values[0] for key, values in parse_qs(parsed.query).items()}
    assert not service.validate_presigned_url(
        params["bucket"], params["key"], params["expires"], "tampered-signature"
    )

from __future__ import annotations

from pathlib import Path

from aws_light.models.storage import Bucket, ObjectMeta


class BucketNotFoundError(Exception):
    pass


class ObjectNotFoundError(Exception):
    pass


class StorageService:
    def __init__(self, storage_root: Path) -> None:
        self._storage_root = storage_root

    def _bucket_path(self, bucket_name: str) -> Path:
        return self._storage_root / bucket_name

    def _object_path(self, bucket_name: str, object_key: str) -> Path:
        return self._bucket_path(bucket_name) / "objects" / object_key

    def _meta_path(self, bucket_name: str, object_key: str) -> Path:
        return self._bucket_path(bucket_name) / "meta" / f"{object_key}.json"

    def create_bucket(self, name: str) -> Bucket:
        bucket_path = self._bucket_path(name)
        bucket_path.mkdir(parents=True, exist_ok=True)
        (bucket_path / "objects").mkdir(exist_ok=True)
        (bucket_path / "meta").mkdir(exist_ok=True)
        bucket = Bucket(name=name)
        bucket_meta_path = bucket_path / "bucket.json"
        bucket_meta_path.write_text(bucket.model_dump_json())
        return bucket

    def delete_bucket(self, name: str) -> None:
        import shutil

        bucket_path = self._bucket_path(name)
        if not bucket_path.exists():
            raise BucketNotFoundError(name)
        shutil.rmtree(bucket_path)

    def list_buckets(self) -> list[Bucket]:
        buckets = []
        if not self._storage_root.exists():
            return buckets
        for entry in self._storage_root.iterdir():
            if entry.is_dir():
                bucket_meta_path = entry / "bucket.json"
                if bucket_meta_path.exists():
                    buckets.append(Bucket.model_validate_json(bucket_meta_path.read_text()))
        return buckets

    def bucket_exists(self, name: str) -> bool:
        return (self._bucket_path(name) / "bucket.json").exists()

    def put_object(
        self, bucket_name: str, object_key: str, data: bytes, content_type: str
    ) -> ObjectMeta:
        if not self.bucket_exists(bucket_name):
            raise BucketNotFoundError(bucket_name)
        object_path = self._object_path(bucket_name, object_key)
        object_path.parent.mkdir(parents=True, exist_ok=True)
        object_path.write_bytes(data)
        meta = ObjectMeta(
            bucket=bucket_name,
            key=object_key,
            size_bytes=len(data),
            content_type=content_type,
        )
        meta_path = self._meta_path(bucket_name, object_key)
        meta_path.parent.mkdir(parents=True, exist_ok=True)
        meta_path.write_text(meta.model_dump_json())
        return meta

    def get_object(self, bucket_name: str, object_key: str) -> bytes:
        if not self.bucket_exists(bucket_name):
            raise BucketNotFoundError(bucket_name)
        object_path = self._object_path(bucket_name, object_key)
        if not object_path.exists():
            raise ObjectNotFoundError(object_key)
        return object_path.read_bytes()

    def delete_object(self, bucket_name: str, object_key: str) -> None:
        if not self.bucket_exists(bucket_name):
            raise BucketNotFoundError(bucket_name)
        object_path = self._object_path(bucket_name, object_key)
        meta_path = self._meta_path(bucket_name, object_key)
        if not object_path.exists():
            raise ObjectNotFoundError(object_key)
        object_path.unlink()
        if meta_path.exists():
            meta_path.unlink()

    def list_objects(self, bucket_name: str, prefix: str = "") -> list[ObjectMeta]:
        if not self.bucket_exists(bucket_name):
            raise BucketNotFoundError(bucket_name)
        meta_dir = self._bucket_path(bucket_name) / "meta"
        objects = []
        if not meta_dir.exists():
            return objects
        for meta_file in meta_dir.rglob("*.json"):
            meta = ObjectMeta.model_validate_json(meta_file.read_text())
            if not prefix or meta.key.startswith(prefix):
                objects.append(meta)
        return objects

    def object_exists(self, bucket_name: str, object_key: str) -> bool:
        return self._object_path(bucket_name, object_key).exists()

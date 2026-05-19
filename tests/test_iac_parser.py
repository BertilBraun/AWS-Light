from __future__ import annotations

import pytest

from aws_light.iac.parser import ManifestParseError, parse_manifests
from aws_light.models.manifest import (
    BucketManifest,
    DatabaseManifest,
    ManifestKind,
    SecretManifest,
    SecretsManifest,
    ServiceManifest,
)

_SERVICE_YAML = """\
apiVersion: aws-light/v1
kind: Service
metadata:
  name: my-api
spec:
  image: my-api:latest
  replicas: 2
  port: 8080
"""

_SECRET_YAML = """\
apiVersion: aws-light/v1
kind: Secret
metadata:
  name: db-password
spec:
  value: supersecret
"""

_BUCKET_YAML = """\
apiVersion: aws-light/v1
kind: Bucket
metadata:
  name: artifacts
spec:
  versioning: false
"""

_MULTI_DOCUMENT_YAML = f"{_SECRET_YAML}---\n{_BUCKET_YAML}---\n{_SERVICE_YAML}"


def test_parse_service_manifest() -> None:
    manifests = parse_manifests(_SERVICE_YAML)
    assert len(manifests) == 1
    assert isinstance(manifests[0], ServiceManifest)
    assert manifests[0].metadata.name == "my-api"
    assert manifests[0].spec.image == "my-api:latest"
    assert manifests[0].spec.replicas == 2


def test_parse_secret_manifest() -> None:
    manifests = parse_manifests(_SECRET_YAML)
    assert len(manifests) == 1
    assert isinstance(manifests[0], SecretManifest)
    assert manifests[0].metadata.name == "db-password"
    assert manifests[0].spec.value == "supersecret"


def test_parse_bucket_manifest() -> None:
    manifests = parse_manifests(_BUCKET_YAML)
    assert len(manifests) == 1
    assert isinstance(manifests[0], BucketManifest)
    assert manifests[0].metadata.name == "artifacts"


def test_parse_multi_document_yaml() -> None:
    manifests = parse_manifests(_MULTI_DOCUMENT_YAML)
    assert len(manifests) == 3
    kinds = {manifest.kind for manifest in manifests}
    assert kinds == {ManifestKind.SERVICE, ManifestKind.SECRET, ManifestKind.BUCKET}


def test_parse_empty_yaml_returns_empty_list() -> None:
    assert parse_manifests("") == []
    assert parse_manifests("---\n---") == []


def test_parse_invalid_yaml_raises_error() -> None:
    with pytest.raises(ManifestParseError):
        parse_manifests("kind: UnknownKind\nmetadata:\n  name: x\n")


def test_parse_service_with_secret_refs() -> None:
    yaml_text = """\
apiVersion: aws-light/v1
kind: Service
metadata:
  name: svc
spec:
  image: svc:1.0
  secretRefs:
    - my-secret
    - other-secret
"""
    manifests = parse_manifests(yaml_text)
    assert isinstance(manifests[0], ServiceManifest)
    assert manifests[0].spec.secret_refs == ["my-secret", "other-secret"]


def test_parse_secrets_bundle_manifest() -> None:
    yaml_text = """\
apiVersion: aws-light/v1
kind: Secrets
secrets:
  db-password: supersecret
  api-key: abc123
"""
    manifests = parse_manifests(yaml_text)
    assert len(manifests) == 1
    assert isinstance(manifests[0], SecretsManifest)
    assert manifests[0].secrets == {"db-password": "supersecret", "api-key": "abc123"}


def test_parse_secrets_bundle_mixed_document() -> None:
    yaml_text = """\
apiVersion: aws-light/v1
kind: Secrets
secrets:
  pw: secret
---
apiVersion: aws-light/v1
kind: Bucket
metadata:
  name: my-bucket
spec:
  versioning: false
"""
    manifests = parse_manifests(yaml_text)
    assert len(manifests) == 2
    assert isinstance(manifests[0], SecretsManifest)
    assert isinstance(manifests[1], BucketManifest)


def test_parse_service_resource_bindings_and_ingress_policy() -> None:
    yaml_text = """\
apiVersion: aws-light/v1
kind: Service
metadata:
  name: api
spec:
  image: api:latest
  resources:
    buckets:
      - name: demo-objects
        access: [read, write]
    databases:
      - name: app-db
        access: [connect]
  ingress:
    external: true
    internal:
      allowFrom:
        - frontend
"""
    manifests = parse_manifests(yaml_text)

    assert isinstance(manifests[0], ServiceManifest)
    assert manifests[0].spec.resources.buckets[0].name == "demo-objects"
    assert manifests[0].spec.resources.buckets[0].access == ["read", "write"]
    assert manifests[0].spec.resources.databases[0].name == "app-db"
    assert manifests[0].spec.resources.databases[0].access == ["connect"]
    assert manifests[0].spec.ingress.external is True
    assert manifests[0].spec.ingress.internal.allow_from == ["frontend"]


def test_parse_service_ingress_internal_boolean() -> None:
    yaml_text = """\
apiVersion: aws-light/v1
kind: Service
metadata:
  name: shared
spec:
  image: shared:latest
  ingress:
    internal: true
"""
    manifests = parse_manifests(yaml_text)

    assert isinstance(manifests[0], ServiceManifest)
    assert manifests[0].spec.ingress.external is False
    assert manifests[0].spec.ingress.internal.enabled is True
    assert manifests[0].spec.ingress.internal.allow_from == []


def test_parse_database_manifest() -> None:
    yaml_text = """\
apiVersion: aws-light/v1
kind: Database
metadata:
  name: app-db
spec:
  engine: postgres
  version: "16"
  storageMb: 512
"""
    manifests = parse_manifests(yaml_text)

    assert len(manifests) == 1
    assert isinstance(manifests[0], DatabaseManifest)
    assert manifests[0].kind == ManifestKind.DATABASE
    assert manifests[0].metadata.name == "app-db"
    assert manifests[0].spec.engine == "postgres"
    assert manifests[0].spec.storage_mb == 512


def test_parse_service_rejects_invalid_resource_access() -> None:
    yaml_text = """\
apiVersion: aws-light/v1
kind: Service
metadata:
  name: bad
spec:
  image: bad:latest
  resources:
    buckets:
      - name: demo-objects
        access: [delete]
"""

    with pytest.raises(ManifestParseError):
        parse_manifests(yaml_text)

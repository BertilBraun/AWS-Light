from __future__ import annotations

from enum import Enum
from typing import Annotated, Literal

from pydantic import BaseModel, Field

from aws_light.models.service import ServiceIngressSpec, ServiceResourceBindings


class ManifestKind(str, Enum):
    SERVICE = "Service"
    SECRET = "Secret"
    SECRETS = "Secrets"
    BUCKET = "Bucket"
    DATABASE = "Database"


class ManifestMetadata(BaseModel):
    name: str
    labels: dict[str, str] = Field(default_factory=dict)


class ServiceManifestSpec(BaseModel):
    image: str
    replicas: int = 1
    min_replicas: int = Field(default=1, alias="minReplicas")
    max_replicas: int = Field(default=10, alias="maxReplicas")
    cpu_request: float = Field(default=0.25, alias="cpuRequest")
    memory_request_mb: int = Field(default=128, alias="memoryRequestMb")
    port: int = 8080
    health_check_path: str = Field(default="/health", alias="healthCheckPath")
    env: dict[str, str] = Field(default_factory=dict)
    secret_refs: list[str] = Field(default_factory=list, alias="secretRefs")
    labels: dict[str, str] = Field(default_factory=dict)
    resources: ServiceResourceBindings = Field(default_factory=ServiceResourceBindings)
    ingress: ServiceIngressSpec = Field(default_factory=ServiceIngressSpec)

    model_config = {"populate_by_name": True}


class SecretManifestSpec(BaseModel):
    value: str


class BucketManifestSpec(BaseModel):
    versioning: bool = False


class DatabaseManifestSpec(BaseModel):
    engine: Literal["postgres"] = "postgres"
    version: str = "16"
    storage_mb: int = Field(default=512, alias="storageMb")

    model_config = {"populate_by_name": True}


class ServiceManifest(BaseModel):
    api_version: str = Field(default="aws-light/v1", alias="apiVersion")
    kind: Literal[ManifestKind.SERVICE]
    metadata: ManifestMetadata
    spec: ServiceManifestSpec

    model_config = {"populate_by_name": True}


class SecretManifest(BaseModel):
    api_version: str = Field(default="aws-light/v1", alias="apiVersion")
    kind: Literal[ManifestKind.SECRET]
    metadata: ManifestMetadata
    spec: SecretManifestSpec

    model_config = {"populate_by_name": True}


class SecretsManifest(BaseModel):
    """Defines multiple secrets in one document under a top-level `secrets:` map."""

    api_version: str = Field(default="aws-light/v1", alias="apiVersion")
    kind: Literal[ManifestKind.SECRETS]
    secrets: dict[str, str]

    model_config = {"populate_by_name": True}


class BucketManifest(BaseModel):
    api_version: str = Field(default="aws-light/v1", alias="apiVersion")
    kind: Literal[ManifestKind.BUCKET]
    metadata: ManifestMetadata
    spec: BucketManifestSpec

    model_config = {"populate_by_name": True}


class DatabaseManifest(BaseModel):
    api_version: str = Field(default="aws-light/v1", alias="apiVersion")
    kind: Literal[ManifestKind.DATABASE]
    metadata: ManifestMetadata
    spec: DatabaseManifestSpec

    model_config = {"populate_by_name": True}


AnyManifest = Annotated[
    ServiceManifest | SecretManifest | SecretsManifest | BucketManifest | DatabaseManifest,
    Field(discriminator="kind"),
]

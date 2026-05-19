from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from aws_light.models.common import ResourceStatus


class BucketBinding(BaseModel):
    name: str
    access: list[Literal["read", "write"]] = Field(default_factory=list)


class DatabaseBinding(BaseModel):
    name: str
    access: list[Literal["connect"]] = Field(default_factory=list)


class ServiceResourceBindings(BaseModel):
    buckets: list[BucketBinding] = Field(default_factory=list)
    databases: list[DatabaseBinding] = Field(default_factory=list)


class InternalIngressPolicy(BaseModel):
    enabled: bool = False
    allow_from: list[str] = Field(default_factory=list, alias="allowFrom")

    model_config = {"populate_by_name": True}


class ServiceIngressSpec(BaseModel):
    external: bool = False
    internal: InternalIngressPolicy = Field(default_factory=InternalIngressPolicy)

    @field_validator("internal", mode="before")
    @classmethod
    def _normalize_internal(
        cls, value: bool | dict[str, object] | InternalIngressPolicy
    ) -> bool | dict[str, object] | InternalIngressPolicy:
        if isinstance(value, bool):
            return {"enabled": value}
        if isinstance(value, dict):
            normalized = dict(value)
            if "allowFrom" in normalized or "allow_from" in normalized:
                normalized.setdefault("enabled", True)
            return normalized
        return value

    model_config = {"populate_by_name": True}


class ReplicaState(BaseModel):
    replica_id: str
    container_id: str
    node_id: str
    status: ResourceStatus
    container_ip: str = ""
    image: str = ""
    cpu_percent: float = 0.0
    memory_mb: float = 0.0
    started_at: datetime


class ServiceSpec(BaseModel):
    name: str
    image: str
    replicas: int = 1
    min_replicas: int = 1
    max_replicas: int = 10
    cpu_request: float = 0.25
    memory_request_mb: int = 128
    port: int = 8080
    health_check_path: str = "/health"
    env: dict[str, str] = Field(default_factory=dict)
    secret_refs: list[str] = Field(default_factory=list)
    labels: dict[str, str] = Field(default_factory=dict)
    resources: ServiceResourceBindings = Field(default_factory=ServiceResourceBindings)
    ingress: ServiceIngressSpec = Field(default_factory=ServiceIngressSpec)


class ServiceState(BaseModel):
    spec: ServiceSpec
    status: ResourceStatus = ResourceStatus.PENDING
    replicas: list[ReplicaState] = Field(default_factory=list)
    created_by: str = ""
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

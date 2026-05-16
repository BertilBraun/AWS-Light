from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from aws_light.models.common import ResourceStatus


class ReplicaState(BaseModel):
    replica_id: str
    container_id: str
    node_id: str
    status: ResourceStatus
    host_port: int
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


class ServiceState(BaseModel):
    spec: ServiceSpec
    status: ResourceStatus = ResourceStatus.PENDING
    replicas: list[ReplicaState] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

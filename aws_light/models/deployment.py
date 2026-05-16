from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from aws_light.models.common import ResourceStatus


class RolloutStrategy(BaseModel):
    max_surge: int = 1
    max_unavailable: int = 0


class DeploymentSpec(BaseModel):
    service_name: str
    new_image: str
    strategy: RolloutStrategy = Field(default_factory=RolloutStrategy)


class RolloutState(BaseModel):
    deployment_id: str
    spec: DeploymentSpec
    status: ResourceStatus = ResourceStatus.PENDING
    old_replica_ids: list[str] = Field(default_factory=list)
    new_replica_ids: list[str] = Field(default_factory=list)
    started_at: datetime = Field(default_factory=datetime.utcnow)
    completed_at: datetime | None = None

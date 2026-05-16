from __future__ import annotations

from pydantic import BaseModel, Field

from aws_light.models.common import ResourceStatus


class ResourceUsage(BaseModel):
    cpu_used: float = 0.0
    memory_used_mb: float = 0.0


class NodeSpec(BaseModel):
    node_id: str
    cpu_capacity: float
    memory_capacity_mb: int


class NodeState(BaseModel):
    spec: NodeSpec
    usage: ResourceUsage = Field(default_factory=ResourceUsage)
    status: ResourceStatus = ResourceStatus.RUNNING
    replica_ids: list[str] = Field(default_factory=list)

    @property
    def available_cpu(self) -> float:
        return self.spec.cpu_capacity - self.usage.cpu_used

    @property
    def available_memory_mb(self) -> float:
        return self.spec.memory_capacity_mb - self.usage.memory_used_mb

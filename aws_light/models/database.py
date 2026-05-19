from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from aws_light.models.common import ResourceStatus


class DatabaseSpec(BaseModel):
    name: str
    engine: Literal["postgres"] = "postgres"
    version: str = "16"
    storage_mb: int = 512


class DatabaseState(BaseModel):
    spec: DatabaseSpec
    status: ResourceStatus = ResourceStatus.PENDING
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

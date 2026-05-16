from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class Bucket(BaseModel):
    name: str
    versioning: bool = False
    created_at: datetime = Field(default_factory=datetime.utcnow)


class ObjectMeta(BaseModel):
    bucket: str
    key: str
    size_bytes: int
    content_type: str
    created_at: datetime = Field(default_factory=datetime.utcnow)


class PresignedUrl(BaseModel):
    url: str
    expires_at: datetime


class CreateBucketRequest(BaseModel):
    name: str
    versioning: bool = False


class PresignRequest(BaseModel):
    ttl_seconds: int = 3600

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field


class EventKind(str, Enum):
    SERVICE_UPDATED = "service.updated"
    REPLICA_STARTED = "replica.started"
    REPLICA_STOPPED = "replica.stopped"
    REPLICA_FAILED = "replica.failed"
    NODE_UPDATED = "node.updated"
    AUTOSCALE_EVALUATED = "autoscale.evaluated"
    AUTOSCALE_TRIGGERED = "autoscale.triggered"
    ROLLOUT_PROGRESS = "rollout.progress"
    HEALTH_CHECK_RECOVERED = "health_check.recovered"
    HEALTH_CHECK_FAILED = "health_check.failed"
    SECRET_CREATED = "secret.created"
    BUCKET_CREATED = "bucket.created"
    OBJECT_UPLOADED = "object.uploaded"


class WebSocketEvent(BaseModel):
    event_id: str = Field(default_factory=lambda: str(uuid4()))
    kind: EventKind
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    payload: dict[str, Any]

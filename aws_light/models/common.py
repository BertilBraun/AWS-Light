from __future__ import annotations

from datetime import datetime
from enum import Enum


class ResourceStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    UPDATING = "updating"
    DEGRADED = "degraded"
    STOPPED = "stopped"
    FAILED = "failed"
    DELETING = "deleting"


class ResourceTimestamps:
    created_at: datetime
    updated_at: datetime

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


class ResourceTimestamps:
    created_at: datetime
    updated_at: datetime

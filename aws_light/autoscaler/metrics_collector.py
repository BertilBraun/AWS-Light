from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from aws_light.config import settings

if TYPE_CHECKING:
    from redis.asyncio import Redis


@dataclass
class ServiceMetrics:
    average_cpu_percent: float
    requests_per_second: float


class MetricsCollector:
    def __init__(self, redis_client: Redis) -> None:  # type: ignore[type-arg]
        self._redis = redis_client
        self._last_rps_counts: dict[str, int] = {}

    async def collect(self, service_name: str) -> ServiceMetrics:
        raw_cpu = await self._redis.get(f"cpu:{service_name}")
        average_cpu = float(raw_cpu) if raw_cpu is not None else 0.0

        raw_rps = await self._redis.get(f"rps:{service_name}")
        current_count = int(raw_rps) if raw_rps is not None else 0
        last_count = self._last_rps_counts.get(service_name, current_count)
        delta = max(0, current_count - last_count)
        requests_per_second = delta / settings.autoscaler_interval_seconds
        self._last_rps_counts[service_name] = current_count

        return ServiceMetrics(
            average_cpu_percent=average_cpu,
            requests_per_second=requests_per_second,
        )

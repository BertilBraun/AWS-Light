from __future__ import annotations

from dataclasses import dataclass

from aws_light.compute.docker_client import DockerClient
from aws_light.models.service import ServiceState
from aws_light.store.json_store import JsonStore


@dataclass
class ServiceMetrics:
    average_cpu_percent: float
    requests_per_second: float


class MetricsCollector:
    def __init__(
        self,
        docker_client: DockerClient,
        service_store: JsonStore[ServiceState],
    ) -> None:
        self._docker_client = docker_client
        self._service_store = service_store
        self._request_counts: dict[str, int] = {}
        self._last_request_counts: dict[str, int] = {}

    def record_request(self, service_name: str) -> None:
        self._request_counts[service_name] = self._request_counts.get(service_name, 0) + 1

    async def collect(self, service_name: str) -> ServiceMetrics:
        service_state = await self._service_store.get(service_name)
        if service_state is None:
            return ServiceMetrics(average_cpu_percent=0.0, requests_per_second=0.0)

        cpu_samples = []
        for replica in service_state.replicas:
            stats = self._docker_client.get_container_stats(replica.container_id)
            if stats is not None:
                cpu_samples.append(stats.cpu_percent)

        average_cpu = sum(cpu_samples) / len(cpu_samples) if cpu_samples else 0.0

        current_count = self._request_counts.get(service_name, 0)
        last_count = self._last_request_counts.get(service_name, 0)
        from aws_light.config import settings

        elapsed_seconds = settings.autoscaler_interval_seconds
        requests_per_second = max(0.0, (current_count - last_count) / elapsed_seconds)
        self._last_request_counts[service_name] = current_count

        return ServiceMetrics(
            average_cpu_percent=average_cpu,
            requests_per_second=requests_per_second,
        )

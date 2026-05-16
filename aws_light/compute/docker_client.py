from __future__ import annotations

from dataclasses import dataclass

import docker
import docker.errors
from docker.models.containers import Container


@dataclass
class ContainerStats:
    cpu_percent: float
    memory_mb: float


@dataclass
class ContainerInfo:
    container_id: str
    name: str
    status: str
    labels: dict[str, str]


class DockerClient:
    def __init__(self) -> None:
        self._client = docker.from_env()

    def ensure_network(self, network_name: str) -> None:
        try:
            self._client.networks.get(network_name)
        except docker.errors.NotFound:
            self._client.networks.create(network_name, driver="bridge")

    def pull_image(self, image: str) -> None:
        self._client.images.pull(image)

    def create_container(
        self,
        image: str,
        name: str,
        env: dict[str, str],
        cpu_quota: float,
        memory_mb: int,
        network: str,
        labels: dict[str, str],
        host_port: int,
        container_port: int,
    ) -> str:
        # Docker cpu_quota is in microseconds per 100ms period (100000 = 1 full core)
        cpu_quota_microseconds = int(cpu_quota * 100000)
        container: Container = self._client.containers.run(
            image,
            detach=True,
            name=name,
            environment=env,
            cpu_quota=cpu_quota_microseconds,
            mem_limit=f"{memory_mb}m",
            network=network,
            labels=labels,
            ports={f"{container_port}/tcp": host_port},
            remove=False,
        )
        return container.id  # type: ignore[no-any-return]

    def remove_container(self, container_id: str) -> None:
        try:
            container: Container = self._client.containers.get(container_id)
            container.stop(timeout=5)
            container.remove(force=True)
        except docker.errors.NotFound:
            pass

    def get_container_stats(self, container_id: str) -> ContainerStats | None:
        try:
            container: Container = self._client.containers.get(container_id)
            raw_stats = container.stats(stream=False)
            cpu_percent = _calculate_cpu_percent(raw_stats)
            memory_bytes = raw_stats["memory_stats"].get("usage", 0)
            memory_mb = memory_bytes / (1024 * 1024)
            return ContainerStats(cpu_percent=cpu_percent, memory_mb=memory_mb)
        except (docker.errors.NotFound, KeyError):
            return None

    def list_containers_by_label(self, label_key: str, label_value: str) -> list[ContainerInfo]:
        containers = self._client.containers.list(filters={"label": f"{label_key}={label_value}"})
        return [
            ContainerInfo(
                container_id=container.id,
                name=container.name,
                status=container.status,
                labels=container.labels,
            )
            for container in containers
        ]

    def container_exists(self, container_id: str) -> bool:
        try:
            self._client.containers.get(container_id)
            return True
        except docker.errors.NotFound:
            return False


def _calculate_cpu_percent(raw_stats: dict) -> float:  # type: ignore[type-arg]
    cpu_delta = (
        raw_stats["cpu_stats"]["cpu_usage"]["total_usage"]
        - raw_stats["precpu_stats"]["cpu_usage"]["total_usage"]
    )
    system_delta = raw_stats["cpu_stats"].get("system_cpu_usage", 0) - raw_stats[
        "precpu_stats"
    ].get("system_cpu_usage", 0)
    num_cpus = raw_stats["cpu_stats"].get("online_cpus", 1)
    if system_delta <= 0 or cpu_delta < 0:
        return 0.0
    return (cpu_delta / system_delta) * num_cpus * 100.0

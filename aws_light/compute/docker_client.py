from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import docker
import docker.errors
from docker.models.containers import Container

from aws_light.config import settings

logger = logging.getLogger(__name__)


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


@dataclass
class ComposeContainerInfo:
    service: str
    container_id: str
    name: str
    image: str
    status: str
    health: str
    ports: list[str]


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
        container_port: int,
    ) -> tuple[str, str]:
        """Create a container and return (container_id, container_ip)."""
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
            remove=False,
        )
        container_ip = self._poll_container_ip(container, network)
        return container.id, container_ip  # type: ignore[return-value]

    def _poll_container_ip(self, container: Container, network: str) -> str:
        for _ in range(settings.container_ip_poll_retries):
            container.reload()
            networks = container.attrs.get("NetworkSettings", {}).get("Networks", {})
            ip = networks.get(network, {}).get("IPAddress", "")
            if ip:
                return ip
            time.sleep(0.2)
        logger.warning(
            "Could not get IP for container %s on network %s", container.id[:12], network
        )
        return ""

    def get_container_ip(self, container_id: str, network: str) -> str:
        try:
            container: Container = self._client.containers.get(container_id)
            container.reload()
            networks = container.attrs.get("NetworkSettings", {}).get("Networks", {})
            return networks.get(network, {}).get("IPAddress", "")  # type: ignore[no-any-return]
        except docker.errors.NotFound:
            return ""

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

    def get_container_logs(self, container_id: str, tail: int = 200) -> str:
        try:
            container: Container = self._client.containers.get(container_id)
            raw_logs = container.logs(stdout=True, stderr=True, tail=tail, timestamps=True)
            return raw_logs.decode(errors="replace")
        except docker.errors.NotFound:
            return ""

    def list_compose_containers(
        self, project_name: str = "aws-light"
    ) -> list[ComposeContainerInfo]:
        containers = self._client.containers.list(
            all=True,
            filters={"label": f"com.docker.compose.project={project_name}"},
        )
        if not containers:
            containers = [
                container
                for container in self._client.containers.list(all=True)
                if container.name.startswith(f"{project_name}-")
            ]
        return [self._compose_container_info(container) for container in containers]

    def _compose_container_info(self, container: Container) -> ComposeContainerInfo:
        container.reload()
        labels = container.labels or {}
        service = labels.get(
            "com.docker.compose.service",
            _service_name_from_container(container.name),
        )
        image_tags = getattr(container.image, "tags", []) or []
        state = container.attrs.get("State", {})
        health = state.get("Health", {}).get("Status", "")
        network_settings = container.attrs.get("NetworkSettings", {})
        return ComposeContainerInfo(
            service=service,
            container_id=container.id,
            name=container.name,
            image=(
                image_tags[0]
                if image_tags
                else container.attrs.get("Config", {}).get("Image", "")
            ),
            status=container.status,
            health=health,
            ports=_format_ports(network_settings.get("Ports", {}) or {}),
        )


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


def _service_name_from_container(container_name: str) -> str:
    name = container_name.removeprefix("aws-light-")
    return name.rsplit("-", 1)[0]


def _format_ports(raw_ports: dict) -> list[str]:  # type: ignore[type-arg]
    formatted = []
    for container_port, host_bindings in raw_ports.items():
        if not host_bindings:
            formatted.append(str(container_port))
            continue
        for binding in host_bindings:
            host_ip = binding.get("HostIp", "")
            host_port = binding.get("HostPort", "")
            formatted.append(f"{host_ip}:{host_port}->{container_port}")
    return formatted

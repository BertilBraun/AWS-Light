from __future__ import annotations

from aws_light.compute.docker_client import DockerClient, _format_volume_bindings


class _FakeDockerContainers:
    def __init__(self, container: _FakeStatsContainer) -> None:
        self._container = container

    def get(self, container_id: str) -> _FakeStatsContainer:
        assert container_id == "container-1"
        return self._container


class _FakeDockerSdkClient:
    def __init__(self, container: _FakeStatsContainer) -> None:
        self.containers = _FakeDockerContainers(container)


class _FakeStatsContainer:
    def __init__(self) -> None:
        self.stats_kwargs: dict[str, object] | None = None

    def stats(self, **kwargs: object) -> dict[str, object]:
        self.stats_kwargs = kwargs
        return {
            "cpu_stats": {
                "cpu_usage": {"total_usage": 200},
                "system_cpu_usage": 2000,
                "online_cpus": 1,
            },
            "precpu_stats": {
                "cpu_usage": {"total_usage": 100},
                "system_cpu_usage": 1000,
            },
            "memory_stats": {"usage": 1048576},
        }


def test_format_volume_bindings_expands_mount_paths_for_docker_sdk() -> None:
    assert _format_volume_bindings(
        {"aws-light-db-app-db-data": "/var/lib/postgresql/data"}
    ) == {
        "aws-light-db-app-db-data": {
            "bind": "/var/lib/postgresql/data",
            "mode": "rw",
        }
    }


def test_format_volume_bindings_preserves_missing_volumes() -> None:
    assert _format_volume_bindings(None) is None


def test_get_container_stats_uses_one_shot_docker_stats() -> None:
    container = _FakeStatsContainer()
    client = object.__new__(DockerClient)
    client._client = _FakeDockerSdkClient(container)  # type: ignore[attr-defined]

    stats = client.get_container_stats("container-1")

    assert stats is not None
    assert stats.memory_mb == 1
    assert container.stats_kwargs == {"stream": False, "one_shot": True}

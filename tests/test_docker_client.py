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


class _FakeNetwork:
    def __init__(self) -> None:
        self.connected: list[tuple[object, list[str] | None]] = []
        self.disconnected: list[object] = []

    def connect(self, container: object, aliases: list[str] | None = None) -> None:
        self.connected.append((container, aliases))

    def disconnect(self, container: object) -> None:
        self.disconnected.append(container)


class _FakeNetworks:
    def __init__(self, network: _FakeNetwork) -> None:
        self._network = network

    def get(self, network_name: str) -> _FakeNetwork:
        assert network_name == "svc-network"
        return self._network


class _FakeContainerLookup:
    def __init__(self, container: _FakeAttachedContainer) -> None:
        self._container = container

    def get(self, container_id: str) -> _FakeAttachedContainer:
        assert container_id == "container-1"
        return self._container


class _FakeAttachedContainer:
    id = "container-1"

    def __init__(self) -> None:
        self.reloads = 0
        self.attrs = {
            "NetworkSettings": {
                "Networks": {
                    "svc-network": {
                        "Aliases": ["container-1", "proxy"],
                    }
                }
            }
        }

    def reload(self) -> None:
        self.reloads += 1


class _FakeNetworkDockerSdkClient:
    def __init__(self, container: _FakeAttachedContainer, network: _FakeNetwork) -> None:
        self.containers = _FakeContainerLookup(container)
        self.networks = _FakeNetworks(network)


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


def test_connect_container_to_network_skips_existing_alias() -> None:
    container = _FakeAttachedContainer()
    network = _FakeNetwork()
    client = object.__new__(DockerClient)
    client._client = _FakeNetworkDockerSdkClient(container, network)  # type: ignore[attr-defined]

    client.connect_container_to_network("container-1", "svc-network", aliases=["proxy"])

    assert container.reloads == 1
    assert network.connected == []
    assert network.disconnected == []

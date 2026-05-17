from __future__ import annotations

from datetime import datetime

from fastapi.testclient import TestClient

import aws_light.dependencies as deps
from aws_light.models.common import ResourceStatus
from aws_light.models.events import EventKind, WebSocketEvent
from aws_light.models.node import NodeSpec, NodeState, ResourceUsage
from aws_light.models.service import ReplicaState, ServiceSpec, ServiceState


def _auth_headers(client: TestClient) -> dict[str, str]:
    response = client.post("/api/v1/auth/login", json={"username": "admin", "password": "admin"})
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def _seed_introspection_state() -> None:
    node = NodeState(
        spec=NodeSpec(node_id="node-00", cpu_capacity=0.5, memory_capacity_mb=512),
        usage=ResourceUsage(cpu_used=0.2, memory_used_mb=128),
        status=ResourceStatus.RUNNING,
        replica_ids=["replica-1"],
    )
    await deps.get_node_store().put("node-00", node)

    service = ServiceState(
        spec=ServiceSpec(
            name="hello-service",
            image="aws-light/hello-service:latest",
            replicas=2,
            cpu_request=0.2,
            memory_request_mb=128,
            port=8000,
        ),
        status=ResourceStatus.DEGRADED,
        replicas=[
            ReplicaState(
                replica_id="replica-1",
                container_id="container-1",
                node_id="node-00",
                status=ResourceStatus.RUNNING,
                container_ip="10.0.0.10",
                image="aws-light/hello-service:latest",
                started_at=datetime.utcnow(),
            )
        ],
    )
    await deps.get_service_store().put("hello-service", service)
    await deps.get_event_bus().publish(
        WebSocketEvent(
            kind=EventKind.SERVICE_UPDATED,
            payload={
                "service_name": "hello-service",
                "status": "degraded",
                "replica_count": 1,
            },
        )
    )


def test_overview_summarizes_cluster_state(client: TestClient) -> None:
    client.portal.call(_seed_introspection_state)
    response = client.get("/api/v1/overview", headers=_auth_headers(client))

    assert response.status_code == 200
    payload = response.json()
    assert payload["services"]["total"] == 1
    assert payload["services"]["desired_replicas"] == 2
    assert payload["services"]["actual_replicas"] == 1
    assert payload["nodes"]["cpu_used"] == 0.2
    assert payload["warnings"] == ["hello-service has 1/2 running replicas"]


def test_service_diagnostics_returns_placement_events_and_warnings(
    client: TestClient,
) -> None:
    client.portal.call(_seed_introspection_state)
    response = client.get(
        "/api/v1/services/hello-service/diagnostics",
        headers=_auth_headers(client),
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["desired_replicas"] == 2
    assert payload["actual_replicas"] == 1
    assert payload["node_placement"][0]["node_id"] == "node-00"
    assert payload["recent_events"][0]["kind"] == "service.updated"
    assert payload["warnings"] == ["Only 1/2 desired replicas are running"]


def test_topology_returns_platform_service_replica_and_node_graph(
    client: TestClient,
) -> None:
    client.portal.call(_seed_introspection_state)
    response = client.get("/api/v1/platform/topology", headers=_auth_headers(client))

    assert response.status_code == 200
    payload = response.json()
    node_ids = {node["id"] for node in payload["nodes"]}
    edge_pairs = {(edge["source"], edge["target"]) for edge in payload["edges"]}

    assert "proxy" in node_ids
    assert "service:hello-service" in node_ids
    assert "replica:replica-1" in node_ids
    assert "node:node-00" in node_ids
    assert ("proxy", "service:hello-service") in edge_pairs
    assert ("replica:replica-1", "node:node-00") in edge_pairs


def test_introspection_requires_auth(client: TestClient) -> None:
    for path in [
        "/api/v1/overview",
        "/api/v1/platform/topology",
        "/api/v1/services/hello-service/diagnostics",
    ]:
        response = client.get(path)
        assert response.status_code in {401, 403}


async def _seed_platform_metrics_and_events() -> None:
    redis = deps.get_redis_client()
    redis.values["proxy:requests:total"] = "3"
    redis.hashes["proxy:requests:service"] = {"hello-service": "3"}
    redis.hashes["proxy:responses:status"] = {"200": "2", "502": "1"}
    redis.hashes["proxy:failures"] = {"upstream_unreachable": "1"}
    await deps.get_event_bus().publish(
        WebSocketEvent(
            kind=EventKind.AUTOSCALE_EVALUATED,
            payload={
                "service_name": "hello-service",
                "current_replicas": 2,
                "average_cpu_percent": 1.5,
                "requests_per_second": 42.0,
            },
        )
    )
    await deps.get_event_bus().publish(
        WebSocketEvent(
            kind=EventKind.HEALTH_CHECK_FAILED,
            payload={
                "service_name": "other-service",
                "replica_id": "replica-other",
            },
        )
    )


def test_platform_metrics_exposes_proxy_counters(client: TestClient) -> None:
    client.portal.call(_seed_platform_metrics_and_events)
    response = client.get("/api/v1/platform/metrics", headers=_auth_headers(client))

    assert response.status_code == 200
    assert response.json()["proxy"] == {
        "requests_total": 3,
        "requests_by_service": {"hello-service": 3},
        "responses_by_status": {"200": 2, "502": 1},
        "failures": {"upstream_unreachable": 1},
    }


def test_platform_events_can_filter_by_component_and_service(client: TestClient) -> None:
    client.portal.call(_seed_platform_metrics_and_events)
    response = client.get(
        "/api/v1/platform/events?component=autoscaler&service=hello-service",
        headers=_auth_headers(client),
    )

    assert response.status_code == 200
    events = response.json()["events"]
    assert len(events) == 1
    assert events[0]["kind"] == "autoscale.evaluated"

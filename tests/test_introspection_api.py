from __future__ import annotations

from datetime import datetime

from fastapi.testclient import TestClient

import aws_light.dependencies as deps
from aws_light.models.common import ResourceStatus
from aws_light.models.database import DatabaseSpec, DatabaseState
from aws_light.models.events import EventKind, WebSocketEvent
from aws_light.models.node import NodeSpec, NodeState, ResourceUsage
from aws_light.models.service import (
    BucketBinding,
    DatabaseBinding,
    InternalIngressPolicy,
    ReplicaState,
    ServiceIngressSpec,
    ServiceResourceBindings,
    ServiceSpec,
    ServiceState,
)
from aws_light.proxy.routing_table import ReplicaEndpoint


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
            name="secret-service",
            image="aws-light/secret-service:latest",
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
                image="aws-light/secret-service:latest",
                started_at=datetime.utcnow(),
            )
        ],
    )
    await deps.get_service_store().put("secret-service", service)
    await deps.get_routing_table().update_service(
        "secret-service",
        [
            ReplicaEndpoint(
                replica_id="replica-1",
                host="10.0.0.10",
                port=8000,
                healthy=True,
            )
        ],
    )
    await deps.get_event_bus().publish(
        WebSocketEvent(
            kind=EventKind.SERVICE_UPDATED,
            payload={
                "service_name": "secret-service",
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
    assert payload["warnings"] == ["secret-service has 1/2 running replicas"]


def test_service_diagnostics_returns_placement_events_and_warnings(
    client: TestClient,
) -> None:
    client.portal.call(_seed_introspection_state)
    response = client.get(
        "/api/v1/services/secret-service/diagnostics",
        headers=_auth_headers(client),
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["desired_replicas"] == 2
    assert payload["actual_replicas"] == 1
    assert payload["routeable_replicas"] == 1
    assert payload["node_placement"][0]["node_id"] == "node-00"
    assert payload["node_placement"][0]["routed"] is True
    assert payload["routing_endpoints"] == [
        {
            "replica_id": "replica-1",
            "host": "10.0.0.10",
            "port": 8000,
            "healthy": True,
            "observed_replica": True,
        }
    ]
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
    assert "service:secret-service" in node_ids
    assert "replica:replica-1" in node_ids
    assert "node:node-00" in node_ids
    assert ("proxy", "service:secret-service") in edge_pairs
    assert ("replica:replica-1", "node:node-00") in edge_pairs
    assert ("redis-routing", "replica:replica-1") in edge_pairs
    assert ("proxy", "replica:replica-1") in edge_pairs


async def _seed_resource_policy_topology_state() -> None:
    deps.get_storage_service().create_bucket("demo-objects")
    await deps.get_database_store().put(
        "app-db",
        DatabaseState(spec=DatabaseSpec(name="app-db", engine="postgres", version="16")),
    )
    await deps.get_service_store().put(
        "api",
        ServiceState(
            spec=ServiceSpec(
                name="api",
                image="api:latest",
                resources=ServiceResourceBindings(
                    buckets=[BucketBinding(name="demo-objects", access=["read", "write"])],
                    databases=[DatabaseBinding(name="app-db", access=["connect"])],
                ),
                ingress=ServiceIngressSpec(external=True, internal=InternalIngressPolicy()),
            ),
            status=ResourceStatus.PENDING,
        ),
    )
    await deps.get_service_store().put(
        "frontend",
        ServiceState(
            spec=ServiceSpec(
                name="frontend",
                image="frontend:latest",
                ingress=ServiceIngressSpec(
                    external=True,
                    internal=InternalIngressPolicy(enabled=True, allow_from=["api"]),
                ),
            ),
            status=ResourceStatus.PENDING,
        ),
    )


def test_topology_includes_resource_bindings_and_ingress_policy(
    client: TestClient,
) -> None:
    client.portal.call(_seed_resource_policy_topology_state)
    response = client.get("/api/v1/platform/topology", headers=_auth_headers(client))

    assert response.status_code == 200
    payload = response.json()
    node_ids = {node["id"] for node in payload["nodes"]}
    edge_pairs = {(edge["source"], edge["target"], edge["label"]) for edge in payload["edges"]}

    assert "bucket:demo-objects" in node_ids
    assert "database:app-db" in node_ids
    assert ("service:api", "bucket:demo-objects", "binds bucket") in edge_pairs
    assert ("service:api", "database:app-db", "binds database") in edge_pairs
    assert ("service:api", "service:frontend", "allowed internal ingress") in edge_pairs


def test_topology_includes_observed_proxy_traffic_edges(client: TestClient) -> None:
    client.portal.call(_seed_introspection_state)
    client.portal.call(_seed_platform_metrics_and_events)
    response = client.get("/api/v1/platform/topology", headers=_auth_headers(client))

    assert response.status_code == 200
    traffic_edges = [
        edge
        for edge in response.json()["edges"]
        if edge["source"] == "proxy"
        and edge["target"] == "service:secret-service"
        and edge["label"] == "observed traffic"
    ]

    assert traffic_edges == [
        {
            "source": "proxy",
            "target": "service:secret-service",
            "label": "observed traffic",
            "metadata": {
                "requests_total": 3,
                "errors_total": 1,
                "avg_latency_ms": 15.0,
            },
        }
    ]


def test_introspection_requires_auth(client: TestClient) -> None:
    for path in [
        "/api/v1/overview",
        "/api/v1/platform/topology",
        "/api/v1/services/secret-service/diagnostics",
    ]:
        response = client.get(path)
        assert response.status_code in {401, 403}


async def _seed_platform_metrics_and_events() -> None:
    redis = deps.get_redis_client()
    redis.values["proxy:requests:total"] = "3"
    redis.hashes["proxy:requests:service"] = {"secret-service": "3"}
    redis.hashes["proxy:responses:status"] = {"200": "2", "502": "1"}
    redis.hashes["proxy:failures"] = {"upstream_unreachable": "1"}
    redis.hashes["proxy:ts:requests:100"] = {"secret-service": "2"}
    redis.hashes["proxy:ts:errors:100"] = {"secret-service": "1"}
    redis.hashes["proxy:ts:status:100"] = {"200": "1", "502": "1"}
    redis.hashes["proxy:ts:latency_sum:100"] = {"secret-service": "3000"}
    redis.hashes["proxy:ts:latency_count:100"] = {"secret-service": "2"}
    await deps.get_event_bus().publish(
        WebSocketEvent(
            kind=EventKind.AUTOSCALE_EVALUATED,
            payload={
                "service_name": "secret-service",
                "current_replicas": 2,
                "average_cpu_percent": 1.5,
                "requests_per_second": 42.0,
                "decision": "hold",
            },
        )
    )
    await deps.get_event_bus().publish(
        WebSocketEvent(
            kind=EventKind.PLATFORM_STARTED,
            payload={"component": "proxy", "port": 8080},
        )
    )
    await deps.get_event_bus().publish(
        WebSocketEvent(
            kind=EventKind.PROXY_REQUEST_FAILED,
            payload={
                "service_name": "secret-service",
                "status_code": 502,
                "failure_reason": "upstream_unreachable",
                "duration_ms": 4.0,
            },
        )
    )
    await deps.get_event_bus().publish(
        WebSocketEvent(
            kind=EventKind.PROXY_TRAFFIC_OBSERVED,
            payload={
                "component": "proxy",
                "window_seconds": 10,
                "requests_total": 3,
                "errors_total": 1,
                "requests_by_service": {"secret-service": 3},
                "responses_by_status": {"200": 2, "502": 1},
                "failures": {"upstream_unreachable": 1},
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
    await deps.get_event_bus().publish(
        WebSocketEvent(
            kind=EventKind.NODE_UPDATED,
            payload={"node_id": "node-00", "cpu_used": 0.1},
        )
    )


def test_platform_metrics_exposes_proxy_counters(client: TestClient) -> None:
    client.portal.call(_seed_platform_metrics_and_events)
    response = client.get("/api/v1/platform/metrics", headers=_auth_headers(client))

    assert response.status_code == 200
    assert response.json()["proxy"] == {
        "requests_total": 3,
        "requests_by_service": {"secret-service": 3},
        "responses_by_status": {"200": 2, "502": 1},
        "failures": {"upstream_unreachable": 1},
    }


def test_platform_routing_exposes_registered_endpoints(client: TestClient) -> None:
    client.portal.call(_seed_introspection_state)
    response = client.get("/api/v1/platform/routing", headers=_auth_headers(client))

    assert response.status_code == 200
    assert response.json() == {
        "services": [
            {
                "service": "secret-service",
                "endpoints": [
                    {
                        "replica_id": "replica-1",
                        "host": "10.0.0.10",
                        "port": 8000,
                        "healthy": True,
                    }
                ],
            }
        ]
    }


def test_platform_timeseries_exposes_proxy_buckets(client: TestClient) -> None:
    client.portal.call(_seed_platform_metrics_and_events)
    response = client.get("/api/v1/platform/timeseries", headers=_auth_headers(client))

    assert response.status_code == 200
    assert response.json() == {
        "bucket_seconds": 10,
        "buckets": [
            {
                "bucket": 100,
                "requests_total": 2,
                "errors_total": 1,
                "requests_by_service": {"secret-service": 2},
                "errors_by_service": {"secret-service": 1},
                "responses_by_status": {"200": 1, "502": 1},
                "avg_latency_ms_by_service": {"secret-service": 15.0},
            }
        ],
    }


def test_platform_events_can_filter_by_component_and_service(client: TestClient) -> None:
    client.portal.call(_seed_platform_metrics_and_events)
    response = client.get(
        "/api/v1/platform/events?component=autoscaler&service=secret-service",
        headers=_auth_headers(client),
    )

    assert response.status_code == 200
    events = response.json()["events"]
    assert len(events) == 1
    assert events[0]["kind"] == "autoscale.evaluated"


def test_platform_events_omit_routine_node_updates(client: TestClient) -> None:
    client.portal.call(_seed_platform_metrics_and_events)
    response = client.get("/api/v1/platform/events", headers=_auth_headers(client))

    assert response.status_code == 200
    assert "node.updated" not in [event["kind"] for event in response.json()["events"]]


def test_platform_events_can_filter_component_startup(client: TestClient) -> None:
    client.portal.call(_seed_platform_metrics_and_events)
    response = client.get(
        "/api/v1/platform/events?component=proxy",
        headers=_auth_headers(client),
    )

    assert response.status_code == 200
    assert [event["kind"] for event in response.json()["events"]] == [
        "proxy.traffic_observed",
        "proxy.request_failed",
        "platform.started",
    ]

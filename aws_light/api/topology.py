from __future__ import annotations

from fastapi import APIRouter, Depends

from aws_light.dependencies import (
    get_database_store,
    get_node_store,
    get_routing_table,
    get_service_store,
    get_storage_service,
)
from aws_light.iam.middleware import get_current_user
from aws_light.models.iam import UserSpec

router = APIRouter(prefix="/api/v1/platform/topology", tags=["platform"])


@router.get("")
async def get_topology(_: UserSpec = Depends(get_current_user)) -> dict[str, object]:
    services = await get_service_store().list()
    databases = await get_database_store().list()
    buckets = get_storage_service().list_buckets()
    nodes = await get_node_store().list()
    routing_table = get_routing_table()
    routed_endpoints_by_service = {
        service_name: await routing_table.get_endpoints(service_name)
        for service_name in await routing_table.all_service_names()
    }

    graph_nodes = [
        _node("client", "Client", "external"),
        _node("proxy", "Proxy", "platform"),
        _node("redis-routing", "Redis routing table", "datastore"),
        _node("redis-metrics", "Redis metrics/events", "datastore"),
        _node("control-plane", "Control plane", "platform"),
        _node("postgres", "Postgres state store", "datastore"),
        _node("orchestrator", "Orchestrator", "platform"),
        _node("health-checker", "Health checker", "platform"),
        _node("autoscaler", "Autoscaler", "platform"),
        _node("storage", "Storage volume", "datastore"),
    ]
    graph_edges = [
        _edge("client", "proxy", "HTTP requests"),
        _edge("proxy", "redis-routing", "reads routing table"),
        _edge("control-plane", "postgres", "desired/observed state"),
        _edge("control-plane", "storage", "bucket/object IO"),
        _edge("orchestrator", "postgres", "reads desired state, writes observed state"),
        _edge("orchestrator", "redis-routing", "publishes replica endpoints"),
        _edge("orchestrator", "redis-metrics", "publishes events and CPU metrics"),
        _edge("health-checker", "postgres", "reads services"),
        _edge("health-checker", "redis-routing", "updates endpoint health"),
        _edge("autoscaler", "redis-metrics", "reads CPU/RPS metrics"),
        _edge("autoscaler", "postgres", "updates service replica target"),
    ]

    for node_state in nodes:
        graph_nodes.append(
            _node(
                f"node:{node_state.spec.node_id}",
                node_state.spec.node_id,
                "node",
                {
                    "cpu_used": node_state.usage.cpu_used,
                    "cpu_reserved": node_state.usage.cpu_used,
                    "cpu_actual": node_state.actual_usage.cpu_used,
                    "cpu_capacity": node_state.spec.cpu_capacity,
                    "memory_used_mb": node_state.usage.memory_used_mb,
                    "memory_reserved_mb": node_state.usage.memory_used_mb,
                    "memory_actual_mb": node_state.actual_usage.memory_used_mb,
                    "memory_capacity_mb": node_state.spec.memory_capacity_mb,
                },
            )
        )

    for bucket in buckets:
        graph_nodes.append(_node(f"bucket:{bucket.name}", bucket.name, "bucket"))

    for database in databases:
        graph_nodes.append(
            _node(
                f"database:{database.spec.name}",
                database.spec.name,
                "database",
                {
                    "engine": database.spec.engine,
                    "version": database.spec.version,
                    "status": database.status.value,
                },
            )
        )

    for service in services:
        service_id = f"service:{service.spec.name}"
        graph_nodes.append(
            _node(
                service_id,
                service.spec.name,
                "service",
                {
                    "desired_replicas": service.spec.replicas,
                    "actual_replicas": len(service.replicas),
                    "status": service.status.value,
                    "ingress_external": service.spec.ingress.external,
                    "ingress_internal": service.spec.ingress.internal.enabled,
                    "ingress_internal_allow_from": service.spec.ingress.internal.allow_from,
                },
            )
        )
        graph_edges.append(_edge("proxy", service_id, "routes requests"))
        graph_edges.append(_edge("health-checker", service_id, "checks health"))
        graph_edges.append(_edge("autoscaler", service_id, "adjusts desired replicas"))
        for bucket_binding in service.spec.resources.buckets:
            graph_edges.append(
                _edge(
                    service_id,
                    f"bucket:{bucket_binding.name}",
                    "binds bucket",
                    {"access": bucket_binding.access},
                )
            )
        for database_binding in service.spec.resources.databases:
            graph_edges.append(
                _edge(
                    service_id,
                    f"database:{database_binding.name}",
                    "binds database",
                    {"access": database_binding.access},
                )
            )
        for source_service in service.spec.ingress.internal.allow_from:
            graph_edges.append(
                _edge(
                    f"service:{source_service}",
                    service_id,
                    "allowed internal ingress",
                )
            )
        routed_by_replica = {
            endpoint.replica_id: endpoint
            for endpoint in routed_endpoints_by_service.get(service.spec.name, [])
        }

        for replica in service.replicas:
            replica_id = f"replica:{replica.replica_id}"
            routing_endpoint = routed_by_replica.get(replica.replica_id)
            graph_nodes.append(
                _node(
                    replica_id,
                    replica.replica_id[:8],
                    "replica",
                    {
                        "service": service.spec.name,
                        "status": replica.status.value,
                        "container_ip": replica.container_ip,
                        "routed": routing_endpoint is not None,
                        "route_healthy": routing_endpoint.healthy if routing_endpoint else False,
                    },
                )
            )
            graph_edges.append(_edge(service_id, replica_id, "owns replica"))
            if routing_endpoint is not None:
                graph_edges.append(
                    _edge(
                        "redis-routing",
                        replica_id,
                        "registered endpoint",
                        {
                            "host": routing_endpoint.host,
                            "port": routing_endpoint.port,
                            "healthy": routing_endpoint.healthy,
                        },
                    )
                )
                if routing_endpoint.healthy:
                    graph_edges.append(_edge("proxy", replica_id, "forwards traffic"))
            graph_edges.append(_edge(replica_id, f"node:{replica.node_id}", "scheduled on"))

    return {"nodes": graph_nodes, "edges": graph_edges}


def _node(
    node_id: str,
    label: str,
    kind: str,
    metadata: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "id": node_id,
        "label": label,
        "kind": kind,
        "metadata": metadata or {},
    }


def _edge(
    source: str,
    target: str,
    label: str,
    metadata: dict[str, object] | None = None,
) -> dict[str, object]:
    return {"source": source, "target": target, "label": label, "metadata": metadata or {}}

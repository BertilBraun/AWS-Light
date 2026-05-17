from __future__ import annotations

from fastapi import APIRouter, Depends

from aws_light.dependencies import get_node_store, get_service_store
from aws_light.iam.middleware import get_current_user
from aws_light.models.iam import UserSpec

router = APIRouter(prefix="/api/v1/platform/topology", tags=["platform"])


@router.get("")
async def get_topology(_: UserSpec = Depends(get_current_user)) -> dict[str, object]:
    services = await get_service_store().list()
    nodes = await get_node_store().list()

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
                    "cpu_capacity": node_state.spec.cpu_capacity,
                    "memory_used_mb": node_state.usage.memory_used_mb,
                    "memory_capacity_mb": node_state.spec.memory_capacity_mb,
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
                },
            )
        )
        graph_edges.append(_edge("proxy", service_id, "routes requests"))
        graph_edges.append(_edge("health-checker", service_id, "checks health"))
        graph_edges.append(_edge("autoscaler", service_id, "adjusts desired replicas"))

        for replica in service.replicas:
            replica_id = f"replica:{replica.replica_id}"
            graph_nodes.append(
                _node(
                    replica_id,
                    replica.replica_id[:8],
                    "replica",
                    {
                        "service": service.spec.name,
                        "status": replica.status.value,
                        "container_ip": replica.container_ip,
                    },
                )
            )
            graph_edges.append(_edge(service_id, replica_id, "owns replica"))
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


def _edge(source: str, target: str, label: str) -> dict[str, str]:
    return {"source": source, "target": target, "label": label}

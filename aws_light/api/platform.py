from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from aws_light.compute.docker_client import DockerClient
from aws_light.config import settings
from aws_light.dependencies import get_event_bus

router = APIRouter(prefix="/api/v1/platform", tags=["platform"])

_PLATFORM_SERVICE_ORDER = [
    "control-plane",
    "orchestrator",
    "proxy",
    "health-checker",
    "autoscaler",
    "postgres",
    "redis",
]


@router.get("/services")
async def list_platform_services() -> list[dict[str, object]]:
    docker_client = DockerClient()
    containers = docker_client.list_compose_containers()
    by_service = {container.service: container for container in containers}

    ordered_names = [
        *[name for name in _PLATFORM_SERVICE_ORDER if name in by_service],
        *sorted(name for name in by_service if name not in _PLATFORM_SERVICE_ORDER),
    ]
    return [
        {
            "service": name,
            "container_id": by_service[name].container_id,
            "container_name": by_service[name].name,
            "image": by_service[name].image,
            "status": by_service[name].status,
            "health": by_service[name].health,
            "ports": by_service[name].ports,
            "role": _describe_platform_service(name),
        }
        for name in ordered_names
    ]


@router.get("/config")
async def get_platform_config() -> dict[str, object]:
    return {
        "scheduler_policy": settings.scheduler_policy,
        "node_count": settings.node_count,
        "node_cpu_capacity": settings.node_cpu_capacity,
        "node_memory_capacity_mb": settings.node_memory_capacity_mb,
    }


@router.get("/services/{service_name}/logs")
async def get_platform_service_logs(service_name: str, tail: int = 200) -> dict[str, object]:
    docker_client = DockerClient()
    containers = docker_client.list_compose_containers()
    container = next((item for item in containers if item.service == service_name), None)
    if container is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Platform service '{service_name}' not found",
        )

    bounded_tail = max(1, min(tail, 1000))
    return {
        "service": service_name,
        "container_id": container.container_id,
        "container_name": container.name,
        "logs": docker_client.get_container_logs(container.container_id, tail=bounded_tail),
    }


@router.get("/services/{service_name}/activity")
async def get_platform_service_activity(service_name: str) -> dict[str, object]:
    if service_name not in _PLATFORM_SERVICE_ORDER:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Platform service '{service_name}' not found",
        )

    events = await get_event_bus().get_recent_events()
    activities = [
        activity
        for activity in (_event_to_activity(event) for event in reversed(events))
        if activity is not None and activity["service"] == service_name
    ]
    return {"service": service_name, "activities": activities[:50]}


def _describe_platform_service(service_name: str) -> str:
    return {
        "control-plane": "REST API, dashboard, IaC, desired state writes",
        "orchestrator": "reconcile loop, Docker actuation, node placement",
        "proxy": "HTTP ingress and request routing",
        "health-checker": "replica probing and health state",
        "autoscaler": "replica decisions from CPU/RPS metrics",
        "postgres": "persistent desired and observed state",
        "redis": "routing, metrics, and event stream",
    }.get(service_name, "")


def _event_to_activity(event: object) -> dict[str, object] | None:
    kind = getattr(event, "kind", "")
    kind_value = getattr(kind, "value", str(kind))
    payload = getattr(event, "payload", {})
    timestamp = getattr(event, "timestamp", None)

    service = _platform_service_for_event(kind_value)
    if service is None:
        return None

    return {
        "service": service,
        "timestamp": timestamp.isoformat() if timestamp is not None else "",
        "kind": kind_value,
        "summary": _summarize_activity(kind_value, payload),
        "payload": payload,
    }


def _platform_service_for_event(kind: str) -> str | None:
    if kind in {"replica.started", "replica.stopped", "service.updated", "rollout.progress"}:
        return "orchestrator"
    if kind == "health_check.failed":
        return "health-checker"
    if kind == "autoscale.triggered":
        return "autoscaler"
    if kind in {"secret.created", "bucket.created", "object.uploaded"}:
        return "control-plane"
    return None


def _summarize_activity(kind: str, payload: dict[str, object]) -> str:
    if kind == "replica.started":
        return (
            f"Started replica {_short(payload.get('replica_id'))} for "
            f"{payload.get('service_name')} on {payload.get('node_id')}"
        )
    if kind == "replica.stopped":
        return (
            f"Stopped replica {_short(payload.get('replica_id'))} "
            f"for {payload.get('service_name')}"
        )
    if kind == "service.updated":
        return (
            f"Service {payload.get('service_name')} is {payload.get('status')} "
            f"with {payload.get('replica_count')} replicas"
        )
    if kind == "health_check.failed":
        return (
            f"Marked {payload.get('service_name')} replica {_short(payload.get('replica_id'))} "
            f"unhealthy after {payload.get('consecutive_failures')} failures"
        )
    if kind == "autoscale.triggered":
        return (
            f"Scaled {payload.get('service_name')} from {payload.get('from_replicas')} "
            f"to {payload.get('to_replicas')} replicas"
        )
    if kind == "rollout.progress":
        return (
            f"Rollout for {payload.get('service_name')} step {payload.get('step')}/"
            f"{payload.get('total_steps')}"
        )
    if kind == "secret.created":
        return f"Created secret {payload.get('secret_name')}"
    if kind == "bucket.created":
        return f"Created bucket {payload.get('bucket_name')}"
    if kind == "object.uploaded":
        return (
            f"Uploaded {payload.get('bucket_name')}/{payload.get('object_key')} "
            f"({payload.get('size_bytes')} bytes)"
        )
    return kind


def _short(value: object) -> str:
    return str(value or "")[:8]

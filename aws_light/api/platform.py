from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from aws_light.compute.docker_client import DockerClient
from aws_light.config import settings
from aws_light.dependencies import get_event_bus, get_redis_client, get_routing_table
from aws_light.iam.middleware import get_current_user, require_role
from aws_light.models.iam import Role, UserSpec

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
async def list_platform_services(
    _: UserSpec = require_role(Role.ADMIN),
) -> list[dict[str, object]]:
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
async def get_platform_config(_: UserSpec = Depends(get_current_user)) -> dict[str, object]:
    return {
        "scheduler_policy": settings.scheduler_policy,
        "node_count": settings.node_count,
        "node_cpu_capacity": settings.node_cpu_capacity,
        "node_memory_capacity_mb": settings.node_memory_capacity_mb,
    }


@router.get("/services/{service_name}/logs")
async def get_platform_service_logs(
    service_name: str,
    tail: int = 200,
    _: UserSpec = require_role(Role.ADMIN),
) -> dict[str, object]:
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
async def get_platform_service_activity(
    service_name: str,
    _: UserSpec = require_role(Role.ADMIN),
) -> dict[str, object]:
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


@router.get("/metrics")
async def get_platform_metrics(
    _: UserSpec = require_role(Role.ADMIN),
) -> dict[str, object]:
    redis_client = get_redis_client()
    total = await redis_client.get("proxy:requests:total")
    by_service = await redis_client.hgetall("proxy:requests:service")
    by_status = await redis_client.hgetall("proxy:responses:status")
    failures = await redis_client.hgetall("proxy:failures")

    return {
        "proxy": {
            "requests_total": _to_int(total),
            "requests_by_service": _int_map(by_service),
            "responses_by_status": _int_map(by_status),
            "failures": _int_map(failures),
        }
    }


@router.get("/timeseries")
async def get_platform_timeseries(
    buckets: int = 36,
    _: UserSpec = require_role(Role.ADMIN),
) -> dict[str, object]:
    redis_client = get_redis_client()
    bounded_buckets = max(1, min(buckets, 180))
    bucket_ids = await _recent_timeseries_buckets(redis_client, bounded_buckets)
    series = []
    for bucket_id in bucket_ids:
        requests = _int_map(await redis_client.hgetall(f"proxy:ts:requests:{bucket_id}"))
        errors = _int_map(await redis_client.hgetall(f"proxy:ts:errors:{bucket_id}"))
        statuses = _int_map(await redis_client.hgetall(f"proxy:ts:status:{bucket_id}"))
        latency_sum = _int_map(await redis_client.hgetall(f"proxy:ts:latency_sum:{bucket_id}"))
        latency_count = _int_map(await redis_client.hgetall(f"proxy:ts:latency_count:{bucket_id}"))
        services = sorted(set(requests) | set(errors) | set(latency_sum) | set(latency_count))
        series.append(
            {
                "bucket": bucket_id,
                "requests_total": sum(requests.values()),
                "errors_total": sum(errors.values()),
                "requests_by_service": requests,
                "errors_by_service": errors,
                "responses_by_status": statuses,
                "avg_latency_ms_by_service": {
                    service: _latency_average_ms(
                        latency_sum.get(service, 0),
                        latency_count.get(service, 0),
                    )
                    for service in services
                },
            }
        )
    return {"bucket_seconds": 10, "buckets": series}


@router.get("/routing")
async def get_platform_routing(
    _: UserSpec = require_role(Role.ADMIN),
) -> dict[str, object]:
    routing_table = get_routing_table()
    service_names = sorted(await routing_table.all_service_names())
    services = []
    for service_name in service_names:
        endpoints = await routing_table.get_endpoints(service_name)
        services.append(
            {
                "service": service_name,
                "endpoints": [
                    {
                        "replica_id": endpoint.replica_id,
                        "host": endpoint.host,
                        "port": endpoint.port,
                        "healthy": endpoint.healthy,
                    }
                    for endpoint in endpoints
                ],
            }
        )
    return {"services": services}


@router.get("/events")
async def get_platform_events(
    limit: int = 100,
    component: str | None = None,
    service: str | None = None,
    _: UserSpec = Depends(get_current_user),
) -> dict[str, object]:
    bounded_limit = max(1, min(limit, 500))
    events = list(reversed(await get_event_bus().get_recent_events()))
    filtered_events = [
        event
        for event in events
        if _is_platform_activity(event)
        and _matches_event_filter(event, component=component, service=service)
    ][:bounded_limit]
    return {
        "events": [event.model_dump(mode="json") for event in filtered_events],
        "limit": bounded_limit,
    }


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

    service = _component_for_event(kind_value, payload)
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
    if kind == "platform.started":
        return None
    if kind == "proxy.request_failed":
        return "proxy"
    if kind in {
        "replica.started",
        "replica.stopped",
        "service.updated",
        "scheduler.selected",
        "scheduler.no_capacity",
        "rollout.progress",
    }:
        return "orchestrator"
    if kind in {"autoscale.evaluated", "autoscale.triggered"}:
        return "autoscaler"
    if kind in {"health_check.failed", "health_check.passed", "health_check.recovered"}:
        return "health-checker"
    if kind in {"secret.created", "bucket.created", "object.uploaded"}:
        return "control-plane"
    return None


def _summarize_activity(kind: str, payload: dict[str, object]) -> str:
    if kind == "platform.started":
        return f"Started {payload.get('component')}"
    if kind == "replica.started":
        return (
            f"Started replica {_short(payload.get('replica_id'))} for "
            f"{payload.get('service_name')} on {payload.get('node_id')}"
        )
    if kind == "replica.stopped":
        return (
            f"Stopped replica {_short(payload.get('replica_id'))} for {payload.get('service_name')}"
        )
    if kind == "service.updated":
        return (
            f"Service {payload.get('service_name')} is {payload.get('status')} "
            f"with {payload.get('replica_count')} replicas"
        )
    if kind == "scheduler.selected":
        return (
            f"Scheduled {payload.get('service_name')} on {payload.get('node_id')} "
            f"using {payload.get('scheduler_policy')}"
        )
    if kind == "scheduler.no_capacity":
        return (
            f"Could not schedule {payload.get('service_name')}: no node fits "
            f"cpu={payload.get('cpu_request')} memory={payload.get('memory_request_mb')}mb"
        )
    if kind == "health_check.failed":
        return (
            f"Marked {payload.get('service_name')} replica {_short(payload.get('replica_id'))} "
            f"unhealthy after {payload.get('consecutive_failures')} failures"
        )
    if kind == "health_check.passed":
        return (
            f"Marked {payload.get('service_name')} replica {_short(payload.get('replica_id'))} "
            "routeable"
        )
    if kind == "proxy.request_failed":
        return (
            f"Returned {payload.get('status_code')} for {payload.get('service_name') or 'unknown'} "
            f"({payload.get('failure_reason')})"
        )
    if kind == "autoscale.triggered":
        return (
            f"Scaled {payload.get('service_name')} from {payload.get('from_replicas')} "
            f"to {payload.get('to_replicas')} replicas"
        )
    if kind == "autoscale.evaluated":
        return (
            f"Evaluated {payload.get('service_name')}: decision={payload.get('decision')} "
            f"cpu={payload.get('average_cpu_percent')} rps={payload.get('requests_per_second')}"
        )
    if kind == "health_check.recovered":
        return (
            f"Recovered {payload.get('service_name')} replica {_short(payload.get('replica_id'))}"
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


def _to_int(value: object) -> int:
    if value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _int_map(values: dict[object, object]) -> dict[str, int]:
    return {_decode_key(key): _to_int(value) for key, value in values.items()}


def _decode_key(value: object) -> str:
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return str(value)


async def _recent_timeseries_buckets(redis_client: object, limit: int) -> list[int]:
    keys = await redis_client.keys("proxy:ts:requests:*")
    bucket_ids = sorted(
        (_bucket_id_from_key(key) for key in keys if _bucket_id_from_key(key) is not None),
        reverse=True,
    )
    return sorted(bucket_ids[:limit])


def _bucket_id_from_key(value: object) -> int | None:
    key = _decode_key(value)
    try:
        return int(key.rsplit(":", 1)[1])
    except (IndexError, ValueError):
        return None


def _latency_average_ms(latency_sum_centims: int, latency_count: int) -> float:
    if latency_count <= 0:
        return 0.0
    return round((latency_sum_centims / 100) / latency_count, 2)


def _matches_event_filter(
    event: object,
    *,
    component: str | None,
    service: str | None,
) -> bool:
    kind = getattr(event, "kind", "")
    kind_value = getattr(kind, "value", str(kind))
    payload = getattr(event, "payload", {})
    event_component = _component_for_event(kind_value, payload)
    if component is not None and event_component != component:
        return False
    return not (service is not None and payload.get("service_name") != service)


def _is_platform_activity(event: object) -> bool:
    kind = getattr(event, "kind", "")
    kind_value = getattr(kind, "value", str(kind))
    payload = getattr(event, "payload", {})
    return _component_for_event(kind_value, payload) is not None


def _component_for_event(kind: str, payload: dict[str, object]) -> str | None:
    if kind == "platform.started":
        component = payload.get("component")
        return str(component) if component is not None else None
    return _platform_service_for_event(kind)

from __future__ import annotations

from collections import Counter

from fastapi import APIRouter, Depends

from aws_light.config import settings
from aws_light.dependencies import get_event_bus, get_node_store, get_service_store
from aws_light.iam.middleware import get_current_user
from aws_light.models.common import ResourceStatus
from aws_light.models.iam import UserSpec
from aws_light.models.service import ServiceState

router = APIRouter(prefix="/api/v1/overview", tags=["overview"])


@router.get("")
async def get_overview(_: UserSpec = Depends(get_current_user)) -> dict[str, object]:
    services = await get_service_store().list()
    nodes = await get_node_store().list()
    events = await get_event_bus().get_recent_events()

    desired_replicas = sum(service.spec.replicas for service in services)
    actual_replicas = sum(
        1
        for service in services
        for replica in service.replicas
        if replica.status == ResourceStatus.RUNNING
    )
    unhealthy_replicas = sum(
        1
        for service in services
        for replica in service.replicas
        if replica.status != ResourceStatus.RUNNING
    )
    service_statuses = Counter(service.status.value for service in services)
    node_count = len(nodes)
    used_cpu = sum(node.usage.cpu_used for node in nodes)
    total_cpu = sum(node.spec.cpu_capacity for node in nodes)
    used_memory = sum(node.usage.memory_used_mb for node in nodes)
    total_memory = sum(node.spec.memory_capacity_mb for node in nodes)
    warnings = _build_warnings(services, node_count, total_cpu, used_cpu)

    return {
        "services": {
            "total": len(services),
            "by_status": dict(service_statuses),
            "desired_replicas": desired_replicas,
            "actual_replicas": actual_replicas,
            "unhealthy_replicas": unhealthy_replicas,
        },
        "nodes": {
            "total": node_count,
            "cpu_used": used_cpu,
            "cpu_capacity": total_cpu,
            "memory_used_mb": used_memory,
            "memory_capacity_mb": total_memory,
        },
        "platform": {
            "scheduler_policy": settings.scheduler_policy,
            "recent_event_count": len(events),
        },
        "warnings": warnings,
    }


def _build_warnings(
    services: list[ServiceState],
    node_count: int,
    total_cpu: float,
    used_cpu: float,
) -> list[str]:
    warnings = []
    if node_count == 0:
        warnings.append("No nodes are registered")
    if total_cpu and used_cpu / total_cpu > 0.8:
        warnings.append("Cluster CPU allocation is above 80%")
    for service in services:
        desired = service.spec.replicas
        actual = sum(1 for replica in service.replicas if replica.status == ResourceStatus.RUNNING)
        if actual < desired:
            warnings.append(
                f"{service.spec.name} has {actual}/{desired} running replicas"
            )
    return warnings

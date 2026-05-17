from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status

from aws_light.compute.docker_client import DockerClient
from aws_light.dependencies import get_event_bus, get_node_store, get_service_store
from aws_light.iam.middleware import get_current_user, require_role
from aws_light.models.common import ResourceStatus
from aws_light.models.iam import Role, UserSpec
from aws_light.models.service import ServiceSpec, ServiceState

router = APIRouter(prefix="/api/v1/services", tags=["services"])


@router.get("", response_model=list[ServiceState])
async def list_services(_: UserSpec = Depends(get_current_user)) -> list[ServiceState]:
    return await get_service_store().list()


@router.post("", response_model=ServiceState, status_code=status.HTTP_201_CREATED)
async def create_service(
    spec: ServiceSpec,
    _: UserSpec = require_role(Role.DEVELOPER),
) -> ServiceState:
    service_store = get_service_store()
    if await service_store.exists(spec.name):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Service '{spec.name}' already exists",
        )
    service_state = ServiceState(
        spec=spec,
        status=ResourceStatus.PENDING,
        replicas=[],
    )
    await service_store.put(spec.name, service_state)
    return service_state


@router.get("/{name}", response_model=ServiceState)
async def get_service(name: str, _: UserSpec = Depends(get_current_user)) -> ServiceState:
    service_state = await get_service_store().get(name)
    if service_state is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Service not found")
    return service_state


@router.get("/{name}/logs")
async def get_service_logs(
    name: str,
    tail: int = 200,
    _: UserSpec = require_role(Role.DEVELOPER),
) -> dict[str, object]:
    service_state = await get_service_store().get(name)
    if service_state is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Service not found")

    docker_client = DockerClient()
    bounded_tail = max(1, min(tail, 1000))
    return {
        "service": name,
        "replicas": [
            {
                "replica_id": replica.replica_id,
                "container_id": replica.container_id,
                "node_id": replica.node_id,
                "logs": docker_client.get_container_logs(replica.container_id, tail=bounded_tail),
            }
            for replica in service_state.replicas
        ],
    }


@router.get("/{name}/diagnostics")
async def get_service_diagnostics(
    name: str,
    _: UserSpec = Depends(get_current_user),
) -> dict[str, object]:
    service_state = await get_service_store().get(name)
    if service_state is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Service not found")

    nodes = {node.spec.node_id: node for node in await get_node_store().list()}
    events = await get_event_bus().get_recent_events()
    related_events = [
        event.model_dump(mode="json")
        for event in reversed(events)
        if getattr(event, "payload", {}).get("service_name") == name
    ][:50]
    running_replicas = [
        replica for replica in service_state.replicas if replica.status == ResourceStatus.RUNNING
    ]
    warnings = _service_warnings(service_state, nodes)

    return {
        "service": service_state.model_dump(mode="json"),
        "desired_replicas": service_state.spec.replicas,
        "actual_replicas": len(running_replicas),
        "healthy_replicas": len(running_replicas),
        "unhealthy_replicas": len(service_state.replicas) - len(running_replicas),
        "node_placement": [
            {
                "node_id": replica.node_id,
                "replica_id": replica.replica_id,
                "status": replica.status.value,
                "container_ip": replica.container_ip,
                "node_known": replica.node_id in nodes,
            }
            for replica in service_state.replicas
        ],
        "recent_events": related_events,
        "warnings": warnings,
    }


@router.patch("/{name}", response_model=ServiceState)
async def update_service(
    name: str,
    spec: ServiceSpec,
    _: UserSpec = require_role(Role.DEVELOPER),
) -> ServiceState:
    service_store = get_service_store()
    existing = await service_store.get(name)
    if existing is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Service not found")
    existing.spec = spec
    existing.status = ResourceStatus.UPDATING
    existing.updated_at = datetime.utcnow()
    await service_store.put(name, existing)
    return existing


def _service_warnings(
    service_state: ServiceState,
    nodes: dict[str, object],
) -> list[str]:
    warnings = []
    desired = service_state.spec.replicas
    running = sum(
        1 for replica in service_state.replicas if replica.status == ResourceStatus.RUNNING
    )
    if running < desired:
        warnings.append(f"Only {running}/{desired} desired replicas are running")

    for replica in service_state.replicas:
        if replica.node_id not in nodes:
            warnings.append(
                f"Replica {replica.replica_id[:8]} references unknown node {replica.node_id}"
            )
        if not replica.container_ip:
            warnings.append(f"Replica {replica.replica_id[:8]} has no container IP")
        if replica.status != ResourceStatus.RUNNING:
            warnings.append(f"Replica {replica.replica_id[:8]} is {replica.status.value}")
    return warnings


@router.delete("/{name}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_service(
    name: str,
    _: UserSpec = require_role(Role.DEVELOPER),
) -> None:
    service_store = get_service_store()
    existing = await service_store.get(name)
    if existing is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Service not found")
    # Mark DELETING — the orchestrator's reconcile loop removes containers and cleans up.
    existing.status = ResourceStatus.DELETING
    existing.updated_at = datetime.utcnow()
    await service_store.put(name, existing)

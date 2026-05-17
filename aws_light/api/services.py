from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status

from aws_light.compute.docker_client import DockerClient
from aws_light.dependencies import get_service_store
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
async def get_service_logs(name: str, tail: int = 200) -> dict[str, object]:
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

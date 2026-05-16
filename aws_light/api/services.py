from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status

from aws_light.iam.middleware import get_current_user, require_role
from aws_light.models.common import ResourceStatus
from aws_light.models.iam import Role, UserSpec
from aws_light.models.service import ServiceSpec, ServiceState
from aws_light.proxy.routing_table import RoutingTable
from aws_light.store.json_store import JsonStore

router = APIRouter(prefix="/api/v1/services", tags=["services"])


def _get_service_store() -> JsonStore[ServiceState]:
    from aws_light.main import get_service_store

    return get_service_store()


def _get_routing_table() -> RoutingTable:
    from aws_light.main import get_routing_table

    return get_routing_table()


def _get_orchestrator():  # type: ignore[no-untyped-def]
    from aws_light.main import get_orchestrator

    return get_orchestrator()


@router.get("", response_model=list[ServiceState])
async def list_services(
    _: UserSpec = Depends(get_current_user),
    service_store: JsonStore[ServiceState] = Depends(_get_service_store),
) -> list[ServiceState]:
    return await service_store.list()


@router.post("", response_model=ServiceState, status_code=status.HTTP_201_CREATED)
async def create_service(
    spec: ServiceSpec,
    _: UserSpec = require_role(Role.DEVELOPER),
    service_store: JsonStore[ServiceState] = Depends(_get_service_store),
    routing_table: RoutingTable = Depends(_get_routing_table),
) -> ServiceState:
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
    await routing_table.update_service(spec.name, [])
    return service_state


@router.get("/{name}", response_model=ServiceState)
async def get_service(
    name: str,
    _: UserSpec = Depends(get_current_user),
    service_store: JsonStore[ServiceState] = Depends(_get_service_store),
) -> ServiceState:
    service_state = await service_store.get(name)
    if service_state is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Service not found")
    return service_state


@router.patch("/{name}", response_model=ServiceState)
async def update_service(
    name: str,
    spec: ServiceSpec,
    _: UserSpec = require_role(Role.DEVELOPER),
    service_store: JsonStore[ServiceState] = Depends(_get_service_store),
) -> ServiceState:
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
    service_store: JsonStore[ServiceState] = Depends(_get_service_store),
    routing_table: RoutingTable = Depends(_get_routing_table),
) -> None:
    if not await service_store.exists(name):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Service not found")
    orchestrator = _get_orchestrator()
    await orchestrator.delete_service(name)
    await routing_table.remove_service(name)

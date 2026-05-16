from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status

from aws_light.iam.middleware import get_current_user, require_role
from aws_light.models.common import ResourceStatus
from aws_light.models.deployment import DeploymentSpec, RolloutState
from aws_light.models.iam import Role, UserSpec
from aws_light.store.json_store import JsonStore

router = APIRouter(prefix="/api/v1/deployments", tags=["deployments"])


def _get_deployment_store() -> JsonStore[RolloutState]:
    from aws_light.main import get_deployment_store

    return get_deployment_store()


def _get_service_store():  # type: ignore[no-untyped-def]
    from aws_light.main import get_service_store

    return get_service_store()


@router.post("", response_model=RolloutState, status_code=status.HTTP_202_ACCEPTED)
async def create_deployment(
    spec: DeploymentSpec,
    _: UserSpec = require_role(Role.DEVELOPER),
    deployment_store: JsonStore[RolloutState] = Depends(_get_deployment_store),
    service_store=Depends(_get_service_store),
) -> RolloutState:
    service_state = await service_store.get(spec.service_name)
    if service_state is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Service '{spec.service_name}' not found",
        )

    rollout = RolloutState(
        deployment_id=str(uuid.uuid4()),
        spec=spec,
        status=ResourceStatus.PENDING,
        old_replica_ids=[r.replica_id for r in service_state.replicas],
    )
    await deployment_store.put(rollout.deployment_id, rollout)
    return rollout


@router.get("", response_model=list[RolloutState])
async def list_deployments(
    _: UserSpec = Depends(get_current_user),
    deployment_store: JsonStore[RolloutState] = Depends(_get_deployment_store),
) -> list[RolloutState]:
    return await deployment_store.list()


@router.get("/{deployment_id}", response_model=RolloutState)
async def get_deployment(
    deployment_id: str,
    _: UserSpec = Depends(get_current_user),
    deployment_store: JsonStore[RolloutState] = Depends(_get_deployment_store),
) -> RolloutState:
    rollout = await deployment_store.get(deployment_id)
    if rollout is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Deployment not found")
    return rollout

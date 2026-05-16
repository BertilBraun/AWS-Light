from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from aws_light.iam.middleware import get_current_user, require_role
from aws_light.models.iam import Role, UserSpec
from aws_light.models.secret import CreateSecretRequest

router = APIRouter(prefix="/api/v1/secrets", tags=["secrets"])


def _get_secrets_manager():  # type: ignore[no-untyped-def]
    from aws_light.main import get_secrets_manager

    return get_secrets_manager()


@router.get("", response_model=list[str])
async def list_secrets(_: UserSpec = Depends(get_current_user)) -> list[str]:
    return await _get_secrets_manager().list_secret_names()


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_secret(
    request: CreateSecretRequest,
    _: UserSpec = require_role(Role.DEVELOPER),
) -> dict[str, str]:
    secrets_manager = _get_secrets_manager()
    if await secrets_manager.exists(request.name):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Secret '{request.name}' already exists",
        )
    await secrets_manager.create_secret(request.name, request.value)
    return {"name": request.name}


@router.get("/{name}")
async def get_secret(
    name: str,
    _: UserSpec = require_role(Role.DEVELOPER),
) -> dict[str, str]:
    value = await _get_secrets_manager().get_secret(name)
    if value is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Secret not found")
    return {"name": name, "value": value}


@router.delete("/{name}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_secret(
    name: str,
    _: UserSpec = require_role(Role.DEVELOPER),
) -> None:
    secrets_manager = _get_secrets_manager()
    if not await secrets_manager.exists(name):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Secret not found")
    await secrets_manager.delete_secret(name)

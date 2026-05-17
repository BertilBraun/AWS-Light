from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from aws_light.dependencies import get_applier
from aws_light.iac.applier import ApplyResult
from aws_light.iac.differ import ManifestDiff
from aws_light.iac.parser import ManifestParseError, parse_manifests
from aws_light.iam.middleware import get_current_user, require_role
from aws_light.models.iam import Role, UserSpec

router = APIRouter(prefix="/api/v1/manifests", tags=["iac"])


class ManifestPayload(BaseModel):
    yaml_text: str


@router.post("/apply", response_model=list[ApplyResult])
async def apply_manifests(
    payload: ManifestPayload,
    _: UserSpec = require_role(Role.DEVELOPER),
) -> list[ApplyResult]:
    try:
        manifests = parse_manifests(payload.yaml_text)
    except ManifestParseError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)
        ) from error
    return await get_applier().apply(manifests)


@router.post("/diff", response_model=list[ManifestDiff])
async def diff_manifests(
    payload: ManifestPayload,
    _: UserSpec = Depends(get_current_user),
) -> list[ManifestDiff]:
    try:
        manifests = parse_manifests(payload.yaml_text)
    except ManifestParseError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)
        ) from error
    return await get_applier().diff(manifests)


@router.post("/destroy", response_model=list[ApplyResult])
async def destroy_manifests(
    payload: ManifestPayload,
    _: UserSpec = require_role(Role.DEVELOPER),
) -> list[ApplyResult]:
    try:
        manifests = parse_manifests(payload.yaml_text)
    except ManifestParseError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)
        ) from error
    return await get_applier().destroy(manifests)

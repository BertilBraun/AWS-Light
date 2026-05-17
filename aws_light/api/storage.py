from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status

from aws_light.dependencies import get_presigned_service, get_storage_service
from aws_light.iam.middleware import get_current_user, require_role
from aws_light.models.iam import Role, UserSpec
from aws_light.models.storage import (
    Bucket,
    CreateBucketRequest,
    ObjectMeta,
    PresignedUrl,
    PresignRequest,
)
from aws_light.storage.storage_service import BucketNotFoundError, ObjectNotFoundError

router = APIRouter(prefix="/api/v1/storage", tags=["storage"])


@router.get("/buckets", response_model=list[Bucket])
async def list_buckets(_: UserSpec = Depends(get_current_user)) -> list[Bucket]:
    return get_storage_service().list_buckets()


@router.post("/buckets", response_model=Bucket, status_code=status.HTTP_201_CREATED)
async def create_bucket(
    request: CreateBucketRequest,
    _: UserSpec = require_role(Role.DEVELOPER),
) -> Bucket:
    storage = get_storage_service()
    if storage.bucket_exists(request.name):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Bucket '{request.name}' already exists",
        )
    return storage.create_bucket(request.name)


@router.delete("/buckets/{bucket_name}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_bucket(
    bucket_name: str,
    _: UserSpec = require_role(Role.DEVELOPER),
) -> None:
    try:
        get_storage_service().delete_bucket(bucket_name)
    except BucketNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Bucket not found"
        ) from None


@router.get("/buckets/{bucket_name}/objects", response_model=list[ObjectMeta])
async def list_objects(
    bucket_name: str,
    prefix: str = Query(default=""),
    _: UserSpec = Depends(get_current_user),
) -> list[ObjectMeta]:
    try:
        return get_storage_service().list_objects(bucket_name, prefix)
    except BucketNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Bucket not found"
        ) from None


@router.put(
    "/buckets/{bucket_name}/objects/{object_key:path}",
    response_model=ObjectMeta,
    status_code=status.HTTP_201_CREATED,
)
async def put_object(
    bucket_name: str,
    object_key: str,
    request: Request,
    _: UserSpec = require_role(Role.DEVELOPER),
) -> ObjectMeta:
    data = await request.body()
    content_type = request.headers.get("content-type", "application/octet-stream")
    try:
        return get_storage_service().put_object(bucket_name, object_key, data, content_type)
    except BucketNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Bucket not found"
        ) from None


@router.get("/buckets/{bucket_name}/objects/{object_key:path}")
async def get_object(
    bucket_name: str,
    object_key: str,
    _: UserSpec = Depends(get_current_user),
) -> Response:
    try:
        data = get_storage_service().get_object(bucket_name, object_key)
        return Response(content=data, media_type="application/octet-stream")
    except BucketNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Bucket not found"
        ) from None
    except ObjectNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Object not found"
        ) from None


@router.delete(
    "/buckets/{bucket_name}/objects/{object_key:path}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_object(
    bucket_name: str,
    object_key: str,
    _: UserSpec = require_role(Role.DEVELOPER),
) -> None:
    try:
        get_storage_service().delete_object(bucket_name, object_key)
    except (BucketNotFoundError, ObjectNotFoundError):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Object not found"
        ) from None


@router.post(
    "/buckets/{bucket_name}/objects/{object_key:path}/presign",
    response_model=PresignedUrl,
)
async def presign_object(
    bucket_name: str,
    object_key: str,
    request: PresignRequest,
    _: UserSpec = require_role(Role.DEVELOPER),
) -> PresignedUrl:
    storage = get_storage_service()
    if not storage.bucket_exists(bucket_name):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Bucket not found"
        ) from None
    if not storage.object_exists(bucket_name, object_key):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Object not found")
    url = get_presigned_service().generate_presigned_get(
        bucket_name, object_key, request.ttl_seconds
    )
    from datetime import datetime, timedelta, timezone

    expires_at = datetime.now(tz=timezone.utc) + timedelta(seconds=request.ttl_seconds)
    return PresignedUrl(url=url, expires_at=expires_at)


@router.get("/presigned")
async def get_presigned_object(
    bucket: str = Query(),
    key: str = Query(),
    expires: str = Query(),
    signature: str = Query(),
) -> Response:
    presigned_service = get_presigned_service()
    if not presigned_service.validate_presigned_url(bucket, key, expires, signature):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid or expired URL")
    try:
        data = get_storage_service().get_object(bucket, key)
        return Response(content=data, media_type="application/octet-stream")
    except (BucketNotFoundError, ObjectNotFoundError):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Object not found"
        ) from None

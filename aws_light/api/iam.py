from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from aws_light.dependencies import get_user_store
from aws_light.iam.auth import create_token, hash_password, verify_password
from aws_light.iam.middleware import get_current_user, require_role
from aws_light.models.iam import (
    CreateUserRequest,
    LoginRequest,
    Role,
    TokenResponse,
    UserSpec,
)

router = APIRouter(prefix="/api/v1", tags=["iam"])


@router.post("/auth/login", response_model=TokenResponse)
async def login(
    request: LoginRequest,
    user_store=Depends(get_user_store),
) -> TokenResponse:
    user = await user_store.get(request.username)
    if user is None or not verify_password(request.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password",
        )
    return TokenResponse(access_token=create_token(user))


@router.get("/auth/me", response_model=UserSpec)
async def get_me(current_user: UserSpec = Depends(get_current_user)) -> UserSpec:
    return current_user


@router.get("/users", response_model=list[UserSpec])
async def list_users(
    _: UserSpec = require_role(Role.ADMIN),
    user_store=Depends(get_user_store),
) -> list[UserSpec]:
    return await user_store.list()


@router.post("/users", response_model=UserSpec, status_code=status.HTTP_201_CREATED)
async def create_user(
    request: CreateUserRequest,
    _: UserSpec = require_role(Role.ADMIN),
    user_store=Depends(get_user_store),
) -> UserSpec:
    if await user_store.exists(request.username):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"User '{request.username}' already exists",
        )
    user = UserSpec(
        username=request.username,
        role=request.role,
        password_hash=hash_password(request.password),
    )
    await user_store.put(request.username, user)
    return user


@router.delete("/users/{username}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(
    username: str,
    current_user: UserSpec = require_role(Role.ADMIN),
    user_store=Depends(get_user_store),
) -> None:
    if username == current_user.username:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot delete your own account",
        )
    if not await user_store.exists(username):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    await user_store.delete(username)

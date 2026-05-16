from __future__ import annotations

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from aws_light.iam.auth import decode_token
from aws_light.iam.permissions import has_minimum_role
from aws_light.models.iam import Role, UserSpec

_bearer_scheme = HTTPBearer()


def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(_bearer_scheme),
) -> UserSpec:
    token = credentials.credentials
    try:
        payload = decode_token(token)
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Token expired"
        ) from None
    except jwt.InvalidTokenError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token"
        ) from None
    return UserSpec(username=payload.sub, role=payload.role, password_hash="")


def require_role(minimum_role: Role):  # type: ignore[no-untyped-def]
    def dependency(current_user: UserSpec = Depends(get_current_user)) -> UserSpec:
        if not has_minimum_role(current_user.role, minimum_role):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Requires {minimum_role.value} role or higher",
            )
        return current_user

    return Depends(dependency)

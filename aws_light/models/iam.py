from __future__ import annotations

from enum import Enum

from pydantic import BaseModel


class Role(str, Enum):
    ADMIN = "admin"
    DEVELOPER = "developer"
    VIEWER = "viewer"


class UserSpec(BaseModel):
    username: str
    role: Role
    password_hash: str


class TokenPayload(BaseModel):
    sub: str
    role: Role
    exp: int


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class LoginRequest(BaseModel):
    username: str
    password: str


class CreateUserRequest(BaseModel):
    username: str
    password: str
    role: Role

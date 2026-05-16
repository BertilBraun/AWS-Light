from __future__ import annotations

from datetime import datetime, timedelta, timezone

import bcrypt
import jwt

from aws_light.config import settings
from aws_light.models.iam import Role, TokenPayload, UserSpec


def hash_password(plain_password: str) -> str:
    return bcrypt.hashpw(plain_password.encode(), bcrypt.gensalt()).decode()


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return bcrypt.checkpw(plain_password.encode(), hashed_password.encode())


def create_token(user: UserSpec) -> str:
    expiry = datetime.now(tz=timezone.utc) + timedelta(hours=settings.jwt_expiry_hours)
    payload = {
        "sub": user.username,
        "role": user.role.value,
        "exp": int(expiry.timestamp()),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_token(token: str) -> TokenPayload:
    decoded = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    return TokenPayload(sub=decoded["sub"], role=Role(decoded["role"]), exp=decoded["exp"])


def make_default_admin() -> UserSpec:
    return UserSpec(
        username=settings.default_admin_username,
        role=Role.ADMIN,
        password_hash=hash_password(settings.default_admin_password),
    )

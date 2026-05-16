from __future__ import annotations

from aws_light.models.iam import Role

_ROLE_HIERARCHY: dict[Role, int] = {
    Role.VIEWER: 0,
    Role.DEVELOPER: 1,
    Role.ADMIN: 2,
}


def role_level(role: Role) -> int:
    return _ROLE_HIERARCHY[role]


def has_minimum_role(user_role: Role, required_role: Role) -> bool:
    return role_level(user_role) >= role_level(required_role)

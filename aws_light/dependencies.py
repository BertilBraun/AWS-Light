from __future__ import annotations

import aws_light.config as _config
from aws_light.models.iam import UserSpec
from aws_light.store.json_store import JsonStore

_user_store: JsonStore[UserSpec] | None = None


def get_user_store() -> JsonStore[UserSpec]:
    global _user_store
    if _user_store is None:
        _user_store = JsonStore(_config.settings.data_directory / "users.json", UserSpec)
    return _user_store

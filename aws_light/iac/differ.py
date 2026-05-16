from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from aws_light.models.manifest import AnyManifest


@dataclass
class ManifestDiff:
    kind: str
    name: str
    action: Literal["create", "update", "none"]
    changed_fields: list[str] = field(default_factory=list)


class Differ:
    def compute_diff(self, desired: AnyManifest, current: AnyManifest | None) -> ManifestDiff:
        kind = desired.kind.value
        name = desired.metadata.name

        if current is None:
            return ManifestDiff(kind=kind, name=name, action="create")

        desired_dict = desired.model_dump(by_alias=False)
        current_dict = current.model_dump(by_alias=False)

        changed_fields = _find_changed_fields(desired_dict, current_dict)
        action: Literal["create", "update", "none"] = "update" if changed_fields else "none"
        return ManifestDiff(kind=kind, name=name, action=action, changed_fields=changed_fields)


def _find_changed_fields(
    desired: dict[str, Any],
    current: dict[str, Any],
    prefix: str = "",
) -> list[str]:
    changed = []
    all_keys = set(desired) | set(current)
    for key in all_keys:
        full_key = f"{prefix}{key}" if prefix else key
        desired_value = desired.get(key)
        current_value = current.get(key)
        if isinstance(desired_value, dict) and isinstance(current_value, dict):
            changed.extend(_find_changed_fields(desired_value, current_value, f"{full_key}."))
        elif desired_value != current_value:
            changed.append(full_key)
    return changed

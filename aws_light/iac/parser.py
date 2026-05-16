from __future__ import annotations

import yaml
from pydantic import TypeAdapter, ValidationError

from aws_light.models.manifest import AnyManifest


class ManifestParseError(Exception):
    pass


_manifest_adapter: TypeAdapter[AnyManifest] = TypeAdapter(AnyManifest)


def parse_manifests(yaml_text: str) -> list[AnyManifest]:
    documents = list(yaml.safe_load_all(yaml_text))
    manifests = []
    for index, document in enumerate(documents):
        if document is None:
            continue
        try:
            manifest = _manifest_adapter.validate_python(document)
            manifests.append(manifest)
        except ValidationError as error:
            raise ManifestParseError(
                f"Invalid manifest at document {index + 1}: {error}"
            ) from error
    return manifests

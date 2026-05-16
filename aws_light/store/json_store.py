from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Generic, TypeVar

from pydantic import BaseModel

ModelType = TypeVar("ModelType", bound=BaseModel)


class JsonStore(Generic[ModelType]):
    def __init__(self, file_path: Path, model_class: type[ModelType]) -> None:
        self._file_path = file_path
        self._model_class = model_class
        self._lock = asyncio.Lock()

    def _read_raw(self) -> dict[str, dict]:  # type: ignore[type-arg]
        if not self._file_path.exists():
            return {}
        with open(self._file_path) as file_handle:
            return json.load(file_handle)  # type: ignore[no-any-return]

    def _write_raw(self, data: dict[str, dict]) -> None:  # type: ignore[type-arg]
        self._file_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._file_path, "w") as file_handle:
            json.dump(data, file_handle, indent=2, default=str)

    async def get(self, identifier: str) -> ModelType | None:
        async with self._lock:
            raw = self._read_raw()
            if identifier not in raw:
                return None
            return self._model_class.model_validate(raw[identifier])

    async def list(self) -> list[ModelType]:
        async with self._lock:
            raw = self._read_raw()
            return [self._model_class.model_validate(value) for value in raw.values()]

    async def put(self, identifier: str, item: ModelType) -> None:
        async with self._lock:
            raw = self._read_raw()
            raw[identifier] = json.loads(item.model_dump_json())
            self._write_raw(raw)

    async def delete(self, identifier: str) -> None:
        async with self._lock:
            raw = self._read_raw()
            raw.pop(identifier, None)
            self._write_raw(raw)

    async def exists(self, identifier: str) -> bool:
        async with self._lock:
            return identifier in self._read_raw()

from __future__ import annotations

import json
from typing import Generic, TypeVar

import asyncpg
from pydantic import BaseModel

ModelType = TypeVar("ModelType", bound=BaseModel)

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS {table} (
    key        TEXT PRIMARY KEY,
    data       JSONB        NOT NULL,
    updated_at TIMESTAMPTZ  NOT NULL DEFAULT NOW()
)
"""

_UPSERT_SQL = """
INSERT INTO {table} (key, data, updated_at)
VALUES ($1, $2, NOW())
ON CONFLICT (key) DO UPDATE
    SET data = EXCLUDED.data,
        updated_at = EXCLUDED.updated_at
"""


class PostgresStore(Generic[ModelType]):
    def __init__(self, pool: asyncpg.Pool, table: str, model_class: type[ModelType]) -> None:
        self._pool = pool
        self._table = table
        self._model_class = model_class

    async def create_table(self) -> None:
        async with self._pool.acquire() as connection:
            await connection.execute(_CREATE_TABLE_SQL.format(table=self._table))

    async def get(self, identifier: str) -> ModelType | None:
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                f"SELECT data FROM {self._table} WHERE key = $1", identifier
            )
        if row is None:
            return None
        return self._model_class.model_validate(json.loads(row["data"]))

    async def list(self) -> list[ModelType]:
        async with self._pool.acquire() as connection:
            rows = await connection.fetch(f"SELECT data FROM {self._table}")
        return [self._model_class.model_validate(json.loads(row["data"])) for row in rows]

    async def put(self, identifier: str, item: ModelType) -> None:
        async with self._pool.acquire() as connection:
            await connection.execute(
                _UPSERT_SQL.format(table=self._table),
                identifier,
                item.model_dump_json(),
            )

    async def delete(self, identifier: str) -> None:
        async with self._pool.acquire() as connection:
            await connection.execute(f"DELETE FROM {self._table} WHERE key = $1", identifier)

    async def exists(self, identifier: str) -> bool:
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                f"SELECT 1 FROM {self._table} WHERE key = $1", identifier
            )
        return row is not None

import os
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI()

DATABASE_BINDING = "app-db"


class NotePayload(BaseModel):
    message: str


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/")
async def root() -> dict[str, object]:
    settings = database_settings(DATABASE_BINDING)
    return {
        "service": "database-service",
        "database": settings["database"],
        "host": settings["host"],
        "ready": bool(settings["url"]),
    }


@app.post("/notes")
async def create_note(payload: NotePayload) -> dict[str, object]:
    async with _connect() as connection:
        await _ensure_schema(connection)
        row = await connection.fetchrow(
            "insert into notes(message) values($1) returning id, message, created_at",
            payload.message,
        )
    return _note(row)


@app.get("/notes")
async def list_notes() -> dict[str, object]:
    async with _connect() as connection:
        await _ensure_schema(connection)
        rows = await connection.fetch(
            "select id, message, created_at from notes order by id desc limit 20"
        )
    return {"notes": [_note(row) for row in rows]}


def database_settings(database_name: str) -> dict[str, object]:
    prefix = f"AWS_LIGHT_DATABASE_{_env_resource_name(database_name)}"
    return {
        "host": os.environ.get(f"{prefix}_HOST", ""),
        "port": int(os.environ.get(f"{prefix}_PORT", "5432")),
        "database": os.environ.get(f"{prefix}_NAME", ""),
        "user": os.environ.get(f"{prefix}_USER", ""),
        "password": os.environ.get(f"{prefix}_PASSWORD", ""),
        "url": os.environ.get(f"{prefix}_URL", ""),
    }


async def _connect():  # type: ignore[no-untyped-def]
    try:
        import asyncpg
    except ImportError as error:
        raise HTTPException(status_code=500, detail="asyncpg is not installed") from error

    settings = database_settings(DATABASE_BINDING)
    if not settings["url"]:
        raise HTTPException(status_code=500, detail="database binding is not configured")
    return await asyncpg.connect(str(settings["url"]))


async def _ensure_schema(connection: object) -> None:
    await connection.execute(
        """
        create table if not exists notes (
            id serial primary key,
            message text not null,
            created_at timestamptz not null default now()
        )
        """
    )


def _note(row: object) -> dict[str, object]:
    created_at = row["created_at"]
    if isinstance(created_at, datetime):
        created_at = created_at.astimezone(timezone.utc).isoformat()
    return {"id": row["id"], "message": row["message"], "created_at": created_at}


def _env_resource_name(resource_name: str) -> str:
    return resource_name.upper().replace("-", "_")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), access_log=False)

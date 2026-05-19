import os
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI, Header, HTTPException

app = FastAPI()

BUCKET_NAME = "combined-objects"
DATABASE_BINDING = "combined-db"


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/")
async def aggregate(x_demo_token: str = Header(default="")) -> dict[str, object]:
    if not authorized(x_demo_token):
        raise HTTPException(status_code=401, detail="invalid demo token")

    storage = await exercise_storage()
    database = await exercise_database()
    cpu = await call_cpu_service()
    flaky = await call_flaky_service()
    return {
        "service": "combined-service",
        "storage": storage,
        "database": database,
        "cpu": cpu,
        "flaky": flaky,
    }


def authorized(token: str) -> bool:
    expected = os.environ.get("COMBINED_API_TOKEN", "")
    return bool(expected) and token == expected


async def exercise_storage() -> dict[str, object]:
    key = "combined-demo.txt"
    value = f"combined-service wrote this at {datetime.now(timezone.utc).isoformat()}"
    async with httpx.AsyncClient(timeout=5.0) as client:
        put_response = await client.put(
            object_url(key),
            content=value.encode(),
            headers=storage_headers("text/plain"),
        )
        get_response = await client.get(object_url(key), headers=storage_headers())
    _raise_platform_error(put_response)
    _raise_platform_error(get_response)
    return {"bucket": BUCKET_NAME, "key": key, "bytes": len(get_response.content)}


async def exercise_database() -> dict[str, object]:
    async with _connect() as connection:
        await connection.execute(
            """
            create table if not exists combined_events (
                id serial primary key,
                message text not null,
                created_at timestamptz not null default now()
            )
            """
        )
        row = await connection.fetchrow(
            "insert into combined_events(message) values($1) returning id, created_at",
            "combined-service aggregate request",
        )
        count = await connection.fetchval("select count(*) from combined_events")
    return {
        "database": database_settings(DATABASE_BINDING)["database"],
        "inserted_id": row["id"],
        "event_count": count,
        "created_at": row["created_at"].astimezone(timezone.utc).isoformat(),
    }


async def call_cpu_service() -> list[dict[str, object]]:
    results = []
    async with httpx.AsyncClient(timeout=10.0) as client:
        for work_ms in (25, 50, 75):
            response = await client.get(
                service_url("cpu-service", f"/?work_ms={work_ms}"),
                headers=service_headers("cpu-service"),
            )
            results.append(_service_result(response))
    return results


async def call_flaky_service() -> dict[str, object]:
    async with httpx.AsyncClient(timeout=5.0) as client:
        response = await client.get(
            service_url("flaky-service", "/"),
            headers=service_headers("flaky-service"),
        )
    return _service_result(response)


def service_url(_service_name: str, path: str) -> str:
    proxy_url = os.environ.get("AWS_LIGHT_PROXY_URL", "http://proxy:8080").rstrip("/")
    return f"{proxy_url}/{path.lstrip('/')}"


def service_headers(service_name: str) -> dict[str, str]:
    return {
        "Host": f"{service_name}.localhost",
        "X-AWS-Light-Service-Token": os.environ.get("AWS_LIGHT_SERVICE_TOKEN", ""),
    }


def object_url(key: str) -> str:
    storage_url = os.environ.get(
        "AWS_LIGHT_STORAGE_URL", "http://proxy:8080/_aws-light/storage"
    ).rstrip("/")
    return f"{storage_url}/buckets/{BUCKET_NAME}/objects/{key}"


def storage_headers(content_type: str | None = None) -> dict[str, str]:
    headers = {"X-AWS-Light-Service-Token": os.environ.get("AWS_LIGHT_SERVICE_TOKEN", "")}
    if content_type is not None:
        headers["content-type"] = content_type
    return headers


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
    url = database_settings(DATABASE_BINDING)["url"]
    if not url:
        raise HTTPException(status_code=500, detail="database binding is not configured")
    return await asyncpg.connect(str(url))


def _service_result(response: httpx.Response) -> dict[str, object]:
    content_type = response.headers.get("content-type", "")
    if "application/json" in content_type:
        body: object = response.json()
    else:
        body = response.text
    return {"status": response.status_code, "body": body}


def _raise_platform_error(response: httpx.Response) -> None:
    if response.status_code < 400:
        return
    raise HTTPException(status_code=response.status_code, detail=response.text)


def _env_resource_name(resource_name: str) -> str:
    return resource_name.upper().replace("-", "_")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), access_log=False)

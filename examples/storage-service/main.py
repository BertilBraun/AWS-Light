import os
import time
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

app = FastAPI()

BUCKET_NAME = os.environ.get("AWS_LIGHT_BUCKET_NAME", "demo-objects")


class ObjectPayload(BaseModel):
    value: str


@app.middleware("http")
async def log_requests(request: Request, call_next):  # type: ignore[no-untyped-def]
    started = time.perf_counter()
    response = await call_next(request)
    if request.url.path != "/health" or response.status_code >= 400:
        _log(
            "request",
            method=request.method,
            path=request.url.path,
            status=response.status_code,
            elapsed_ms=round((time.perf_counter() - started) * 1000, 2),
        )
    return response


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/")
async def root() -> dict[str, object]:
    async with httpx.AsyncClient(timeout=5.0) as client:
        response = await client.get(objects_url(), headers=storage_headers())
    _raise_platform_error(response)
    return {
        "service": "storage-service",
        "bucket": BUCKET_NAME,
        "objects": [item["key"] for item in response.json()],
    }


@app.put("/objects/{key}")
async def put_object(key: str, payload: ObjectPayload) -> dict[str, object]:
    async with httpx.AsyncClient(timeout=5.0) as client:
        response = await client.put(
            object_url(key),
            content=payload.value.encode(),
            headers=storage_headers("text/plain"),
        )
    _raise_platform_error(response)
    return response.json()


@app.get("/objects/{key}")
async def get_object(key: str) -> dict[str, object]:
    async with httpx.AsyncClient(timeout=5.0) as client:
        response = await client.get(object_url(key), headers=storage_headers())
    if response.status_code == 404:
        raise HTTPException(status_code=404, detail="object not found")
    _raise_platform_error(response)
    value = response.content.decode(errors="replace")
    return {"key": key, "value": value, "size": len(value)}


@app.delete("/objects/{key}")
async def delete_object(key: str) -> dict[str, object]:
    async with httpx.AsyncClient(timeout=5.0) as client:
        response = await client.delete(object_url(key), headers=storage_headers())
    if response.status_code not in {204, 404}:
        _raise_platform_error(response)
    return {"key": key, "deleted": True}


def objects_url() -> str:
    return f"{storage_base_url()}/buckets/{BUCKET_NAME}/objects"


def object_url(key: str) -> str:
    if "/" in key or "\\" in key or key in {"", ".", ".."}:
        raise HTTPException(status_code=400, detail="invalid key")
    return f"{objects_url()}/{key}"


def storage_headers(content_type: str | None = None) -> dict[str, str]:
    headers = {"X-AWS-Light-Service-Token": os.environ.get("AWS_LIGHT_SERVICE_TOKEN", "")}
    if content_type is not None:
        headers["content-type"] = content_type
    return headers


def storage_base_url() -> str:
    return os.environ.get("AWS_LIGHT_STORAGE_URL", "http://proxy:8080/_aws-light/storage").rstrip(
        "/"
    )


def _raise_platform_error(response: httpx.Response) -> None:
    if response.status_code < 400:
        return
    raise HTTPException(status_code=response.status_code, detail=response.text)


def _log(event: str, **fields: object) -> None:
    import json

    print(
        json.dumps({"ts": datetime.now(timezone.utc).isoformat(), "event": event, **fields}),
        flush=True,
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), access_log=False)

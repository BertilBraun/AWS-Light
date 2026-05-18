import os
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

app = FastAPI()
STORE_ROOT = Path(os.environ.get("STORE_ROOT", "/tmp/aws-light-storage-demo"))


class ObjectPayload(BaseModel):
    value: str


@app.middleware("http")
async def log_requests(request: Request, call_next):
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


@app.on_event("startup")
def startup() -> None:
    STORE_ROOT.mkdir(parents=True, exist_ok=True)
    _log("startup", root=str(STORE_ROOT))


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/")
def root() -> dict[str, object]:
    return {"service": "storage-service", "objects": _list_objects()}


@app.put("/objects/{key}")
def put_object(key: str, payload: ObjectPayload) -> dict[str, object]:
    path = _safe_path(key)
    path.write_text(payload.value, encoding="utf-8")
    return {"key": key, "size": len(payload.value)}


@app.get("/objects/{key}")
def get_object(key: str) -> dict[str, object]:
    path = _safe_path(key)
    if not path.exists():
        raise HTTPException(status_code=404, detail="object not found")
    value = path.read_text(encoding="utf-8")
    return {"key": key, "value": value, "size": len(value)}


@app.delete("/objects/{key}")
def delete_object(key: str) -> dict[str, object]:
    path = _safe_path(key)
    if path.exists():
        path.unlink()
    return {"key": key, "deleted": True}


def _safe_path(key: str) -> Path:
    if "/" in key or "\\" in key or key in {"", ".", ".."}:
        raise HTTPException(status_code=400, detail="invalid key")
    return STORE_ROOT / key


def _list_objects() -> list[str]:
    if not STORE_ROOT.exists():
        return []
    return sorted(item.name for item in STORE_ROOT.iterdir() if item.is_file())


def _log(event: str, **fields: object) -> None:
    import json

    print(
        json.dumps({"ts": datetime.now(timezone.utc).isoformat(), "event": event, **fields}),
        flush=True,
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), access_log=False)

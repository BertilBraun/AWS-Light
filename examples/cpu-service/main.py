import hashlib
import os
import time
from datetime import datetime, timezone

from fastapi import FastAPI, Request

app = FastAPI()


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


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/")
def root(work_ms: int = 150) -> dict[str, object]:
    bounded_work_ms = max(1, min(work_ms, 5000))
    end = time.perf_counter() + bounded_work_ms / 1000
    iterations = 0
    digest = b"aws-light"
    while time.perf_counter() < end:
        digest = hashlib.sha256(digest).digest()
        iterations += 1
    return {
        "service": "cpu-service",
        "work_ms": bounded_work_ms,
        "iterations": iterations,
        "digest": digest.hex()[:16],
    }


def _log(event: str, **fields: object) -> None:
    import json

    print(
        json.dumps({"ts": datetime.now(timezone.utc).isoformat(), "event": event, **fields}),
        flush=True,
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), access_log=False)

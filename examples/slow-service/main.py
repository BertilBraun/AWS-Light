import asyncio
import os
import random
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
async def root(min_ms: int = 100, max_ms: int = 1200) -> dict[str, object]:
    bounded_min = max(0, min(min_ms, 5000))
    bounded_max = max(bounded_min, min(max_ms, 5000))
    delay_ms = random.randint(bounded_min, bounded_max)
    await asyncio.sleep(delay_ms / 1000)
    return {"service": "slow-service", "delay_ms": delay_ms}


def _log(event: str, **fields: object) -> None:
    import json

    print(
        json.dumps({"ts": datetime.now(timezone.utc).isoformat(), "event": event, **fields}),
        flush=True,
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), access_log=False)

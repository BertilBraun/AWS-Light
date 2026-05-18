import os
import random
import time
from datetime import datetime, timezone

from fastapi import FastAPI, Request, Response, status

app = FastAPI()


REQUEST_FAILURE_RATE = float(os.environ.get("REQUEST_FAILURE_RATE", "0.25"))
HEALTH_FAILURE_RATE = float(os.environ.get("HEALTH_FAILURE_RATE", "0.10"))


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
def health(response: Response) -> dict[str, str]:
    if random.random() < HEALTH_FAILURE_RATE:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "unhealthy"}
    return {"status": "ok"}


@app.get("/")
def root(response: Response) -> dict[str, object]:
    if random.random() < REQUEST_FAILURE_RATE:
        response.status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
        return {"service": "flaky-service", "ok": False, "error": "simulated failure"}
    return {"service": "flaky-service", "ok": True}


def _log(event: str, **fields: object) -> None:
    import json

    print(
        json.dumps({"ts": datetime.now(timezone.utc).isoformat(), "event": event, **fields}),
        flush=True,
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), access_log=False)

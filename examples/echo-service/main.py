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


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def echo(path: str, request: Request) -> dict[str, object]:
    body = await request.body()
    return {
        "service": "echo-service",
        "method": request.method,
        "path": f"/{path}",
        "query": dict(request.query_params),
        "headers": {
            key: value
            for key, value in request.headers.items()
            if key.lower() in {"host", "content-type", "user-agent"}
        },
        "body": body.decode(errors="replace"),
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

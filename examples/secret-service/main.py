import os
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, Request, Response, status
from fastapi.responses import JSONResponse


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    port = int(os.environ.get("PORT", "8000"))
    _log("startup", port=port)
    yield


app = FastAPI(lifespan=lifespan)


@app.middleware("http")
async def log_requests(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    started = time.perf_counter()
    assert request.client is not None, "Request client is None"
    response = await call_next(request)
    elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
    if request.url.path != "/health" or response.status_code >= 400:
        _log(
            "request",
            method=request.method,
            path=request.url.path,
            client=request.client.host,
            status=response.status_code,
            elapsed_ms=elapsed_ms,
        )
    return response


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/")
def root() -> dict[str, str]:
    return {
        "service": "secret-service",
        "my_secret": os.environ.get("MY_SECRET", "<not set>"),
        "another_secret": os.environ.get("ANOTHER_SECRET", "<not set>"),
    }


@app.exception_handler(404)
def not_found(_request: object, _exc: object) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_404_NOT_FOUND,
        content={"error": "not found"},
    )


def _log(event: str, **fields: object) -> None:
    import json

    print(
        json.dumps(
            {
                "ts": datetime.now(timezone.utc).isoformat(),
                "event": event,
                **fields,
            }
        ),
        flush=True,
    )


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port, access_log=False)

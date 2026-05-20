import os

import httpx
from fastapi import FastAPI
from fastapi.responses import JSONResponse

app = FastAPI()


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/")
async def root() -> JSONResponse:
    async with httpx.AsyncClient(timeout=5.0) as client:
        response = await client.get(backend_url("/message"), headers=backend_headers())
    return JSONResponse(
        status_code=response.status_code,
        content={
            "service": "internal-frontend",
            "backend_status": response.status_code,
            "backend": (
                response.json()
                if response.headers.get("content-type") == "application/json"
                else response.text
            ),
        },
    )


def backend_url(path: str) -> str:
    proxy_url = os.environ.get("AWS_LIGHT_PROXY_URL", "http://proxy:8080").rstrip("/")
    return f"{proxy_url}/{path.lstrip('/')}"


def backend_headers() -> dict[str, str]:
    return {
        "Host": "internal-backend.localhost",
        "X-AWS-Light-Service-Token": os.environ.get("AWS_LIGHT_SERVICE_TOKEN", ""),
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), access_log=False)

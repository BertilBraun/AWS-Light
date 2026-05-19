import os
from datetime import datetime, timezone

from fastapi import FastAPI

app = FastAPI()


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/message")
def message() -> dict[str, str]:
    return {
        "service": "internal-backend",
        "message": "hello from the internal backend",
        "ts": datetime.now(timezone.utc).isoformat(),
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), access_log=False)

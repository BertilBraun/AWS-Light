import os

from fastapi import FastAPI, status
from fastapi.responses import JSONResponse

app = FastAPI()


@app.get('/health')
def health() -> dict[str, str]:
    return {'status': 'ok'}


@app.get('/')
def root() -> dict[str, str]:
    return {
        'my_secret': os.environ.get('MY_SECRET', '<not set>'),
        'another_secret': os.environ.get('ANOTHER_SECRET', '<not set>'),
    }


@app.exception_handler(404)
def not_found(_request: object, _exc: object) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_404_NOT_FOUND,
        content={'error': 'not found'},
    )


if __name__ == '__main__':
    import uvicorn

    port = int(os.environ.get('PORT', '8000'))
    uvicorn.run(app, host='0.0.0.0', port=port)

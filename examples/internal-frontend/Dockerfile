FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir fastapi httpx "uvicorn[standard]"
COPY main.py .
CMD ["python", "main.py"]

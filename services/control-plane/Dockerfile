FROM python:3.10-slim
WORKDIR /app

COPY aws_light/ aws_light/
COPY pyproject.toml .
RUN pip install --no-cache-dir -e ".[prod]"

COPY services/control-plane/main.py .
CMD ["python", "main.py"]

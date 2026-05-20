# Getting Started

This guide brings up the AWS-Light platform, builds example workload images, and
deploys the combined demo stack.

## Prerequisites

- Docker Desktop or Docker Engine with Docker Compose.
- Python 3.10 or newer.
- A shell with `docker`, `docker compose`, and `python` on `PATH`.

On Windows, run the commands from PowerShell in the repository root.

## Start The Platform

Create local configuration:

```powershell
Copy-Item .env.example .env
```

Start the platform services:

```powershell
docker compose up -d --build
```

The main endpoints are:

- `http://localhost:8000`: control plane API and dashboard.
- `http://localhost:8080`: service proxy.

Install the CLI locally:

```powershell
pip install -e ".[dev]"
```

Log in with the default local admin account from `.env`:

```powershell
aws-light login --user admin --password admin
```

## Build Example Images

The platform creates containers from image names in manifests. Build the images
before applying examples:

```powershell
docker build -t aws-light/echo-service:latest examples/echo-service
docker build -t aws-light/slow-service:latest examples/slow-service
docker build -t aws-light/flaky-service:latest examples/flaky-service
docker build -t aws-light/cpu-service:latest examples/cpu-service
docker build -t aws-light/storage-service:latest examples/storage-service
docker build -t aws-light/secret-service:latest examples/secret-service
docker build -t aws-light/database-service:latest examples/database-service
docker build -t aws-light/internal-backend:latest examples/internal-backend
docker build -t aws-light/internal-frontend:latest examples/internal-frontend
docker build -t aws-light/combined-service:latest examples/combined-service
```

For the main demo, only `cpu-service`, `flaky-service`, and `combined-service`
are required.

## Deploy The Combined Demo

Apply the combined stack manifest:

```powershell
aws-light apply examples/combined-stack.yaml
```

Wait for the orchestrator and health checker to reconcile. Then open:

```text
http://combined-service.localhost:8080/?demo_token=demo-token
```

Expected response shape:

```json
{
  "service": "combined-service",
  "storage": { "...": "..." },
  "database": { "...": "..." },
  "cpu": [{ "...": "..." }],
  "flaky": { "...": "..." }
}
```

`flaky-service` intentionally returns failures sometimes. A `500` body inside
the `flaky` field is part of the demo; it shows a downstream service failure
being aggregated rather than the whole platform failing.

If `combined-service.localhost` does not resolve in your environment, call the
proxy directly:

```powershell
curl.exe -H "Host: combined-service.localhost" "http://localhost:8080/?demo_token=demo-token"
```

## Dashboard

Open the dashboard at:

```text
http://localhost:8000
```

The dashboard shows:

- platform services,
- managed services and replicas,
- scheduler and autoscaler activity,
- proxy metrics,
- topology nodes and edges.

![Dashboard](images/dashboard.png)

## Stop Or Reset

Stop containers but keep data:

```powershell
docker compose down
```

Remove platform Postgres and storage volumes too:

```powershell
docker compose down -v
```

The second command is destructive for local platform state.

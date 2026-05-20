# Operations And Troubleshooting

This page collects commands used while developing and demoing AWS-Light.

## Platform Commands

Start or rebuild the platform:

```powershell
docker compose up -d --build
```

Stop the platform:

```powershell
docker compose down
```

Stop and delete platform volumes:

```powershell
docker compose down -v
```

Use `down -v` only when you want to remove platform Postgres data and bucket
storage.

## Rebuild One Platform Component

```powershell
docker compose build --no-cache proxy
docker compose up -d --no-deps --force-recreate proxy
```

Replace `proxy` with `control-plane`, `orchestrator`, `health-checker`, or
`autoscaler` as needed.

## Rebuild Example Images

```powershell
docker build -t aws-light/combined-service:latest examples/combined-service
docker build -t aws-light/cpu-service:latest examples/cpu-service
docker build -t aws-light/flaky-service:latest examples/flaky-service
```

If the image tag in the manifest did not change, remove the old workload
container so the orchestrator recreates it from the rebuilt image:

```powershell
docker rm -f <container-name>
```

Find workload containers:

```powershell
docker ps --format "{{.Names}}\t{{.Image}}\t{{.Status}}"
```

## CLI Commands

```powershell
aws-light login --user admin --password admin
aws-light diff examples/combined-stack.yaml
aws-light apply examples/combined-stack.yaml
aws-light status
aws-light status combined-service
aws-light storage ls
aws-light destroy examples/combined-stack.yaml
```

## Logs

```powershell
docker logs --tail 200 aws-light-proxy-1
docker logs --tail 200 aws-light-orchestrator-1
docker logs --tail 200 aws-light-control-plane-1
docker logs --tail 200 aws-light-health-checker-1
docker logs --tail 200 aws-light-autoscaler-1
```

For workload containers, use the generated container name from `docker ps`.

## Troubleshooting

### `invalid demo token`

Use:

```text
http://combined-service.localhost:8080/?demo_token=demo-token
```

or send the header:

```powershell
curl.exe -H "Host: combined-service.localhost" -H "X-Demo-Token: demo-token" http://localhost:8080/
```

Also confirm `combined-service` was recreated after rebuilding its image.

### `external ingress denied`

The target service has `ingress.external: false`. This is expected for internal
services such as `cpu-service` and `flaky-service` in the combined demo.

### `internal ingress denied`

The request included a service token, but the target service did not allow that
source in `ingress.internal.allowFrom`, or the running proxy image is stale.

Rebuild the proxy when proxy code changes:

```powershell
docker compose build --no-cache proxy
docker compose up -d --no-deps --force-recreate proxy
```

### `no healthy replica found`

The proxy has no healthy endpoint for the requested service. Check:

- service container exists,
- health endpoint returns 200,
- health-checker logs,
- routing table in the dashboard.

Right after deleting a workload container, there may be a short reconciliation
window before the replacement is healthy.

### Database connection errors

Check that:

- a `Database` manifest exists,
- the service has `resources.databases` with `access: [connect]`,
- the database container is attached to the service network,
- the service was recreated after changing image or env behavior.

### Stale images

Docker image tags such as `latest` do not automatically trigger a rollout when
the manifest does not change. Rebuild the image and remove the old workload
container, or change the image tag in the manifest and reapply.

## Load Testing

The repository includes `scripts/load_test_proxy.py` for proxy load testing.
Local numbers vary heavily by machine, Docker backend, service behavior, and
concurrency. Treat the script as a comparative tool for changes, not a benchmark
claim.

# Examples

Examples live in `examples/`. Each workload has a Dockerfile and usually a YAML
manifest that declares how AWS-Light should run it.

## Combined Stack

Manifest: `examples/combined-stack.yaml`

This is the main end-to-end demo. It includes:

- `Secrets`: creates `combined-api-token` with value `demo-token`.
- `Bucket`: creates `combined-objects`.
- `Database`: creates `combined-db`.
- `cpu-service`: internal-only service with multiple replicas.
- `flaky-service`: internal-only service that intentionally fails sometimes.
- `combined-service`: the only externally reachable service.

`combined-service` aggregates all platform features in one request:

1. validates the demo token,
2. writes and reads an object through `AWS_LIGHT_STORAGE_URL`,
3. inserts a database row using injected database connection settings,
4. calls `cpu-service` through the proxy several times,
5. calls `flaky-service` through the proxy and returns its response.

Deploy:

```powershell
docker build -t aws-light/cpu-service:latest examples/cpu-service
docker build -t aws-light/flaky-service:latest examples/flaky-service
docker build -t aws-light/combined-service:latest examples/combined-service
aws-light apply examples/combined-stack.yaml
```

Call:

```powershell
curl.exe "http://combined-service.localhost:8080/?demo_token=demo-token"
```

## Internal Call

Manifest: `examples/internal-call.yaml`

This example demonstrates internal ingress policy:

- `internal-frontend` is externally reachable.
- `internal-backend` is not externally reachable.
- `internal-backend` allows internal calls only from `internal-frontend`.

The frontend calls the backend through the proxy with its injected service token.
Direct host access to the backend is denied.

## Database Service

Manifest: `examples/database-service.yaml`

This focused example demonstrates a service bound to an application database.
The orchestrator provisions a dedicated Postgres container, generates connection
credentials, and injects `AWS_LIGHT_DATABASE_*` environment variables into the
service.

## Storage Service

Manifest: `examples/storage-service.yaml`

This example uses the platform storage API rather than local filesystem state.
It demonstrates how a workload can use a declared bucket through the injected
`AWS_LIGHT_STORAGE_URL` and service token.

## Smaller Workloads

These examples are useful for testing scheduler, proxy, health, and autoscaler
behavior:

- `echo-service`: simple request/response service.
- `slow-service`: intentionally slow responses.
- `flaky-service`: intermittent request and health failures.
- `cpu-service`: CPU work endpoint for load and autoscaling demos.
- `secret-service`: reads injected secrets.

Apply any manifest with:

```powershell
aws-light apply examples/<name>.yaml
```

Destroy resources from a manifest with:

```powershell
aws-light destroy examples/<name>.yaml
```

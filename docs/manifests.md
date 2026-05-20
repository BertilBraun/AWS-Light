# Manifests

AWS-Light uses YAML manifests to describe desired platform state. The CLI sends
manifest files to the control plane, which validates and stores desired state.
The orchestrator then reconciles Docker containers, networks, and credentials.

## Common Shape

```yaml
apiVersion: aws-light/v1
kind: Service
metadata:
  name: example-service
spec:
  image: aws-light/example-service:latest
```

Multiple manifests can be placed in one file separated by `---`.

## Service

`Service` describes a Dockerized workload.

```yaml
apiVersion: aws-light/v1
kind: Service
metadata:
  name: combined-service
spec:
  image: aws-light/combined-service:latest
  replicas: 1
  minReplicas: 1
  maxReplicas: 3
  cpuRequest: 0.2
  memoryRequestMb: 160
  port: 8000
  healthCheckPath: /health
  env:
    COMBINED_BUCKET_NAME: combined-objects
  secretRefs:
    - combined-api-token
  resources:
    buckets:
      - name: combined-objects
        access: [read, write]
    databases:
      - name: combined-db
        access: [connect]
  ingress:
    external: true
    internal: false
```

Important fields:

- `image`: Docker image the orchestrator runs.
- `replicas`: desired replica count.
- `minReplicas` / `maxReplicas`: autoscaler bounds.
- `cpuRequest` / `memoryRequestMb`: scheduler resource request.
- `port`: container port the proxy and health checker use.
- `healthCheckPath`: HTTP path used for replica health checks.
- `env`: plain application environment variables.
- `secretRefs`: platform secrets injected as env vars.
- `resources`: explicit bucket and database bindings.
- `ingress`: external and internal proxy policy.

## Secrets

`Secrets` creates one or more platform secrets.

```yaml
apiVersion: aws-light/v1
kind: Secrets
secrets:
  combined-api-token: demo-token
```

A service can reference secrets with `secretRefs`. Secret names are converted to
environment variable names by uppercasing and replacing `-` with `_`.

## Bucket

`Bucket` creates platform object storage.

```yaml
apiVersion: aws-light/v1
kind: Bucket
metadata:
  name: combined-objects
spec:
  versioning: false
```

Workloads access buckets through:

```env
AWS_LIGHT_STORAGE_URL=http://proxy:8080/_aws-light/storage
AWS_LIGHT_SERVICE_TOKEN=...
```

The storage API enforces the service's declared bucket binding and access verbs.

## Database

`Database` creates an application Postgres database container.

```yaml
apiVersion: aws-light/v1
kind: Database
metadata:
  name: combined-db
spec:
  engine: postgres
  version: "16"
  storageMb: 512
```

Bound services receive:

```env
AWS_LIGHT_DATABASE_COMBINED_DB_URL=postgresql://...
AWS_LIGHT_DATABASE_COMBINED_DB_HOST=aws-light-db-combined-db
AWS_LIGHT_DATABASE_COMBINED_DB_PORT=5432
AWS_LIGHT_DATABASE_COMBINED_DB_NAME=combined_db
AWS_LIGHT_DATABASE_COMBINED_DB_USER=combined_db_user
AWS_LIGHT_DATABASE_COMBINED_DB_PASSWORD=...
```

## Ingress Policy

External ingress controls host access through `*.localhost:8080`.

```yaml
ingress:
  external: true
```

Internal ingress controls workload-to-workload calls through the proxy.

```yaml
ingress:
  external: false
  internal:
    allowFrom:
      - combined-service
```

Semantics:

- `external: false`: host-facing proxy calls are denied.
- `internal: false`: internal service-token calls are denied.
- `internal.allowFrom`: only listed source services may call the target.
- `internal: true`: any managed service may call the target.

The target service owns the policy. For example, `cpu-service` decides that
`combined-service` may call it.

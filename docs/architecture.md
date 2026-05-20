# Architecture

AWS-Light is split into a small control plane and several data-plane workers.
The split is deliberately similar to larger container platforms, but sized for a
single machine.

## Component Model

```text
Browser / CLI
  -> control-plane :8000
  -> proxy :8080

control-plane
  -> Postgres for desired state
  -> Redis for events and metrics
  -> storage volume for bucket object data

orchestrator
  -> Postgres desired state
  -> Docker socket
  -> Redis routing table and CPU metrics

proxy
  -> Redis routing table and metrics
  -> service replicas on per-service networks
  -> control-plane storage API for reserved storage paths

health-checker
  -> Redis routing table
  -> service replicas on per-service networks

autoscaler
  -> Redis CPU/RPS metrics
  -> Postgres service desired replica counts
```

## Control Plane

The control plane owns API and state mutation:

- authentication and users,
- service CRUD,
- manifest apply/diff/destroy,
- secrets,
- buckets and objects,
- topology and platform views,
- dashboard and WebSocket event stream.

It does not run the reconcile loop for workload containers.

## Orchestrator

The orchestrator reads desired state and reconciles Docker:

- creates and removes workload containers,
- creates per-service Docker networks,
- attaches proxy and health-checker to service networks,
- provisions one Postgres container per `Database`,
- injects service identity, secrets, storage URL, and database connection env,
- registers replica endpoints in Redis,
- publishes CPU metrics for autoscaling.

## Proxy

The proxy is the controlled HTTP path for services:

- external requests enter via `service-name.localhost:8080`,
- internal requests include `X-AWS-Light-Service-Token`,
- ingress policy is enforced before routing,
- healthy replica endpoints are loaded from Redis,
- proxy metrics are buffered and flushed to Redis,
- platform storage paths under `/_aws-light/storage` are forwarded to the control plane.

Recent performance work keeps the proxy simple while reducing hot-path overhead:

- cached hot store reads,
- batched Redis metric writes,
- raw upstream forwarding with explicit downstream connection close behavior,
- a dedicated metrics buffer instead of per-request Redis pipelines.

## Workload Identity

Every managed service replica receives:

```env
AWS_LIGHT_SERVICE_NAME=combined-service
AWS_LIGHT_SERVICE_TOKEN=...
AWS_LIGHT_PROXY_URL=http://proxy:8080
AWS_LIGHT_STORAGE_URL=http://proxy:8080/_aws-light/storage
```

Internal HTTP calls include the service token. The proxy maps that token back to
the source service and checks the target service's `ingress.internal` policy.

The token is also used by the platform storage API to enforce bucket bindings.

## Storage

Buckets are platform resources. Object data is owned by the control plane storage
service and stored on the `storage-data` Docker volume.

Workloads do not mount bucket directories. They call:

```text
http://proxy:8080/_aws-light/storage/buckets/<bucket>/objects/<key>
```

The proxy reserves `/_aws-light/storage` and forwards those requests to the
control plane. The storage API checks that the calling service is bound to the
bucket with the required `read` or `write` access.

## Databases

Application databases are not stored in the platform Postgres instance.

Each `Database` manifest creates a dedicated Postgres container with a persistent
Docker volume. The database container joins only the networks of services that
declare a `resources.databases` binding with `connect` access.

Database traffic uses the Postgres protocol directly. It does not go through the
HTTP proxy.

## Networks

Each service gets its own Docker network:

```text
aws-light-svc-combined-service
aws-light-svc-cpu-service
aws-light-svc-flaky-service
```

Replicas join their own service network. The proxy and health-checker join every
service network. Databases join the service networks of bound services.

This prevents unrelated workloads from directly calling one another. Supported
HTTP service-to-service traffic flows through the proxy where identity, policy,
routing, and metrics are visible.

## Autoscaling

The proxy records request counts. The orchestrator records container CPU usage.
The autoscaler reads those Redis metrics and updates desired replica counts in
Postgres within each service's `minReplicas` and `maxReplicas` bounds. The
orchestrator applies the new desired state during reconciliation.

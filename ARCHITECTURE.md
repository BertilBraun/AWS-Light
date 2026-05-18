# AWS Light — Microservices Architecture Plan

This document is the definitive spec for migrating AWS Light from a single-process
monolith to a properly separated set of Docker containers. It supersedes the original
`PLAN.md`, which described the monolith that has already been built.

---

## Why this migration

The monolith runs everything — API, reconcile loop, TCP proxy, health checker, autoscaler
— inside one uvicorn process sharing one asyncio event loop. The consequences:

- `docker-py` calls are synchronous/blocking. Creating a container freezes the proxy.
- A crash or `--reload` restart kills the proxy and drops all in-flight connections.
- Shared in-memory state (RoutingTable, EventBus) cannot be accessed from a second process.
- The `RollingController` (container actuation) lives inside the API layer.

---

## What stays the same

Every piece of business logic is preserved unchanged:

- All Pydantic models (`models/`)
- IAM: JWT encode/decode, bcrypt, role hierarchy
- Bin-pack scheduling algorithm
- Autoscaler decision logic (CPU/RPS thresholds)
- Rolling update sequencing
- IaC parser and differ
- Fernet encryption for secrets
- HMAC-SHA256 presigned URLs
- Asyncio TCP proxy piping
- Click CLI
- All `api/` route handlers

---

## Infrastructure additions

### PostgreSQL 16
Persistent source of truth. Survives Redis going down, container restarts, machine
reboots. All desired state and observed state lives here.

### Redis 7
Ephemeral coordination layer. Can be wiped and rebuilt from Postgres + running Docker
containers within one reconcile tick (~5 s). Used for:
- Routing table (which replicas exist and whether they're healthy)
- Per-service request counters (proxy writes, autoscaler reads)
- Per-service CPU metrics (orchestrator writes, autoscaler reads)
- Event stream (WebSocket dashboard pub/sub)

If Redis goes down the proxy returns 503 until the orchestrator repopulates it.
No desired state, no secrets, no user data is lost.

### Docker networks

```
aws-light-internal  (bridge)
    postgres, redis, control-plane, orchestrator, autoscaler
    — service-to-service traffic

aws-light-data  (bridge, name fixed so orchestrator can attach managed containers)
    proxy, health-checker, [managed containers created at runtime]
    — proxy and health-checker reach managed containers by Docker network IP
```

The proxy and health-checker sit on both networks.

### Docker volumes

```
postgres-data   — Postgres data directory
storage-data    — bucket directories and object files (control-plane only)
```

No shared filesystem for application state. Postgres replaces the JSON files.

---

## Service specifications

### control-plane

**Does:** REST API (all routes), WebSocket /ws dashboard, IaC apply/diff/destroy,
desired state writes (services, users, secrets, deployments).

**Does NOT:** touch Docker, make routing decisions, start long-running actuation tasks.

**Handoff for deployments:** `POST /api/v1/deployments` writes a `RolloutState` with
`status=PENDING` to Postgres and returns immediately. The orchestrator watches for
pending rollouts and executes them.

**Networks:** `aws-light-internal`

**Exposes:** `:8000` to host

**Mounts:** `storage-data:/app/data/storage`

---

### orchestrator

**Does:**
- Reconcile loop every 5 s: reads all `ServiceState` rows from Postgres, compares
  desired replica count and image against running Docker containers, creates or removes
  containers.
- After creating a container: reads its IP on `aws-light-data` via `docker inspect`,
  writes endpoint to Redis routing table.
- After removing a container: removes endpoint from Redis routing table.
- Image drift detection: if a running replica's stored image differs from `spec.image`,
  remove it so a fresh one is created next tick.
- Rolling deployments: polls Postgres for `RolloutState` rows with `status=PENDING`,
  executes the surge/unavailable sequencing (moved from `RollingController`).
- Orphan cleanup on startup: removes Docker containers with `aws-light.managed=true`
  not present in Postgres.
- Routing table bootstrap on startup: reads Postgres, inspects all managed containers,
  repopulates Redis routing table from scratch. This recovers from a Redis wipe.
- CPU stats: every 30 s, calls `container.stats()` for each managed container, writes
  avg per-service CPU% to Redis. These calls are blocking (docker-py limitation) but
  now only block the orchestrator, not the proxy or API.

**Does NOT:** serve HTTP, make autoscale decisions, probe health.

**Networks:** `aws-light-internal` (for Postgres + Redis)

**Mounts:** `/var/run/docker.sock:/var/run/docker.sock`

---

### proxy

**Does:**
- Asyncio TCP server on `:8080`.
- Per request: reads `Host` header → service name → looks up healthy endpoints from
  Redis → round-robin selects one → opens TCP connection to `{container_ip}:{container_port}`
  on the `aws-light-data` network → pipes bytes bidirectionally.
- `INCR rps:{service_name}` in Redis on each successfully proxied request.
- Returns HTTP 503 JSON if no healthy replicas.

**Does NOT:** touch Postgres, touch Docker.

**Networks:** `aws-light-internal` (Redis) + `aws-light-data` (managed containers)

**Exposes:** `:8080` to host

---

### health-checker

**Does:**
- Every 10 s: reads all endpoints from Redis routing table.
- HTTP GET to `{container_ip}:{container_port}{health_check_path}` for each replica.
- 3 consecutive failures → sets `healthy: false` on that endpoint in Redis.
- Recovery → sets `healthy: true`.
- Publishes `health_check.failed` events to Redis Streams.

**Does NOT:** touch Postgres, touch Docker.

**Networks:** `aws-light-internal` (Redis) + `aws-light-data` (managed containers)

---

### autoscaler

**Does:**
- Every 30 s: reads `cpu:{service}` and `rps:{service}` from Redis.
- Applies thresholds (CPU > 70% or rps > 100 → scale up; CPU < 20% and rps < 10
  for 3 consecutive checks → scale down).
- Writes new `spec.replicas` directly to Postgres (`ServiceState`).
- Orchestrator picks this up on its next reconcile tick.
- Publishes `autoscale.triggered` to Redis Streams.

**Does NOT:** touch Docker, serve HTTP.

**Networks:** `aws-light-internal`

---

## Postgres schema

All tables follow the same pattern: a text primary key and a JSONB column holding the
full Pydantic model. This keeps `PostgresStore[T]` generic and requires no migrations
when models grow new optional fields.

```sql
CREATE TABLE IF NOT EXISTS services (
    key         TEXT PRIMARY KEY,
    data        JSONB        NOT NULL,
    updated_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS users (
    key         TEXT PRIMARY KEY,
    data        JSONB        NOT NULL,
    updated_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS secrets (
    key         TEXT PRIMARY KEY,
    data        JSONB        NOT NULL,
    updated_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS deployments (
    key         TEXT PRIMARY KEY,
    data        JSONB        NOT NULL,
    updated_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
```

Tables are created by the control-plane on startup with `CREATE TABLE IF NOT EXISTS`.
No migration tooling needed at this stage.

---

## Redis schema

```
routing:{service_name}          JSON string: list[ReplicaEndpoint]
                                Set by orchestrator; read by proxy and health-checker.

rps:{service_name}              Integer counter (INCR).
                                Set by proxy; read by autoscaler (delta per interval).

cpu:{service_name}              Float string.
                                Set by orchestrator; read by autoscaler.

events                          Redis Stream (XADD / XREAD BLOCK).
                                Written by all services; read by control-plane WebSocket.
                                Capped at 1 000 entries (MAXLEN).
```

---

## Container networking for managed services

**Before (monolith):** Each managed container binds a host port (20000, 20001, …).
The proxy connects via `localhost:20001`. Requires global port allocation.

**After:** Managed containers join `aws-light-data` with no host port binding. The
orchestrator reads the container's assigned IP on that network from `docker inspect`
and stores it in Redis:

```python
container = docker_client.containers.run(image, ..., network="aws-light-data")
container.reload()
ip = container.attrs["NetworkSettings"]["Networks"]["aws-light-data"]["IPAddress"]
# store {replica_id, host=ip, port=container_port, healthy=True} in Redis
```

The proxy and health-checker, also on `aws-light-data`, connect to that IP directly.
No port counter, no host port binding, no port conflicts.

**Race condition:** Docker may not assign the IP immediately. The orchestrator retries
`docker inspect` up to 10 times with 200 ms sleep until the IP is non-empty.

---

## Directory structure

```
aws-light/
├── docker-compose.yml
├── .env.example
├── ARCHITECTURE.md               ← this file
│
├── aws_light/                    ← single Python package installed into every image
│   ├── models/                   ← unchanged
│   ├── store/
│   │   ├── postgres_store.py     ← NEW  replaces json_store in production
│   │   └── json_store.py         ← KEPT for tests (no Postgres needed in tests)
│   ├── events/
│   │   └── redis_event_bus.py    ← NEW  replaces in-memory EventBus
│   ├── proxy/
│   │   ├── redis_routing_table.py← NEW  replaces in-memory RoutingTable
│   │   ├── load_balancer.py      ← unchanged
│   │   ├── health_checker.py     ← MODIFIED: probe container_ip:port not localhost:port
│   │   └── proxy_server.py       ← MODIFIED: Redis INCR for RPS; use RedisRoutingTable
│   ├── compute/
│   │   ├── docker_client.py      ← MODIFIED: returns container IP; no host port param
│   │   ├── node_manager.py       ← unchanged
│   │   ├── scheduler.py          ← unchanged
│   │   └── orchestrator.py       ← MODIFIED: uses Redis routing table; publishes CPU;
│   │                                          executes pending rollouts (moved from
│   │                                          RollingController); no port counter
│   ├── deployment/
│   │   └── rolling_controller.py ← REMOVED (logic moves into orchestrator)
│   ├── autoscaler/
│   │   ├── metrics_collector.py  ← MODIFIED: reads cpu/rps from Redis, not Docker
│   │   └── autoscaler.py         ← unchanged
│   ├── iac/                      ← unchanged
│   ├── iam/                      ← unchanged
│   ├── secrets/                  ← unchanged (just uses PostgresStore instead of JsonStore)
│   ├── storage/                  ← unchanged
│   ├── dashboard/
│   │   └── event_bus.py          ← REMOVED (replaced by redis_event_bus.py)
│   └── config.py                 ← MODIFIED: add DATABASE_URL, REDIS_URL; remove
│                                              data_directory, replica_port_start
│
├── services/
│   ├── control-plane/
│   │   ├── Dockerfile
│   │   └── main.py               ← FastAPI app + lifespan (no background loops)
│   ├── orchestrator/
│   │   ├── Dockerfile
│   │   └── main.py               ← reconcile loop + rolling update executor
│   ├── proxy/
│   │   ├── Dockerfile
│   │   └── main.py               ← asyncio TCP server entry point
│   ├── health-checker/
│   │   ├── Dockerfile
│   │   └── main.py               ← HTTP probe loop entry point
│   └── autoscaler/
│       ├── Dockerfile
│       └── main.py               ← autoscale loop entry point
│
├── cli/
│   └── main.py                   ← unchanged
│
├── examples/
│   ├── secret-service/
│   ├── echo-service/
│   ├── slow-service/
│   ├── flaky-service/
│   ├── cpu-service/
│   └── storage-service/
│
└── tests/
    ├── conftest.py               ← MODIFIED: use JsonStore + in-memory stubs (no Postgres
    │                                          or Redis needed in unit tests)
    └── ...                       ← all other test files unchanged
```

---

## `docker-compose.yml`

```yaml
networks:
  internal:
    driver: bridge
  data:
    name: aws-light-data
    driver: bridge

volumes:
  postgres-data:
  storage-data:

services:

  postgres:
    image: postgres:16-alpine
    networks: [internal]
    volumes:
      - postgres-data:/var/lib/postgresql/data
    environment:
      POSTGRES_DB: awslight
      POSTGRES_USER: awslight
      POSTGRES_PASSWORD: awslight
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U awslight"]
      interval: 2s
      timeout: 3s
      retries: 15

  redis:
    image: redis:7-alpine
    networks: [internal]
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 2s
      timeout: 3s
      retries: 10

  control-plane:
    build:
      context: .
      dockerfile: services/control-plane/Dockerfile
    networks: [internal]
    ports: ["8000:8000"]
    volumes:
      - storage-data:/app/data/storage
    env_file: .env
    depends_on:
      postgres: { condition: service_healthy }
      redis:    { condition: service_healthy }
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/healthz"]
      interval: 3s
      retries: 10

  orchestrator:
    build:
      context: .
      dockerfile: services/orchestrator/Dockerfile
    networks: [internal]
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
    env_file: .env
    depends_on:
      postgres: { condition: service_healthy }
      redis:    { condition: service_healthy }

  proxy:
    build:
      context: .
      dockerfile: services/proxy/Dockerfile
    networks: [internal, data]
    ports: ["8080:8080"]
    env_file: .env
    depends_on:
      redis: { condition: service_healthy }

  health-checker:
    build:
      context: .
      dockerfile: services/health-checker/Dockerfile
    networks: [internal, data]
    env_file: .env
    depends_on:
      redis: { condition: service_healthy }

  autoscaler:
    build:
      context: .
      dockerfile: services/autoscaler/Dockerfile
    networks: [internal]
    env_file: .env
    depends_on:
      postgres: { condition: service_healthy }
      redis:    { condition: service_healthy }
```

---

## `.env.example`

```env
DATABASE_URL=postgresql://awslight:awslight@postgres:5432/awslight
REDIS_URL=redis://redis:6379

JWT_SECRET=change-me-in-production
ENCRYPTION_KEY=

DEFAULT_ADMIN_USERNAME=admin
DEFAULT_ADMIN_PASSWORD=admin

NODE_COUNT=10
NODE_CPU_CAPACITY=0.5
NODE_MEMORY_CAPACITY_MB=512

AUTOSCALER_INTERVAL_SECONDS=30
HEALTH_CHECK_INTERVAL_SECONDS=10

DOCKER_NETWORK=aws-light-data
```

---

## New abstractions to implement

### `store/postgres_store.py`

Replaces `JsonStore`. Identical interface — same five methods. Backed by asyncpg.

```python
class PostgresStore(Generic[ModelType]):
    def __init__(self, pool: asyncpg.Pool, table: str, model_class: type[ModelType]) -> None: ...
    async def get(self, identifier: str) -> ModelType | None: ...
    async def put(self, identifier: str, item: ModelType) -> None: ...   # upsert
    async def list(self) -> list[ModelType]: ...
    async def delete(self, identifier: str) -> None: ...
    async def exists(self, identifier: str) -> bool: ...
```

Each method opens a connection from the pool (no manual transaction needed for
single-statement operations). `put` uses `INSERT ... ON CONFLICT (key) DO UPDATE`.

### `events/redis_event_bus.py`

Replaces in-memory `EventBus`. Backed by a single Redis Stream key `events`.

```python
class RedisEventBus:
    async def publish(self, event: WebSocketEvent) -> None:
        # XADD events MAXLEN ~ 1000 * kind "..." payload "..."
    async def subscribe(self) -> asyncio.Queue[WebSocketEvent]:
        # starts a background XREAD BLOCK task; feeds a local asyncio.Queue
    async def unsubscribe(self, queue: asyncio.Queue) -> None: ...
    async def get_recent_events(self) -> list[WebSocketEvent]:
        # XRANGE events - + COUNT 100
```

The `subscribe` implementation spawns one background asyncio task per subscriber
that loops on `XREAD BLOCK 30000 STREAMS events {last_id}`. When it gets entries it
puts them onto the local queue. `unsubscribe` cancels that task.

### `proxy/redis_routing_table.py`

Replaces in-memory `RoutingTable`. Same interface. Backed by one Redis key per service
holding a JSON-encoded `list[ReplicaEndpoint]`.

```python
class RedisRoutingTable:
    async def update_service(self, service_name: str, endpoints: list[ReplicaEndpoint]) -> None:
        # SET routing:{service_name} json
    async def get_endpoints(self, service_name: str) -> list[ReplicaEndpoint]:
        # GET routing:{service_name}
    async def set_healthy(self, replica_id: str, healthy: bool) -> None:
        # GET → deserialise → update flag → SET  (atomic via Lua script)
    async def remove_service(self, service_name: str) -> None:
        # DEL routing:{service_name}
    async def all_service_names(self) -> list[str]:
        # KEYS routing:*  (acceptable at this scale)
```

---

## Service entry points

Each `services/*/main.py` imports from `aws_light.*` and runs only its slice.

### `services/control-plane/main.py`
```python
# Creates asyncpg pool + Redis client
# Runs CREATE TABLE IF NOT EXISTS for all four tables
# Seeds default admin user
# Creates FastAPI app with all API routers
# Subscribes to Redis Stream for WebSocket /ws
# No background loops — purely request/response
```

### `services/orchestrator/main.py`
```python
# Creates asyncpg pool + Redis client + DockerClient
# Bootstraps aws-light-data network (docker.networks.create if absent)
# Bootstraps Redis routing table from Postgres + docker inspect
# Removes orphan containers
# Runs three asyncio loops:
#   _reconcile_loop()      every 5 s
#   _rollout_loop()        every 2 s (polls for PENDING RolloutState rows)
#   _cpu_stats_loop()      every 30 s
```

### `services/proxy/main.py`
```python
# Creates Redis client
# Starts asyncio TCP server on :8080
# Each connection: read Host header → Redis lookup → pipe
```

### `services/health-checker/main.py`
```python
# Creates Redis client
# Runs _health_check_loop() every 10 s
```

### `services/autoscaler/main.py`
```python
# Creates asyncpg pool + Redis client
# Runs _autoscale_loop() every 30 s
```

---

## Dockerfile (shared pattern)

All five service Dockerfiles follow the same structure. Only the last two lines differ.

```dockerfile
FROM python:3.10-slim
WORKDIR /app

# Install the aws_light package (all business logic lives here)
COPY aws_light/ aws_light/
COPY pyproject.toml .
RUN pip install -e ".[prod]"

# Service-specific entry point
COPY services/control-plane/main.py .
CMD ["python", "main.py"]
```

`pyproject.toml` will gain a `[prod]` extras group:
```toml
[project.optional-dependencies]
prod = ["asyncpg>=0.29", "redis[asyncio]>=5.0"]
```

Tests keep using `JsonStore` and the in-memory stubs so they never need a running
Postgres or Redis.

---

## Per-file change inventory

| File | Change |
|---|---|
| `config.py` | Add `DATABASE_URL`, `REDIS_URL`; remove `data_directory`, `replica_port_start` |
| `store/postgres_store.py` | **NEW** — `PostgresStore[T]`, asyncpg-backed |
| `store/json_store.py` | Kept unchanged; used only in tests |
| `events/redis_event_bus.py` | **NEW** — Redis Streams backed pub/sub |
| `dashboard/event_bus.py` | **REMOVED** |
| `proxy/redis_routing_table.py` | **NEW** — Redis-backed routing table |
| `proxy/routing_table.py` | **REMOVED** |
| `proxy/health_checker.py` | Change probe URL from `localhost:{host_port}` to `{container_ip}:{container_port}` |
| `proxy/proxy_server.py` | Use `RedisRoutingTable`; replace `_request_counts` dict with `INCR rps:{svc}` |
| `compute/docker_client.py` | `create_container()` removes `host_port` param, returns `(container_id, container_ip)` after polling `docker inspect` for IP; add `get_container_ip()` helper |
| `compute/orchestrator.py` | Use `RedisRoutingTable`; remove port counter; add CPU stats publisher; add `_rollout_loop()` (rolling update execution moved from `RollingController`) |
| `compute/node_manager.py` | Unchanged |
| `compute/scheduler.py` | Unchanged |
| `deployment/rolling_controller.py` | **REMOVED** — logic absorbed into orchestrator |
| `autoscaler/metrics_collector.py` | Read `cpu:{svc}` and `rps:{svc}` from Redis; no Docker socket |
| `autoscaler/autoscaler.py` | Unchanged |
| `models/service.py` | Add `container_ip: str = ""` to `ReplicaState`; remove `host_port` (no longer needed) |
| `models/` everything else | Unchanged |
| `api/deployments.py` | `POST /api/v1/deployments` writes `RolloutState(status=PENDING)` to Postgres and returns; no longer starts a task |
| `api/` everything else | Unchanged |
| `iac/applier.py` | Unchanged |
| `iac/parser.py` | Unchanged |
| `iam/` | Unchanged |
| `secrets/` | Unchanged |
| `storage/` | Unchanged |
| `main.py` | **REMOVED** — replaced by five `services/*/main.py` entry points |
| `dependencies.py` | **REMOVED** — each service wires its own dependencies |
| `tests/conftest.py` | Keep using `JsonStore` + in-memory `EventBus` stubs; inject via service-specific test fixtures |
| `tests/*.py` | All pass unchanged (they test business logic, not storage backends) |
| `docker-compose.yml` | **NEW** |
| `services/*/Dockerfile` | **NEW** (×5) |
| `services/*/main.py` | **NEW** (×5) |
| `.env.example` | **NEW** |

---

## Implementation phases

Work in this order. Each phase leaves the system in a testable state.

### Phase 1 — New backends (no service split yet)

1. Add `asyncpg` and `redis[asyncio]` to `pyproject.toml` optional deps.
2. Write `store/postgres_store.py` with the same five-method interface as `JsonStore`.
3. Write `events/redis_event_bus.py`.
4. Write `proxy/redis_routing_table.py`.
5. Update `config.py` with `DATABASE_URL` and `REDIS_URL`.

**Test:** Unit tests still pass (they use `JsonStore`). Write integration tests for
`PostgresStore` using a local Postgres (or `pytest-postgresql`).

### Phase 2 — Docker client and managed container networking

1. Remove `host_port` parameter from `docker_client.create_container()`.
2. Add IP-polling logic: after `containers.run()`, retry `container.reload()` until
   `Networks["aws-light-data"]["IPAddress"]` is non-empty (max 10 × 200 ms).
3. Add `container_ip: str = ""` to `ReplicaState`; remove `host_port`.
4. Update orchestrator to store `container_ip` and register it in `RedisRoutingTable`.
5. Update `health_checker.py` to probe `container_ip:container_port`.
6. Update `proxy_server.py` to use `RedisRoutingTable` and `INCR rps:*`.
7. Remove port counter from orchestrator.

**Test:** `docker compose up` with just `redis` + the monolith (temporarily wire
`RedisRoutingTable` into the existing `main.py`). Verify that `apply` creates a
container, the routing table in Redis is populated, and `curl secret-service.localhost:8080`
works.

### Phase 3 — Move RollingController into orchestrator

1. Extract the rollout execution logic from `rolling_controller.py` into
   `orchestrator.py` as `_rollout_loop()` (polls Postgres every 2 s for
   `RolloutState` rows with `status = PENDING`).
2. Change `api/deployments.py` to write `RolloutState(status=PENDING)` and return
   immediately.
3. Delete `deployment/rolling_controller.py`.
4. Delete `dashboard/event_bus.py`.

### Phase 4 — Autoscaler reads Redis metrics

1. Update `metrics_collector.py` to read from Redis (`GET cpu:{svc}`, cumulative `GET rps:{svc}`)
   instead of calling Docker directly.
2. Add `_cpu_stats_loop()` to orchestrator (every 30 s, calls `container.stats()`,
   writes `SET cpu:{svc} {avg}`).

### Phase 5 — Split into service containers

1. Write `services/control-plane/main.py`: FastAPI app, Postgres pool + Redis client
   in lifespan, no background loops, `GET /healthz` endpoint.
2. Write `services/orchestrator/main.py`: Postgres pool + Redis client + DockerClient,
   three async loops (reconcile, rollout, CPU stats), routing table bootstrap on startup.
3. Write `services/proxy/main.py`: Redis client, TCP server, nothing else.
4. Write `services/health-checker/main.py`: Redis client, probe loop.
5. Write `services/autoscaler/main.py`: Postgres pool + Redis client, scale loop.
6. Write five Dockerfiles.
7. Write `docker-compose.yml`.
8. Write `.env.example`.
9. Delete `main.py` and `dependencies.py`.

### Phase 6 — Verification

Run the full end-to-end sequence:

```bash
docker compose up --build

# In another terminal:
aws-light login --user admin --password admin
aws-light apply examples/secret-service.yaml

# Wait ~5 s for reconcile tick
curl http://secret-service.localhost:8080/
# {"my_secret": "hello-from-secret", "another_secret": "second-secret-value"}

# Kill Redis; wait 10 s; restart Redis
docker compose restart redis
# Within ~5 s of Redis coming back, proxy recovers automatically

# Open http://localhost:8000 — dashboard shows live nodes and services
```

---

## What the control plane is and is not

The user's instinct was right: the original monolith had the control plane doing actuation
(the `RollingController`). After this migration the control plane is strictly:

- **REST API surface** — auth, CRUD operations, IaC parsing, desired state writes
- **WebSocket gateway** — reads Redis Stream and forwards to browser
- **Nothing else**

It maps to `kube-apiserver` in Kubernetes terms. It never touches Docker. It never
starts a container. It writes a `RolloutState(status=PENDING)` row to Postgres and
stops. The orchestrator — which maps to `kube-controller-manager` — is the only thing
that ever calls `docker.containers.run()`.

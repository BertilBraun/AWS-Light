# Proxy Performance Design

## Goal

Increase proxy request throughput by removing avoidable hot-path I/O and connection setup. The first implementation pass targets:

- reusable upstream HTTP connections through a pooled async HTTP client,
- cached proxy metadata reads through a generic TTL store wrapper,
- batched Redis metric writes.

Request body streaming is out of scope for this pass. WebSocket proxying remains on the existing raw socket path.

## Current Constraints

The proxy currently handles each normal HTTP request with raw `asyncio` streams. It opens a new upstream TCP connection per request, rewrites the request headers to `Connection: close`, reads the full upstream response into memory, forwards it to the client, and closes the upstream connection.

The authorization path reads service metadata from `service_store` on many requests. Internal service-token handling can also list and read secrets before the request reaches load balancing.

Proxy metrics are written to Redis per request. Each request increments the autoscaler RPS key and executes a Redis pipeline for dashboard counters and time-series data.

## Architecture

### Generic TTL Store Cache

Add a generic `TTLStoreCache[T]` that implements the existing `AnyStore[T]` protocol and wraps any store implementation.

Behavior:

- `get(identifier)` returns a cached item while it is within TTL.
- `exists(identifier)` returns `True` when a non-expired cached item exists. Otherwise it delegates to the wrapped store. It does not cache negative existence results in this pass.
- `put(identifier, item)` delegates to the wrapped store and updates or invalidates the cached key.
- `delete(identifier)` delegates to the wrapped store and invalidates the cached key.
- `list()` delegates to the wrapped store without generic caching.
- The cache has a max size and evicts least-recently-used entries when full.

The proxy runtime should wrap `service_store` and `secret_store` in `services/proxy/main.py`. Other services continue using plain `PostgresStore`, so control-plane and orchestrator reads retain their current freshness semantics.

Suggested defaults:

- TTL: 5 seconds.
- Max entries: 1024.

### Pooled HTTP Forwarding

Add one long-lived `httpx.AsyncClient` owned by `ProxyServer` for normal HTTP forwarding.

One async client is enough for all replicas. `httpx` pools reusable connections internally by origin, so each `scheme://host:port` gets its own reusable connection set under the same client.

The client should be configured with explicit connection limits and timeouts. Suggested initial values:

- `max_connections`: 1000,
- `max_keepalive_connections`: 200,
- `keepalive_expiry`: 30 seconds,
- connect timeout: 5 seconds,
- read timeout: 30 seconds,
- write timeout: 30 seconds,
- pool timeout: 5 seconds.

Normal HTTP request forwarding should:

- keep the current request-body buffering behavior based on `Content-Length`,
- preserve fallback behavior across healthy replica candidates,
- rewrite hop-by-hop and host headers consistently with the current proxy,
- stream the upstream response body to the downstream client after response headers are available,
- record the upstream status and duration after forwarding completes.

WebSocket upgrades keep the existing raw socket path and `_pipe_stream` behavior because the normal HTTP client is not the right abstraction for bidirectional tunnels.

### Batched Redis Metrics

Change `_record_proxy_result` so it records into local counters and schedules Redis writes through a periodic flush loop instead of executing a Redis pipeline per request.

The flush loop should run while the proxy is started and flush every 1 second by default.

The flush should preserve current Redis key semantics:

- `proxy:requests:total`,
- `proxy:responses:status`,
- `proxy:requests:service`,
- `proxy:last_duration_ms:{service}`,
- `proxy:ts:requests:{bucket}`,
- `proxy:ts:status:{bucket}`,
- `proxy:ts:latency_sum:{bucket}`,
- `proxy:ts:latency_count:{bucket}`,
- `proxy:ts:errors:{bucket}`,
- `proxy:failures`,
- `rps:{service}`.

Failure websocket events can remain immediate so failures still surface quickly. Aggregated traffic activity can keep the current 10-second dashboard event window.

On shutdown, `ProxyServer.stop()` should flush pending metrics before closing the Redis client.

## Data Flow

1. Client connects to the proxy and sends an HTTP request.
2. Proxy reads headers and a `Content-Length` request body, rejecting transfer-encoded requests as it does today.
3. Proxy resolves service ingress policy through the cached store wrapper.
4. Internal requests resolve service-token ownership through cached secret/service lookups.
5. Proxy asks the existing balancer for healthy replica candidates.
6. For normal HTTP, proxy forwards through the shared `httpx.AsyncClient`.
7. The upstream response headers are rewritten and sent to the client.
8. Response body chunks stream from upstream to client.
9. Proxy records result counters in memory.
10. Metrics flush periodically writes the aggregated deltas to Redis.

## Error Handling

Keep existing status behavior for all paths that have not started sending a downstream response:

- no healthy replica: 503,
- upstream unreachable across all candidates: 502,
- upstream timeout: 504,
- bad request or incomplete request body: 400,
- denied ingress: 403,
- transfer encoding on request: 501.

If one candidate fails to connect or respond, the proxy should try the next candidate where doing so is safe before any response has been sent to the client.

Once response headers have been sent to the downstream client, later upstream stream failures should close the downstream response and record a failure metric. They cannot be converted into a clean 502 without violating HTTP response framing.

## Testing

Add focused tests for:

- TTL cache hit, miss, expiry, LRU eviction, `put()` update, and `delete()` invalidation.
- Proxy external and internal ingress still using cached service metadata.
- One shared upstream client can forward to multiple replica origins.
- HTTP forwarding preserves status, response headers, and body.
- Fallback replica attempts still work before response headers are sent.
- Batched Redis metrics aggregate multiple requests into fewer pipeline executions.
- Pending metrics flush during proxy shutdown.
- WebSocket behavior continues to use raw socket forwarding.

The existing proxy tests should remain green, with metric assertions updated for explicit flush points where needed.

## Out of Scope

- Streaming request bodies.
- HTTP/2 upstream forwarding.
- A custom hand-written upstream connection pool.
- Generic `list()` caching for all stores.
- Cross-process cache invalidation.

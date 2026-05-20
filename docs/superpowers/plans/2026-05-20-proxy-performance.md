# Proxy Performance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Increase proxy throughput by caching store reads, forwarding normal HTTP through a pooled async client, and batching Redis metric writes.

**Architecture:** Add a generic TTL/LRU store wrapper that implements `AnyStore[T]`, wire it only into the proxy runtime, and keep the underlying `PostgresStore` unchanged. Refactor `ProxyServer` so normal HTTP uses one long-lived `httpx.AsyncClient` while WebSocket upgrades keep the existing raw stream tunnel. Record proxy metrics into local counters and flush aggregated deltas to Redis on a short interval and shutdown.

**Tech Stack:** Python 3.10+, asyncio, httpx, asyncpg-backed stores, redis asyncio client, pytest.

---

## File Structure

- Create `aws_light/store/cached_store.py`: generic TTL/LRU cache wrapper for any `AnyStore[T]`.
- Modify `services/proxy/main.py`: wrap service and secret stores with `TTLStoreCache` for proxy process only.
- Modify `aws_light/proxy/proxy_server.py`: add pooled `httpx.AsyncClient`, HTTP streaming forwarding, batched Redis metrics, and shutdown cleanup.
- Modify `tests/test_proxy_server.py`: update metric tests for explicit flush and add pooled forwarding behavior coverage.
- Create `tests/test_cached_store.py`: focused cache wrapper tests.

## Task 1: TTL Store Cache

- [ ] Write tests in `tests/test_cached_store.py` for `get()` hit/miss/expiry, LRU eviction, `put()` cache update, `delete()` invalidation, `exists()` cache hit, and uncached `list()`.
- [ ] Run `pytest tests/test_cached_store.py -v` and verify failures because `aws_light.store.cached_store` does not exist.
- [ ] Implement `TTLStoreCache` in `aws_light/store/cached_store.py` with monotonic clock injection for tests.
- [ ] Run `pytest tests/test_cached_store.py -v` and verify it passes.

## Task 2: Batched Redis Metrics

- [ ] Update proxy metric tests so `_record_proxy_result()` does not write Redis until `_flush_proxy_metrics()` is called.
- [ ] Add a shutdown-flush test for pending metrics.
- [ ] Run targeted metric tests and verify failures.
- [ ] Implement local metric counters, a periodic flush task, `_flush_proxy_metrics()`, and shutdown flushing.
- [ ] Run targeted metric tests and verify they pass.

## Task 3: Pooled HTTP Forwarding

- [ ] Add proxy tests for normal HTTP forwarding through an injected fake upstream client, including status/body/header forwarding and fallback to a second endpoint before response headers are sent.
- [ ] Run targeted proxy forwarding tests and verify failures.
- [ ] Implement `httpx.AsyncClient` ownership in `ProxyServer`, normal HTTP forwarding through that client, response header rewriting, response body streaming, and client close on shutdown.
- [ ] Run targeted proxy forwarding tests and verify they pass.

## Task 4: Proxy Runtime Wiring

- [ ] Modify `services/proxy/main.py` to wrap `service_store` and `secret_store` with `TTLStoreCache`.
- [ ] Run `pytest tests/test_proxy_server.py tests/test_cached_store.py -v`.
- [ ] Run the full test suite if targeted tests pass.

## Task 5: Review and Commit

- [ ] Inspect `git diff`.
- [ ] Run formatting/linting if needed.
- [ ] Run final verification commands.
- [ ] Commit only files changed for this implementation.

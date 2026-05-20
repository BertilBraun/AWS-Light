from __future__ import annotations

import asyncio

from aws_light.dashboard.event_bus import EventBus
from aws_light.models.service import (
    InternalIngressPolicy,
    ServiceIngressSpec,
    ServiceSpec,
    ServiceState,
)
from aws_light.proxy.proxy_server import (
    _403_INTERNAL_RESPONSE,
    _403_RESPONSE,
    _502_RESPONSE,
    _503_RESPONSE,
    _504_RESPONSE,
    ProxyServer,
    UpstreamResponseError,
    _extract_request_content_length,
    _extract_response_status,
    _has_transfer_encoding,
    _read_full_response,
    _read_http_head,
    _rewrite_request_headers,
    _rewrite_response_headers,
)
from aws_light.proxy.routing_table import ReplicaEndpoint


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, int | str] = {}
        self.hashes: dict[str, dict[str, int]] = {}
        self.pipeline_execute_count = 0

    def pipeline(self) -> FakePipeline:
        return FakePipeline(self)


class FakePipeline:
    def __init__(self, redis: FakeRedis) -> None:
        self.redis = redis
        self.commands: list[tuple[str, str, str | None, int | str]] = []

    def incr(self, key: str) -> None:
        self.commands.append(("incr", key, None, 1))

    def incrby(self, key: str, amount: int) -> None:
        self.commands.append(("incr", key, None, amount))

    def hincrby(self, key: str, field: str, amount: int) -> None:
        self.commands.append(("hincrby", key, field, amount))

    def set(self, key: str, value: str) -> None:
        self.commands.append(("set", key, None, value))

    async def execute(self) -> None:
        self.redis.pipeline_execute_count += 1
        for command, key, field, value in self.commands:
            if command == "incr":
                self.redis.values[key] = self.redis.values.get(key, 0) + int(value)
            elif command == "hincrby" and field is not None:
                bucket = self.redis.hashes.setdefault(key, {})
                bucket[field] = bucket.get(field, 0) + int(value)
            elif command == "set":
                self.redis.values[key] = str(value)


class FakeServiceStore:
    def __init__(self, services: dict[str, ServiceState]) -> None:
        self.services = services

    async def get(self, identifier: str) -> ServiceState | None:
        return self.services.get(identifier)


class FakeSecretsManager:
    def __init__(self, secrets: dict[str, str]) -> None:
        self.secrets = secrets

    async def list_secret_names(self) -> list[str]:
        return list(self.secrets)

    async def get_secret(self, name: str) -> str | None:
        return self.secrets.get(name)


class FakeBalancer:
    def __init__(self) -> None:
        self.requested_services: list[str] = []

    async def healthy_replicas_for_request(self, service_name: str) -> list[object]:
        self.requested_services.append(service_name)
        raise AssertionError("Policy allowed request to reach load balancing")


class StaticBalancer:
    def __init__(self, endpoints: list[ReplicaEndpoint]) -> None:
        self.endpoints = endpoints
        self.requested_services: list[str] = []

    async def healthy_replicas_for_request(self, service_name: str) -> list[ReplicaEndpoint]:
        self.requested_services.append(service_name)
        return self.endpoints


class FakeWriter:
    def __init__(self) -> None:
        self.data = b""

    def write(self, data: bytes) -> None:
        self.data += data

    async def drain(self) -> None:
        return None


class CloseAwareFakeWriter(FakeWriter):
    def __init__(self) -> None:
        super().__init__()
        self.closed = False
        self.waited_closed = False

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        self.waited_closed = True


class FakeUpstreamWriter(FakeWriter):
    def close(self) -> None:
        return None

    async def wait_closed(self) -> None:
        return None


class FakeOpenConnection:
    def __init__(self, responses: list[bytes | Exception]) -> None:
        self.responses = list(responses)
        self.connections: list[tuple[str, int]] = []
        self.writers: list[FakeUpstreamWriter] = []

    async def __call__(self, host: str, port: int):  # type: ignore[no-untyped-def]
        self.connections.append((host, port))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        writer = FakeUpstreamWriter()
        self.writers.append(writer)
        return _reader_with(response), writer


def _reader_with(data: bytes) -> asyncio.StreamReader:
    reader = asyncio.StreamReader()
    reader.feed_data(data)
    reader.feed_eof()
    return reader


def _service_state(
    name: str,
    *,
    external: bool = False,
    internal: bool = False,
    allow_from: list[str] | None = None,
) -> ServiceState:
    return ServiceState(
        spec=ServiceSpec(
            name=name,
            image=f"example/{name}:latest",
            ingress=ServiceIngressSpec(
                external=external,
                internal=InternalIngressPolicy(
                    enabled=internal,
                    allow_from=allow_from or [],
                ),
            ),
        )
    )


async def test_read_http_head_returns_buffered_body_prefix() -> None:
    reader = _reader_with(
        b"POST / HTTP/1.1\r\nHost: secret-service.localhost\r\nContent-Length: 6\r\n\r\nabcdef"
    )

    head, body_prefix = await _read_http_head(reader)

    assert head.endswith(b"\r\n\r\n")
    assert b"Content-Length: 6" in head
    assert body_prefix == b"abcdef"


def test_rewrite_request_headers_targets_upstream_and_closes_connection() -> None:
    rewritten = _rewrite_request_headers(
        b"GET / HTTP/1.1\r\n"
        b"Host: secret-service.localhost\r\n"
        b"Connection: keep-alive\r\n"
        b"Keep-Alive: timeout=5\r\n"
        b"User-Agent: test-client\r\n"
        b"\r\n",
        "127.0.0.1",
        9000,
    )

    assert b"Host: 127.0.0.1:9000\r\n" in rewritten
    assert b"User-Agent: test-client\r\n" in rewritten
    assert b"Connection: close\r\n" in rewritten
    assert b"Connection: keep-alive" not in rewritten
    assert b"Keep-Alive:" not in rewritten


def test_rewrite_response_headers_strips_hop_by_hop_headers() -> None:
    rewritten = _rewrite_response_headers(
        b"HTTP/1.1 200 OK\r\n"
        b"Content-Length: 2\r\n"
        b"Connection: keep-alive\r\n"
        b"Transfer-Encoding: chunked\r\n"
        b"\r\n"
    )

    assert b"Content-Length: 2\r\n" in rewritten
    assert b"Transfer-Encoding: chunked\r\n" in rewritten
    assert b"Connection: close\r\n" in rewritten
    assert b"Connection: keep-alive" not in rewritten


async def test_read_full_response_uses_content_length() -> None:
    reader = _reader_with(
        b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\nConnection: keep-alive\r\n\r\nhello"
    )

    response = await _read_full_response(reader)

    assert response.endswith(b"\r\n\r\nhello")
    assert b"Content-Length: 5\r\n" in response
    assert b"Connection: close\r\n" in response
    assert b"Connection: keep-alive" not in response


async def test_read_full_response_rejects_incomplete_content_length_body() -> None:
    reader = _reader_with(b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\n\r\nhe")

    try:
        await _read_full_response(reader)
    except UpstreamResponseError:
        pass
    else:
        raise AssertionError("Expected incomplete upstream response to raise")


def test_proxy_error_responses_have_matching_content_length() -> None:
    for response in (
        _403_RESPONSE,
        _403_INTERNAL_RESPONSE,
        _502_RESPONSE,
        _503_RESPONSE,
        _504_RESPONSE,
    ):
        head, body = response.split(b"\r\n\r\n", 1)
        content_length = _extract_request_content_length(head + b"\r\n\r\n")

        assert content_length == len(body)


async def test_proxy_denies_external_request_when_service_not_exposed() -> None:
    service_store = FakeServiceStore({"internal-api": _service_state("internal-api")})
    balancer = FakeBalancer()
    proxy = ProxyServer(
        balancer=balancer,  # type: ignore[arg-type]
        port=8080,
        service_store=service_store,  # type: ignore[arg-type]
    )
    writer = FakeWriter()

    await proxy._proxy_request(
        _reader_with(b"GET / HTTP/1.1\r\nHost: internal-api.localhost\r\n\r\n"),
        writer,  # type: ignore[arg-type]
    )

    assert writer.data == _403_RESPONSE
    assert balancer.requested_services == []


async def test_proxy_forwards_platform_storage_path_before_ingress_policy(
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    service_store = FakeServiceStore({"proxy:8080": _service_state("proxy:8080")})
    balancer = FakeBalancer()
    open_connection = FakeOpenConnection([b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}"])
    monkeypatch.setattr(asyncio, "open_connection", open_connection)
    proxy = ProxyServer(
        balancer=balancer,  # type: ignore[arg-type]
        port=8080,
        service_store=service_store,  # type: ignore[arg-type]
        secrets_manager=FakeSecretsManager(
            {"aws-light-service-token-combined-service": "combined-token"}
        ),  # type: ignore[arg-type]
    )
    writer = FakeWriter()

    await proxy._proxy_request(
        _reader_with(
            b"GET /_aws-light/storage/buckets/combined-objects/objects HTTP/1.1\r\n"
            b"Host: proxy:8080\r\n"
            b"X-AWS-Light-Service-Token: combined-token\r\n"
            b"\r\n"
        ),
        writer,  # type: ignore[arg-type]
    )

    assert b"HTTP/1.1 200 OK" in writer.data
    assert open_connection.connections == [("control-plane", 8000)]
    assert b"Host: control-plane:8000\r\n" in open_connection.writers[0].data
    assert balancer.requested_services == []


async def test_proxy_allows_external_request_when_service_is_exposed() -> None:
    service_store = FakeServiceStore({"public-api": _service_state("public-api", external=True)})
    proxy = ProxyServer(
        balancer=FakeBalancer(),  # type: ignore[arg-type]
        port=8080,
        service_store=service_store,  # type: ignore[arg-type]
    )
    writer = FakeWriter()

    try:
        await proxy._proxy_request(
            _reader_with(b"GET / HTTP/1.1\r\nHost: public-api.localhost\r\n\r\n"),
            writer,  # type: ignore[arg-type]
        )
    except AssertionError:
        pass
    else:
        raise AssertionError("Expected exposed service to reach load balancing")


async def test_proxy_forwards_http_over_raw_upstream_connection(
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    service_store = FakeServiceStore({"public-api": _service_state("public-api", external=True)})
    open_connection = FakeOpenConnection(
        [
            b"HTTP/1.1 201 Created\r\n"
            b"Content-Type: application/json\r\n"
            b"Content-Length: 12\r\n"
            b"Connection: keep-alive\r\n"
            b"\r\n"
            b'{"ok": true}'
        ]
    )
    monkeypatch.setattr(asyncio, "open_connection", open_connection)
    proxy = ProxyServer(
        balancer=StaticBalancer(
            [ReplicaEndpoint("replica-1", "10.0.0.5", 9000, healthy=True)]
        ),  # type: ignore[arg-type]
        port=8080,
        service_store=service_store,  # type: ignore[arg-type]
    )
    writer = FakeWriter()

    await proxy._proxy_request(
        _reader_with(
            b"POST /items?x=1 HTTP/1.1\r\n"
            b"Host: public-api.localhost\r\n"
            b"Connection: close\r\n"
            b"Content-Length: 4\r\n"
            b"\r\n"
            b"body"
        ),
        writer,  # type: ignore[arg-type]
    )

    assert writer.data == (
        b"HTTP/1.1 201 Created\r\n"
        b"Content-Type: application/json\r\n"
        b"Content-Length: 12\r\n"
        b"Connection: close\r\n"
        b"\r\n"
        b'{"ok": true}'
    )
    assert open_connection.connections == [("10.0.0.5", 9000)]
    assert open_connection.writers[0].data == (
        b"POST /items?x=1 HTTP/1.1\r\n"
        b"Host: 10.0.0.5:9000\r\n"
        b"Content-Length: 4\r\n"
        b"Connection: close\r\n"
        b"\r\n"
        b"body"
    )


async def test_proxy_closes_client_connection_after_one_http_request(
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    service_store = FakeServiceStore({"public-api": _service_state("public-api", external=True)})
    open_connection = FakeOpenConnection(
        [
            b"HTTP/1.1 200 OK\r\nContent-Length: 3\r\n\r\none",
            b"HTTP/1.1 200 OK\r\nContent-Length: 3\r\n\r\ntwo",
        ]
    )
    monkeypatch.setattr(asyncio, "open_connection", open_connection)
    proxy = ProxyServer(
        balancer=StaticBalancer(
            [ReplicaEndpoint("replica-1", "10.0.0.5", 9000, healthy=True)]
        ),  # type: ignore[arg-type]
        port=8080,
        service_store=service_store,  # type: ignore[arg-type]
    )
    writer = CloseAwareFakeWriter()

    await proxy._handle_connection(
        _reader_with(
            b"GET /one HTTP/1.1\r\n"
            b"Host: public-api.localhost\r\n"
            b"\r\n"
            b"GET /two HTTP/1.1\r\n"
            b"Host: public-api.localhost\r\n"
            b"Connection: close\r\n"
            b"\r\n"
        ),
        writer,  # type: ignore[arg-type]
    )

    assert writer.data.count(b"HTTP/1.1 200 OK\r\n") == 1
    assert writer.data.endswith(b"\r\nConnection: close\r\n\r\none")
    assert open_connection.connections == [("10.0.0.5", 9000)]
    assert writer.closed
    assert writer.waited_closed


async def test_proxy_falls_back_to_next_http_replica_before_response_starts(
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    service_store = FakeServiceStore({"public-api": _service_state("public-api", external=True)})
    open_connection = FakeOpenConnection(
        [
            OSError("first replica failed"),
            b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok",
        ]
    )
    monkeypatch.setattr(asyncio, "open_connection", open_connection)
    proxy = ProxyServer(
        balancer=StaticBalancer(
            [
                ReplicaEndpoint("replica-1", "10.0.0.5", 9000, healthy=True),
                ReplicaEndpoint("replica-2", "10.0.0.6", 9001, healthy=True),
            ]
        ),  # type: ignore[arg-type]
        port=8080,
        service_store=service_store,  # type: ignore[arg-type]
    )
    writer = FakeWriter()

    await proxy._proxy_request(
        _reader_with(b"GET / HTTP/1.1\r\nHost: public-api.localhost\r\n\r\n"),
        writer,  # type: ignore[arg-type]
    )

    assert b"HTTP/1.1 200 OK\r\n" in writer.data
    assert open_connection.connections == [("10.0.0.5", 9000), ("10.0.0.6", 9001)]


async def test_proxy_allows_internal_request_from_listed_caller() -> None:
    service_store = FakeServiceStore(
        {
            "frontend": _service_state("frontend"),
            "backend": _service_state("backend", allow_from=["frontend"]),
        }
    )
    proxy = ProxyServer(
        balancer=FakeBalancer(),  # type: ignore[arg-type]
        port=8080,
        service_store=service_store,  # type: ignore[arg-type]
        secrets_manager=FakeSecretsManager(
            {"aws-light-service-token-frontend": "frontend-token"}
        ),  # type: ignore[arg-type]
    )
    writer = FakeWriter()

    try:
        await proxy._proxy_request(
            _reader_with(
                b"GET / HTTP/1.1\r\n"
                b"Host: backend.localhost\r\n"
                b"X-AWS-Light-Service-Token: frontend-token\r\n"
                b"\r\n"
            ),
            writer,  # type: ignore[arg-type]
        )
    except AssertionError:
        pass
    else:
        raise AssertionError("Expected listed internal caller to reach load balancing")


async def test_proxy_denies_internal_request_from_unlisted_caller() -> None:
    service_store = FakeServiceStore(
        {
            "admin": _service_state("admin"),
            "frontend": _service_state("frontend"),
            "backend": _service_state("backend", allow_from=["frontend"]),
        }
    )
    balancer = FakeBalancer()
    proxy = ProxyServer(
        balancer=balancer,  # type: ignore[arg-type]
        port=8080,
        service_store=service_store,  # type: ignore[arg-type]
        secrets_manager=FakeSecretsManager(
            {
                "aws-light-service-token-admin": "admin-token",
                "aws-light-service-token-frontend": "frontend-token",
            }
        ),  # type: ignore[arg-type]
    )
    writer = FakeWriter()

    await proxy._proxy_request(
        _reader_with(
            b"GET / HTTP/1.1\r\n"
            b"Host: backend.localhost\r\n"
            b"X-AWS-Light-Service-Token: admin-token\r\n"
            b"\r\n"
        ),
        writer,  # type: ignore[arg-type]
    )

    assert writer.data == _403_INTERNAL_RESPONSE
    assert balancer.requested_services == []


async def test_proxy_allows_internal_request_when_target_is_broadly_internal() -> None:
    service_store = FakeServiceStore(
        {
            "frontend": _service_state("frontend"),
            "shared": _service_state("shared", internal=True),
        }
    )
    proxy = ProxyServer(
        balancer=FakeBalancer(),  # type: ignore[arg-type]
        port=8080,
        service_store=service_store,  # type: ignore[arg-type]
        secrets_manager=FakeSecretsManager(
            {"aws-light-service-token-frontend": "frontend-token"}
        ),  # type: ignore[arg-type]
    )
    writer = FakeWriter()

    try:
        await proxy._proxy_request(
            _reader_with(
                b"GET / HTTP/1.1\r\n"
                b"Host: shared.localhost\r\n"
                b"X-AWS-Light-Service-Token: frontend-token\r\n"
                b"\r\n"
            ),
            writer,  # type: ignore[arg-type]
        )
    except AssertionError:
        pass
    else:
        raise AssertionError("Expected broadly internal service to reach load balancing")


def test_request_content_length_parsing() -> None:
    assert _extract_request_content_length(b"GET / HTTP/1.1\r\n\r\n") == 0
    assert _extract_request_content_length(b"POST / HTTP/1.1\r\nContent-Length: 3\r\n\r\n") == 3
    bad_content_length = b"POST / HTTP/1.1\r\nContent-Length: bad\r\n\r\n"
    assert _extract_request_content_length(bad_content_length) is None
    assert _has_transfer_encoding(b"POST / HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n")


def test_extract_response_status() -> None:
    assert _extract_response_status(b"HTTP/1.1 204 No Content\r\n\r\n") == 204
    assert _extract_response_status(b"not-http\r\n\r\n") is None


async def test_record_proxy_result_writes_redis_metrics() -> None:
    redis = FakeRedis()
    proxy = ProxyServer(balancer=None, port=8080, redis_client=redis)  # type: ignore[arg-type]

    await proxy._record_proxy_result("secret-service", 200, None, 12.3)
    await proxy._record_proxy_result("secret-service", 502, "upstream_unreachable", 4.0)

    assert redis.values == {}
    assert redis.hashes == {}

    await proxy._flush_proxy_metrics()

    assert redis.values["proxy:requests:total"] == 2
    assert redis.values["rps:secret-service"] == 2
    assert redis.values["proxy:last_duration_ms:secret-service"] == "4.00"
    assert redis.hashes["proxy:requests:service"] == {"secret-service": 2}
    assert redis.hashes["proxy:responses:status"] == {"200": 1, "502": 1}
    assert redis.hashes["proxy:failures"] == {"upstream_unreachable": 1}
    request_buckets = [
        values for key, values in redis.hashes.items() if key.startswith("proxy:ts:requests:")
    ]
    assert request_buckets == [{"secret-service": 2}]
    error_buckets = [
        values for key, values in redis.hashes.items() if key.startswith("proxy:ts:errors:")
    ]
    assert error_buckets == [{"secret-service": 1}]
    assert redis.pipeline_execute_count == 1


async def test_proxy_stop_flushes_pending_redis_metrics() -> None:
    redis = FakeRedis()
    proxy = ProxyServer(balancer=None, port=8080, redis_client=redis)  # type: ignore[arg-type]

    await proxy._record_proxy_result("secret-service", 200, None, 12.3)
    await proxy.stop()

    assert redis.values["proxy:requests:total"] == 1
    assert redis.values["rps:secret-service"] == 1


async def test_record_proxy_result_publishes_failure_activity() -> None:
    redis = FakeRedis()
    event_bus = EventBus()
    proxy = ProxyServer(
        balancer=None,  # type: ignore[arg-type]
        port=8080,
        redis_client=redis,  # type: ignore[arg-type]
        event_bus=event_bus,
    )

    await proxy._record_proxy_result("secret-service", 502, "upstream_unreachable", 4.0)

    events = await event_bus.get_recent_events()
    assert len(events) == 1
    assert events[0].kind.value == "proxy.request_failed"
    assert events[0].payload == {
        "service_name": "secret-service",
        "status_code": 502,
        "failure_reason": "upstream_unreachable",
        "duration_ms": 4.0,
    }


async def test_proxy_publishes_aggregated_traffic_activity() -> None:
    redis = FakeRedis()
    event_bus = EventBus()
    proxy = ProxyServer(
        balancer=None,  # type: ignore[arg-type]
        port=8080,
        redis_client=redis,  # type: ignore[arg-type]
        event_bus=event_bus,
    )

    await proxy._record_proxy_result("secret-service", 200, None, 12.3)
    await proxy._record_proxy_result("secret-service", 502, "upstream_unreachable", 4.0)
    await proxy._flush_proxy_metrics()
    await proxy._publish_traffic_activity()

    events = await event_bus.get_recent_events()
    assert events[-1].kind.value == "proxy.traffic_observed"
    assert events[-1].payload["requests_total"] == 2
    assert events[-1].payload["errors_total"] == 1
    assert events[-1].payload["requests_by_service"] == {"secret-service": 2}
    assert events[-1].payload["responses_by_status"] == {"200": 1, "502": 1}


async def test_proxy_timing_snapshot_resets_aggregated_stage_timings() -> None:
    proxy = ProxyServer(balancer=None, port=8080)  # type: ignore[arg-type]

    await proxy._record_timing("read", 1.0)
    await proxy._record_timing("read", 3.0)
    await proxy._record_timing("route", 2.0)

    snapshot = await proxy._timing_snapshot()
    second_snapshot = await proxy._timing_snapshot()

    assert snapshot == {
        "read": {"count": 2, "avg_ms": 2.0, "max_ms": 3.0},
        "route": {"count": 1, "avg_ms": 2.0, "max_ms": 2.0},
    }
    assert second_snapshot == {}

from __future__ import annotations

import asyncio

from aws_light.dashboard.event_bus import EventBus
from aws_light.proxy.proxy_server import (
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


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, int | str] = {}
        self.hashes: dict[str, dict[str, int]] = {}

    def pipeline(self) -> FakePipeline:
        return FakePipeline(self)


class FakePipeline:
    def __init__(self, redis: FakeRedis) -> None:
        self.redis = redis
        self.commands: list[tuple[str, str, str | None, int | str]] = []

    def incr(self, key: str) -> None:
        self.commands.append(("incr", key, None, 1))

    def hincrby(self, key: str, field: str, amount: int) -> None:
        self.commands.append(("hincrby", key, field, amount))

    def set(self, key: str, value: str) -> None:
        self.commands.append(("set", key, None, value))

    async def execute(self) -> None:
        for command, key, field, value in self.commands:
            if command == "incr":
                self.redis.values[key] = self.redis.values.get(key, 0) + int(value)
            elif command == "hincrby" and field is not None:
                bucket = self.redis.hashes.setdefault(key, {})
                bucket[field] = bucket.get(field, 0) + int(value)
            elif command == "set":
                self.redis.values[key] = str(value)


def _reader_with(data: bytes) -> asyncio.StreamReader:
    reader = asyncio.StreamReader()
    reader.feed_data(data)
    reader.feed_eof()
    return reader


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
    for response in (_502_RESPONSE, _503_RESPONSE, _504_RESPONSE):
        head, body = response.split(b"\r\n\r\n", 1)
        content_length = _extract_request_content_length(head + b"\r\n\r\n")

        assert content_length == len(body)


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

    assert redis.values["proxy:requests:total"] == 2
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

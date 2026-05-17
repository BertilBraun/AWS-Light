from __future__ import annotations

import asyncio

from aws_light.proxy.proxy_server import (
    _extract_request_content_length,
    _has_transfer_encoding,
    _read_full_response,
    _read_http_head,
    _rewrite_request_headers,
    _rewrite_response_headers,
)


def _reader_with(data: bytes) -> asyncio.StreamReader:
    reader = asyncio.StreamReader()
    reader.feed_data(data)
    reader.feed_eof()
    return reader


async def test_read_http_head_returns_buffered_body_prefix() -> None:
    reader = _reader_with(
        b"POST / HTTP/1.1\r\n"
        b"Host: hello-service.localhost\r\n"
        b"Content-Length: 6\r\n"
        b"\r\n"
        b"abcdef"
    )

    head, body_prefix = await _read_http_head(reader)

    assert head.endswith(b"\r\n\r\n")
    assert b"Content-Length: 6" in head
    assert body_prefix == b"abcdef"


def test_rewrite_request_headers_targets_upstream_and_closes_connection() -> None:
    rewritten = _rewrite_request_headers(
        b"GET / HTTP/1.1\r\n"
        b"Host: hello-service.localhost\r\n"
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
        b"HTTP/1.1 200 OK\r\n"
        b"Content-Length: 5\r\n"
        b"Connection: keep-alive\r\n"
        b"\r\n"
        b"hello"
    )

    response = await _read_full_response(reader)

    assert response.endswith(b"\r\n\r\nhello")
    assert b"Content-Length: 5\r\n" in response
    assert b"Connection: close\r\n" in response
    assert b"Connection: keep-alive" not in response


def test_request_content_length_parsing() -> None:
    assert _extract_request_content_length(b"GET / HTTP/1.1\r\n\r\n") == 0
    assert _extract_request_content_length(b"POST / HTTP/1.1\r\nContent-Length: 3\r\n\r\n") == 3
    bad_content_length = b"POST / HTTP/1.1\r\nContent-Length: bad\r\n\r\n"
    assert _extract_request_content_length(bad_content_length) is None
    assert _has_transfer_encoding(b"POST / HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n")

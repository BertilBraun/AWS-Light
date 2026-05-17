from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from aws_light.proxy.load_balancer import NoHealthyReplicaError, RoundRobinBalancer

if TYPE_CHECKING:
    from redis.asyncio import Redis

logger = logging.getLogger(__name__)

_HEADER_DELIMITER = b"\r\n\r\n"
_MAX_HEADER_BYTES = 64 * 1024
_HOP_BY_HOP_HEADERS = {
    b"connection",
    b"keep-alive",
    b"proxy-authenticate",
    b"proxy-authorization",
    b"te",
    b"trailer",
    b"transfer-encoding",
    b"upgrade",
}
_RESPONSE_HOP_BY_HOP_HEADERS = _HOP_BY_HOP_HEADERS - {b"transfer-encoding"}

_503_RESPONSE = (
    b"HTTP/1.1 503 Service Unavailable\r\n"
    b"Content-Type: application/json\r\n"
    b"Content-Length: 36\r\n"
    b"Connection: close\r\n"
    b"\r\n"
    b'{"error": "no healthy replica found"}'
)

_502_RESPONSE = (
    b"HTTP/1.1 502 Bad Gateway\r\n"
    b"Content-Type: application/json\r\n"
    b"Content-Length: 29\r\n"
    b"Connection: close\r\n"
    b"\r\n"
    b'{"error": "upstream unreachable"}'
)

_400_RESPONSE = (
    b"HTTP/1.1 400 Bad Request\r\n"
    b"Content-Type: application/json\r\n"
    b"Content-Length: 24\r\n"
    b"Connection: close\r\n"
    b"\r\n"
    b'{"error": "bad request"}'
)

_501_RESPONSE = (
    b"HTTP/1.1 501 Not Implemented\r\n"
    b"Content-Type: application/json\r\n"
    b"Content-Length: 42\r\n"
    b"Connection: close\r\n"
    b"\r\n"
    b'{"error": "transfer encoding unsupported"}'
)

_PROXY_METRIC_TOTAL = "proxy:requests:total"
_PROXY_METRIC_BY_SERVICE = "proxy:requests:service"
_PROXY_METRIC_BY_STATUS = "proxy:responses:status"
_PROXY_METRIC_FAILURES = "proxy:failures"


class ProxyServer:
    def __init__(
        self,
        balancer: RoundRobinBalancer,
        port: int,
        redis_client: Redis | None = None,  # type: ignore[type-arg]
    ) -> None:
        self._balancer = balancer
        self._port = port
        self._redis = redis_client
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle_connection, "0.0.0.0", self._port)
        logger.info("Proxy server listening on port %d", self._port)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def _handle_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            await self._proxy_request(reader, writer)
        except Exception:
            logger.exception("Error handling proxy connection")
        finally:
            writer.close()
            await writer.wait_closed()

    async def _proxy_request(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        header_bytes, body_prefix = await _read_http_head(reader)
        if not header_bytes:
            return

        if _has_transfer_encoding(header_bytes):
            writer.write(_501_RESPONSE)
            await writer.drain()
            await self._record_proxy_result(None, 501, "unsupported_transfer_encoding", 0.0)
            return

        content_length = _extract_request_content_length(header_bytes)
        if content_length is None:
            writer.write(_400_RESPONSE)
            await writer.drain()
            await self._record_proxy_result(None, 400, "bad_content_length", 0.0)
            return

        body = body_prefix[:content_length]
        remaining_body_bytes = content_length - len(body)
        if remaining_body_bytes > 0:
            try:
                body += await asyncio.wait_for(
                    reader.readexactly(remaining_body_bytes), timeout=30.0
                )
            except (asyncio.TimeoutError, asyncio.IncompleteReadError):
                writer.write(_400_RESPONSE)
                await writer.drain()
                await self._record_proxy_result(None, 400, "incomplete_request_body", 0.0)
                return

        service_name = _extract_service_name(header_bytes)
        if service_name is None:
            writer.write(_503_RESPONSE)
            await writer.drain()
            await self._record_proxy_result(None, 503, "missing_host", 0.0)
            return

        try:
            endpoint = await self._balancer.next_healthy_replica(service_name)
        except NoHealthyReplicaError:
            writer.write(_503_RESPONSE)
            await writer.drain()
            await self._record_proxy_result(service_name, 503, "no_healthy_replica", 0.0)
            return

        if self._redis is not None:
            await self._redis.incr(f"rps:{service_name}")

        started = time.perf_counter()
        try:
            upstream_reader, upstream_writer = await asyncio.wait_for(
                asyncio.open_connection(endpoint.host, endpoint.port), timeout=5.0
            )
        except (OSError, asyncio.TimeoutError):
            writer.write(_502_RESPONSE)
            await writer.drain()
            duration_ms = (time.perf_counter() - started) * 1000
            await self._record_proxy_result(service_name, 502, "upstream_unreachable", duration_ms)
            return

        is_websocket = _is_websocket_upgrade(header_bytes)
        rewritten_headers = _rewrite_request_headers(header_bytes, endpoint.host, endpoint.port)
        upstream_writer.write(rewritten_headers)
        if not is_websocket and body:
            upstream_writer.write(body)
        await upstream_writer.drain()

        if is_websocket:
            await asyncio.gather(
                _pipe_stream(reader, upstream_writer),
                _pipe_stream(upstream_reader, writer),
                return_exceptions=True,
            )
        else:
            upstream_response = await _read_full_response(upstream_reader)
            status_code = _extract_response_status(upstream_response) or 502
            writer.write(upstream_response)
            await writer.drain()
            upstream_writer.close()
            await upstream_writer.wait_closed()
            duration_ms = (time.perf_counter() - started) * 1000
            await self._record_proxy_result(service_name, status_code, None, duration_ms)

    async def _record_proxy_result(
        self,
        service_name: str | None,
        status_code: int,
        failure_reason: str | None,
        duration_ms: float,
    ) -> None:
        if self._redis is None:
            return

        pipe = self._redis.pipeline()
        pipe.incr(_PROXY_METRIC_TOTAL)
        pipe.hincrby(_PROXY_METRIC_BY_STATUS, str(status_code), 1)
        if service_name:
            pipe.hincrby(_PROXY_METRIC_BY_SERVICE, service_name, 1)
            pipe.set(f"proxy:last_duration_ms:{service_name}", f"{duration_ms:.2f}")
        if failure_reason:
            pipe.hincrby(_PROXY_METRIC_FAILURES, failure_reason, 1)
        await pipe.execute()


async def _read_http_head(reader: asyncio.StreamReader) -> tuple[bytes, bytes]:
    buffer = b""
    while _HEADER_DELIMITER not in buffer:
        chunk = await asyncio.wait_for(reader.read(4096), timeout=10.0)
        if not chunk:
            break
        buffer += chunk
        if len(buffer) > _MAX_HEADER_BYTES:
            return b"", b""

    header_end = buffer.find(_HEADER_DELIMITER)
    if header_end == -1:
        return buffer, b""

    split_at = header_end + len(_HEADER_DELIMITER)
    return buffer[:split_at], buffer[split_at:]


def _extract_service_name(header_bytes: bytes) -> str | None:
    for line in header_bytes.split(b"\r\n"):
        if line.lower().startswith(b"host:"):
            host_value = line[5:].strip().decode(errors="replace")
            hostname = host_value.split(":")[0]
            if hostname.endswith(".localhost"):
                return hostname[: -len(".localhost")]
            return hostname
    return None


def _is_websocket_upgrade(header_bytes: bytes) -> bool:
    return b"upgrade: websocket" in header_bytes.lower()


def _extract_response_status(response_bytes: bytes) -> int | None:
    status_line = response_bytes.split(b"\r\n", 1)[0]
    parts = status_line.split()
    if len(parts) < 2:
        return None
    try:
        return int(parts[1])
    except ValueError:
        return None


def _rewrite_request_headers(header_bytes: bytes, upstream_host: str, upstream_port: int) -> bytes:
    lines = _header_lines(header_bytes)
    if not lines:
        return header_bytes

    rewritten = [lines[0], f"Host: {upstream_host}:{upstream_port}".encode()]
    for line in lines[1:]:
        name = _header_name(line)
        if name is None or name in _HOP_BY_HOP_HEADERS or name == b"host":
            continue
        rewritten.append(line)
    rewritten.append(b"Connection: close")
    return b"\r\n".join(rewritten) + _HEADER_DELIMITER


def _rewrite_response_headers(header_bytes: bytes) -> bytes:
    lines = _header_lines(header_bytes)
    if not lines:
        return header_bytes

    rewritten = [lines[0]]
    for line in lines[1:]:
        name = _header_name(line)
        if name is None or name in _RESPONSE_HOP_BY_HOP_HEADERS:
            continue
        rewritten.append(line)
    rewritten.append(b"Connection: close")
    return b"\r\n".join(rewritten) + _HEADER_DELIMITER


async def _pipe_stream(source: asyncio.StreamReader, destination: asyncio.StreamWriter) -> None:
    try:
        while True:
            chunk = await source.read(4096)
            if not chunk:
                break
            destination.write(chunk)
            await destination.drain()
    except (ConnectionResetError, BrokenPipeError):
        pass
    finally:
        destination.close()


async def _read_full_response(reader: asyncio.StreamReader) -> bytes:
    header_bytes, body_prefix = await _read_http_head(reader)
    if not header_bytes:
        return b""

    rewritten_headers = _rewrite_response_headers(header_bytes)
    content_length = _extract_content_length(header_bytes)
    if content_length is None:
        chunks = [rewritten_headers, body_prefix]
        try:
            while True:
                chunk = await asyncio.wait_for(reader.read(4096), timeout=30.0)
                if not chunk:
                    break
                chunks.append(chunk)
        except asyncio.TimeoutError:
            pass
        return b"".join(chunks)

    body = body_prefix[:content_length]
    remaining = max(0, content_length - len(body))
    chunks = [rewritten_headers, body]
    try:
        while remaining > 0:
            chunk = await asyncio.wait_for(reader.read(min(4096, remaining)), timeout=30.0)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    except (asyncio.TimeoutError, asyncio.IncompleteReadError):
        pass
    return b"".join(chunks)


def _extract_content_length(header_bytes: bytes) -> int | None:
    value = _header_value(header_bytes, b"content-length")
    if value is None:
        return None
    try:
        content_length = int(value)
    except ValueError:
        return None
    if content_length < 0:
        return None
    return content_length


def _extract_request_content_length(header_bytes: bytes) -> int | None:
    value = _header_value(header_bytes, b"content-length")
    if value is None:
        return 0
    return _extract_content_length(header_bytes)


def _has_transfer_encoding(header_bytes: bytes) -> bool:
    return _header_value(header_bytes, b"transfer-encoding") is not None


def _header_value(header_bytes: bytes, name: bytes) -> bytes | None:
    for line in header_bytes.split(b"\r\n"):
        header_name = _header_name(line)
        if header_name == name:
            return line.split(b":", 1)[1].strip()
    return None


def _header_lines(header_bytes: bytes) -> list[bytes]:
    return [line for line in header_bytes.split(b"\r\n") if line]


def _header_name(line: bytes) -> bytes | None:
    if b":" not in line:
        return None
    return line.split(b":", 1)[0].strip().lower()

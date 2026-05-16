from __future__ import annotations

import asyncio
import logging
from collections import defaultdict

from aws_light.proxy.load_balancer import NoHealthyReplicaError, RoundRobinBalancer

logger = logging.getLogger(__name__)

_503_RESPONSE = (
    b"HTTP/1.1 503 Service Unavailable\r\n"
    b"Content-Type: application/json\r\n"
    b"Content-Length: 36\r\n"
    b"\r\n"
    b'{"error": "no healthy replica found"}'
)

_502_RESPONSE = (
    b"HTTP/1.1 502 Bad Gateway\r\n"
    b"Content-Type: application/json\r\n"
    b"Content-Length: 29\r\n"
    b"\r\n"
    b'{"error": "upstream unreachable"}'
)


class ProxyServer:
    def __init__(self, balancer: RoundRobinBalancer, port: int) -> None:
        self._balancer = balancer
        self._port = port
        self._request_counts: dict[str, int] = defaultdict(int)
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle_connection, "0.0.0.0", self._port)
        logger.info("Proxy server listening on port %d", self._port)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    def get_request_count(self, service_name: str) -> int:
        return self._request_counts.get(service_name, 0)

    def reset_request_count(self, service_name: str) -> None:
        self._request_counts[service_name] = 0

    async def _handle_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            await self._proxy_request(reader, writer)
        except Exception:
            logger.exception("Error handling proxy connection")
        finally:
            writer.close()

    async def _proxy_request(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        header_bytes = await _read_http_headers(reader)
        if not header_bytes:
            return

        service_name = _extract_service_name(header_bytes)
        if service_name is None:
            writer.write(_503_RESPONSE)
            await writer.drain()
            return

        try:
            endpoint = await self._balancer.next_healthy_replica(service_name)
        except NoHealthyReplicaError:
            writer.write(_503_RESPONSE)
            await writer.drain()
            return

        self._request_counts[service_name] += 1

        try:
            upstream_reader, upstream_writer = await asyncio.wait_for(
                asyncio.open_connection("127.0.0.1", endpoint.port), timeout=5.0
            )
        except (OSError, asyncio.TimeoutError):
            writer.write(_502_RESPONSE)
            await writer.drain()
            return

        is_websocket = _is_websocket_upgrade(header_bytes)
        rewritten_headers = _rewrite_host_header(header_bytes, endpoint.port)
        upstream_writer.write(rewritten_headers)
        await upstream_writer.drain()

        if is_websocket:
            await asyncio.gather(
                _pipe_stream(reader, upstream_writer),
                _pipe_stream(upstream_reader, writer),
                return_exceptions=True,
            )
        else:
            upstream_response = await _read_full_response(upstream_reader)
            writer.write(upstream_response)
            await writer.drain()
            upstream_writer.close()


async def _read_http_headers(reader: asyncio.StreamReader) -> bytes:
    buffer = b""
    while b"\r\n\r\n" not in buffer:
        chunk = await asyncio.wait_for(reader.read(4096), timeout=10.0)
        if not chunk:
            break
        buffer += chunk
    return buffer


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
    lower = header_bytes.lower()
    return b"upgrade: websocket" in lower


def _rewrite_host_header(header_bytes: bytes, upstream_port: int) -> bytes:
    lines = header_bytes.split(b"\r\n")
    rewritten = []
    for line in lines:
        if line.lower().startswith(b"host:"):
            rewritten.append(f"Host: localhost:{upstream_port}".encode())
        else:
            rewritten.append(line)
    return b"\r\n".join(rewritten)


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
    chunks = []
    try:
        while True:
            chunk = await asyncio.wait_for(reader.read(4096), timeout=30.0)
            if not chunk:
                break
            chunks.append(chunk)
    except asyncio.TimeoutError:
        pass
    return b"".join(chunks)

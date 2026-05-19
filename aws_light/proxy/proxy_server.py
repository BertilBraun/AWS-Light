from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import Counter
from typing import TYPE_CHECKING

from aws_light.proxy.load_balancer import NoHealthyReplicaError, RoundRobinBalancer

if TYPE_CHECKING:
    from redis.asyncio import Redis

    from aws_light.dashboard.event_bus import EventBus
    from aws_light.models.service import ServiceState
    from aws_light.secrets.secrets_manager import SecretsManager
    from aws_light.store.base import AnyStore

from aws_light.models.events import EventKind, WebSocketEvent

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

def _error_response(status_line: bytes, body: bytes) -> bytes:
    return (
        status_line
        + b"\r\nContent-Type: application/json\r\n"
        + f"Content-Length: {len(body)}\r\n".encode()
        + b"Connection: close\r\n"
        + b"\r\n"
        + body
    )


_503_RESPONSE = _error_response(
    b"HTTP/1.1 503 Service Unavailable",
    b'{"error": "no healthy replica found"}',
)
_502_RESPONSE = _error_response(
    b"HTTP/1.1 502 Bad Gateway",
    b'{"error": "upstream unreachable"}',
)
_504_RESPONSE = _error_response(
    b"HTTP/1.1 504 Gateway Timeout",
    b'{"error": "upstream timeout"}',
)
_400_RESPONSE = _error_response(
    b"HTTP/1.1 400 Bad Request",
    b'{"error": "bad request"}',
)
_403_RESPONSE = _error_response(
    b"HTTP/1.1 403 Forbidden",
    b'{"error": "external ingress denied"}',
)
_403_INTERNAL_RESPONSE = _error_response(
    b"HTTP/1.1 403 Forbidden",
    b'{"error": "internal ingress denied"}',
)
_501_RESPONSE = _error_response(
    b"HTTP/1.1 501 Not Implemented",
    b'{"error": "transfer encoding unsupported"}',
)

_PROXY_METRIC_TOTAL = "proxy:requests:total"
_PROXY_METRIC_BY_SERVICE = "proxy:requests:service"
_PROXY_METRIC_BY_STATUS = "proxy:responses:status"
_PROXY_METRIC_FAILURES = "proxy:failures"
_PROXY_TIMESERIES_BUCKET_SECONDS = 10
_PROXY_ACTIVITY_INTERVAL_SECONDS = 10.0
_SERVICE_TOKEN_HEADER = b"x-aws-light-service-token"
_SERVICE_TOKEN_SECRET_PREFIX = "aws-light-service-token-"


class ProxyServer:
    def __init__(
        self,
        balancer: RoundRobinBalancer,
        port: int,
        redis_client: Redis | None = None,  # type: ignore[type-arg]
        event_bus: EventBus | None = None,
        service_store: AnyStore[ServiceState] | None = None,
        secrets_manager: SecretsManager | None = None,
    ) -> None:
        self._balancer = balancer
        self._port = port
        self._redis = redis_client
        self._event_bus = event_bus
        self._service_store = service_store
        self._secrets_manager = secrets_manager
        self._server: asyncio.AbstractServer | None = None
        self._activity_task: asyncio.Task[None] | None = None
        self._traffic_lock = asyncio.Lock()
        self._traffic_total = 0
        self._traffic_by_service: Counter[str] = Counter()
        self._traffic_by_status: Counter[int] = Counter()
        self._traffic_failures: Counter[str] = Counter()

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle_connection, "0.0.0.0", self._port)
        if self._event_bus is not None:
            self._activity_task = asyncio.create_task(self._traffic_activity_loop())
        logger.info("Proxy server listening on port %d", self._port)

    async def stop(self) -> None:
        if self._activity_task is not None:
            self._activity_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._activity_task
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

        source_service = await self._source_service_from_request(header_bytes)
        token_present = _header_value(header_bytes, _SERVICE_TOKEN_HEADER) is not None
        if token_present:
            if source_service is None or not await self._internal_ingress_allowed(
                service_name, source_service
            ):
                writer.write(_403_INTERNAL_RESPONSE)
                await writer.drain()
                await self._record_proxy_result(
                    service_name, 403, "internal_ingress_denied", 0.0
                )
                return
        elif not await self._external_ingress_allowed(service_name):
            writer.write(_403_RESPONSE)
            await writer.drain()
            await self._record_proxy_result(
                service_name, 403, "external_ingress_denied", 0.0
            )
            return

        try:
            endpoints = await self._balancer.healthy_replicas_for_request(service_name)
        except NoHealthyReplicaError:
            writer.write(_503_RESPONSE)
            await writer.drain()
            await self._record_proxy_result(service_name, 503, "no_healthy_replica", 0.0)
            return

        if self._redis is not None:
            await self._redis.incr(f"rps:{service_name}")

        started = time.perf_counter()
        upstream_reader: asyncio.StreamReader | None = None
        upstream_writer: asyncio.StreamWriter | None = None
        endpoint = None
        failures: list[str] = []
        for candidate in endpoints:
            try:
                upstream_reader, upstream_writer = await asyncio.wait_for(
                    asyncio.open_connection(candidate.host, candidate.port), timeout=5.0
                )
                endpoint = candidate
                break
            except (OSError, asyncio.TimeoutError) as error:
                failures.append(f"{candidate.replica_id[:8]} {candidate.host}:{candidate.port}")
                logger.warning(
                    "Proxy could not connect to %s replica %s at %s:%d: %s",
                    service_name,
                    candidate.replica_id[:8],
                    candidate.host,
                    candidate.port,
                    error,
                )

        if upstream_reader is None or upstream_writer is None or endpoint is None:
            writer.write(_502_RESPONSE)
            await writer.drain()
            duration_ms = (time.perf_counter() - started) * 1000
            logger.warning(
                "Proxy returning 502 for %s after %d failed upstream connection attempts: %s",
                service_name,
                len(failures),
                ", ".join(failures) or "none",
            )
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
            try:
                upstream_response = await _read_full_response(upstream_reader)
            except UpstreamResponseTimeout:
                writer.write(_504_RESPONSE)
                await writer.drain()
                upstream_writer.close()
                await upstream_writer.wait_closed()
                duration_ms = (time.perf_counter() - started) * 1000
                logger.warning(
                    "Proxy timed out waiting for %s replica %s response after %.2fms",
                    service_name,
                    endpoint.replica_id[:8],
                    duration_ms,
                )
                await self._record_proxy_result(
                    service_name, 504, "upstream_response_timeout", duration_ms
                )
                return
            except UpstreamResponseError:
                writer.write(_502_RESPONSE)
                await writer.drain()
                upstream_writer.close()
                await upstream_writer.wait_closed()
                duration_ms = (time.perf_counter() - started) * 1000
                await self._record_proxy_result(
                    service_name, 502, "upstream_invalid_response", duration_ms
                )
                return
            status_code = _extract_response_status(upstream_response) or 502
            writer.write(upstream_response)
            await writer.drain()
            upstream_writer.close()
            await upstream_writer.wait_closed()
            duration_ms = (time.perf_counter() - started) * 1000
            await self._record_proxy_result(service_name, status_code, None, duration_ms)

    async def _external_ingress_allowed(self, service_name: str) -> bool:
        if self._service_store is None:
            return True
        service_state = await self._service_store.get(service_name)
        if service_state is None:
            return False
        return service_state.spec.ingress.external

    async def _internal_ingress_allowed(self, service_name: str, source_service: str) -> bool:
        if self._service_store is None:
            return True
        service_state = await self._service_store.get(service_name)
        if service_state is None:
            return False
        policy = service_state.spec.ingress.internal
        return policy.enabled or source_service in policy.allow_from

    async def _source_service_from_request(self, header_bytes: bytes) -> str | None:
        token_value = _header_value(header_bytes, _SERVICE_TOKEN_HEADER)
        if token_value is None or self._secrets_manager is None:
            return None
        token = token_value.decode(errors="replace")
        for secret_name in await self._secrets_manager.list_secret_names():
            if not secret_name.startswith(_SERVICE_TOKEN_SECRET_PREFIX):
                continue
            stored_token = await self._secrets_manager.get_secret(secret_name)
            if stored_token != token:
                continue
            service_name = secret_name.removeprefix(_SERVICE_TOKEN_SECRET_PREFIX)
            if self._service_store is not None and await self._service_store.get(service_name) is None:
                return None
            return service_name
        return None

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
        bucket = (
            int(time.time() // _PROXY_TIMESERIES_BUCKET_SECONDS) * _PROXY_TIMESERIES_BUCKET_SECONDS
        )
        timeseries_service = service_name or "__unknown__"
        pipe.hincrby(f"proxy:ts:requests:{bucket}", timeseries_service, 1)
        pipe.hincrby(f"proxy:ts:status:{bucket}", str(status_code), 1)
        pipe.hincrby(f"proxy:ts:latency_sum:{bucket}", timeseries_service, int(duration_ms * 100))
        pipe.hincrby(f"proxy:ts:latency_count:{bucket}", timeseries_service, 1)
        if status_code >= 500:
            pipe.hincrby(f"proxy:ts:errors:{bucket}", timeseries_service, 1)
        if failure_reason:
            pipe.hincrby(_PROXY_METRIC_FAILURES, failure_reason, 1)
        await pipe.execute()
        await self._record_traffic_activity(service_name, status_code, failure_reason)
        if failure_reason and self._event_bus is not None:
            await self._event_bus.publish(
                WebSocketEvent(
                    kind=EventKind.PROXY_REQUEST_FAILED,
                    payload={
                        "service_name": service_name,
                        "status_code": status_code,
                        "failure_reason": failure_reason,
                        "duration_ms": round(duration_ms, 2),
                    },
                )
            )

    async def _record_traffic_activity(
        self,
        service_name: str | None,
        status_code: int,
        failure_reason: str | None,
    ) -> None:
        async with self._traffic_lock:
            self._traffic_total += 1
            self._traffic_by_service[service_name or "__unknown__"] += 1
            self._traffic_by_status[status_code] += 1
            if failure_reason:
                self._traffic_failures[failure_reason] += 1

    async def _traffic_activity_loop(self) -> None:
        while True:
            await asyncio.sleep(_PROXY_ACTIVITY_INTERVAL_SECONDS)
            await self._publish_traffic_activity()

    async def _publish_traffic_activity(self) -> None:
        if self._event_bus is None:
            return
        async with self._traffic_lock:
            total = self._traffic_total
            by_service = dict(self._traffic_by_service)
            by_status = {str(status): count for status, count in self._traffic_by_status.items()}
            failures = dict(self._traffic_failures)
            self._traffic_total = 0
            self._traffic_by_service.clear()
            self._traffic_by_status.clear()
            self._traffic_failures.clear()
        if total == 0:
            return
        error_count = sum(
            count for status, count in by_status.items() if int(status) >= 500
        )
        logger.info(
            "Proxy handled %d requests in %.0fs (%d errors)",
            total,
            _PROXY_ACTIVITY_INTERVAL_SECONDS,
            error_count,
        )
        await self._event_bus.publish(
            WebSocketEvent(
                kind=EventKind.PROXY_TRAFFIC_OBSERVED,
                payload={
                    "component": "proxy",
                    "window_seconds": int(_PROXY_ACTIVITY_INTERVAL_SECONDS),
                    "requests_total": total,
                    "errors_total": error_count,
                    "requests_by_service": by_service,
                    "responses_by_status": by_status,
                    "failures": failures,
                },
            )
        )


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


class UpstreamResponseError(Exception):
    pass


class UpstreamResponseTimeout(UpstreamResponseError):
    pass


async def _read_full_response(reader: asyncio.StreamReader) -> bytes:
    try:
        header_bytes, body_prefix = await _read_http_head(reader)
    except asyncio.TimeoutError as exc:
        raise UpstreamResponseTimeout from exc
    if not header_bytes:
        raise UpstreamResponseError

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
            raise UpstreamResponseTimeout from None
        return b"".join(chunks)

    body = body_prefix[:content_length]
    remaining = max(0, content_length - len(body))
    chunks = [rewritten_headers, body]
    try:
        while remaining > 0:
            chunk = await asyncio.wait_for(reader.read(min(4096, remaining)), timeout=30.0)
            if not chunk:
                raise UpstreamResponseError
            chunks.append(chunk)
            remaining -= len(chunk)
    except asyncio.TimeoutError:
        raise UpstreamResponseTimeout from None
    except asyncio.IncompleteReadError as exc:
        raise UpstreamResponseError from exc
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

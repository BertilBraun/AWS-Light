from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import Counter, defaultdict
from typing import TYPE_CHECKING

from aws_light.proxy.load_balancer import NoHealthyReplicaError, RoundRobinBalancer
from aws_light.proxy.routing_table import ReplicaEndpoint

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
_PROXY_METRIC_FLUSH_INTERVAL_SECONDS = 1.0
_PROXY_TIMING_LOG_INTERVAL_SECONDS = 5.0
_SERVICE_TOKEN_HEADER = b"x-aws-light-service-token"
_SERVICE_TOKEN_SECRET_PREFIX = "aws-light-service-token-"
_PLATFORM_STORAGE_PREFIX = "/_aws-light/storage"
_CONTROL_PLANE_HOST = "control-plane"
_CONTROL_PLANE_PORT = 8000


class ProxyServer:
    def __init__(
        self,
        balancer: RoundRobinBalancer,
        port: int,
        redis_client: Redis | None = None,
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
        self._metric_flush_task: asyncio.Task[None] | None = None
        self._timing_task: asyncio.Task[None] | None = None
        self._traffic_lock = asyncio.Lock()
        self._traffic_total = 0
        self._traffic_by_service: Counter[str] = Counter()
        self._traffic_by_status: Counter[int] = Counter()
        self._traffic_failures: Counter[str] = Counter()
        self._metrics_lock = asyncio.Lock()
        self._metric_total = 0
        self._metric_by_service: Counter[str] = Counter()
        self._metric_by_status: Counter[int] = Counter()
        self._metric_failures: Counter[str] = Counter()
        self._metric_last_duration_ms: dict[str, float] = {}
        self._metric_rps: Counter[str] = Counter()
        self._metric_ts_requests: defaultdict[int, Counter[str]] = defaultdict(Counter)
        self._metric_ts_status: defaultdict[int, Counter[str]] = defaultdict(Counter)
        self._metric_ts_latency_sum: defaultdict[int, Counter[str]] = defaultdict(Counter)
        self._metric_ts_latency_count: defaultdict[int, Counter[str]] = defaultdict(Counter)
        self._metric_ts_errors: defaultdict[int, Counter[str]] = defaultdict(Counter)
        self._timing_lock = asyncio.Lock()
        self._timing_counts: Counter[str] = Counter()
        self._timing_sums_ms: defaultdict[str, float] = defaultdict(float)
        self._timing_max_ms: dict[str, float] = {}

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle_connection, "0.0.0.0", self._port)
        if self._event_bus is not None:
            self._activity_task = asyncio.create_task(self._traffic_activity_loop())
        if self._redis is not None:
            self._metric_flush_task = asyncio.create_task(self._metric_flush_loop())
        self._timing_task = asyncio.create_task(self._timing_log_loop())
        logger.info("Proxy server listening on port %d", self._port)

    async def stop(self) -> None:
        if self._timing_task is not None:
            self._timing_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._timing_task
        if self._metric_flush_task is not None:
            self._metric_flush_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._metric_flush_task
        if self._activity_task is not None:
            self._activity_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._activity_task
        await self._flush_proxy_metrics()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def _handle_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            request_started = time.perf_counter()
            await self._proxy_request(reader, writer)
            await self._record_timing(
                "total",
                (time.perf_counter() - request_started) * 1000,
            )
        except Exception:
            logger.exception("Error handling proxy connection")
        finally:
            writer.close()
            await writer.wait_closed()

    async def _proxy_request(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        request_started = time.perf_counter()
        header_bytes, body_prefix = await _read_http_head(reader)
        await self._record_timing("read", (time.perf_counter() - request_started) * 1000)
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

        if _extract_request_path(header_bytes).startswith(_PLATFORM_STORAGE_PREFIX):
            await self._forward_to_upstream(
                header_bytes,
                body,
                writer,
                _CONTROL_PLANE_HOST,
                _CONTROL_PLANE_PORT,
                "__platform_storage__",
            )
            return

        service_name = _extract_service_name(header_bytes)
        if service_name is None:
            writer.write(_503_RESPONSE)
            await writer.drain()
            await self._record_proxy_result(None, 503, "missing_host", 0.0)
            return

        auth_started = time.perf_counter()
        source_service = await self._source_service_from_request(header_bytes)
        token_present = _header_value(header_bytes, _SERVICE_TOKEN_HEADER) is not None
        if token_present:
            if source_service is None or not await self._internal_ingress_allowed(
                service_name, source_service
            ):
                await self._record_timing("auth", (time.perf_counter() - auth_started) * 1000)
                writer.write(_403_INTERNAL_RESPONSE)
                await writer.drain()
                await self._record_proxy_result(
                    service_name, 403, "internal_ingress_denied", 0.0
                )
                return
        elif not await self._external_ingress_allowed(service_name):
            await self._record_timing("auth", (time.perf_counter() - auth_started) * 1000)
            writer.write(_403_RESPONSE)
            await writer.drain()
            await self._record_proxy_result(
                service_name, 403, "external_ingress_denied", 0.0
            )
            return
        await self._record_timing("auth", (time.perf_counter() - auth_started) * 1000)

        route_started = time.perf_counter()
        try:
            endpoints = await self._balancer.healthy_replicas_for_request(service_name)
        except NoHealthyReplicaError:
            await self._record_timing("route", (time.perf_counter() - route_started) * 1000)
            writer.write(_503_RESPONSE)
            await writer.drain()
            await self._record_proxy_result(service_name, 503, "no_healthy_replica", 0.0)
            return
        await self._record_timing("route", (time.perf_counter() - route_started) * 1000)

        is_websocket = _is_websocket_upgrade(header_bytes)
        if not is_websocket:
            await self._forward_http_to_candidates(
                header_bytes,
                body,
                writer,
                endpoints,
                service_name,
            )
            return

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

        rewritten_headers = _rewrite_request_headers(header_bytes, endpoint.host, endpoint.port)
        upstream_writer.write(rewritten_headers)
        if body:
            upstream_writer.write(body)
        await upstream_writer.drain()

        await asyncio.gather(
            _pipe_stream(reader, upstream_writer),
            _pipe_stream(upstream_reader, writer),
            return_exceptions=True,
        )

    async def _forward_to_upstream(
        self,
        header_bytes: bytes,
        body: bytes,
        writer: asyncio.StreamWriter,
        upstream_host: str,
        upstream_port: int,
        metric_service: str,
    ) -> None:
        await self._forward_http_to_candidates(
            header_bytes,
            body,
            writer,
            [ReplicaEndpoint(metric_service, upstream_host, upstream_port, healthy=True)],
            metric_service,
        )

    async def _forward_http_to_candidates(
        self,
        header_bytes: bytes,
        body: bytes,
        writer: asyncio.StreamWriter,
        endpoints: list[ReplicaEndpoint],
        metric_service: str,
    ) -> None:
        started = time.perf_counter()
        failures: list[str] = []
        timeout_seen = False
        for endpoint in endpoints:
            try:
                status_code = await self._forward_http_to_endpoint(
                    header_bytes,
                    body,
                    writer,
                    endpoint,
                )
            except (asyncio.TimeoutError, UpstreamResponseTimeout) as error:
                timeout_seen = True
                failures.append(f"{endpoint.replica_id[:8]} {endpoint.host}:{endpoint.port}")
                logger.warning(
                    "Proxy timed out waiting for %s replica %s at %s:%d: %s",
                    metric_service,
                    endpoint.replica_id[:8],
                    endpoint.host,
                    endpoint.port,
                    error,
                )
                continue
            except (OSError, UpstreamResponseError) as error:
                failures.append(f"{endpoint.replica_id[:8]} {endpoint.host}:{endpoint.port}")
                logger.warning(
                    "Proxy could not reach %s replica %s at %s:%d: %s",
                    metric_service,
                    endpoint.replica_id[:8],
                    endpoint.host,
                    endpoint.port,
                    error,
                )
                continue
            duration_ms = (time.perf_counter() - started) * 1000
            await self._record_proxy_result(metric_service, status_code, None, duration_ms)
            return

        status_code = 504 if timeout_seen else 502
        failure_reason = "upstream_response_timeout" if timeout_seen else "upstream_unreachable"
        writer.write(_504_RESPONSE if timeout_seen else _502_RESPONSE)
        await writer.drain()
        duration_ms = (time.perf_counter() - started) * 1000
        logger.warning(
            "Proxy returning %d for %s after %d failed upstream HTTP attempts: %s",
            status_code,
            metric_service,
            len(failures),
            ", ".join(failures) or "none",
        )
        await self._record_proxy_result(metric_service, status_code, failure_reason, duration_ms)

    async def _forward_http_to_endpoint(
        self,
        header_bytes: bytes,
        body: bytes,
        writer: asyncio.StreamWriter,
        endpoint: ReplicaEndpoint,
    ) -> int:
        upstream_started = time.perf_counter()
        upstream_reader, upstream_writer = await asyncio.wait_for(
            asyncio.open_connection(endpoint.host, endpoint.port), timeout=5.0
        )
        try:
            rewritten_headers = _rewrite_request_headers(header_bytes, endpoint.host, endpoint.port)
            upstream_writer.write(rewritten_headers)
            if body:
                upstream_writer.write(body)
            await upstream_writer.drain()

            upstream_response = await _read_full_response(upstream_reader)
            await self._record_timing(
                "upstream_headers",
                (time.perf_counter() - upstream_started) * 1000,
            )
            status_code = _extract_response_status(upstream_response) or 502
            stream_started = time.perf_counter()
            writer.write(upstream_response)
            await writer.drain()
            await self._record_timing("stream", (time.perf_counter() - stream_started) * 1000)
            return status_code
        finally:
            upstream_writer.close()
            await upstream_writer.wait_closed()

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
            if (
                self._service_store is not None
                and await self._service_store.get(service_name) is None
            ):
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

        bucket = (
            int(time.time() // _PROXY_TIMESERIES_BUCKET_SECONDS) * _PROXY_TIMESERIES_BUCKET_SECONDS
        )
        timeseries_service = service_name or "__unknown__"
        async with self._metrics_lock:
            self._metric_total += 1
            self._metric_by_status[status_code] += 1
            if service_name:
                self._metric_by_service[service_name] += 1
                self._metric_last_duration_ms[service_name] = duration_ms
                self._metric_rps[service_name] += 1
            self._metric_ts_requests[bucket][timeseries_service] += 1
            self._metric_ts_status[bucket][str(status_code)] += 1
            self._metric_ts_latency_sum[bucket][timeseries_service] += int(duration_ms * 100)
            self._metric_ts_latency_count[bucket][timeseries_service] += 1
            if status_code >= 500:
                self._metric_ts_errors[bucket][timeseries_service] += 1
            if failure_reason:
                self._metric_failures[failure_reason] += 1
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

    async def _metric_flush_loop(self) -> None:
        while True:
            await asyncio.sleep(_PROXY_METRIC_FLUSH_INTERVAL_SECONDS)
            await self._flush_proxy_metrics()

    async def _flush_proxy_metrics(self) -> None:
        if self._redis is None:
            return
        async with self._metrics_lock:
            total = self._metric_total
            by_service = Counter(self._metric_by_service)
            by_status = Counter(self._metric_by_status)
            failures = Counter(self._metric_failures)
            last_duration_ms = dict(self._metric_last_duration_ms)
            rps = Counter(self._metric_rps)
            ts_requests = {
                bucket: Counter(values) for bucket, values in self._metric_ts_requests.items()
            }
            ts_status = {
                bucket: Counter(values) for bucket, values in self._metric_ts_status.items()
            }
            ts_latency_sum = {
                bucket: Counter(values) for bucket, values in self._metric_ts_latency_sum.items()
            }
            ts_latency_count = {
                bucket: Counter(values) for bucket, values in self._metric_ts_latency_count.items()
            }
            ts_errors = {
                bucket: Counter(values) for bucket, values in self._metric_ts_errors.items()
            }
            self._metric_total = 0
            self._metric_by_service.clear()
            self._metric_by_status.clear()
            self._metric_failures.clear()
            self._metric_last_duration_ms.clear()
            self._metric_rps.clear()
            self._metric_ts_requests.clear()
            self._metric_ts_status.clear()
            self._metric_ts_latency_sum.clear()
            self._metric_ts_latency_count.clear()
            self._metric_ts_errors.clear()
        if total == 0:
            return

        pipe = self._redis.pipeline()
        pipe.incrby(_PROXY_METRIC_TOTAL, total)
        for service, count in rps.items():
            pipe.incrby(f"rps:{service}", count)
        for status_code, count in by_status.items():
            pipe.hincrby(_PROXY_METRIC_BY_STATUS, str(status_code), count)
        for service, count in by_service.items():
            pipe.hincrby(_PROXY_METRIC_BY_SERVICE, service, count)
        for service, duration_ms in last_duration_ms.items():
            pipe.set(f"proxy:last_duration_ms:{service}", f"{duration_ms:.2f}")
        for bucket, values in ts_requests.items():
            for service, count in values.items():
                pipe.hincrby(f"proxy:ts:requests:{bucket}", service, count)
        for bucket, values in ts_status.items():
            for status_field, count in values.items():
                pipe.hincrby(f"proxy:ts:status:{bucket}", status_field, count)
        for bucket, values in ts_latency_sum.items():
            for service, amount in values.items():
                pipe.hincrby(f"proxy:ts:latency_sum:{bucket}", service, amount)
        for bucket, values in ts_latency_count.items():
            for service, count in values.items():
                pipe.hincrby(f"proxy:ts:latency_count:{bucket}", service, count)
        for bucket, values in ts_errors.items():
            for service, count in values.items():
                pipe.hincrby(f"proxy:ts:errors:{bucket}", service, count)
        for failure_reason, count in failures.items():
            pipe.hincrby(_PROXY_METRIC_FAILURES, failure_reason, count)
        await pipe.execute()

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

    async def _record_timing(self, stage: str, duration_ms: float) -> None:
        async with self._timing_lock:
            self._timing_counts[stage] += 1
            self._timing_sums_ms[stage] += duration_ms
            self._timing_max_ms[stage] = max(
                duration_ms,
                self._timing_max_ms.get(stage, 0.0),
            )

    async def _timing_snapshot(self) -> dict[str, dict[str, float | int]]:
        async with self._timing_lock:
            counts = Counter(self._timing_counts)
            sums_ms = dict(self._timing_sums_ms)
            max_ms = dict(self._timing_max_ms)
            self._timing_counts.clear()
            self._timing_sums_ms.clear()
            self._timing_max_ms.clear()
        return {
            stage: {
                "count": count,
                "avg_ms": round(sums_ms[stage] / count, 2),
                "max_ms": round(max_ms.get(stage, 0.0), 2),
            }
            for stage, count in counts.items()
            if count
        }

    async def _timing_log_loop(self) -> None:
        while True:
            await asyncio.sleep(_PROXY_TIMING_LOG_INTERVAL_SECONDS)
            await self._log_timing_snapshot()

    async def _log_timing_snapshot(self) -> None:
        snapshot = await self._timing_snapshot()
        if not snapshot:
            return
        parts = [
            (
                f"{stage}=count:{values['count']} "
                f"avg:{values['avg_ms']:.2f}ms max:{values['max_ms']:.2f}ms"
            )
            for stage, values in sorted(snapshot.items())
        ]
        logger.info(
            "Proxy timing summary over %.0fs: %s",
            _PROXY_TIMING_LOG_INTERVAL_SECONDS,
            "; ".join(parts),
        )

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


def _extract_request_path(header_bytes: bytes) -> str:
    request_line = header_bytes.split(b"\r\n", 1)[0]
    parts = request_line.split()
    if len(parts) < 2:
        return ""
    return parts[1].decode(errors="replace")


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

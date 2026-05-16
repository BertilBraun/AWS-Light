from __future__ import annotations

import asyncio
import contextlib

from redis.asyncio import Redis

from aws_light.models.events import WebSocketEvent

_STREAM_KEY = "events"
_STREAM_MAXLEN = 1000
_BLOCK_TIMEOUT_MS = 30_000


class RedisEventBus:
    def __init__(self, redis_client: Redis) -> None:  # type: ignore[type-arg]
        self._redis = redis_client
        self._subscribers: dict[asyncio.Queue[WebSocketEvent], asyncio.Task[None]] = {}

    async def publish(self, event: WebSocketEvent) -> None:
        await self._redis.xadd(
            _STREAM_KEY,
            {"event": event.model_dump_json()},
            maxlen=_STREAM_MAXLEN,
            approximate=True,
        )

    async def subscribe(self) -> asyncio.Queue[WebSocketEvent]:
        queue: asyncio.Queue[WebSocketEvent] = asyncio.Queue(maxsize=200)
        last_id = await self._current_stream_id()
        task = asyncio.create_task(self._read_loop(queue, last_id))
        self._subscribers[queue] = task
        return queue

    async def unsubscribe(self, queue: asyncio.Queue[WebSocketEvent]) -> None:
        task = self._subscribers.pop(queue, None)
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def get_recent_events(self) -> list[WebSocketEvent]:
        entries = await self._redis.xrange(_STREAM_KEY, count=100)
        events = []
        for _entry_id, fields in entries:
            raw = fields.get(b"event") or fields.get("event")
            if raw is not None:
                events.append(WebSocketEvent.model_validate_json(raw))
        return events

    async def _current_stream_id(self) -> str:
        info = await self._redis.xinfo_stream(_STREAM_KEY)
        if isinstance(info, dict):
            last = info.get("last-generated-id") or info.get("last_generated_id") or "0-0"
        else:
            last = "0-0"
        return str(last)

    async def _read_loop(self, queue: asyncio.Queue[WebSocketEvent], last_id: str) -> None:
        current_id = last_id
        while True:
            try:
                results = await self._redis.xread(
                    {_STREAM_KEY: current_id},
                    block=_BLOCK_TIMEOUT_MS,
                    count=100,
                )
                if not results:
                    continue
                for _stream, entries in results:
                    for entry_id, fields in entries:
                        raw = fields.get(b"event") or fields.get("event")
                        if raw is not None:
                            event = WebSocketEvent.model_validate_json(raw)
                            with contextlib.suppress(asyncio.QueueFull):
                                queue.put_nowait(event)
                        current_id = entry_id.decode() if isinstance(entry_id, bytes) else entry_id
            except asyncio.CancelledError:
                return
            except Exception:
                await asyncio.sleep(1)

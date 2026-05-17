from __future__ import annotations

import asyncio
from collections import deque

from aws_light.models.events import WebSocketEvent

_RECENT_EVENT_BUFFER_SIZE = 100


class EventBus:
    def __init__(self) -> None:
        self._subscribers: list[asyncio.Queue[WebSocketEvent]] = []
        self._recent_events: deque[WebSocketEvent] = deque(maxlen=_RECENT_EVENT_BUFFER_SIZE)
        self._lock = asyncio.Lock()

    async def publish(self, event: WebSocketEvent) -> None:
        async with self._lock:
            self._recent_events.append(event)
            dead_queues = []
            for queue in self._subscribers:
                try:
                    queue.put_nowait(event)
                except asyncio.QueueFull:
                    dead_queues.append(queue)
            for queue in dead_queues:
                self._subscribers.remove(queue)

    async def subscribe(self) -> asyncio.Queue[WebSocketEvent]:
        queue: asyncio.Queue[WebSocketEvent] = asyncio.Queue(maxsize=200)
        async with self._lock:
            self._subscribers.append(queue)
        return queue

    async def unsubscribe(self, queue: asyncio.Queue[WebSocketEvent]) -> None:
        async with self._lock:
            if queue in self._subscribers:
                self._subscribers.remove(queue)

    async def get_recent_events(self) -> list[WebSocketEvent]:
        return list(self._recent_events)


event_bus = EventBus()

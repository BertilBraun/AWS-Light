from __future__ import annotations

import asyncio

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from aws_light.dependencies import (
    get_event_bus,
    get_node_store,
    get_secrets_manager,
    get_service_store,
    get_storage_service,
)

router = APIRouter(tags=["dashboard"])


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    await websocket.accept()

    event_bus = get_event_bus()
    service_store = get_service_store()
    node_store = get_node_store()
    secrets_manager = get_secrets_manager()
    storage_service = get_storage_service()

    services = await service_store.list()
    nodes = await node_store.list()
    secret_names = await secrets_manager.list_secret_names()
    buckets = storage_service.list_buckets()
    recent_events = await event_bus.get_recent_events()

    snapshot = {
        "kind": "snapshot",
        "nodes": [node.model_dump(mode="json") for node in nodes],
        "services": [service.model_dump(mode="json") for service in services],
        "secrets": secret_names,
        "buckets": [bucket.model_dump(mode="json") for bucket in buckets],
        "events": [event.model_dump(mode="json") for event in recent_events],
    }
    await websocket.send_json(snapshot)

    queue = await event_bus.subscribe()
    try:
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=30.0)
                await websocket.send_json(event.model_dump(mode="json"))
            except asyncio.TimeoutError:
                await websocket.send_json({"kind": "ping"})
    except WebSocketDisconnect:
        pass
    finally:
        await event_bus.unsubscribe(queue)

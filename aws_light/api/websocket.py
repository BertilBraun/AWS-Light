from __future__ import annotations

import asyncio

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

router = APIRouter(tags=["dashboard"])


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    from aws_light.main import (
        get_event_bus,
        get_node_manager,
        get_secrets_manager,
        get_service_store,
        get_storage_service,
    )

    await websocket.accept()

    event_bus = get_event_bus()
    service_store = get_service_store()
    node_manager = get_node_manager()
    secrets_manager = get_secrets_manager()
    storage_service = get_storage_service()

    services = await service_store.list()
    nodes = node_manager.get_all_nodes()
    secret_names = await secrets_manager.list_secret_names()
    buckets = storage_service.list_buckets()
    recent_events = event_bus.get_recent_events()

    snapshot = {
        "kind": "snapshot",
        "services": [service.model_dump(mode="json") for service in services],
        "nodes": [node.model_dump(mode="json") for node in nodes],
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

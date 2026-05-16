from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from aws_light.iam.middleware import get_current_user
from aws_light.models.iam import UserSpec
from aws_light.models.node import NodeState

router = APIRouter(prefix="/api/v1/nodes", tags=["nodes"])


def _get_node_manager():  # type: ignore[no-untyped-def]
    from aws_light.main import get_node_manager

    return get_node_manager()


@router.get("", response_model=list[NodeState])
async def list_nodes(_: UserSpec = Depends(get_current_user)) -> list[NodeState]:
    node_manager = _get_node_manager()
    return node_manager.get_all_nodes()


@router.get("/{node_id}", response_model=NodeState)
async def get_node(node_id: str, _: UserSpec = Depends(get_current_user)) -> NodeState:
    node_manager = _get_node_manager()
    node = node_manager.get_node(node_id)
    if node is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Node not found")
    return node

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from aws_light.dependencies import get_node_store
from aws_light.iam.middleware import get_current_user
from aws_light.models.iam import UserSpec
from aws_light.models.node import NodeState

router = APIRouter(prefix="/api/v1/nodes", tags=["nodes"])


@router.get("", response_model=list[NodeState])
async def list_nodes(_: UserSpec = Depends(get_current_user)) -> list[NodeState]:
    return await get_node_store().list()


@router.get("/{node_id}", response_model=NodeState)
async def get_node(node_id: str, _: UserSpec = Depends(get_current_user)) -> NodeState:
    node = await get_node_store().get(node_id)
    if node is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Node not found")
    return node

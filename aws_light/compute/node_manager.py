from __future__ import annotations

from aws_light.config import settings
from aws_light.models.common import ResourceStatus
from aws_light.models.node import NodeSpec, NodeState, ResourceUsage


class NodeManager:
    def __init__(self) -> None:
        self._nodes: dict[str, NodeState] = {}

    def initialize(self) -> None:
        for index in range(settings.node_count):
            node_id = f"node-{index:02d}"
            self._nodes[node_id] = NodeState(
                spec=NodeSpec(
                    node_id=node_id,
                    cpu_capacity=settings.node_cpu_capacity,
                    memory_capacity_mb=settings.node_memory_capacity_mb,
                ),
                usage=ResourceUsage(),
                status=ResourceStatus.RUNNING,
                replica_ids=[],
            )

    def get_all_nodes(self) -> list[NodeState]:
        return list(self._nodes.values())

    def get_node(self, node_id: str) -> NodeState | None:
        return self._nodes.get(node_id)

    def allocate(self, node_id: str, replica_id: str, cpu: float, memory_mb: float) -> None:
        node = self._nodes[node_id]
        node.usage.cpu_used += cpu
        node.usage.memory_used_mb += memory_mb
        node.replica_ids.append(replica_id)

    def deallocate(self, node_id: str, replica_id: str, cpu: float, memory_mb: float) -> None:
        node = self._nodes.get(node_id)
        if node is None:
            return
        node.usage.cpu_used = max(0.0, node.usage.cpu_used - cpu)
        node.usage.memory_used_mb = max(0.0, node.usage.memory_used_mb - memory_mb)
        if replica_id in node.replica_ids:
            node.replica_ids.remove(replica_id)

from __future__ import annotations

from aws_light.models.node import NodeState


class SchedulingError(Exception):
    pass


class BinPackScheduler:
    def select_node(
        self,
        nodes: list[NodeState],
        cpu_request: float,
        memory_request_mb: float,
    ) -> NodeState:
        candidates = [
            node
            for node in nodes
            if node.available_cpu >= cpu_request and node.available_memory_mb >= memory_request_mb
        ]
        if not candidates:
            raise SchedulingError(
                f"No node can fit request: cpu={cpu_request}, memory={memory_request_mb}mb"
            )
        # Bin-pack: prefer the node with the least available capacity (most full)
        return min(candidates, key=lambda node: node.available_cpu)

from __future__ import annotations

from aws_light.models.node import NodeState


class SchedulingError(Exception):
    pass


class Scheduler:
    def select_node(
        self,
        nodes: list[NodeState],
        cpu_request: float,
        memory_request_mb: float,
    ) -> NodeState:
        raise NotImplementedError


class BinPackScheduler(Scheduler):
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
        # Bin-pack: prefer the most allocated node that still has capacity.
        return min(
            candidates,
            key=lambda node: (
                node.available_cpu,
                node.available_memory_mb,
                node.spec.node_id,
            ),
        )


class SpreadScheduler(Scheduler):
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
        # Spread: prefer the least occupied node.
        return min(
            candidates,
            key=lambda node: (
                len(node.replica_ids),
                node.usage.cpu_used,
                node.usage.memory_used_mb,
                node.spec.node_id,
            ),
        )


def create_scheduler(policy: str) -> Scheduler:
    normalized = policy.strip().lower()
    if normalized == "binpack":
        return BinPackScheduler()
    if normalized == "spread":
        return SpreadScheduler()
    raise ValueError(f"Unknown scheduler policy '{policy}'. Use 'binpack' or 'spread'.")

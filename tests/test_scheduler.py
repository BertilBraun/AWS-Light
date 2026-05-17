from __future__ import annotations

import pytest

from aws_light.compute.scheduler import BinPackScheduler, SchedulingError
from aws_light.models.common import ResourceStatus
from aws_light.models.node import NodeSpec, NodeState, ResourceUsage


def _make_node(
    node_id: str,
    cpu_used: float = 0.0,
    memory_used_mb: float = 0.0,
    replica_ids: list[str] | None = None,
) -> NodeState:
    return NodeState(
        spec=NodeSpec(node_id=node_id, cpu_capacity=0.5, memory_capacity_mb=512),
        usage=ResourceUsage(cpu_used=cpu_used, memory_used_mb=memory_used_mb),
        status=ResourceStatus.RUNNING,
        replica_ids=replica_ids or [],
    )


def test_select_node_prefers_node_with_fewest_replicas() -> None:
    scheduler = BinPackScheduler()
    occupied = _make_node("node-00", cpu_used=0.1, replica_ids=["replica-1"])
    empty = _make_node("node-01", cpu_used=0.0)
    selected = scheduler.select_node([occupied, empty], 0.1, 64)
    assert selected.spec.node_id == "node-01"


def test_select_node_spreads_across_empty_nodes_by_name() -> None:
    scheduler = BinPackScheduler()
    second = _make_node("node-01")
    first = _make_node("node-00")
    selected = scheduler.select_node([second, first], 0.1, 64)
    assert selected.spec.node_id == "node-00"


def test_select_node_uses_load_as_tiebreaker() -> None:
    scheduler = BinPackScheduler()
    lightly_loaded = _make_node("node-00", cpu_used=0.1, replica_ids=["replica-1"])
    heavily_loaded = _make_node("node-01", cpu_used=0.2, replica_ids=["replica-2"])
    selected = scheduler.select_node([heavily_loaded, lightly_loaded], 0.1, 64)
    assert selected.spec.node_id == "node-00"


def test_select_node_raises_when_no_node_fits_cpu() -> None:
    scheduler = BinPackScheduler()
    full_node = _make_node("node-00", cpu_used=0.45)
    with pytest.raises(SchedulingError):
        scheduler.select_node([full_node], cpu_request=0.25, memory_request_mb=64)


def test_select_node_raises_when_no_node_fits_memory() -> None:
    scheduler = BinPackScheduler()
    full_node = _make_node("node-00", memory_used_mb=450)
    with pytest.raises(SchedulingError):
        scheduler.select_node([full_node], cpu_request=0.1, memory_request_mb=128)


def test_select_node_raises_on_empty_node_list() -> None:
    scheduler = BinPackScheduler()
    with pytest.raises(SchedulingError):
        scheduler.select_node([], cpu_request=0.1, memory_request_mb=64)


def test_select_node_with_exact_fit_succeeds() -> None:
    scheduler = BinPackScheduler()
    node = _make_node("node-00", cpu_used=0.25, memory_used_mb=256)
    selected = scheduler.select_node([node], cpu_request=0.25, memory_request_mb=256)
    assert selected.spec.node_id == "node-00"

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from aws_light.autoscaler.autoscaler import (
    _SCALE_DOWN_CONSECUTIVE_CHECKS_REQUIRED,
    _SCALE_DOWN_CPU_THRESHOLD,
    _SCALE_DOWN_RPS_THRESHOLD,
    _SCALE_UP_CPU_THRESHOLD,
    Autoscaler,
)
from aws_light.autoscaler.metrics_collector import MetricsCollector, ServiceMetrics
from aws_light.dashboard.event_bus import EventBus
from aws_light.models.common import ResourceStatus
from aws_light.models.service import ServiceSpec, ServiceState
from aws_light.store.json_store import JsonStore


def _make_service(
    name: str = "svc",
    replicas: int = 2,
    min_replicas: int = 1,
    max_replicas: int = 5,
) -> ServiceState:
    return ServiceState(
        spec=ServiceSpec(
            name=name,
            image="test:latest",
            replicas=replicas,
            min_replicas=min_replicas,
            max_replicas=max_replicas,
        ),
        status=ResourceStatus.RUNNING,
    )


@pytest.fixture()
async def service_store(tmp_path: Path) -> JsonStore[ServiceState]:
    store: JsonStore[ServiceState] = JsonStore(tmp_path / "services.json", ServiceState)
    return store


@pytest.fixture()
def event_bus() -> EventBus:
    return EventBus()


@pytest.fixture()
def mock_metrics() -> MetricsCollector:
    collector = MagicMock(spec=MetricsCollector)
    collector.collect = AsyncMock(
        return_value=ServiceMetrics(average_cpu_percent=0.0, requests_per_second=0.0)
    )
    return collector


@pytest.fixture()
def autoscaler(
    service_store: JsonStore[ServiceState],
    mock_metrics: MetricsCollector,
    event_bus: EventBus,
) -> Autoscaler:
    return Autoscaler(
        service_store=service_store,
        metrics_collector=mock_metrics,
        event_bus=event_bus,
    )


async def test_scale_up_when_cpu_above_threshold(
    autoscaler: Autoscaler,
    service_store: JsonStore[ServiceState],
    mock_metrics: MetricsCollector,
) -> None:
    service = _make_service(replicas=2)
    await service_store.put("svc", service)
    mock_metrics.collect = AsyncMock(  # type: ignore[method-assign]
        return_value=ServiceMetrics(
            average_cpu_percent=_SCALE_UP_CPU_THRESHOLD + 1, requests_per_second=0.0
        )
    )
    await autoscaler._evaluate_all_services()
    updated = await service_store.get("svc")
    assert updated is not None
    assert updated.spec.replicas == 3


async def test_scale_up_clamps_at_max_replicas(
    autoscaler: Autoscaler,
    service_store: JsonStore[ServiceState],
    mock_metrics: MetricsCollector,
) -> None:
    service = _make_service(replicas=5, max_replicas=5)
    await service_store.put("svc", service)
    mock_metrics.collect = AsyncMock(  # type: ignore[method-assign]
        return_value=ServiceMetrics(average_cpu_percent=99.0, requests_per_second=0.0)
    )
    await autoscaler._evaluate_all_services()
    updated = await service_store.get("svc")
    assert updated is not None
    assert updated.spec.replicas == 5


async def test_scale_down_requires_consecutive_checks(
    autoscaler: Autoscaler,
    service_store: JsonStore[ServiceState],
    mock_metrics: MetricsCollector,
) -> None:
    service = _make_service(replicas=3)
    await service_store.put("svc", service)
    mock_metrics.collect = AsyncMock(  # type: ignore[method-assign]
        return_value=ServiceMetrics(
            average_cpu_percent=_SCALE_DOWN_CPU_THRESHOLD - 1,
            requests_per_second=_SCALE_DOWN_RPS_THRESHOLD - 1,
        )
    )
    for _ in range(_SCALE_DOWN_CONSECUTIVE_CHECKS_REQUIRED - 1):
        await autoscaler._evaluate_all_services()

    still_same = await service_store.get("svc")
    assert still_same is not None
    assert still_same.spec.replicas == 3

    await autoscaler._evaluate_all_services()
    scaled_down = await service_store.get("svc")
    assert scaled_down is not None
    assert scaled_down.spec.replicas == 2


async def test_scale_down_clamps_at_min_replicas(
    autoscaler: Autoscaler,
    service_store: JsonStore[ServiceState],
    mock_metrics: MetricsCollector,
) -> None:
    service = _make_service(replicas=1, min_replicas=1)
    await service_store.put("svc", service)
    mock_metrics.collect = AsyncMock(  # type: ignore[method-assign]
        return_value=ServiceMetrics(average_cpu_percent=0.0, requests_per_second=0.0)
    )
    for _ in range(_SCALE_DOWN_CONSECUTIVE_CHECKS_REQUIRED):
        await autoscaler._evaluate_all_services()
    not_scaled = await service_store.get("svc")
    assert not_scaled is not None
    assert not_scaled.spec.replicas == 1


async def test_no_scaling_when_min_equals_max(
    autoscaler: Autoscaler,
    service_store: JsonStore[ServiceState],
    mock_metrics: MetricsCollector,
) -> None:
    service = _make_service(replicas=2, min_replicas=2, max_replicas=2)
    await service_store.put("svc", service)
    mock_metrics.collect = AsyncMock(  # type: ignore[method-assign]
        return_value=ServiceMetrics(average_cpu_percent=99.0, requests_per_second=999.0)
    )
    await autoscaler._evaluate_all_services()
    unchanged = await service_store.get("svc")
    assert unchanged is not None
    assert unchanged.spec.replicas == 2
    mock_metrics.collect.assert_not_called()

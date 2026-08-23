"""Focused unit tests for the runtime daily-operation diary."""
from __future__ import annotations

import asyncio
import copy
import json
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from custom_components.omnibattery.tracking.daily_timeline import (
    ACTION_DISCHARGE,
    ACTION_GRID_CHARGE,
    ACTION_SOLAR_CHARGE,
    CONTEXT_DYNAMIC_PRICE,
    DailyOperationTimelineManager,
)

MADRID = ZoneInfo("Europe/Madrid")


class FakeStore:
    """Async Store double retaining exactly what the manager writes."""

    def __init__(self, data=None):
        self.data = copy.deepcopy(data)
        self.writes: list[dict] = []

    async def async_load(self):
        return copy.deepcopy(self.data)

    async def async_save(self, data):
        self.data = copy.deepcopy(data)
        self.writes.append(copy.deepcopy(data))


class MutableClock:
    def __init__(self, value: datetime):
        self.value = value

    def __call__(self) -> datetime:
        return self.value


def _manager(clock: MutableClock, store: FakeStore | None = None, *, mode="dynamic_pricing"):
    hass = SimpleNamespace(config=SimpleNamespace(time_zone="Europe/Madrid"))
    entry = SimpleNamespace(
        entry_id="entry-one",
        data={"pricing_mode": mode, "household_consumption_sensor": "sensor.house"},
        options={},
    )
    return DailyOperationTimelineManager(
        hass,
        entry,
        SimpleNamespace(pricing_mode=mode),
        store=store or FakeStore(),
        now_provider=clock,
        debounce_seconds=0.01,
    )


def _capture(value: float, coverage: float = 900.0):
    values = [None] * 96
    coverages = [0.0] * 96
    values[40] = value
    coverages[40] = coverage
    return {"interval_energy_kwh": values, "interval_coverage_s": coverages}


@pytest.mark.asyncio
async def test_fake_persistence_restores_current_day_and_actions():
    clock = MutableClock(datetime(2026, 8, 23, 10, 7, tzinfo=MADRID))
    store = FakeStore()
    manager = _manager(clock, store)

    manager.refresh_actual_partial(_capture(0.21), _capture(0.42, 600.0))
    manager.record_runtime_decision(
        {"source": "chronological", "slot": "slot-1"},
        action_mask=ACTION_SOLAR_CHARGE | ACTION_GRID_CHARGE,
        context_mask=CONTEXT_DYNAMIC_PRICE,
        duration_s=120,
        simultaneous=True,
    )
    await manager.async_save_all()

    assert len(store.writes) == 1
    restored = _manager(clock, store)
    assert await restored.async_load() is True

    snapshot = restored.build_public_snapshot()
    assert snapshot["local_date"] == "2026-08-23"
    assert snapshot["operations"]["actual_action_mask"][40] == 3
    assert snapshot["operations"]["actual_context_mask"][40] & CONTEXT_DYNAMIC_PRICE
    assert (
        snapshot["operations"]["observed_seconds_by_action_by_interval"][40]["solar_charge"]
        == 120
    )
    assert snapshot["series"]["solar_actual_kwh"][40] == pytest.approx(0.42)
    assert snapshot["series"]["consumption_actual_kwh"][40] == pytest.approx(0.21)


def test_closed_intervals_are_immutable_and_reevaluation_starts_at_present():
    clock = MutableClock(datetime(2026, 8, 23, 10, 7, tzinfo=MADRID))
    manager = _manager(clock)
    manager.refresh_actual_partial(consumption_kwh=1.0, solar_kwh=0.5, coverage_s=300)
    manager.record_runtime_decision(action_mask=ACTION_SOLAR_CHARGE, duration_s=300)
    manager.rebuild_future_projection(
        [{"index": 40, "action_mask": ACTION_GRID_CHARGE}, {"index": 41, "action_mask": ACTION_DISCHARGE}],
        mode="dynamic_pricing",
    )

    clock.value = datetime(2026, 8, 23, 10, 16, tzinfo=MADRID)
    manager.refresh_actual_partial(consumption_kwh=2.0, solar_kwh=0.1, coverage_s=100)
    before = manager.build_public_snapshot()
    closed_action = before["operations"]["actual_action_mask"][40]
    closed_solar = before["series"]["solar_actual_kwh"][40]
    assert before["operations"]["closed"][40] is True

    # The old current interval is now closed.  Both an old runtime callback and
    # a new projection are forbidden from rewriting it.
    assert manager.record_runtime_decision(
        {"action_mask": ACTION_DISCHARGE}, at=datetime(2026, 8, 23, 10, 5, tzinfo=MADRID)
    ) is False
    manager.rebuild_future_projection(
        [
            {"index": 40, "action_mask": ACTION_DISCHARGE},
            {"index": 41, "action_mask": ACTION_GRID_CHARGE},
        ],
        mode="dynamic_pricing",
    )
    after = manager.build_public_snapshot()
    assert after["operations"]["actual_action_mask"][40] == closed_action
    assert after["series"]["solar_actual_kwh"][40] == closed_solar
    assert after["operations"]["planned_action_mask"][41] == ACTION_GRID_CHARGE


@pytest.mark.asyncio
async def test_corrupt_store_degrades_to_empty_diary():
    clock = MutableClock(datetime(2026, 8, 23, 10, 7, tzinfo=MADRID))
    store = FakeStore(["not", "a", "timeline"])
    manager = _manager(clock, store)

    assert await manager.async_load() is False
    assert manager.last_error == "load: invalid_store"
    snapshot = manager.build_public_snapshot()
    assert snapshot["interval_count"] == 96
    assert all(value == 0 for value in snapshot["operations"]["actual_action_mask"])


@pytest.mark.asyncio
async def test_debounce_coalesces_many_runtime_updates():
    clock = MutableClock(datetime(2026, 8, 23, 10, 7, tzinfo=MADRID))
    store = FakeStore()
    manager = _manager(clock, store)

    for duration in (1, 2, 3, 4):
        manager.record_runtime_decision(action_mask=ACTION_GRID_CHARGE, duration_s=duration)
    await asyncio.sleep(0.03)

    assert len(store.writes) == 1
    assert store.writes[0]["cells"][40]["observed_seconds_by_action"]["grid_charge"] == 10


def test_snapshot_has_96_lists_and_is_strictly_json_safe():
    clock = MutableClock(datetime(2026, 8, 23, 10, 7, tzinfo=MADRID))
    manager = _manager(clock)
    manager.refresh_actual_partial(
        consumption_kwh=float("nan"),
        solar_kwh=float("inf"),
        coverage_s=float("nan"),
    )
    manager.rebuild_future_projection(
        [{"index": 41, "solar_kwh": float("nan"), "consumption_kwh": 0.2}],
        mode="dynamic_pricing",
    )
    snapshot = manager.build_public_snapshot()

    assert snapshot["interval_count"] == 96
    for section in (snapshot["series"], snapshot["operations"]):
        for value in section.values():
            if isinstance(value, list):
                assert len(value) == 96
    json.dumps(snapshot, allow_nan=False)


def test_realtime_price_keeps_real_current_decision_but_no_future_plan():
    clock = MutableClock(datetime(2026, 8, 23, 10, 7, tzinfo=MADRID))
    manager = _manager(clock, mode="realtime_price")
    manager.record_runtime_decision(
        {"mode": "realtime_price", "grid_charge_decision": "scheduled"},
        action_mask=ACTION_GRID_CHARGE,
        duration_s=90,
    )
    manager.rebuild_future_projection(
        [
            {"index": 40, "action_mask": ACTION_GRID_CHARGE, "solar_kwh": 1.0},
            {"index": 41, "action_mask": ACTION_GRID_CHARGE, "solar_kwh": 1.0},
            {"index": 42, "action_mask": ACTION_DISCHARGE, "solar_kwh": 1.0},
        ],
        mode="realtime_price",
    )
    snapshot = manager.build_public_snapshot()

    assert snapshot["operations"]["actual_action_mask"][40] == ACTION_GRID_CHARGE
    assert snapshot["operations"]["planned_action_mask"][40] == ACTION_GRID_CHARGE
    assert snapshot["operations"]["planned_action_mask"][41] == 0
    assert snapshot["operations"]["planned_action_mask"][42] == 0
    assert snapshot["series"]["solar_forecast_kwh"][41] is None
    assert snapshot["series"]["solar_forecast_kwh"][42] is None

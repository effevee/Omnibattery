"""Tests for deadline-aware predictive charge allocation."""
import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from custom_components.omnibattery.pricing import PriceSlot
from custom_components.omnibattery.pricing.chronological import (
    ChronologicalPlan,
    EnergyInterval,
    SlotAllocation,
    allocate_price_slots,
    build_energy_deadlines,
    normalize_energy_shape,
    simulate_allocations,
)
from custom_components.omnibattery.pricing.engine import PricingManager


BASE = datetime(2026, 8, 18)


def _interval(index, consumption, solar=0.0):
    start = BASE + timedelta(minutes=15 * index)
    return EnergyInterval(start, start + timedelta(minutes=15), consumption, solar)


def _slot(start_minutes, end_minutes, price):
    return PriceSlot(
        BASE + timedelta(minutes=start_minutes),
        BASE + timedelta(minutes=end_minutes),
        price,
    )


def test_normalized_shape_preserves_exact_total():
    result = normalize_energy_shape([1, 2, 3], 2.16)
    assert sum(result) == pytest.approx(2.16)


def test_later_solar_does_not_erase_early_deadline():
    intervals = [
        _interval(0, 1.0),
        _interval(1, 1.0),
        _interval(2, 0.0, 5.0),
    ]
    deadlines = build_energy_deadlines(intervals, usable_initial_kwh=1.0)
    assert deadlines
    assert deadlines[-1].required_cumulative_kwh == pytest.approx(1.0)
    assert deadlines[-1].deadline <= intervals[1].end


def test_reference_pattern_reserves_early_energy_and_keeps_rest_flexible():
    intervals = [_interval(i, 0.1) for i in range(96)]
    # 1.30 kWh has been consumed beyond usable storage by 03:30.
    deadlines = build_energy_deadlines(intervals[:14], usable_initial_kwh=0.1)
    early_a = _slot(30, 45, 0.20)
    early_b = _slot(45, 60, 0.21)
    cheap_late = _slot(13 * 60 + 45, 14 * 60, 0.01)
    plan = allocate_price_slots(
        intervals,
        deadlines,
        [cheap_late, early_b, early_a],
        total_required_kwh=2.16,
        effective_power_kw=6.0,
        now=BASE + timedelta(minutes=5),
        horizon_end=BASE + timedelta(days=1),
        headroom_kwh=5.0,
        usable_initial_kwh=0.1,
    )
    early = sum(a.planned_battery_kwh for a in plan.allocations if a.deadline)
    flexible = sum(a.planned_battery_kwh for a in plan.allocations if not a.deadline)
    assert early == pytest.approx(1.3)
    assert flexible == pytest.approx(0.86)
    assert next(a for a in plan.allocations if a.slot == cheap_late).kind == "flexible"


def test_cheapest_slot_after_deadline_cannot_cover_early_requirement():
    intervals = [_interval(0, 1.0)]
    deadlines = build_energy_deadlines(intervals, usable_initial_kwh=0.0)
    late = _slot(60, 75, -1.0)
    plan = allocate_price_slots(
        intervals,
        deadlines,
        [late],
        total_required_kwh=1.0,
        effective_power_kw=4.0,
        now=BASE,
        horizon_end=BASE + timedelta(days=1),
    )
    assert plan.deadline_shortfall_kwh == pytest.approx(1.0)
    assert plan.allocations == []


def test_crossing_slot_only_contributes_capacity_before_deadline():
    intervals = [_interval(2, 1.0)]  # deadline 00:45
    deadline = build_energy_deadlines(intervals, 0.0)
    crossing = _slot(30, 60, 0.1)
    plan = allocate_price_slots(
        intervals,
        deadline,
        [crossing],
        total_required_kwh=1.0,
        effective_power_kw=2.0,
        charge_efficiency=1.0,
        now=BASE,
        horizon_end=BASE + timedelta(days=1),
    )
    assert plan.allocations[0].planned_battery_kwh == pytest.approx(0.5)
    assert plan.total_shortfall_kwh == pytest.approx(0.5)


def test_current_slot_uses_only_remaining_duration():
    slot = _slot(0, 15, 0.1)
    plan = allocate_price_slots(
        [],
        [],
        [slot],
        total_required_kwh=1.0,
        effective_power_kw=6.0,
        charge_efficiency=1.0,
        now=BASE + timedelta(minutes=5),
        horizon_end=BASE + timedelta(days=1),
    )
    assert plan.allocated_kwh == pytest.approx(1.0)


def test_price_ceiling_reports_deadline_shortfall():
    interval = _interval(0, 0.5)
    plan = allocate_price_slots(
        [interval],
        build_energy_deadlines([interval], 0.0),
        [_slot(0, 15, 0.3)],
        total_required_kwh=0.5,
        effective_power_kw=4.0,
        now=BASE,
        horizon_end=BASE + timedelta(days=1),
        max_price_threshold=0.2,
    )
    assert plan.deadline_shortfall_kwh == pytest.approx(0.5)
    assert plan.reason == "price_threshold"


def test_simulation_applies_allocation_at_interval_boundary_once():
    intervals = [_interval(0, 1.0), _interval(1, 1.0)]
    slot = _slot(0, 15, 0.1)
    allocation = SlotAllocation(slot, 1.0, intervals[0].end, "deadline")
    result = simulate_allocations(intervals, 0.0, [allocation])
    assert result.final_projected_energy_kwh == pytest.approx(-1.0)


def test_time_slot_windows_are_materialized_and_split_at_midnight():
    now = BASE + timedelta(minutes=30)
    controller = SimpleNamespace(
        charging_time_slots=[
            {
                "start_time": "22:00",
                "end_time": "02:00",
                "days": ["tue"],
            },
            {
                "start_time": "10:00",
                "end_time": "11:00",
                "days": ["tue"],
            },
        ]
    )

    slots = PricingManager(SimpleNamespace(), controller)._time_slot_price_slots(now)

    assert [(slot.start.hour, slot.end.hour) for slot in slots] == [
        (0, 2),
        (10, 11),
        (22, 0),
    ]
    assert slots[-1].end.date() == BASE.date() + timedelta(days=1)


def test_time_slot_plan_applies_only_the_active_window_quota():
    now = BASE + timedelta(hours=1)
    active = _slot(0, 120, 0.0)
    later = _slot(180, 240, 0.0)
    controller = SimpleNamespace(
        charging_time_slots=[
            {"start_time": "00:00", "end_time": "02:00", "days": ["tue"]},
            {"start_time": "03:00", "end_time": "04:00", "days": ["tue"]},
        ],
        _active_time_slot_quota_kwh=None,
    )
    manager = PricingManager(SimpleNamespace(), controller)
    manager._build_chronological_plan = lambda **_kwargs: ChronologicalPlan(
        allocations=[
            SlotAllocation(active, 0.4, BASE + timedelta(hours=2), "deadline"),
            SlotAllocation(later, 0.8, None, "flexible"),
        ],
        total_required_kwh=1.2,
    )

    decision = manager._apply_time_slot_chronological_plan(
        {"should_charge": True, "planned_grid_charge_kwh": 1.2},
        now=now,
    )

    assert decision["should_charge"] is True
    assert decision["active_slot_energy_target_kwh"] == pytest.approx(0.4)
    assert controller._active_time_slot_quota_kwh == pytest.approx(0.4)


def test_time_slot_preview_is_built_before_the_first_window():
    now = BASE + timedelta(hours=1)
    controller = SimpleNamespace(
        charging_time_slots=[
            {"start_time": "03:00", "end_time": "04:00", "days": ["tue"]},
        ],
        _time_slot_chronological_preview_date=None,
        solar_forecast_remaining_sensor=None,
        solar_forecast_sensor=None,
        _last_decision_data=None,
    )
    manager = PricingManager(SimpleNamespace(), controller)

    async def decision():
        return {"should_charge": True}

    notifications = []

    async def notify(**kwargs):
        notifications.append(kwargs)

    manager._current_horizon_grid_charging_decision = decision
    manager._apply_time_slot_chronological_plan = lambda data, now: {
        **data,
        "should_charge": False,
        "deadline_shortfall_kwh": 0.4,
    }
    manager._send_predictive_charging_notification = notify

    asyncio.run(manager._ensure_time_slot_chronological_preview(now=now))

    assert controller._time_slot_chronological_preview_date == BASE.date()
    assert controller._last_decision_data["deadline_shortfall_kwh"] == 0.4
    assert len(notifications) == 1

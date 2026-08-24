"""Regression coverage for controller-backed daily-operation projections."""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from custom_components.omnibattery import ChargeDischargeController
from custom_components.omnibattery.pricing import PriceSlot
from custom_components.omnibattery.pricing.chronological import SlotAllocation
from custom_components.omnibattery.pricing.daily_timeline import (
    ACTION_GRID_CHARGE,
    ACTION_SOLAR_CHARGE,
    CONTEXT_CHARGE_DELAY,
    CONTEXT_SETPOINT,
    BatteryProjectionInput,
    ProjectionIntervalInput,
)


MADRID = ZoneInfo("Europe/Madrid")


def _controller_for_projection(
    now: datetime,
    intervals: list[ProjectionIntervalInput],
    allocations: list[SlotAllocation],
    batteries: list[BatteryProjectionInput],
    **overrides,
):
    plan = SimpleNamespace(intervals=intervals, allocations=allocations)
    values = {
        "_daily_operation_mode": lambda: "normal",
        "_daily_operation_float": ChargeDischargeController._daily_operation_float,
        "_daily_operation_battery_inputs": lambda: batteries,
        "_consumption_tracker": object(),
        "_pricing_mgr": SimpleNamespace(
            _build_chronological_plan=lambda **_kwargs: plan
        ),
        "_last_decision_data": {},
        "_last_chronological_diagnostics": {},
        "_dynamic_pricing_schedule": None,
        "predictive_charging_enabled": False,
        "charge_delay_enabled": False,
        "_daily_operation_delay_active": lambda: False,
        "_daily_operation_delay_unlock": lambda _now: None,
        "_delay_soc_setpoint_enabled": False,
        "_delay_setpoint_reached": False,
        "_charge_delay_unlocked": False,
        "_charge_delay_status": {},
        "max_price_threshold": None,
        "manual_mode_enabled": False,
        "enable_system_power_limits": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_projection_respects_combined_system_charge_limit():
    now = datetime(2026, 8, 24, 10, 0, tzinfo=MADRID)
    end = now + timedelta(minutes=15)
    slot = PriceSlot(now, end, 0.1)
    controller = _controller_for_projection(
        now,
        [ProjectionIntervalInput(now, end)],
        [SlotAllocation(slot, 2.0, None, "scheduled")],
        [
            BatteryProjectionInput("a", 0, 10, 0, 100, 4000, 4000),
            BatteryProjectionInput("b", 0, 10, 0, 100, 4000, 4000),
        ],
        enable_system_power_limits=True,
        system_max_charge_power=2000,
        system_max_discharge_power=2000,
    )

    result = ChargeDischargeController._daily_operation_build_projection(
        controller, now
    )

    assert result is not None
    item = result["intervals"][0]
    assert item["grid_to_battery_kwh"] == pytest.approx(0.5)
    assert item["charge_power_w"] == pytest.approx(2000.0)


def test_global_manual_mode_omits_automatic_future_projection():
    controller = SimpleNamespace(
        _daily_operation_mode=lambda: "dynamic_pricing",
        manual_mode_enabled=True,
    )

    result = ChargeDischargeController._daily_operation_build_projection(
        controller, datetime(2026, 8, 24, 10, 0, tzinfo=MADRID)
    )

    assert result == {
        "intervals": [],
        "mode": "dynamic_pricing",
        "stale": False,
        "sources": {"operation_plan": "manual_mode"},
    }


def test_setpoint_context_stops_after_the_interval_that_reaches_it():
    now = datetime(2026, 8, 24, 10, 0, tzinfo=MADRID)
    ends = [now + timedelta(minutes=15 * step) for step in range(1, 4)]
    intervals = [
        ProjectionIntervalInput(
            now + timedelta(minutes=15 * index), end,
        )
        for index, end in enumerate(ends)
    ]
    slot = PriceSlot(now, ends[0], 0.1)
    controller = _controller_for_projection(
        now,
        intervals,
        [SlotAllocation(slot, 1.0, None, "scheduled")],
        [BatteryProjectionInput("a", 0, 4, 0, 100, 4000, 4000)],
        charge_delay_enabled=True,
        _delay_soc_setpoint_enabled=True,
        _delay_soc_setpoint=20.0,
        _charge_delay_status={"state": "Charging to setpoint"},
    )

    result = ChargeDischargeController._daily_operation_build_projection(
        controller, now
    )

    assert result is not None
    masks = [item["context_mask"] for item in result["intervals"]]
    assert masks[0] & CONTEXT_SETPOINT
    assert not masks[1] & CONTEXT_SETPOINT
    assert not masks[2] & CONTEXT_SETPOINT


def test_weekly_full_charge_bypasses_delay_and_setpoint_projection_markers():
    now = datetime(2026, 8, 24, 10, 0, tzinfo=MADRID)
    end = now + timedelta(minutes=15)
    slot = PriceSlot(now, end, 0.1)
    controller = _controller_for_projection(
        now,
        [ProjectionIntervalInput(now, end, consumption_kwh=0.0, solar_kwh=0.0)],
        [SlotAllocation(slot, 1.0, None, "scheduled")],
        [BatteryProjectionInput("a", 0, 10, 0, 100, 4000, 4000)],
        charge_delay_enabled=True,
        _delay_soc_setpoint_enabled=True,
        _delay_soc_setpoint=50.0,
        _delay_setpoint_reached=False,
        _charge_delay_status={
            "state": "Delayed (10:45 est.)",
            "estimated_unlock_time": "10:45",
        },
        _balance_monitor_overrides_delay=lambda: True,
    )

    result = ChargeDischargeController._daily_operation_build_projection(
        controller, now
    )

    assert result is not None
    assert all(
        not item["context_mask"] & (CONTEXT_CHARGE_DELAY | CONTEXT_SETPOINT)
        and item.get("delay_until") is None
        for item in result["intervals"]
    )


def test_external_solar_does_not_hide_grid_charge_during_net_import():
    coordinator = SimpleNamespace(
        capabilities=SimpleNamespace(has_mppt_pv=False, has_solar_telemetry=False),
        data={"ac_power": -600},
    )
    controller = SimpleNamespace(
        coordinators=[coordinator],
        _consumption_tracker=SimpleNamespace(
            _read_total_solar_power_kw=lambda: 1.4
        ),
        grid_charging_active=False,
        previous_sensor=700.0,
        predictive_charging_enabled=False,
        charge_delay_enabled=False,
        previous_power=0.0,
        _daily_operation_mode=lambda: "normal",
        _daily_operation_float=ChargeDischargeController._daily_operation_float,
        _coordinator_delivered_power=(
            ChargeDischargeController._coordinator_delivered_power
        ),
        _is_battery_manual_owned=lambda _coordinator: False,
        _daily_operation_delay_active=lambda: False,
        _charge_delay_unlocked=False,
        _charge_delay_status={"state": "Disabled"},
    )

    decision = ChargeDischargeController._daily_operation_runtime_decision(
        controller, datetime(2026, 8, 24, 10, 0, tzinfo=MADRID)
    )

    assert decision["action_mask"] == ACTION_GRID_CHARGE


def test_direct_solar_and_ac_draw_are_both_reported_during_net_import():
    coordinator = SimpleNamespace(
        capabilities=SimpleNamespace(has_mppt_pv=True, has_solar_telemetry=True),
        data={"ac_power": -600, "mppt1_power": 800},
    )
    controller = SimpleNamespace(
        coordinators=[coordinator],
        _consumption_tracker=None,
        grid_charging_active=False,
        previous_sensor=700.0,
        predictive_charging_enabled=False,
        charge_delay_enabled=False,
        previous_power=0.0,
        _daily_operation_mode=lambda: "normal",
        _daily_operation_float=ChargeDischargeController._daily_operation_float,
        _coordinator_delivered_power=(
            ChargeDischargeController._coordinator_delivered_power
        ),
        _is_battery_manual_owned=lambda _coordinator: False,
        _daily_operation_delay_active=lambda: False,
        _charge_delay_unlocked=False,
        _charge_delay_status={"state": "Disabled"},
    )

    decision = ChargeDischargeController._daily_operation_runtime_decision(
        controller, datetime(2026, 8, 24, 10, 0, tzinfo=MADRID)
    )

    assert decision["action_mask"] == ACTION_SOLAR_CHARGE | ACTION_GRID_CHARGE


def test_previous_command_is_not_reported_as_measured_operation():
    controller = SimpleNamespace(
        coordinators=[],
        _consumption_tracker=None,
        grid_charging_active=False,
        previous_sensor=None,
        previous_power=750.0,
        predictive_charging_enabled=False,
        charge_delay_enabled=False,
        _daily_operation_mode=lambda: "normal",
        _daily_operation_float=ChargeDischargeController._daily_operation_float,
        _is_battery_manual_owned=lambda _coordinator: False,
        _daily_operation_delay_active=lambda: False,
        _charge_delay_unlocked=False,
        _charge_delay_status={"state": "Disabled"},
    )

    decision = ChargeDischargeController._daily_operation_runtime_decision(
        controller, datetime(2026, 8, 24, 10, 0, tzinfo=MADRID)
    )

    assert decision["action_mask"] == ACTION_GRID_CHARGE
    assert decision["charge_power_w"] == pytest.approx(750.0)
    assert decision["source"] == "runtime_command"

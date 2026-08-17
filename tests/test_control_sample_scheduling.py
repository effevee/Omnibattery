"""Regression tests for separating meter health from control samples.

The controller is exercised with small stubs so these tests cover the real
``_run_control_cycle`` and predictive-control branches without requiring a live
Home Assistant event loop or a battery connection.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from homeassistant.core import State

from custom_components.omnibattery import ChargeDischargeController


class _HashableNamespace(SimpleNamespace):
    __hash__ = object.__hash__
    __eq__ = object.__eq__


async def _async_noop(*_args, **_kwargs):
    return None


async def _async_false(*_args, **_kwargs):
    return False


def _state(value, reported_at, *, updated_at=None, attributes=None):
    return State(
        "sensor.grid_power",
        str(value),
        attributes=attributes or {},
        last_changed=updated_at or reported_at,
        last_reported=reported_at,
        last_updated=updated_at or reported_at,
    )


def _main_controller(state_holder, pd_calls):
    class _Coordinator(SimpleNamespace):
        __hash__ = object.__hash__

    coordinator = _Coordinator(
        _is_shutting_down=False,
        is_available=True,
        data={"battery_soc": 50},
        max_soc=90,
        min_soc=10,
        name="battery",
        commanded_charge_power=0,
        commanded_discharge_power=0,
    )

    async def _max_soc_measurement(*_args, **_kwargs):
        return False

    class _States:
        def get(self, _entity_id):
            return state_holder["state"]

    def _pd(error, sensor_elapsed_s, stale_safety_recalc):
        pd_calls.append((error, sensor_elapsed_s, stale_safety_recalc))
        return -500.0

    controller = SimpleNamespace(
        coordinators=[coordinator],
        _phase_power_limiter=SimpleNamespace(
            enabled=False,
            begin_cycle=lambda: None,
        ),
        _consumption_tracker=None,
        _balance_monitor=None,
        _pricing_mgr=SimpleNamespace(maybe_check_price_data_health=lambda: None),
        manual_mode_enabled=False,
        _weekly_charge_mgr=SimpleNamespace(handle_registers=_async_noop),
        _charge_delay_mgr=SimpleNamespace(handle_daily_reset_and_eval=lambda: None),
        _refresh_operation_blockers=lambda: None,
        _try_apply_manual_slot=_async_noop,
        _phase_safety_pending=False,
        _max_soc_mgr=SimpleNamespace(handle_measurement=_max_soc_measurement),
        predictive_charging_enabled=False,
        previous_power=-400.0,
        previous_sensor=None,
        previous_error=0.0,
        first_execution=False,
        last_output_sign=-1,
        last_error_sign=0,
        sign_changes=0,
        error_integral=0.0,
        derivative_filtered=0.0,
        _stale_cycles=0,
        _last_sensor_report_time=None,
        _last_sensor_cadence_time=None,
        _last_control_sample_value=None,
        _control_sample_is_new=True,
        _slow_sensor_issue_created=False,
        _slow_sensor_intervals=0,
        _fast_sensor_intervals=0,
        _max_sensor_stale_s=65.0,
        consumption_sensor="sensor.grid_power",
        config_entry=SimpleNamespace(entry_id="control-sample", data={}),
        hass=SimpleNamespace(states=_States()),
        _apply_meter_transform=lambda state: float(state.state),
        _check_solar_forecast_health=lambda: None,
        _is_capacity_protection_soc_limited=lambda: False,
        _filter_grid_sample=lambda raw, _elapsed: raw,
        compute_active_target=lambda: 0.0,
        _resolve_home_consumption_sensor=lambda: None,
        _external_loads=SimpleNamespace(
            calculate_adjustment=lambda: 0.0,
            check_ev_charger_state=lambda: (False, False),
        ),
        _hourly_balance_mgr=None,
        _apply_capacity_protection=lambda sensor, target: (target, sensor),
        _capacity_protection_force_idle=False,
        deadband=40.0,
        _is_charge_blocked=lambda *_args, **_kwargs: False,
        _is_discharge_blocked=lambda *_args, **_kwargs: False,
        is_charge_blocked=lambda *_args, **_kwargs: False,
        is_discharge_blocked=lambda *_args, **_kwargs: False,
        _stop_blocked_active_batteries=_async_false,
        _stop_all_batteries_for_block=_async_noop,
        _refresh_effective_system_capacities=lambda: None,
        no_pd_mode_enabled=False,
        _check_feedforward_step=lambda _error: False,
        _compute_pd_new_power=_pd,
        _apply_zero_cross_hold=lambda power, _error, stale_recalc=False: power,
        _apply_min_power=lambda power, _error: power,
        _apply_relay_dwell=lambda power, _error: power,
        _is_operation_allowed=lambda _is_charging: True,
        _price_based_discharge_blocked=False,
        _solar_surplus_discharge_blocked=False,
        _get_available_batteries=lambda is_charging, include_operation_blocks=True: [coordinator],
        _effective_system_capacity=lambda _batteries, is_charging: 2500.0,
        max_contracted_power=0,
        grid_charging_active=False,
        _daily_grid_at_min_soc_kwh=0.0,
        _grid_at_min_soc_last_ts=None,
        _grid_at_min_soc_sensor=None,
        _power_distribution=SimpleNamespace(
            _select_batteries_for_operation=lambda _power, batteries, is_charging=None: batteries,
            _distribute_power_by_limits=lambda power, batteries, is_charging=None: {
                battery: power / len(batteries) for battery in batteries
            },
        ),
        _log_power_command_plan=lambda **_kwargs: None,
        _set_battery_power=_async_noop,
        _update_pd_quality_metrics=lambda *_args, **_kwargs: None,
        _pd_demand_blocked=lambda _error, _commanded_power: False,
        _set_pd_limited=lambda _value: None,
        _set_pd_blocked=lambda _value: None,
        _pd_limited=False,
        _pd_blocked=False,
        _active_discharge_batteries=[],
        _active_charge_batteries=[],
    )
    controller._run_control_cycle = ChargeDischargeController._run_control_cycle.__get__(
        controller, ChargeDischargeController
    )
    controller._track_sensor_report = ChargeDischargeController._track_sensor_report.__get__(
        controller, ChargeDischargeController
    )
    controller._sensor_age_seconds = ChargeDischargeController._sensor_age_seconds.__get__(
        controller, ChargeDischargeController
    )
    controller._sensor_is_within_stale_tolerance = ChargeDischargeController._sensor_is_within_stale_tolerance.__get__(
        controller, ChargeDischargeController
    )
    return controller


def test_repeated_publication_does_not_reapply_pd_but_real_change_runs_once():
    first_report = datetime(2026, 1, 1, tzinfo=timezone.utc)
    state_holder = {"state": _state(100, first_report)}
    pd_calls = []
    controller = _main_controller(state_holder, pd_calls)

    asyncio.run(controller._run_control_cycle(now=first_report + timedelta(seconds=1)))
    assert len(pd_calls) == 1
    previous_power = controller.previous_power

    # Same transformed value, newer last_reported, unchanged last_updated.
    state_holder["state"] = _state(
        100,
        first_report + timedelta(seconds=4),
        updated_at=first_report,
    )
    asyncio.run(controller._run_control_cycle(now=first_report + timedelta(seconds=4)))
    assert len(pd_calls) == 1
    assert controller.previous_power == previous_power

    # A real value change is consumed exactly once by the incremental PD path.
    state_holder["state"] = _state(
        200,
        first_report + timedelta(seconds=8),
        updated_at=first_report + timedelta(seconds=8),
    )
    asyncio.run(controller._run_control_cycle(now=first_report + timedelta(seconds=8)))
    assert len(pd_calls) == 2

    # The watchdog sees the already-consumed state but must not apply P/D again.
    previous_power = controller.previous_power
    asyncio.run(controller._run_control_cycle(now=first_report + timedelta(seconds=10)))
    assert len(pd_calls) == 2
    assert controller.previous_power == previous_power


def test_manual_grid_charge_does_not_induce_automatic_discharge_when_idle():
    first_report = datetime(2026, 1, 1, tzinfo=timezone.utc)
    state_holder = {"state": _state(700, first_report)}
    pd_calls = []
    controller = _main_controller(state_holder, pd_calls)
    controller.previous_power = 0.0
    controller.last_output_sign = 0
    controller.ki = 0.0
    controller._power_distribution._rebalance_expired_load_sharing_hold = _async_false

    manual = _HashableNamespace(
        _is_shutting_down=False,
        battery_manual_mode_enabled=True,
        data={"battery_power": 700},
        name="manual battery",
    )
    controller.coordinators.append(manual)

    asyncio.run(controller._run_control_cycle(now=first_report + timedelta(seconds=1)))

    # With no automatic charge active, the intentional manual import is not a
    # reason to discharge an automatic battery.
    assert pd_calls == []
    assert controller.previous_power == 0.0


def test_manual_grid_charge_reduces_automatic_charge_without_discharge():
    first_report = datetime(2026, 1, 1, tzinfo=timezone.utc)
    state_holder = {"state": _state(1000, first_report)}
    pd_calls = []
    controller = _main_controller(state_holder, pd_calls)
    controller.previous_power = 2000.0
    controller.last_output_sign = 1
    controller.ki = 0.0
    controller._power_distribution._rebalance_expired_load_sharing_hold = _async_false
    controller.coordinators[0].data = {
        "battery_soc": 50,
        "battery_power": 2000,
    }

    def _reduce_automatic_charge(error, sensor_elapsed_s, stale_safety_recalc):
        pd_calls.append((error, sensor_elapsed_s, stale_safety_recalc))
        return 1000.0

    controller._compute_pd_new_power = _reduce_automatic_charge
    manual = _HashableNamespace(
        _is_shutting_down=False,
        battery_manual_mode_enabled=True,
        data={"battery_power": 1000},
        name="manual battery",
    )
    controller.coordinators.append(manual)

    asyncio.run(controller._run_control_cycle(now=first_report + timedelta(seconds=1)))

    # The 1 kW import is used to reduce the automatic 2 kW charge to 1 kW;
    # it must not be turned into an automatic discharge command.
    assert pd_calls and pd_calls[0][0] == 1000
    assert controller.previous_power == 1000.0


def test_busy_loop_cadence_does_not_consume_pending_real_change():
    first_report = datetime(2026, 1, 1, tzinfo=timezone.utc)
    state_holder = {"state": _state(100, first_report)}
    pd_calls = []
    controller = _main_controller(state_holder, pd_calls)

    asyncio.run(controller._run_control_cycle(now=first_report + timedelta(seconds=1)))
    assert len(pd_calls) == 1

    # Publication callbacks continue to feed cadence while the previous cycle is
    # busy. They intentionally do not update the last control fingerprint.
    for seconds in (4, 8):
        ChargeDischargeController._observe_sensor_cadence(
            controller, first_report + timedelta(seconds=seconds)
        )

    state_holder["state"] = _state(
        250,
        first_report + timedelta(seconds=8),
        updated_at=first_report + timedelta(seconds=8),
    )
    asyncio.run(controller._run_control_cycle(now=first_report + timedelta(seconds=9)))

    assert len(pd_calls) == 2
    assert controller._last_sensor_cadence_time == first_report + timedelta(seconds=8)


def test_silent_sensor_uses_stale_safety_without_reapplying_pd():
    first_report = datetime(2026, 1, 1, tzinfo=timezone.utc)
    state_holder = {"state": _state(100, first_report)}
    pd_calls = []
    controller = _main_controller(state_holder, pd_calls)

    asyncio.run(controller._run_control_cycle(now=first_report + timedelta(seconds=1)))
    previous_power = controller.previous_power

    asyncio.run(controller._run_control_cycle(now=first_report + timedelta(seconds=70)))

    assert len(pd_calls) == 1
    assert controller.previous_power == previous_power


def _predictive_controller(state_holder, writes):
    class _Coordinator(SimpleNamespace):
        __hash__ = object.__hash__

    coordinator = _Coordinator(name="battery", data={"battery_soc": 50}, max_soc=90)

    class _States:
        def get(self, _entity_id):
            return state_holder["state"]

    controller = SimpleNamespace(
        is_charge_blocked=lambda: False,
        get_charge_blockers=dict,
        hass=SimpleNamespace(states=_States()),
        consumption_sensor="sensor.grid_power",
        _apply_meter_transform=lambda state: float(state.state),
        _last_sensor_report_time=None,
        _last_sensor_cadence_time=None,
        _last_control_sample_value=None,
        _control_sample_is_new=True,
        _slow_sensor_issue_created=False,
        _slow_sensor_intervals=0,
        _fast_sensor_intervals=0,
        config_entry=SimpleNamespace(entry_id="predictive-sample", data={}),
        _max_sensor_stale_s=65.0,
        _grid_charging_initialized=True,
        grid_charging_active=True,
        first_execution=False,
        _predictive_charge_target_soc={coordinator: 80.0},
        _get_available_batteries=lambda is_charging: [coordinator],
        _filter_grid_sample=lambda raw, _elapsed: raw,
        _effective_system_capacity=lambda _batteries, is_charging: 2500.0,
        max_contracted_power=1000.0,
        dt=2.0,
        kp=0.3,
        kd=0.0,
        derivative_tau=3.0,
        derivative_filtered=0.0,
        previous_error=0.0,
        previous_power=-500.0,
        max_power_change_per_cycle=800.0,
        _power_distribution=SimpleNamespace(
            _select_batteries_for_operation=lambda _power, batteries, is_charging=None: batteries,
            _distribute_power_by_limits=lambda power, batteries, is_charging=None: {
                battery: power / len(batteries) for battery in batteries
            },
        ),
        coordinators=[coordinator],
        _phase_power_limiter=SimpleNamespace(enabled=False),
        _set_battery_power=lambda coordinator, charge, discharge: _record_write(
            writes, coordinator, charge, discharge
        ),
    )
    controller._track_sensor_report = ChargeDischargeController._track_sensor_report.__get__(
        controller, ChargeDischargeController
    )
    controller._sensor_is_within_stale_tolerance = ChargeDischargeController._sensor_is_within_stale_tolerance.__get__(
        controller, ChargeDischargeController
    )
    controller._handle_predictive_grid_charging = ChargeDischargeController._handle_predictive_grid_charging.__get__(
        controller, ChargeDischargeController
    )
    return controller


async def _record_write(writes, coordinator, charge, discharge):
    writes.append((coordinator, charge, discharge))


def test_predictive_pd_does_not_integrate_identical_publications():
    first_report = datetime.now(timezone.utc)
    state_holder = {"state": _state(100, first_report)}
    writes = []
    controller = _predictive_controller(state_holder, writes)

    asyncio.run(controller._handle_predictive_grid_charging())
    first_power = controller.previous_power
    first_write_count = len(writes)

    state_holder["state"] = _state(
        100,
        first_report + timedelta(seconds=4),
        updated_at=first_report,
    )
    asyncio.run(controller._handle_predictive_grid_charging())

    assert controller.previous_power == first_power
    assert len(writes) == first_write_count

    state_holder["state"] = _state(
        200,
        first_report + timedelta(seconds=8),
        updated_at=first_report + timedelta(seconds=8),
    )
    asyncio.run(controller._handle_predictive_grid_charging())
    assert controller.previous_power != first_power

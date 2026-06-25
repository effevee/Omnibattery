"""Characterization tests for the excluded-device load logic.

These pin the *current* behavior of the two excluded-device methods on
ExternalLoads so the extraction can be proven to change nothing.

No hardware and no real Home Assistant: ExternalLoads is built with a stub
controller (SimpleNamespace) and a fake hass whose ``states.get`` returns
lightweight state stand-ins. Both methods only read ``config_entry.data`` and
``hass.states``, plus ``previous_power`` for the charge/discharge branch.
"""
from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest

from homeassistant.util import dt as dt_util

from custom_components.omnibattery.infra.external_loads import ExternalLoads


# ----------------------------------------------------------------------
# Test doubles
# ----------------------------------------------------------------------

class _FakeStates:
    def __init__(self, mapping):
        self._mapping = mapping

    def get(self, entity_id):
        return self._mapping.get(entity_id)


def _state(value, unit="W"):
    """A minimal stand-in for a Home Assistant State object."""
    return SimpleNamespace(state=str(value), attributes={"unit_of_measurement": unit})


def _controller(excluded_devices, states=None, previous_power=0.0,
                solar_production_sensor=None, home_consumption_sensor=None):
    controller_stub = SimpleNamespace(
        previous_power=previous_power,
        _excluded_included_adjustment=None,
        _solar_surplus_discharge_blocked=False,
        solar_production_sensor=solar_production_sensor,
        home_consumption_sensor=home_consumption_sensor,
        _ev_charging_states={},
        _ev_pause_until={},
    )
    config_entry = SimpleNamespace(data={"excluded_devices": excluded_devices})
    hass = SimpleNamespace(states=_FakeStates(states or {}))
    return ExternalLoads(hass, config_entry, controller_stub)


def _device(**overrides):
    """A telemetry excluded device with sensible defaults; override per test."""
    base = {
        "enabled": True,
        "ev_charger_no_telemetry": False,
        "power_sensor": "sensor.dev",
        "included_in_consumption": True,
        "allow_solar_surplus": False,
    }
    base.update(overrides)
    return base


# ----------------------------------------------------------------------
# consumption_delta_kw  (returns kW; W->kW converted)
# ----------------------------------------------------------------------

def test_delta_kw_no_devices_is_zero():
    assert _controller([]).consumption_delta_kw() == 0.0


def test_delta_kw_included_device_is_subtracted():
    loads = _controller([_device(included_in_consumption=True)],
                       {"sensor.dev": _state(500)})
    assert loads.consumption_delta_kw() == pytest.approx(-0.5)


def test_delta_kw_not_included_device_is_added():
    loads = _controller([_device(included_in_consumption=False)],
                       {"sensor.dev": _state(500)})
    assert loads.consumption_delta_kw() == pytest.approx(0.5)


def test_delta_kw_kilowatt_unit_not_reconverted():
    loads = _controller([_device(included_in_consumption=True)],
                       {"sensor.dev": _state(0.5, unit="kW")})
    assert loads.consumption_delta_kw() == pytest.approx(-0.5)


def test_delta_kw_two_devices_net():
    devices = [
        _device(power_sensor="sensor.a", included_in_consumption=True),
        _device(power_sensor="sensor.b", included_in_consumption=False),
    ]
    loads = _controller(devices,
                       {"sensor.a": _state(500), "sensor.b": _state(200)})
    # -0.5 (subtracted) + 0.2 (added)
    assert loads.consumption_delta_kw() == pytest.approx(-0.3)


@pytest.mark.parametrize("device, states", [
    (_device(enabled=False), {"sensor.dev": _state(500)}),
    (_device(ev_charger_no_telemetry=True), {"sensor.dev": _state(500)}),
    (_device(power_sensor=None), {}),
    (_device(), {"sensor.dev": _state("unavailable")}),
    (_device(), {}),  # sensor missing entirely
])
def test_delta_kw_skips_unusable_devices(device, states):
    assert _controller([device], states).consumption_delta_kw() == 0.0


# ----------------------------------------------------------------------
# calculate_adjustment  (returns W; kW unit handled)
# Positive = reduce battery discharge, negative = increase it.
# ----------------------------------------------------------------------

def test_adjustment_no_devices_is_zero_and_resets_included():
    loads = _controller([])
    assert loads.calculate_adjustment() == 0.0
    assert loads._controller._excluded_included_adjustment == 0.0


def test_adjustment_included_no_surplus_subtracts():
    loads = _controller([_device(included_in_consumption=True, allow_solar_surplus=False)],
                       {"sensor.dev": _state(500)})
    assert loads.calculate_adjustment() == pytest.approx(500.0)
    assert loads._controller._excluded_included_adjustment == pytest.approx(500.0)


def test_adjustment_not_included_adds_discharge():
    loads = _controller([_device(included_in_consumption=False)],
                       {"sensor.dev": _state(500)})
    assert loads.calculate_adjustment() == pytest.approx(-500.0)
    assert loads._controller._excluded_included_adjustment == pytest.approx(0.0)


def test_adjustment_solar_surplus_no_solar_sensor_always_zero_no_discharge_blocked_when_idle():
    # No solar sensor configured + device idle → no adjustment, no discharge block
    loads = _controller(
        [_device(included_in_consumption=True, allow_solar_surplus=True)],
        {"sensor.dev": _state(0)},
    )
    loads.calculate_adjustment()
    assert loads._controller._solar_surplus_discharge_blocked is False


def test_adjustment_solar_surplus_no_solar_sensor_sets_discharge_blocked_when_active():
    # No solar sensor configured + device active → no adjustment, discharge blocked
    loads = _controller(
        [_device(included_in_consumption=True, allow_solar_surplus=True)],
        {"sensor.dev": _state(500)},
    )
    assert loads.calculate_adjustment() == pytest.approx(0.0)
    assert loads._controller._solar_surplus_discharge_blocked is True


def test_adjustment_solar_surplus_no_solar_sensor_kw_sensor_converted():
    # No solar sensor; device sensor in kW → 1500 W active → discharge blocked
    loads = _controller(
        [_device(included_in_consumption=True, allow_solar_surplus=True)],
        {"sensor.dev": _state(1.5, unit="kW")},
    )
    assert loads.calculate_adjustment() == pytest.approx(0.0)
    assert loads._controller._solar_surplus_discharge_blocked is True


# PV-surplus priority (#421/#415): surplus = max(0, solar − home_base_load),
# home_base_load = home_consumption − excluded devices. For a single device,
# base_load = home − device; the device is offset by the surplus first, the
# battery only excludes the grid portion it must import.

def _surplus_loads(device_w, solar_w, home_w, **device_overrides):
    return _controller(
        [_device(included_in_consumption=True, allow_solar_surplus=True, **device_overrides)],
        {"sensor.dev": _state(device_w), "sensor.solar": _state(solar_w),
         "sensor.home": _state(home_w)},
        solar_production_sensor="sensor.solar",
        home_consumption_sensor="sensor.home",
    )


def test_adjustment_pv_priority_partial_surplus_offsets_device():
    # Discussion #421 numbers: EV=3419 W, solar=2000 W, home=4438 W (base 1019).
    # surplus = 2000 − 1019 = 981 → exclude 3419 − 981 = 2438 W; battery idle.
    loads = _surplus_loads(3419, 2000, 4438)
    assert loads.calculate_adjustment() == pytest.approx(2438.0)
    assert loads._controller._excluded_included_adjustment == pytest.approx(2438.0)
    assert loads._controller._solar_surplus_discharge_blocked is False


def test_adjustment_pv_priority_surplus_covers_device_battery_charges_rest():
    # device=1000, solar=3000, home=2000 (base 1000) → surplus=2000 ≥ device.
    # Device fully offset → adjustment 0; PD sees export → battery charges leftover.
    loads = _surplus_loads(1000, 3000, 2000)
    assert loads.calculate_adjustment() == pytest.approx(0.0)
    assert loads._controller._solar_surplus_discharge_blocked is False


def test_adjustment_pv_priority_no_surplus_full_exclusion():
    # solar ≤ base load → no surplus for the device → full exclusion.
    # device=1500, solar=800, home=2300 (base 800) → surplus=0 → exclude 1500.
    loads = _surplus_loads(1500, 800, 2300)
    assert loads.calculate_adjustment() == pytest.approx(1500.0)


def test_adjustment_pv_priority_home_consumption_unavailable_full_exclusion():
    # Home Consumption sensor down → conservative full exclusion (battery never
    # powers the device), regardless of solar.
    loads = _controller(
        [_device(included_in_consumption=True, allow_solar_surplus=True)],
        {"sensor.dev": _state(1000), "sensor.solar": _state(3000),
         "sensor.home": _state("unavailable")},
        solar_production_sensor="sensor.solar",
        home_consumption_sensor="sensor.home",
    )
    assert loads.calculate_adjustment() == pytest.approx(1000.0)
    assert loads._controller._solar_surplus_discharge_blocked is False


def test_adjustment_pv_priority_kw_units():
    # All sensors in kW: device=4.5, solar=2.0, home=5.0 (base 0.5 kW).
    # surplus = 2.0 − 0.5 = 1.5 kW → exclude 4.5 − 1.5 = 3.0 kW = 3000 W.
    loads = _controller(
        [_device(included_in_consumption=True, allow_solar_surplus=True)],
        {"sensor.dev": _state(4.5, unit="kW"), "sensor.solar": _state(2.0, unit="kW"),
         "sensor.home": _state(5.0, unit="kW")},
        solar_production_sensor="sensor.solar",
        home_consumption_sensor="sensor.home",
    )
    assert loads.calculate_adjustment() == pytest.approx(3000.0)


def test_adjustment_pv_priority_shared_budget_across_two_devices():
    # Two surplus devices share one budget: base = home − (devA+devB).
    # home=4000, devA=1500, devB=1000 → base=1500; solar=2500 → surplus=1000.
    # Loop order: A takes min(1500,1000)=1000 → exclude 500; B sees 0 left → exclude 1000.
    devices = [
        _device(power_sensor="sensor.a", allow_solar_surplus=True),
        _device(power_sensor="sensor.b", allow_solar_surplus=True),
    ]
    loads = _controller(
        devices,
        {"sensor.a": _state(1500), "sensor.b": _state(1000),
         "sensor.solar": _state(2500), "sensor.home": _state(4000)},
        solar_production_sensor="sensor.solar",
        home_consumption_sensor="sensor.home",
    )
    assert loads.calculate_adjustment() == pytest.approx(1500.0)  # 500 + 1000


def test_adjustment_included_no_surplus_kw_sensor_converted_correctly():
    # 1.5 kW sensor → adjustment should be 1500 W
    loads = _controller(
        [_device(included_in_consumption=True, allow_solar_surplus=False)],
        {"sensor.dev": _state(1.5, unit="kW")},
    )
    assert loads.calculate_adjustment() == pytest.approx(1500.0)


@pytest.mark.parametrize("device", [
    _device(enabled=False),
    _device(ev_charger_no_telemetry=True),
    _device(power_sensor=None),
])
def test_adjustment_skips_unusable_devices(device):
    loads = _controller([device], {"sensor.dev": _state(500)})
    assert loads.calculate_adjustment() == 0.0


# ----------------------------------------------------------------------
# exclusion_pct slider: scales the excluded portion (100 = full, default).
# ----------------------------------------------------------------------

def test_exclusion_pct_partial_scales_adjustment():
    # 60% excluded → battery covers 40% → only 60% of 500 W is excluded.
    loads = _controller(
        [_device(included_in_consumption=True, exclusion_pct=60)],
        {"sensor.dev": _state(500)},
    )
    assert loads.calculate_adjustment() == pytest.approx(300.0)
    assert loads._controller._excluded_included_adjustment == pytest.approx(300.0)


def test_exclusion_pct_partial_scales_delta_kw():
    loads = _controller(
        [_device(included_in_consumption=True, exclusion_pct=60)],
        {"sensor.dev": _state(500)},
    )
    assert loads.consumption_delta_kw() == pytest.approx(-0.3)


def test_exclusion_pct_zero_means_no_exclusion():
    loads = _controller(
        [_device(included_in_consumption=True, exclusion_pct=0)],
        {"sensor.dev": _state(500)},
    )
    assert loads.calculate_adjustment() == pytest.approx(0.0)


def test_exclusion_pct_scales_solar_surplus_grid_portion():
    # device=4500, solar=2000, home=5000 (base 500) → surplus 1500, grid portion
    # 4500 − 1500 = 3000 W, scaled by 50% → 1500 W.
    loads = _surplus_loads(4500, 2000, 5000, exclusion_pct=50)
    assert loads.calculate_adjustment() == pytest.approx(1500.0)


def test_exclusion_pct_default_is_full_exclusion():
    # No exclusion_pct key → factor 1.0 (unchanged from original behaviour).
    loads = _controller(
        [_device(included_in_consumption=True)],
        {"sensor.dev": _state(500)},
    )
    assert loads.calculate_adjustment() == pytest.approx(500.0)


# ----------------------------------------------------------------------
# check_ev_charger_state  (no-telemetry EV: 5-min pause then discharge-block)
# Time-dependent: a frozen clock drives the pause window. Returns
# (pause_active, ev_charging_active).
# ----------------------------------------------------------------------

def _ev_device(**overrides):
    """A no-telemetry EV charger: a state sensor, not a numeric power sensor."""
    base = {
        "enabled": True,
        "ev_charger_no_telemetry": True,
        "power_sensor": "sensor.ev",
    }
    base.update(overrides)
    return base


def test_ev_start_charging_starts_pause():
    loads = _controller([_ev_device()], {"sensor.ev": _state("charging")})
    # First detection: pause begins, not yet discharge-blocking.
    assert loads.check_ev_charger_state() == (True, False)


def test_ev_pause_stays_active_within_5_min():
    loads = _controller([_ev_device()], {"sensor.ev": _state("charging")})
    loads.check_ev_charger_state()                # pause starts (now + 5 min)
    # An immediate re-check is well within the 5-minute window.
    assert loads.check_ev_charger_state() == (True, False)


def test_ev_charging_active_after_pause_expires():
    loads = _controller([_ev_device()], {"sensor.ev": _state("charging")})
    loads.check_ev_charger_state()                # pause starts
    # Simulate the 5-min pause having elapsed by moving the stored deadline into
    # the past. (freezegun can't patch HA's dt_util.utcnow with the HA pytest
    # plugin disabled, so we manipulate the deadline directly instead.)
    loads._controller._ev_pause_until["sensor.ev"] = dt_util.utcnow() - timedelta(minutes=1)
    assert loads.check_ev_charger_state() == (False, True)


def test_ev_stop_charging_cancels_pause():
    states = {"sensor.ev": _state("charging")}
    loads = _controller([_ev_device()], states)
    loads.check_ev_charger_state()                # pause starts
    states["sensor.ev"] = _state("idle")          # EV stops mid-pause
    assert loads.check_ev_charger_state() == (False, False)
    assert loads._controller._ev_pause_until == {}


def test_ev_idle_does_nothing():
    loads = _controller([_ev_device()], {"sensor.ev": _state("idle")})
    assert loads.check_ev_charger_state() == (False, False)


def test_ev_spanish_cargando_detected():
    loads = _controller([_ev_device()], {"sensor.ev": _state("Cargando")})
    assert loads.check_ev_charger_state() == (True, False)


@pytest.mark.parametrize("device", [
    _ev_device(enabled=False),
    _ev_device(power_sensor=None),
    # A numeric excluded device (not a no-telemetry EV) must be ignored here.
    _device(ev_charger_no_telemetry=False, power_sensor="sensor.ev"),
])
def test_ev_skips_non_applicable_devices(device):
    loads = _controller([device], {"sensor.ev": _state("charging")})
    assert loads.check_ev_charger_state() == (False, False)

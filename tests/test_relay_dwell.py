"""Tests for ``_apply_relay_dwell`` — the relay anti-chatter shut-off dwell.

When the controller decides to send the battery back to idle, the dwell keeps it
engaged at minimum power for ``_relay_cooldown_s`` seconds first, so the relay
doesn't click off the instant demand falls and on again when it returns.

The dwell is timed from the moment idle was FIRST requested, not from when the
battery engaged. The original implementation measured from engagement, so after
a long active run ``held_s`` was already far past the cooldown and the hold never
fired — the regression these tests pin down.

The method is exercised unbound with a light stub, so no full controller is built.
``previous_power`` convention is + charge / - discharge.
"""
from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

from homeassistant.util import dt as dt_util

from custom_components.omnibattery import (
    ChargeDischargeController,
    RELAY_COOLDOWN_HOLD_POWER,
)


def _ctrl(
    previous_power,
    *,
    cooldown_s=30,
    deadband=40,
    min_charge=50,
    min_discharge=50,
    shutoff_since=None,
):
    return SimpleNamespace(
        _relay_cooldown_s=cooldown_s,
        previous_power=previous_power,
        deadband=deadband,
        min_charge_power=min_charge,
        min_discharge_power=min_discharge,
        _relay_shutoff_since=shutoff_since,
    )


def _dwell(ctrl, new_power, error):
    return ChargeDischargeController._apply_relay_dwell(ctrl, new_power, error)


def test_shutoff_starts_hold_at_min_discharge():
    # Discharging 300W, demand vanishes -> controller asks for idle. Instead of
    # dropping to 0 the dwell holds at min discharge and arms the timer.
    ctrl = _ctrl(previous_power=-300)
    out = _dwell(ctrl, new_power=0, error=300)
    assert out == -50
    assert ctrl._relay_shutoff_since is not None


def test_shutoff_starts_hold_at_min_charge():
    ctrl = _ctrl(previous_power=200)
    out = _dwell(ctrl, new_power=0, error=-200)
    assert out == 50
    assert ctrl._relay_shutoff_since is not None


def test_min_power_zero_falls_back_to_hold_power():
    ctrl = _ctrl(previous_power=-300, min_charge=0, min_discharge=0)
    out = _dwell(ctrl, new_power=0, error=300)
    assert out == -RELAY_COOLDOWN_HOLD_POWER


def test_hold_continues_within_dwell():
    # 10s into a 30s dwell, still being asked for idle -> keep holding.
    since = dt_util.utcnow() - timedelta(seconds=10)
    ctrl = _ctrl(previous_power=-50, shutoff_since=since)
    out = _dwell(ctrl, new_power=0, error=50)
    assert out == -50
    assert ctrl._relay_shutoff_since == since


def test_hold_releases_after_dwell():
    # Past the 30s dwell -> allow idle and re-arm for next time.
    since = dt_util.utcnow() - timedelta(seconds=31)
    ctrl = _ctrl(previous_power=-50, shutoff_since=since)
    out = _dwell(ctrl, new_power=0, error=50)
    assert out == 0
    assert ctrl._relay_shutoff_since is None


def test_long_active_run_then_shutoff_still_holds():
    # Regression: the battery has been engaged for minutes (shutoff_since is None
    # because it never tried to idle). The FIRST idle request must start a fresh
    # dwell and hold -- the old engage-based timer would have skipped it.
    ctrl = _ctrl(previous_power=-300, shutoff_since=None)
    out = _dwell(ctrl, new_power=0, error=300)
    assert out == -50
    assert ctrl._relay_shutoff_since is not None


def test_large_imbalance_bypasses_hold():
    # A sudden real load (error far beyond what the battery was handling) must not
    # be left on the grid: skip the hold, go to the commanded idle, drop the timer.
    ctrl = _ctrl(previous_power=-100, shutoff_since=dt_util.utcnow())
    out = _dwell(ctrl, new_power=0, error=500)
    assert out == 0
    assert ctrl._relay_shutoff_since is None


def test_active_power_rearms_timer():
    # Controller commands real power (not idle) -> dwell does nothing, timer clears.
    ctrl = _ctrl(previous_power=-300, shutoff_since=dt_util.utcnow())
    out = _dwell(ctrl, new_power=-400, error=400)
    assert out == -400
    assert ctrl._relay_shutoff_since is None


def test_cooldown_disabled_no_hold():
    ctrl = _ctrl(previous_power=-300, cooldown_s=0)
    out = _dwell(ctrl, new_power=0, error=300)
    assert out == 0
    assert ctrl._relay_shutoff_since is None

"""Restore-state contract for the Zendure synthetic energy sensors.

Regression guard: the lifetime ``total_charging_energy`` counter must never be
zeroed just because the last persisted state was non-numeric. Zendure's single
connection drops often, so a restart's last recorded state is frequently
``unavailable``/``unknown``. The accumulator is now persisted as typed extra
data (``_SyntheticEnergyData``) taken from the entity object, not the state
string, and a malformed payload falls back rather than resetting to 0.
"""
from __future__ import annotations

from custom_components.marstek_venus_energy_manager.sensors.calculated_sensors import (
    _SyntheticEnergyData,
)


def test_roundtrip_preserves_value_and_reset_date():
    data = _SyntheticEnergyData(1.91, "2026-06-20")
    assert _SyntheticEnergyData.from_dict(data.as_dict()) == data


def test_total_counter_has_no_reset_date():
    # Lifetime (non-daily) entities carry reset_date=None and survive a roundtrip.
    assert _SyntheticEnergyData.from_dict({"kwh": 1.91, "reset_date": None}) == (
        _SyntheticEnergyData(1.91, None)
    )


def test_string_kwh_is_coerced():
    # Stored JSON may round-trip the number as a string; it must still parse.
    assert _SyntheticEnergyData.from_dict({"kwh": "1.91", "reset_date": None}).kwh == 1.91


def test_non_numeric_payload_returns_none_not_zero():
    # The crux: a non-numeric value yields None so the caller keeps the running
    # total instead of wiping it to 0.0 (the original reset bug).
    assert _SyntheticEnergyData.from_dict({"kwh": "unavailable"}) is None


def test_missing_kwh_returns_none():
    assert _SyntheticEnergyData.from_dict({}) is None
    assert _SyntheticEnergyData.from_dict({"reset_date": "2026-06-20"}) is None

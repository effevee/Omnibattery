"""Regression coverage for the dual solar-forecast migration."""
from types import SimpleNamespace

import pytest

from custom_components.omnibattery.pricing.engine import PricingManager
from custom_components.omnibattery.solar_forecast import (
    normalize_solar_forecast_config,
    read_solar_forecast_kwh,
)


def _state(value, unit="kWh"):
    return SimpleNamespace(state=str(value), attributes={"unit_of_measurement": unit})


def test_remaining_forecast_wins_and_is_normalized_from_wh():
    states = {
        "sensor.today": _state(20.52),
        "sensor.remaining": _state(1810, "Wh"),
    }
    controller = SimpleNamespace(
        solar_forecast_sensor="sensor.today",
        solar_forecast_remaining_sensor="sensor.remaining",
    )
    hass = SimpleNamespace(states=SimpleNamespace(get=states.get))

    forecast = read_solar_forecast_kwh(hass, controller)

    assert forecast is not None
    assert forecast.kwh == pytest.approx(1.81)
    assert forecast.source == "remaining"
    assert controller.solar_forecast_source == "remaining"


def test_remaining_forecast_is_not_reduced_by_production():
    """20.52 today - 12.34 produced is legacy-only; remaining stays 1.81."""
    states = {
        "sensor.today": _state(20.52),
        "sensor.remaining": _state(1.81),
    }
    controller = SimpleNamespace(
        solar_forecast_sensor="sensor.today",
        solar_forecast_remaining_sensor="sensor.remaining",
        _daily_solar_energy_kwh=12.34,
        _solar_t_start=8.0,
        _consumption_tracker=SimpleNamespace(),
    )
    hass = SimpleNamespace(states=SimpleNamespace(get=states.get))

    assert PricingManager(hass, controller)._remaining_solar_today_kwh(14.0) == pytest.approx(1.81)


def test_invalid_remaining_sensor_falls_back_to_legacy_today():
    states = {
        "sensor.today": _state(20520, "Wh"),
        "sensor.remaining": _state("nan"),
    }
    controller = SimpleNamespace(
        solar_forecast_sensor="sensor.today",
        solar_forecast_remaining_sensor="sensor.remaining",
    )
    hass = SimpleNamespace(states=SimpleNamespace(get=states.get))

    forecast = read_solar_forecast_kwh(hass, controller)

    assert forecast is not None
    assert forecast.kwh == pytest.approx(20.52)
    assert forecast.source == "today"


def test_config_normalization_keeps_remaining_and_preserves_legacy_only_entries():
    assert normalize_solar_forecast_config(
        {
            "solar_forecast_sensor": "sensor.today",
            "solar_forecast_remaining_sensor": "sensor.remaining",
        }
    ) == {"solar_forecast_remaining_sensor": "sensor.remaining"}
    assert normalize_solar_forecast_config(
        {"solar_forecast_sensor": "sensor.today"}
    ) == {"solar_forecast_sensor": "sensor.today"}

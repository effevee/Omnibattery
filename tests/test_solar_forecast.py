"""Regression coverage for the dual solar-forecast migration."""
from types import SimpleNamespace

import pytest

from custom_components.omnibattery.pricing.engine import PricingManager
from custom_components.omnibattery.const import CONF_SOLAR_FORECAST_REMAINING_SENSOR
from custom_components.omnibattery.solar_forecast import (
    SolarForecastInput,
    get_configured_solar_forecast_sensor,
    normalize_solar_forecast_config,
    read_remaining_solar_kwh,
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


def test_forecast_reader_uses_persisted_remaining_sensor_when_runtime_cache_is_empty():
    controller = SimpleNamespace(
        config_entry=SimpleNamespace(
            data={CONF_SOLAR_FORECAST_REMAINING_SENSOR: "sensor.remaining"},
            options={},
        ),
        solar_forecast_remaining_sensor=None,
        solar_forecast_sensor=None,
    )
    hass = SimpleNamespace(
        states=SimpleNamespace(
            get=lambda entity_id: _state(41.94) if entity_id == "sensor.remaining" else None
        )
    )

    assert get_configured_solar_forecast_sensor(controller, "remaining") == "sensor.remaining"
    forecast = read_solar_forecast_kwh(hass, controller)

    assert forecast is not None
    assert forecast.source == "remaining"
    assert forecast.kwh == pytest.approx(41.94)


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


def test_remaining_solar_adapter_normalizes_temporal_shape_to_remaining_energy():
    normalized = SolarForecastInput(
        remaining_kwh=4.0,
        source="remaining_sensor",
        temporal_shape=(1.0, 2.0, 1.0),
    ).normalized_shape()

    assert normalized == pytest.approx([1.0, 2.0, 1.0])
    assert sum(normalized) == pytest.approx(4.0)


def test_remaining_solar_adapter_exposes_safe_fallback():
    controller = SimpleNamespace(
        solar_forecast_sensor=None,
        solar_forecast_remaining_sensor=None,
    )
    hass = SimpleNamespace(states=SimpleNamespace(get=lambda _entity_id: None))

    result = read_remaining_solar_kwh(hass, controller)

    assert result.remaining_kwh == 0.0
    assert result.source == "fallback"
    assert controller.solar_forecast_source == "fallback"

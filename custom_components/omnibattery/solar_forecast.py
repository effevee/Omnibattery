"""Read solar forecast sensors with an explicit forecast horizon.

``solar_forecast_sensor`` is the legacy whole-day (``today``) value.  Newer
providers also expose a ``remaining today`` value.  Keeping the distinction in
one place prevents callers from accidentally subtracting production twice.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Literal

from .const import (
    CONF_SOLAR_FORECAST_REMAINING_SENSOR,
    CONF_SOLAR_FORECAST_SENSOR,
)


ForecastSource = Literal["remaining", "today"]


@dataclass(frozen=True)
class SolarForecast:
    """A normalized solar forecast and the horizon it represents."""

    kwh: float
    source: ForecastSource
    sensor: str

    @property
    def remaining_kwh(self) -> float:
        """Normalized future energy consumed by all control decisions."""
        return self.kwh

    @property
    def diagnostic_source(self) -> str:
        """Stable diagnostic label distinguishing the migration paths."""
        return "remaining_sensor" if self.source == "remaining" else "today_legacy"


@dataclass(frozen=True)
class SolarForecastInput:
    """Consumer-facing solar contract with an optional normalized curve."""

    remaining_kwh: float
    source: str
    temporal_shape: tuple[float, ...] | None = None

    def __post_init__(self) -> None:
        """Keep the normalized contract finite even with a loose sensor value."""
        try:
            value = float(self.remaining_kwh)
        except (TypeError, ValueError):
            value = 0.0
        object.__setattr__(
            self,
            "remaining_kwh",
            value if math.isfinite(value) and value >= 0.0 else 0.0,
        )

    def normalized_shape(self) -> list[float] | None:
        """Return a shape whose values sum exactly to ``remaining_kwh``."""
        if self.temporal_shape is None:
            return None
        values = []
        for value in self.temporal_shape:
            try:
                parsed = float(value)
            except (TypeError, ValueError):
                parsed = 0.0
            values.append(parsed if math.isfinite(parsed) and parsed >= 0.0 else 0.0)
        total = sum(values)
        if total <= 0.0:
            return [0.0] * len(values)
        factor = max(0.0, self.remaining_kwh) / total
        normalized = [value * factor for value in values]
        if normalized:
            normalized[-1] += max(0.0, self.remaining_kwh) - sum(normalized)
        return normalized


def get_configured_solar_forecast_sensor(
    controller: Any,
    source: ForecastSource,
) -> str | None:
    """Return the effective configured entity for a forecast horizon.

    The controller keeps a runtime copy of these values, but an options update
    can briefly leave that copy behind the config entry.  Read the persisted
    entry first so horizon selection cannot silently fall back to the daily
    legacy path while the remaining-today sensor is configured.
    """
    if source == "remaining":
        key = CONF_SOLAR_FORECAST_REMAINING_SENSOR
        attribute = "solar_forecast_remaining_sensor"
    else:
        key = CONF_SOLAR_FORECAST_SENSOR
        attribute = "solar_forecast_sensor"

    config_entry = getattr(controller, "config_entry", None)
    if config_entry is not None:
        has_persisted_config = False
        for config in (
            getattr(config_entry, "data", None),
            getattr(config_entry, "options", None),
        ):
            if config is None:
                continue
            has_persisted_config = True
            if key not in config:
                continue
            value = config.get(key)
            return value if value else None
        # A real config entry is authoritative even when the key is absent.
        # This prevents a stale runtime attribute from resurrecting a sensor
        # that was cleared in the options flow.
        if has_persisted_config:
            return None

    # Small unit-test doubles and lightweight consumers may not carry a config
    # entry; preserve their existing runtime-only contract.
    value = getattr(controller, attribute, None)
    return value if value else None


def normalize_solar_forecast_config(data: dict[str, Any]) -> dict[str, Any]:
    """Keep at most one persisted solar forecast horizon.

    A configured remaining-today sensor supersedes ``today``. Empty values are
    removed rather than stored as config keys, which lets Repairs distinguish a
    real legacy configuration from a cleared field.
    """
    normalized = dict(data)
    remaining = normalized.get(CONF_SOLAR_FORECAST_REMAINING_SENSOR)
    if remaining:
        normalized.pop(CONF_SOLAR_FORECAST_SENSOR, None)
    else:
        normalized.pop(CONF_SOLAR_FORECAST_REMAINING_SENSOR, None)
        if not normalized.get(CONF_SOLAR_FORECAST_SENSOR):
            normalized.pop(CONF_SOLAR_FORECAST_SENSOR, None)
    return normalized


def _state_kwh(state: Any) -> float | None:
    """Return a finite non-negative sensor state in kWh, converting Wh."""
    if state is None or getattr(state, "state", None) in ("unknown", "unavailable"):
        return None
    try:
        value = float(state.state)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value) or value < 0:
        return None
    unit = str(getattr(state, "attributes", {}).get("unit_of_measurement", "kWh")).strip().lower()
    if unit == "wh":
        value /= 1000.0
    elif unit != "kwh":
        return None
    return value


def read_solar_forecast_kwh(hass: Any, controller: Any) -> SolarForecast | None:
    """Read the preferred forecast: remaining first, legacy today second.

    An unavailable remaining sensor deliberately falls back to the configured
    legacy sensor during the migration.  A valid remaining value is never
    transformed; consumers can rely on it already being the future horizon.
    """
    candidates = (
        (
            "remaining",
            get_configured_solar_forecast_sensor(controller, "remaining"),
        ),
        ("today", get_configured_solar_forecast_sensor(controller, "today")),
    )
    for source, sensor in candidates:
        if not sensor:
            continue
        value = _state_kwh(hass.states.get(sensor))
        if value is not None:
            # Kept on the controller for diagnostics and lightweight consumers.
            controller.solar_forecast_source = source
            forecast = SolarForecast(value, source, sensor)
            controller.solar_forecast_diagnostic_source = forecast.diagnostic_source
            return forecast
    controller.solar_forecast_source = None
    controller.solar_forecast_diagnostic_source = None
    return None


def read_remaining_solar_kwh(hass: Any, controller: Any) -> SolarForecastInput:
    """Read the normalized future-solar contract, with a safe zero fallback."""
    forecast = read_solar_forecast_kwh(hass, controller)
    if forecast is None:
        controller.solar_forecast_source = "fallback"
        controller.solar_forecast_diagnostic_source = "fallback"
        return SolarForecastInput(0.0, "fallback")
    return SolarForecastInput(forecast.remaining_kwh, forecast.diagnostic_source)

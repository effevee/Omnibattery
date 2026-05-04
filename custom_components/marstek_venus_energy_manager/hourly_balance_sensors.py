"""Hourly net balance sensor entities for Marstek Venus Energy Manager."""
from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, CONF_ENABLE_HOURLY_BALANCE
from .hourly_balance import HourlyBalanceManager


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up hourly balance sensor entities."""
    if not entry.data.get(CONF_ENABLE_HOURLY_BALANCE, False):
        return

    controller = hass.data[DOMAIN][entry.entry_id].get("controller")
    if controller is None or controller._hourly_balance_mgr is None:
        return

    mgr: HourlyBalanceManager = controller._hourly_balance_mgr

    entities: list[SensorEntity] = [
        HourlyNetEnergySensor(entry, mgr),
        HourlyBalanceOffsetSensor(entry, mgr),
        HourlyBalanceStatusSensor(entry, mgr),
    ]

    for entity in entities:
        mgr.register_sensor(entity)

    async_add_entities(entities)


# ---------------------------------------------------------------------------
# Base class
# ---------------------------------------------------------------------------

class _HourlyBalanceBase(SensorEntity):
    """Common base for hourly balance sensors."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, entry: ConfigEntry, mgr: HourlyBalanceManager) -> None:
        self._entry = entry
        self._mgr = mgr

    @property
    def device_info(self) -> dict:
        return {
            "identifiers": {(DOMAIN, "marstek_venus_system")},
            "name": "Marstek Venus System",
            "manufacturer": "Marstek",
            "model": "Venus Multi-Battery System",
        }


# ---------------------------------------------------------------------------
# Concrete sensors
# ---------------------------------------------------------------------------

class HourlyNetEnergySensor(_HourlyBalanceBase):
    """Current hour net energy (import − export) in Wh."""

    _attr_translation_key = "hourly_net_energy"
    _attr_native_unit_of_measurement = "Wh"
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:scale-balance"

    def __init__(self, entry: ConfigEntry, mgr: HourlyBalanceManager) -> None:
        super().__init__(entry, mgr)
        self._attr_unique_id = f"{entry.entry_id}_hourly_net_energy"

    @property
    def native_value(self) -> float | None:
        status = self._mgr.get_status_dict()
        return status["net_wh"]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        status = self._mgr.get_status_dict()
        return {
            "imp_wh": status["imp_wh"],
            "exp_wh": status["exp_wh"],
            "target_net_wh": status["target_net_wh"],
            "elapsed_min": status["elapsed_min"],
            "remaining_min": status["remaining_min"],
            "in_active_slot": status["in_active_slot"],
        }


class HourlyBalanceOffsetSensor(_HourlyBalanceBase):
    """Current setpoint offset applied by the hourly balance feature (W)."""

    _attr_translation_key = "hourly_balance_offset"
    _attr_native_unit_of_measurement = "W"
    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:transmission-tower"

    def __init__(self, entry: ConfigEntry, mgr: HourlyBalanceManager) -> None:
        super().__init__(entry, mgr)
        self._attr_unique_id = f"{entry.entry_id}_hourly_balance_offset"

    @property
    def native_value(self) -> float | None:
        return self._mgr.get_status_dict()["offset_w"]


class HourlyBalanceStatusSensor(_HourlyBalanceBase):
    """String state label for the hourly balance feature, with full status as attributes."""

    _attr_translation_key = "hourly_balance_status"
    _attr_icon = "mdi:chart-timeline-variant"

    def __init__(self, entry: ConfigEntry, mgr: HourlyBalanceManager) -> None:
        super().__init__(entry, mgr)
        self._attr_unique_id = f"{entry.entry_id}_hourly_balance_status"

    @property
    def native_value(self) -> str:
        return self._mgr.get_state_label()

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        status = self._mgr.get_status_dict()
        return {
            "net_wh": status["net_wh"],
            "imp_wh": status["imp_wh"],
            "exp_wh": status["exp_wh"],
            "elapsed_min": status["elapsed_min"],
            "remaining_min": status["remaining_min"],
            "target_net_wh": status["target_net_wh"],
            "offset_w": status["offset_w"],
            "in_active_slot": status["in_active_slot"],
            "hour_iso": status["hour_iso"],
            "history": status["history"],
        }

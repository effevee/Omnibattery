"""Huawei SUN2000 + LUNA2000 driver.

This driver is deliberately *split-transport*, unlike every other brand here:

* **Telemetry is read natively** over Modbus TCP (FC03) from the inverter, so
  the control loop sees fresh battery power and SOC at its own cadence. Going
  through the ``huawei_solar`` integration's entities instead would cap the
  feedback at its hardcoded 30 s coordinator interval — far too slow for the PD
  loop, which polls every 2 s.
* **Set-points are written through the ``huawei_solar`` integration's services**
  rather than by writing registers here. A forcible charge is a four-register
  sequence (power, duration, target mode, mode) whose ordering and safety
  semantics that integration already owns and maintains; duplicating it would
  mean re-implementing a control path against undocumented registers.

The practical consequence: ``huawei_solar`` must stay installed and hold the
battery device, while this driver needs its own read path to the same inverter.
Both connections coexist behind a Modbus proxy (the add-on exists precisely to
fan one Modbus slave out to several clients).

Sign conventions:
  Omnibattery net power: +charge / −discharge
  Huawei 37001 charge/discharge power: +charge / −discharge  → identical, no flip.

Idle semantics deserve a note. ``stop_forcible_charge`` does not idle the
battery: it hands control back to the inverter's own working mode, which
immediately resumes self-consumption. A *held* zero is instead expressed as a
forcible charge at 0 W, which the hardware accepts and sustains. That is what
``apply_setpoint(0)`` does, so Omnibattery keeps ownership of the grid balance;
``standby()`` uses the real stop, because releasing the battery back to the
inverter is the correct state to leave behind on shutdown.

Register map and control behaviour verified on a SUN2000-8K-MAP0
(V200R024C00SPC110) with a LUNA2000 13.8 kWh behind an EMMA-A02.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er

from ..infra.huawei_modbus_client import (
    HuaweiModbusClient,
    decode_i16,
    decode_i32,
    decode_string,
    decode_u16,
    decode_u32,
)
from .base import (
    BatteryDriver,
    DriverCapabilities,
    ReadGroup,
    SetpointResult,
    TelemetrySnapshot,
)

_LOGGER = logging.getLogger(__name__)

_DOMAIN_HUAWEI_SOLAR = "huawei_solar"

# Forcible charge/discharge mode (register 47100).
_FORCIBLE_STOP = 0
_FORCIBLE_CHARGE = 1
_FORCIBLE_DISCHARGE = 2

# Working mode (register 47086), StorageWorkingModesC.
_WORKING_MODE_LABELS = {
    0: "Adaptive",
    1: "Fixed charge/discharge",
    2: "Maximise self consumption",
    3: "Time of use (LG)",
    4: "Fully fed to grid",
    5: "Time of use (LUNA2000)",
}

# Storage running status (register 37000), StorageStatus.
_STORAGE_STATUS_LABELS = {
    0: "Offline",
    1: "Standby",
    2: "Running",
    3: "Fault",
    4: "Sleep mode",
}

# Envelope ceiling. The live per-model caps come from 37046/37048; this only
# bounds a malformed reading so it cannot inflate the PD limits.
_HW_MAX_POWER_W = 15000

# A forcible command carries a duration and expires on its own. Long enough that
# a re-issue is never racing the timer, short enough to act as a watchdog: if
# Omnibattery dies mid-command, the inverter returns to its own control within
# this window instead of staying latched.
_COMMAND_DURATION_MIN = 10

# Every set-point costs four serialised Modbus writes inside huawei_solar and
# the battery needs ~10 s to reach a new target, so a 2 s PD tick must not turn
# into a 2 s write rate. Re-issue only on a meaningful change, a direction
# change, or once the command is old enough to be worth refreshing.
_WRITE_DEADBAND_W = 250
_MIN_WRITE_INTERVAL_S = 15.0
_COMMAND_REFRESH_S = 240.0

# Documented ranges of the inverter's own cutoff registers. They are narrower
# than the SOC window Omnibattery lets the user pick, which is why the software
# enforces the window and these writes are only a best-effort backstop.
_CHARGE_CUTOFF_RANGE = (90.0, 100.0)      # register 47081
_DISCHARGE_CUTOFF_RANGE = (0.0, 20.0)     # register 47082

# Measured ramp from register write to settled power (7-14 s observed); declared
# conservatively. Telemetry itself is ~4 ms away, so the readback delay is the
# physical ramp, not the transport.
_ACTUATOR_LATENCY_S = 15.0

# --- register blocks (all FC03 holding, all verified on hardware) ------------
# (start, count, scan_interval, {key: (offset, decoder, scale)})
_BLOCK_LIVE = (37000, 5, "high", {
    "inverter_state": (0, "u16", 1),
    "battery_power": (1, "i32", 1),
    "battery_voltage": (3, "u16", 0.1),
    "battery_soc": (4, "u16", 0.1),
})
_BLOCK_DAILY = (37015, 8, "low", {
    "total_daily_charging_energy": (0, "u32", 0.01),
    "total_daily_discharging_energy": (2, "u32", 0.01),
    "internal_temperature": (7, "i16", 0.1),
})
_BLOCK_LIMITS = (37046, 4, "low", {
    "max_charge_power": (0, "u32", 1),
    "max_discharge_power": (2, "u32", 1),
})
_BLOCK_TOTALS = (37066, 4, "low", {
    "total_charging_energy": (0, "u32", 0.01),
    "total_discharging_energy": (2, "u32", 0.01),
})
_BLOCK_CAPACITY = (37758, 2, "very_low", {
    "battery_total_energy": (0, "u32", 1),
})
_BLOCK_PV = (32064, 18, "high", {
    "pv_power": (0, "i32", 1),
    "ac_power": (16, "i32", 1),
})
_BLOCK_CONFIG = (47081, 7, "low", {
    "charging_cutoff_capacity": (0, "u16", 0.1),
    "discharging_cutoff_capacity": (1, "u16", 0.1),
    "user_work_mode": (5, "u16", 1),
    "charge_from_grid": (6, "u16", 1),
})
_BLOCK_FORCIBLE_MODE = (47100, 1, "medium", {
    "force_mode": (0, "u16", 1),
})
_BLOCK_FORCIBLE_POWER = (47246, 5, "medium", {
    "set_charge_power": (1, "u32", 1),
    "set_discharge_power": (3, "u32", 1),
})
_BLOCK_MODEL = (30000, 15, "very_low", {"device_name": (0, "str", 15)})
_BLOCK_SERIAL = (37052, 10, "very_low", {"serial_number": (0, "str", 10)})

_BLOCKS = (
    _BLOCK_LIVE, _BLOCK_PV, _BLOCK_FORCIBLE_MODE, _BLOCK_FORCIBLE_POWER,
    _BLOCK_DAILY, _BLOCK_LIMITS, _BLOCK_TOTALS, _BLOCK_CONFIG,
    _BLOCK_CAPACITY, _BLOCK_MODEL, _BLOCK_SERIAL,
)

_DECODERS = {"u16": decode_u16, "i16": decode_i16, "u32": decode_u32, "i32": decode_i32}

SENSOR_DEFINITIONS = [
    {"key": "battery_soc", "name": "Battery SOC", "unit": "%", "device_class": "battery", "state_class": "measurement", "scale": 1, "precision": 1, "scan_interval": "high", "enabled_by_default": True},
    {"key": "battery_power", "name": "Battery Power", "unit": "W", "device_class": "power", "state_class": "measurement", "scale": 1, "precision": 0, "scan_interval": "high", "enabled_by_default": True},
    {"key": "battery_voltage", "name": "Battery Voltage", "unit": "V", "device_class": "voltage", "state_class": "measurement", "scale": 1, "precision": 1, "scan_interval": "high", "enabled_by_default": True},
    {"key": "battery_total_energy", "name": "Battery Total Energy", "unit": "Wh", "device_class": "energy_storage", "state_class": "measurement", "scale": 1, "precision": 0, "scan_interval": "very_low", "enabled_by_default": True},
    {"key": "pv_power", "name": "PV Power", "unit": "W", "device_class": "power", "state_class": "measurement", "scale": 1, "precision": 0, "scan_interval": "high", "enabled_by_default": True},
    {"key": "ac_power", "name": "Inverter Active Power", "unit": "W", "device_class": "power", "state_class": "measurement", "scale": 1, "precision": 0, "scan_interval": "high", "enabled_by_default": True},
    {"key": "internal_temperature", "name": "Battery Temperature", "unit": "°C", "device_class": "temperature", "state_class": "measurement", "scale": 1, "precision": 1, "scan_interval": "low", "enabled_by_default": True},
    {"key": "total_charging_energy", "name": "Total Charging Energy", "unit": "kWh", "device_class": "energy", "state_class": "total_increasing", "scale": 1, "precision": 2, "scan_interval": "low", "enabled_by_default": True},
    {"key": "total_discharging_energy", "name": "Total Discharging Energy", "unit": "kWh", "device_class": "energy", "state_class": "total_increasing", "scale": 1, "precision": 2, "scan_interval": "low", "enabled_by_default": True},
    {"key": "total_daily_charging_energy", "name": "Daily Charging Energy", "unit": "kWh", "device_class": "energy", "state_class": "total_increasing", "scale": 1, "precision": 2, "scan_interval": "low", "enabled_by_default": True},
    {"key": "total_daily_discharging_energy", "name": "Daily Discharging Energy", "unit": "kWh", "device_class": "energy", "state_class": "total_increasing", "scale": 1, "precision": 2, "scan_interval": "low", "enabled_by_default": True},
    {"key": "max_charge_power", "name": "Max Charge Power", "unit": "W", "device_class": "power", "state_class": "measurement", "scale": 1, "precision": 0, "category": "diagnostic", "scan_interval": "low", "enabled_by_default": False},
    {"key": "max_discharge_power", "name": "Max Discharge Power", "unit": "W", "device_class": "power", "state_class": "measurement", "scale": 1, "precision": 0, "category": "diagnostic", "scan_interval": "low", "enabled_by_default": False},
    {"key": "charging_cutoff_capacity", "name": "Charging Cutoff SOC", "unit": "%", "state_class": "measurement", "scale": 1, "precision": 1, "category": "diagnostic", "scan_interval": "low", "enabled_by_default": False},
    {"key": "discharging_cutoff_capacity", "name": "Discharging Cutoff SOC", "unit": "%", "state_class": "measurement", "scale": 1, "precision": 1, "category": "diagnostic", "scan_interval": "low", "enabled_by_default": False},
    {"key": "inverter_state", "name": "Storage Status", "data_type": "char", "icon": "mdi:state-machine", "category": "diagnostic", "scan_interval": "high", "enabled_by_default": True},
    {"key": "user_work_mode", "name": "Working Mode", "data_type": "char", "icon": "mdi:cog-outline", "category": "diagnostic", "scan_interval": "low", "enabled_by_default": True},
    {"key": "device_name", "name": "Device Model", "data_type": "char", "icon": "mdi:information-outline", "category": "diagnostic", "scan_interval": "very_low", "enabled_by_default": True},
    {"key": "serial_number", "name": "Serial Number", "data_type": "char", "icon": "mdi:identifier", "category": "diagnostic", "scan_interval": "very_low", "enabled_by_default": False},
]


class HuaweiSolarDriver(BatteryDriver):
    """One Huawei inverter's attached LUNA2000, read natively and driven by service."""

    def __init__(
        self,
        hass: HomeAssistant,
        host: str,
        *,
        port: int = 502,
        slave_id: int = 1,
        battery_device_id: str = "",
        max_charge_power_w: int = 5000,
        max_discharge_power_w: int = 5000,
        client: Optional[HuaweiModbusClient] = None,
    ) -> None:
        self.hass = hass
        self._battery_device_id = battery_device_id
        self._client = client if client is not None else HuaweiModbusClient(host, port, slave_id)
        self._shutting_down = False
        self._model: Optional[str] = None
        self._serial: Optional[str] = None
        # Last command actually written, so the deadband can compare against what
        # the hardware was told rather than against what it currently delivers.
        self._last_written_w: Optional[int] = None
        self._last_write_monotonic = 0.0
        max_charge_power_w = max(0, min(int(max_charge_power_w), _HW_MAX_POWER_W))
        max_discharge_power_w = max(0, min(int(max_discharge_power_w), _HW_MAX_POWER_W))
        self._capabilities = DriverCapabilities(
            # 47081/47082 are real cutoff registers, but they only accept
            # 90-100 % and 0-20 % respectively — narrower than the window a user
            # may configure here. Claiming hardware enforcement would silently
            # leave an out-of-range limit unenforced, so the control layer owns
            # the SOC window and the registers are kept as a backstop.
            hardware_soc_cutoff=False,
            has_force_mode=True,
            push_telemetry=False,
            max_charge_power_w=max_charge_power_w,
            max_discharge_power_w=max_discharge_power_w,
            has_mppt_pv=False,
            has_alarm_registers=False,
            has_rs485_control=False,
            has_energy_counters=True,
            has_daily_energy_counters=True,
            # A forcible command is acknowledged in its registers long before the
            # battery has ramped, so an immediate readback would report a
            # mismatch that resolves itself seconds later.
            setpoint_confirm_reliable=False,
            actuator_latency_s=_ACTUATOR_LATENCY_S,
            readback_latency_s=_ACTUATOR_LATENCY_S,
            engage_grace_s=_ACTUATOR_LATENCY_S,
        )
        self._read_groups = [
            ReadGroup(interval, tuple(keys))
            for _start, _count, interval, keys in _BLOCKS
        ]

    # --- identity -----------------------------------------------------------

    @property
    def capabilities(self) -> DriverCapabilities:
        return self._capabilities

    @property
    def model_label(self) -> Optional[str]:
        return self._model or "Huawei LUNA2000"

    @property
    def serial(self) -> Optional[str]:
        return self._serial

    @property
    def connected(self) -> bool:
        return self._client.connected

    @property
    def read_groups(self) -> list[ReadGroup]:
        return self._read_groups

    @property
    def sensor_definitions(self) -> list[dict]:
        return SENSOR_DEFINITIONS

    @property
    def number_definitions(self) -> list[dict]:
        return []

    @property
    def select_definitions(self) -> list[dict]:
        return []

    @property
    def switch_definitions(self) -> list[dict]:
        return []

    @property
    def binary_sensor_definitions(self) -> list[dict]:
        return []

    @property
    def button_definitions(self) -> list[dict]:
        return []

    @property
    def all_definitions(self) -> list[dict]:
        return SENSOR_DEFINITIONS

    # --- connection lifecycle ----------------------------------------------

    async def connect(self) -> bool:
        if not await self._client.async_connect():
            return False
        # Identity is cheap and only read here; it also proves the slave id
        # points at an inverter rather than at the EMMA or a charger.
        identity = await self.read_telemetry(["device_name", "serial_number"])
        self._model = identity.get("device_name") or self._model
        self._serial = identity.get("serial_number") or self._serial
        return True

    async def close(self) -> None:
        await self._client.async_close()

    def set_shutting_down(self, value: bool) -> None:
        self._shutting_down = bool(value)
        self._client.set_shutting_down(value)

    # --- telemetry (read) ---------------------------------------------------

    async def read_telemetry(self, keys: Optional[list[str]] = None) -> TelemetrySnapshot:
        requested = set(keys) if keys is not None else None
        snapshot: TelemetrySnapshot = {}

        for start, count, _interval, fields in _BLOCKS:
            wanted = fields if requested is None else {
                key: spec for key, spec in fields.items() if key in requested
            }
            if not wanted:
                continue
            regs = await self._client.async_read_holding_block(start, count)
            if regs is None:
                # A failed block omits its keys rather than publishing zeros; the
                # coordinator treats missing keys as stale, which is correct.
                continue
            for key, (offset, kind, scale) in wanted.items():
                try:
                    if kind == "str":
                        value = decode_string(regs, offset, int(scale))
                    else:
                        raw = _DECODERS[kind](regs, offset)
                        # Rounding here keeps binary-fraction artefacts such as
                        # 354 * 0.1 -> 35.300000000000004 out of the telemetry
                        # cache, which is compared and logged verbatim.
                        value = raw if scale == 1 else round(raw * scale, 4)
                except (IndexError, ValueError, KeyError):
                    continue
                if value is None:
                    continue
                snapshot[key] = value

        # Enum registers become their label; the raw number stays available to
        # the control layer under the same key only where it is numeric.
        if "inverter_state" in snapshot:
            snapshot["inverter_state"] = _STORAGE_STATUS_LABELS.get(
                int(snapshot["inverter_state"]), f"Unknown ({snapshot['inverter_state']})"
            )
        if "user_work_mode" in snapshot:
            snapshot["user_work_mode"] = _WORKING_MODE_LABELS.get(
                int(snapshot["user_work_mode"]), f"Unknown ({snapshot['user_work_mode']})"
            )
        return snapshot

    # --- control (write) ----------------------------------------------------

    async def apply_setpoint(
        self,
        net_power_w: int,
        *,
        mode_hint: Optional[str] = None,
        read_back: bool = True,
    ) -> SetpointResult:
        """Command a signed net power through the huawei_solar services."""
        applied = max(
            -self._capabilities.max_discharge_power_w,
            min(self._capabilities.max_charge_power_w, int(net_power_w)),
        )
        if not self._battery_device_id:
            return SetpointResult(
                ok=False, net_power_w=applied, confirmed=False,
                failure_reason="no_battery_device",
            )

        if not self._should_write(applied):
            # Nothing was sent, but the standing command still delivers this
            # set-point, so this is a success with the previous value held.
            return SetpointResult(
                ok=True, net_power_w=applied, confirmed=False,
                applied=self._echo(applied),
            )

        if applied >= 0:
            # 0 W is a held idle, not a release: a forcible charge at zero keeps
            # the inverter's own self-consumption control out of the loop.
            service, data = "forcible_charge", {"power": applied}
        else:
            service, data = "forcible_discharge", {"power": -applied}
        data["duration"] = _COMMAND_DURATION_MIN

        if not await self._call_service(service, data):
            return SetpointResult(
                ok=False, net_power_w=applied, confirmed=False,
                failure_reason="service_call_failed",
            )
        self._last_written_w = applied
        self._last_write_monotonic = asyncio.get_running_loop().time()

        if not read_back:
            return SetpointResult(
                ok=True, net_power_w=applied, confirmed=False, applied=self._echo(applied)
            )

        echo = await self.read_telemetry(
            ["force_mode", "set_charge_power", "set_discharge_power", "battery_power"]
        )
        expected_mode = _FORCIBLE_CHARGE if applied >= 0 else _FORCIBLE_DISCHARGE
        confirmed = echo.get("force_mode") == expected_mode
        battery_power = echo.get("battery_power")
        applied_echo = self._echo(applied)
        applied_echo.update(echo)
        return SetpointResult(
            ok=True,
            net_power_w=applied,
            confirmed=confirmed,
            # The command registers echo instantly while the battery is still
            # ramping, so a confirmed echo is never an exact power match.
            exact=False,
            battery_power_w=int(battery_power) if battery_power is not None else None,
            applied=applied_echo,
        )

    @staticmethod
    def _direction(power_w: int) -> int:
        """Charging, discharging, or held at zero — as three distinct states."""
        if power_w == 0:
            return 0
        return 1 if power_w > 0 else -1

    def _should_write(self, applied: int) -> bool:
        """Whether this set-point is worth four Modbus writes and a 10 s ramp."""
        if self._last_written_w is None:
            return True
        # A change of direction — including leaving or entering a held zero — is
        # the most material change there is, and the control layer has already
        # applied its own hysteresis before asking. Deferring it would keep the
        # battery pushing the wrong way across the meter for the whole interval,
        # so this is checked before any rate limit.
        if self._direction(applied) != self._direction(self._last_written_w):
            return True
        since = asyncio.get_running_loop().time() - self._last_write_monotonic
        # Refresh before the command's own duration runs out, so the battery
        # never silently falls back to inverter control mid-regulation.
        if since >= _COMMAND_REFRESH_S:
            return True
        # Within one direction, rewriting mid-ramp achieves nothing: the battery
        # is still travelling towards the previous target.
        if since < _MIN_WRITE_INTERVAL_S:
            return False
        return abs(applied - self._last_written_w) >= _WRITE_DEADBAND_W

    def _echo(self, applied: int) -> dict:
        return {
            "force_mode": _FORCIBLE_CHARGE if applied >= 0 else _FORCIBLE_DISCHARGE,
            "set_charge_power": applied if applied > 0 else 0,
            "set_discharge_power": -applied if applied < 0 else 0,
        }

    async def _call_service(self, service: str, data: dict) -> bool:
        try:
            await self.hass.services.async_call(
                _DOMAIN_HUAWEI_SOLAR,
                service,
                {"device_id": self._battery_device_id, **data},
                blocking=True,
            )
            return True
        except Exception as exc:
            if not self._shutting_down:
                _LOGGER.warning(
                    "Huawei driver: %s.%s failed: %s", _DOMAIN_HUAWEI_SOLAR, service, exc
                )
            return False

    async def write_control(self, key: str, value: int) -> bool:
        """No user-facing control entities are exposed by this driver."""
        return False

    def net_power_from_data(self, data: dict) -> Optional[int]:
        mode = data.get("force_mode")
        charge = data.get("set_charge_power")
        discharge = data.get("set_discharge_power")
        if mode is None or charge is None or discharge is None:
            return None
        mode = int(round(float(mode)))
        if mode == _FORCIBLE_CHARGE:
            return int(round(float(charge)))
        if mode == _FORCIBLE_DISCHARGE:
            return -int(round(float(discharge)))
        return 0

    @property
    def control_dependency_keys(self) -> frozenset:
        return frozenset({
            "force_mode", "set_charge_power", "set_discharge_power",
            "max_charge_power", "max_discharge_power",
            "charging_cutoff_capacity", "discharging_cutoff_capacity",
        })

    # --- concrete methods the coordinator calls without isinstance guards ----

    async def apply_config(
        self,
        *,
        max_soc_pct: float,
        min_soc_pct: float,
        max_charge_power_w: int,
        max_discharge_power_w: int,
        **_kwargs,
    ) -> bool:
        """Push the SOC window to the inverter's own cutoff registers.

        Power caps are deliberately skipped: 37046/37048 are commissioning
        values that belong to the installer, not to a battery manager.

        Always reports success. The SOC window is enforced by the control layer
        for this brand, so tightening the inverter's own cutoffs is a bonus, not
        a requirement — reporting failure here would raise a warning about
        something that is working as designed.
        """
        await self.set_charge_cutoff(max_soc_pct)
        await self._write_cutoff("discharging", min_soc_pct)
        return True

    async def set_charge_cutoff(self, soc_pct: float) -> bool:
        return await self._write_cutoff("charging", soc_pct)

    async def _write_cutoff(self, which: str, soc_pct: float) -> bool:
        """Write a cutoff through the huawei_solar number entity for it.

        Values the register cannot represent are skipped rather than clamped: a
        clamped write would move the hardware backstop somewhere the user never
        asked for. The control layer enforces the real window either way.
        """
        low, high = (
            _CHARGE_CUTOFF_RANGE if which == "charging" else _DISCHARGE_CUTOFF_RANGE
        )
        if not low <= float(soc_pct) <= high:
            _LOGGER.debug(
                "Huawei driver: %s cutoff %.1f%% is outside the register range "
                "%.0f-%.0f%%; leaving the hardware backstop untouched",
                which, float(soc_pct), low, high,
            )
            return False
        entity_id = self._resolve_entity(f"storage_{which}_cutoff_capacity")
        if entity_id is None:
            return False
        try:
            await self.hass.services.async_call(
                "number",
                "set_value",
                {"entity_id": entity_id, "value": round(float(soc_pct), 1)},
                blocking=True,
            )
            return True
        except Exception as exc:
            if not self._shutting_down:
                _LOGGER.warning(
                    "Huawei driver: writing %s cutoff via %s failed: %s",
                    which, entity_id, exc,
                )
            return False

    def _resolve_entity(self, register_name: str) -> Optional[str]:
        """Find a huawei_solar entity by its register name.

        huawei_solar builds unique ids as ``<serial>_<register name>``, which is
        stable across renames and translations — unlike the entity id or the
        friendly name, which the user owns.

        The search spans the whole config entry rather than the battery device,
        because huawei_solar splits battery settings across devices: the charge
        cutoff sits on the inverter while the discharge cutoff sits on the
        battery. Looking only at the configured battery device finds one and
        misses the other.
        """
        if not self._battery_device_id:
            return None
        device = dr.async_get(self.hass).async_get(self._battery_device_id)
        if device is None:
            return None
        registry = er.async_get(self.hass)
        suffix = f"_{register_name}"
        for config_entry_id in device.config_entries:
            for entry in er.async_entries_for_config_entry(registry, config_entry_id):
                if (
                    entry.platform == _DOMAIN_HUAWEI_SOLAR
                    and not entry.disabled
                    and entry.unique_id.endswith(suffix)
                ):
                    return entry.entity_id
        return None

    async def standby(self) -> bool:
        """Release the battery back to the inverter before shutting down.

        Unlike ``apply_setpoint(0)``, this is a real stop: leaving a forcible
        command latched when Omnibattery goes away would freeze the battery at
        whatever it was last told until the command's duration expires.
        """
        ok = await self._call_service("stop_forcible_charge", {})
        if ok:
            self._last_written_w = None
            self._last_write_monotonic = 0.0
        return ok

    async def set_rs485_control(self, enable: bool) -> bool:
        return False

    async def get_rs485_control(self) -> Optional[bool]:
        return None

    # --- config-flow probe ---------------------------------------------------

    @classmethod
    async def probe(
        cls, hass: HomeAssistant, host: str, port: int = 502, slave_id: int = 1
    ) -> tuple[bool, Optional[str], Optional[int], Optional[int]]:
        """Check the read path and report model plus the hardware power caps."""
        driver = cls(hass, host, port=port, slave_id=slave_id)
        try:
            if not await driver.connect():
                return False, None, None, None
            data = await driver.read_telemetry(
                ["device_name", "battery_soc", "max_charge_power", "max_discharge_power"]
            )
            # SOC proves a battery is actually attached; the model alone would
            # also match an inverter running without storage.
            if "battery_soc" not in data:
                return False, data.get("device_name"), None, None
            return (
                True,
                data.get("device_name"),
                data.get("max_charge_power"),
                data.get("max_discharge_power"),
            )
        finally:
            await driver.close()

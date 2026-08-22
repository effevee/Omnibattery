"""Unit tests for the Huawei SUN2000 + LUNA2000 driver.

The driver is split-transport: Modbus for telemetry, huawei_solar services for
control. Both halves are faked here, so no inverter and no HA runtime is needed.

Register values in the fixtures are the ones read from a real
SUN2000-8K-MAP0 / LUNA2000 13.8 kWh, so the decoding assertions below double as
a regression test for the register map itself.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.omnibattery.drivers import DriverCapabilities
from custom_components.omnibattery.drivers.huawei import (
    SENSOR_DEFINITIONS,
    HuaweiSolarDriver,
)
from custom_components.omnibattery.infra.huawei_modbus_client import (
    decode_i16,
    decode_i32,
    decode_string,
    decode_u16,
    decode_u32,
)

_BATTERY_DEVICE = "dev-batteries"

# start address -> registers, as captured from the reference installation.
_LIVE_BLOCKS = {
    37000: [2, 0xFFFF, 0xFCD7, 7963, 610],          # running, -809 W, 796.3 V, 61.0 %
    32064: [0, 0] + [0] * 14 + [0, 758],            # PV 0 W, AC 758 W
    47100: [1],                                      # forcible mode = charge
    47246: [0, 0, 1500, 0, 0],                       # target mode TIME, charge 1500 W
    37015: [0, 1492, 0, 1135, 0, 0, 0xFFF6, 354],    # 14.92 / 11.35 kWh, 35.4 °C
    37046: [0, 7000, 0, 7000],
    37066: [6, 0x11B0, 6, 0x0BE7],                   # totals
    47081: [1000, 50, 0, 0, 0, 2, 1],                # cutoffs 100/5 %, mode 2, grid on
    37758: [0, 13800],
    30000: [0x5355, 0x4E32, 0x3030, 0x302D, 0x384B, 0x2D4D, 0x4150, 0x3000]
           + [0] * 7,                                # "SUN2000-8K-MAP0"
    37052: [0x5441, 0x3234, 0x3730, 0x3037, 0x3431, 0x3234, 0, 0, 0, 0],  # TA2470074124
}


def _fake_client(blocks=None):
    table = dict(_LIVE_BLOCKS if blocks is None else blocks)
    client = MagicMock()
    client.connected = True
    client.async_connect = AsyncMock(return_value=True)
    client.async_close = AsyncMock()
    client.set_shutting_down = MagicMock()
    client.async_read_holding_block = AsyncMock(
        side_effect=lambda start, count: table.get(start)
    )
    return client


def _driver(client=None, hass=None, device_id=_BATTERY_DEVICE, **kw):
    return HuaweiSolarDriver(
        hass if hass is not None else MagicMock(),
        "1.2.3.4",
        port=502,
        slave_id=4,
        battery_device_id=device_id,
        client=client if client is not None else _fake_client(),
        **kw,
    )


def _hass_with_services():
    hass = MagicMock()
    hass.services.async_call = AsyncMock()
    return hass


# ----------------------------------------------------------------------
# decoding primitives
# ----------------------------------------------------------------------
def test_decoders_handle_sign_and_width():
    assert decode_u16([0xFFFF]) == 65535
    assert decode_i16([0xFFFF]) == -1
    assert decode_u32([0x0001, 0x0000]) == 65536
    assert decode_i32([0xFFFF, 0xFCD7]) == -809
    assert decode_i32([0x0000, 0x02F6]) == 758


def test_decode_string_stops_at_nul_and_returns_none_when_empty():
    assert decode_string([0x4142, 0x4300, 0x5858], 0, 3) == "ABC"
    assert decode_string([0, 0], 0, 2) is None


# ----------------------------------------------------------------------
# capabilities / identity
# ----------------------------------------------------------------------
def test_capabilities():
    caps = _driver().capabilities
    assert isinstance(caps, DriverCapabilities)
    # The inverter enforces 47081/47082 itself, so SOC limits are not software-only.
    assert caps.hardware_soc_cutoff is True
    assert caps.has_force_mode is True
    assert caps.push_telemetry is False
    assert caps.has_energy_counters is True
    assert caps.has_daily_energy_counters is True
    # The command registers echo before the battery has ramped.
    assert caps.setpoint_confirm_reliable is False
    assert caps.actuator_latency_s == 15.0


def test_power_envelope_is_clamped_to_the_hardware_ceiling():
    caps = _driver(max_charge_power_w=99000, max_discharge_power_w=-5).capabilities
    assert caps.max_charge_power_w == 15000
    assert caps.max_discharge_power_w == 0


@pytest.mark.asyncio
async def test_connect_reads_identity():
    driver = _driver()
    assert await driver.connect() is True
    assert driver.model_label == "SUN2000-8K-MAP0"
    assert driver.serial == "TA2470074124"


@pytest.mark.asyncio
async def test_connect_fails_when_transport_fails():
    client = _fake_client()
    client.async_connect = AsyncMock(return_value=False)
    assert await _driver(client).connect() is False


def test_sensor_definitions_are_unique_and_declare_a_cadence():
    keys = [d["key"] for d in SENSOR_DEFINITIONS]
    assert len(keys) == len(set(keys))
    assert all(d.get("scan_interval") for d in SENSOR_DEFINITIONS)


# ----------------------------------------------------------------------
# telemetry
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_read_telemetry_decodes_the_reference_installation():
    data = await _driver().read_telemetry()
    assert data["battery_soc"] == pytest.approx(61.0)
    # Huawei's sign convention matches Omnibattery's: negative is discharge.
    assert data["battery_power"] == -809
    assert data["battery_voltage"] == pytest.approx(796.3)
    assert data["battery_total_energy"] == 13800
    assert data["max_charge_power"] == 7000
    # Exact, not approx: scaled values must not leak binary-fraction artefacts.
    assert data["internal_temperature"] == 35.4
    assert data["total_daily_charging_energy"] == pytest.approx(14.92)
    assert data["charging_cutoff_capacity"] == pytest.approx(100.0)
    assert data["discharging_cutoff_capacity"] == pytest.approx(5.0)
    assert data["ac_power"] == 758


@pytest.mark.asyncio
async def test_enum_registers_become_labels():
    data = await _driver().read_telemetry(["inverter_state", "user_work_mode"])
    assert data["inverter_state"] == "Running"
    assert data["user_work_mode"] == "Maximise self consumption"


@pytest.mark.asyncio
async def test_unknown_enum_value_is_reported_rather_than_hidden():
    blocks = dict(_LIVE_BLOCKS)
    blocks[37000] = [99, 0, 0, 0, 500]
    data = await _driver(_fake_client(blocks)).read_telemetry(["inverter_state"])
    assert data["inverter_state"] == "Unknown (99)"


@pytest.mark.asyncio
async def test_key_filter_only_reads_the_blocks_it_needs():
    client = _fake_client()
    data = await _driver(client).read_telemetry(["battery_soc"])
    assert set(data) == {"battery_soc"}
    assert client.async_read_holding_block.await_count == 1


@pytest.mark.asyncio
async def test_failed_block_omits_its_keys_instead_of_publishing_zero():
    blocks = dict(_LIVE_BLOCKS)
    del blocks[37000]  # the client returns None for this block
    data = await _driver(_fake_client(blocks)).read_telemetry()
    assert "battery_soc" not in data
    assert "battery_power" not in data
    # An unrelated block is unaffected.
    assert data["battery_total_energy"] == 13800


# ----------------------------------------------------------------------
# control
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_charge_calls_forcible_charge_with_a_duration():
    hass = _hass_with_services()
    result = await _driver(hass=hass).apply_setpoint(1500, read_back=False)
    assert result.ok is True
    assert result.net_power_w == 1500
    domain, service, data = hass.services.async_call.await_args.args[:3]
    assert (domain, service) == ("huawei_solar", "forcible_charge")
    assert data["power"] == 1500
    assert data["device_id"] == _BATTERY_DEVICE
    # The duration is the watchdog: the command must expire on its own.
    assert data["duration"] > 0


@pytest.mark.asyncio
async def test_discharge_sends_a_positive_magnitude():
    hass = _hass_with_services()
    result = await _driver(hass=hass).apply_setpoint(-900, read_back=False)
    assert result.net_power_w == -900
    _domain, service, data = hass.services.async_call.await_args.args[:3]
    assert service == "forcible_discharge"
    assert data["power"] == 900


@pytest.mark.asyncio
async def test_idle_is_a_held_zero_not_a_release():
    """0 W must keep the inverter's own control out of the loop.

    stop_forcible_charge would hand the battery back to self-consumption, which
    is the opposite of idling it.
    """
    hass = _hass_with_services()
    await _driver(hass=hass).apply_setpoint(0, read_back=False)
    _domain, service, data = hass.services.async_call.await_args.args[:3]
    assert service == "forcible_charge"
    assert data["power"] == 0


@pytest.mark.asyncio
async def test_setpoint_is_clamped_to_the_envelope():
    hass = _hass_with_services()
    driver = _driver(hass=hass, max_charge_power_w=5000, max_discharge_power_w=5000)
    result = await driver.apply_setpoint(9999, read_back=False)
    assert result.net_power_w == 5000


@pytest.mark.asyncio
async def test_missing_battery_device_fails_without_calling_a_service():
    hass = _hass_with_services()
    result = await _driver(hass=hass, device_id="").apply_setpoint(1000, read_back=False)
    assert result.ok is False
    assert result.failure_reason == "no_battery_device"
    hass.services.async_call.assert_not_awaited()


@pytest.mark.asyncio
async def test_service_failure_is_reported_and_not_cached_as_written():
    hass = _hass_with_services()
    hass.services.async_call = AsyncMock(side_effect=RuntimeError("boom"))
    driver = _driver(hass=hass)
    result = await driver.apply_setpoint(1000, read_back=False)
    assert result.ok is False
    assert result.failure_reason == "service_call_failed"
    # A failed write must not satisfy the deadband for the next attempt.
    hass.services.async_call = AsyncMock()
    assert (await driver.apply_setpoint(1000, read_back=False)).ok is True
    hass.services.async_call.assert_awaited()


@pytest.mark.asyncio
async def test_readback_reports_the_echo_as_inexact():
    hass = _hass_with_services()
    result = await _driver(hass=hass).apply_setpoint(1500, read_back=True)
    assert result.confirmed is True
    # The registers echo instantly while the battery is still ramping.
    assert result.exact is False
    assert result.battery_power_w == -809


# ----------------------------------------------------------------------
# write throttling
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_small_change_inside_the_deadband_is_not_rewritten():
    hass = _hass_with_services()
    driver = _driver(hass=hass)
    await driver.apply_setpoint(1500, read_back=False)
    hass.services.async_call.reset_mock()
    result = await driver.apply_setpoint(1550, read_back=False)
    # Still reported as delivered — the standing command covers it.
    assert result.ok is True
    assert result.net_power_w == 1550
    hass.services.async_call.assert_not_awaited()


@pytest.mark.asyncio
async def test_direction_change_always_writes_however_small():
    hass = _hass_with_services()
    driver = _driver(hass=hass)
    await driver.apply_setpoint(10, read_back=False)
    hass.services.async_call.reset_mock()
    await driver.apply_setpoint(-10, read_back=False)
    _domain, service, _data = hass.services.async_call.await_args.args[:3]
    assert service == "forcible_discharge"


@pytest.mark.asyncio
async def test_large_change_within_one_direction_waits_for_the_ramp():
    hass = _hass_with_services()
    driver = _driver(hass=hass)
    await driver.apply_setpoint(1000, read_back=False)
    hass.services.async_call.reset_mock()
    # Well beyond the deadband, but the battery is still travelling towards the
    # previous target, so rewriting mid-ramp achieves nothing.
    await driver.apply_setpoint(3000, read_back=False)
    hass.services.async_call.assert_not_awaited()


@pytest.mark.asyncio
async def test_leaving_a_held_zero_is_written_immediately():
    hass = _hass_with_services()
    driver = _driver(hass=hass)
    await driver.apply_setpoint(0, read_back=False)
    hass.services.async_call.reset_mock()
    # Idle -> charging is a change of state, not a change of magnitude.
    await driver.apply_setpoint(3000, read_back=False)
    hass.services.async_call.assert_awaited()


@pytest.mark.asyncio
async def test_write_interval_elapsed_allows_a_material_change():
    hass = _hass_with_services()
    driver = _driver(hass=hass)
    await driver.apply_setpoint(1000, read_back=False)
    driver._last_write_monotonic -= 60.0
    hass.services.async_call.reset_mock()
    await driver.apply_setpoint(3000, read_back=False)
    hass.services.async_call.assert_awaited()


@pytest.mark.asyncio
async def test_standing_command_is_refreshed_before_its_duration_expires():
    hass = _hass_with_services()
    driver = _driver(hass=hass)
    await driver.apply_setpoint(1000, read_back=False)
    driver._last_write_monotonic -= 300.0
    hass.services.async_call.reset_mock()
    # Same value, no direction change: only the refresh timer justifies this.
    await driver.apply_setpoint(1000, read_back=False)
    hass.services.async_call.assert_awaited()


# ----------------------------------------------------------------------
# command echo
# ----------------------------------------------------------------------
@pytest.mark.parametrize(
    "data,expected",
    [
        ({"force_mode": 1, "set_charge_power": 1200, "set_discharge_power": 0}, 1200),
        ({"force_mode": 2, "set_charge_power": 0, "set_discharge_power": 800}, -800),
        ({"force_mode": 0, "set_charge_power": 0, "set_discharge_power": 0}, 0),
        ({"force_mode": 1, "set_charge_power": 0, "set_discharge_power": 0}, 0),
    ],
)
def test_net_power_from_data(data, expected):
    assert _driver().net_power_from_data(data) == expected


def test_net_power_from_data_is_none_when_the_echo_is_incomplete():
    # None must fall through to a real write rather than skipping it.
    assert _driver().net_power_from_data({"force_mode": 1}) is None
    assert _driver().net_power_from_data({}) is None


def test_control_dependency_keys_cover_the_echo():
    keys = _driver().control_dependency_keys
    assert {"force_mode", "set_charge_power", "set_discharge_power"} <= keys


# ----------------------------------------------------------------------
# shutdown and configuration
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_standby_releases_the_battery_back_to_the_inverter():
    hass = _hass_with_services()
    driver = _driver(hass=hass)
    await driver.apply_setpoint(1500, read_back=False)
    assert await driver.standby() is True
    _domain, service, _data = hass.services.async_call.await_args.args[:3]
    assert service == "stop_forcible_charge"
    # The throttle must not suppress the first command after a release.
    hass.services.async_call.reset_mock()
    await driver.apply_setpoint(1500, read_back=False)
    hass.services.async_call.assert_awaited()


@pytest.mark.asyncio
async def test_write_control_reports_unsupported_keys():
    assert await _driver().write_control("force_mode", 1) is False


@pytest.mark.asyncio
async def test_set_charge_cutoff_without_a_resolvable_entity_returns_false():
    driver = _driver(hass=_hass_with_services())
    driver._resolve_entity = lambda name: None
    assert await driver.set_charge_cutoff(90) is False


@pytest.mark.asyncio
async def test_apply_config_writes_both_cutoffs():
    hass = _hass_with_services()
    driver = _driver(hass=hass)
    driver._resolve_entity = lambda name: f"number.{name}"
    assert await driver.apply_config(
        max_soc_pct=95, min_soc_pct=10,
        max_charge_power_w=7000, max_discharge_power_w=7000,
    ) is True
    written = {
        call.args[2]["entity_id"]: call.args[2]["value"]
        for call in hass.services.async_call.await_args_list
    }
    assert written == {
        "number.storage_charging_cutoff_capacity": 95.0,
        "number.storage_discharging_cutoff_capacity": 10.0,
    }

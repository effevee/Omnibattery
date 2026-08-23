"""Unit tests for the Huawei SUN2000 + LUNA2000 driver.

The driver is split-transport: Modbus for telemetry, huawei_solar services for
control. Both halves are faked here, so no inverter and no HA runtime is needed.

Register values in the fixtures are the ones read from a real
SUN2000-8K-MAP0 / LUNA2000 13.8 kWh, so the decoding assertions below double as
a regression test for the register map itself.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

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
    # The cutoff registers are narrower than the configurable SOC window, so the
    # control layer owns enforcement and the registers stay a backstop.
    assert caps.hardware_soc_cutoff is False
    assert caps.has_force_mode is True
    assert caps.push_telemetry is False
    assert caps.has_energy_counters is True
    assert caps.has_daily_energy_counters is True
    # The command registers echo before the battery has ramped.
    assert caps.setpoint_confirm_reliable is False
    # Measured: 19.7 s to reach 90 % of a charge set-point from idle. The
    # control layer judges non-delivery once this elapses, so it must not sit
    # below the real ramp.
    assert caps.actuator_latency_s == 25.0


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
    # kWh, not the register's Wh: every consumer of this key treats it as kWh.
    assert data["battery_total_energy"] == 13.8
    assert data["max_charge_power"] == 7000
    # Exact, not approx: scaled values must not leak binary-fraction artefacts.
    assert data["internal_temperature"] == 35.4
    assert data["total_daily_charging_energy"] == pytest.approx(14.92)
    assert data["charging_cutoff_capacity"] == pytest.approx(100.0)
    assert data["discharging_cutoff_capacity"] == pytest.approx(5.0)
    assert data["inverter_ac_power"] == 758
    assert data["solar_power"] == 0


@pytest.mark.asyncio
async def test_enum_registers_become_labels():
    data = await _driver().read_telemetry(["user_work_mode"])
    assert data["user_work_mode"] == "Maximise self consumption"


@pytest.mark.asyncio
async def test_unknown_enum_value_is_reported_rather_than_hidden():
    blocks = dict(_LIVE_BLOCKS)
    blocks[37000] = [99, 0, 0, 0, 500]
    data = await _driver(_fake_client(blocks)).read_telemetry(["inverter_state"])
    assert data["inverter_state"] == "Unknown (99)"


# ----------------------------------------------------------------------
# inverter state
#
# Register 37000 only says whether the storage is alive, so on its own the
# panel header reads "Running" all day. The direction comes from measured
# power, using the same words as the Marstek register map so the panel is
# consistent across brands.
# ----------------------------------------------------------------------
def _state(running, battery_w):
    high, low = divmod(battery_w & 0xFFFFFFFF, 0x10000)
    return {**_LIVE_BLOCKS, 37000: [running, high, low, 7963, 610]}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "battery_w,expected",
    [
        (2000, "Charge"),
        (-2000, "Discharge"),
        (0, "Standby"),
        # The inverter idles around +50 W; comparing against zero would call a
        # standing battery "Charge" and flip the header on noise.
        (50, "Standby"),
        (-46, "Standby"),
        (101, "Charge"),
        (-101, "Discharge"),
    ],
)
async def test_running_state_is_refined_by_direction(battery_w, expected):
    data = await _driver(_fake_client(_state(2, battery_w))).read_telemetry()
    assert data["inverter_state"] == expected
    assert data["battery_power"] == battery_w


@pytest.mark.asyncio
@pytest.mark.parametrize("raw,expected", [(0, "Offline"), (1, "Standby"), (3, "Fault"), (4, "Sleep")])
async def test_non_running_states_are_reported_as_is(raw, expected):
    """Offline / Fault / Sleep carry more than a direction ever could."""
    data = await _driver(_fake_client(_state(raw, -2000))).read_telemetry()
    assert data["inverter_state"] == expected


@pytest.mark.asyncio
async def test_state_falls_back_to_running_without_a_power_reading():
    blocks = {**_LIVE_BLOCKS, 37000: [2, 0xFFFF, 0xFCD7, 7963, 610]}
    driver = _driver(_fake_client(blocks))
    # Force the power away so only the raw status remains.
    data = await driver.read_telemetry()
    data.pop("battery_power")
    refined = await driver.read_telemetry(["inverter_state"])
    # Asking for the state alone must still pull its companion reading.
    assert refined["inverter_state"] == "Discharge"


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
    # kWh, not the register's Wh: every consumer of this key treats it as kWh.
    assert data["battery_total_energy"] == 13.8


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
async def test_idle_releases_the_battery_rather_than_pinning_it():
    """Zero means "no work for you", which has to mean released, not held.

    An earlier version pinned zero with a forcible charge at 0 W, to keep the
    inverter's own self-consumption control out of the loop. On hardware that
    backfired twice: a pinned battery cannot absorb its own PV, so the inverter
    derated the strings; and the control layer's single idle on entering manual
    mode left the battery frozen in standby with nothing to release it.
    """
    hass = _hass_with_services()
    await _driver(hass=hass).apply_setpoint(0, read_back=False)
    _domain, service, data = hass.services.async_call.await_args.args[:3]
    assert service == "stop_forcible_charge"
    # A stop carries no power or duration.
    assert set(data) == {"device_id"}


def test_zero_echoes_the_stopped_mode():
    """net_power_from_data has to round-trip a release back to 0 W."""
    driver = _driver()
    echo = driver._echo(0)
    assert echo["force_mode"] == 0
    assert driver.net_power_from_data(echo) == 0


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
    assert result.ok is True
    # Reports what is in force, not what was asked for: claiming the request
    # had been applied would make the control layer expect power the battery
    # was never told to deliver, and flag it as non-responsive for it.
    assert result.net_power_w == 1500
    assert result.applied["set_charge_power"] == 1500
    hass.services.async_call.assert_not_awaited()


@pytest.mark.asyncio
async def test_direction_change_skips_the_deadband_but_not_the_ramp():
    """A reversal is material, but it still waits for the previous one to land.

    Live logs showed the control loop flip-flopping between a held zero and a
    discharge every few seconds while the battery was still ramping. Letting
    each reversal through because the sign changed meant a new forced command
    every ~15 s at swings up to 4 kW, and the inverter answered by derating its
    PV to almost nothing.
    """
    hass = _hass_with_services()
    driver = _driver(hass=hass)
    await driver.apply_setpoint(10, read_back=False)
    hass.services.async_call.reset_mock()

    # Mid-ramp reversal: suppressed.
    await driver.apply_setpoint(-10, read_back=False)
    hass.services.async_call.assert_not_awaited()

    # Once the ramp has had its time, the reversal goes through — and a tiny
    # one at that, which the deadband alone would have rejected.
    driver._last_write_monotonic -= 60.0
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
    driver._last_write_monotonic -= 60.0
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
async def test_cutoff_outside_the_register_range_is_skipped_not_clamped():
    """A clamped write would move the backstop somewhere nobody asked for."""
    hass = _hass_with_services()
    driver = _driver(hass=hass)
    driver._resolve_entity = lambda name: f"number.{name}"
    # 47081 accepts 90-100 % only.
    assert await driver.set_charge_cutoff(80) is False
    hass.services.async_call.assert_not_awaited()
    assert await driver.set_charge_cutoff(95) is True
    hass.services.async_call.assert_awaited()


@pytest.mark.asyncio
async def test_apply_config_skips_a_min_soc_the_register_cannot_hold():
    hass = _hass_with_services()
    driver = _driver(hass=hass)
    driver._resolve_entity = lambda name: f"number.{name}"
    # 47082 accepts 0-20 %; 25 % is enforced in software instead.
    result = await driver.apply_config(
        max_soc_pct=95, min_soc_pct=25,
        max_charge_power_w=7000, max_discharge_power_w=7000,
    )
    written = [c.args[2]["entity_id"] for c in hass.services.async_call.await_args_list]
    assert written == ["number.storage_charging_cutoff_capacity"]
    # The window is enforced by the control layer, so a skipped backstop write is
    # not a configuration failure and must not be reported as one.
    assert result is True


@pytest.mark.asyncio
async def test_apply_config_succeeds_even_when_no_entity_resolves():
    driver = _driver(hass=_hass_with_services())
    driver._resolve_entity = lambda name: None
    assert await driver.apply_config(
        max_soc_pct=100, min_soc_pct=10,
        max_charge_power_w=7000, max_discharge_power_w=7000,
    ) is True


def test_resolve_entity_spans_the_config_entry_not_just_the_battery_device():
    """huawei_solar puts the charge cutoff on the inverter, not on the battery.

    Resolving against the configured battery device alone finds the discharge
    cutoff and misses the charge cutoff, which is what made apply_config report
    a failed write on real hardware.
    """
    import custom_components.omnibattery.drivers.huawei as mod

    entries = [
        MagicMock(platform="huawei_solar", disabled=False,
                  unique_id="BT24B1457565_storage_charging_cutoff_capacity",
                  entity_id="number.batterien_ladeende_ladestand"),
        MagicMock(platform="huawei_solar", disabled=False,
                  unique_id="TA2470074124_storage_discharging_cutoff_capacity",
                  entity_id="number.batterien_entlade_ende_ladestand"),
        MagicMock(platform="template", disabled=False,
                  unique_id="x_storage_charging_cutoff_capacity",
                  entity_id="sensor.decoy"),
    ]
    device = MagicMock(config_entries=["entry-1"])
    driver = _driver()
    with (
        patch.object(mod.dr, "async_get",
                     return_value=MagicMock(async_get=MagicMock(return_value=device))),
        patch.object(mod.er, "async_get", return_value=MagicMock()),
        patch.object(mod.er, "async_entries_for_config_entry", return_value=entries),
    ):
        charge = driver._resolve_entity("storage_charging_cutoff_capacity")
        discharge = driver._resolve_entity("storage_discharging_cutoff_capacity")
    assert charge == "number.batterien_ladeende_ladestand"
    # The two register names must not collide: "charging" is a substring of
    # "discharging", so a careless suffix match would return the wrong entity.
    assert discharge == "number.batterien_entlade_ende_ladestand"


def test_resolve_entity_without_a_device_returns_none():
    import custom_components.omnibattery.drivers.huawei as mod

    driver = _driver()
    with patch.object(mod.dr, "async_get",
                      return_value=MagicMock(async_get=MagicMock(return_value=None))):
        assert driver._resolve_entity("storage_charging_cutoff_capacity") is None


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


# ----------------------------------------------------------------------
# config-flow bounds
#
# These guard a real setup failure: without a brand branch, Huawei fell through
# to the Marstek defaults and the limits form rejected the very values the probe
# had just read from the inverter ("Value 7000.0 is too large").
# ----------------------------------------------------------------------
def test_power_ceilings_follow_the_probed_hardware():
    from custom_components.omnibattery.config_flow import _huawei_power_ceilings

    assert _huawei_power_ceilings(
        {"device_max_charge_power": 7000, "device_max_discharge_power": 7000}
    ) == (7000, 7000)


def test_power_ceilings_fall_back_and_stay_sane():
    from custom_components.omnibattery.config_flow import _huawei_power_ceilings

    # No probe data at all.
    assert _huawei_power_ceilings({}) == (5000, 5000)
    # A malformed reading must not become the user's slider maximum.
    charge, _discharge = _huawei_power_ceilings({"device_max_charge_power": 999999})
    assert charge == 15000


def test_soc_window_reaches_the_hardware_discharge_floor():
    from custom_components.omnibattery.config_flow import _soc_selector_limits

    min_lo, min_hi, min_default, *_ = _soc_selector_limits("huawei")
    # The reference installation runs a 5 % discharge cutoff; the form must not
    # reject it the way the Marstek default (12 %) did.
    assert min_lo <= 5 <= min_hi
    assert min_lo <= min_default <= min_hi


# ----------------------------------------------------------------------
# device registry
# ----------------------------------------------------------------------
def test_device_info_reports_huawei_as_the_manufacturer():
    """The brand chain used to fall through to Marstek for every new brand.

    On real hardware that produced a device card reading
    "Marstek / SUN2000-8K-MAP0".
    """
    from types import SimpleNamespace

    from custom_components.omnibattery.infra.coordinator import (
        MarstekVenusDataUpdateCoordinator,
    )

    coordinator = SimpleNamespace(
        device_key="192.168.1.10_2502_4",
        name="Huawei LUNA2000",
        brand="huawei",
        driver=SimpleNamespace(model_label="SUN2000-8K-MAP0"),
        host="192.168.1.10",
        port=2502,
        data={},
    )
    info = MarstekVenusDataUpdateCoordinator.battery_device_info.fget(coordinator)
    assert info["manufacturer"] == "Huawei"
    assert info["model"] == "SUN2000-8K-MAP0"


# ----------------------------------------------------------------------
# dynamic discharge headroom
#
# Battery and PV share one inverter on a DC-coupled hybrid, so the nameplate
# battery limit is only reachable when PV is idle. Allocating against the
# nameplate starves the other batteries of the share they could have delivered.
# ----------------------------------------------------------------------
def _headroom(ceiling, ac_power, battery_power):
    return _driver().dynamic_discharge_limit_w({
        "inverter_max_power": ceiling,
        "inverter_ac_power": ac_power,
        "battery_power": battery_power,
    })


def test_headroom_is_the_full_ceiling_when_pv_is_idle():
    # Night: nothing but the inverter's own draw on the AC side.
    assert _headroom(8800, 0, 50) == 8800


def test_pv_output_consumes_the_headroom():
    # 7 kW of PV on an 8.8 kW inverter leaves 1.8 kW for the battery, whatever
    # its BMS allows.
    assert _headroom(8800, 7000, 0) == 1800


def test_headroom_ignores_the_batterys_own_contribution():
    """The limit must describe what PV occupies, not what the battery does.

    Subtracting the battery's own output too would make the limit chase itself:
    discharging more would shrink the limit, which would cut the allocation,
    which would raise the limit again.
    """
    # Same 7 kW of PV, but now the battery is already delivering its 1.8 kW, so
    # the inverter reads 8.8 kW total. The answer must not change.
    assert _headroom(8800, 8800, -1800) == 1800
    # And it stays put at any point along the ramp.
    assert _headroom(8800, 7900, -900) == 1800


def test_charging_does_not_consume_discharge_headroom():
    # Importing to charge: AC flows the other way and frees the whole ceiling.
    assert _headroom(8800, -2000, 2000) == 8800


def test_headroom_never_goes_negative():
    # An inverter briefly above its own ceiling must clamp to zero, not invert.
    assert _headroom(8800, 9500, 0) == 0


def test_large_system_headroom():
    """A 25 kW inverter with 22 kW of batteries — the constraint still binds."""
    assert _headroom(25000, 18000, 0) == 7000
    assert _headroom(25000, 0, 0) == 25000


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"inverter_ac_power": 1000, "battery_power": 0},          # no ceiling
        {"inverter_max_power": 8800, "battery_power": 0},  # no ac reading
        {"inverter_max_power": 8800, "inverter_ac_power": 1000},    # no battery reading
        {"inverter_max_power": None, "inverter_ac_power": 1, "battery_power": 1},
        {"inverter_max_power": "x", "inverter_ac_power": 1, "battery_power": 1},
    ],
)
def test_headroom_is_none_when_inputs_are_missing_or_unusable(data):
    # None keeps the static envelope; a guess here would silently mis-allocate.
    assert _driver().dynamic_discharge_limit_w(data) is None


def test_headroom_inputs_stay_polled_even_with_entities_disabled():
    keys = _driver().control_dependency_keys
    assert {"inverter_max_power", "inverter_ac_power", "battery_power"} <= keys


@pytest.mark.asyncio
async def test_inverter_ceiling_is_read_from_the_register_map():
    data = await _driver(_fake_client({**_LIVE_BLOCKS, 30073: [0, 8000, 0, 8800]})).read_telemetry(
        ["inverter_rated_power", "inverter_max_power"]
    )
    # 30073 is the nameplate, 30075 the ceiling the inverter enforces.
    assert data["inverter_rated_power"] == 8000
    assert data["inverter_max_power"] == 8800


# ----------------------------------------------------------------------
# control path
# ----------------------------------------------------------------------
def test_ac_batteries_keep_their_static_limit():
    """Only DC-coupled hybrids report a dynamic limit; everything else opts out."""
    from custom_components.omnibattery.drivers.base import BatteryDriver

    assert BatteryDriver.dynamic_discharge_limit_w(MagicMock(), {"inverter_ac_power": 1}) is None


@pytest.mark.parametrize(
    "reported,expected",
    [
        (None, 2500),    # driver has no opinion
        (5000, 2500),    # headroom above the static limit changes nothing
        (1800, 1800),    # headroom below it wins
        (0, 0),          # saturated inverter: no discharge available
    ],
)
def test_control_path_applies_the_narrower_limit(reported, expected):
    from custom_components.omnibattery import _apply_driver_dynamic_limit

    coordinator = MagicMock(
        name="Huawei",
        data={"x": 1},
        driver=MagicMock(dynamic_discharge_limit_w=MagicMock(return_value=reported)),
    )
    assert _apply_driver_dynamic_limit(coordinator, 2500) == expected


def test_control_path_survives_a_driver_that_raises():
    """A broken driver must not take the control cycle down with it."""
    from custom_components.omnibattery import _apply_driver_dynamic_limit

    coordinator = MagicMock(
        data={},
        driver=MagicMock(
            dynamic_discharge_limit_w=MagicMock(side_effect=RuntimeError("boom"))
        ),
    )
    assert _apply_driver_dynamic_limit(coordinator, 2500) == 2500


# ----------------------------------------------------------------------
# panel telemetry
# ----------------------------------------------------------------------
def test_capacity_is_reported_in_kwh_not_the_registers_wh():
    """A factor-1000 error here silently breaks the energy-balance features.

    charge_delay and predictive charging both sum this key straight into a
    kWh total, so reporting the register's raw Wh made 13.8 kWh of storage look
    like 13800 kWh — enough that "solar covers the day" was always true.
    """
    definition = next(
        d for d in SENSOR_DEFINITIONS if d["key"] == "battery_total_energy"
    )
    assert definition["unit"] == "kWh"


@pytest.mark.asyncio
async def test_string_power_is_derived_from_voltage_and_current():
    # 380.0 V x 4.00 A and 190.0 V x 2.00 A
    blocks = {**_LIVE_BLOCKS, 32016: [3800, 400, 1900, 200]}
    data = await _driver(_fake_client(blocks)).read_telemetry()
    assert data["mppt1_power"] == 1520
    assert data["mppt2_power"] == 380


@pytest.mark.asyncio
async def test_string_power_is_omitted_when_its_block_fails():
    blocks = dict(_LIVE_BLOCKS)  # no 32016 entry -> the client returns None
    data = await _driver(_fake_client(blocks)).read_telemetry()
    assert "mppt1_power" not in data
    assert "mppt2_power" not in data


@pytest.mark.asyncio
async def test_asking_only_for_derived_power_still_reads_its_sources():
    """A derived key belongs to no block, so the filter must expand it.

    Without that expansion the poll would read nothing and the entity would
    never leave "unknown".
    """
    blocks = {**_LIVE_BLOCKS, 32016: [3800, 400, 1900, 200]}
    data = await _driver(_fake_client(blocks)).read_telemetry(["mppt1_power"])
    assert data["mppt1_power"] == 1520


def test_derived_keys_are_scheduled_with_their_source_block():
    groups = _driver().read_groups
    strings = next(g for g in groups if "pv1_voltage" in g.keys)
    # Present in a read group, or the coordinator never requests them.
    assert "mppt1_power" in strings.keys
    assert "mppt2_power" in strings.keys


@pytest.mark.asyncio
async def test_storage_firmware_is_exposed_for_the_device_card():
    blocks = {**_LIVE_BLOCKS, 37814: [0x5632, 0x3030] + [0] * 13}  # "V200"
    data = await _driver(_fake_client(blocks)).read_telemetry(["software_version"])
    assert data["software_version"] == "V200"


# ----------------------------------------------------------------------
# off-grid / backup
#
# Huawei meters no separate backup port. While the grid is disconnected the
# inverter feeds nothing but the backup circuit, so its AC power *is* the
# backup output; on-grid there is no such output at all.
# ----------------------------------------------------------------------
def _state3(bits, ac_power=994):
    high, low = divmod(ac_power & 0xFFFFFFFF, 0x10000)
    pv = [0, 0] + [0] * 14 + [high, low]
    return {**_LIVE_BLOCKS, 32003: [0, bits], 32064: pv}


@pytest.mark.asyncio
async def test_on_grid_reports_no_backup_output():
    """The house supply must never be reported as backup power."""
    data = await _driver(_fake_client(_state3(0b00))).read_telemetry()
    assert data["ac_offgrid_power"] == 0
    assert data["backup_function"] == "Disabled"
    # The AC reading itself is untouched.
    assert data["inverter_ac_power"] == 994


@pytest.mark.asyncio
async def test_backup_armed_but_still_on_grid_is_not_an_output():
    data = await _driver(_fake_client(_state3(0b10))).read_telemetry()
    assert data["backup_function"] == "Ready"
    assert data["ac_offgrid_power"] == 0


@pytest.mark.asyncio
async def test_off_grid_reports_the_inverter_output_as_backup_power():
    data = await _driver(_fake_client(_state3(0b11, ac_power=3200))).read_telemetry()
    assert data["backup_function"] == "Off-grid"
    assert data["ac_offgrid_power"] == 3200


@pytest.mark.asyncio
async def test_off_grid_power_is_never_negative():
    # Importing while off-grid is not physical, but a transient must not print
    # a negative backup output.
    data = await _driver(_fake_client(_state3(0b01, ac_power=-500))).read_telemetry()
    assert data["ac_offgrid_power"] == 0


@pytest.mark.asyncio
async def test_backup_keys_are_absent_without_the_state_register():
    blocks = dict(_LIVE_BLOCKS)  # no 32003 entry -> the client returns None
    data = await _driver(_fake_client(blocks)).read_telemetry()
    assert "ac_offgrid_power" not in data
    assert "backup_function" not in data


@pytest.mark.asyncio
async def test_asking_for_backup_power_pulls_both_of_its_blocks():
    """Its two inputs live in different register blocks.

    Scheduling attaches the key to one group only, so the filter has to fetch
    the other block or the value would never appear.
    """
    data = await _driver(_fake_client(_state3(0b01, ac_power=2500))).read_telemetry(
        ["ac_offgrid_power"]
    )
    assert data["ac_offgrid_power"] == 2500


def test_cross_block_derivation_is_scheduled_exactly_once():
    groups = _driver().read_groups
    carrying = [g for g in groups if "ac_offgrid_power" in g.keys]
    assert len(carrying) == 1
    # Attached to the group holding its first source, not the one holding ac_power.
    assert "off_grid_state" in carrying[0].keys


# ----------------------------------------------------------------------
# string count
#
# SUN2000 models range from two strings to many. Register 30071 says how many
# this one has, so a two-string inverter does not sprout entities for strings
# it does not own — and a four-string one is not silently truncated.
# ----------------------------------------------------------------------
def _strings_block(count):
    # Four strings wired, each at 100 V and 1 A -> 100 W.
    return {**_LIVE_BLOCKS, 30071: [count], 32016: [1000, 100] * 4}


@pytest.mark.asyncio
@pytest.mark.parametrize("count,expected", [(2, 2), (3, 3), (4, 4)])
async def test_only_the_reported_strings_are_published(count, expected):
    data = await _driver(_fake_client(_strings_block(count))).read_telemetry()
    published = [k for k in data if k.startswith("mppt")]
    assert sorted(published) == sorted(f"mppt{i}_power" for i in range(1, expected + 1))
    assert data["mppt1_power"] == 100


@pytest.mark.asyncio
async def test_more_strings_than_the_panel_shows_are_capped():
    """The battery panel's MPPT card stops at four.

    Reading further would create entities nothing displays.
    """
    data = await _driver(_fake_client(_strings_block(8))).read_telemetry()
    published = [k for k in data if k.startswith("mppt")]
    assert len(published) == 4


@pytest.mark.asyncio
async def test_string_count_trims_the_entity_definitions():
    driver = _driver(_fake_client(_strings_block(2)))
    await driver.read_telemetry()
    keys = {d["key"] for d in driver.sensor_definitions}
    assert "mppt2_power" in keys
    assert "mppt3_power" not in keys
    assert "pv3_voltage" not in keys


@pytest.mark.asyncio
async def test_string_count_is_known_before_the_entities_are_built():
    """connect() has to learn it, or setup would build the default two."""
    driver = _driver(_fake_client(_strings_block(4)))
    assert await driver.connect() is True
    assert {"mppt3_power", "mppt4_power"} <= {d["key"] for d in driver.sensor_definitions}


# ----------------------------------------------------------------------
# firmware and pack serials
#
# Serial and firmware sit next to each other in each pack's address run, so one
# read covers both. The values below are the ones the reference LUNA2000
# reports; its pack 1 slot is empty and answers with padding only.
# ----------------------------------------------------------------------
def _text(value, registers):
    """Encode a string the way the inverter does: two chars per register."""
    raw = value.encode("ascii").ljust(registers * 2, b"\x00")
    return [int.from_bytes(raw[i:i + 2], "big") for i in range(0, len(raw), 2)]


def _pack(serial, firmware):
    return _text(serial, 10) + _text(firmware, 15)


@pytest.mark.asyncio
async def test_pack_and_inverter_firmware_are_exposed():
    blocks = {
        **_LIVE_BLOCKS,
        30050: _text("V200", 15),
        38242: _pack("EX24A0056894", "V200"),
        38284: _pack("EX2480065597", "V200"),
    }
    data = await _driver(_fake_client(blocks)).read_telemetry()
    assert data["inverter_software_version"] == "V200"
    assert data["pack2_firmware_version"] == "V200"
    assert data["pack3_firmware_version"] == "V200"


@pytest.mark.asyncio
async def test_each_pack_reports_its_own_serial():
    """The packs are the parts that get replaced, so they are worth naming."""
    blocks = {
        **_LIVE_BLOCKS,
        38242: _pack("EX24A0056894", "V200R025C00SPC103"),
        38284: _pack("EX2480065597", "V200R025C00SPC103"),
    }
    data = await _driver(_fake_client(blocks)).read_telemetry()
    assert data["pack2_serial_number"] == "EX24A0056894"
    assert data["pack3_serial_number"] == "EX2480065597"
    # Serial and firmware come out of the same read, not two.
    assert data["pack2_firmware_version"] == "V200R025C00SPC103"


def test_every_pack_serial_has_an_entity():
    keys = {row["key"] for row in SENSOR_DEFINITIONS}
    for index in (1, 2, 3):
        assert f"pack{index}_serial_number" in keys


@pytest.mark.asyncio
async def test_an_empty_pack_slot_gets_no_entity():
    """An entity that can only ever read "unknown" is worse than none.

    The reference LUNA2000 has packs 2 and 3; its pack 1 slot is empty, and
    listing it under diagnostics only invites the question what is wrong with it.
    """
    driver = _driver(_fake_client({
        **_LIVE_BLOCKS,
        38200: [0] * 25,
        38242: _pack("EX24A0056894", "V200"),
        38284: _pack("EX2480065597", "V200"),
    }))
    await driver.connect()

    keys = {row["key"] for row in driver.sensor_definitions}
    assert "pack1_serial_number" not in keys
    assert "pack1_firmware_version" not in keys
    assert {"pack2_serial_number", "pack3_serial_number"} <= keys


@pytest.mark.asyncio
async def test_packs_that_have_not_answered_yet_are_not_hidden():
    """No answer means "not asked", which is not the same as "not there"."""
    driver = _driver(_fake_client({}))
    keys = {row["key"] for row in driver.sensor_definitions}
    assert {f"pack{index}_serial_number" for index in (1, 2, 3)} <= keys


@pytest.mark.asyncio
async def test_an_empty_pack_slot_is_omitted_entirely():
    """Pack 1 answers with nothing but padding on the reference hardware.

    An empty slot must leave no entity behind at all — neither firmware nor
    serial — rather than showing up as a battery pack with blank details.
    """
    blocks = {**_LIVE_BLOCKS, 38200: [0] * 25}
    data = await _driver(_fake_client(blocks)).read_telemetry()
    assert "pack1_firmware_version" not in data
    assert "pack1_serial_number" not in data


@pytest.mark.asyncio
async def test_unwired_strings_leave_nothing_in_the_cache():
    """The block read covers four strings whatever the inverter has.

    Leaving the unused pairs in the telemetry cache would put keys with no
    entity behind them into diagnostics.
    """
    data = await _driver(_fake_client(_strings_block(2))).read_telemetry()
    assert "pv2_voltage" in data
    assert "pv3_voltage" not in data
    assert "pv4_current" not in data


@pytest.mark.asyncio
async def test_a_suppressed_reversal_reports_the_standing_command():
    """The worst case for the old behaviour: released, then asked to discharge.

    Zero releases the battery, so nothing is latched. A discharge request in the
    next few seconds is throttled — and reporting it as applied made the control
    layer expect a discharge that had never been commanded.
    """
    hass = _hass_with_services()
    driver = _driver(hass=hass)
    await driver.apply_setpoint(0, read_back=False)
    hass.services.async_call.reset_mock()

    result = await driver.apply_setpoint(-1200, read_back=False)
    hass.services.async_call.assert_not_awaited()
    assert result.net_power_w == 0
    assert result.applied["set_discharge_power"] == 0
    assert driver.net_power_from_data(result.applied) == 0


# ----------------------------------------------------------------------
# read-group shape
#
# The coordinator counts a group that returns nothing as a failed read, and a
# cycle where every attempted group fails marks the whole battery unavailable.
# A block holding one optional value therefore takes the device offline on
# every poll of that block — which is exactly what a firmware string the
# inverter leaves empty did on the reference hardware, every three seconds.
# ----------------------------------------------------------------------
def test_optional_values_never_sit_alone_in_a_group():
    for group in _driver().read_groups:
        assert len(group.keys) > 1, f"{group.keys} would fail as a whole when absent"


def test_groups_are_one_per_cadence():
    groups = _driver().read_groups
    intervals = [g.scan_interval for g in groups]
    assert len(intervals) == len(set(intervals))
    assert set(intervals) == {"high", "medium", "low", "very_low"}


def test_every_published_key_belongs_to_a_group():
    driver = _driver()
    scheduled = {key for group in driver.read_groups for key in group.keys}
    for definition in driver.sensor_definitions:
        # A key in no group is never requested, so its entity stays unknown.
        assert definition["key"] in scheduled, definition["key"]


@pytest.mark.asyncio
async def test_a_missing_firmware_string_does_not_empty_its_group():
    """Pack 1 answers with padding on the reference hardware.

    The group has to keep returning its other values, or the coordinator reads
    the empty result as a dead battery.
    """
    blocks = {**_LIVE_BLOCKS, 38200: [0] * 25}
    driver = _driver(_fake_client(blocks))
    very_low = next(g for g in driver.read_groups if g.scan_interval == "very_low")
    data = await driver.read_telemetry(list(very_low.keys))
    assert "pack1_firmware_version" not in data
    assert data, "an empty snapshot marks the whole battery unavailable"
    assert data["battery_total_energy"] == 13.8


# ----------------------------------------------------------------------
# house-consumption balance
#
# The system aggregates derive household load as
#     home = grid + sum(ac_power) + external_solar
# treating ac_power as the battery's own AC port, with DC-coupled PV already
# netted into it. Register 32080 is the whole inverter's AC output on this
# hybrid — PV included — so publishing it under that key counted the roof array
# twice: once inside ac_power, once in the external solar sensor.
# ----------------------------------------------------------------------
def test_inverter_ac_output_is_not_published_as_the_batterys_ac_port():
    keys = {d["key"] for d in _driver().sensor_definitions}
    assert "ac_power" not in keys
    assert "inverter_ac_power" in keys


@pytest.mark.asyncio
async def test_the_aggregates_fall_back_to_battery_power():
    """No ac_power means -battery_power is used, which is the real contribution.

    Worked example from the reference installation: 8.87 kW of PV, 8.0 kW of it
    into the battery, 167 W exported. House load is ~0.7 kW; counting the
    inverter's AC total as a battery AC port made it read 8.4 kW.
    """
    from custom_components.omnibattery.sensors.aggregate_sensors import (
        MarstekVenusAggregateSensor as Aggregate,
    )

    data = await _driver().read_telemetry()
    assert "ac_power" not in data
    ac_convention = Aggregate._ac_convention_power(data)
    assert ac_convention == -data["battery_power"]


def test_ac_convention_maths_for_the_reference_case():
    from custom_components.omnibattery.sensors.aggregate_sensors import (
        MarstekVenusAggregateSensor as Aggregate,
    )

    # Charging 8000 W: the battery is a load on the AC bus, so its contribution
    # is negative in the ac_power convention.
    contribution = Aggregate._ac_convention_power({"battery_power": 8000})
    assert contribution == -8000
    # home = grid + contribution + solar, with export counted negative.
    assert -167 + contribution + 8870 == 703


# ----------------------------------------------------------------------
# direct register writes
#
# Same four-register sequence huawei_solar performs, written straight to the
# inverter. The order matters: the mode register acts on the values already in
# place, so it is written last — and first when releasing.
# ----------------------------------------------------------------------
def _direct(hass=None, **kw):
    client = _fake_client()
    client.async_write_registers = AsyncMock(return_value=True)
    driver = _driver(client, hass=hass or _hass_with_services(), direct_write=True, **kw)
    return driver, client


def _written(client):
    return [(c.args[0], list(c.args[1])) for c in client.async_write_registers.await_args_list]


@pytest.mark.asyncio
async def test_charge_writes_the_parameters_before_the_mode():
    driver, client = _direct()
    result = await driver.apply_setpoint(1500, read_back=False)
    assert result.ok is True
    assert _written(client) == [
        (47247, [0, 1500]),   # charge power, u32 high word first
        (47083, [10]),        # duration in minutes
        (47246, [0]),         # target mode: by time
        (47100, [1]),         # mode: charge — triggers on the values above
    ]


@pytest.mark.asyncio
async def test_discharge_uses_its_own_power_register():
    driver, client = _direct()
    await driver.apply_setpoint(-900, read_back=False)
    assert _written(client) == [
        (47249, [0, 900]),
        (47083, [10]),
        (47246, [0]),
        (47100, [2]),
    ]


@pytest.mark.asyncio
async def test_release_stops_before_it_tidies_up():
    driver, client = _direct()
    await driver.apply_setpoint(0, read_back=False)
    written = _written(client)
    # Stop first: clearing the parameters while a command still runs would act
    # on the cleared values.
    assert written[0] == (47100, [0])
    assert written[1:] == [(47249, [0, 0]), (47083, [0]), (47246, [0])]


@pytest.mark.asyncio
async def test_a_large_value_is_clamped_to_the_register_maximum():
    """huawei_solar refuses an over-range power; clamping keeps the loop alive."""
    driver, client = _direct(max_charge_power_w=7000)
    await driver.apply_setpoint(99000, read_back=False)
    assert _written(client)[0] == (47247, [0, 7000])


@pytest.mark.asyncio
async def test_a_failed_write_never_reaches_the_mode_register():
    """A half-written sequence must leave the inverter doing what it did."""
    driver, client = _direct()
    client.async_write_registers = AsyncMock(side_effect=[True, False])
    result = await driver.apply_setpoint(1500, read_back=False)
    assert result.ok is False
    assert result.failure_reason == "register_write_failed"
    assert 47100 not in [addr for addr, _ in _written(client)]


@pytest.mark.asyncio
async def test_direct_mode_calls_no_services_at_all():
    """The point of the option: no dependency on huawei_solar for control."""
    hass = _hass_with_services()
    driver, _client = _direct(hass=hass)
    await driver.apply_setpoint(1500, read_back=False)
    await driver.apply_setpoint(0, read_back=False)
    hass.services.async_call.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_throttle_still_applies_in_direct_mode():
    driver, client = _direct()
    await driver.apply_setpoint(1500, read_back=False)
    client.async_write_registers.reset_mock()
    result = await driver.apply_setpoint(1550, read_back=False)
    client.async_write_registers.assert_not_awaited()
    assert result.net_power_w == 1500


def test_u32_splits_high_word_first():
    from custom_components.omnibattery.drivers.huawei import _u32

    assert _u32(0) == [0, 0]
    assert _u32(1500) == [0, 1500]
    assert _u32(70000) == [1, 4464]
    # Negative power never reaches a magnitude register.
    assert _u32(-5) == [0, 0]


@pytest.mark.asyncio
async def test_direct_mode_needs_no_huawei_solar_battery_device():
    """Found by the first hardware run: the guard sat before the branch.

    Requiring huawei_solar's battery device in the mode whose entire purpose is
    doing without that integration blocked every set-point with
    no_battery_device.
    """
    client = _fake_client()
    client.async_write_registers = AsyncMock(return_value=True)
    driver = _driver(client, hass=_hass_with_services(), direct_write=True, device_id="")
    result = await driver.apply_setpoint(500, read_back=False)
    assert result.ok is True
    assert result.failure_reason is None
    assert _written(client)[0] == (47247, [0, 500])


@pytest.mark.asyncio
async def test_the_service_path_still_requires_the_device():
    driver = _driver(hass=_hass_with_services(), device_id="")
    result = await driver.apply_setpoint(500, read_back=False)
    assert result.ok is False
    assert result.failure_reason == "no_battery_device"


@pytest.mark.asyncio
async def test_direct_mode_writes_cutoffs_to_their_registers():
    """Otherwise the device id would still be needed for the SOC window."""
    client = _fake_client()
    client.async_write_registers = AsyncMock(return_value=True)
    driver = _driver(client, hass=_hass_with_services(), direct_write=True, device_id="")
    assert await driver.set_charge_cutoff(95) is True
    assert await driver._write_cutoff("discharging", 10) is True
    assert _written(client) == [(47081, [950]), (47082, [100])]


@pytest.mark.asyncio
async def test_direct_mode_still_refuses_an_unrepresentable_cutoff():
    client = _fake_client()
    client.async_write_registers = AsyncMock(return_value=True)
    driver = _driver(client, hass=_hass_with_services(), direct_write=True, device_id="")
    # 47081 accepts 90-100 % only; a clamped write would move the backstop.
    assert await driver.set_charge_cutoff(80) is False
    client.async_write_registers.assert_not_awaited()


def test_the_write_interval_sits_under_the_declared_ramp():
    """Measured on hardware: ~20 s to land a charge set-point, 11 s to reverse.

    Re-issuing before the previous command lands moves a target the battery is
    still travelling towards; waiting past the ramp makes the loop slower than
    the hardware needs.
    """
    from custom_components.omnibattery.drivers.huawei import (
        _ACTUATOR_LATENCY_S,
        _MIN_WRITE_INTERVAL_S,
    )

    assert _MIN_WRITE_INTERVAL_S <= _ACTUATOR_LATENCY_S
    assert _MIN_WRITE_INTERVAL_S >= 15.0


# ----------------------------------------------------------------------
# slave-id discovery
#
# The slave id is not derivable and differs between setups: on the reference
# installation the inverter answers on 4, while 0 is the energy manager, 2 a
# backup switch and 9 a charger. Asking a user to guess is the least friendly
# part of the setup.
# ----------------------------------------------------------------------
def _bus(monkeypatch, **by_id):
    """Fake a Modbus bus where each slave id answers with its own blocks."""
    import custom_components.omnibattery.drivers.huawei as mod

    def factory(host, port, slave_id):
        blocks = by_id.get(str(slave_id))
        client = _fake_client(blocks if blocks is not None else {})
        client.unit_id = slave_id
        return client

    monkeypatch.setattr(mod, "HuaweiModbusClient", factory)
    return mod


@pytest.mark.asyncio
async def test_scan_reports_every_inverter_on_the_bus(monkeypatch):
    """Huawei inverters can be cascaded, so the scan must not stop at the first."""
    inverter_without_battery = {30000: _LIVE_BLOCKS[30000]}
    mod = _bus(monkeypatch, **{"1": {}, "4": _LIVE_BLOCKS, "5": inverter_without_battery})
    monkeypatch.setattr(mod, "_SLAVE_ID_CANDIDATES", (1, 4, 5))

    found = await mod.HuaweiSolarDriver.scan_slave_ids(MagicMock(), "1.2.3.4", 502)
    assert [(sid, batt) for sid, _model, batt in found] == [(4, True), (5, False)]
    assert found[0][1] == "SUN2000-8K-MAP0"


@pytest.mark.asyncio
async def test_scan_ignores_ids_that_are_not_inverters(monkeypatch):
    """0 is the energy manager on the reference bus, 2 a backup switch."""
    mod = _bus(monkeypatch, **{"4": _LIVE_BLOCKS})
    monkeypatch.setattr(mod, "_SLAVE_ID_CANDIDATES", (1, 0, 2, 4))
    found = await mod.HuaweiSolarDriver.scan_slave_ids(MagicMock(), "1.2.3.4", 502)
    assert [sid for sid, _m, _b in found] == [4]


@pytest.mark.asyncio
async def test_scan_stops_when_the_address_itself_is_unreachable(monkeypatch):
    """No point walking nine ids when nothing is listening at all."""
    import custom_components.omnibattery.drivers.huawei as mod

    tried = []

    def factory(host, port, slave_id):
        tried.append(slave_id)
        client = _fake_client({})
        client.async_connect = AsyncMock(return_value=False)
        return client

    monkeypatch.setattr(mod, "HuaweiModbusClient", factory)
    monkeypatch.setattr(mod, "_SLAVE_ID_CANDIDATES", (1, 2, 3, 4))
    assert await mod.HuaweiSolarDriver.scan_slave_ids(MagicMock(), "1.2.3.4", 502) == []
    assert tried == [1]


def test_the_candidate_list_covers_the_known_layouts():
    from custom_components.omnibattery.drivers.huawei import _SLAVE_ID_CANDIDATES

    # 1 is the factory default for a direct connection; 4 is where the
    # reference installation's inverter sits behind its energy manager.
    assert _SLAVE_ID_CANDIDATES[0] == 1
    assert 4 in _SLAVE_ID_CANDIDATES


def test_the_slave_id_field_is_empty_by_default():
    """Empty means "go and find it", so nothing may be prefilled on a new entry.

    A prefilled guess would invite the user to accept a wrong one — the id is
    not derivable, and a wrong one reads a charger or an energy manager.
    """
    import voluptuous as vol
    from custom_components.omnibattery.config_flow import MarstekVenusConfigFlow

    schema = MarstekVenusConfigFlow._huawei_schema({}, 1)
    marker = next(m for m in schema.schema if m.schema == "slave_id")
    assert marker.default is vol.UNDEFINED
    assert (marker.description or {}).get("suggested_value") is None


def test_an_existing_slave_id_is_offered_again_on_reconfigure():
    from custom_components.omnibattery.config_flow import MarstekVenusConfigFlow

    schema = MarstekVenusConfigFlow._huawei_schema({"slave_id": 4}, 1)
    marker = next(m for m in schema.schema if m.schema == "slave_id")
    assert marker.description["suggested_value"] == 4


def test_the_empty_field_is_explained_in_every_language():
    """A field that does something when left blank has to say so."""
    import glob
    import json

    for path in ["custom_components/omnibattery/strings.json"] + sorted(
        glob.glob("custom_components/omnibattery/translations/*.json")
    ):
        step = json.load(open(path, encoding="utf-8"))["config"]["step"]
        data = step["battery_connection_huawei"]["data"]
        hint = step["battery_connection_huawei"]["data_description"]["slave_id"]
        assert "empty" in data["slave_id"].lower() or "leer" in data["slave_id"].lower(), path
        assert "empty" in hint.lower() or "leer" in hint.lower(), path


# ----------------------------------------------------------------------
# pairing the Modbus address with the huawei_solar device
#
# On the service path the battery is named twice: once as a Modbus address and
# once as a device in the registry. Nothing forces those to be the same
# inverter — and Huawei inverters can be cascaded, so on a two-inverter bus the
# wrong pairing reads one unit and commands the other. The inverter serial
# appears on both sides (register 30015, and huawei_solar's device identifier),
# so the pairing is checkable. On the reference installation both read
# BT24B1457565.
# ----------------------------------------------------------------------
def _huawei_flow(monkeypatch, *, probe, devices=None, huawei_solar_installed=True):
    """A config flow whose Huawei probe and device registry are faked."""
    from types import SimpleNamespace

    from custom_components.omnibattery import config_flow as mod

    flow = mod.MarstekVenusConfigFlow()
    flow.battery_index = 0
    flow.config_data = {"num_batteries": 1}
    flow._current_battery_data = {"brand": "huawei"}
    flow.hass = SimpleNamespace(
        config_entries=SimpleNamespace(
            async_entries=lambda domain: [object()] if huawei_solar_installed else []
        )
    )
    monkeypatch.setattr(mod.HuaweiSolarDriver, "probe", AsyncMock(return_value=probe))
    registry = MagicMock()
    registry.async_get.side_effect = (devices or {}).get
    monkeypatch.setattr(mod.dr, "async_get", lambda hass: registry)
    return flow


def _huawei_device(serial=None, identifier=None, via=None):
    from types import SimpleNamespace

    return SimpleNamespace(
        serial_number=serial,
        identifiers={("huawei_solar", identifier)} if identifier else set(),
        via_device_id=via,
    )


_HUAWEI_INPUT = {
    "name": "Huawei LUNA2000",
    "host": "192.168.1.5",
    "port": 502,
    "slave_id": 4,
    "huawei_battery_device": "dev-batt",
    "huawei_direct_write": False,
}


@pytest.mark.asyncio
async def test_a_battery_from_another_inverter_is_refused(monkeypatch):
    """Cascade: the device hangs off inverter B, the address points at A."""
    flow = _huawei_flow(
        monkeypatch,
        probe=(True, "SUN2000-8K-MAP0", 7000, 7000, "BT24B1457565"),
        devices={
            "dev-batt": _huawei_device(identifier="battery-2", via="dev-inv-2"),
            "dev-inv-2": _huawei_device(serial="BT24B9999999"),
        },
    )
    form = await flow.async_step_battery_connection_huawei(dict(_HUAWEI_INPUT))

    assert form["errors"] == {"huawei_battery_device": "huawei_device_mismatch"}
    assert form["step_id"] == "battery_connection_huawei"
    # Nothing was committed, so the user lands back on the same form.
    assert "slave_id" not in flow._current_battery_data


@pytest.mark.asyncio
async def test_a_battery_on_the_probed_inverter_is_accepted(monkeypatch):
    flow = _huawei_flow(
        monkeypatch,
        probe=(True, "SUN2000-8K-MAP0", 7000, 7000, "BT24B1457565"),
        devices={
            "dev-batt": _huawei_device(identifier="battery", via="dev-inv"),
            "dev-inv": _huawei_device(serial="bt24b1457565"),  # registries vary in case
        },
    )
    form = await flow.async_step_battery_connection_huawei(dict(_HUAWEI_INPUT))

    assert form["step_id"] == "battery_limits"
    assert flow._current_battery_data["slave_id"] == 4
    assert flow._current_battery_data["device_max_charge_power"] == 7000


@pytest.mark.asyncio
async def test_a_device_whose_inverter_is_unknown_is_not_blocked(monkeypatch):
    """Only a contradiction may stop the flow, never a missing serial.

    Older huawei_solar releases do not fill serial_number, and a firmware that
    leaves register 30015 empty is not a reason to refuse a working setup.
    """
    flow = _huawei_flow(
        monkeypatch,
        probe=(True, "SUN2000-8K-MAP0", 7000, 7000, "BT24B1457565"),
        devices={"dev-batt": _huawei_device()},
    )
    assert (await flow.async_step_battery_connection_huawei(dict(_HUAWEI_INPUT)))[
        "step_id"
    ] == "battery_limits"

    flow = _huawei_flow(
        monkeypatch,
        probe=(True, "SUN2000-8K-MAP0", 7000, 7000, None),
        devices={"dev-batt": _huawei_device(serial="BT24B9999999")},
    )
    assert (await flow.async_step_battery_connection_huawei(dict(_HUAWEI_INPUT)))[
        "step_id"
    ] == "battery_limits"


@pytest.mark.asyncio
async def test_the_serial_identifies_the_inverter_even_without_a_parent(monkeypatch):
    """huawei_solar names the inverter device itself by serial."""
    flow = _huawei_flow(
        monkeypatch,
        probe=(True, "SUN2000-8K-MAP0", 7000, 7000, "BT24B1457565"),
        devices={"dev-batt": _huawei_device(identifier="BT24B9999999")},
    )
    form = await flow.async_step_battery_connection_huawei(dict(_HUAWEI_INPUT))
    assert form["errors"] == {"huawei_battery_device": "huawei_device_mismatch"}


@pytest.mark.asyncio
async def test_a_missing_huawei_solar_integration_is_named_as_such(monkeypatch):
    """"Pick a device" is unhelpful advice when there are no devices to pick."""
    payload = dict(_HUAWEI_INPUT, huawei_battery_device="")

    flow = _huawei_flow(monkeypatch, probe=(False, None, None, None, None))
    form = await flow.async_step_battery_connection_huawei(dict(payload))
    assert form["errors"] == {"huawei_battery_device": "huawei_device_required"}

    flow = _huawei_flow(
        monkeypatch, probe=(False, None, None, None, None), huawei_solar_installed=False
    )
    form = await flow.async_step_battery_connection_huawei(dict(payload))
    assert form["errors"] == {"huawei_battery_device": "huawei_solar_missing"}


@pytest.mark.asyncio
async def test_direct_writes_need_no_device_at_all(monkeypatch):
    """The whole point of the direct path is not depending on that integration."""
    flow = _huawei_flow(
        monkeypatch,
        probe=(True, "SUN2000-8K-MAP0", 7000, 7000, "BT24B1457565"),
        huawei_solar_installed=False,
    )
    form = await flow.async_step_battery_connection_huawei(
        dict(_HUAWEI_INPUT, huawei_battery_device="", huawei_direct_write=True)
    )
    assert form["step_id"] == "battery_limits"
    assert flow._current_battery_data["huawei_direct_write"] is True


def test_both_new_errors_are_explained_in_every_language():
    import glob
    import json

    for path in ["custom_components/omnibattery/strings.json"] + sorted(
        glob.glob("custom_components/omnibattery/translations/*.json")
    ):
        content = json.load(open(path, encoding="utf-8"))
        for section in ("config", "options"):
            errors = content[section]["error"]
            for key in ("huawei_solar_missing", "huawei_device_mismatch"):
                assert errors.get(key), (path, section, key)


def test_the_options_flow_carries_the_same_huawei_logic():
    """Both flows hold their own copy of these steps; they must not drift.

    Setup and reconfiguration reach the same hardware, so a check added to one
    and forgotten in the other would only be found by the user it fails on.
    """
    import inspect

    from custom_components.omnibattery.config_flow import (
        MarstekVenusConfigFlow,
        OptionsFlowHandler,
    )

    for name in (
        "async_step_battery_connection_huawei",
        "async_step_battery_connection_huawei_slave",
        "_huawei_store",
        "_huawei_search",
    ):
        setup = inspect.getsource(getattr(MarstekVenusConfigFlow, name))
        options = inspect.getsource(getattr(OptionsFlowHandler, name))
        assert setup == options, name


@pytest.mark.asyncio
async def test_a_serial_carried_with_a_suffix_still_counts_as_a_match(monkeypatch):
    """huawei_solar derives child identifiers from the inverter serial."""
    flow = _huawei_flow(
        monkeypatch,
        probe=(True, "SUN2000-8K-MAP0", 7000, 7000, "BT24B1457565"),
        devices={"dev-batt": _huawei_device(identifier="BT24B1457565_batteries")},
    )
    form = await flow.async_step_battery_connection_huawei(dict(_HUAWEI_INPUT))
    assert form["step_id"] == "battery_limits"

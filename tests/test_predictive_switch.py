"""Unit tests for ``PredictiveChargingSwitch`` (#68).

The switch is the dashboard enable toggle for predictive grid charging. It must:
  * be created whenever predictive charging is configured, not only while
    currently enabled (otherwise the sliders show with no toggle),
  * move the ``enabled`` and ``overridden`` flags together so every consumer
    stays consistent regardless of which flag it reads, and
  * reload the entry when the ``enabled`` value flips, so the setup-time gating
    (consumption-capture / dynamic-pricing schedules, status sensor) re-arms.

Exercised without the full Home Assistant runtime: entities are built on stub
hass/entry/controller objects and ``async_write_ha_state`` is neutralised.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from custom_components.omnibattery.const import (
    CONF_ENABLE_PREDICTIVE_CHARGING,
    CONF_PREDICTIVE_CHARGING_OVERRIDDEN,
    DOMAIN,
)
from custom_components.omnibattery.switch import (
    PredictiveChargingSwitch,
    async_setup_entry,
)


def _make_switch(*, enabled, overridden, entry_data=None):
    controller = SimpleNamespace(
        predictive_charging_enabled=enabled,
        predictive_charging_overridden=overridden,
        grid_charging_active=False,
    )
    entry = SimpleNamespace(entry_id="test-entry", data=dict(entry_data or {}))
    reloads: list[str] = []

    async def _async_call(*_a, **_k):
        return None

    async def _async_reload(entry_id):
        reloads.append(entry_id)

    def _update_entry(target, *, data):
        target.data = data

    hass = SimpleNamespace(
        config_entries=SimpleNamespace(
            async_update_entry=_update_entry,
            async_reload=_async_reload,
        ),
        services=SimpleNamespace(async_call=_async_call),
    )
    sw = PredictiveChargingSwitch(hass, entry, controller)
    sw.async_write_ha_state = lambda: None  # not registered with HA
    return sw, controller, entry, reloads


def test_is_on_requires_enabled_and_not_overridden():
    assert _make_switch(enabled=True, overridden=False)[0].is_on is True
    # Enabled but paused (legacy override state) reads OFF, matching the pricing
    # engine which pauses on ``overridden``.
    assert _make_switch(enabled=True, overridden=True)[0].is_on is False
    # Configured-but-disabled (issue #68 reporter's state) reads OFF.
    assert _make_switch(enabled=False, overridden=False)[0].is_on is False


def test_turn_on_from_disabled_enables_and_reloads():
    sw, controller, entry, reloads = _make_switch(enabled=False, overridden=True)
    asyncio.run(sw.async_turn_on())
    assert controller.predictive_charging_enabled is True
    assert controller.predictive_charging_overridden is False
    assert entry.data[CONF_ENABLE_PREDICTIVE_CHARGING] is True
    assert entry.data[CONF_PREDICTIVE_CHARGING_OVERRIDDEN] is False
    # Enabling flips the value → the entry reloads so the setup-time schedules and
    # status sensor re-arm.
    assert reloads == ["test-entry"]


def test_turn_off_from_enabled_disables_and_reloads():
    sw, controller, entry, reloads = _make_switch(enabled=True, overridden=False)
    asyncio.run(sw.async_turn_off())
    assert controller.predictive_charging_enabled is False
    assert controller.predictive_charging_overridden is True
    assert entry.data[CONF_ENABLE_PREDICTIVE_CHARGING] is False
    assert entry.data[CONF_PREDICTIVE_CHARGING_OVERRIDDEN] is True
    assert reloads == ["test-entry"]


def test_resume_from_legacy_pause_does_not_reload():
    # Legacy paused entry: enabled stayed True, only overridden was set. Turning
    # the switch back on clears the override without flipping enabled, so no
    # reload is needed (the schedules were already armed at setup).
    sw, controller, entry, reloads = _make_switch(enabled=True, overridden=True)
    asyncio.run(sw.async_turn_on())
    assert controller.predictive_charging_overridden is False
    assert reloads == []


def test_toggle_preserves_other_entry_data():
    sw, _controller, entry, _reloads = _make_switch(
        enabled=True, overridden=False, entry_data={"unrelated": 42}
    )
    asyncio.run(sw.async_turn_off())
    assert entry.data["unrelated"] == 42


def test_switch_created_when_configured_but_disabled():
    """#68: the enable toggle must be built even when predictive charging is
    currently disabled, as long as it has been through config (key present)."""
    controller = SimpleNamespace(weekly_full_charge_enabled=False)
    entry = SimpleNamespace(
        entry_id="test-entry",
        data={CONF_ENABLE_PREDICTIVE_CHARGING: False},
    )
    hass = SimpleNamespace(
        data={DOMAIN: {"test-entry": {"coordinators": [], "controller": controller}}}
    )
    added: list = []
    asyncio.run(async_setup_entry(hass, entry, lambda ents: added.extend(ents)))
    assert any(isinstance(e, PredictiveChargingSwitch) for e in added)


def test_switch_absent_when_never_configured():
    """No predictive config key at all (legacy/undeclared) → no switch."""
    controller = SimpleNamespace(weekly_full_charge_enabled=False)
    entry = SimpleNamespace(entry_id="test-entry", data={})
    hass = SimpleNamespace(
        data={DOMAIN: {"test-entry": {"coordinators": [], "controller": controller}}}
    )
    added: list = []
    asyncio.run(async_setup_entry(hass, entry, lambda ents: added.extend(ents)))
    assert not any(isinstance(e, PredictiveChargingSwitch) for e in added)

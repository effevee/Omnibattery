"""Static contracts for Daily Operation rendering decisions."""

from pathlib import Path


PANEL = Path("custom_components/omnibattery/frontend/marstek-panel.js")


def test_delayed_intervals_are_not_rendered_as_available_solar_windows():
    panel = PANEL.read_text(encoding="utf-8")

    assert "const solarOpportunity = !snapshot.isSkipped[index]" in panel
    assert "const solarWindow = solarOpportunity && !delay;" in panel


def test_disabled_charge_delay_suppresses_stale_clock_markers():
    panel = PANEL.read_text(encoding="utf-8")

    assert "const delayEnabled = !snapshot.delayInfo" in panel
    assert "const delay = delayEnabled && !weeklyDelayBypassed" in panel

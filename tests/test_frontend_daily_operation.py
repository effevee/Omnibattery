"""Static contracts for Daily Operation rendering decisions."""

from pathlib import Path


PANEL = Path("custom_components/omnibattery/frontend/marstek-panel.js")


def test_delayed_solar_opportunity_keeps_yellow_window_and_clock_context():
    panel = PANEL.read_text(encoding="utf-8")

    assert "const solarOpportunity = !snapshot.isSkipped[index]" in panel
    assert "const solarWindow = solarOpportunity;" in panel
    assert "cell.classList.toggle(\"daily-op-delay\", item.delay);" in panel
    assert "delayMark.hidden = !item.delay;" in panel


def test_daily_operation_action_colours_follow_the_visual_contract():
    panel = PANEL.read_text(encoding="utf-8")

    assert 'return bit === 1 ? "solar" : bit === 2 ? "grid" : "discharge";' in panel
    assert 'solar: "var(--daily-op-solar-charge)"' in panel
    assert 'grid: "var(--daily-op-grid)"' in panel
    assert 'discharge: "var(--daily-op-discharge)"' in panel
    assert 'item.decision === "not_needed" ? "not-needed" : "neutral"' in panel
    assert "const baseAction = item.solarWindow" in panel


def test_disabled_charge_delay_suppresses_stale_clock_markers():
    panel = PANEL.read_text(encoding="utf-8")

    assert "const delayEnabled = !snapshot.delayInfo" in panel
    assert "const delay = delayEnabled && !weeklyDelayBypassed" in panel

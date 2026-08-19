"""Pure tests for the learned/provider solar timeline."""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from custom_components.omnibattery.pricing.solar_timeline import (
    build_boundaries,
    build_solar_timeline,
    normalize_timeline_weights,
)
from custom_components.omnibattery.solar_forecast import SolarForecastPeriod


MADRID = ZoneInfo("Europe/Madrid")


def _solar_window():
    return (
        datetime(2026, 8, 18, 8, 0, tzinfo=MADRID),
        datetime(2026, 8, 18, 20, 0, tzinfo=MADRID),
    )


def _horizon():
    return build_boundaries(
        datetime(2026, 8, 18, 9, 7, tzinfo=MADRID),
        datetime(2026, 8, 18, 20, 0, tzinfo=MADRID),
    )


def test_normalization_preserves_exact_budget():
    result = normalize_timeline_weights([1.0, 2.0, 3.0], 4.25)

    assert result is not None
    assert sum(result) == pytest.approx(4.25)


def test_partial_first_interval_keeps_actual_start_and_duration():
    boundaries = _horizon()

    assert boundaries[0][0].hour == 9
    assert boundaries[0][0].minute == 7
    assert (boundaries[0][1] - boundaries[0][0]).total_seconds() == pytest.approx(8 * 60)
    assert boundaries[-1][1].hour == 20
    assert sum((end - start).total_seconds() for start, end in boundaries) == pytest.approx(
        (datetime(2026, 8, 18, 20, 0, tzinfo=MADRID).timestamp()
         - datetime(2026, 8, 18, 9, 7, tzinfo=MADRID).timestamp())
    )


@pytest.mark.parametrize(
    ("local_date", "expected_intervals", "expected_hours"),
    [
        (datetime(2026, 3, 29, tzinfo=MADRID), 92, 23.0),
        (datetime(2026, 10, 25, tzinfo=MADRID), 100, 25.0),
    ],
)
def test_boundaries_integrate_dst_days(
    local_date: datetime, expected_intervals: int, expected_hours: float
):
    boundaries = build_boundaries(local_date, local_date + timedelta(days=1))

    assert len(boundaries) == expected_intervals
    assert sum(end.timestamp() - start.timestamp() for start, end in boundaries) == pytest.approx(
        expected_hours * 3600
    )


def test_provider_periods_have_priority_and_are_resampled_to_horizon():
    solar_start, solar_end = _solar_window()
    provider = [SolarForecastPeriod(solar_start, solar_end, 8.0)]

    result = build_solar_timeline(
        _horizon(),
        6.0,
        provider_periods=provider,
        solar_start=solar_start,
        solar_end=solar_end,
        mode="active",
    )

    assert result.source == "provider"
    assert len(result.intervals_kwh) == len(_horizon())
    assert sum(result.intervals_kwh) == pytest.approx(6.0)
    assert result.energy_error_kwh == pytest.approx(0.0)


def test_mature_profile_is_used_when_provider_is_absent():
    solar_start, solar_end = _solar_window()
    shape = [0.0] * 96
    shape[48] = 1.0

    result = build_solar_timeline(
        _horizon(),
        3.0,
        learned_shape=shape,
        learned_mature=True,
        solar_start=solar_start,
        solar_end=solar_end,
    )

    assert result.source == "learned_profile"
    assert sum(result.intervals_kwh) == pytest.approx(3.0)
    assert max(result.intervals_kwh) == pytest.approx(3.0)


def test_automatic_timeline_falls_back_to_sinusoidal_until_profile_is_mature():
    solar_start, solar_end = _solar_window()
    shape = [0.0] * 96
    shape[48] = 1.0

    result = build_solar_timeline(
        _horizon(),
        3.0,
        learned_shape=shape,
        learned_mature=False,
        solar_start=solar_start,
        solar_end=solar_end,
    )

    assert result.source == "sinusoidal"
    assert "profile_not_mature" in (result.fallback_reason or "")


def test_shadow_keeps_sinusoidal_control_and_reports_selected_candidate():
    solar_start, solar_end = _solar_window()
    provider = [SolarForecastPeriod(solar_start, solar_end, 8.0)]

    result = build_solar_timeline(
        _horizon(),
        6.0,
        provider_periods=provider,
        solar_start=solar_start,
        solar_end=solar_end,
        mode="shadow",
    )

    assert result.source == "sinusoidal"
    assert result.shadow_selected_source == "provider"
    assert result.shadow_intervals_kwh
    assert sum(result.intervals_kwh) == pytest.approx(6.0)
    assert sum(result.shadow_intervals_kwh) == pytest.approx(6.0)


def test_safety_margin_is_applied_once_to_future_total():
    solar_start, solar_end = _solar_window()

    result = build_solar_timeline(
        _horizon(),
        5.0,
        safety_margin_kwh=1.25,
        solar_start=solar_start,
        solar_end=solar_end,
        mode="off",
    )

    assert result.remaining_raw_kwh == pytest.approx(5.0)
    assert result.remaining_effective_kwh == pytest.approx(3.75)
    assert sum(result.intervals_kwh) == pytest.approx(3.75)


def test_legacy_positional_shape_is_used_only_for_the_exact_horizon():
    solar_start, solar_end = _solar_window()
    boundaries = _horizon()
    shape = [0.0] * len(boundaries)
    shape[0] = 1.0

    result = build_solar_timeline(
        boundaries,
        2.0,
        temporal_shape=shape,
        solar_start=solar_start,
        solar_end=solar_end,
        mode="active",
    )
    assert result.source == "provider"
    assert result.intervals_kwh[0] == pytest.approx(2.0)

    invalid = build_solar_timeline(
        boundaries,
        2.0,
        temporal_shape=[1.0],
        solar_start=solar_start,
        solar_end=solar_end,
        mode="active",
    )
    assert invalid.source == "sinusoidal"
    assert "legacy_shape_length" in (invalid.fallback_reason or "")

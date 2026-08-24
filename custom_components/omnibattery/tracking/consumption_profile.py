"""Twenty-eight-day, quarter-hour household-consumption profile.

The profile deliberately has no dependency on the battery-control decisions.  It
stores the adjusted household demand as energy and coverage for each local
quarter-hour, then exposes a forecast with an explicit confidence contract.
Keeping the binning and the forecast in this module makes it possible to test
the algorithm without constructing a Home Assistant controller.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from time import monotonic
from typing import Any, Callable, Literal
from zoneinfo import ZoneInfo

from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from ..const import DEFAULT_BASE_CONSUMPTION_KWH, DOMAIN

_LOGGER = logging.getLogger(__name__)

INTERVAL_MINUTES = 15
INTERVAL_SECONDS = INTERVAL_MINUTES * 60
INTERVAL_COUNT = 24 * 60 // INTERVAL_MINUTES
INTERVALS_PER_HOUR = 60 // INTERVAL_MINUTES
PROFILE_RETENTION_DAYS = 28
MIN_INTERVAL_COVERAGE_S = INTERVAL_SECONDS * 0.75
MAX_SAMPLE_GAP_SECONDS = 5 * 60
PROFILE_STORE_VERSION = 1
PROFILE_STORE_KEY = "consumption_profile"
# Increment whenever persisted raw-day semantics change. Rebuilding from
# Recorder prevents previously contaminated intervals—such as profiles whose
# external-load backfill used the wrong energy conversion—from surviving an
# upgrade.
PROFILE_CAPTURE_VERSION = 3
# Temporary household-shape fallback used until the learned profile is mature.
# Values are relative hourly demand, not kWh.  The six quiet overnight hours
# stay at the lowest level, breakfast has a small lift, daytime demand rises
# around lunch and dinner is the strongest peak.  The values are normalized
# when converted to energy, so this heuristic never changes the daily total.
FALLBACK_HOURLY_WEIGHTS = (
    0.35, 0.35, 0.35, 0.35, 0.35, 0.35,
    0.60, 0.90, 1.10, 0.95, 1.00, 1.10,
    1.20, 1.25, 1.20, 1.05, 1.00, 1.05,
    1.15, 1.30, 1.45, 1.40, 1.00, 0.60,
)
FALLBACK_WEIGHT_SUM = sum(FALLBACK_HOURLY_WEIGHTS)
FALLBACK_LIVE_ADJUSTMENT_LIMIT = 0.30
FALLBACK_LIVE_ADJUSTMENT_START_HOUR = 3.0
FALLBACK_LIVE_ADJUSTMENT_FULL_HOUR = 12.0
# Descriptive aliases kept public so tests and diagnostics can refer to the
# policy without depending on the internal constant spelling.
MIN_COVERAGE_SECONDS = MIN_INTERVAL_COVERAGE_S
MAX_SAMPLE_GAP_S = MAX_SAMPLE_GAP_SECONDS

FallbackKind = Literal["legacy_daily", "current_rate"]


@dataclass(frozen=True)
class BinContribution:
    """Energy and observed time assigned to one local quarter-hour."""

    local_date: date
    interval_index: int
    energy_kwh: float
    coverage_s: float

    @property
    def bin_index(self) -> int:
        """Alias used by callers that call the quarter-hour a bin."""
        return self.interval_index


@dataclass
class ProfileDay:
    """Raw persisted data for one local date."""

    local_date: date
    energy_kwh: list[float] = field(default_factory=lambda: [0.0] * INTERVAL_COUNT)
    coverage_s: list[float] = field(default_factory=lambda: [0.0] * INTERVAL_COUNT)
    complete: bool = False

    def normalized_interval(self, interval_index: int) -> float | None:
        """Return a 15-minute-normalized value, or ``None`` without coverage."""
        if not 0 <= interval_index < INTERVAL_COUNT:
            return None
        coverage = self.coverage_s[interval_index]
        energy = self.energy_kwh[interval_index]
        if coverage < MIN_INTERVAL_COVERAGE_S:
            return None
        if not math.isfinite(coverage) or not math.isfinite(energy):
            return None
        if coverage < 0.0 or energy < 0.0:
            return None
        return max(0.0, energy / coverage * INTERVAL_SECONDS)

    def valid_interval_count(self) -> int:
        """Number of intervals with at least the required coverage."""
        return sum(
            self.normalized_interval(index) is not None
            for index in range(INTERVAL_COUNT)
        )

    def has_valid_data(self) -> bool:
        """Whether the day has at least one usable interval."""
        return self.valid_interval_count() > 0

    def as_dict(self) -> dict[str, Any]:
        """Serialize the raw day without persisting derived profiles."""
        return {
            "date": self.local_date.isoformat(),
            "energy_kwh": [
                round(max(0.0, float(value)), 9) for value in self.energy_kwh
            ],
            "coverage_s": [
                round(max(0.0, float(value)), 3) for value in self.coverage_s
            ],
            "complete": bool(self.complete),
        }


@dataclass
class ConsumptionForecast:
    """A forecast plus the evidence used to produce it."""

    energy_kwh: float
    intervals_kwh: list[float]
    source: str
    mature: bool
    coverage_ratio: float = 0.0
    weekday_samples: int = 0
    total_days: int = 0
    fallback_reason: str | None = None
    day_type_samples: int = 0
    profile_age_days: int | None = None
    newest_profile_date: date | None = None

    def __post_init__(self) -> None:
        """Keep data safe for Home Assistant state attributes and arithmetic."""
        safe_intervals: list[float] = []
        for value in self.intervals_kwh:
            try:
                parsed = float(value)
            except (TypeError, ValueError):
                parsed = 0.0
            safe_intervals.append(parsed if math.isfinite(parsed) and parsed >= 0.0 else 0.0)
        self.intervals_kwh = safe_intervals
        try:
            parsed_energy = float(self.energy_kwh)
        except (TypeError, ValueError):
            parsed_energy = 0.0
        self.energy_kwh = (
            parsed_energy if math.isfinite(parsed_energy) and parsed_energy >= 0.0 else 0.0
        )
        try:
            coverage = float(self.coverage_ratio)
        except (TypeError, ValueError):
            coverage = 0.0
        self.coverage_ratio = min(1.0, max(0.0, coverage)) if math.isfinite(coverage) else 0.0

    @property
    def interval_profile_kwh(self) -> list[float]:
        """Human-readable alias for the persisted API terminology."""
        return self.intervals_kwh


def _finite_non_negative(value: Any) -> float | None:
    """Parse a non-negative finite number."""
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed) or parsed < 0.0:
        return None
    return parsed


def _timezone_for_value(value: datetime, fallback: Any = None) -> Any:
    """Choose a timezone for an aware datetime or a supplied fallback."""
    if value.tzinfo is not None:
        return value.tzinfo
    return fallback


def _as_timestamp(value: datetime) -> float:
    """Return an absolute timestamp, treating naive datetimes as UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc).timestamp()
    return value.timestamp()


def _datetime_from_timestamp(timestamp: float, tz: Any) -> datetime:
    """Build a datetime in ``tz`` without relying on Home Assistant state."""
    if tz is None:
        return datetime.fromtimestamp(timestamp, timezone.utc).replace(tzinfo=None)
    return datetime.fromtimestamp(timestamp, tz)


def _next_local_quarter_timestamp(cursor_ts: float, tz: Any) -> float:
    """Find the next local quarter boundary in absolute time.

    Mapping both folds is important on the autumn DST transition.  Mapping a
    nonexistent wall time is also useful: zoneinfo maps the skipped boundary to
    the transition instant, which cleanly splits the interval before and after
    the missing local hour.
    """
    cursor = _datetime_from_timestamp(cursor_ts, tz)
    wall = cursor.replace(tzinfo=None)
    quarter_minute = (wall.minute // INTERVAL_MINUTES) * INTERVAL_MINUTES
    boundary = wall.replace(
        minute=quarter_minute,
        second=0,
        microsecond=0,
    )
    if boundary <= wall:
        boundary += timedelta(minutes=INTERVAL_MINUTES)

    if tz is None:
        return boundary.replace(tzinfo=timezone.utc).timestamp()

    candidates: list[float] = []
    for fold in (0, 1):
        try:
            candidate = boundary.replace(tzinfo=tz, fold=fold).timestamp()
        except (OverflowError, ValueError):
            continue
        if candidate > cursor_ts + 1e-7:
            candidates.append(candidate)
    if candidates:
        next_boundary = min(candidates)
    else:
        next_boundary = cursor_ts + INTERVAL_SECONDS

    # A fall-back transition moves the wall clock backwards.  In that case the
    # next wall quarter (03:00 in Madrid) is later than the actual transition,
    # so split at the offset change as well; otherwise the two occurrences of
    # the repeated hour would be merged into one arbitrary quarter.
    current_offset = cursor.utcoffset()
    if current_offset is not None:
        probe = cursor_ts + INTERVAL_SECONDS
        max_probe = cursor_ts + 2 * 3600
        while probe <= max_probe and probe < next_boundary - 1e-7:
            probe_dt = _datetime_from_timestamp(probe, tz)
            if probe_dt.utcoffset() != current_offset:
                lo = cursor_ts
                hi = probe
                for _ in range(40):
                    mid = (lo + hi) / 2.0
                    if _datetime_from_timestamp(mid, tz).utcoffset() == current_offset:
                        lo = mid
                    else:
                        hi = mid
                next_boundary = min(next_boundary, hi)
                break
            probe += INTERVAL_SECONDS
    if candidates:
        return next_boundary
    # A pathological custom tzinfo must not make the split loop infinite.
    return next_boundary


def _local_segments(
    start_local: datetime,
    end_local: datetime,
) -> list[tuple[float, float, datetime]]:
    """Split an absolute range at local quarter boundaries.

    The returned tuple contains absolute start/end timestamps and the local
    midpoint used to identify the target date and interval.  A repeated local
    hour therefore produces two sets of segments with the same local bin; a
    skipped hour produces none.
    """
    start_ts = _as_timestamp(start_local)
    end_ts = _as_timestamp(end_local)
    if not math.isfinite(start_ts) or not math.isfinite(end_ts) or end_ts <= start_ts:
        return []

    tz = _timezone_for_value(start_local)
    if tz is None and end_local.tzinfo is not None:
        tz = end_local.tzinfo

    segments: list[tuple[float, float, datetime]] = []
    cursor = start_ts
    while cursor < end_ts - 1e-7:
        boundary = _next_local_quarter_timestamp(cursor, tz)
        segment_end = min(end_ts, max(cursor + 1e-7, boundary))
        midpoint = _datetime_from_timestamp((cursor + segment_end) / 2.0, tz)
        segments.append((cursor, segment_end, midpoint))
        cursor = segment_end
    return segments


def _interval_index(local_time: time) -> int:
    """Return the quarter-hour index for a local time."""
    return min(INTERVAL_COUNT - 1, (local_time.hour * 60 + local_time.minute) // INTERVAL_MINUTES)


def split_sample_across_bins(
    start_local: datetime,
    end_local: datetime,
    previous_kw: float,
    current_kw: float,
) -> list[BinContribution]:
    """Integrate one trapezoidal sample over every local bin it crosses.

    ``previous_kw`` and ``current_kw`` are linearly interpolated over the
    absolute interval.  Invalid or negative power and a non-positive interval
    are rejected by returning no contributions.  The function is pure and does
    not apply charging-window masks; capture is always 24-hour data.
    """
    previous = _finite_non_negative(previous_kw)
    current = _finite_non_negative(current_kw)
    if previous is None or current is None:
        return []

    start_ts = _as_timestamp(start_local)
    end_ts = _as_timestamp(end_local)
    duration = end_ts - start_ts
    if not math.isfinite(duration) or duration <= 0.0:
        return []

    contributions: list[BinContribution] = []
    for segment_start, segment_end, midpoint in _local_segments(start_local, end_local):
        start_fraction = (segment_start - start_ts) / duration
        end_fraction = (segment_end - start_ts) / duration
        power_start = previous + (current - previous) * start_fraction
        power_end = previous + (current - previous) * end_fraction
        seconds = segment_end - segment_start
        energy_kwh = ((power_start + power_end) / 2.0) * seconds / 3600.0
        if not math.isfinite(energy_kwh) or energy_kwh < 0.0:
            continue
        contributions.append(
            BinContribution(
                local_date=midpoint.date(),
                interval_index=_interval_index(midpoint.timetz().replace(tzinfo=None)),
                energy_kwh=energy_kwh,
                coverage_s=seconds,
            )
        )
    return contributions


def _state_to_power_kw(state: Any) -> float | None:
    """Convert a Recorder state to a usable non-negative kW value."""
    if state is None or getattr(state, "state", None) in ("unknown", "unavailable"):
        return None
    value = _finite_non_negative(getattr(state, "state", None))
    if value is None:
        return None
    unit = str(getattr(state, "attributes", {}).get("unit_of_measurement", "W")).lower()
    if unit == "kw":
        return value
    if unit == "w":
        return value / 1000.0
    return None


def _state_timestamp(state: Any) -> datetime | None:
    """Use the Recorder update timestamp, tolerating small test doubles."""
    timestamp = getattr(state, "last_updated", None)
    if timestamp is None:
        timestamp = getattr(state, "last_changed", None)
    return timestamp if isinstance(timestamp, datetime) else None


def fallback_daily_intervals(daily_kwh: float) -> list[float]:
    """Distribute a daily fallback total over the 96 local quarter-hours.

    The shape is deliberately a short-lived heuristic.  It is normalized by
    the sum of its hourly weights, so callers can safely use it with any daily
    total without creating or losing energy.
    """
    daily = _finite_non_negative(daily_kwh) or 0.0
    if FALLBACK_WEIGHT_SUM <= 0.0:
        return [daily / INTERVAL_COUNT] * INTERVAL_COUNT
    per_weighted_hour = daily / FALLBACK_WEIGHT_SUM
    return [
        per_weighted_hour * weight / INTERVALS_PER_HOUR
        for weight in FALLBACK_HOURLY_WEIGHTS
        for _ in range(INTERVALS_PER_HOUR)
    ]


def adjust_remaining_fallback_energy(
    baseline_remaining_kwh: float,
    daily_expected_kwh: float,
    consumed_today_kwh: float,
    elapsed_hours: float,
) -> tuple[float, float]:
    """Condition a shaped daily fallback on today's observed consumption.

    The curve remains the primary forecast.  After the first three hours, its
    remaining energy is progressively reconciled with the unconsumed part of
    the daily estimate.  The correction reaches full strength at noon and is
    capped to 30% of the baseline remainder so a one-off appliance spike cannot
    erase the household demand still expected later in the day.

    Return ``(adjusted_energy, applied_correction)`` for diagnostics.
    """
    baseline = _finite_non_negative(baseline_remaining_kwh) or 0.0
    daily = _finite_non_negative(daily_expected_kwh) or 0.0
    consumed = _finite_non_negative(consumed_today_kwh)
    elapsed = _finite_non_negative(elapsed_hours) or 0.0
    if baseline <= 0.0 or daily <= 0.0 or consumed is None:
        return baseline, 0.0

    confidence = max(
        0.0,
        min(
            1.0,
            (elapsed - FALLBACK_LIVE_ADJUSTMENT_START_HOUR)
            / (
                FALLBACK_LIVE_ADJUSTMENT_FULL_HOUR
                - FALLBACK_LIVE_ADJUSTMENT_START_HOUR
            ),
        ),
    )
    remaining_daily_budget = max(0.0, daily - consumed)
    raw_correction = (remaining_daily_budget - baseline) * confidence
    limit = baseline * FALLBACK_LIVE_ADJUSTMENT_LIMIT
    correction = max(-limit, min(limit, raw_correction))
    return max(0.0, baseline + correction), correction


def _series_to_bins(states: list[Any], tz: Any = None) -> dict[date, ProfileDay]:
    """Convert one Recorder power series into raw profile days."""
    result: dict[date, ProfileDay] = {}
    previous_ts: datetime | None = None
    previous_kw: float | None = None
    for state in states:
        timestamp = _state_timestamp(state)
        power_kw = _state_to_power_kw(state)
        if timestamp is None or power_kw is None:
            previous_ts = None
            previous_kw = None
            continue
        if tz is not None:
            timestamp = (
                timestamp.astimezone(tz)
                if timestamp.tzinfo is not None
                else timestamp.replace(tzinfo=tz)
            )
        if previous_ts is not None and previous_kw is not None:
            gap = _as_timestamp(timestamp) - _as_timestamp(previous_ts)
            if 0.0 < gap <= MAX_SAMPLE_GAP_SECONDS:
                for contribution in split_sample_across_bins(
                    previous_ts,
                    timestamp,
                    previous_kw,
                    power_kw,
                ):
                    day = result.setdefault(
                        contribution.local_date,
                        ProfileDay(contribution.local_date),
                    )
                    index = contribution.interval_index
                    day.energy_kwh[index] += contribution.energy_kwh
                    day.coverage_s[index] += contribution.coverage_s
        previous_ts = timestamp
        previous_kw = power_kw

    return result


def _apply_external_load_to_day(
    adjusted: ProfileDay,
    device_day: ProfileDay,
    factor: float,
) -> None:
    """Apply one external load to a home profile day in place.

    ``ProfileDay.energy_kwh`` stores energy, not power.  Normalize the device
    energy to the home interval's covered duration by scaling the two coverage
    values directly.  ``factor`` is negative for excluded loads and positive
    for additional loads.
    """
    for index in range(INTERVAL_COUNT):
        device_coverage = device_day.coverage_s[index]
        home_coverage = adjusted.coverage_s[index]
        if device_coverage <= 0.0 or home_coverage <= 0.0:
            continue
        matched_device_energy = (
            device_day.energy_kwh[index]
            * home_coverage
            / device_coverage
        )
        adjusted.energy_kwh[index] = max(
            0.0,
            adjusted.energy_kwh[index] + factor * matched_device_energy,
        )


class ConsumptionProfileTracker:
    """Capture and query the 28-day local quarter-hour profile."""

    def __init__(
        self,
        hass: Any,
        config_entry: Any,
        controller: Any,
        *,
        fallback_daily_kwh: float | Callable[[], float] | None = None,
    ) -> None:
        self._hass = hass
        self._config_entry = config_entry
        self._controller = controller
        self._store: Store = Store(
            hass,
            PROFILE_STORE_VERSION,
            f"{DOMAIN}.{config_entry.entry_id}.{PROFILE_STORE_KEY}",
        )
        self._days: dict[date, ProfileDay] = {}
        self._last_sample_time: datetime | None = None
        self._last_sample_monotonic: float | None = None
        self._last_power_kw: float | None = None
        self._last_local_date: date | None = None
        self._last_save_monotonic = 0.0
        self._save_task: asyncio.Task | None = None
        self._backfill_task: asyncio.Task | None = None
        self._last_error: str | None = None
        self._loaded = False
        self._invalidated = False
        self._fallback_daily_kwh = fallback_daily_kwh
        self._active_fingerprint = self.configuration_fingerprint()

    # ------------------------------------------------------------------
    # Configuration, time and validation
    # ------------------------------------------------------------------

    def _timezone(self) -> Any:
        """Return the Home Assistant configured timezone."""
        configured = getattr(getattr(self._hass, "config", None), "time_zone", None)
        if configured:
            try:
                return dt_util.get_time_zone(configured) or ZoneInfo(configured)
            except Exception:  # noqa: BLE001 - HA may expose a custom tz provider
                pass
        return timezone.utc

    def _now(self) -> datetime:
        """Return an aware local Home Assistant time."""
        try:
            current = dt_util.now()
        except Exception:  # noqa: BLE001
            current = datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=self._timezone())
        return current.astimezone(self._timezone())

    def _today(self) -> date:
        return self._now().date()

    def configuration_fingerprint(self) -> str:
        """Hash consumption sources and load adjustments, excluding solar forecast."""
        data = getattr(self._config_entry, "data", {}) or {}
        devices = []
        for device in data.get("excluded_devices", []) or []:
            if not isinstance(device, dict):
                continue
            devices.append(
                {
                    key: device.get(key)
                    for key in (
                        "enabled",
                        "power_sensor",
                        "included_in_consumption",
                        "exclusion_pct",
                        "ev_charger_no_telemetry",
                    )
                }
            )
        devices.sort(
            key=lambda device: json.dumps(
                device,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            )
        )
        payload = {
            "consumption_sensor": data.get("consumption_sensor"),
            "household_consumption_sensor": data.get("household_consumption_sensor"),
            "solar_production_sensor": data.get("solar_production_sensor"),
            "meter_inverted": bool(data.get("meter_inverted", False)),
            "excluded_devices": devices,
            "timezone": getattr(getattr(self._hass, "config", None), "time_zone", None),
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @staticmethod
    def _parse_day(raw: Any) -> ProfileDay | None:
        """Validate one persisted day in isolation."""
        if not isinstance(raw, dict):
            return None
        try:
            local_date = date.fromisoformat(str(raw["date"]))
        except (KeyError, TypeError, ValueError):
            return None
        energy = raw.get("energy_kwh")
        coverage = raw.get("coverage_s")
        if not isinstance(energy, list) or not isinstance(coverage, list):
            return None
        if len(energy) != INTERVAL_COUNT or len(coverage) != INTERVAL_COUNT:
            return None
        parsed_energy: list[float] = []
        parsed_coverage: list[float] = []
        for energy_value, coverage_value in zip(energy, coverage):
            parsed_e = _finite_non_negative(energy_value)
            parsed_c = _finite_non_negative(coverage_value)
            if parsed_e is None or parsed_c is None:
                return None
            parsed_energy.append(parsed_e)
            parsed_coverage.append(parsed_c)
        return ProfileDay(
            local_date=local_date,
            energy_kwh=parsed_energy,
            coverage_s=parsed_coverage,
            complete=bool(raw.get("complete", False)),
        )

    def _retention_floor(self, reference_date: date | None = None) -> date:
        reference_date = reference_date or self._today()
        return reference_date - timedelta(days=PROFILE_RETENTION_DAYS)

    def _prune(self, reference_date: date | None = None) -> None:
        """Keep the current day plus the previous 28 local dates."""
        floor = self._retention_floor(reference_date)
        self._days = {
            local_date: day
            for local_date, day in self._days.items()
            if floor <= local_date <= (reference_date or self._today())
        }

    async def async_load(self) -> bool:
        """Restore the raw profile, dropping corrupt days without failing setup."""
        try:
            data = await self._store.async_load()
        except Exception as exc:  # noqa: BLE001
            self._last_error = f"load: {exc}"
            _LOGGER.warning("Consumption profile: failed to load Store: %s", exc)
            self._loaded = True
            return False

        if not data:
            self._loaded = True
            return False
        if not isinstance(data, dict):
            self._last_error = "load: invalid Store payload"
            self._loaded = True
            _LOGGER.warning("Consumption profile: discarded invalid Store payload")
            return False

        expected_fingerprint = self.configuration_fingerprint()
        stored_fingerprint = data.get("configuration_fingerprint")
        stored_timezone = data.get("timezone")
        current_timezone = getattr(
            getattr(self._hass, "config", None), "time_zone", None
        )
        if (
            stored_fingerprint
            and stored_fingerprint != expected_fingerprint
        ) or (stored_timezone and stored_timezone != current_timezone):
            self._days = {}
            self._invalidated = True
            self._active_fingerprint = expected_fingerprint
            self._last_error = "profile invalidated after source or timezone change"
            self._loaded = True
            _LOGGER.info("Consumption profile: invalidated after configuration change")
            return False

        if data.get("capture_version") != PROFILE_CAPTURE_VERSION:
            self._days = {}
            self._invalidated = True
            self._active_fingerprint = expected_fingerprint
            self._last_error = "profile invalidated after capture contract change"
            self._loaded = True
            _LOGGER.info(
                "Consumption profile: discarded raw days after capture contract "
                "change; Recorder backfill will rebuild them"
            )
            await self.async_save()
            return False

        raw_days = data.get("days", [])
        loaded: dict[date, ProfileDay] = {}
        if isinstance(raw_days, list):
            for raw_day in raw_days:
                parsed = self._parse_day(raw_day)
                if parsed is None:
                    _LOGGER.warning("Consumption profile: discarded corrupt day")
                    continue
                previous = loaded.get(parsed.local_date)
                if previous is None or sum(parsed.coverage_s) > sum(previous.coverage_s):
                    loaded[parsed.local_date] = parsed

        self._days = loaded
        self._prune()
        self._active_fingerprint = expected_fingerprint
        self._loaded = True
        _LOGGER.info(
            "Consumption profile: restored %d valid days",
            len(self._days),
        )
        return bool(self._days)

    def invalidate_if_configuration_changed(self) -> bool:
        """Clear incompatible raw data after an options/source update."""
        current = self.configuration_fingerprint()
        if current == self._active_fingerprint:
            return False
        self._days = {}
        self._active_fingerprint = current
        self._invalidated = True
        self._last_error = "profile invalidated after source or load-adjustment change"
        self._last_sample_time = None
        self._last_sample_monotonic = None
        self._last_power_kw = None
        self.request_save()
        _LOGGER.info("Consumption profile: invalidated after source/load configuration change")
        return True

    def _store_payload(self) -> dict[str, Any]:
        current_date = self._today()
        self._prune(current_date)
        return {
            "interval_minutes": INTERVAL_MINUTES,
            "retention_days": PROFILE_RETENTION_DAYS,
            "capture_version": PROFILE_CAPTURE_VERSION,
            "timezone": getattr(getattr(self._hass, "config", None), "time_zone", None),
            "configuration_fingerprint": self.configuration_fingerprint(),
            "days": [
                self._days[local_date].as_dict()
                for local_date in sorted(self._days)
            ],
        }

    async def async_save(self) -> None:
        """Persist the raw days and never propagate Store failures to control."""
        try:
            await self._store.async_save(self._store_payload())
            self._last_error = None
        except Exception as exc:  # noqa: BLE001
            self._last_error = f"save: {exc}"
            _LOGGER.error("Consumption profile: failed to save Store: %s", exc)

    def request_save(self) -> None:
        """Schedule one save, coalescing rapid control-loop requests."""
        if self._save_task is not None and not self._save_task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # Unit tests may call a capture helper outside a running loop.
            self._save_task = None
            return
        self._save_task = loop.create_task(self.async_save())

    async def async_save_all(self) -> None:
        """Flush profile data and stop a pending Recorder backfill."""
        if self._backfill_task is not None and not self._backfill_task.done():
            self._backfill_task.cancel()
            try:
                await self._backfill_task
            except asyncio.CancelledError:
                pass
        if self._save_task is not None and not self._save_task.done():
            try:
                await self._save_task
            except asyncio.CancelledError:
                pass
        await self.async_save()

    # ------------------------------------------------------------------
    # Capture and raw-day operations
    # ------------------------------------------------------------------

    def _day(self, local_date: date) -> ProfileDay:
        return self._days.setdefault(local_date, ProfileDay(local_date))

    @property
    def days(self) -> list[ProfileDay]:
        """Return raw days in chronological order for read-only consumers."""
        return [self._days[local_date] for local_date in sorted(self._days)]

    def _close_previous_days(self, current_date: date) -> bool:
        changed = False
        for local_date, day in self._days.items():
            if local_date < current_date and not day.complete:
                day.complete = True
                changed = True
        self._prune(current_date)
        return changed

    def record_power_sample(
        self,
        power_kw: float | None,
        *,
        local_time: datetime | None = None,
        monotonic_time: float | None = None,
    ) -> None:
        """Record one adjusted power reading, including all local-day bins crossed."""
        if local_time is None:
            local_time = self._now()
        elif local_time.tzinfo is None:
            local_time = local_time.replace(tzinfo=self._timezone())
        else:
            local_time = local_time.astimezone(self._timezone())

        current_date = local_time.date()
        self._day(current_date)
        if self._last_local_date != current_date:
            if self._close_previous_days(current_date):
                self.request_save()
            self._last_local_date = current_date

        parsed_power = _finite_non_negative(power_kw)
        if parsed_power is None:
            # Unknown/unavailable/invalid input breaks continuity.  The next
            # valid reading becomes a baseline and does not fill the gap.
            self._last_sample_time = None
            self._last_sample_monotonic = None
            self._last_power_kw = None
            return

        if self._last_sample_time is not None and self._last_power_kw is not None:
            if monotonic_time is not None and self._last_sample_monotonic is not None:
                elapsed = monotonic_time - self._last_sample_monotonic
            else:
                elapsed = _as_timestamp(local_time) - _as_timestamp(self._last_sample_time)
            if 0.0 < elapsed <= MAX_SAMPLE_GAP_SECONDS:
                for contribution in split_sample_across_bins(
                    self._last_sample_time,
                    local_time,
                    self._last_power_kw,
                    parsed_power,
                ):
                    day = self._day(contribution.local_date)
                    index = contribution.interval_index
                    day.energy_kwh[index] += contribution.energy_kwh
                    day.coverage_s[index] += contribution.coverage_s
            elif elapsed > MAX_SAMPLE_GAP_SECONDS:
                _LOGGER.debug(
                    "Consumption profile: discarded %.1fs sample gap",
                    elapsed,
                )

        self._last_sample_time = local_time
        self._last_sample_monotonic = monotonic_time
        self._last_power_kw = parsed_power
        self._prune(current_date)

        if self._last_save_monotonic == 0.0 or monotonic() - self._last_save_monotonic >= 300:
            self._last_save_monotonic = monotonic()
            self.request_save()

    def add_day(self, day: ProfileDay) -> None:
        """Merge a validated raw day, preferring greater interval coverage."""
        parsed = self._parse_day(day.as_dict())
        if parsed is None:
            return
        existing = self._days.get(parsed.local_date)
        if existing is None:
            self._days[parsed.local_date] = parsed
            return
        for index in range(INTERVAL_COUNT):
            if parsed.coverage_s[index] > existing.coverage_s[index]:
                existing.energy_kwh[index] = parsed.energy_kwh[index]
                existing.coverage_s[index] = parsed.coverage_s[index]
        existing.complete = existing.complete or parsed.complete

    def current_day_capture(
        self, local_date: date | None = None
    ) -> dict[str, Any] | None:
        """Return the raw live capture for one local day.

        This is intentionally separate from the forecast: the current day is
        persisted while it is being collected, but it is not eligible for
        training until it is complete. The bounded snapshot is used by the
        diagnostic capture sensor so users can verify the learning stream.

        A day without any covered samples is not a zero-energy capture.  It is
        the transient state while the profile is loading, being rebuilt from
        Recorder, or waiting for the first valid sample after a restart.
        """
        local_date = local_date or self._today()
        day = self._days.get(local_date)
        if day is None or sum(day.coverage_s) <= 0.0:
            return None

        energy = list(day.energy_kwh)
        coverage = list(day.coverage_s)
        safe_energy = []
        for value in energy:
            parsed = _finite_non_negative(value)
            safe_energy.append(round(parsed, 6) if parsed is not None else 0.0)
        safe_coverage = []
        for value in coverage:
            parsed = _finite_non_negative(value)
            safe_coverage.append(round(parsed, 3) if parsed is not None else 0.0)
        total_coverage = sum(safe_coverage)
        coverage_ratio = min(
            1.0,
            max(0.0, total_coverage / (INTERVAL_COUNT * INTERVAL_SECONDS)),
        )
        hourly = [
            round(sum(safe_energy[index:index + 4]), 6)
            for index in range(0, INTERVAL_COUNT, 4)
        ]
        return {
            "date": local_date.isoformat(),
            "complete": bool(day.complete),
            "energy_kwh": round(sum(safe_energy), 6),
            "hourly_energy_kwh": hourly,
            "interval_energy_kwh": safe_energy,
            "interval_coverage_s": safe_coverage,
            "valid_intervals": day.valid_interval_count(),
            "coverage_ratio": round(coverage_ratio, 6),
        }

    # ------------------------------------------------------------------
    # Forecast construction
    # ------------------------------------------------------------------

    @staticmethod
    def _day_type(local_date: date) -> str:
        return "weekend" if local_date.weekday() >= 5 else "weekday"

    @staticmethod
    def _age_weight(age_days: int) -> float:
        if age_days <= 6:
            return 1.0
        if age_days <= 13:
            return 0.75
        if age_days <= 20:
            return 0.50
        if age_days <= 27:
            return 0.25
        return 0.0

    def _fallback_daily_value(self) -> float:
        source = self._fallback_daily_kwh
        try:
            value = source() if callable(source) else source
            if value is None:
                value = self._controller.get_avg_daily_consumption()
            parsed = _finite_non_negative(value)
        except Exception:  # noqa: BLE001
            parsed = None
        if parsed is None or parsed <= 0.0:
            try:
                parsed = _finite_non_negative(
                    self._controller.get_avg_daily_consumption()
                )
            except Exception:  # noqa: BLE001
                parsed = None
        return parsed if parsed is not None and parsed > 0.0 else DEFAULT_BASE_CONSUMPTION_KWH

    def _current_rate_value(self) -> float:
        try:
            value = self._controller.get_adjusted_home_power_kw()
            parsed = _finite_non_negative(value)
            if parsed is not None:
                return parsed
        except Exception:  # noqa: BLE001
            pass
        return self._fallback_daily_value() / 24.0

    def _fallback_intervals(self, fallback: FallbackKind) -> list[float]:
        if fallback == "current_rate":
            return [self._current_rate_value() * INTERVAL_SECONDS / 3600.0] * INTERVAL_COUNT
        return fallback_daily_intervals(self._fallback_daily_value())

    def _usable_days(self) -> list[ProfileDay]:
        # The current day remains persisted and receives live samples, but it
        # is partial by definition and must not count as a historical sample.
        return [
            day
            for day in self._days.values()
            if day.complete and day.has_valid_data()
        ]

    def forecast_for_date(
        self,
        target_date: date,
        *,
        fallback: FallbackKind = "legacy_daily",
        interval_indices: set[int] | None = None,
    ) -> ConsumptionForecast:
        """Build the weighted weekday/day-type/global forecast for ``target_date``."""
        today = self._today()
        days = self._usable_days()
        total_days = len(days)
        fallback_intervals = self._fallback_intervals(fallback)
        requested_indices = {
            index
            for index in (interval_indices or set(range(INTERVAL_COUNT)))
            if 0 <= index < INTERVAL_COUNT
        }
        if not requested_indices:
            requested_indices = set(range(INTERVAL_COUNT))

        values: list[float] = []
        weekday_counts: list[int] = []
        day_type_counts: list[int] = []
        interval_has_candidate: list[bool] = []
        newest: date | None = None

        for interval_index in range(INTERVAL_COUNT):
            weekday_values: list[tuple[float, float, date]] = []
            day_type_values: list[tuple[float, float, date]] = []
            global_values: list[tuple[float, float, date]] = []
            for day in days:
                value = day.normalized_interval(interval_index)
                if value is None:
                    continue
                age = max(0, (today - day.local_date).days)
                weight = self._age_weight(age)
                if weight <= 0.0:
                    continue
                item = (value, weight, day.local_date)
                global_values.append(item)
                if day.local_date.weekday() == target_date.weekday():
                    weekday_values.append(item)
                if self._day_type(day.local_date) == self._day_type(target_date):
                    day_type_values.append(item)

            weekday_counts.append(len(weekday_values))
            day_type_counts.append(len(day_type_values))
            candidates = weekday_values + day_type_values + global_values
            if candidates and interval_index in requested_indices:
                candidate_dates = [item[2] for item in candidates]
                latest = max(candidate_dates)
                newest = latest if newest is None else max(newest, latest)
            if weekday_values:
                weekday_sum = sum(value * weight for value, weight, _ in weekday_values)
                weekday_weight_sum = sum(weight for _, weight, _ in weekday_values)
                weekday_mean = weekday_sum / weekday_weight_sum if weekday_weight_sum else 0.0
            else:
                weekday_mean = 0.0

            if day_type_values:
                day_type_sum = sum(value * weight for value, weight, _ in day_type_values)
                day_type_weight_sum = sum(weight for _, weight, _ in day_type_values)
                day_type_mean = day_type_sum / day_type_weight_sum if day_type_weight_sum else 0.0
            else:
                day_type_mean = 0.0

            if global_values:
                global_sum = sum(value * weight for value, weight, _ in global_values)
                global_weight_sum = sum(weight for _, weight, _ in global_values)
                global_mean = global_sum / global_weight_sum if global_weight_sum else 0.0
            else:
                global_mean = 0.0

            if weekday_values:
                confidence = min(1.0, len(weekday_values) / 4.0)
                weekday_weight = 0.65 * confidence
                base = day_type_mean or global_mean
                value = weekday_weight * weekday_mean + (1.0 - weekday_weight) * base
            else:
                value = day_type_mean or global_mean
            has_candidate = bool(weekday_values or day_type_values or global_values)
            interval_has_candidate.append(has_candidate)
            values.append(max(0.0, value if has_candidate else 0.0))

        requested_count = len(requested_indices)
        coverage_ratio = (
            sum(
                present
                for index, present in enumerate(interval_has_candidate)
                if index in requested_indices
            )
            / requested_count
            if requested_count
            else 0.0
        )
        weekday_sample_intervals = sum(
            count >= 2
            for index, count in enumerate(weekday_counts)
            if index in requested_indices
        )
        newest_age = (today - newest).days if newest is not None else None
        mature = (
            weekday_sample_intervals >= math.ceil(requested_count * 0.75)
            and total_days >= 7
            and coverage_ratio >= 0.80
            and newest_age is not None
            and newest_age <= 7
        )

        fallback_reason: str | None = None
        if not mature:
            if not days:
                fallback_reason = "no_profile_data"
            elif total_days < 7:
                fallback_reason = "insufficient_days"
            elif weekday_sample_intervals < math.ceil(requested_count * 0.75):
                fallback_reason = "insufficient_weekday_samples"
            elif coverage_ratio < 0.80:
                fallback_reason = "insufficient_coverage"
            elif newest_age is None or newest_age > 7:
                fallback_reason = "stale_profile"
            else:
                fallback_reason = self._last_error or "profile_not_mature"
            values = fallback_intervals
            source = fallback
        else:
            # Fill a rare uncovered interval from the same coherent profile's
            # global mean when available.  This avoids a zero-energy hole while
            # keeping the source profile rather than silently switching an
            # entire control decision to the legacy estimate.
            profile_mean = (
                sum(
                    values[index]
                    for index, present in enumerate(interval_has_candidate)
                    if present and index in requested_indices
                )
                / max(
                    1,
                    sum(
                        present
                        for index, present in enumerate(interval_has_candidate)
                        if present and index in requested_indices
                    ),
                )
            )
            values = [
                value if present else profile_mean
                for value, present in zip(values, interval_has_candidate)
            ]
            source = "profile"

        return ConsumptionForecast(
            energy_kwh=sum(values),
            intervals_kwh=values,
            source=source,
            mature=mature,
            coverage_ratio=coverage_ratio,
            weekday_samples=min(
                (count for index, count in enumerate(weekday_counts) if index in requested_indices),
                default=0,
            ),
            day_type_samples=min(
                (count for index, count in enumerate(day_type_counts) if index in requested_indices),
                default=0,
            ),
            total_days=total_days,
            fallback_reason=fallback_reason,
            profile_age_days=newest_age,
            newest_profile_date=newest,
        )

    # ------------------------------------------------------------------
    # Date-range queries and charging-window masks
    # ------------------------------------------------------------------

    @staticmethod
    def _day_name(local_date: date) -> str:
        return ("mon", "tue", "wed", "thu", "fri", "sat", "sun")[local_date.weekday()]

    @staticmethod
    def _parse_slot(slot: dict[str, Any]) -> tuple[time, time] | None:
        try:
            return time.fromisoformat(slot["start_time"]), time.fromisoformat(slot["end_time"])
        except (KeyError, TypeError, ValueError):
            return None

    def _slot_active(self, local_dt: datetime) -> bool:
        """Respect each slot's weekday, including the overnight predecessor."""
        slots = getattr(self._controller, "charging_time_slots", []) or []
        if not slots:
            return False
        current_name = self._day_name(local_dt.date())
        previous_name = self._day_name(local_dt.date() - timedelta(days=1))
        current_time = local_dt.timetz().replace(tzinfo=None)
        for slot in slots:
            if not isinstance(slot, dict) or slot.get("enabled", True) is False:
                continue
            parsed = self._parse_slot(slot)
            if parsed is None:
                continue
            start, end = parsed
            days = slot.get("days", []) or []
            if start <= end:
                if current_name in days and start <= current_time < end:
                    return True
            else:
                if current_name in days and current_time >= start:
                    return True
                if previous_name in days and current_time < end:
                    return True
        return False

    def _day_has_operating_slot(self, local_date: date) -> bool:
        """Return whether any configured slot can operate on this local date."""
        slots = getattr(self._controller, "charging_time_slots", []) or []
        if not slots:
            return True
        name = self._day_name(local_date)
        previous_name = self._day_name(local_date - timedelta(days=1))
        for slot in slots:
            if not isinstance(slot, dict) or slot.get("enabled", True) is False:
                continue
            parsed = self._parse_slot(slot)
            if parsed is None:
                continue
            start, end = parsed
            days = slot.get("days", []) or []
            if name in days or (start > end and previous_name in days):
                return True
        return False

    def _legacy_fallback_day_scale(
        self,
        local_date: date,
        forecast: ConsumptionForecast,
    ) -> float:
        """Keep the legacy fallback daily total across local DST transitions.

        The stored shape has 24 nominal hours.  A local day can contain 23 or 25
        real hours, so normalize the actually traversed wall-clock segments.
        Charging-window masks intentionally do not affect this scale: excluded
        hours must remove their household demand rather than redistribute it.
        """
        if forecast.source != "legacy_daily":
            return 1.0

        timezone = self._timezone()
        day_start = datetime.combine(local_date, time.min, tzinfo=timezone)
        next_day = datetime.combine(
            local_date + timedelta(days=1),
            time.min,
            tzinfo=timezone,
        )
        local_day_energy = 0.0
        fallback_intervals = forecast.intervals_kwh
        for segment_start, segment_end, midpoint in _local_segments(day_start, next_day):
            index = _interval_index(midpoint.timetz().replace(tzinfo=None))
            local_day_energy += (
                fallback_intervals[index]
                * (segment_end - segment_start)
                / INTERVAL_SECONDS
            )

        if local_day_energy <= 0.0:
            return 0.0
        return forecast.energy_kwh / local_day_energy

    def forecast_energy_between(
        self,
        start: datetime,
        end: datetime,
        *,
        exclude_charging_windows: bool,
        fallback: FallbackKind,
    ) -> ConsumptionForecast:
        """Return predicted energy in an arbitrary local-time range."""
        if start.tzinfo is None:
            start = start.replace(tzinfo=self._timezone())
        else:
            start = start.astimezone(self._timezone())
        if end.tzinfo is None:
            end = end.replace(tzinfo=self._timezone())
        else:
            end = end.astimezone(self._timezone())
        if _as_timestamp(end) <= _as_timestamp(start):
            return ConsumptionForecast(
                energy_kwh=0.0,
                intervals_kwh=[0.0] * INTERVAL_COUNT,
                source=fallback,
                mature=False,
                fallback_reason="empty_range",
            )

        segments = _local_segments(start, end)
        usable_segments: list[tuple[float, float, datetime]] = []
        for segment_start, segment_end, midpoint in segments:
            local_date = midpoint.date()
            if exclude_charging_windows and (
                not self._day_has_operating_slot(local_date)
                or self._slot_active(midpoint)
            ):
                continue
            usable_segments.append((segment_start, segment_end, midpoint))

        interval_indices_by_date: dict[date, set[int]] = {}
        for _, _, midpoint in usable_segments:
            interval_indices_by_date.setdefault(midpoint.date(), set()).add(
                _interval_index(midpoint.timetz().replace(tzinfo=None))
            )

        forecasts: dict[date, ConsumptionForecast] = {}
        aggregate_intervals = [0.0] * INTERVAL_COUNT
        energy = 0.0
        expected_seconds = 0.0
        profile_seconds = 0.0
        mature = True
        sources: set[str] = set()
        reasons: list[str] = []
        weekday_samples: list[int] = []
        day_type_samples: list[int] = []
        total_days = 0
        newest: date | None = None
        fallback_scales: dict[date, float] = {}
        for segment_start, segment_end, midpoint in usable_segments:
            local_date = midpoint.date()
            forecast = forecasts.get(local_date)
            if forecast is None:
                forecast = self.forecast_for_date(
                    local_date,
                    fallback=fallback,
                    interval_indices=interval_indices_by_date.get(local_date),
                )
                forecasts[local_date] = forecast
                mature = mature and forecast.mature
                sources.add(forecast.source)
                if forecast.fallback_reason:
                    reasons.append(forecast.fallback_reason)
                weekday_samples.append(forecast.weekday_samples)
                day_type_samples.append(forecast.day_type_samples)
                total_days = max(total_days, forecast.total_days)
                if forecast.newest_profile_date is not None:
                    newest = (
                        forecast.newest_profile_date
                        if newest is None
                        else max(newest, forecast.newest_profile_date)
                    )

            seconds = segment_end - segment_start
            index = _interval_index(midpoint.timetz().replace(tzinfo=None))
            scale = fallback_scales.get(local_date)
            if scale is None:
                scale = self._legacy_fallback_day_scale(local_date, forecast)
                fallback_scales[local_date] = scale
            portion = (
                forecast.intervals_kwh[index]
                * seconds
                / INTERVAL_SECONDS
                * scale
            )
            energy += max(0.0, portion)
            aggregate_intervals[index] += max(0.0, portion)
            expected_seconds += seconds
            if forecast.source == "profile":
                profile_seconds += seconds

        source = "profile" if sources == {"profile"} else fallback
        coverage_ratio = profile_seconds / expected_seconds if expected_seconds > 0 else 0.0
        return ConsumptionForecast(
            energy_kwh=energy,
            intervals_kwh=aggregate_intervals,
            source=source,
            mature=mature and source == "profile",
            coverage_ratio=coverage_ratio,
            weekday_samples=min(weekday_samples) if weekday_samples else 0,
            day_type_samples=min(day_type_samples) if day_type_samples else 0,
            total_days=total_days,
            fallback_reason=";".join(dict.fromkeys(reasons)) or None,
            profile_age_days=(self._today() - newest).days if newest else None,
            newest_profile_date=newest,
        )

    # ------------------------------------------------------------------
    # Recorder backfill
    # ------------------------------------------------------------------

    async def async_backfill_from_recorder(self) -> bool:
        """Populate missing raw intervals with one query per source.

        Recorder is optional.  This method is intentionally best-effort and can
        be run in a background task after startup without affecting control.
        """
        try:
            from homeassistant.components.recorder import get_instance, history
        except ImportError:
            self._last_error = "backfill: recorder unavailable"
            return False

        source_entity = getattr(self._controller, "home_consumption_sensor", None)
        if not source_entity:
            resolver = getattr(self._controller, "_consumption_tracker", None)
            resolver = getattr(resolver, "_home_consumption_entity_id", None)
            if callable(resolver):
                try:
                    source_entity = resolver()
                except Exception:  # noqa: BLE001
                    source_entity = None
        if not source_entity:
            self._last_error = "backfill: home consumption entity unavailable"
            return False

        today = self._today()
        local_tz = self._timezone()
        start_time = datetime.combine(
            today - timedelta(days=PROFILE_RETENTION_DAYS),
            time.min,
            tzinfo=local_tz,
        )
        end_time = self._now()
        try:
            recorder = get_instance(self._hass)
            home_states_map = await recorder.async_add_executor_job(
                history.state_changes_during_period,
                self._hass,
                start_time,
                end_time,
                source_entity,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self._last_error = f"backfill: {exc}"
            _LOGGER.warning("Consumption profile: Recorder backfill failed: %s", exc)
            return False

        try:
            home_days = await recorder.async_add_executor_job(
                _series_to_bins,
                (home_states_map or {}).get(source_entity, []),
                local_tz,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self._last_error = f"backfill: binning failed: {exc}"
            _LOGGER.warning("Consumption profile: home backfill binning failed: %s", exc)
            return False
        if not home_days:
            self._last_error = "backfill: no home consumption states"
            return False

        external_days: list[tuple[dict[str, Any], dict[date, ProfileDay]]] = []
        config_data = getattr(self._config_entry, "data", {}) or {}
        for device in config_data.get("excluded_devices", []) or []:
            if not isinstance(device, dict) or not device.get("enabled", True):
                continue
            if device.get("ev_charger_no_telemetry", False):
                continue
            sensor = device.get("power_sensor")
            if not sensor:
                continue
            try:
                states_map = await recorder.async_add_executor_job(
                    history.state_changes_during_period,
                    self._hass,
                    start_time,
                    end_time,
                    sensor,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                _LOGGER.debug("Consumption profile: external backfill failed for %s: %s", sensor, exc)
                continue
            try:
                device_days = await recorder.async_add_executor_job(
                    _series_to_bins,
                    (states_map or {}).get(sensor, []),
                    local_tz,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                _LOGGER.debug(
                    "Consumption profile: external backfill binning failed for %s: %s",
                    sensor,
                    exc,
                )
                continue
            external_days.append((device, device_days))

        changed = False
        for local_date, home_day in home_days.items():
            if local_date > today or local_date < self._retention_floor(today):
                continue
            adjusted = ProfileDay(
                local_date,
                list(home_day.energy_kwh),
                list(home_day.coverage_s),
                complete=local_date < today,
            )
            for device, device_days in external_days:
                device_day = device_days.get(local_date)
                if device_day is None:
                    continue
                if device.get("included_in_consumption", True):
                    try:
                        exclusion_factor = max(
                            0.0,
                            min(100.0, float(device.get("exclusion_pct", 100.0))),
                        ) / 100.0
                    except (TypeError, ValueError):
                        exclusion_factor = 1.0
                    sign = -exclusion_factor
                else:
                    sign = 1.0
                _apply_external_load_to_day(adjusted, device_day, sign)
            before = self._days.get(local_date)
            for index in range(INTERVAL_COUNT):
                if (
                    before is None
                    or adjusted.coverage_s[index] > before.coverage_s[index]
                ):
                    if before is None:
                        before = ProfileDay(local_date)
                        self._days[local_date] = before
                    before.energy_kwh[index] = adjusted.energy_kwh[index]
                    before.coverage_s[index] = adjusted.coverage_s[index]
                    before.complete = before.complete or adjusted.complete
                    changed = True

        if changed:
            self._prune(today)
            await self.async_save()
        self._last_error = None if changed else "backfill: no better intervals"
        return changed

    def start_backfill(self) -> None:
        """Start at most one background Recorder backfill."""
        if self._backfill_task is not None and not self._backfill_task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._backfill_task = None
            return
        self._backfill_task = loop.create_task(self.async_backfill_from_recorder())

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    def diagnostics(self, target_date: date | None = None) -> dict[str, Any]:
        """Return bounded support information suitable for diagnostics."""
        target_date = target_date or self._today()
        forecast = self.forecast_for_date(target_date)
        coverage_by_day = {
            local_date.isoformat(): round(
                sum(day.coverage_s) / (INTERVAL_COUNT * INTERVAL_SECONDS),
                3,
            )
            for local_date, day in sorted(self._days.items())
        }
        return {
            "interval_minutes": INTERVAL_MINUTES,
            "retention_days": PROFILE_RETENTION_DAYS,
            "available_dates": [local_date.isoformat() for local_date in sorted(self._days)],
            "coverage_by_day": coverage_by_day,
            "valid_days": len(self._usable_days()),
            "weekday": sum(day.local_date.weekday() < 5 for day in self._usable_days()),
            "weekend": sum(day.local_date.weekday() >= 5 for day in self._usable_days()),
            "target_date": target_date.isoformat(),
            "source": forecast.source,
            "mature": forecast.mature,
            "coverage_ratio": round(forecast.coverage_ratio, 3),
            "weekday_samples": forecast.weekday_samples,
            "day_type_samples": forecast.day_type_samples,
            "total_profile_days": forecast.total_days,
            "newest_profile_date": (
                forecast.newest_profile_date.isoformat()
                if forecast.newest_profile_date
                else None
            ),
            "fallback_reason": forecast.fallback_reason,
            "last_error": self._last_error,
        }

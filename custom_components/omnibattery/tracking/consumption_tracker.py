"""Consumption history, energy accumulators and solar timing for Marstek Venus.

Owns:
- Persistent stores for consumption history, household/solar accumulators and solar T_start
- Daily 23:55 (local) capture of derived home consumption
- Startup backfill from recorder history
- Real-time accumulation of home consumption
- Solar T_start detection plus astronomical sunrise/T_end estimation

Reads/writes the controller's existing public attributes for backward
compatibility with sensors and binary_sensors that read those attrs directly:
    _daily_consumption_history, _daily_grid_at_min_soc_kwh, _grid_at_min_soc_sensor,
    _household_energy_accumulator, _household_accumulator_date, _solar_t_start.
"""
from __future__ import annotations

import asyncio
import logging
import math
from datetime import date, datetime, time, timedelta
from time import monotonic
from typing import TYPE_CHECKING, Any, Optional

from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from ..const import DEFAULT_BASE_CONSUMPTION_KWH, DOMAIN
from .consumption_profile import ConsumptionForecast, ConsumptionProfileTracker, INTERVAL_COUNT
from .solar_profile import SolarProfileTracker

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.config_entries import ConfigEntry

_LOGGER = logging.getLogger(__name__)

CONSUMPTION_HISTORY_SCOPE = "full_day_home"
VACATION_NIGHT_START = time(1, 0)
VACATION_NIGHT_END = time(5, 0)
VACATION_NIGHT_SECONDS = 4 * 3600
VACATION_NIGHT_MIN_COVERAGE_S = VACATION_NIGHT_SECONDS * 0.75
VACATION_NIGHT_SAMPLE_GAP_S = 5 * 60

# Grid, solar and battery telemetry are published independently. During a
# battery charge, a short-lived mismatch can make the derived household
# balance negative or implausibly small even though the house is still using
# power. The display sensor keeps its last coherent value during that mismatch;
# the tracker below remains strict so invalid samples are not integrated into
# daily energy totals.
HOME_CONSUMPTION_HOLD_S = 15.0
HOME_CONSUMPTION_MIN_BALANCE_W = 20.0


def coordinator_ac_power_w(coordinator: Any) -> float | None:
    """Return a coordinator's signed AC power in watts.

    Marstek coordinators expose ``ac_power`` directly.  Registerless drivers
    expose ``battery_power`` with the opposite sign, so use the same fallback
    convention as the aggregate Home Consumption sensor.
    """
    data = getattr(coordinator, "data", None)
    if not data:
        return None
    value = data.get("ac_power")
    if value is None:
        battery_power = data.get("battery_power")
        if battery_power is None:
            return None
        value = -battery_power
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def has_battery_charging(coordinators: Any) -> bool:
    """Return whether an available battery is currently charging."""
    for coordinator in coordinators or ():
        if getattr(coordinator, "is_available", True) is False:
            continue
        ac_power_w = coordinator_ac_power_w(coordinator)
        if ac_power_w is not None and ac_power_w < -1.0:
            return True
    return False


def home_balance_is_suspicious(
    balance_w: float,
    *,
    battery_charging: bool,
    last_valid_w: float | None,
) -> bool:
    """Identify an impossible or transiently collapsed home-power balance.

    A positive low load can be legitimate when no battery is charging. While
    charging, only a balance below the small absolute floor is considered
    suspicious. A relative-to-previous-value threshold is intentionally avoided:
    real household demand can change by more than half between samples, and
    rejecting that change turns a valid positive reading into ``unknown``.
    """
    if not math.isfinite(balance_w) or balance_w <= 0.0:
        return True
    if not battery_charging:
        return False

    return balance_w < HOME_CONSUMPTION_MIN_BALANCE_W


class ConsumptionTracker:
    """Manages consumption history, accumulators and solar timing."""

    def __init__(
        self,
        hass: "HomeAssistant",
        config_entry: "ConfigEntry",
        controller: Any,
    ) -> None:
        self._hass = hass
        self._controller = controller
        self._config_entry = config_entry

        # Persistent stores
        self._consumption_store: Store = Store(
            hass, 1, f"{DOMAIN}_consumption_history"
        )
        self._solar_t_start_store: Store = Store(
            hass, 1, f"{DOMAIN}.{config_entry.entry_id}.solar_t_start"
        )
        self._accumulator_store: Store = Store(
            hass, 1, f"{DOMAIN}.{config_entry.entry_id}.accumulators"
        )
        self._daily_energy_store: Store = Store(
            hass, 1, f"{DOMAIN}.{config_entry.entry_id}.daily_energy"
        )
        self._vacation_store: Store = Store(
            hass, 1, f"{DOMAIN}.{config_entry.entry_id}.vacation_learning"
        )
        # [{"start": ISO-8601, "end": ISO-8601|None}], retained so a later
        # Recorder rebuild can never put absent-period data back into training.
        self._vacation_periods: list[dict[str, str | None]] = []
        self._vacation_nights: list[dict[str, float | str]] = []
        self._vacation_last_sample_time: datetime | None = None
        self._vacation_last_sample_mono: float | None = None
        self._vacation_last_power_kw: float | None = None

        # The legacy seven-day total history remains owned by this tracker for
        # compatibility.  The quarter-hour profile is deliberately isolated in
        # its own Store and learns the same adjusted home demand over 24 hours.
        self._consumption_profile = ConsumptionProfileTracker(
            hass,
            config_entry,
            controller,
            fallback_daily_kwh=self.get_avg_daily_consumption,
        )
        # Public alias for diagnostics and consumers that do not need to know
        # which legacy tracker owns the input derivation.
        self.consumption_profile = self._consumption_profile
        self._solar_profile = SolarProfileTracker(hass, config_entry, controller)
        self.solar_profile = self._solar_profile

        # Transient state (not exposed to sensors)
        self._household_last_accumulation_time: Optional[float] = None
        self._daily_solar_last_time: Optional[float] = None
        self._daily_home_last_time: Optional[float] = None
        self._daily_grid_last_time: Optional[float] = None
        # Previous power sample (kW) for trapezoidal integration of the daily totals
        self._daily_solar_last_power_kw: Optional[float] = None
        self._daily_home_last_power_kw: Optional[float] = None
        self._daily_grid_last_power_kw: Optional[float] = None
        self._last_valid_home_power_kw: Optional[float] = None
        self._last_valid_home_power_monotonic: Optional[float] = None
        self._last_valid_raw_home_power_kw: Optional[float] = None
        self._last_valid_raw_home_power_monotonic: Optional[float] = None
        self._grid_at_min_soc_last_save_mono: float = 0.0
        self._accumulator_last_save_monotonic: float = 0.0
        self._solar_noon_cache: Optional[tuple[date, float]] = None

    async def load_consumption_profile(self) -> bool:
        """Restore the independent quarter-hour profile Store."""
        return await self._consumption_profile.async_load()

    async def load_vacation_state(self) -> None:
        """Restore exclusion periods and valid overnight baseline observations."""
        try:
            data = await self._vacation_store.async_load() or {}
            periods = data.get("periods", [])
            nights = data.get("nights", [])
            self._vacation_periods = [
                {"start": str(item["start"]), "end": item.get("end")}
                for item in periods if isinstance(item, dict) and item.get("start")
            ]
            self._vacation_nights = [
                {"date": str(item["date"]), "energy_kwh": float(item["energy_kwh"]),
                 "coverage_s": float(item["coverage_s"])}
                for item in nights if isinstance(item, dict) and item.get("date")
                and float(item.get("coverage_s", 0)) >= VACATION_NIGHT_MIN_COVERAGE_S
            ][-30:]
            self._sync_profile_vacation_exclusions()
        except (TypeError, ValueError) as exc:
            _LOGGER.warning("Could not restore vacation learning state: %s", exc)

    async def _save_vacation_state(self) -> None:
        await self._vacation_store.async_save({
            "periods": self._vacation_periods,
            "nights": self._vacation_nights[-30:],
        })

    @staticmethod
    def _as_aware(value: str | datetime | None) -> datetime | None:
        if value is None:
            return None
        if isinstance(value, datetime):
            return value
        try:
            return datetime.fromisoformat(value)
        except (TypeError, ValueError):
            return None

    def _period_intersects(self, start: datetime, end: datetime) -> bool:
        """Whether an interval overlaps a persisted or current vacation."""
        for item in self._vacation_periods:
            period_start = self._as_aware(item.get("start"))
            period_end = self._as_aware(item.get("end"))
            if period_start is None:
                continue
            zone = start.tzinfo or period_start.tzinfo or dt_util.UTC
            if start.tzinfo is None:
                start = start.replace(tzinfo=zone)
                end = end.replace(tzinfo=zone)
            if period_start.tzinfo is None:
                period_start = period_start.replace(tzinfo=zone)
            if period_end is not None and period_end.tzinfo is None:
                period_end = period_end.replace(tzinfo=zone)
            if period_start < end and (period_end is None or period_end > start):
                return True
        return False

    def _sync_profile_vacation_exclusions(self) -> None:
        setter = getattr(self._consumption_profile, "set_excluded_periods", None)
        if callable(setter):
            setter(self._vacation_periods)

    def is_vacation_active(self) -> bool:
        return bool(getattr(self._controller, "vacation_mode_enabled", False))

    async def async_set_vacation_mode(self, enabled: bool) -> None:
        """Record a toggle boundary and break both learning integrators."""
        now = dt_util.now()
        if enabled:
            if not self._vacation_periods or self._vacation_periods[-1].get("end"):
                self._vacation_periods.append({"start": now.isoformat(), "end": None})
            # A vacation affects the complete legacy day even if it began late.
            self._controller._daily_consumption_history = [
                (day, energy) for day, energy in self._controller._daily_consumption_history
                if day != now.date()
            ]
        elif self._vacation_periods and self._vacation_periods[-1].get("end") is None:
            self._vacation_periods[-1]["end"] = now.isoformat()
        self._household_last_accumulation_time = None
        self._vacation_last_sample_time = None
        self._vacation_last_sample_mono = None
        self._vacation_last_power_kw = None
        self._consumption_profile.record_power_sample(None, local_time=now)
        self._sync_profile_vacation_exclusions()
        await self.save_consumption_history()
        await self._save_vacation_state()

    def _vacation_baseline_kw(self) -> tuple[float, str]:
        """Return median valid-night load, then prior profile, history, default."""
        valid = [item for item in self._vacation_nights
                 if float(item.get("coverage_s", 0.0)) >= VACATION_NIGHT_MIN_COVERAGE_S]
        values = [float(item["energy_kwh"]) / 4.0 for item in valid[-3:]]
        if values:
            values.sort()
            return values[len(values) // 2], "vacation_night_median"
        try:
            today = dt_util.now().date()
            midnight = datetime.combine(today, time.min, tzinfo=dt_util.now().tzinfo)
            prior = self._consumption_profile.forecast_energy_between(
                midnight + timedelta(hours=1), midnight + timedelta(hours=5),
                exclude_charging_windows=False, fallback="legacy_daily",
            )
            if prior.source == "profile" and prior.energy_kwh > 0:
                return prior.energy_kwh / 4.0, "prior_night_profile"
        except Exception:  # noqa: BLE001
            pass
        history = [energy for day, energy in self._controller._daily_consumption_history
                   if not self._period_intersects(
                       datetime.combine(day, time.min, tzinfo=dt_util.now().tzinfo),
                       datetime.combine(day + timedelta(days=1), time.min, tzinfo=dt_util.now().tzinfo))]
        if history:
            return sum(history) / len(history) / 24.0, "daily_history"
        return DEFAULT_BASE_CONSUMPTION_KWH / 24.0, "default"

    def vacation_diagnostics(self) -> dict[str, Any]:
        baseline_kw, source = self._vacation_baseline_kw()
        return {
            "active": self.is_vacation_active(),
            "baseline_kw": round(baseline_kw, 4),
            "baseline_daily_kwh": round(baseline_kw * 24.0, 3),
            "baseline_source": source,
            "valid_nights": sum(float(item.get("coverage_s", 0.0)) >= VACATION_NIGHT_MIN_COVERAGE_S for item in self._vacation_nights),
            "night_window": "01:00-05:00",
            "min_coverage_hours": 3.0,
            "excluded_periods": list(self._vacation_periods),
        }

    def forecast_consumption_between(self, start: datetime, end: datetime, *, fallback: str = "legacy_daily") -> ConsumptionForecast:
        """Single forecast API: learned profile normally, constant baseline away."""
        if not self.is_vacation_active():
            return self._consumption_profile.forecast_energy_between(
                start, end, exclude_charging_windows=False, fallback=fallback
            )
        seconds = max(0.0, end.timestamp() - start.timestamp())
        baseline_kw, source = self._vacation_baseline_kw()
        energy = baseline_kw * seconds / 3600.0
        intervals = [baseline_kw * 0.25] * INTERVAL_COUNT
        return ConsumptionForecast(energy, intervals, "vacation_baseline", False,
            fallback_reason=source)

    def forecast_consumption_for_date(self, target_date: date, *, fallback: str = "legacy_daily") -> ConsumptionForecast:
        now = dt_util.now()
        midnight = datetime.combine(target_date, time.min, tzinfo=now.tzinfo)
        return self.forecast_consumption_between(midnight, midnight + timedelta(days=1), fallback=fallback)

    def start_consumption_profile_backfill(self) -> None:
        """Start the non-blocking Recorder backfill for the quarter-hour profile."""
        self._consumption_profile.start_backfill()

    async def load_solar_profile(self) -> bool:
        """Restore the isolated direct-PV temporal profile Store."""
        if self._solar_profile.mode == "off":
            return False
        return await self._solar_profile.async_load()

    def start_solar_profile_backfill(self) -> None:
        """Start best-effort direct-power Recorder backfill."""
        if self._solar_profile.mode == "off":
            return
        self._solar_profile.start_backfill()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    async def save_consumption_history(self) -> None:
        """Persist consumption history to disk via HA Store."""
        try:
            data = {
                "consumption_scope": CONSUMPTION_HISTORY_SCOPE,
                "history": [
                    (d.isoformat(), c)
                    for d, c in self._controller._daily_consumption_history
                ],
                "grid_at_min_soc_kwh": self._controller._daily_grid_at_min_soc_kwh,
            }
            await self._consumption_store.async_save(data)
        except Exception as e:
            _LOGGER.error("Failed to save consumption history: %s", e)

    async def load_consumption_history(self) -> bool:
        """Load consumption history from HA Store. Returns True if data was loaded."""
        try:
            data = await self._consumption_store.async_load()
            if data and "grid_at_min_soc_kwh" in data:
                # This accumulator has its own measurement scope and remains
                # valid when legacy windowed household history is invalidated.
                self._controller._daily_grid_at_min_soc_kwh = round(
                    float(data["grid_at_min_soc_kwh"]), 2
                )
                _LOGGER.info(
                    "Loaded grid-at-min-soc accumulator from store: %.2f kWh",
                    self._controller._daily_grid_at_min_soc_kwh,
                )
            if data and "history" in data and data["history"]:
                if data.get("consumption_scope") != CONSUMPTION_HISTORY_SCOPE:
                    # Historical versions excluded predictive grid-charging
                    # windows and even whole weekdays not selected by a window.
                    # Those totals cannot be mixed with the new 24-hour contract.
                    # Returning True prevents restoration from the equally stale
                    # entity attributes; setup seeds placeholders and Recorder
                    # backfill replaces them with complete daily totals.
                    self._controller._daily_consumption_history = []
                    _LOGGER.info(
                        "Discarded legacy windowed consumption history; "
                        "the last seven days will be rebuilt as full-day totals"
                    )
                    return True
                self._controller._daily_consumption_history = [
                    (date.fromisoformat(date_str), round(consumption, 2))
                    for date_str, consumption in data["history"]
                ]
                local_tz = dt_util.now().tzinfo
                self._controller._daily_consumption_history = [
                    (day, consumption)
                    for day, consumption in self._controller._daily_consumption_history
                    if not self._period_intersects(
                        datetime.combine(day, time.min, tzinfo=local_tz),
                        datetime.combine(day + timedelta(days=1), time.min, tzinfo=local_tz),
                    )
                ]
                history = self._controller._daily_consumption_history
                _LOGGER.info(
                    "Loaded consumption history from store: %d days (oldest: %s, newest: %s)",
                    len(history),
                    history[0][0] if history else "N/A",
                    history[-1][0] if history else "N/A",
                )
                return True
            _LOGGER.debug("No consumption history found in store")
            return False
        except Exception as e:
            _LOGGER.warning("Failed to load consumption history from store: %s", e)
            return False

    def save_solar_t_start(self) -> None:
        """Fire-and-forget: persist solar_t_start alongside today's date."""
        asyncio.create_task(self._solar_t_start_store.async_save({
            "date": date.today().isoformat(),
            "t_start": self._controller._solar_t_start,
        }))

    async def load_solar_t_start(self) -> None:
        """Restore solar_t_start from storage if it was captured today."""
        try:
            data = await self._solar_t_start_store.async_load()
            if not data:
                return
            if data.get("date") == date.today().isoformat() and data.get("t_start") is not None:
                self._controller._solar_t_start = data["t_start"]
                _LOGGER.info(
                    "Charge Delay: Restored solar T_start=%.2fh from storage (HA restart)",
                    self._controller._solar_t_start,
                )
        except Exception as e:
            _LOGGER.error("Charge Delay: Failed to load solar T_start from storage: %s", e)

    def save_accumulators(self) -> None:
        """Fire-and-forget: persist household and solar accumulators to storage."""
        asyncio.create_task(self.async_save_accumulators())

    async def async_save_accumulators(self) -> None:
        """Await-able persist of the home-consumption accumulator (used on unload).

        The accumulator holds adjusted derived home consumption for the full
        local day.
        """
        ctrl = self._controller
        try:
            await self._accumulator_store.async_save({
                "consumption_scope": CONSUMPTION_HISTORY_SCOPE,
                "date": ctrl._household_accumulator_date.isoformat() if ctrl._household_accumulator_date else None,
                "household_kwh": round(ctrl._household_energy_accumulator, 4),
            })
        except Exception as e:
            _LOGGER.error("Failed to save accumulators: %s", e)

    async def load_accumulators(self) -> None:
        """Restore the home-consumption accumulator from storage (today's value only)."""
        try:
            data = await self._accumulator_store.async_load()
            if not data:
                return
            stored_date_str = data.get("date")
            if not stored_date_str or stored_date_str != date.today().isoformat():
                return
            today = date.today()
            ctrl = self._controller
            if data.get("consumption_scope") != CONSUMPTION_HISTORY_SCOPE:
                # The old same-day accumulator may exclude hours inside a
                # predictive charging window. Rebuild today's elapsed total so
                # an upgrade/reload does not produce one more partial day.
                rebuilt = await self.backfill_home_from_history(today)
                if rebuilt is None:
                    _LOGGER.warning(
                        "Could not rebuild today's legacy windowed consumption "
                        "accumulator from Recorder; starting a new full-day total"
                    )
                    return
                ctrl._household_energy_accumulator = rebuilt
                ctrl._household_accumulator_date = today
                _LOGGER.info(
                    "Rebuilt today's home-consumption accumulator from Recorder: "
                    "%.2f kWh",
                    rebuilt,
                )
                await self.async_save_accumulators()
                return
            ctrl._household_energy_accumulator = float(data.get("household_kwh", 0.0))
            ctrl._household_accumulator_date = today
            _LOGGER.info(
                "Restored home-consumption accumulator from storage: %.2f kWh",
                ctrl._household_energy_accumulator,
            )
        except Exception as e:
            _LOGGER.warning("Failed to load accumulators from storage: %s", e)

    def save_daily_energy(self) -> None:
        """Fire-and-forget: persist the exact daily solar/home/grid energy totals."""
        asyncio.create_task(self.async_save_daily_energy())

    def capture_daily_solar_forecast(self, forecast_kwh: Any) -> bool:
        """Keep the first full-day solar forecast observed for the local day.

        The live forecast is deliberately allowed to change during the day,
        but the dashboard also needs a stable reference from the daily 00:05
        evaluation. Repeated evaluations on the same day never overwrite the
        first valid value.
        """
        try:
            value = float(forecast_kwh)
        except (TypeError, ValueError):
            return False
        if not math.isfinite(value) or value < 0.0:
            return False

        today = date.today()
        ctrl = self._controller
        if (
            ctrl._daily_solar_forecast_initial_date == today
            and ctrl._daily_solar_forecast_initial_kwh is not None
        ):
            return False

        ctrl._daily_solar_forecast_initial_date = today
        ctrl._daily_solar_forecast_initial_kwh = round(value, 4)
        self.save_daily_energy()
        return True

    async def async_save_daily_energy(self) -> None:
        """Await-able persist of the daily energy totals (used on unload)."""
        ctrl = self._controller
        # The grid meter (consumption_sensor) is always configured, so this always
        # has something worth saving (import/export); the date is keyed to today.
        try:
            await self._daily_energy_store.async_save({
                "date": date.today().isoformat(),
                "solar_kwh": round(ctrl._daily_solar_energy_kwh, 4),
                "home_kwh": round(ctrl._daily_home_energy_kwh, 4),
                "grid_import_kwh": round(ctrl._daily_grid_import_energy_kwh, 4),
                "grid_export_kwh": round(ctrl._daily_grid_export_energy_kwh, 4),
                "solar_forecast_initial_kwh": ctrl._daily_solar_forecast_initial_kwh,
                "solar_forecast_initial_date": (
                    ctrl._daily_solar_forecast_initial_date.isoformat()
                    if ctrl._daily_solar_forecast_initial_date is not None
                    else None
                ),
            })
        except Exception as e:
            _LOGGER.error("Failed to save daily energy: %s", e)

    async def load_daily_energy(self) -> None:
        """Restore the daily solar/home/grid energy totals (today's values only)."""
        ctrl = self._controller
        try:
            data = await self._daily_energy_store.async_load()
            if not data or data.get("date") != date.today().isoformat():
                return
            today = date.today()
            ctrl._daily_solar_energy_kwh = float(data.get("solar_kwh", 0.0))
            ctrl._daily_solar_energy_date = today
            ctrl._daily_home_energy_kwh = float(data.get("home_kwh", 0.0))
            ctrl._daily_home_energy_date = today
            ctrl._daily_grid_import_energy_kwh = float(data.get("grid_import_kwh", 0.0))
            ctrl._daily_grid_export_energy_kwh = float(data.get("grid_export_kwh", 0.0))
            ctrl._daily_grid_energy_date = today
            initial_date = data.get("solar_forecast_initial_date")
            initial_value = data.get("solar_forecast_initial_kwh")
            if initial_date == today.isoformat() and initial_value is not None:
                try:
                    value = float(initial_value)
                except (TypeError, ValueError):
                    value = None
                if value is not None and math.isfinite(value) and value >= 0.0:
                    ctrl._daily_solar_forecast_initial_kwh = value
                    ctrl._daily_solar_forecast_initial_date = today
            _LOGGER.info(
                "Restored daily energy totals from storage: solar=%.2f kWh, home=%.2f kWh, "
                "grid import=%.2f kWh, grid export=%.2f kWh",
                ctrl._daily_solar_energy_kwh, ctrl._daily_home_energy_kwh,
                ctrl._daily_grid_import_energy_kwh, ctrl._daily_grid_export_energy_kwh,
            )
        except Exception as e:
            _LOGGER.warning("Failed to load daily energy from storage: %s", e)

    # ------------------------------------------------------------------
    # Exact daily energy totals (real power sensors, full day)
    # ------------------------------------------------------------------

    def handle_daily_energy_reset(self) -> None:
        """Reset the exact daily solar/home totals at local-midnight rollover."""
        ctrl = self._controller
        today = date.today()
        if ctrl._daily_solar_energy_date != today:
            if ctrl._daily_solar_energy_date is not None:
                _LOGGER.info(
                    "Daily solar energy reset (was %.2f kWh for %s)",
                    ctrl._daily_solar_energy_kwh, ctrl._daily_solar_energy_date,
                )
            ctrl._daily_solar_energy_kwh = 0.0
            self._daily_solar_last_time = None
            self._daily_solar_last_power_kw = None
            ctrl._daily_solar_energy_date = today
        if ctrl._daily_home_energy_date != today:
            if ctrl._daily_home_energy_date is not None:
                _LOGGER.info(
                    "Daily home energy reset (was %.2f kWh for %s)",
                    ctrl._daily_home_energy_kwh, ctrl._daily_home_energy_date,
                )
            ctrl._daily_home_energy_kwh = 0.0
            self._daily_home_last_time = None
            self._daily_home_last_power_kw = None
            ctrl._daily_home_energy_date = today
        if ctrl._daily_grid_energy_date != today:
            if ctrl._daily_grid_energy_date is not None:
                _LOGGER.info(
                    "Daily grid energy reset (import=%.2f export=%.2f kWh for %s)",
                    ctrl._daily_grid_import_energy_kwh,
                    ctrl._daily_grid_export_energy_kwh,
                    ctrl._daily_grid_energy_date,
                )
            ctrl._daily_grid_import_energy_kwh = 0.0
            ctrl._daily_grid_export_energy_kwh = 0.0
            self._daily_grid_last_time = None
            self._daily_grid_last_power_kw = None
            ctrl._daily_grid_energy_date = today
        if ctrl._daily_solar_forecast_initial_date != today:
            ctrl._daily_solar_forecast_initial_kwh = None
            ctrl._daily_solar_forecast_initial_date = today

    def _read_power_kw(self, entity_id: str) -> Optional[float]:
        """Read a power entity and return its value in kW, or None if unusable."""
        state = self._hass.states.get(entity_id)
        if state is None or state.state in ("unknown", "unavailable"):
            return None
        try:
            value = float(state.state)
        except (ValueError, TypeError):
            return None
        if not math.isfinite(value) or value < 0.0:
            return None
        unit = str(state.attributes.get("unit_of_measurement", "W")).strip().lower()
        if unit == "kw":
            return value
        if unit == "w":
            return value / 1000.0
        return None

    def _read_total_solar_power_kw(self) -> Optional[float]:
        """Total instantaneous solar production (kW): external sensor + battery PV.

        Sums the configured external solar_production_sensor (e.g. an APS/ECU
        feed) and the DC-coupled PV reported by every battery. Venus vA/vD
        contributes its MPPT inputs; Anker contributes its official aggregate
        PV value. A battery with panels on its own inputs is counted even when
        no external sensor is configured. Returns None only when no source has
        a usable reading.
        """
        ctrl = self._controller
        total_kw = 0.0
        have_reading = False
        if getattr(ctrl, "solar_production_sensor", None):
            ext_kw = self._read_power_kw(ctrl.solar_production_sensor)
            if ext_kw is not None:
                total_kw += max(0.0, ext_kw)
                have_reading = True
        for coordinator in ctrl.coordinators:
            # Skip disconnected units: their PV readings go stale (merged dict,
            # never expired) and would inflate the integrated daily solar total.
            capabilities = getattr(coordinator, "capabilities", None)
            has_mppt = bool(getattr(capabilities, "has_mppt_pv", False))
            has_aggregate = bool(getattr(capabilities, "has_solar_telemetry", False))
            if not (has_mppt or has_aggregate):
                continue
            if not coordinator.is_available or not coordinator.data:
                continue
            mppt_w = 0.0
            seen = False
            pv_keys = (
                ("mppt1_power", "mppt2_power", "mppt3_power", "mppt4_power")
                if has_mppt
                else ("solar_power",)
            )
            for key in pv_keys:
                value = coordinator.data.get(key)
                try:
                    parsed = float(value)
                except (TypeError, ValueError):
                    continue
                if math.isfinite(parsed) and parsed >= 0.0:
                    mppt_w += parsed
                    seen = True
            if seen:
                total_kw += max(0.0, mppt_w) / 1000.0
                have_reading = True
        return total_kw if have_reading else None

    def _solar_runtime_context(self, forecast_reference_kwh: float | None) -> dict[str, bool]:
        """Return conservative context used to flag possible curtailment."""
        ctrl = self._controller
        solar_coordinators = []
        for coordinator in getattr(ctrl, "coordinators", ()) or ():
            capabilities = getattr(coordinator, "capabilities", None)
            has_mppt = bool(getattr(capabilities, "has_mppt_pv", False))
            has_aggregate = bool(getattr(capabilities, "has_solar_telemetry", False))
            if (
                (has_mppt or has_aggregate)
                and getattr(coordinator, "is_available", False)
                and getattr(coordinator, "data", None)
            ):
                solar_coordinators.append(coordinator)

        battery_full_risk = bool(solar_coordinators)
        for coordinator in solar_coordinators:
            data = coordinator.data or {}
            try:
                soc = float(data.get("battery_soc"))
                max_soc = float(getattr(coordinator, "max_soc", 100.0) or 100.0)
                capacity = float(
                    data.get(
                        "battery_total_energy",
                        getattr(coordinator, "battery_capacity_kwh", 0.0),
                    )
                    or 0.0
                )
            except (AttributeError, TypeError, ValueError):
                battery_full_risk = False
                break
            headroom_kwh = max(0.0, (max_soc - soc) / 100.0 * max(0.0, capacity))
            if soc < max_soc - 0.5 and headroom_kwh > max(0.05, capacity * 0.01):
                battery_full_risk = False
                break

        export_zero = False
        try:
            meter_state = self._hass.states.get(
                getattr(ctrl, "consumption_sensor", None)
            )
            grid_w = ctrl._apply_meter_transform(meter_state)
            export_zero = grid_w is not None and abs(float(grid_w)) <= 50.0
        except (AttributeError, TypeError, ValueError):
            pass

        try:
            reference = float(
                forecast_reference_kwh
                if forecast_reference_kwh is not None
                else getattr(ctrl, "_daily_solar_forecast_initial_kwh", 0.0) or 0.0
            )
        except (TypeError, ValueError):
            reference = 0.0
        explicit = bool(getattr(ctrl, "solar_curtailment_active", False))
        for coordinator in solar_coordinators:
            data = coordinator.data or {}
            explicit = explicit or any(
                bool(data.get(key, False))
                for key in (
                    "curtailment_active",
                    "solar_curtailment",
                    "anti_export_active",
                )
            )
        return {
            "battery_full_risk": battery_full_risk,
            "export_zero": export_zero,
            "expected_high": math.isfinite(reference) and reference >= 0.5,
            "explicit_curtailment": explicit,
        }

    async def accumulate_daily_solar_energy(self) -> None:
        """Integrate total solar production power → exact daily kWh.

        Total = external solar sensor + battery-reported DC-coupled PV.
        Trapezoidal rule: averages the previous and current sample so a ramping
        production curve is not systematically miscounted (left-Riemann bias).
        """
        power_kw = self._read_total_solar_power_kw()
        if power_kw is None:
            self._daily_solar_last_time = None
            self._daily_solar_last_power_kw = None
            if self._solar_profile.mode != "off":
                self._solar_profile.record_power_sample(None)
            return
        power_kw = max(0.0, power_kw)
        # The direct sample is deliberately read once and shared by both the
        # exact daily accumulator and the learned shape tracker.  This avoids
        # a coordinator update between two reads producing divergent totals.
        if self._solar_profile.mode != "off":
            self._solar_profile.update_runtime_context(
                **self._solar_runtime_context(
                    getattr(
                        self._controller,
                        "_daily_solar_forecast_initial_kwh",
                        None,
                    )
                )
            )
            self._solar_profile.record_power_sample(
                power_kw,
                forecast_reference_kwh=getattr(
                    self._controller, "_daily_solar_forecast_initial_kwh", None
                ),
            )
        now = monotonic()
        if self._daily_solar_last_time is not None and self._daily_solar_last_power_kw is not None:
            dt_hours = (now - self._daily_solar_last_time) / 3600.0
            if dt_hours > 0:
                avg_kw = (self._daily_solar_last_power_kw + power_kw) / 2.0
                self._controller._daily_solar_energy_kwh += avg_kw * dt_hours
        self._daily_solar_last_time = now
        self._daily_solar_last_power_kw = power_kw

    def _derive_home_power_kw_unclamped(self) -> Optional[float]:
        """Derive the signed physical home balance in kW.

        Mirrors the aggregate Home Consumption power sensor (home = grid +
        sum(ac_power) + external_solar) so the daily energy total integrates
        exactly what the dashboard power flow shows.
        """
        ctrl = self._controller
        if not ctrl.consumption_sensor:
            return None
        grid_w = ctrl._apply_meter_transform(self._hass.states.get(ctrl.consumption_sensor))
        if grid_w is None:
            return None
        total_kw = grid_w / 1000.0
        for coordinator in ctrl.coordinators:
            # Skip a disconnected battery: coordinator.data keeps its last
            # ac_power (the dict is merged, never expired), so a unit that dies
            # mid-discharge would keep adding a phantom AC contribution while the
            # grid meter already carries its shifted load — double-counting it
            # into home consumption and the integrated daily total.
            if coordinator.is_available and coordinator.data:
                ac = coordinator_ac_power_w(coordinator)
                if ac is not None:
                    total_kw += ac / 1000.0
        if ctrl.solar_production_sensor:
            solar_kw = self._read_power_kw(ctrl.solar_production_sensor)
            if solar_kw is not None:
                total_kw += solar_kw
        return total_kw

    def _derive_home_power_kw(self) -> Optional[float]:
        """Return the physical home balance with its legacy non-negative API."""
        total_kw = self._derive_home_power_kw_unclamped()
        if total_kw is None:
            return None
        # Callers validate a collapsed zero against the last coherent balance.
        # Keep this public helper's long-standing non-negative contract for
        # dashboard/profile consumers.
        return max(0.0, total_kw)

    def _validate_home_power_kw(
        self,
        *,
        raw_power_kw: float,
        candidate_power_kw: float,
        last_value_attr: str,
        last_time_attr: str,
    ) -> Optional[float]:
        """Hold a coherent balance briefly across independently sampled inputs."""
        if not math.isfinite(raw_power_kw) or not math.isfinite(candidate_power_kw):
            return None
        candidate_power_kw = max(0.0, candidate_power_kw)
        last_valid_kw = getattr(self, last_value_attr, None)
        last_valid_at = getattr(self, last_time_attr, None)
        suspicious = home_balance_is_suspicious(
            raw_power_kw * 1000.0,
            battery_charging=has_battery_charging(
                getattr(self._controller, "coordinators", ())
            ),
            last_valid_w=(last_valid_kw * 1000.0 if last_valid_kw is not None else None),
        ) or candidate_power_kw <= 0.0

        now = monotonic()
        if suspicious:
            if (
                last_valid_kw is not None
                and last_valid_at is not None
                and 0.0 <= now - last_valid_at <= HOME_CONSUMPTION_HOLD_S
            ):
                _LOGGER.debug(
                    "Holding last valid home consumption %.0f W after "
                    "suspicious balance %.0f W",
                    last_valid_kw * 1000.0,
                    raw_power_kw * 1000.0,
                )
                return last_valid_kw
            return None

        setattr(self, last_value_attr, candidate_power_kw)
        setattr(self, last_time_attr, now)
        return candidate_power_kw

    def get_validated_home_power_kw(self) -> Optional[float]:
        """Return guarded physical demand without external-load adjustments."""
        raw_power_kw = self._derive_home_power_kw_unclamped()
        if raw_power_kw is None:
            return None
        return self._validate_home_power_kw(
            raw_power_kw=raw_power_kw,
            candidate_power_kw=raw_power_kw,
            last_value_attr="_last_valid_raw_home_power_kw",
            last_time_attr="_last_valid_raw_home_power_monotonic",
        )

    def get_adjusted_home_power_kw(self) -> Optional[float]:
        """Return household demand adjusted for configured external loads.

        This is the input shared by the legacy household-learning accumulator
        and the quarter-hour profile. A short-lived last-valid hold prevents a
        transiently collapsed grid/battery balance from becoming learned demand.
        """
        raw_power_kw = self._derive_home_power_kw_unclamped()
        if raw_power_kw is None:
            return None
        power_kw = raw_power_kw
        external_loads = getattr(self._controller, "_external_loads", None)
        if external_loads is not None:
            try:
                power_kw += float(external_loads.consumption_delta_kw())
            except (AttributeError, TypeError, ValueError):
                pass
        return self._validate_home_power_kw(
            raw_power_kw=raw_power_kw,
            candidate_power_kw=power_kw,
            last_value_attr="_last_valid_home_power_kw",
            last_time_attr="_last_valid_home_power_monotonic",
        )

    async def accumulate_daily_home_energy(self) -> None:
        """Integrate home consumption power → exact daily kWh.

        Derives the same value the power-flow dashboard shows from grid + battery
        AC + solar. Trapezoidal rule averages the previous and current sample so a
        ramping load curve is not systematically miscounted (left-Riemann bias).
        """
        ctrl = self._controller
        # Integrate the physical grid+battery+solar balance shown by the Home
        # Consumption entity. External-load adjustments belong to predictive
        # demand learning and must not change this physical dashboard total.
        # A transient negative balance breaks integration instead of adding a
        # fabricated zero.
        power_kw = self.get_validated_home_power_kw()
        if power_kw is None:
            self._daily_home_last_time = None
            self._daily_home_last_power_kw = None
            return
        now = monotonic()
        if self._daily_home_last_time is not None and self._daily_home_last_power_kw is not None:
            dt_hours = (now - self._daily_home_last_time) / 3600.0
            if dt_hours > 0:
                avg_kw = (self._daily_home_last_power_kw + power_kw) / 2.0
                ctrl._daily_home_energy_kwh += avg_kw * dt_hours
        self._daily_home_last_time = now
        self._daily_home_last_power_kw = power_kw

    async def accumulate_daily_grid_energy(self) -> None:
        """Integrate the net grid meter → exact daily import/export kWh.

        Sign convention of the consumption sensor: positive = importing from the
        grid, negative = exporting to it. Each half integrates separately so the
        panel can show both totals.
        """
        ctrl = self._controller
        # Use the same meter transform as the PD loop so a user-inverted meter
        # (meter_inverted) keeps the +import / -export convention; otherwise the
        # import and export totals would be swapped.
        grid_w = ctrl._apply_meter_transform(self._hass.states.get(ctrl.consumption_sensor))
        if grid_w is None:
            self._daily_grid_last_time = None
            self._daily_grid_last_power_kw = None
            return
        power_kw = grid_w / 1000.0
        now = monotonic()
        # Trapezoidal rule with zero-crossing split: when the meter sign flips
        # between samples (import↔export), the interval is split at the crossing so
        # each half is booked to the correct side instead of misclassifying the
        # whole interval by the start sample's sign.
        if self._daily_grid_last_time is not None and self._daily_grid_last_power_kw is not None:
            dt_hours = (now - self._daily_grid_last_time) / 3600.0
            if dt_hours > 0:
                prev_kw = self._daily_grid_last_power_kw
                curr_kw = power_kw
                if (prev_kw >= 0) == (curr_kw >= 0):
                    kwh = (prev_kw + curr_kw) / 2.0 * dt_hours
                    if kwh >= 0:
                        ctrl._daily_grid_import_energy_kwh += kwh
                    else:
                        ctrl._daily_grid_export_energy_kwh += -kwh
                else:
                    frac = abs(prev_kw) / (abs(prev_kw) + abs(curr_kw))
                    dt_first = dt_hours * frac
                    dt_second = dt_hours - dt_first
                    kwh_first = prev_kw / 2.0 * dt_first
                    kwh_second = curr_kw / 2.0 * dt_second
                    if kwh_first >= 0:
                        ctrl._daily_grid_import_energy_kwh += kwh_first
                    else:
                        ctrl._daily_grid_export_energy_kwh += -kwh_first
                    if kwh_second >= 0:
                        ctrl._daily_grid_import_energy_kwh += kwh_second
                    else:
                        ctrl._daily_grid_export_energy_kwh += -kwh_second
        self._daily_grid_last_time = now
        self._daily_grid_last_power_kw = power_kw

    # ------------------------------------------------------------------
    # Consumption history queries
    # ------------------------------------------------------------------

    def get_avg_daily_consumption(self) -> float:
        """Get average daily consumption from history, with fallback."""
        if self.is_vacation_active():
            return self._vacation_baseline_kw()[0] * 24.0
        history = self._controller._daily_consumption_history
        if history:
            total = sum(c for _, c in history)
            return total / len(history)
        return DEFAULT_BASE_CONSUMPTION_KWH

    async def get_dynamic_base_consumption(self) -> float:
        """Get dynamic base consumption from the 7-day average of daily home consumption.

        Daily values are captured at 23:55 from the full-day home-energy
        accumulator; this method opportunistically backfills missing days from
        the Home Consumption sensor's recorder history.
        """
        ctrl = self._controller

        if self.is_vacation_active():
            baseline_kw, _source = self._vacation_baseline_kw()
            return baseline_kw * 24.0

        # OPPORTUNISTIC BACKFILL: Replace default entries with real data from HA history
        # This recovers real data after restarts or when defaults were pre-populated.
        # Window = the 7 most recent calendar days.
        # Gate on <7 real entries so a permanently unfillable day (no recorder
        # data) isn't re-queried on every predictive evaluation.
        real_data_dates = {
            d for d, c in ctrl._daily_consumption_history if c != DEFAULT_BASE_CONSUMPTION_KWH
        }
        if len(real_data_dates) < 7:
            for past_date in self._recent_history_days(7):
                local_tz = dt_util.now().tzinfo
                start = datetime.combine(past_date, time.min, tzinfo=local_tz)
                if self._period_intersects(start, start + timedelta(days=1)):
                    continue
                if past_date not in real_data_dates:
                    value = await self.backfill_home_from_history(past_date)
                    if value is not None and value >= 1.5:
                        replaced = False
                        for i, (d, c) in enumerate(ctrl._daily_consumption_history):
                            if d == past_date:
                                ctrl._daily_consumption_history[i] = (past_date, value)
                                replaced = True
                                break
                        if not replaced:
                            ctrl._daily_consumption_history.append((past_date, value))
                        ctrl._daily_consumption_history.sort(key=lambda x: x[0])
                        ctrl._daily_consumption_history = ctrl._daily_consumption_history[-7:]
                    await asyncio.sleep(0.1)  # Small delay between history queries

        # Calculate average from history
        if len(ctrl._daily_consumption_history) == 0:
            _LOGGER.warning(
                "No consumption history, using fallback: %.1f kWh",
                DEFAULT_BASE_CONSUMPTION_KWH,
            )
            return DEFAULT_BASE_CONSUMPTION_KWH

        total = sum(consumption for _, consumption in ctrl._daily_consumption_history)
        average = total / len(ctrl._daily_consumption_history)

        if average <= 0:
            _LOGGER.warning(
                "Average consumption is 0, using fallback: %.1f kWh",
                DEFAULT_BASE_CONSUMPTION_KWH,
            )
            return DEFAULT_BASE_CONSUMPTION_KWH

        real_count = sum(
            1 for _, c in ctrl._daily_consumption_history if c != DEFAULT_BASE_CONSUMPTION_KWH
        )
        source = "grid + battery AC + solar"
        _LOGGER.info(
            "Dynamic base consumption: %.1f kWh (avg of %d days, %d real + %d defaults, source: %s)",
            average, len(ctrl._daily_consumption_history),
            real_count, len(ctrl._daily_consumption_history) - real_count,
            source,
        )

        return average

    def _recent_history_days(
        self, n: int = 7, *, before: Optional[date] = None
    ) -> list[date]:
        """Return the ``n`` calendar dates before ``before`` (default today).

        Predictive charging windows control when grid charging may run; they do
        not make the household or battery inactive. Every calendar day therefore
        belongs in the consumption history.
        """
        before = before or date.today()
        return [before - timedelta(days=offset) for offset in range(1, n + 1)]

    def _home_consumption_entity_id(self) -> Optional[str]:
        """Resolve the aggregate Home Consumption power sensor's entity_id.

        That sensor already encapsulates "household sensor if configured, else
        derived (grid + battery AC + solar)", so its recorder history is the
        single source for backfilling daily home consumption.
        """
        from homeassistant.helpers import entity_registry as er

        ent_reg = er.async_get(self._hass)
        return ent_reg.async_get_entity_id(
            "sensor", DOMAIN, "marstek_venus_system_home_consumption"
        )

    async def backfill_home_from_history(self, target_date: date) -> Optional[float]:
        """Integrate home power history for target_date → kWh.

        Integrates the aggregate Home Consumption sensor, which already resolves to
        the household sensor or the derived value (grid + battery AC + solar) per the
        active precedence. Predictive grid-charging windows do not mask household
        demand: battery charging is already cancelled by the battery AC term in
        ``grid + battery AC + solar``. Returns None if no usable data.
        """
        local_tz = dt_util.get_time_zone(self._hass.config.time_zone) or dt_util.UTC
        day_start = datetime.combine(target_date, time.min, tzinfo=local_tz)
        if self._period_intersects(day_start, day_start + timedelta(days=1)):
            return None
        source_entity = self._home_consumption_entity_id()
        if not source_entity:
            return None

        try:
            from homeassistant.components.recorder import history, get_instance
        except ImportError:
            _LOGGER.warning("Recorder not available for household backfill")
            return None

        start_time = datetime.combine(target_date, datetime.min.time()).replace(tzinfo=local_tz)
        end_time = datetime.combine(target_date, datetime.max.time()).replace(tzinfo=local_tz)

        try:
            recorder_instance = get_instance(self._hass)
            states_map = await recorder_instance.async_add_executor_job(
                history.state_changes_during_period,
                self._hass,
                start_time,
                end_time,
                source_entity,
            )
        except Exception as e:
            _LOGGER.error("Home consumption backfill query failed for %s: %s", target_date, e)
            return None

        entity_states = states_map.get(source_entity, [])
        if not entity_states:
            _LOGGER.debug("No home consumption history for %s", target_date)
            return None

        # Integrate power × dt over the full local day.
        energy_kwh = 0.0
        prev_ts: Optional[datetime] = None
        prev_kw: Optional[float] = None

        for state in entity_states:
            if state.state in ('unknown', 'unavailable'):
                prev_ts = None
                prev_kw = None
                continue
            try:
                power_w = float(state.state)
            except (ValueError, TypeError):
                prev_ts = None
                prev_kw = None
                continue

            unit = state.attributes.get("unit_of_measurement", "W")
            power_kw = power_w / 1000.0 if unit == "W" else power_w
            ts = state.last_updated

            if prev_ts is not None and prev_kw is not None:
                dt_hours = (ts - prev_ts).total_seconds() / 3600.0
                energy_kwh += max(0.0, prev_kw) * dt_hours

            prev_ts = ts
            prev_kw = power_kw

        # Apply excluded-device adjustment using historical power data for each device.
        # Mirrors the real-time logic in _excluded_devices_consumption_delta_kw():
        #   included_in_consumption=True  → device is in home sensor but battery skips it → subtract
        #   included_in_consumption=False → device not in home sensor but battery covers it → add
        excluded_devices = self._config_entry.data.get("excluded_devices", [])
        for device in excluded_devices:
            if not device.get("enabled", True):
                continue
            if device.get("ev_charger_no_telemetry", False):
                continue
            power_sensor = device.get("power_sensor")
            if not power_sensor:
                continue
            try:
                dev_states_map = await recorder_instance.async_add_executor_job(
                    history.state_changes_during_period,
                    self._hass,
                    start_time,
                    end_time,
                    power_sensor,
                )
            except Exception as e:
                _LOGGER.debug(
                    "Excluded device backfill query failed for %s on %s: %s",
                    power_sensor, target_date, e,
                )
                continue

            dev_states = dev_states_map.get(power_sensor, [])
            if not dev_states:
                continue

            dev_kwh = 0.0
            prev_ts = None
            prev_kw = None
            for dev_state in dev_states:
                if dev_state.state in ('unknown', 'unavailable'):
                    prev_ts = None
                    prev_kw = None
                    continue
                try:
                    dev_w = float(dev_state.state)
                except (ValueError, TypeError):
                    prev_ts = None
                    prev_kw = None
                    continue
                dev_unit = dev_state.attributes.get("unit_of_measurement", "W")
                dev_kw = dev_w / 1000.0 if dev_unit == "W" else dev_w
                ts = dev_state.last_updated
                if prev_ts is not None and prev_kw is not None:
                    dt_hours = (ts - prev_ts).total_seconds() / 3600.0
                    dev_kwh += max(0.0, prev_kw) * dt_hours
                prev_ts = ts
                prev_kw = dev_kw

            if device.get("included_in_consumption", True):
                energy_kwh -= dev_kwh
            else:
                energy_kwh += dev_kwh

        energy_kwh = max(0.0, energy_kwh)

        if energy_kwh <= 0:
            _LOGGER.debug("Household backfill for %s: no energy accumulated", target_date)
            return None

        result = round(energy_kwh, 2)
        _LOGGER.debug("Household backfill for %s: %.2f kWh", target_date, result)
        return result

    async def startup_backfill_consumption(self) -> None:
        """Run backfill from recorder history shortly after startup.

        Called once after a delay to give the recorder and coordinators time
        to initialize. Replaces default entries with real historical data.
        """
        ctrl = self._controller

        if not (ctrl.predictive_charging_enabled or ctrl.charge_delay_enabled):
            return

        # The profile backfill has one range query per configured source and is
        # intentionally independent from the legacy seven-day reconstruction.
        # Start it here so existing startup behaviour remains available while
        # the new Store learns in the background.
        self.start_consumption_profile_backfill()
        self.start_solar_profile_backfill()

        _LOGGER.info(
            "Startup backfill: attempting to replace defaults with real data "
            "(current history: %d entries, %d real)",
            len(ctrl._daily_consumption_history),
            sum(1 for _, c in ctrl._daily_consumption_history if c != DEFAULT_BASE_CONSUMPTION_KWH),
        )

        # Try to backfill past days from recorder history.
        # Window = the 7 most recent calendar days.
        target_days = [
            past_date for past_date in self._recent_history_days(7)
            if not self._period_intersects(
                datetime.combine(past_date, time.min, tzinfo=dt_util.now().tzinfo),
                datetime.combine(past_date + timedelta(days=1), time.min, tzinfo=dt_util.now().tzinfo),
            )
        ]
        real_data_dates = {
            d for d, c in ctrl._daily_consumption_history if c != DEFAULT_BASE_CONSUMPTION_KWH
        }
        backfill_count = 0
        for past_date in target_days:
            if past_date not in real_data_dates:
                value = await self.backfill_home_from_history(past_date)
                if value is not None and value >= 1.5:
                    replaced = False
                    for i, (d, c) in enumerate(ctrl._daily_consumption_history):
                        if d == past_date:
                            ctrl._daily_consumption_history[i] = (past_date, value)
                            replaced = True
                            break
                    if not replaced:
                        ctrl._daily_consumption_history.append((past_date, value))
                    ctrl._daily_consumption_history.sort(key=lambda x: x[0])
                    ctrl._daily_consumption_history = ctrl._daily_consumption_history[-7:]
                await asyncio.sleep(0.1)
                backfill_count += 1

        # Fill any remaining gaps in the window so we always have 7 calendar days.
        # Use the average of real entries as the gap value; fall back to
        # DEFAULT_BASE_CONSUMPTION_KWH only if there are no real entries at all.
        real_values = [
            c for _, c in ctrl._daily_consumption_history if c != DEFAULT_BASE_CONSUMPTION_KWH
        ]
        gap_value = (
            round(sum(real_values) / len(real_values), 2) if real_values
            else DEFAULT_BASE_CONSUMPTION_KWH
        )
        existing_dates = {d for d, _ in ctrl._daily_consumption_history}
        for past_date in target_days:
            if past_date not in existing_dates:
                ctrl._daily_consumption_history.append((past_date, gap_value))
                _LOGGER.info(
                    "Startup backfill: no data found for %s, inserted %.2f kWh (%s)",
                    past_date, gap_value,
                    "avg of real days" if real_values else "default fallback",
                )
        ctrl._daily_consumption_history.sort(key=lambda x: x[0])
        ctrl._daily_consumption_history = ctrl._daily_consumption_history[-7:]

        real_after = sum(
            1 for _, c in ctrl._daily_consumption_history if c != DEFAULT_BASE_CONSUMPTION_KWH
        )
        _LOGGER.info(
            "Startup backfill complete: attempted %d days, now %d real entries out of %d total",
            backfill_count, real_after, len(ctrl._daily_consumption_history),
        )

        # Persist updated history to disk
        await self.save_consumption_history()

    def initialize_history_with_defaults(self) -> None:
        """Initialize consumption history with default values for the past 7 days.

        This provides an immediate 7-day average on first use, using the fallback
        consumption value. Real data will gradually replace these estimates as days pass.

        Only initializes if history is completely empty (first-time setup).
        """
        ctrl = self._controller

        if len(ctrl._daily_consumption_history) > 0:
            return

        _LOGGER.info(
            "Initializing consumption history with default values (%.1f kWh per day)",
            DEFAULT_BASE_CONSUMPTION_KWH,
        )

        # Pre-populate the 7 most recent calendar days with fallback values.
        for past_date in self._recent_history_days(7):
            ctrl._daily_consumption_history.append((past_date, DEFAULT_BASE_CONSUMPTION_KWH))

        _LOGGER.info(
            "Pre-populated consumption history with %d days of default values",
            len(ctrl._daily_consumption_history),
        )

    async def capture_daily_consumption(self, now=None) -> None:
        """Scheduled task to capture daily home consumption.

        Runs daily at 23:55 to snapshot the full-day home-energy accumulator
        into the 7-day history before it resets at midnight, so predictive
        charging always has historical data.

        Args:
            now: Timestamp from scheduler (unused, for compatibility)
        """
        ctrl = self._controller

        if not ctrl.predictive_charging_enabled:
            return

        today = date.today()
        day_start = datetime.combine(today, time.min, tzinfo=dt_util.now().tzinfo)
        if self._period_intersects(day_start, day_start + timedelta(days=1)):
            ctrl._daily_consumption_history = [
                (day, energy) for day, energy in ctrl._daily_consumption_history if day != today
            ]
            await self.save_consumption_history()
            _LOGGER.info("Daily consumption capture skipped: %s intersects vacation mode", today)
            return

        # Consumption comes from the adjusted full-day home-energy accumulator.
        # Grid charging is not household demand: the negative battery AC term
        # cancels it in the derived ``grid + battery AC + solar`` power.
        current_value = round(ctrl._household_energy_accumulator, 2)
        if current_value < 1.5:
            _LOGGER.info(
                "Daily consumption capture: accumulator low (%.2f kWh) — skipping. "
                "Today's value will be recovered from recorder history on the next "
                "predictive-charging cycle.",
                current_value,
            )
            return

        try:
            has_today = any(d == today for d, _ in ctrl._daily_consumption_history)

            if has_today:
                ctrl._daily_consumption_history = [
                    (d, current_value if d == today else c)
                    for d, c in ctrl._daily_consumption_history
                ]
                _LOGGER.info(
                    "Daily consumption capture: UPDATED today's value: %.2f kWh (%d days in history)",
                    current_value, len(ctrl._daily_consumption_history),
                )
            else:
                ctrl._daily_consumption_history.append((today, current_value))
                _LOGGER.info(
                    "Daily consumption capture: CAPTURED today's value: %.2f kWh (%d days in history)",
                    current_value, len(ctrl._daily_consumption_history),
                )

                ctrl._daily_consumption_history.sort(key=lambda x: x[0])
                ctrl._daily_consumption_history = ctrl._daily_consumption_history[-7:]

            await self.save_consumption_history()

        except (ValueError, TypeError) as e:
            _LOGGER.error("Daily consumption capture: Failed to parse sensor value: %s", e)

    async def reset_daily_grid_at_min_soc(self, _now=None) -> None:
        """Reset the daily grid-at-min-soc accumulator at midnight."""
        ctrl = self._controller
        _LOGGER.debug(
            "Daily reset: clearing grid-at-min-soc accumulator (was %.3f kWh)",
            ctrl._daily_grid_at_min_soc_kwh,
        )
        ctrl._daily_grid_at_min_soc_kwh = 0.0
        if ctrl._grid_at_min_soc_sensor:
            ctrl._grid_at_min_soc_sensor.async_write_ha_state()
        await self.save_consumption_history()

    # ------------------------------------------------------------------
    # Solar timing
    # ------------------------------------------------------------------

    def calculate_solar_noon(self) -> float:
        """Calculate local solar noon from HA longitude and timezone.

        Returns solar noon as a float hour (e.g. 13.25 = 13:15).
        Cached per day (recalculated when date changes to handle DST transitions).
        """
        from zoneinfo import ZoneInfo

        today = datetime.now().date()
        if self._solar_noon_cache is not None and self._solar_noon_cache[0] == today:
            return self._solar_noon_cache[1]

        tz = ZoneInfo(self._hass.config.time_zone)
        utc_offset = datetime.now(tz).utcoffset().total_seconds() / 3600
        solar_noon = 12.0 - (self._hass.config.longitude / 15.0) + utc_offset
        self._solar_noon_cache = (today, solar_noon)
        _LOGGER.info(
            "Weekly Full Charge Delay: Solar noon calculated at %.2fh (longitude=%.2f, UTC offset=%.1f)",
            solar_noon, self._hass.config.longitude, utc_offset,
        )
        return solar_noon

    def calculate_sunrise(self) -> Optional[float]:
        """Estimate local sunrise time from HA latitude/longitude and day of year.

        Uses the standard solar declination + hour-angle formula.
        Returns sunrise as a float hour (e.g. 7.5 = 07:30), or None if the
        sun never rises today (polar night) or if HA location is not configured.
        """
        try:
            latitude = self._hass.config.latitude
            if latitude is None:
                return None

            day_of_year = datetime.now().timetuple().tm_yday
            lat_rad = math.radians(latitude)

            # Solar declination (degrees → radians)
            declination_rad = math.radians(
                -23.45 * math.cos(math.radians(360 / 365 * (day_of_year + 10)))
            )

            # Hour angle at sunrise: cos(H) = -tan(lat) * tan(dec)
            cos_h = -math.tan(lat_rad) * math.tan(declination_rad)
            if cos_h < -1 or cos_h > 1:
                return None  # Polar day / polar night

            hour_angle_deg = math.degrees(math.acos(cos_h))
            solar_noon = self.calculate_solar_noon()
            return solar_noon - hour_angle_deg / 15.0
        except Exception:  # noqa: BLE001
            return None

    def detect_solar_t_start(self) -> None:
        """Detect start of solar production via grid sensor and battery state.

        Primary: sets controller._solar_t_start when grid_power <= 0 while batteries
        are not discharging, indicating solar is covering the full house load.

        Fallback: if the primary condition hasn't fired within 30 min after the
        astronomically estimated sunrise (high-consumption day where grid power
        never reaches zero), uses the estimated sunrise as t_start so the
        sinusoidal energy model can still run.

        Only checks after 7:00 to avoid false triggers from overnight grid charging.
        """
        ctrl = self._controller

        if ctrl._solar_t_start is not None:
            return  # Already detected today

        now = datetime.now()
        if now.hour < 7:
            return  # Too early, any export is likely from nocturnal grid charging

        now_h = now.hour + now.minute / 60.0

        # --- Primary: grid ≤ 0 and batteries not discharging ---
        grid_state = self._hass.states.get(ctrl.consumption_sensor)
        grid_power = ctrl._apply_meter_transform(grid_state)
        if grid_power is not None and grid_power <= 0:
            total_battery_power = sum(
                (c.data.get("battery_power", 0) or 0)
                for c in ctrl.coordinators if c.data
            )
            if total_battery_power <= 0:
                ctrl._solar_t_start = now_h
                self.save_solar_t_start()
                t_end = self.estimate_t_end()
                _LOGGER.info(
                    "Charge Delay: Solar T_start detected via grid=%.0fW, battery=%.0fW "
                    "at %.2fh, estimated T_end=%.2fh",
                    grid_power, total_battery_power, ctrl._solar_t_start, t_end,
                )
                return

        # --- Fallback: astronomical sunrise + 30 min buffer ---
        estimated_sunrise = self.calculate_sunrise()
        if estimated_sunrise is not None and now_h >= estimated_sunrise + 0.5:
            ctrl._solar_t_start = estimated_sunrise
            self.save_solar_t_start()
            t_end = self.estimate_t_end()
            _LOGGER.info(
                "Charge Delay: Solar T_start set via astronomical sunrise fallback "
                "(estimated=%.2fh, now=%.2fh, T_end=%.2fh)",
                estimated_sunrise, now_h, t_end,
            )

    def estimate_t_end(self) -> float:
        """Estimate end of solar production by symmetry around solar noon.

        Returns T_end as a float hour. Dynamically extends if batteries
        are still charging beyond the estimated T_end.
        """
        ctrl = self._controller
        solar_noon = self.calculate_solar_noon()
        t_end = 2 * solar_noon - ctrl._solar_t_start

        # Dynamic extension: if current time is past T_end but batteries still charging
        now = datetime.now()
        now_h = now.hour + now.minute / 60.0
        if now_h > t_end:
            any_charging = any(
                (c.data.get("battery_power", 0) or 0) > 0
                for c in ctrl.coordinators if c.data
            )
            if any_charging:
                extended_t_end = now_h + 1.0
                _LOGGER.debug(
                    "Weekly Full Charge Delay: Extended T_end from %.2fh to %.2fh (active production)",
                    t_end, extended_t_end,
                )
                return extended_t_end

        return t_end

    @staticmethod
    def h_to_hhmm(h: Optional[float]) -> Optional[str]:
        """Convert decimal hours to HH:MM string."""
        if h is None:
            return None
        hours = int(h)
        minutes = int((h - hours) * 60)
        return f"{hours:02d}:{minutes:02d}"

    @staticmethod
    def get_solar_fraction_done(now_h: float, t_start: float, t_end: float) -> float:
        """Calculate cumulative fraction of daily solar energy produced by now.

        Uses sinusoidal model: F(t) = [1 - cos(π × (t - t_start) / (t_end - t_start))] / 2
        Returns value clamped to [0, 1].
        """
        if t_end <= t_start:
            return 1.0  # Invalid window, assume all produced

        if now_h <= t_start:
            return 0.0
        if now_h >= t_end:
            return 1.0

        progress = (now_h - t_start) / (t_end - t_start)
        fraction = (1.0 - math.cos(math.pi * progress)) / 2.0
        return max(0.0, min(1.0, fraction))

    def get_today_target_soc(self) -> int:
        """Get today's charge target SOC.

        On weekly full charge day → 100.
        Otherwise → average max_soc across batteries.
        """
        ctrl = self._controller
        if ctrl._weekly_charge_mgr.is_active():
            return 100

        if ctrl.coordinators:
            return round(sum(c.max_soc for c in ctrl.coordinators) / len(ctrl.coordinators))
        return 100

    # ------------------------------------------------------------------
    # Real-time accumulation (called from control loop)
    # ------------------------------------------------------------------

    def handle_accumulator_daily_reset(self) -> None:
        """Reset the home-consumption accumulator on day rollover.

        Compares the accumulator date against today; if changed, resets the
        accumulator value and clears the last-accumulation timestamp so the next
        sample doesn't integrate over the reset gap.
        """
        ctrl = self._controller

        today = date.today()
        if ctrl._household_accumulator_date != today:
            if ctrl._household_accumulator_date is not None:
                _LOGGER.info(
                    "Household accumulator daily reset (was %.2f kWh for %s)",
                    ctrl._household_energy_accumulator,
                    ctrl._household_accumulator_date,
                )
            ctrl._household_energy_accumulator = 0.0
            self._household_last_accumulation_time = None
            ctrl._household_accumulator_date = today

    def is_in_consumption_window(self) -> bool:
        """Return True because household consumption is measured all day.

        Kept as a compatibility helper for consumers and third-party tests that
        used the former windowed contract.
        """
        return True

    def get_consumption_window_hours_per_day(self) -> float:
        """Return the 24-hour basis used by daily consumption history."""
        return 24.0

    def consumption_window_hours_in_range(self, from_h: float, to_h: float) -> float:
        """Return all hours in a same-day range, including charging windows."""
        return max(0.0, to_h - from_h)

    async def accumulate_household_consumption(self) -> None:
        """Integrate home power → kWh accumulator (called every control cycle).

        Derives the home power the dashboard shows (grid + battery AC + solar) so
        predictive charging gets an accurate consumption estimate. Accumulates
        throughout the day; battery grid-charging power is cancelled by the
        battery AC term in the home-power derivation. Uses monotonic time to
        avoid issues with system clock changes.
        """
        ctrl = self._controller

        # Capture the same adjusted demand for both learning paths unless the
        # manual vacation switch is on. Physical daily energy counters are
        # integrated elsewhere and intentionally never pause.
        profile_now = dt_util.now()
        profile_mono = monotonic()
        power_kw = self.get_adjusted_home_power_kw()
        if self.is_vacation_active():
            self._record_vacation_night_sample(power_kw, profile_now, profile_mono)
            self._household_last_accumulation_time = None
            self._consumption_profile.record_power_sample(None, local_time=profile_now)
            return
        self._consumption_profile.record_power_sample(
            power_kw,
            local_time=profile_now,
            monotonic_time=profile_mono,
        )

        if power_kw is None:
            return

        now = profile_mono
        if self._household_last_accumulation_time is not None:
            dt_hours = (now - self._household_last_accumulation_time) / 3600.0
            ctrl._household_energy_accumulator += max(0.0, power_kw) * dt_hours
        self._household_last_accumulation_time = now

    def _record_vacation_night_sample(
        self, power_kw: float | None, local_time: datetime, monotonic_time: float
    ) -> None:
        """Integrate 01:00–05:00 samples into a valid-night baseline record."""
        if local_time.tzinfo is None:
            local_time = local_time.replace(tzinfo=dt_util.now().tzinfo)
        parsed = power_kw if power_kw is not None and math.isfinite(power_kw) and power_kw >= 0 else None
        if parsed is None:
            self._vacation_last_sample_time = None
            self._vacation_last_sample_mono = None
            self._vacation_last_power_kw = None
            return
        previous_time = self._vacation_last_sample_time
        previous_power = self._vacation_last_power_kw
        previous_mono = self._vacation_last_sample_mono
        if previous_time is not None and previous_power is not None and previous_mono is not None:
            elapsed = monotonic_time - previous_mono
            if 0 < elapsed <= VACATION_NIGHT_SAMPLE_GAP_S:
                # Split at the fixed local night boundaries; date belongs to the
                # night that starts at 01:00, so no cross-midnight ambiguity.
                cursor = previous_time
                end = local_time
                night_start = datetime.combine(cursor.date(), VACATION_NIGHT_START, tzinfo=cursor.tzinfo)
                night_end = datetime.combine(cursor.date(), VACATION_NIGHT_END, tzinfo=cursor.tzinfo)
                overlap_start = max(cursor, night_start)
                overlap_end = min(end, night_end)
                if overlap_end > overlap_start:
                    fraction_start = (overlap_start - cursor).total_seconds() / max(1e-9, (end - cursor).total_seconds())
                    fraction_end = (overlap_end - cursor).total_seconds() / max(1e-9, (end - cursor).total_seconds())
                    start_power = previous_power + (parsed - previous_power) * fraction_start
                    end_power = previous_power + (parsed - previous_power) * fraction_end
                    coverage = (overlap_end - overlap_start).total_seconds()
                    key = cursor.date().isoformat()
                    record = next((item for item in self._vacation_nights if item["date"] == key), None)
                    if record is None:
                        record = {"date": key, "energy_kwh": 0.0, "coverage_s": 0.0}
                        self._vacation_nights.append(record)
                    record["energy_kwh"] = float(record["energy_kwh"]) + (start_power + end_power) / 2 * coverage / 3600.0
                    record["coverage_s"] = float(record["coverage_s"]) + coverage
                    if float(record["coverage_s"]) >= VACATION_NIGHT_MIN_COVERAGE_S:
                        self._vacation_nights = self._vacation_nights[-30:]
                        asyncio.create_task(self._save_vacation_state())
        self._vacation_last_sample_time = local_time
        self._vacation_last_sample_mono = monotonic_time
        self._vacation_last_power_kw = parsed

    # ------------------------------------------------------------------
    # Throttle helpers used by the control loop
    # ------------------------------------------------------------------

    def maybe_save_accumulators(self) -> None:
        """Persist accumulators every 5 min (called every cycle)."""
        now_mono = monotonic()
        if now_mono - self._accumulator_last_save_monotonic >= 300:
            self._accumulator_last_save_monotonic = now_mono
            self.save_accumulators()
            self.save_daily_energy()
            self._consumption_profile.request_save()

    async def maybe_save_grid_at_min_soc_history(self) -> None:
        """Persist consumption history every ~5 min during grid-at-min-soc accumulation.

        Called from the PD control loop when accumulating grid imports while SOC
        is pinned to min_soc. Throttles writes to once every ~5 min. Uses elapsed
        monotonic time, not a cycle count, because the control loop is event-driven
        (variable cadence) — a count would fire faster or slower with the sensor rate.
        """
        now_mono = monotonic()
        if now_mono - self._grid_at_min_soc_last_save_mono >= 300:
            self._grid_at_min_soc_last_save_mono = now_mono
            await self.save_consumption_history()

    async def async_save_all(self) -> None:
        """Await every throttled persistence store at once.

        Called on unload so a reload does not revert the TOTAL_INCREASING daily
        energy sensors (consumption history + grid-at-min-soc, daily solar/home/
        grid totals, household/solar accumulators) to the last throttled (~5 min)
        save, which would step their values backwards and spam the HA log.
        """
        await self.save_consumption_history()
        await self.async_save_accumulators()
        await self.async_save_daily_energy()
        await self._consumption_profile.async_save_all()
        await self._solar_profile.async_save_all()
        await self._save_vacation_state()

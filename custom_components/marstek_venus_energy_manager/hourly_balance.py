"""Hourly net balance manager for Marstek Venus Energy Manager."""
from __future__ import annotations

import logging
from datetime import datetime, time as dt_time
from time import monotonic
from typing import TYPE_CHECKING, Any

from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    DOMAIN,
    HOURLY_BALANCE_STORAGE_KEY,
    HOURLY_BALANCE_STORAGE_VERSION,
    HOURLY_BALANCE_HISTORY_MAX,
    HOURLY_BALANCE_FORCE_RECALC_REMAINING_MIN,
    HOURLY_BALANCE_MIN_REMAINING_MIN,
    CONF_HOURLY_BALANCE_TARGET_NET_WH,
    CONF_HOURLY_BALANCE_MAX_OFFSET_W,
    CONF_HOURLY_BALANCE_HYSTERESIS_W,
    CONF_HOURLY_BALANCE_RAMP_IN_MIN,
    DEFAULT_HOURLY_BALANCE_TARGET_NET_WH,
    DEFAULT_HOURLY_BALANCE_MAX_OFFSET_W,
    DEFAULT_HOURLY_BALANCE_HYSTERESIS_W,
    DEFAULT_HOURLY_BALANCE_RAMP_IN_MIN,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.config_entries import ConfigEntry

_LOGGER = logging.getLogger(__name__)


class HourlyBalanceManager:
    """Tracks grid import/export per civil hour and adjusts setpoint offset."""

    def __init__(self, hass: HomeAssistant, config_entry: ConfigEntry, controller: Any) -> None:
        self._hass = hass
        self._config_entry = config_entry
        self._controller = controller
        self._store: Store = Store(
            hass,
            HOURLY_BALANCE_STORAGE_VERSION,
            f"{DOMAIN}.{config_entry.entry_id}.{HOURLY_BALANCE_STORAGE_KEY}",
        )

        # Accumulators for current hour
        self._current_hour: int | None = None
        self._hour_started_local: datetime | None = None
        self._imp_wh: float = 0.0
        self._exp_wh: float = 0.0
        self._last_grid_w: float | None = None
        self._last_sample_monotonic: float | None = None
        self._last_offset_w: float = 0.0

        # History and save throttle
        self._history: list[dict] = []
        
        # Internal state
        self._last_theoretical_offset_w: float = 0.0
        self._last_block_reason: str | None = None
        self._save_counter: int = 0

        # Registered sensor entities for push updates
        self._sensors: list[Any] = []

    # ------------------------------------------------------------------
    # Setup / teardown
    # ------------------------------------------------------------------

    async def async_setup(self) -> None:
        """Load persisted state from store."""
        stored = await self._store.async_load()
        if stored:
            self._history = stored.get("history", [])

            # Restore current-hour accumulators only if still the same hour
            now_local = dt_util.now()
            saved_hour_iso = stored.get("hour_iso")
            if saved_hour_iso:
                try:
                    saved_hour = datetime.fromisoformat(saved_hour_iso)
                    if (saved_hour.year == now_local.year
                            and saved_hour.month == now_local.month
                            and saved_hour.day == now_local.day
                            and saved_hour.hour == now_local.hour):
                        self._imp_wh = stored.get("imp_wh", 0.0)
                        self._exp_wh = stored.get("exp_wh", 0.0)
                        self._last_offset_w = stored.get("last_offset_w", 0.0)
                        self._current_hour = now_local.hour
                        self._hour_started_local = now_local.replace(
                            minute=0, second=0, microsecond=0
                        )
                        _LOGGER.info(
                            "HourlyBalance: restored hour %s — imp=%.0fWh exp=%.0fWh",
                            saved_hour_iso, self._imp_wh, self._exp_wh,
                        )
                except ValueError:
                    pass

        _LOGGER.info(
            "HourlyBalance: setup complete, %d history entries loaded",
            len(self._history),
        )

    async def async_unload(self) -> None:
        """Persist state to store."""
        await self._save()

    # ------------------------------------------------------------------
    # Sensor registration
    # ------------------------------------------------------------------

    def register_sensor(self, entity: Any) -> None:
        self._sensors.append(entity)

    def _push_sensors(self) -> None:
        for sensor in self._sensors:
            sensor.async_write_ha_state()

    # ------------------------------------------------------------------
    # Main processing loop (called every PD cycle)
    # ------------------------------------------------------------------

    async def async_process(self) -> None:
        """Process one cycle. Reads grid power directly so it can run before
        the PD's deadband / stale-sensor early-returns gate the rest of the
        control loop."""
        # Edge case: manual mode — clear offset and do nothing
        if self._controller.manual_mode_enabled:
            self._controller.remove_setpoint_offset("hourly_balance")
            self._last_sample_monotonic = None
            self._last_grid_w = None
            self._push_sensors()
            return

        in_slot = self._is_in_active_slot()

        if not in_slot:
            self._controller.remove_setpoint_offset("hourly_balance")
            # Reset integration so we restart clean when slot opens again
            self._last_sample_monotonic = None
            self._last_grid_w = None
            self._push_sensors()
            return

        # Read grid power directly from the consumption sensor, applying the
        # same meter transform the PD loop uses. If the sensor is unavailable
        # we still push sensors so HA's last_updated keeps advancing, but we
        # skip integration and reset the monotonic anchor.
        grid_state = self._hass.states.get(self._controller.consumption_sensor)
        grid_w = self._controller._apply_meter_transform(grid_state)
        if grid_w is None:
            self._last_sample_monotonic = None
            self._last_grid_w = None
            self._push_sensors()
            return

        now_local = dt_util.now()
        now_mono = monotonic()

        # Detect hour change
        if self._current_hour != now_local.hour:
            if self._current_hour is not None:
                # Close previous hour
                net_wh = self._imp_wh - self._exp_wh
                entry = {
                    "hour_iso": self._hour_started_local.isoformat() if self._hour_started_local else None,
                    "imp_wh": round(self._imp_wh, 1),
                    "exp_wh": round(self._exp_wh, 1),
                    "net_wh": round(net_wh, 1),
                    "target_net_wh": self._target_net_wh(),
                }
                self._history.append(entry)
                if len(self._history) > HOURLY_BALANCE_HISTORY_MAX:
                    self._history = self._history[-HOURLY_BALANCE_HISTORY_MAX:]
                _LOGGER.info(
                    "HourlyBalance: closed hour %s — imp=%.0fWh exp=%.0fWh net=%.0fWh",
                    entry["hour_iso"], self._imp_wh, self._exp_wh, net_wh,
                )

            # Start new hour
            self._current_hour = now_local.hour
            self._hour_started_local = now_local.replace(minute=0, second=0, microsecond=0)
            self._imp_wh = 0.0
            self._exp_wh = 0.0
            self._last_sample_monotonic = None
            self._last_grid_w = None
            self._last_offset_w = 0.0

        # Integrate Wh (trapezoidal rule). When the sign changes between
        # samples, split the interval at the linearly-interpolated zero
        # crossing so import and export buckets stay clean.
        if self._last_sample_monotonic is not None and self._last_grid_w is not None:
            dt_h = (now_mono - self._last_sample_monotonic) / 3600.0
            prev_w = self._last_grid_w
            curr_w = grid_w
            if (prev_w >= 0) == (curr_w >= 0):
                wh = (prev_w + curr_w) / 2.0 * dt_h
                if wh >= 0:
                    self._imp_wh += wh
                else:
                    self._exp_wh += -wh
            else:
                # Sign change: split at zero crossing
                frac = abs(prev_w) / (abs(prev_w) + abs(curr_w))
                dt_first = dt_h * frac
                dt_second = dt_h - dt_first
                wh_first = prev_w / 2.0 * dt_first
                wh_second = curr_w / 2.0 * dt_second
                if wh_first >= 0:
                    self._imp_wh += wh_first
                else:
                    self._exp_wh += -wh_first
                if wh_second >= 0:
                    self._imp_wh += wh_second
                else:
                    self._exp_wh += -wh_second

        self._last_sample_monotonic = now_mono
        self._last_grid_w = grid_w

        # Calculate offset
        target_net_wh = self._target_net_wh()
        net_wh = self._imp_wh - self._exp_wh
        elapsed_min = (now_local - self._hour_started_local).total_seconds() / 60.0
        remaining_min = max(0.0, 60.0 - elapsed_min)

        if remaining_min < HOURLY_BALANCE_MIN_REMAINING_MIN:
            offset_w = 0.0
        else:
            deficit_wh = target_net_wh - net_wh  # >0 means we exported too much, need to import
            needed_avg_w = deficit_wh / (remaining_min / 60.0)
            offset_w = needed_avg_w  # positive = shift target towards import

            # Ramp-in: attenuate during the first ramp_in_min minutes
            ramp_in_min = self._config_entry.data.get(
                CONF_HOURLY_BALANCE_RAMP_IN_MIN, DEFAULT_HOURLY_BALANCE_RAMP_IN_MIN
            )
            if elapsed_min < ramp_in_min and ramp_in_min > 0:
                offset_w *= elapsed_min / ramp_in_min

            # Saturation
            max_offset_w = self._config_entry.data.get(
                CONF_HOURLY_BALANCE_MAX_OFFSET_W, DEFAULT_HOURLY_BALANCE_MAX_OFFSET_W
            )
            offset_w = max(-max_offset_w, min(max_offset_w, offset_w))

        # Hysteresis (bypass near end of hour)
        if remaining_min >= HOURLY_BALANCE_FORCE_RECALC_REMAINING_MIN:
            hysteresis_w = self._config_entry.data.get(
                CONF_HOURLY_BALANCE_HYSTERESIS_W, DEFAULT_HOURLY_BALANCE_HYSTERESIS_W
            )
            if abs(offset_w - self._last_offset_w) < hysteresis_w:
                offset_w = self._last_offset_w

        # If compensation is blocked, zero the offset so the PD controller
        # doesn't chase an unreachable target. The integration (imp/exp)
        # continues tracking so the correct offset will be applied once
        # the block lifts.
        self._last_theoretical_offset_w = offset_w
        self._last_block_reason = self._get_compensation_block_reason(offset_w)
        if self._last_block_reason is not None:
            _LOGGER.debug(
                "HourlyBalance: offset would be %.0fW but blocked by %s → applying 0W",
                offset_w, self._last_block_reason,
            )
            offset_w = 0.0

        self._controller.set_setpoint_offset("hourly_balance", offset_w)
        self._last_offset_w = offset_w

        # Throttled save (~120 cycles ≈ 5 min at 2.5 s/cycle)
        self._save_counter += 1
        if self._save_counter >= 120:
            self._save_counter = 0
            await self._save()

        self._push_sensors()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    async def _save(self) -> None:
        data = {
            "history": self._history,
            "hour_iso": self._hour_started_local.isoformat() if self._hour_started_local else None,
            "imp_wh": self._imp_wh,
            "exp_wh": self._exp_wh,
            "last_offset_w": self._last_offset_w,
        }
        await self._store.async_save(data)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _target_net_wh(self) -> float:
        return float(self._config_entry.data.get(
            CONF_HOURLY_BALANCE_TARGET_NET_WH, DEFAULT_HOURLY_BALANCE_TARGET_NET_WH
        ))

    def _is_in_active_slot(self) -> bool:
        """Return True if we should apply hourly balance right now.

        Logic mirrors no_discharge_time_slots: if no enabled slots exist,
        return True (apply 24/7).  Otherwise return True only when current
        day+time falls inside at least one enabled slot.
        """
        all_slots = self._config_entry.data.get("no_discharge_time_slots", [])
        enabled_slots = [s for s in all_slots if s.get("enabled", True)]

        if not enabled_slots:
            return True

        now = datetime.now()
        current_time = now.time()
        current_day = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"][now.weekday()]

        for slot in enabled_slots:
            if current_day not in slot.get("days", []):
                continue
            try:
                start_t = dt_time.fromisoformat(slot["start_time"])
                end_t = dt_time.fromisoformat(slot["end_time"])
            except (KeyError, ValueError):
                continue
            if start_t <= current_time <= end_t:
                return True

        return False

    def _get_compensation_block_reason(self, offset: float) -> str | None:
        """Return a reason string if compensation is currently blocked, else None.

        solar_charge_delay blocks both offset directions: when active the
        battery can't charge so solar surplus accumulates as export, swinging
        the offset negative; the balance can't correct either way.
        Hysteresis and max_soc only prevent charging, so they are checked
        only for offset > 0.
        Uses _charge_delay_status (kept current by the PD cycle) to avoid
        calling _is_charge_delayed() which has side-effects.
        """
        ctrl = self._controller
        if ctrl.charge_delay_enabled:
            delay_state = ctrl._charge_delay_status.get("state", "")
            _delay_not_blocking = {
                "Disabled", "Charging allowed", "Skipped - Full Charge Day",
                "Charging to setpoint",
            }
            if delay_state not in _delay_not_blocking and not delay_state.startswith("Unlocking"):
                return "solar_charge_delay"
        if offset > 0:
            if any(getattr(c, "_hysteresis_active", False) for c in ctrl.coordinators):
                return "hysteresis"
            with_data = [c for c in ctrl.coordinators if c.data]
            if with_data and all(
                c.data.get("battery_soc", 0) >= c.max_soc for c in with_data
            ):
                return "max_soc"
        return None

    def get_status_dict(self) -> dict:
        """Return a snapshot dict for sensor attributes."""
        now_local = dt_util.now()
        elapsed_min = 0.0
        remaining_min = 60.0
        if self._hour_started_local is not None:
            elapsed_min = (now_local - self._hour_started_local).total_seconds() / 60.0
            remaining_min = max(0.0, 60.0 - elapsed_min)

        offset = self._last_offset_w
        return {
            "net_kwh": round((self._exp_wh - self._imp_wh) / 1000, 3),
            "imp_wh": round(self._imp_wh, 1),
            "exp_wh": round(self._exp_wh, 1),
            "elapsed_min": round(elapsed_min, 1),
            "remaining_min": round(remaining_min, 1),
            "target_net_wh": self._target_net_wh(),
            "offset_w": round(offset, 1),
            "theoretical_offset_w": round(self._last_theoretical_offset_w, 1),
            "in_active_slot": self._is_in_active_slot(),
            "hour_iso": self._hour_started_local.isoformat() if self._hour_started_local else None,
            "charge_block_reason": self._last_block_reason,
            "history": self._history[-24:],
        }

    def get_state_label(self) -> str:
        """Return a string state label for the status sensor."""
        if not self._is_in_active_slot():
            return "out_of_slot"
        if self._current_hour is None:
            return "idle"

        if self._last_block_reason:
            return "compensation_stopped"

        max_offset_w = self._config_entry.data.get(
            CONF_HOURLY_BALANCE_MAX_OFFSET_W, DEFAULT_HOURLY_BALANCE_MAX_OFFSET_W
        )
        if abs(self._last_theoretical_offset_w) >= max_offset_w - 0.5:
            return "capped"

        if self._last_theoretical_offset_w != 0:
            return "compensating_import" if self._last_theoretical_offset_w > 0 else "compensating_export"
            
        return "idle"

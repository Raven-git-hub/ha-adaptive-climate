"""
Coordinator for Adaptive Climate Control.

Responsibilities:
- Load and persist almanac data via HA storage
- Run the 10-minute heartbeat
- Write CSV event logs and daily almanac snapshots
- Manage the 21-day rolling CSV log window
- Expose current state to sensors
"""

from __future__ import annotations

import csv
import logging
import os
from datetime import datetime, timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .almanac import RoomAlmanac, get_current_period, get_day_of_year
from .const import (
    DOMAIN,
    ALMANAC_LEARNING_WINDOW,
    CONF_ROOMS,
    CONF_CLIMATE_ENTITY,
    CONF_SENSORS,
    CONF_SENSOR_TRUST,
    CONF_DEFAULT_TEMPS,
    ACTION_IDLE,
    ACTION_COOLING,
    ACTION_WARMING,
    ACTIVITY_ACTIVE,
    ACTIVITY_ASLEEP,
    ACTIVITY_AWAY,
    PERIOD_NAMES,
)

_LOGGER = logging.getLogger(__name__)

STORAGE_KEY = f"{DOMAIN}.almanac"
STORAGE_VERSION = 1
HEARTBEAT_INTERVAL = timedelta(minutes=10)


class AdaptiveClimateCoordinator(DataUpdateCoordinator):
    """
    Central coordinator for Adaptive Climate Control.
    One instance per config entry (i.e. per installation).
    """

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry_id: str,
        rooms_config: list[dict[str, Any]],
        log_dir: str,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=HEARTBEAT_INTERVAL,
        )
        self.config_entry_id = config_entry_id
        self.rooms_config = rooms_config
        self.log_dir = log_dir

        # Storage
        self._store: Store = Store(hass, STORAGE_VERSION, STORAGE_KEY)

        # Runtime state - keyed by room_id
        self.almanacs: dict[str, RoomAlmanac] = {}
        self.corrective_states: dict[str, str] = {}
        self.corrective_since: dict[str, datetime | None] = {}
        self.activity_states: dict[str, str] = {}

    # -------------------------------------------------------------------
    # Startup and shutdown
    # -------------------------------------------------------------------

    async def async_setup(self) -> None:
        """Load persisted almanac data and initialise rooms."""
        stored = await self._store.async_load()

        for room_cfg in self.rooms_config:
            room_id = room_cfg["room_id"]
            default_temps = room_cfg.get(CONF_DEFAULT_TEMPS, {p: 21.0 for p in PERIOD_NAMES})

            if stored and room_id in stored.get("rooms", {}):
                self.almanacs[room_id] = RoomAlmanac.from_dict(
                    stored["rooms"][room_id], default_temps
                )
                _LOGGER.info("Loaded almanac for room: %s", room_id)
            else:
                self.almanacs[room_id] = RoomAlmanac(room_id, default_temps)
                _LOGGER.info("Initialised new almanac for room: %s", room_id)

            # Register sensors
            for sensor_cfg in room_cfg.get(CONF_SENSORS, []):
                self.almanacs[room_id].add_sensor(
                    sensor_cfg["entity_id"],
                    sensor_cfg.get(CONF_SENSOR_TRUST, "uncertain"),
                )

            # Initialise corrective state
            self.corrective_states[room_id] = ACTION_IDLE
            self.corrective_since[room_id] = None
            self.activity_states[room_id] = ACTIVITY_ACTIVE

        # Ensure log directory exists
        os.makedirs(self.log_dir, exist_ok=True)

    async def async_save(self) -> None:
        """Persist all almanac data to HA storage."""
        data = {
            "rooms": {
                room_id: almanac.to_dict()
                for room_id, almanac in self.almanacs.items()
            }
        }
        await self._store.async_save(data)
        _LOGGER.debug("Almanac saved to storage")

    # -------------------------------------------------------------------
    # Heartbeat
    # -------------------------------------------------------------------

    async def _async_update_data(self) -> dict[str, Any]:
        """
        Called every 10 minutes by the DataUpdateCoordinator.
        Reads sensor states, checks comfort bands, and acts if needed.
        """
        now = datetime.now()
        result = {}

        for room_id, almanac in self.almanacs.items():
            room_cfg = self._get_room_cfg(room_id)
            if room_cfg is None:
                continue

            # Update sensor readings from HA state machine
            for sensor_cfg in room_cfg.get(CONF_SENSORS, []):
                entity_id = sensor_cfg["entity_id"]
                state = self.hass.states.get(entity_id)
                if state and state.state not in ("unknown", "unavailable"):
                    try:
                        temp = float(state.state)
                        almanac.record_sensor_reading(entity_id, temp, now)
                    except ValueError:
                        _LOGGER.warning("Non-numeric state for sensor %s: %s", entity_id, state.state)

            # Determine activity state
            activity = self._get_activity_state(room_cfg)
            self.activity_states[room_id] = activity

            # Check corrective action timeout
            self._check_corrective_timeout(room_id, now)

            # Evaluate comfort and act if needed
            period = get_current_period(now)
            action_taken = self._evaluate_and_act(room_id, almanac, room_cfg, period, activity, now)

            result[room_id] = {
                "period": period,
                "target_temp": almanac.current_target(),
                "good_band": almanac.current_good_band(),
                "confidence": almanac.current_confidence(),
                "activity": activity,
                "corrective_state": self.corrective_states[room_id],
                "action_taken": action_taken,
            }

        # Daily maintenance at midnight
        await self._daily_maintenance(now)

        # Save after every heartbeat
        await self.async_save()

        return result

    # -------------------------------------------------------------------
    # Comfort evaluation
    # -------------------------------------------------------------------

    def _evaluate_and_act(
        self,
        room_id: str,
        almanac: RoomAlmanac,
        room_cfg: dict,
        period: str,
        activity: str,
        now: datetime,
    ) -> str:
        """
        Evaluate whether corrective action is needed and act accordingly.
        Returns a string describing what was done (or "none").
        """
        from .const import PERIODS

        # No action during overnight period
        if period == "overnight":
            return "none"

        # If a corrective action is already underway, monitor only
        if self.corrective_states[room_id] != ACTION_IDLE:
            _LOGGER.debug("Room %s: corrective action in progress, monitoring", room_id)
            return "monitoring"

        # If away, no action
        if activity == ACTIVITY_AWAY:
            return "none"

        # Get weighted temperature reading
        weighted_temp = self._get_weighted_temperature(almanac)
        if weighted_temp is None:
            _LOGGER.debug("Room %s: no sensor readings available", room_id)
            return "none"

        target = almanac.current_target()
        deviation = weighted_temp - target

        # Determine the effective trigger threshold (highest trust sensor drives this)
        threshold = self._get_effective_threshold(almanac)

        if deviation > threshold:
            return self._act_too_hot(room_id, almanac, room_cfg, activity, now)
        elif deviation < -threshold:
            return self._act_too_cold(room_id, almanac, room_cfg, activity, now)

        return "none"

    def _get_weighted_temperature(self, almanac: RoomAlmanac) -> float | None:
        """
        Return a trust-weighted average temperature across all sensors.
        Sensors with higher trust contribute more to the result.
        """
        total_weight = 0.0
        weighted_sum = 0.0

        for sensor in almanac.sensors.values():
            reading = sensor.latest_reading()
            if reading is not None:
                weight = sensor.trust_score
                weighted_sum += reading * weight
                total_weight += weight

        if total_weight == 0:
            return None
        return weighted_sum / total_weight

    def _get_effective_threshold(self, almanac: RoomAlmanac) -> float:
        """Return the action threshold of the most trusted sensor."""
        if not almanac.sensors:
            from .const import TRUST_THRESHOLD_MIN
            return TRUST_THRESHOLD_MIN
        best = max(almanac.sensors.values(), key=lambda s: s.trust_score)
        return best.action_threshold

    def _act_too_hot(
        self,
        room_id: str,
        almanac: RoomAlmanac,
        room_cfg: dict,
        activity: str,
        now: datetime,
    ) -> str:
        """Initiate cooling action."""
        from .const import (
            COOLING_SETPOINT_OFFSET,
            COOLING_SETPOINT_OFFSET_UNOCCUPIED,
            FAN_SPEEDS,
        )

        climate_entity = room_cfg.get(CONF_CLIMATE_ENTITY)
        target = almanac.current_target()
        occupied = activity == ACTIVITY_ACTIVE or activity == ACTIVITY_ASLEEP

        if occupied:
            new_setpoint = target + COOLING_SETPOINT_OFFSET
            self._set_climate_temperature(climate_entity, new_setpoint)
            self._step_fan_up(climate_entity)
            _LOGGER.info("Room %s: too hot (occupied) - cooling to %.1f, fan up", room_id, new_setpoint)
        else:
            new_setpoint = target + COOLING_SETPOINT_OFFSET_UNOCCUPIED
            self._set_climate_temperature(climate_entity, new_setpoint)
            _LOGGER.info("Room %s: too hot (unoccupied) - cooling to %.1f", room_id, new_setpoint)

        self.corrective_states[room_id] = ACTION_COOLING
        self.corrective_since[room_id] = now
        return "cooling_initiated"

    def _act_too_cold(
        self,
        room_id: str,
        almanac: RoomAlmanac,
        room_cfg: dict,
        activity: str,
        now: datetime,
    ) -> str:
        """Initiate warming action."""
        from .const import WARMING_SETPOINT_OFFSET, FAN_SPEEDS

        climate_entity = room_cfg.get(CONF_CLIMATE_ENTITY)
        target = almanac.current_target()

        current_fan = self._get_current_fan_speed(climate_entity)
        if current_fan is None:
            return "none"

        if current_fan != FAN_SPEEDS[0]:
            self._step_fan_down(climate_entity)
            _LOGGER.info("Room %s: too cold - stepping fan down", room_id)
        else:
            new_setpoint = target + WARMING_SETPOINT_OFFSET
            self._set_climate_temperature(climate_entity, new_setpoint)
            _LOGGER.info("Room %s: too cold (fan at min) - warming to %.1f", room_id, new_setpoint)

        self.corrective_states[room_id] = ACTION_WARMING
        self.corrective_since[room_id] = now
        return "warming_initiated"

    def _check_corrective_timeout(self, room_id: str, now: datetime) -> None:
        """Reset corrective state if it has been active longer than the timeout."""
        from .const import CORRECTIVE_ACTION_TIMEOUT

        since = self.corrective_since[room_id]
        if since is None:
            return
        elapsed = (now - since).total_seconds() / 60
        if elapsed >= CORRECTIVE_ACTION_TIMEOUT:
            _LOGGER.info(
                "Room %s: corrective action timed out after %d minutes, resetting to IDLE",
                room_id,
                int(elapsed),
            )
            self.corrective_states[room_id] = ACTION_IDLE
            self.corrective_since[room_id] = None

    # -------------------------------------------------------------------
    # Activity state
    # -------------------------------------------------------------------

    def _get_activity_state(self, room_cfg: dict) -> str:
        """Determine current activity state from presence and sleep sensors."""
        from .const import CONF_PRESENCE, CONF_SLEEP_SENSOR

        presence_entities = room_cfg.get(CONF_PRESENCE, [])
        sleep_sensor = room_cfg.get(CONF_SLEEP_SENSOR)

        # Check sleep first
        if sleep_sensor:
            sleep_state = self.hass.states.get(sleep_sensor)
            if sleep_state and sleep_state.state == "on":
                return ACTIVITY_ASLEEP

        # Check presence
        if presence_entities:
            for entity_id in presence_entities:
                state = self.hass.states.get(entity_id)
                if state and state.state in ("home", "on"):
                    return ACTIVITY_ACTIVE
            return ACTIVITY_AWAY

        # No presence sensor configured - assume active
        return ACTIVITY_ACTIVE

    # -------------------------------------------------------------------
    # Climate entity control
    # -------------------------------------------------------------------

    def _set_climate_temperature(self, entity_id: str, temperature: float) -> None:
        """Set the target temperature on a climate entity."""
        self.hass.async_create_task(
            self.hass.services.async_call(
                "climate",
                "set_temperature",
                {"entity_id": entity_id, "temperature": round(temperature, 1)},
            )
        )

    def _get_current_fan_speed(self, entity_id: str) -> str | None:
        """Return the current fan mode of a climate entity."""
        from .const import FAN_SPEEDS
        state = self.hass.states.get(entity_id)
        if state is None:
            return None
        fan_mode = state.attributes.get("fan_mode", "").lower()
        if fan_mode in FAN_SPEEDS:
            return fan_mode
        return FAN_SPEEDS[0]

    def _step_fan_up(self, entity_id: str) -> None:
        """Increase fan speed by one step."""
        from .const import FAN_SPEEDS
        current = self._get_current_fan_speed(entity_id)
        if current is None:
            return
        idx = FAN_SPEEDS.index(current)
        if idx < len(FAN_SPEEDS) - 1:
            self._set_fan_mode(entity_id, FAN_SPEEDS[idx + 1])

    def _step_fan_down(self, entity_id: str) -> None:
        """Decrease fan speed by one step."""
        from .const import FAN_SPEEDS
        current = self._get_current_fan_speed(entity_id)
        if current is None:
            return
        idx = FAN_SPEEDS.index(current)
        if idx > 0:
            self._set_fan_mode(entity_id, FAN_SPEEDS[idx - 1])

    def _set_fan_mode(self, entity_id: str, fan_mode: str) -> None:
        """Set fan mode on a climate entity."""
        self.hass.async_create_task(
            self.hass.services.async_call(
                "climate",
                "set_fan_mode",
                {"entity_id": entity_id, "fan_mode": fan_mode},
            )
        )

    # -------------------------------------------------------------------
    # Intervention recording (called by climate_watcher)
    # -------------------------------------------------------------------

    def record_intervention(
        self,
        room_id: str,
        new_temp: float,
        direction: str,
    ) -> None:
        """Accept a learning signal from the climate watcher."""
        if room_id not in self.almanacs:
            _LOGGER.warning("Intervention for unknown room: %s", room_id)
            return

        self.almanacs[room_id].record_intervention(new_temp, direction)
        self._write_event_log(room_id, new_temp, direction)
        self.hass.async_create_task(self.async_save())

    # -------------------------------------------------------------------
    # CSV logging
    # -------------------------------------------------------------------

    def _event_log_path(self, dt: datetime) -> str:
        """Return the path for today's event log CSV."""
        filename = f"events_{dt.strftime('%Y_%m_%d')}.csv"
        return os.path.join(self.log_dir, filename)

    def _write_event_log(self, room_id: str, new_temp: float, direction: str) -> None:
        """Append an intervention event to today's CSV log."""
        now = datetime.now()
        path = self._event_log_path(now)
        almanac = self.almanacs[room_id]
        file_exists = os.path.exists(path)

        try:
            with open(path, "a", newline="") as f:
                writer = csv.writer(f)
                if not file_exists:
                    writer.writerow([
                        "timestamp", "room", "period", "day_of_year",
                        "new_temp", "direction", "sensor_id", "sensor_reading", "trust_score"
                    ])
                period = get_current_period(now)
                day = get_day_of_year(now.date())
                for sensor_id, sensor in almanac.sensors.items():
                    reading = sensor.latest_reading()
                    writer.writerow([
                        now.isoformat(),
                        room_id,
                        period,
                        day,
                        new_temp,
                        direction,
                        sensor_id,
                        reading if reading is not None else "",
                        round(sensor.trust_score, 3),
                    ])
        except OSError as e:
            _LOGGER.error("Failed to write event log: %s", e)

    def _write_almanac_snapshot(self) -> None:
        """Write a full almanac snapshot to CSV."""
        path = os.path.join(self.log_dir, "almanac_snapshot.csv")
        try:
            with open(path, "w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow([
                    "room", "period", "day_of_year",
                    "target_temp", "good_band_low", "good_band_high",
                    "confidence", "last_updated"
                ])
                for room_id, almanac in self.almanacs.items():
                    for slot in almanac.all_slots():
                        writer.writerow([
                            room_id,
                            slot.period,
                            slot.day_of_year,
                            round(slot.target_temp, 2),
                            round(slot.good_band_low, 2),
                            round(slot.good_band_high, 2),
                            slot.confidence,
                            slot.last_updated.isoformat() if slot.last_updated else "",
                        ])
            _LOGGER.info("Almanac snapshot written to %s", path)
        except OSError as e:
            _LOGGER.error("Failed to write almanac snapshot: %s", e)

    def _prune_old_csv_logs(self) -> None:
        """Delete event log CSVs older than the learning window."""
        cutoff = datetime.now() - timedelta(days=ALMANAC_LEARNING_WINDOW)
        try:
            for filename in os.listdir(self.log_dir):
                if not filename.startswith("events_") or not filename.endswith(".csv"):
                    continue
                filepath = os.path.join(self.log_dir, filename)
                try:
                    date_str = filename[len("events_"):-len(".csv")]
                    file_date = datetime.strptime(date_str, "%Y_%m_%d")
                    if file_date < cutoff:
                        os.remove(filepath)
                        _LOGGER.info("Pruned old event log: %s", filename)
                except ValueError:
                    pass
        except OSError as e:
            _LOGGER.error("Failed to prune CSV logs: %s", e)

    # -------------------------------------------------------------------
    # Daily maintenance
    # -------------------------------------------------------------------

    _last_maintenance_day: int = -1

    async def _daily_maintenance(self, now: datetime) -> None:
        """Run once per day at the first heartbeat after midnight."""
        today = now.date().toordinal()
        if today == self._last_maintenance_day:
            return
        self._last_maintenance_day = today
        _LOGGER.info("Running daily maintenance")
        self._write_almanac_snapshot()
        self._prune_old_csv_logs()

    # -------------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------------

    def _get_room_cfg(self, room_id: str) -> dict | None:
        """Look up room config by room_id."""
        for cfg in self.rooms_config:
            if cfg["room_id"] == room_id:
                return cfg
        return None

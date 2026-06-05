"""
Sensor platform for Adaptive Climate Control.

Exposes the following entities per room:
- Predicted comfort temperature (almanac target for current slot)
- Almanac confidence (0-100%)
- Activity state (active / asleep / away)
- Current period (morning / day / afternoon / evening / overnight)
- Corrective action state (idle / cooling_active / warming_active)
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, VERSION
from .coordinator import AdaptiveClimateCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Adaptive Climate Control sensors from a config entry."""
    coordinator: AdaptiveClimateCoordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]

    entities = []
    for room_id in coordinator.almanacs:
        entities.extend([
            ACCPredictedTempSensor(coordinator, entry, room_id),
            ACCConfidenceSensor(coordinator, entry, room_id),
            ACCActivityStateSensor(coordinator, entry, room_id),
            ACCCurrentPeriodSensor(coordinator, entry, room_id),
            ACCCorrectiveStateSensor(coordinator, entry, room_id),
        ])

    async_add_entities(entities)
    _LOGGER.info(
        "Adaptive Climate Control: registered %d sensors for %d rooms",
        len(entities),
        len(coordinator.almanacs),
    )


# -------------------------------------------------------------------
# Base sensor
# -------------------------------------------------------------------

class ACCBaseSensor(CoordinatorEntity, SensorEntity):
    """Base class for all Adaptive Climate Control sensors."""

    def __init__(
        self,
        coordinator: AdaptiveClimateCoordinator,
        entry: ConfigEntry,
        room_id: str,
        sensor_key: str,
        name_suffix: str,
        icon: str,
    ) -> None:
        super().__init__(coordinator)
        self._room_id = room_id
        self._sensor_key = sensor_key
        self._attr_name = f"ACC {room_id.replace('_', ' ').title()} {name_suffix}"
        self._attr_unique_id = f"{DOMAIN}_{entry.entry_id}_{room_id}_{sensor_key}"
        self._attr_icon = icon
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry.entry_id}_{room_id}")},
            name=f"Adaptive Climate Control - {room_id.replace('_', ' ').title()}",
            manufacturer="Adaptive Climate Control",
            model="Learning Climate Controller",
            sw_version=VERSION,
        )

    def _room_data(self) -> dict | None:
        """Return the coordinator data for this room."""
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get(self._room_id)


# -------------------------------------------------------------------
# Predicted comfort temperature
# -------------------------------------------------------------------

class ACCPredictedTempSensor(ACCBaseSensor):
    """Current almanac target temperature for this room and time slot."""

    def __init__(self, coordinator, entry, room_id):
        super().__init__(
            coordinator, entry, room_id,
            sensor_key="predicted_temp",
            name_suffix="Predicted Temperature",
            icon="mdi:thermometer-auto",
        )
        self._attr_native_unit_of_measurement = "°C"
        self._attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def native_value(self) -> float | None:
        data = self._room_data()
        if data is None:
            return None
        return round(data.get("target_temp", 0), 1)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self._room_data()
        if data is None:
            return {}
        band = data.get("good_band", (None, None))
        return {
            "good_band_low": round(band[0], 1) if band[0] is not None else None,
            "good_band_high": round(band[1], 1) if band[1] is not None else None,
        }


# -------------------------------------------------------------------
# Almanac confidence
# -------------------------------------------------------------------

class ACCConfidenceSensor(ACCBaseSensor):
    """Confidence score for the current almanac slot (0-100)."""

    def __init__(self, coordinator, entry, room_id):
        super().__init__(
            coordinator, entry, room_id,
            sensor_key="confidence",
            name_suffix="Almanac Confidence",
            icon="mdi:chart-line",
        )
        self._attr_native_unit_of_measurement = "%"
        self._attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def native_value(self) -> float | None:
        data = self._room_data()
        if data is None:
            return None
        return round(data.get("confidence", 0), 1)


# -------------------------------------------------------------------
# Activity state
# -------------------------------------------------------------------

class ACCActivityStateSensor(ACCBaseSensor):
    """Current activity state for this room."""

    def __init__(self, coordinator, entry, room_id):
        super().__init__(
            coordinator, entry, room_id,
            sensor_key="activity_state",
            name_suffix="Activity State",
            icon="mdi:account-check",
        )

    @property
    def native_value(self) -> str | None:
        data = self._room_data()
        if data is None:
            return None
        return data.get("activity")


# -------------------------------------------------------------------
# Current period
# -------------------------------------------------------------------

class ACCCurrentPeriodSensor(ACCBaseSensor):
    """Current time period (morning / day / afternoon / evening / overnight)."""

    def __init__(self, coordinator, entry, room_id):
        super().__init__(
            coordinator, entry, room_id,
            sensor_key="current_period",
            name_suffix="Current Period",
            icon="mdi:clock-time-four-outline",
        )

    @property
    def native_value(self) -> str | None:
        data = self._room_data()
        if data is None:
            return None
        return data.get("period")


# -------------------------------------------------------------------
# Corrective action state
# -------------------------------------------------------------------

class ACCCorrectiveStateSensor(ACCBaseSensor):
    """Current corrective action state (idle / cooling_active / warming_active)."""

    def __init__(self, coordinator, entry, room_id):
        super().__init__(
            coordinator, entry, room_id,
            sensor_key="corrective_state",
            name_suffix="Corrective State",
            icon="mdi:thermostat",
        )

    @property
    def native_value(self) -> str | None:
        data = self._room_data()
        if data is None:
            return None
        return data.get("corrective_state")

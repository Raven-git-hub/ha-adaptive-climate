"""
Climate Watcher for Adaptive Climate Control.

Listens for manual adjustments to climate entities and records them
as learning signals in the almanac via the coordinator.

A manual adjustment is detected when the target temperature changes
on a climate entity and the change was NOT initiated by this integration.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING

from homeassistant.core import HomeAssistant, callback, Event
from homeassistant.helpers.event import async_track_state_change_event

if TYPE_CHECKING:
    from .coordinator import AdaptiveClimateCoordinator

_LOGGER = logging.getLogger(__name__)


class ClimateWatcher:
    """
    Watches one or more climate entities for manual temperature adjustments.
    When a change is detected that was not made by the coordinator,
    it records an intervention with the almanac.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: "AdaptiveClimateCoordinator",
    ) -> None:
        self.hass = hass
        self.coordinator = coordinator

        # Track the last known target temp per entity so we can detect direction
        self._last_target: dict[str, float | None] = {}

        # Track temps set by the coordinator so we can ignore them
        self._coordinator_set: dict[str, float | None] = {}

        # Unsub callbacks for cleanup
        self._unsub_listeners: list = []

    def start(self) -> None:
        """Register state change listeners for all watched climate entities."""
        entity_ids = self._get_all_climate_entities()

        if not entity_ids:
            _LOGGER.warning("ClimateWatcher: no climate entities to watch")
            return

        # Seed last known targets from current state
        for entity_id in entity_ids:
            state = self.hass.states.get(entity_id)
            if state:
                self._last_target[entity_id] = self._extract_target_temp(state)
            else:
                self._last_target[entity_id] = None
            self._coordinator_set[entity_id] = None

        unsub = async_track_state_change_event(
            self.hass,
            entity_ids,
            self._handle_state_change,
        )
        self._unsub_listeners.append(unsub)
        _LOGGER.info(
            "ClimateWatcher started, watching: %s",
            ", ".join(entity_ids),
        )

    def stop(self) -> None:
        """Unregister all state change listeners."""
        for unsub in self._unsub_listeners:
            unsub()
        self._unsub_listeners.clear()
        _LOGGER.info("ClimateWatcher stopped")

    def notify_coordinator_set(self, entity_id: str, temperature: float) -> None:
        """
        Called by the coordinator before it sets a temperature,
        so the watcher knows to ignore the resulting state change.
        """
        self._coordinator_set[entity_id] = temperature
        _LOGGER.debug(
            "ClimateWatcher: coordinator set %.1f on %s - will ignore",
            temperature,
            entity_id,
        )

    @callback
    def _handle_state_change(self, event: Event) -> None:
        """Handle a state change event on a watched climate entity."""
        entity_id = event.data.get("entity_id")
        new_state = event.data.get("new_state")
        old_state = event.data.get("old_state")

        if new_state is None:
            return

        new_target = self._extract_target_temp(new_state)
        old_target = self._extract_target_temp(old_state) if old_state else None

        if new_target is None:
            return

        # No change in target temperature
        if new_target == old_target:
            return

        # Was this change made by the coordinator? If so, ignore it
        coordinator_temp = self._coordinator_set.get(entity_id)
        if coordinator_temp is not None and abs(new_target - coordinator_temp) < 0.1:
            _LOGGER.debug(
                "ClimateWatcher: ignoring coordinator-initiated change on %s",
                entity_id,
            )
            self._coordinator_set[entity_id] = None
            self._last_target[entity_id] = new_target
            return

        # This is a manual adjustment - record it
        last = self._last_target.get(entity_id)
        if last is not None:
            direction = "up" if new_target > last else "down"
        else:
            direction = "up" if new_target > 21.0 else "down"

        self._last_target[entity_id] = new_target

        _LOGGER.info(
            "ClimateWatcher: manual adjustment detected on %s - %.1f -> %.1f (%s)",
            entity_id,
            last if last is not None else 0.0,
            new_target,
            direction,
        )

        # Find which room this entity belongs to and record the intervention
        room_id = self._get_room_for_entity(entity_id)
        if room_id is None:
            _LOGGER.warning(
                "ClimateWatcher: could not find room for entity %s",
                entity_id,
            )
            return

        self.coordinator.record_intervention(room_id, new_target, direction)

    def _extract_target_temp(self, state) -> float | None:
        """Pull the target temperature from a climate entity state."""
        if state is None:
            return None
        try:
            temp = state.attributes.get("temperature")
            if temp is not None:
                return float(temp)
        except (ValueError, TypeError):
            pass
        return None

    def _get_all_climate_entities(self) -> list[str]:
        """Return all climate entity IDs across all configured rooms."""
        entities = []
        for room_cfg in self.coordinator.rooms_config:
            entity_id = room_cfg.get("climate_entity")
            if entity_id and entity_id not in entities:
                entities.append(entity_id)
        return entities

    def _get_room_for_entity(self, entity_id: str) -> str | None:
        """Find the room_id that owns a given climate entity."""
        for room_cfg in self.coordinator.rooms_config:
            if room_cfg.get("climate_entity") == entity_id:
                return room_cfg.get("room_id")
        return None

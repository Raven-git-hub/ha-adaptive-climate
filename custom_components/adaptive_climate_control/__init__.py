"""
Adaptive Climate Control Integration for Home Assistant.

This integration learns your temperature preferences over time,
building a personal comfort almanac to intelligently control
your climate system.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

from .const import DOMAIN, CONF_ROOMS
from .coordinator import AdaptiveClimateCoordinator
from .climate_watcher import ClimateWatcher

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["sensor"]


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up the Adaptive Climate Control component."""
    hass.data.setdefault(DOMAIN, {})
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Adaptive Climate Control from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    rooms_config = entry.data.get(CONF_ROOMS, [])
    log_dir = entry.data.get("log_dir", hass.config.path("adaptive_climate_control_logs"))

    # Create coordinator
    coordinator = AdaptiveClimateCoordinator(
        hass=hass,
        config_entry_id=entry.entry_id,
        rooms_config=rooms_config,
        log_dir=log_dir,
    )

    # Load persisted almanac data
    try:
        await coordinator.async_setup()
    except Exception as err:
        _LOGGER.error("Failed to set up Adaptive Climate Control: %s", err)
        raise ConfigEntryNotReady from err

    # Run first refresh
    await coordinator.async_config_entry_first_refresh()

    # Create and start the climate watcher
    watcher = ClimateWatcher(hass, coordinator)
    watcher.start()

    # Store references for platform setup and unload
    hass.data[DOMAIN][entry.entry_id] = {
        "coordinator": coordinator,
        "watcher": watcher,
    }

    # Forward setup to sensor platform
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Register reload handler
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))

    await async_setup_services(hass)
    _LOGGER.info("Adaptive Climate Control set up successfully")
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    # Stop the climate watcher
    entry_data = hass.data[DOMAIN].get(entry.entry_id, {})
    watcher: ClimateWatcher | None = entry_data.get("watcher")
    if watcher:
        watcher.stop()

    # Save almanac before unloading
    coordinator: AdaptiveClimateCoordinator | None = entry_data.get("coordinator")
    if coordinator:
        await coordinator.async_save()

    # Unload platforms
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)
        _LOGGER.info("Adaptive Climate Control unloaded successfully")

    return unload_ok


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload a config entry when options are updated."""
    await async_unload_entry(hass, entry)
    await async_setup_entry(hass, entry)


async def async_setup_services(hass: HomeAssistant) -> None:
    """Register Adaptive Climate Control services."""
    import voluptuous as vol
    from homeassistant.helpers import config_validation as cv

    async def handle_nudge_temperature(call):
        """Handle a nudge_temperature service call from the Lovelace card."""
        room_id = call.data.get("room_id")
        direction = call.data.get("direction")

        for entry_id, entry_data in hass.data.get(DOMAIN, {}).items():
            coordinator = entry_data.get("coordinator")
            if coordinator and room_id in coordinator.almanacs:
                coordinator.record_intervention(room_id, coordinator.almanacs[room_id].current_target(), direction)
                _LOGGER.info("Nudge service: room=%s direction=%s", room_id, direction)
                return

        _LOGGER.warning("Nudge service: room_id '%s' not found", room_id)

    hass.services.async_register(
        DOMAIN,
        "nudge_temperature",
        handle_nudge_temperature,
        schema=vol.Schema({
            vol.Required("room_id"): cv.string,
            vol.Required("direction"): vol.In(["up", "down"]),
        }),
    )

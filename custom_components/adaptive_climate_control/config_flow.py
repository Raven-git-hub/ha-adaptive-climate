"""
Config Flow for Adaptive Climate Control.

Guides the user through setting up:
1. Log directory
2. Rooms (name, climate entity, presence, sleep sensor)
3. Temperature sensors and trust levels per room
4. Default temperatures per period per room
5. Option to add another room
"""

from __future__ import annotations

import re
import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
import homeassistant.helpers.config_validation as cv

from .const import (
    DOMAIN,
    CONF_ROOMS,
    CONF_CLIMATE_ENTITY,
    CONF_SENSORS,
    CONF_SENSOR_TRUST,
    CONF_PRESENCE,
    CONF_SLEEP_SENSOR,
    CONF_DEFAULT_TEMPS,
    PERIOD_NAMES,
)

_LOGGER = logging.getLogger(__name__)

TRUST_OPTIONS = ["reliable", "uncertain", "unreliable"]
ROOM_ID_PATTERN = re.compile(r"^[a-z0-9_]+$")


def _get_climate_entities(hass: HomeAssistant) -> list[str]:
    """Return all climate entity IDs available in HA."""
    return [
        state.entity_id
        for state in hass.states.async_all("climate")
    ]


def _get_sensor_entities(hass: HomeAssistant) -> list[str]:
    """Return all temperature sensor entity IDs available in HA."""
    return [
        state.entity_id
        for state in hass.states.async_all("sensor")
        if state.attributes.get("device_class") == "temperature"
    ]


def _get_presence_entities(hass: HomeAssistant) -> list[str]:
    """Return person, device_tracker, and binary_sensor entities for presence."""
    entities = []
    for domain in ("person", "device_tracker", "binary_sensor"):
        entities.extend([
            state.entity_id
            for state in hass.states.async_all(domain)
        ])
    return entities


def _get_binary_sensors(hass: HomeAssistant) -> list[str]:
    """Return binary sensors suitable for sleep detection."""
    return [
        state.entity_id
        for state in hass.states.async_all("binary_sensor")
    ]


class AdaptiveClimateConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle the config flow for Adaptive Climate Control."""

    VERSION = 1

    def __init__(self) -> None:
        self._rooms: list[dict[str, Any]] = []
        self._current_room: dict[str, Any] = {}
        self._log_dir: str = ""

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Step 1: Log directory."""
        if self._async_current_entries():
            return self.async_abort(reason="already_configured")

        errors = {}
        default_log_dir = self.hass.config.path("adaptive_climate_control_logs")

        if user_input is not None:
            self._log_dir = user_input.get("log_dir", default_log_dir)
            return await self.async_step_room()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Optional("log_dir", default=default_log_dir): str,
            }),
            errors=errors,
        )

    async def async_step_room(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Step 2: Configure a room."""
        errors = {}
        climate_entities = _get_climate_entities(self.hass)
        presence_entities = _get_presence_entities(self.hass)
        binary_sensors = _get_binary_sensors(self.hass)

        if user_input is not None:
            room_id = user_input.get("room_id", "").strip().lower()

            if not ROOM_ID_PATTERN.match(room_id):
                errors["room_id"] = "invalid_room_id"
            elif any(r["room_id"] == room_id for r in self._rooms):
                errors["room_id"] = "room_id_exists"
            elif user_input.get("climate_entity") not in climate_entities:
                errors["climate_entity"] = "invalid_entity"
            else:
                self._current_room = {
                    "room_id": room_id,
                    CONF_CLIMATE_ENTITY: user_input["climate_entity"],
                    CONF_PRESENCE: user_input.get("presence_entities", []),
                    CONF_SLEEP_SENSOR: user_input.get("sleep_sensor"),
                    CONF_SENSORS: [],
                    CONF_DEFAULT_TEMPS: {},
                }
                return await self.async_step_sensors()

        schema = vol.Schema({
            vol.Required("room_id"): str,
            vol.Required(CONF_CLIMATE_ENTITY): vol.In(climate_entities),
            vol.Optional(CONF_PRESENCE, default=[]): cv.multi_select(
                {e: e for e in presence_entities}
            ),
            vol.Optional(CONF_SLEEP_SENSOR): vol.In([""] + binary_sensors),
        })

        return self.async_show_form(
            step_id="room",
            data_schema=schema,
            errors=errors,
        )

    async def async_step_sensors(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Step 3: Add temperature sensors to this room."""
        errors = {}
        sensor_entities = _get_sensor_entities(self.hass)

        if user_input is not None:
            if user_input.get("sensor_entity") not in sensor_entities:
                errors["sensor_entity"] = "invalid_entity"
            else:
                self._current_room[CONF_SENSORS].append({
                    "entity_id": user_input["sensor_entity"],
                    CONF_SENSOR_TRUST: user_input.get("sensor_trust", "uncertain"),
                })

                if user_input.get("add_another_sensor", False):
                    return await self.async_step_sensors()

                return await self.async_step_defaults()

        schema = vol.Schema({
            vol.Required("sensor_entity"): vol.In(sensor_entities),
            vol.Optional(CONF_SENSOR_TRUST, default="uncertain"): vol.In(TRUST_OPTIONS),
            vol.Optional("add_another_sensor", default=False): bool,
        })

        return self.async_show_form(
            step_id="sensors",
            data_schema=schema,
            errors=errors,
        )

    async def async_step_defaults(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Step 4: Set default temperatures per period."""
        errors = {}

        if user_input is not None:
            valid = True
            for period in PERIOD_NAMES:
                temp = user_input.get(period, 21.0)
                if not (10.0 <= temp <= 35.0):
                    errors[period] = "invalid_temperature"
                    valid = False

            if valid:
                self._current_room[CONF_DEFAULT_TEMPS] = {
                    period: user_input[period]
                    for period in PERIOD_NAMES
                }
                self._rooms.append(self._current_room)
                self._current_room = {}
                return await self.async_step_another_room()

        schema = vol.Schema({
            vol.Optional("morning", default=21.0): vol.Coerce(float),
            vol.Optional("day", default=22.0): vol.Coerce(float),
            vol.Optional("afternoon", default=22.0): vol.Coerce(float),
            vol.Optional("evening", default=21.0): vol.Coerce(float),
            vol.Optional("overnight", default=19.0): vol.Coerce(float),
        })

        return self.async_show_form(
            step_id="defaults",
            data_schema=schema,
            errors=errors,
        )

    async def async_step_another_room(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Step 5: Ask if the user wants to add another room."""
        if user_input is not None:
            if user_input.get("add_another", False):
                return await self.async_step_room()
            return self._create_entry()

        return self.async_show_form(
            step_id="another_room",
            data_schema=vol.Schema({
                vol.Optional("add_another", default=False): bool,
            }),
        )

    def _create_entry(self) -> config_entries.FlowResult:
        """Create the config entry with all collected data."""
        return self.async_create_entry(
            title="Adaptive Climate Control",
            data={
                CONF_ROOMS: self._rooms,
                "log_dir": self._log_dir,
            },
        )

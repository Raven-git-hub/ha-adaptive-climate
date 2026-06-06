"""Constants for Adaptive Climate Control."""

DOMAIN = "adaptive_climate_control"
VERSION = "0.1.0"

PERIODS = {
    "morning":   {"start": 5,  "end": 9},
    "day":       {"start": 9,  "end": 12},
    "afternoon": {"start": 12, "end": 17},
    "evening":   {"start": 17, "end": 23},
    "overnight": {"start": 23, "end": 5},
}

PERIOD_NAMES = list(PERIODS.keys())

ALMANAC_DAYS = 365
ALMANAC_LEARNING_WINDOW = 21
ALMANAC_EWMA_ALPHA = 0.3

TRUST_HIGH   = 0.8
TRUST_MEDIUM = 0.5
TRUST_LOW    = 0.2

TRUST_THRESHOLD_MIN = 0.5
TRUST_THRESHOLD_MAX = 5.0

SENSOR_HISTORY_WINDOW = 30

FAN_SPEEDS = ["low", "medium", "high"]

COOLING_SETPOINT_OFFSET = -2.0
COOLING_SETPOINT_OFFSET_UNOCCUPIED = -1.0
WARMING_SETPOINT_OFFSET = 1.0

CORRECTIVE_ACTION_TIMEOUT = 60

ACTIVITY_ACTIVE    = "active"
ACTIVITY_ASLEEP    = "asleep"
ACTIVITY_AWAY      = "away"

ACTIVITY_STATES = [ACTIVITY_ACTIVE, ACTIVITY_ASLEEP, ACTIVITY_AWAY]

SLEEP_TEMP_OFFSET = -1.5

ACTION_IDLE    = "idle"
ACTION_COOLING = "cooling_active"
ACTION_WARMING = "warming_active"

CONF_ROOMS          = "rooms"
CONF_CLIMATE_ENTITY = "climate_entity"
CONF_SENSORS        = "sensors"
CONF_SENSOR_TRUST   = "sensor_trust"
CONF_PRESENCE       = "presence_entities"
CONF_SLEEP_SENSOR   = "sleep_sensor"
CONF_AWAY_SETPOINT  = "away_setpoint"
CONF_SLEEP_OFFSET   = "sleep_offset"
CONF_DEFAULT_TEMPS  = "default_temperatures"

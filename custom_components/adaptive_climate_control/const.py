cat > custom_components/adaptive_climate_control/const.py << 'EOF'
"""Constants for Adaptive Climate Control."""

DOMAIN = "adaptive_climate_control"
VERSION = "0.1.0"

# -------------------------------------------------------------------
# Time periods
# Each period is defined by its name and start hour (24h, inclusive)
# The period ends when the next one begins
# -------------------------------------------------------------------
PERIODS = {
    "morning":   {"start": 5,  "end": 9},
    "day":       {"start": 9,  "end": 12},
    "afternoon": {"start": 12, "end": 17},
    "evening":   {"start": 17, "end": 23},
    "overnight": {"start": 23, "end": 5},
}

PERIOD_NAMES = list(PERIODS.keys())

# -------------------------------------------------------------------
# Almanac
# -------------------------------------------------------------------
ALMANAC_DAYS = 365          # slots per period
ALMANAC_LEARNING_WINDOW = 21  # days of raw event history to retain
ALMANAC_EWMA_ALPHA = 0.3    # learning rate (0=ignore new, 1=forget old)

# -------------------------------------------------------------------
# Sensor trust
# -------------------------------------------------------------------
TRUST_HIGH   = 0.8   # seed for "Reliable" sensors
TRUST_MEDIUM = 0.5   # seed for "Uncertain" sensors (default)
TRUST_LOW    = 0.2   # seed for "Unreliable" sensors

TRUST_THRESHOLD_MIN = 0.5   # action threshold at full trust (°C)
TRUST_THRESHOLD_MAX = 5.0   # action threshold at zero trust (°C)

SENSOR_HISTORY_WINDOW = 30  # minutes of rolling sensor readings to retain

# -------------------------------------------------------------------
# Control logic
# -------------------------------------------------------------------
FAN_SPEEDS = ["low", "medium", "high"]

COOLING_SETPOINT_OFFSET  = -2.0   # °C below target when cooling aggressively
COOLING_SETPOINT_OFFSET_UNOCCUPIED = -1.0
WARMING_SETPOINT_OFFSET  = 1.0    # °C above target when warming

CORRECTIVE_ACTION_TIMEOUT = 60    # minutes before a corrective action expires

# -------------------------------------------------------------------
# Activity states
# -------------------------------------------------------------------
ACTIVITY_ACTIVE    = "active"
ACTIVITY_ASLEEP    = "asleep"
ACTIVITY_AWAY      = "away"

ACTIVITY_STATES = [ACTIVITY_ACTIVE, ACTIVITY_ASLEEP, ACTIVITY_AWAY]

SLEEP_TEMP_OFFSET  = -1.5   # °C reduction during sleep

# -------------------------------------------------------------------
# Corrective action states
# -------------------------------------------------------------------
ACTION_IDLE    = "idle"
ACTION_COOLING = "cooling_active"
ACTION_WARMING = "warming_active"

# -------------------------------------------------------------------
# Config entry keys
# These are the keys used when storing config flow data
# -------------------------------------------------------------------
CONF_ROOMS          = "rooms"
CONF_CLIMATE_ENTITY = "climate_entity"
CONF_SENSORS        = "sensors"
CONF_SENSOR_TRUST   = "sensor_trust"
CONF_PRESENCE       = "presence_entities"
CONF_SLEEP_SENSOR   = "sleep_sensor"
CONF_AWAY_SETPOINT  = "away_setpoint"
CONF_SLEEP_OFFSET   = "sleep_offset"
CONF_DEFAULT_TEMPS  = "default_temperatures"
EOF

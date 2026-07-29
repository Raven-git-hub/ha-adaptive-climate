-- =====================================================================
-- Adaptive Climate - SQLite storage schema (v1)
--
-- CSV on disk is the archival source of truth; this database is the
-- query layer for the analyser and the UI. It is always rebuildable
-- from CSV, so the container holds no irreplaceable state.
--
-- Design notes:
--   * Sensor readings and unit states are stored LONG, not wide. Rooms
--     have a variable number of each. Long format also makes "average
--     this sensor only over settled samples" a WHERE clause rather than
--     a special case.
--   * Section is RECORDED at observation time, never inferred later from
--     the clock.
--   * All timestamps are ISO-8601 with UTC offset, stored as TEXT.
--     ts_utc is carried alongside for safety across DST or relocation.
--   * Where Light stored one ambient_lux + per-group brightness, Climate
--     stores per-sensor temperature + per-unit climate state, and splits
--     the almanac into a per-unit setpoint table and a per-sensor
--     comfort/band/trust table. See docs/TRUST_MODEL.md.
-- =====================================================================

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;


-- ---------------------------------------------------------------------
-- Config snapshots
-- Every version that was ever active, so a historical row can always be
-- interpreted with the config that produced it.
-- ---------------------------------------------------------------------
CREATE TABLE config_version (
    id            INTEGER PRIMARY KEY,
    applied_at    TEXT    NOT NULL,
    payload       TEXT    NOT NULL,          -- full config JSON
    payload_sha   TEXT    NOT NULL UNIQUE
);


-- ---------------------------------------------------------------------
-- Section runs
-- One row per room per section per day, written at crossover. Records
-- planned vs. actual so a collapsed or missed section is visible.
-- (Climate has no sun collisions, but a missed crossover after downtime
-- is still worth recording.)
-- ---------------------------------------------------------------------
CREATE TABLE section_run (
    id              INTEGER PRIMARY KEY,
    room_id         TEXT    NOT NULL,
    local_date      TEXT    NOT NULL,        -- YYYY-MM-DD, local
    section         TEXT    NOT NULL
                    CHECK (section IN ('sunrise','day','afternoon','sunset','night','sleep')),
    planned_start   TEXT,                    -- computed boundary
    actual_start    TEXT,                    -- when the scene was really fired
    ended_at        TEXT,
    outcome         TEXT    NOT NULL DEFAULT 'ran'
                    CHECK (outcome IN ('ran','missed','caught_up')),
    outcome_reason  TEXT,
    UNIQUE (room_id, local_date, section)
);
CREATE INDEX idx_section_run_lookup ON section_run(room_id, local_date);


-- ---------------------------------------------------------------------
-- Heartbeats - room level
-- ---------------------------------------------------------------------
CREATE TABLE heartbeat (
    id              INTEGER PRIMARY KEY,
    room_id         TEXT    NOT NULL,
    ts              TEXT    NOT NULL,        -- local ISO-8601 with offset
    ts_utc          TEXT    NOT NULL,
    local_date      TEXT    NOT NULL,
    section         TEXT    NOT NULL,        -- recorded, not inferred
    sensor_n        INTEGER NOT NULL DEFAULT 0,   -- how many sensors reported
    occupied        INTEGER,                 -- NULL if no presence configured (reserved)
    any_unit_on     INTEGER NOT NULL,
    deferred_ms     INTEGER NOT NULL DEFAULT 0,   -- >0 if delayed waiting for the guard to clear
    UNIQUE (room_id, ts)
);
CREATE INDEX idx_heartbeat_scan ON heartbeat(room_id, local_date, section);
CREATE INDEX idx_heartbeat_time ON heartbeat(room_id, ts_utc);


-- ---------------------------------------------------------------------
-- Heartbeats - per sensor
-- One temperature reading per sensor per heartbeat. The individual
-- readings matter (each sensor has its own comfort/trust), so unlike
-- Light there is no single room-level ambient value.
-- ---------------------------------------------------------------------
CREATE TABLE heartbeat_sensor (
    heartbeat_id    INTEGER NOT NULL REFERENCES heartbeat(id) ON DELETE CASCADE,
    sensor_id       TEXT    NOT NULL,
    temperature     REAL,                    -- NULL if the sensor was unavailable
    PRIMARY KEY (heartbeat_id, sensor_id)
);
CREATE INDEX idx_hbsensor_sensor ON heartbeat_sensor(sensor_id);


-- ---------------------------------------------------------------------
-- Heartbeats - per unit
-- The full climate state, not just an on/off + one number. is_on is
-- stored explicitly rather than derived, because a unit can be on at any
-- setpoint.
-- ---------------------------------------------------------------------
CREATE TABLE heartbeat_unit (
    heartbeat_id    INTEGER NOT NULL REFERENCES heartbeat(id) ON DELETE CASCADE,
    unit_id         TEXT    NOT NULL,
    is_on           INTEGER NOT NULL,
    hvac_mode       TEXT,                    -- cool | fan_only | dry | off ...
    fan_mode        TEXT,                    -- low | medium ...
    setpoint        REAL,                    -- target temperature the unit sits on
    current_temp    REAL,                    -- the unit's own reported temperature, if any
    ac_state        TEXT                     -- normal | cooling | warming | leak (as last driven)
                    CHECK (ac_state IN ('normal','cooling','warming','leak') OR ac_state IS NULL),
    PRIMARY KEY (heartbeat_id, unit_id)
);
CREATE INDEX idx_hbunit_unit ON heartbeat_unit(unit_id, is_on);


-- ---------------------------------------------------------------------
-- Reactive events - room level
-- One row per consolidated window, not per state change.
-- ---------------------------------------------------------------------
CREATE TABLE reactive (
    id                  INTEGER PRIMARY KEY,
    room_id             TEXT    NOT NULL,
    ts                  TEXT    NOT NULL,
    ts_utc              TEXT    NOT NULL,
    local_date          TEXT    NOT NULL,
    section             TEXT    NOT NULL,
    window_seconds      INTEGER NOT NULL,
    occupied            INTEGER,
    suspended_maint     INTEGER NOT NULL DEFAULT 0,
    UNIQUE (room_id, ts)
);
CREATE INDEX idx_reactive_scan ON reactive(room_id, local_date, section);


-- ---------------------------------------------------------------------
-- Reactive events - per unit (what the user changed)
-- ---------------------------------------------------------------------
CREATE TABLE reactive_unit (
    reactive_id         INTEGER NOT NULL REFERENCES reactive(id) ON DELETE CASCADE,
    unit_id             TEXT    NOT NULL,
    setpoint_before     REAL,
    setpoint_after      REAL,
    changed             INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (reactive_id, unit_id)
);


-- ---------------------------------------------------------------------
-- Reactive events - per sensor snapshot (what each sensor read when the
-- user reacted). This is the raw material for the trust model: the
-- deviation of each sensor from its learned comfort at reaction time is
-- what tightens its band and raises its trust.
-- ---------------------------------------------------------------------
CREATE TABLE reactive_sensor (
    reactive_id         INTEGER NOT NULL REFERENCES reactive(id) ON DELETE CASCADE,
    sensor_id           TEXT    NOT NULL,
    temperature         REAL,
    PRIMARY KEY (reactive_id, sensor_id)
);


-- ---------------------------------------------------------------------
-- Almanac - header, one row per room per section per publication
-- ---------------------------------------------------------------------
CREATE TABLE almanac (
    id              INTEGER PRIMARY KEY,
    room_id         TEXT    NOT NULL,
    section         TEXT    NOT NULL
                    CHECK (section IN ('sunrise','day','afternoon','sunset','night','sleep')),
    valid_from      TEXT    NOT NULL,        -- YYYY-MM-DD; the almanac's HA sensor state
    state           TEXT    NOT NULL
                    CHECK (state IN ('provisional','bootstrap','learning')),
    sample_days     INTEGER NOT NULL DEFAULT 0,
    confidence      REAL,
    built_at        TEXT    NOT NULL,
    UNIQUE (room_id, section, valid_from)
);
CREATE INDEX idx_almanac_lookup ON almanac(room_id, section, valid_from);


-- ---------------------------------------------------------------------
-- Almanac - per unit: the learned setpoint (what we act on)
-- setpoint NULL means "no opinion yet"; off=1 is an explicit forced-off.
-- ---------------------------------------------------------------------
CREATE TABLE almanac_unit (
    almanac_id      INTEGER NOT NULL REFERENCES almanac(id) ON DELETE CASCADE,
    unit_id         TEXT    NOT NULL,
    setpoint        REAL,
    off             INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (almanac_id, unit_id)
);


-- ---------------------------------------------------------------------
-- Almanac - per sensor: comfort reading, band half-width and trust
-- (what we watch). comfort is the reading the sensor shows while you are
-- comfortable at the unit setpoint; band is the half-width in degrees;
-- trust is the normalised inverse of band, 0..1. See docs/TRUST_MODEL.md.
-- ---------------------------------------------------------------------
CREATE TABLE almanac_sensor (
    almanac_id      INTEGER NOT NULL REFERENCES almanac(id) ON DELETE CASCADE,
    sensor_id       TEXT    NOT NULL,
    comfort         REAL,
    band            REAL,                    -- half-width, degrees
    trust           REAL,                    -- 0..1
    PRIMARY KEY (almanac_id, sensor_id)
);


-- ---------------------------------------------------------------------
-- Event log - the visible record of what the container did and when.
-- ---------------------------------------------------------------------
CREATE TABLE event (
    id          INTEGER PRIMARY KEY,
    ts          TEXT    NOT NULL,
    ts_utc      TEXT    NOT NULL,
    room_id     TEXT,
    severity    TEXT    NOT NULL CHECK (severity IN ('debug','info','warning','error')),
    category    TEXT    NOT NULL
                CHECK (category IN (
                    'crossover',         -- section changed
                    'maintenance',       -- nudge observed / applied
                    'quorum',            -- votes crossed / cleared the quorum
                    'correction',        -- corrective action started / stopped
                    'reactive',          -- user intervention captured
                    'hold',              -- maintenance suspended / released
                    'heartbeat',         -- deferrals and gaps (debug normally)
                    'analysis',          -- analyser run, inputs and outcome
                    'almanac',           -- generated / published to HA
                    'leak',              -- leak detected / released
                    'connection',        -- HA websocket up, down, reconnecting
                    'deploy',            -- helpers and automations written
                    'config',            -- config changed, by whom
                    'validation'         -- missing or unavailable entity detected
                )),
    message     TEXT    NOT NULL,            -- human-readable, shown in the UI
    detail      TEXT                         -- optional JSON payload for expansion
);
CREATE INDEX idx_event_feed ON event(ts_utc DESC);
CREATE INDEX idx_event_filter ON event(room_id, category, severity, ts_utc DESC);


-- ---------------------------------------------------------------------
-- CSV ingest ledger - makes re-ingestion idempotent.
-- ---------------------------------------------------------------------
CREATE TABLE ingest_file (
    path            TEXT PRIMARY KEY,
    kind            TEXT NOT NULL CHECK (kind IN ('heartbeat','reactive')),
    room_id         TEXT NOT NULL,
    local_date      TEXT NOT NULL,
    content_sha     TEXT NOT NULL,
    rows_ingested   INTEGER NOT NULL DEFAULT 0,
    rows_skipped    INTEGER NOT NULL DEFAULT 0,
    last_ingest_at  TEXT NOT NULL
);


-- ---------------------------------------------------------------------
-- Convenience view: the almanac currently in force per room/section
-- ---------------------------------------------------------------------
CREATE VIEW current_almanac AS
SELECT a.*
FROM almanac a
JOIN (
    SELECT room_id, section, MAX(valid_from) AS vf
    FROM almanac
    WHERE valid_from <= date('now','localtime')
    GROUP BY room_id, section
) latest
  ON latest.room_id = a.room_id
 AND latest.section = a.section
 AND latest.vf = a.valid_from;

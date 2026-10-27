-- =====================================================================
-- Adaptive Climate - SQLite storage schema (v2, storage user_version 2)
--
-- CSV on disk is the archival source of truth; this database is the
-- query layer for the analyser and the UI. It is always rebuildable
-- from CSV, so the container holds no irreplaceable state.
--
-- Versioning: this file is the CURRENT shape. A fresh database runs it
-- and is stamped with the latest PRAGMA user_version; an older database
-- is brought to the same shape by the numbered migrations in
-- app/store.py, which recreate changed tables from the CREATE text in
-- this file. So: never edit a CREATE here without adding a migration,
-- and fresh and migrated databases stay byte-identical in sqlite_master
-- (tools/store_check.py checks this). user_version 0 = v1 (v1.0-poc),
-- 1 = v2 phase 0, 2 = phase 1 (open section runs closed; data only).
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
    -- v2 outdoor input, decided once per section (docs/V2_DESIGN.md)
    outdoor_mean    REAL,                    -- forecast mean over the window (or probe value)
    outdoor_max     REAL,                    -- forecast max (or probe value)
    comfort_point   REAL,                    -- room comfort point the outdoor value is compared with
    lean            REAL,                    -- k x delta, in band widths, before the per-edge caps
    blocked         INTEGER                  -- the block decision as computed, whether or not applied
                    CHECK (blocked IN (0,1) OR blocked IS NULL),
    block_mode      TEXT                     -- outdoor.block_mode then: blocked=1 + shadow = "would have blocked"
                    CHECK (block_mode IN ('off','shadow','active') OR block_mode IS NULL),
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
    outdoor_temp    REAL,                    -- v2: outdoor value at this heartbeat, NULL if none
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
    ac_state        TEXT                     -- v1: normal | cooling | warming | leak (inferred)
                    CHECK (ac_state IN ('normal','cooling','warming','leak') OR ac_state IS NULL),
    control_state   TEXT                     -- v2: the unit's state helper at this heartbeat
                    CHECK (control_state IN ('cool','boost','rest','user_off','manual',
                                             'config_off','leak') OR control_state IS NULL),
    control_offset  INTEGER,                 -- v2: section offset in whole steps
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
    kind                TEXT                 -- v2: room-level kind, derived from the units' kinds
                        CHECK (kind IN ('setpoint','on','off','manual','knob_turn','knob_press')
                               OR kind IS NULL),   -- NULL (pre-v2 rows) reads as 'setpoint'
    outdoor_temp        REAL,                -- v2: outdoor value when the person reacted
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
    kind                TEXT                 -- v2: this unit's reaction kind
                        CHECK (kind IN ('setpoint','on','off','manual','knob_turn','knob_press')
                               OR kind IS NULL),
    power_before        INTEGER              -- v2: unit on (1) / off (0) at the window's start
                        CHECK (power_before IN (0,1) OR power_before IS NULL),
    power_after         INTEGER              -- v2: unit on (1) / off (0) at the window's end
                        CHECK (power_after IN (0,1) OR power_after IS NULL),
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
-- Almanac - header, one row per room per section per publication.
-- Derived data: migrations drop and recreate the almanac tables rather
-- than copy them; the replay rebuilds them from history.
-- ---------------------------------------------------------------------
CREATE TABLE almanac (
    id              INTEGER PRIMARY KEY,
    room_id         TEXT    NOT NULL,
    section         TEXT    NOT NULL
                    CHECK (section IN ('sunrise','day','afternoon','sunset','night','sleep')),
    valid_from      TEXT    NOT NULL,        -- YYYY-MM-DD; the almanac's HA sensor state
    status          TEXT    NOT NULL         -- provisional until the first setpoint-moving reaction
                    CHECK (status IN ('provisional','learned')),
    confidence      REAL,                    -- 0..1; drives the UI, not control
    built_at        TEXT    NOT NULL,
    UNIQUE (room_id, section, valid_from)
);
CREATE INDEX idx_almanac_lookup ON almanac(room_id, section, valid_from);


-- ---------------------------------------------------------------------
-- Almanac - per unit: the learned setpoint (what we act on)
-- setpoint NULL means "no opinion yet"; off=1 is an explicit forced-off.
-- setpoint_applied is the whole-degree value the unit is driven to,
-- already rounded toward the latest setpoint-moving reaction.
-- ---------------------------------------------------------------------
CREATE TABLE almanac_unit (
    almanac_id      INTEGER NOT NULL REFERENCES almanac(id) ON DELETE CASCADE,
    unit_id         TEXT    NOT NULL,
    setpoint        REAL,                    -- precise, as learned
    setpoint_applied REAL,                   -- rounded; NULL until the phase 1 learner fills it
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
                    'validation',        -- missing or unavailable entity detected
                    'control',           -- v2 unit state change (boost, rest, release, ...)
                    'block',             -- v2 cooling-block decision, including shadow
                    'outdoor',           -- v2 outdoor data missing or failed
                    'storage'            -- database migration, backup, re-ingest
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
    kind            TEXT NOT NULL CHECK (kind IN ('heartbeat','reactive','section_run')),
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

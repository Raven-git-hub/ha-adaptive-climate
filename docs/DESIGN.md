# Design decisions

Adaptive Climate is adapted from Adaptive Light, which ran in production for
months. Where a decision carries over unchanged, it is stated briefly with a
pointer to the principle; where climate departs, the departure is the point.

## Inherited from Adaptive Light (unchanged)

1. **The config is the single source of truth.** Everything downstream — the
   helper/automation generator, the scheduler, the analyser, both UI data feeds —
   is a pure function of `schema/config.schema.json`. Learned data is *not* in
   the config; it lives in the almanac store and is regenerated nightly.

2. **Home Assistant owns behaviour; the container owns observation.** HA runs the
   automations and the maintenance loop so traces stay visible and debuggable.
   The container observes over WebSocket, learns, schedules crossovers, and pushes
   almanacs over REST. It never touches HA's filesystem.

3. **Scheduling is level-triggered.** A slow loop asks "which section should be
   active now?" and fires a crossover when the answer changes. Startup, reconnect,
   clock change and paused VM all take the same path. No catch-up routine.

4. **Two booleans, two meanings.** `ac_active_<room>` = an AC automation is
   mid-change, ignore what you see (lives seconds). `ac_hold_<room>` = a human
   intervened, maintenance stands down until the next crossover (lives hours).
   Conflating them would let a reactive event silently stop observation.

5. **Heartbeats defer, they do not skip.** A heartbeat that lands during a guard
   window waits for the guard to clear rather than sampling a mid-transition
   state, and records how long it waited.

6. **The almanac is pushed, not read from disk.** The container POSTs to
   `/api/states/sensor.ac_almanac_<room>`. No YAML edit, no restart, no
   allow-list, no `/share` mount. API-set states do not survive an HA restart, so
   the container re-pushes on a timer and on `homeassistant_start`.

7. **Generated artefacts contain no baked-in state.** The generator emits no
   literal setpoints. Forced-off units read the almanac at runtime and branch on
   `off`. Changing a unit's mode or a band needs only an almanac republish;
   regeneration is required only when *entities* change. Output is deterministic,
   so regeneration is a safe overwrite.

8. **Entity IDs only, never device IDs.** Device IDs are opaque UUIDs that break
   on re-pairing and cannot be validated by an entity-existence check.

9. **One `input_select`, not six booleans.** A single select for the active
   section cannot desynchronise the way a set of booleans can.

10. **CSV is the archive; SQLite is the query layer.** Every observation is
    written to CSV first, then SQLite. SQLite is always rebuildable from CSV, so
    the container holds no irreplaceable state. Per-sensor and per-unit readings
    are stored **long**, not wide, because rooms have a variable number of each.

11. **The event log is a feature, not debug output.** The container is the only
    component that sees the whole picture; every action it takes is recorded and
    surfaced. `section_run` records planned vs. actual so a collapsed or missed
    section is a row with a reason, not silence.

12. **Reactive detection ignores automation-caused changes.** A setpoint change
    counts as a human intervention only when it was not caused by an automation —
    established by the guard boolean being on, or by the change carrying a
    `context.parent_id`. Otherwise our own maintenance loop, or a coexisting
    system during cutover, would be learned from at 5× weight and corrupt the
    almanac.

13. **Helpers are named from the room's stable id, not its display name.** The
    friendly name must slugify to the object id the generated automations
    reference. Deployment verifies this and refuses rather than deploy a broken
    reference.

## Departures for climate

### D1. The sensor trust model replaces the single measured value

Adaptive Light reduced a room to one measured lux (the mean of its sensors) and
one `lux_target`. Adaptive Climate keeps each sensor distinct: per-sensor comfort
reading, band and trust, plus a per-unit setpoint. This is the substance of the
project and has its own document — see `docs/TRUST_MODEL.md`.

### D2. Learning target: setpoint plus per-sensor comfort

The setpoint is learned the same way Light learned brightness (recency-weighted
rolling average over settled periods). What is new is that each sensor
separately learns the reading it shows while comfortable, and a trust/band pair
inferred from your reactions. A reaction teaches both "the setpoint was wrong"
and "believe this sensor more".

### D3. The AC unit is a state machine, not a dimmer

Light nudged one number. A climate unit is put into one of four states —
Normal / Cooling / Warming / Leak — each a distinct (fan speed, mode, setpoint
offset) triple, chosen by where the measured temperature sits relative to the
comfort band. The state table is in the README and fixed for now;
user-customisable profiles are on the roadmap. This makes the generated scene
and maintenance automations richer than Light's brightness nudge, and makes the
per-unit stored sample a small record (is_on, hvac_mode, fan_mode, setpoint,
current_temp) rather than a single 0–255 int.

### D4. The maintenance loop is quorum-based

Light nudged whenever measured lux left a simple margin around the target.
Climate tallies per-sensor votes and acts only when the quorum is met, stopping
when the tally falls below quorum or after one hour. See `docs/TRUST_MODEL.md`
for the voting rules. **Decided:** the quorum loop runs in **Home Assistant
templates**, on HA's own clock, preserving principle 2 (HA owns behaviour) and
surviving container downtime. The container's job is to publish an almanac
carrying everything the template needs — per-unit setpoint, per-sensor comfort
and band — in the shape fixed by `docs/ALMANAC_FORMAT.md`. The generated
maintenance template is Phase 5.

### D5. Sun machinery removed entirely

Light supported clock, sun-relative and composite (earliest/latest) triggers,
with a collision policy and a year-ahead collision scan, because lighting tracks
daylight. Climate tracks your routine, not the sun. All section triggers are
fixed clock times. The `astral` dependency, `sun.sun` verification, the
collision/priority machinery and the year-ahead scan are all gone. The scheduler
is correspondingly simpler: sort six clock times, fire crossovers as they pass.
`doctor.py` drops its sun checks.

### D6. Leak detection

Optional, per unit, chosen via a live `binary_sensor` picker on the Config page
(the same live-entity pattern as every other picker). When a sensor is picked,
the generator wires the leak automation's trigger directly to it going `on`,
and the release automation's condition directly to it no longer reading `on` -
no manual automation editing required. Left blank, the generator falls back to
its original behaviour: a dedicated boolean helper
`input_boolean.ac_leak_<room>_<unit>` and a stub automation with an empty
trigger for the user to wire their own sensor into by hand. Either way, while
the boolean is on the unit runs in Leak mode (DRY). Leak mode is a **latch**:
release requires the user's confirmation (`input_boolean.ac_leak_confirmed_
<room>_<unit>`) and, when the sensor is known, that it has actually stopped
reporting a leak - confirming alone is not enough while the sensor still reads
wet, closing the gap the manual-only path left open.

### D7. Temperature unit is configurable

`system.temperature_unit` is `C` (default) or `F`. Lux had no unit; temperature
does, and it affects display, the reactive minimum delta, and the maintenance
step. Internally everything is stored in the configured unit; conversion, if any,
is a UI concern.

### D8. Port and namespace

Adaptive Light occupies port 8099 and the `al_` / `AL_` namespace. To let both
run on the same Docker host, Adaptive Climate uses port **8098**, the `ac_`
helper/entity prefix, `AC_` environment variables, and the `adaptive-climate`
image and container names.

## Open decisions still to settle

- **Occupancy.** Light used presence to gate whether a heartbeat was eligible for
  learning. Climate is currently silent on it. Decide whether an empty room
  should stop cooling / suspend learning, or whether presence is irrelevant here.
  Schema reserves an optional `presence_sensors` slot per room so we can add it
  without a migration.
- **Direction of correction with mixed votes** — when high- and low-trust sensors
  disagree on sign. Current lean: weight by trust to pick the direction, but the
  quorum count itself stays unweighted per the spec.

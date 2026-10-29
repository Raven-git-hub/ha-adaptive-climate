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
   mid-change, ignore what you see (lives seconds); scenes, maintenance and the
   leak automations raise it around every command. `ac_hold_<room>` = a manual
   pause a person flips (v2: the system never raises or clears it); while it is
   on, scenes and maintenance send no commands. Conflating them would let a
   reactive event silently stop observation.

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

12. **Reactive detection ignores automation-caused changes.** A change counts as
    a human intervention only when it was not caused by an automation —
    established by the guard boolean being on, by the change carrying a
    `context.parent_id`, or (v2) by the unit now showing the command its state
    helper implies, which is our own command arriving after the guard came down
    (`app.control.matches_command`; HA's manual-change automation applies the
    same test). Otherwise our own maintenance loop, or a coexisting system
    during cutover, would be learned from and corrupt the almanac.

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

Light nudged one number. Each climate unit has a state, kept in Home Assistant
helpers so it survives container downtime (v2, phase 3; `docs/V2_DESIGN.md`,
Control): `cool` (cool, fan low, base + offset), `boost` (cool, fan medium,
base + offset − the boost step), `rest` (off, waiting), `user_off` and `manual`
(a person turned it off or chose another mode or fan; hands off until the
section changes), `config_off` (the section's scene forces it off) and `leak`
(off, latched). The system only ever cools a unit or turns it off: v1's
Warming state (fan_only) is gone, and leak protection turns the unit off
rather than to dry, which would still run the compressor.

The **base** is the section's setpoint (`setpoint_applied` at the section
change, or what a person chose since); the **offset** builds up in whole steps
within a section (−1 after a boost, +1 after a rest, capped at ±3) and resets at
the next section change. Commands are clamped to the unit's own range and sent
only on transitions. The per-unit helpers are `ac_state`, `ac_base`,
`ac_offset`, `ac_since`, `ac_grace_until` and `ac_unblocked`, all named
`<room>_<unit>` from stable ids. The README has the state table; the per-unit
stored sample stays a small record (is_on, hvac_mode, fan_mode, setpoint,
current_temp, plus the state and offset helpers).

### D4. The maintenance loop is quorum-based

Light nudged whenever measured lux left a simple margin around the target.
Climate tallies per-sensor votes: a sensor votes when it is past either of its
trigger edges (one band width from its comfort reading, leaned by the outdoor
temperature through `sensor.ac_conditions_<room>`; docs/V2_DESIGN.md), all
votes count toward one pooled quorum, and the trust-weighted
majority picks the direction, too warm or too cold. See `docs/TRUST_MODEL.md`
for the voting rules. Too warm boosts a cooling unit; too cold rests it; a boost
or rest ends when the sensors still on its side fall below the quorum (a boost
also after `boost_max_minutes`), with minimum times in heartbeats to protect the
compressor and a grace period after a person's change.

**Decided:** the loop runs in **Home Assistant templates**, on HA's own clock,
preserving principle 2 (HA owns behaviour) and surviving container downtime.
The logic is `app/control.py`, pure Python, rendered as Jinja by the generator:
the scene runs the section change, maintenance runs the votes and one step per
unit, and a per-unit manual-change automation applies a person's change to the
unit's helpers straight away. `tools/control_logic_check.py` replays
`tools/control_check.py`'s scenario table through the generated automations
against `app/control.py`. The container's job is to publish an almanac carrying
everything the templates need — per-unit `setpoint_applied`, per-sensor comfort,
band and trust — in the shape fixed by `docs/ALMANAC_FORMAT.md`.

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
trigger for the user to wire their own sensor into by hand.

**The boolean is not a passive record - it drives the AC.** The leak automation
raises `input_boolean.ac_leak_<room>_<unit>` and, in the same action list,
immediately puts the unit's state helper in `leak` and turns it OFF (no
setpoint, no fan: dry would still run the compressor, and a leaking unit keeps
leaking), under the guard and regardless of the hold. Maintenance checks the latch before anything else
too, so a latch raised by hand still wins, and holds a unit in `leak` without
re-sending. Critically, **every scene (crossover) automation for a leak-enabled
unit checks the leak boolean before anything else** - ahead of even an explicit
forced-off scene - and re-asserts OFF if it's on, instead of driving the unit
back to cooling. Without this, a
scheduled boundary landing while a leak was still active would have silently
reverted the unit to Cool, which was a real defect caught after the picker
shipped (see `tools/leak_response_check.py`).

Leak mode is a **latch**: release requires the user's confirmation
(`input_boolean.ac_leak_confirmed_<room>_<unit>`) and, when the sensor is
known, that it has actually stopped reporting a leak - confirming alone is not
enough while the sensor still reads wet. On release, the unit is treated as at
a section change immediately (cool at the section's setpoint, or rest or
config_off as the section says), rather than left off until whatever
scheduled crossover happens to be next. Released before there is an almanac,
the unit rests: still off, until there is a setpoint to cool to.

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

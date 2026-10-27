# Almanac format

The almanac is the learned model the system acts on. One JSON document per
room, built by the analyser's reaction replay (app/analyser.py), persisted by
the store (app/store.py), pushed to Home Assistant as the state attributes of
`sensor.ac_almanac_<room>`, and read back by the UI. This one shape is a
contract between all four, so it is written down here. How the values are
learned is in `docs/TRUST_MODEL.md` and `docs/V2_DESIGN.md` (Learning model).

Because D4 keeps the maintenance/quorum loop **in Home Assistant** (see
docs/DESIGN.md), the almanac must carry everything the generated HA template
needs to evaluate the quorum on its own clock: per unit the setpoint to drive,
and per sensor the comfort reading and band to vote against. Trust is included
for display and tie-breaking; the quorum count itself is unweighted.

## Shape

```json
{
  "room_id": "main_room",
  "sections": {
    "day": {
      "status": "learned",
      "valid_from": "2026-08-09",
      "confidence": 0.5,
      "units": {
        "main_room_ac": { "setpoint": 21.5, "setpoint_applied": 22.0, "off": false }
      },
      "sensors": {
        "living1": { "comfort": 25.0, "band": 1.0, "trust": 0.889 },
        "living2": { "comfort": 24.4, "band": 5.0, "trust": 0.0 },
        "living3": { "comfort": 25.3, "band": 5.0, "trust": 0.0 }
      }
    }
  }
}
```

## Field notes

- **status** — `provisional` until the section's first setpoint-moving
  reaction (a setpoint change or knob turn), then `learned`.
- **valid_from** — the local date of the replay that built this row. There is
  no validity delay: the replay runs right after a reaction, 65 minutes later
  for the comfort window, on a heartbeat that can seed something new, and
  nightly at 00:15, and each run's result is in force at once. The store's
  `current_almanac(room, as_of)` returns, per section, the newest row whose
  `valid_from <= as_of`. A new setpoint still only reaches the unit from the
  section's next run: until phase 3b the hold stops maintenance after a
  reaction, and from then the scene's per-unit base helper does.
- **confidence** — `1 − 0.5^(quiet/3)`, quiet being the completed runs since
  the section's last reaction that had a heartbeat and no reaction. For the UI
  (and later the knob's LED), never for control.
- **sections** — a section appears once anything in it is seeded. A section
  with no heartbeats yet has no entry.
- **units** — only seeded units appear: a unit needs a heartbeat in that
  section with it on and a setpoint. A missing unit means "leave it alone".
- **units[].setpoint** — the learned setpoint, stored precise.
- **units[].setpoint_applied** — the whole-degree value the templates drive,
  already rounded toward the latest setpoint-moving reaction (half up before
  there is one), so templates never round; they only clamp it to the unit's
  `[min_temp, max_temp]` (docs/HARDWARE.md) and add their correction offset.
  Absent or `null` means no seed - leave the unit as it is.
- **units[].off** — an explicit forced-off for this section. Scene "off" comes
  from config and is baked into the automation directly; this flag mirrors it so
  the two agree once an almanac exists. The replay always writes `false`.
- **sensors** — only seeded sensors appear (a reading in that section).
- **sensors[].comfort** — the reading this sensor shows while you are content.
  Not shared between sensors and not equal to the setpoint.
- **sensors[].band** — comfort half-width in degrees. The HA template votes when
  `abs(reading - comfort) >= band`. Tight = trusted.
- **sensors[].trust** — normalised inverse of band, 0..1. Display and
  tie-breaking only.

## What the HA maintenance template does with it (D4, implemented)

`app/generator.py:build_maintenance_automation` emits, per room, an automation
on a `time_pattern` trigger (every `heartbeat_interval_minutes`, HA's own clock
per D4). Per heartbeat, for the active section: read each sensor's
`comfort`/`band`, count votes (`abs(reading - comfort) >= band`) among sensors
that are currently available and have a learned comfort (unavailable/unlearned
sensors count toward neither the tally nor the quorum base), apply the quorum
rule (1→1, 2→2, 3+→ceil(n/2)), and if met, drive each eligible unit into
Cooling or Warming by the trust-weighted breach direction, from its
`setpoint_applied` clamped per `docs/HARDWARE.md`. A unit is corrected only if
it is already on, has a `setpoint_applied`, is not forced off for the section,
and (if leak-enabled) its leak boolean is off. The cap uses
`input_datetime.ac_correction_started_<room>`: reset every heartbeat the quorum
is not met, left untouched while it is, so elapsed time approximates how long
correction has been continuously warranted. The `ceil(n/2)` and trust-weighted
direction logic are specified in `docs/TRUST_MODEL.md`, and the Jinja
implementing them is cross-checked against `app.trust.evaluate_quorum` (the
tested reference) in `tools/maintenance_logic_check.py`, which also renders the
setpoint templates against a plain-Python clamp. Phase 3 replaces this
automation with the cool-or-rest state machine (docs/V2_DESIGN.md).

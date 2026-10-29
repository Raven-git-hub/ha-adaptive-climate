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
  "margin": 6.7,
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

- **margin** — the room's cooling-block margin M in degrees (phase 5): cooling
  is blocked for a section when its warmest forecast hour is at least M below
  the room comfort point. Learned per room by the replay
  (`app.analyser.learn_margin`, docs/V2_DESIGN.md, The cooling block) from
  `outdoor.initial_margin` (default 0), within 0 to 10: at 0 a section is
  blocked when its warmest forecast hour is at or below the comfort point, and
  learning never takes it below that. It is replay output, not stored:
  the runtime adds it to the pushed attributes, and `/api/almanac/<room>`
  serves it; `store.current_almanac` has no `margin`. Control doesn't read it
  from here: the container decides each section's block and publishes it in
  the conditions entity (below).
- **status** — `provisional` until the section's first setpoint-moving
  reaction (a person's vote, or a setpoint change or knob turn recorded before
  the votes), then `learned`.
- **valid_from** — the local date of the replay that built this row. There is
  no validity delay: the replay runs right after a reaction, 65 minutes later
  for the comfort window, on a heartbeat that can seed something new, and
  nightly at 00:15, and each run's result is in force at once. The store's
  `current_almanac(room, as_of)` returns, per section, the newest row whose
  `valid_from <= as_of`. A new setpoint still only reaches the unit from the
  section's next run: until phase 3b the hold stops maintenance after a
  reaction, and from then the scene's per-unit base helper does.
- **confidence** — `1 − 0.5^(quiet/3)`, quiet being the completed runs since
  the section's last reaction that had a heartbeat and no reaction. For the UI,
  never for control.
- **sections** — a section appears once anything in it is seeded. A section
  with no heartbeats yet has no entry.
- **units** — only seeded units appear: a unit needs a heartbeat in that
  section with it on and a setpoint. A missing unit means "leave it alone".
- **units[].setpoint** — the learned setpoint, stored precise.
- **units[].setpoint_applied** — the whole-degree value the templates drive,
  so templates never round; they only clamp it to the unit's
  `[min_temp, max_temp]` (docs/HARDWARE.md) and add the unit's section offset.
  The replay tracks it reaction by reaction (docs/TRUST_MODEL.md): normally
  the step nearest `setpoint`, an exact half toward the latest reaction's
  direction (half up before any); a reaction whose chosen value differs from
  the value in force moves it at least one step toward that choice and never
  past it, and one that chose the value in force keeps it. Nothing else moves
  it, so it can differ from the step nearest `setpoint` (S 23.25 can apply 24
  after a correction to 24). Absent or `null` means no seed - leave the unit
  as it is.
- **units[].off** — an explicit forced-off for this section. Scene "off" comes
  from config and is baked into the automation directly; this flag mirrors it so
  the two agree once an almanac exists. The replay always writes `false`.
- **sensors** — only seeded sensors appear (a reading in that section).
- **sensors[].comfort** — the reading this sensor shows while you are content.
  Not shared between sensors and not equal to the setpoint.
- **sensors[].band** — comfort half-width in degrees, nudged by each reaction
  (docs/TRUST_MODEL.md). The HA template votes when
  `abs(reading - comfort) >= band`. Tight = trusted.
- **sensors[].trust** — normalised inverse of band, 0..1. Display and
  tie-breaking only.

## What the HA templates do with it (D4; v2 phase 3)

`app/generator.py` renders `app/control.py` as Jinja (docs/V2_DESIGN.md,
Control). The maintenance automation runs on a `time_pattern` trigger (every
`heartbeat_interval_minutes`, HA's own clock per D4) and reads the active
section: each sensor's position is `(reading - comfort) / band` (a missing
band counts as 5.0; a sensor that is unavailable, stale (last reported longer
ago than `system.sensor_stale_minutes`) or has no `comfort` counts toward
neither the tally nor the quorum base), it votes past either trigger
edge (the conditions entity's leaned `cold_edge` and `warm_edge`, below), the pooled quorum is 1→1, 2→2,
3+→ceil(n/2), and the trust-weighted majority of the voters picks too warm or
too cold. Each unit then takes one step of the state machine from its helpers.
`setpoint_applied` is what the scene copies into each unit's base at the
section change, and the fallback while the base is unset; templates never
round it, only clamp the command to the unit's range (docs/HARDWARE.md). A unit
with no `setpoint_applied` and no base is left alone. The rules are specified
in `docs/TRUST_MODEL.md` and `docs/V2_DESIGN.md`, and the generated Jinja is
replayed against `app/control.py` in `tools/control_logic_check.py`.

## The conditions entity (phase 5)

`sensor.ac_conditions_<room>` carries what the container decided for the
section in force: how far the vote edges lean with the outdoor temperature,
and the cooling block. It is pushed like the almanac (at each crossover, every
5 minutes, on every websocket reconnect and on `homeassistant_start`), and is
not a helper deploy creates. Decided once per section and locked for it
(docs/V2_DESIGN.md, Outdoor temperature).

```json
{
  "state": "night",
  "attributes": {
    "room_id": "main_room",
    "section": "night",
    "cold_edge": -0.4,
    "warm_edge": 1.69,
    "blocked": false,
    "would_block": true,
    "block_mode": "shadow",
    "margin": 6.0,
    "comfort_point": 23.9,
    "outdoor_mean": 17.0,
    "outdoor_max": 17.0,
    "delta_mean": 6.9,
    "delta_max": 6.9,
    "lean": 0.69,
    "fail_open": null,
    "edges": {
      "living1": { "cold": 21.4, "warm": 31.85 }
    },
    "computed_at": "2026-08-03T20:30:07+08:00"
  }
}
```

- **state** — the section these conditions are for. The templates use the
  entity only while its state is the section in force (the scene's own; the
  scene select's elsewhere); missing, `unknown`, `unavailable` or another
  section's reads as `cold_edge` −1, `warm_edge` +1, not blocked.
- **cold_edge / warm_edge** — the trigger edges in band widths that every
  sensor votes against: −1 + lean (capped at ±0.6) and +1 + lean (capped at
  −0.6 on the hot side only), to 3 decimals. −1 / +1 when failing open.
- **blocked** — the block as control should see it: the decision, in
  `active` mode only. The templates add the room's heat sources live (any
  `on` blocks in `shadow` and `active`, by this `block_mode`, or the config's
  when there is no entity).
- **would_block** — the decision itself, shadow's included: Δ of the max ≥
  margin, or, after a blocked section, Δ of the max > margin − 1.
- **block_mode** — `off`, `shadow` or `active`, as in force when the section
  was decided.
- **margin** — the margin the decision used (the almanac's `margin` is the
  current one, which a reaction during the section can move).
- **comfort_point** — the trust-weighted mean of the usable sensors' comfort
  readings (plain mean while no sensor has trust), to 2 decimals.
- **outdoor_mean / outdoor_max** — the forecast over the section's window;
  **delta_mean / delta_max** — comfort point minus each (positive: colder
  outside); **lean** — `outdoor.lean_per_degree` × delta_mean, before the
  caps. All null when failing open for want of them.
- **fail_open** — null, or why there is no lean and no block:
  `no_outdoor` (no forecast, `outdoor.source` none, or the placeholder
  published while the forecast is fetched) or `no_comfort_point`.
- **edges** — each sensor's trigger edges in degrees, `comfort + edge ×
  band`, against the section's almanac at the time of the push. For people
  and the UI; the templates use the band-width edges.
- **computed_at** — when the container decided these conditions, or re-read
  them after a restart inside the section.

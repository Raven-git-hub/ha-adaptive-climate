# Almanac format

The almanac is the learned model the system acts on. One JSON document per
room, regenerated nightly by the analyser (app/analyser.py), persisted by the
store (app/store.py), pushed to Home Assistant as the state attributes of
`sensor.ac_almanac_<room>`, and read back by the UI. This one shape is a
contract between all four, so it is written down here.

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
      "state": "learning",
      "valid_from": "2026-08-03",
      "sample_days": 8,
      "confidence": 1.0,
      "units": {
        "main_room_ac": { "setpoint": 21.46, "off": false }
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

- **state** — `bootstrap` (< `bootstrap_min_days`, published immediately) or
  `learning` (>= that, published with the validity delay). `provisional` is a
  runtime seed, not an analyser output, so it does not appear here.
- **valid_from** — the date this almanac comes into force. For `learning` it is
  `as_of + validity_delay_days`, so a fresh build does not swing behaviour the
  same night; for `bootstrap` it is the build date. The store's
  `current_almanac(room, as_of)` returns, per section, the newest row whose
  `valid_from <= as_of`.
- **units[].setpoint** — the learned setpoint, stored precise. The generator /
  runtime snaps it to the unit's `target_temp_step` and clamps to the unit's
  `[min_temp, max_temp]` before calling `climate.set_temperature`
  (docs/HARDWARE.md). `null` means no opinion yet - leave the unit as it is.
- **units[].off** — an explicit forced-off for this section. Scene "off" comes
  from config and is baked into the automation directly; this flag mirrors it so
  the two agree once an almanac exists.
- **sensors[].comfort** — the reading this sensor shows while you are content at
  the unit setpoint. Not shared between sensors and not equal to the setpoint.
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
Cooling or Warming by the trust-weighted breach direction, clamped/snapped per
`docs/HARDWARE.md`. A unit is corrected only if it is already on, has a learned
setpoint, is not forced off for the section, and (if leak-enabled) its leak
boolean is off. The one-hour cap uses `input_datetime.ac_correction_started_<room>`:
reset every heartbeat the quorum is not met, left untouched while it is, so
elapsed time approximates how long correction has been continuously warranted.
The `ceil(n/2)` and trust-weighted direction logic are specified in
`docs/TRUST_MODEL.md`, and the Jinja implementing them is cross-checked against
`app.trust.evaluate_quorum` (the tested reference) in
`tools/maintenance_logic_check.py` - 45/45 hand and fuzzed scenarios match.

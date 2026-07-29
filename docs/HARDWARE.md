# Hardware realities (from live Home Assistant state)

Recorded from the target HA instance (version 2026.7.4, timezone
`Asia/Hong_Kong`, unit °C) so the generator and analyser design against what
the devices actually do, not what we assumed. Re-check with
`tools/doctor.py` if the hardware changes.

## The three climate units (three rooms)

| Room | Config unit | entity_id | hvac_modes (relevant) | range | step |
|---|---|---|---|---|---|
| Main Room | main_room_ac | `climate.lounge_lounge` | cool, fan_only, dry, off | 16–30 | 1 |
| Main Bedroom | main_bedroom_ac | `climate.home_s_device_2_home_s_device_2` | cool, fan_only, dry, off | 16–30 | 1 |
| Baby Room | baby_room_ac | `climate.baby_room_ac` | cool, fan_only, dry, off | 18–30 | 1 |

All three expose `cool` / `fan_only` / `dry` / `off`, so the four-state machine
(Normal=cool, Cooling=cool, Warming=fan_only, Leak=dry) is drivable on every
unit.

## Fan modes are reported per current hvac_mode (important)

The full fan list (`quiet, low, medium, high, auto`, plus `strong` on the Main
Room unit) is exposed while a unit is in `cool`. A unit in `dry` reports
`fan_modes: null` - which is exactly what we first saw on
`climate.home_s_device_2...` because it happened to be sitting in DRY at read
time. It is **not** a hardware limitation; all three units have the same fan
control in `cool`.

Two consequences for the generator (Phase 5):

1. **Capture a unit's fan list while it is in `cool`.** The doctor should read
   fan_modes from a unit in cool, not whatever mode it happens to be in, or it
   will record a false "no fan" for any unit resting in dry/fan_only.

2. **Guard `set_fan_mode` anyway.** When the state machine drives a unit *into*
   DRY (Leak) or fan_only (Warming), fan control may be unavailable at that
   moment. A `set_fan_mode` call must tolerate failure/absence rather than abort
   the crossover. Map Normal -> `low`, Cooling -> `medium` on all three units;
   fall back to skipping fan if the current mode rejects it.

## Setpoints: store precise, apply snapped

`target_temp_step` is 1 (whole degrees) on all units. The almanac may hold a
fractional learned setpoint (e.g. 21.6); the service call rounds to the unit's
step before `climate.set_temperature`. The ±2°C maintenance nudge is unaffected,
and `reactive_min_delta = 0.5` still catches every real change (minimum possible
is 1°). Sensors report to 0.1°, so the comfort band keeps fine resolution
regardless of the coarse thermostat step - this is exactly why setpoint and
sensor comfort are separated (docs/TRUST_MODEL.md).

## Applied setpoints clamp per unit

Baby Room is 18–30, the other two 16–30. Cooling (almanac − 2) and any nudge
must clamp to the unit's own range.

## Timezone

HA reports `Asia/Hong_Kong`. Because climate is clock-only, the scheduler fires
on this local clock. The runtime should take `time_zone` from HA's
`/api/config` (as the doctor already reads) so container and HA cannot disagree;
set `TZ=Asia/Hong_Kong` in `.env` as well, for log timestamps.

## Sensor triage

Genuine room-comfort sensors, by room:

- **Main Room:** `sensor.living_room_temp` (24.9),
  `sensor.living_room_temperature_1_temperature` (24.4),
  `sensor.living_room_temperature_2_temperature` (25.3). Three sensors ~0.9°
  apart while presumably all comfortable - the trust-model premise in the flesh,
  and enough for a real quorum (n=3 → 2 votes). ("Main Room" and "Living Room"
  are the same space; cf. the junk `light_sensor_main_room` entity.)
- **Main Bedroom:** `sensor.main_bedroom_temperature_temperature` (22.6),
  `sensor.bedroom_temperature_2` (22.8).
- **Baby Room:** `sensor.baby_room_temperature_temperature` (24.2).

Unconfirmed - excluded pending a decision, because a mis-placed sensor poisons a
room's quorum:

- `sensor.view_plus_temperature` (24.9) and `sensor.switchbot_hub_temperature`
  (26.6) - location not yet confirmed; candidates for the Main Room.
- `sensor.bedroom_temperature` - currently `unavailable`; possibly a third Main
  Bedroom sensor. `sensor.brodie_s_room_temperature` (24.7) is a separate
  bedroom with no AC, so not in play.

Excluded as noise or non-comfort: the Ember mug target/current pairs (~54°),
`sensor.244091581428635_temperature` (dishwasher, 0),
`sensor.network_cabinet_sensor_temperature` (−10.2), the Marvin/Raven case
probes, `sensor.light_sensor_main_room_temperature` (0.0),
`sensor.bathroom_sensor_temperature` (no AC in the bathroom). The outdoor sensor
is context only, never a comfort vote. An `unavailable` sensor casts no vote -
the trust model already handles that.

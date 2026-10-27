# Adaptive Climate v2 — design and build plan

27 Oct 2026

## Why v2

v2 keeps the v1 architecture but replaces how the system learns and how it drives the units. v1 changed the AC units correctly but never visibly learned: every correction snapped back at the next section change.

Four causes stacked:

1. **It learned from its own output.** Heartbeats recorded the setpoint the system had imposed, and the analyser treated it as "content".
2. **Corrections were drowned out.** A six-hour section gives about 36 heartbeats a day; over 21 days with a 7-day half-life that is roughly 300 heartbeat-weights. A reaction weighs about 4.5, so a 1° correction moved the target by about 0.015°.
3. **Rounding erased what was left.** Units take whole degrees, so a learned 21.02 was applied as 21. `tools/analyser_demo.py` asserted exactly this outcome as a pass.
4. **Turning a unit off taught nothing.** Off heartbeats were excluded and on/off was not a reaction, yet off was the household's main "too cold" gesture.

Control had its own problems. The "Warming" state (fan_only plus a setpoint offset) kept blowing air into a room that was already too cold, and nothing stopped the AC cooling against a heater in winter.

v2 changes four things:

- **Learning:** human reactions move targets; quiet periods only add confidence.
- **Control:** units are only ever cooling or off.
- **Outdoor temperature:** it leans the comfort bands and can block cooling outright.
- **Household input:** a physical knob, so learning isn't limited to one person.

## What carries over

The v1 architecture stays; v2 changes what flows through it. These carry over, with the changes noted:

- **Config is the single source of truth.** Everything downstream is a pure function of it; learned data lives in the almanac store.
- **Home Assistant owns behaviour.** The maintenance loop runs as generated HA templates, so control survives container downtime.
- **CSV is the archive, SQLite the query layer.** The almanac must stay rebuildable from CSV alone.
- **We own only the `ac_` namespace**, named from stable room and unit ids.
- **Two booleans, two meanings:** `ac_active_<room>` (mid-change, ignore), raised by scenes and now also by maintenance around every command; and `ac_hold_<room>`, now only a manual pause switch a person can flip. The system never raises or clears the hold; while it is on, scenes and maintenance send no commands.
- **Reactions ignore our own changes**, via `context.parent_id`, the guard boolean, and a check that the change isn't what the unit's state helper already implies (see [Where it runs](#where-it-runs)).
- **The event log is a feature.** Every decision is recorded with its reason.
- **Unchanged modules:** clock-only scheduler, HA client, doctor, and the offline test harnesses. Deploy reconcile and the leak latch keep their design but change in phase 3b: new helper domains and starting values, and leak automations that write the state helper.

Two v1 behaviours do not carry over:

- **Release.** When a correction's quorum lapsed, v1 left the unit in its corrected state (medium fan at −2°, or fan_only) until the next section change. v2 adds an explicit release step.
- **Hold on reaction.** v1 raised `ac_hold` on every reaction, which stopped maintenance for the rest of the section. v2 keeps controlling the unit after a person acts, starting from what they chose (see [Person reactions](#person-reactions)).

## Learning model

Targets move only when a person acts. A section with no reaction adds confidence and moves nothing, so the system can never learn from its own output.

### What counts as a reaction

A change is a person's when it carries no `context.parent_id`, `ac_active` is off, and the unit isn't now showing the command its state helper implies, which would be our own command arriving after the guard came down. For `rest`, `user_off`, `config_off` and `leak` that command is off: the change is ours if the unit is off and its setpoint didn't change, so a setpoint changed while off is still a person's. For `cool` and `boost` it is ours if the unit shows the command's mode and its setpoint to within `reactive_min_delta` (any setpoint, if the command sets none). The fan is never compared, so a person's fan-only change while the unit sits exactly at our command isn't seen. In `manual` every change is a person's. The container's reaction detector and HA's manual-change automation apply the same test, `app.control.matches_command`.

A unit dropping to `unavailable` or `unknown`, or coming back, is never a reaction: nobody can say who changed it. If it comes back in a different power, mode, setpoint or fan from the one it dropped out in, the runtime logs a `control` event saying what changed.

| Event | Detected by | Teaches |
| --- | --- | --- |
| Setpoint change (remote, app, HA) | `temperature` attribute change by a person | Setpoint moves toward the chosen value; band and trust learn |
| Unit turned on by a person | State goes from off to any non-off mode | "Too warm" (cooler wanted): band and trust learn; if cooling was blocked, the margin learns |
| Unit turned off by a person | State goes to off | "Too cold or unwanted" (warmer wanted): band and trust learn; if we started cooling under 30 min ago, the margin learns |
| Mode or fan change by a person | `hvac_mode` or `fan_mode` changes while the unit stays on | Nothing: logged, not learned. The unit goes to `manual` |
| Knob turned warmer or cooler | ESPHome event | Same as a setpoint change of one step per detent |
| Knob pressed ("this is right") | ESPHome event | Comfort readings move toward current readings; confidence rises |

### Grouping and kind

Every reaction kind, the knob included, is grouped by the existing 120-second extending window (`system.reactive_window_seconds`): each further change extends the window, and the whole window is recorded as one reaction. Per unit, the record keeps the **first** `setpoint_before` in the window and the last `setpoint_after`. (v1 overwrites `setpoint_before` on every change; phase 0 fixes this.)

The reaction kind is stored per unit: `setpoint`, `on`, `off`, `manual`, `knob_turn` or `knob_press` (a knob long press records as `off`). A unit's kind in a window is `on` or `off` if its power changed between the first and last state (off → any mode is on), else `setpoint` if its setpoint was changed at all in the window, else `manual` (a mode or fan change while on). Its `setpoint_before/after` and `power_before/after` keep the first and last values. The room-level kind is derived from the units' kinds when the reaction is recorded, and stored with it: `off` if any unit went off, else `on`, else `setpoint`, else `manual`. A unit turned on and given a new setpoint in the same window is one reaction of kind `on`; setpoint learning still applies to it, because its `setpoint_after` changed.

A **setpoint-moving reaction** is a setpoint change or a knob turn, or a reaction of any kind in which a unit's setpoint changed (an `on` that comes with a new setpoint, say). It moves S and the applied setpoint, makes the section learned and opens a comfort window.

### Setpoint

Every reaction whose `setpoint_after` changed moves the section's learned setpoint halfway toward what the person chose. The learning rate is `learning.reaction_rate`, default 0.5.

```
S_new = S_before + rate × (S_chosen − S_before)
```

S_chosen is the person's raw value, whatever offset or boost was active at the time. Offsets never feed learning.

The stored value stays fractional. The applied value, `setpoint_applied`, is on whole degrees and is tracked through the replay, reaction by reaction:

- **Normally it is the whole step nearest S.** An exact half goes toward the latest reaction's direction, the sign of S_chosen − S_before. A seed alone has no direction yet and rounds half up.
- **A correction always shows.** After a setpoint-moving reaction whose chosen value differs from the `setpoint_applied` in force at that moment, the new value moves at least one step from that previous value toward the chosen value, and never past it. A correction from 22 to 23 stores 22.5 and applies 23, so a 1° correction shows the next day. Plain rounding would send 22.5 back to 22, repeating the v1 bug.
- **A chosen value equal to the applied value keeps it.**
- **Nothing else moves it.** Quiet days, and reactions that change no setpoint (on/off or manual alone, or 22 → 23 → 22 in one window), leave it where it is.

This replaced rounding toward the direction of the latest reaction, which over-applied: Main Room sleep had S 24.17 and applied 25. In the worked example in `tools/analyser_demo.py` (23 in force when the person chose 25), 24.17 now applies 24: the nearest step, and still one step up. Had 24 been in force, the correction would still have to show, and 25 would apply. If S is 23 and the unit is boosting at 21 when a person sets 22, the gesture is warmer, but 22 is below the applied 23, so 22 is applied.

### Per-sensor comfort readings

A comfort reading moves only from evidence that a person accepted the room:

- **After a setpoint-moving reaction:** the mean of the sensor's heartbeat readings from 30 to 60 minutes after the reaction. The comfort reading moves toward it at `reaction_rate`.
- **A knob press:** the current reading, applied immediately at the same rate.

The 30-to-60-minute window belongs to the reaction's section, even if a section boundary falls inside it. It is disqualified by:

- any reaction of any kind in the window,
- the unit being in `boost`, `rest` or `leak` at any heartbeat in the window, or
- fewer than 2 heartbeats in the window.

The update is applied at the first replay after the window closes; the runtime schedules a replay 65 minutes after each setpoint-moving reaction for this.

Quiet heartbeats no longer feed comfort readings. This keeps heater evenings and the system's own corrections out of the model.

### Band and trust

A sensor's band is the recency-weighted mean of the deviations at which a person reacted, counting only sensors that had drifted by at least the high-trust deviation, clamped to ±0.5° to ±5°. Trust follows from the band. As in v1, except:

- **Deviation** is measured against the sensor's comfort reading as it stood in the replay at the moment of the reaction.
- **Weighting** uses the `recency_half_life_days` half-life over all history. There is no rolling window.
- **Direction matching** applies to every reaction kind. A reaction that wants it warmer (setpoint raised, meaning `setpoint_after` above `setpoint_before`; unit turned off; knob warmer) teaches only sensors that were below their comfort reading. One that wants it cooler (setpoint lowered, unit turned on, knob cooler) teaches only sensors above it. The band itself stays symmetric.

### Confidence

```
confidence = 1 − 0.5^(quiet / 3)
```

*quiet* counts the section's completed runs since its last reaction that had at least one heartbeat and no reaction of any kind. Any reaction resets it to 0. Confidence drives the UI and the knob's feedback, not control.

### Rebuilding

The almanac is a deterministic replay of every reaction, in time order, from a seed, so it rebuilds from CSV alone. There is no rolling window; older reactions fade because each new one moves the value halfway.

**Seed:**

- **Per unit:** the earliest heartbeat in that room and section with the unit on and a non-null setpoint.
- **Per sensor:** its earliest non-null reading in that section.
- **No seed:** the unit has no setpoint to apply, so it is left alone. The scene and maintenance send it no commands.

The runtime's provisional seeding at crossover is retired. Instead, the runtime replays every room at startup, before any crossover can fire, and again when the first heartbeat lands in a section with no almanac.

**The replay runs:**

- at startup, for every room, and pushed to HA before any crossover (catch-up included), so a crossover just after a start or a migration finds `setpoint_applied`;
- after every reaction, so the UI updates immediately;
- 65 minutes after each setpoint-moving reaction, to apply its comfort window (on startup the runtime re-arms this for the setpoint-moving reactions of the last 65 minutes, so a restart doesn't hold the comfort update until 00:15);
- on the first heartbeat in a section with no almanac, or on any heartbeat that can seed a unit or sensor the almanac doesn't have yet (a unit that was off at the section's first heartbeat);
- nightly at 00:15, as a consistency check.

The bootstrap state and validity delay are removed. A section is **provisional** until its first setpoint-moving reaction, then **learned**.

A new target applies from the section's next run. At each section change the scene copies `setpoint_applied` into each unit's base helper, and almanac pushes never change the base mid-section (see [Control](#control-cool-or-rest)).

## Control: cool or rest

The system only ever cools a unit or turns it off, apart from leak protection. When a person picks another mode or fan speed, it leaves the unit alone until the section changes. Triggers fire at a band edge and release at the comfort reading, and that gap is what stops it flapping.

### States and transitions

| State | Unit does |
| --- | --- |
| `cool` | cool, fan low, base + offset |
| `boost` | cool, fan medium, base + offset − boost step |
| `rest` | off, waiting |
| `user_off` | off, because a person turned it off; hands off until the section changes |
| `manual` | the mode and fan a person chose; hands off until the section changes |
| `config_off` | off, because the section's scene config forces it off; maintenance never touches it |
| `leak` | off, latched (no setpoint, no fan: dry would still run the compressor) |

**Base** is the unit's `input_number.ac_base_<room>_<unit>`. At each section change the scene sets it to the almanac's `setpoint_applied`. A person's setpoint change or turn-on resets it (see [Person reactions](#person-reactions)). Almanac pushes never change it mid-section, so a new S takes effect from the section's next run. A base of 0 means unset, and templates fall back to `setpoint_applied`. Every command is base + offset, minus the boost step in `boost`. Offsets build up within a section, reset at the next section change, and never feed learning.

System transitions:

- `cool` → `boost` when the quorum is met and the direction is too warm.
- `boost` → `cool` when back at comfort or after the boost cap; offset −1.
- `cool` → `rest` when the quorum is met and the direction is too cold, or when cooling is blocked and the unit isn't unblocked.
- `rest` → `cool` when back at comfort; offset +1. A rest doesn't wake while blocked.
- Any state → `leak` when the leak sensor is wet. Leak beats everything, including a person turning the unit off. When the leak is fixed and confirmed, the unit is evaluated as at a section change.
- **Section change:** offsets reset to 0, base = `setpoint_applied`, `since` = now, and `ac_unblocked` and `ac_grace_until` are cleared. Each unit starts in `config_off` if the section's scene forces it off, otherwise in `rest` if cooling is blocked, otherwise in `cool`. `user_off` and `manual` end here; a unit in `leak` stays there, and off is sent again. A section change overrides minimum times.

### Person reactions

The manual-change automation applies these per unit, straight away, so they hold even while the container is down. The container records the same change as a reaction for learning.

- **Setpoint change, unit on:** base = the chosen value, offset 0, state `cool`, `since` = now. The grace period starts.
- **Setpoint change, unit off or in `manual`:** the state stays as it is (`rest` stays `rest`, `user_off` stays `user_off`, `manual` stays `manual`, and so on), base = the chosen value and offset 0. No grace period starts and `since` is kept, so a rest still keeps its minimum time. The new base applies the next time the unit cools.
- **Turned off:** state `user_off`.
- **Turned on:** state `cool`, base = the unit's current setpoint, offset 0, `since` = now. The grace period starts. `input_boolean.ac_unblocked_<room>_<unit>` turns on, so the cooling block is ignored for that unit for the rest of the section.
- **Mode or fan change while on:** state `manual`. Logged, not learned.
- **While a leak is latched,** none of these change anything: it stays `leak`.
- **The knob (phase 8):** a turn counts as a setpoint change; a press changes nothing here.

The **grace period** runs until `input_datetime.ac_grace_until_<room>_<unit>`, which a person's setpoint change or turn-on sets to now + `system.control.person_grace_minutes` (default 30). Until then the unit is held in `cool`: no transition into `boost` or `rest`, a cooling block included. A leak still wins, and a section change clears it.

### Rules

- **Quorum, exactly as in v1:** one pooled count. A sensor votes when it is past either of its leaned trigger edges, and votes on both sides count toward the same tally. 1 usable sensor needs 1 vote, 2 need 2, 3 or more need at least half. When the quorum is met, the trust-weighted majority of the voters' sides picks the direction, `too_warm` or `too_cold`; an exact tie is `none`, which means no action.
- **Release:** a boost or rest ends when the pooled count of sensors still on the triggering side of their comfort readings falls below the quorum, counted as at least 1. With no usable sensor, a rest wakes into `cool` rather than wait for ever.
- **Boost cap:** `system.control.boost_max_minutes` (default 60). The unit then returns to `cool` at −1; if the room is still too warm next heartbeat, another boost can start, so it steps down gradually.
- **Offsets and limits:** the offset moves in whole steps and is capped at ±3 on its own. The boost step, `system.control.boost_degrees` (default 2), goes on top, and the final command is clamped to the unit's own range (18 to 30° for the baby room, 16 to 30° for the others).
- **Minimum times:** counted in heartbeats, to protect the compressor: at least 1 in `cool` or `boost` (a boost that a block sends to `rest` included), at least 2 in `rest`. A minimum of n heartbeats is met when the time since `since` is at least (n − 0.5) × the heartbeat interval, which is 5 and 15 minutes at the default 10-minute interval. A section change and a leak override them.
- **No seed:** while a unit has neither a base nor a `setpoint_applied`, maintenance leaves it alone and a section change resets its fields without a command. A leak still turns it off, and a leak released before there is an almanac goes to `rest`: still off, and woken by maintenance once there is a setpoint to cool to.
- **Commands:** sent only on transitions and at section changes, never re-sent every heartbeat. Maintenance and the leak automations raise `ac_active` around every command, as scenes do.
- **Fan:** low in `cool`, medium in `boost`. Every command sets the mode first and the fan last, and the fan waits (up to 30 s, then carries on) until the unit reports the new mode, because a unit offers no fan modes in dry or off. A fan call the unit's mode still rejects is skipped, not fatal (as in v1).
- **Pause:** while `ac_hold_<room>` is on, scenes and maintenance send no commands. Maintenance doesn't run at all; scenes and a leak release still write the helpers. A leak trip ignores the pause. Only a person turns it on or off.

## Outdoor temperature

Outdoor temperature never sets a target. It leans each sensor's trigger edges and, when it is cold enough, blocks cooling for the section.

### Source and values

`outdoor.source` is `forecast`, `probe` or `none`.

- **Forecast:** a weather entity's hourly forecast (`weather.get_forecasts`), read at each section change. The section gets two values over its window: the **mean** (drives the lean) and the **max** (drives the block).
- **Probe:** a temperature sensor, smoothed as a 30-minute rolling mean. The same value serves as mean and max.

Every heartbeat and reaction records the outdoor value. Every section run records its mean, max, lean and block decision. That history is what later tuning replays against.

### Comparing outside with inside

The **room comfort point** is the trust-weighted mean of the sensors' comfort readings, or a plain mean until trust is earned. It is not the AC setpoint, which can sit several degrees from the room's air temperature.

Δ = room comfort point − outdoor value. A positive Δ means colder outside.

### The lean

Trigger edges are measured in band widths from each sensor's comfort reading; with no lean they sit at −1 and +1. The lean shifts them by k × Δ, where k = `outdoor.lean_per_degree` (default 0.1 band per °C):

- **Cold edge (rest):** −1 + k × Δ, capped at ±0.6.
- **Warm edge (cool):** +1 + k × Δ, capped at −0.6 on the hot side, uncapped on the cold side.

The caps keep each edge on its own side of the comfort reading, which is where boosts and rests release. That prevents rest/wake ping-pong.

| Outside vs comfort (Δ) | Cold edge | Warm edge | Effect |
| --- | --- | --- | --- |
| −6 °C (hot) | −1.6 | +0.4 | Cools early, rests late |
| 0 °C | −1.0 | +1.0 | No lean |
| +6 °C (cool) | −0.4 | +1.6 | Rests early, cools late |
| +12 °C (cold) | −0.4 | +2.2 | Usually blocked anyway |

k is fixed in v2. Learning it (the "outdoor trust") is deferred.

### The cooling block

Cooling is blocked when the section's warmest forecast hour is at least the **margin** M below the room comfort point (Δ of the max ≥ M). The block lifts only when Δ of the max ≤ M − 1. It is decided at each section change and locked for the section.

While blocked, the scene rests the units, "too warm" votes are ignored, and the amber arc shows a lock. Leak protection is unaffected, since a leak turns the unit off.

If a person turns a unit on during a block, that unit's `ac_unblocked` helper turns on, and the block is ignored for it until the section changes. The margin is per room: it starts at 6 °C, stays between 2 and 10 °C, and learns from reactions:

- **Turned on while blocked:** M moves halfway toward Δ + 0.5, so that day would not have been blocked.
- **Turned off within 30 minutes of the system starting cooling, unblocked:** M moves halfway toward that day's Δ.

### Shadow mode

`outdoor.block_mode` is `off`, `shadow` or `active`, defaulting to `shadow`. In shadow mode the block is computed and logged ("would have blocked Night: forecast max 17°, comfort 23.9°") but not applied. Margin learning still runs against the decisions it would have made. Switch to `active` after reviewing about two weeks of shadow decisions.

### Heat sources

`rooms[].heat_sources` optionally lists entities such as heater plugs. While any is on, cooling is blocked immediately, regardless of the forecast. This applies in `shadow` and `active` modes.

### No outdoor value

Fail open: no lean and no block. The status strip and the log show a warning. A mistaken block in a Hong Kong summer, especially in the baby room, is worse than a possible heater fight.

## Where it runs

The container decides once per section; Home Assistant acts at every heartbeat. Everything HA needs is published to state entities, so control continues if the container is down.

### Container

- **At each section change:** computes the room comfort point, Δ, the block decision and each sensor's trigger edges in degrees, then publishes them to a new `sensor.ac_conditions_<room>`. It is re-pushed on a timer and on `homeassistant_start`, like the almanac.
- **On every reaction:** records it with its per-unit kind and the outdoor value, replays the almanac and pushes it. After a setpoint-moving reaction, it schedules another replay 65 minutes later for the comfort window.
- **At startup:** replays every room and pushes its almanac before any crossover.
- **On the first heartbeat in a section with no almanac:** replays that room.
- **Nightly:** full replay as a consistency check.
- **Its own commands** (phase 8, for the knob): the container's REST calls carry no `context.parent_id`, so it raises `ac_active` around any command it sends itself.

### Home Assistant (generated)

- **Scene automation, per room and section:** raises `ac_active`, records the section, and sets each unit's base, offset, `since`, grace, `ac_unblocked` and state as at a section change, then sends the commands. A unit with no seed is left alone. While `ac_hold` is on, it sends no commands.
- **Maintenance automation, every heartbeat:** reads the almanac, the conditions entity and each unit's helpers. It counts the pooled quorum against the published edges and runs the state machine. On a transition it raises `ac_active`, writes the helpers, sends the commands and lowers `ac_active`.
- **Manual-change automation, per unit:** triggered by the unit's mode changing (a state trigger with `to: null`) or its `temperature` or `fan_mode` attribute changing, never by a `current_temperature` update; catches a person's change (no `context.parent_id`, `ac_active` off) and applies the [person reactions](#person-reactions) straight away, reading whether the unit is on after the change. It ignores our own command arriving late, by the test in [What counts as a reaction](#what-counts-as-a-reaction), and classifies the change's kind as the runtime does.
- **Leak automations, per unit:** set the state helper to `leak` when the sensor trips. On a confirmed release, they evaluate the unit as at a section change.
- **Heat sources:** the generator writes the room's heat-source entities into the maintenance template, which checks them directly. A heater switching on blocks cooling without waiting for the container.

### Helpers

| Helper | Scope | Purpose |
| --- | --- | --- |
| `input_select.ac_state_<room>_<unit>` | Unit, new | `cool`, `boost`, `rest`, `user_off`, `manual`, `config_off`, `leak`. Deploy sets a new one to `manual`: hands off until the room's next section change |
| `input_number.ac_base_<room>_<unit>` | Unit, new | The section's base setpoint: `setpoint_applied` at the section change, or what a person chose. 0 to 100 in steps of 0.5; created at 0, its minimum, which means unset |
| `input_number.ac_offset_<room>_<unit>` | Unit, new | Section-scoped offset, −`offset_cap` to +`offset_cap` in whole steps. Deploy sets a new one to 0 |
| `input_datetime.ac_since_<room>_<unit>` | Unit, new | When the current state began (boost cap, minimum times). Deploy sets a new one to now |
| `input_datetime.ac_grace_until_<room>_<unit>` | Unit, new | When a person's grace period ends: their setpoint change or turn-on + `person_grace_minutes`. Cleared at a section change (set to now); a past or unset value means no grace. Created at HA's default, today 00:00 |
| `input_boolean.ac_unblocked_<room>_<unit>` | Unit, new | A person turned the unit on during a block; the block is ignored for it until the section changes |
| `sensor.ac_conditions_<room>` | Room, new | Outdoor values, comfort point, block decision, per-sensor edges |
| `ac_active`, `ac_scene`, `ac_almanac` | Room, kept | As in v1 |
| `ac_hold_<room>` | Room, changed | A manual pause switch only; the system never sets or clears it |
| `ac_correction_started_<room>` | Room, removed | Replaced by the per-unit `since` helper; the first v2 deploy prunes it |

No helper has an `initial`: Home Assistant would reset it to that at every restart. Deploy sets the starting values of the helpers it has just created, and only those.

### Almanac contract additions

- Per unit: `setpoint` (precise) plus `setpoint_applied` (already on a whole degree, tracked through the replay as in [Setpoint](#setpoint)), so templates never round.
- Per section: `status` (`provisional` or `learned`) and `confidence`.
- Per room: the learned `margin`.

### One logic, checked twice

The state machine, including person reactions, leak precedence and minimum times, lives in a pure-Python module, `app/control.py` (phase 3a). The generator renders the same logic as Jinja (phase 3b). `tools/control_check.py` holds the scenario table, as plain values in vote, step, section-change and person-change steps; The check also removes each rule in turn and requires the scenarios that guard it to fail. `tools/control_logic_check.py` (phase 3b, replacing v1's `maintenance_logic_check`) runs the generated automations as Home Assistant would, replays every scenario through the maintenance, scene, manual-change and leak automations, and compares the helpers and commands with `app/control.py`.

## UI

The Now view becomes the donut sketch: a donut per room, a correction scale and a sensor table. The visual layer replaces numbers as the first thing anyone reads.

### Now: donut

- **Fixed arcs:** blue (rest), green (comfortable), amber (cool). The comfort reading sits at the top.
- **One marker per sensor,** numbered to match its table row. Positions are in band widths, scaled so the leaned trigger edges land on the fixed arc boundaries. A change in the forecast therefore moves every marker, which is the lean made visible.
- **Markers pin at the ends** of the scale with an overflow tick, since a trusted sensor can travel far past its band.
- **Centre fan:** stopped, slow or fast for off, low and medium. The bottom segment names the state: Cool · Low, Boost · Medium, Resting, Off (by you), Manual, Off (scheduled), Leak · Off.
- **Block:** the amber arc greys out with a lock. In shadow mode it gets a dashed lock reading "would block".

### Now: correction scale

- **Centre number:** the applied setpoint.
- **Chevrons:** the section offset, up to three either way.
- **Magenta marker:** outdoor value against the room comfort point, labelled with the forecast mean and max.

### Now: sensor table

- **Sorted by position in band,** warmest first, so the table reads in the same order as the donut. Raw readings aren't comparable across sensors.
- **Columns:** reading, name, band (shown as ±°) and a trust bar.
- **Zone bar** on the left, coloured blue, green or amber, which shows the quorum at a glance.

A **"Leak fixed"** button appears on the room card while a leak is latched. It replaces the trip to Developer Tools.

### Almanac and Log

- **Almanac:** per section and unit, the learned and applied setpoint, status and confidence, plus a reaction timeline ("went cooler 3× this week") and the margin's history.
- **Log:** new event types for each reaction kind, state changes (boost, rest, release), block decisions (including shadow), and outdoor-data failures.

### Usability fixes

- **Config hot reload:** a saved config reaches the running observer without a container restart.
- **One Apply button:** it saves, previews the changes, and deploys only when HA entities change. This replaces the unclear split between Save and Deploy.

## Household input: the knob

The knob says "warmer" or "cooler", never a number, so anyone in the house can teach the system without understanding setpoints or sensors.

| Gesture | Action now | Teaches |
| --- | --- | --- |
| Turn one detent warmer | Setpoint +1 (a resting unit stays off) | A warmer reaction |
| Turn one detent cooler | Setpoint −1; an off or resting unit starts cooling | A cooler reaction |
| Short press | Nothing changes | "This is right": comfort readings and confidence |
| Long press (2 s) | Unit off | A person-off reaction (`user_off`) |

Detents are grouped like every other reaction, by the 120-second extending window.

**Wiring:** ESPHome `rotary_encoder` and a button `binary_sensor` fire HA events carrying the action and the room. The container records the knob event as the reaction, then makes the change itself. Its REST calls carry no `context.parent_id`, so it raises `ac_active` around them, and the change isn't counted twice.

**Scope:** each knob is assigned to one room in config (`rooms[].knobs`) and drives every unit in it.

**Feedback:** if the knob has an LED, it shows blue for resting, green for cooling, amber for a boost, and a short flash to confirm input. The exact hardware is an open question.

## Migration

No new repo. Tag the current state `v1.0-poc`, build on a `v2` branch, and merge once the phase checks pass.

A fresh repo would throw away the parts that already work, all of which carry over: the HA client, scheduler, deploy reconcile, leak latch and test harnesses. The live install's `ac_*` entities are also reconciled in place by deploy; a separate project would either collide with them or need a manual teardown. And the existing archive holds real reactions worth seeding from.

The clean start comes from removing dead code instead:

- the Warming/fan_only path
- bootstrap and validity-delay logic
- the room-level correction timer
- the runtime's provisional seeding at crossover
- raising `ac_hold` on reactions
- rewritten `docs/TRUST_MODEL.md`, `docs/DESIGN.md` (D3, D4) and `README.md`

### Config v2

Phase 0 defines every v2 key in the schema up front, with defaults, so saving a config works from the start. The loader migrates v1 configs on load.

- `learning.reaction_rate`: 0.5
- `learning.recency_half_life_days`: 7, kept for band learning (v1's analyser used it, but the schema never listed it)
- `system.control`: `boost_degrees` 2, `boost_max_minutes` 60, `cool_min_heartbeats` 1, `rest_min_heartbeats` 2, `person_grace_minutes` 30, `offset_cap` 3
- `outdoor`: `source` none, `weather_entity` null, `probe_entity` null, `block_mode` shadow, `lean_per_degree` 0.1, `initial_margin` 6. `weather_entity` (a `weather.*` entity) must be set when `source` is `forecast`, and `probe_entity` (a `sensor.*` entity) when it is `probe`. Phase 4 consumes them.
- `rooms[].heat_sources` (entity ids) and `rooms[].knobs` (`{id, name?}`, a placeholder until the hardware is settled): empty

The loader moves `system.max_step_degrees` to `system.control.boost_degrees` and `system.correction_max_minutes` to `system.control.boost_max_minutes`. `bootstrap_min_days`, `validity_delay_days`, `analysis_window_days` and `reactive_weight` are dropped. A scene's per-unit `mode: off` is kept and maps to the `config_off` state.

### Storage

Phase 0 gives the database a real migration path:

- **Versioned migrations:** `PRAGMA user_version`, with numbered migrations run at startup. A fresh database runs the schema file and is stamped with the latest version; an older one is migrated to the identical shape.
- **Safety:** before migrating an existing database, the store copies it to `<name>.v1-backup-<timestamp>` next to it and logs the path as a `storage` event. Each migration runs in one transaction with foreign keys off and must pass `foreign_key_check` and `integrity_check` before it commits; otherwise it rolls back, leaving the database as it was, and the app stays up without a runtime rather than restart into the same failure.
- **Rebuilt tables:** SQLite can't change a column's allowed values in place, so tables whose lists change are rebuilt from the schema file's own CREATE text, rows copied. The almanac is derived, so it is dropped and recreated empty; `event` is rebuilt by copying its rows, with new `control`, `block`, `outdoor` and `storage` categories.
- **New columns:** heartbeat outdoor value; per-unit control state and offset; reaction kind (room and unit), per-unit power before and after, and outdoor value; section-run outdoor mean, max, comfort point, lean, and the block decision as `blocked` plus the `block_mode` in force (so shadow "would have blocked" is `blocked` = 1 in `shadow`).
- **Older rows:** treated as `kind = setpoint` with no outdoor value.
- **CSV:** new columns are appended at the end, so old files still ingest. `section_run` is now written to CSV as well.
- **Re-ingest:** rebuilding SQLite from CSV is a prerequisite for Gate A, not for phase 1. Section runs reach the CSV only from phase 0, so re-ingest has to apply migration 2's rule (close an open run at the room's next run's start) to whatever it rebuilds.
- **Migration 2 (phase 1, data only):** v1 closed a section run by its close date, so every sleep run and every run a restart interrupted stayed open. Migration 2 closes each open run except the room's latest at the next run's start; the runtime now closes the room's open run by being open, and dates a run by its planned start.

### Seeding from v1 data

Replay the archive's existing reactions through the v2 learner, starting from each section's seed (see [Rebuilding](#rebuilding)), and ignore quiet heartbeats. Finish `tools/compare_analyser.py` (currently a skeleton) to diff the v1 and v2 almanacs before cutover.

### Cutover

CSV re-ingest must work first. Then deploy v2 with the block in shadow mode. Deploy creates the new helpers, rewrites the automations and removes the old correction timer, all inside the `ac_` namespace. Rolling back means redeploying from the `v1.0-poc` tag.

## Build plan

Phases 0 to 5 are built and checked offline against the existing harnesses, then deployed together. The block only goes live after a shadow review.

| Phase | Scope | Check |
| --- | --- | --- |
| 0 · Foundations | Storage migrations (`user_version`, backup first, checked transaction, rollback; `almanac` and `event` rebuilt); `section_run` written to CSV; every v2 config key with defaults, and the v1 config migration; `setpoint_before` keeps the first value in a window | `store_check` migrates a v1 database keeping every row, finds it identical to a fresh one, and sees a forced failure leave it untouched; `config_check` migrates the v1 example to exactly the v2 example; a three-step setpoint change records its first `setpoint_before` |
| 1 · Learning flip | Analyser becomes a replay of reactions from per-unit and per-sensor seeds; `setpoint_applied` tracked through the replay (see [Setpoint](#setpoint)); comfort from accepted readings; confidence; the runtime's provisional seeding retired | `analyser_demo` applies a 1° correction the next day; `compare_analyser` diffs v1 and v2 almanacs |
| 2 · On/off as reactions | Runtime detects a person turning units on or off, or changing mode or fan; reaction kind stored per unit and room; a unit coming back from unavailable changed is logged | `runtime_check` records person on/off and still ignores the system's own changes; on plus a setpoint change in one window is one `on` reaction |
| 3a · Control core | `app/control.py`: votes, the state machine, section changes and person reactions as pure functions; `app.trust.evaluate_edges` | `control_check` runs the scenario table, and each rule's scenarios fail when it is removed |
| 3b · Cool-or-rest in HA, deploy and leak | Generator Jinja for maintenance, scenes and the manual-change automation, with the new unit helpers; maintenance raises the guard; the Warming/fan_only path is removed. `input_number` (and `input_boolean`) helper domains, created at 0; every `ac_since_*` set to now on creation; leak automations write the state helper; the runtime's reaction detector also ignores a change that matches what the unit's state helper implies (`rest` → off, and so on), as the manual-change automation does | `control_logic_check` replays every `control_check` scenario through the generated automations with zero differences; `deploy_reconcile_check` creates the new helpers with the right initial values; `leak_response_check` still passes |
| 4 · Outdoor input | Forecast or probe; outdoor value stored on heartbeats, reactions and section runs | The doctor lists weather entities with an hourly forecast; `store_check` sees the new columns |
| 5 · Lean and block, in shadow | Conditions entity with per-sensor edges; block decisions and margin learning logged, not applied | Published edges reproduce the lean table; shadow decisions appear in the log |
| **Gate A** | CSV re-ingest works; then cutover: deploy v2 to HA, block in shadow | SQLite rebuilt from CSV alone gives the same almanac |
| 6 · Now view | Donut, correction scale, sensor table and the Leak fixed button | Every state renders: Cool, Boost, Rest, Off, Manual, Off (scheduled), Leak, blocked and shadow |
| 7 · Usability | Config hot reload; one Apply button with a change preview | A saved config reaches the running observer without a restart |
| **Gate B** | Two-week shadow review: the block goes active | |
| 8 · Knob | ESPHome events, the four gestures and LED feedback, once the hardware is settled | One detent produces exactly one reaction and one setpoint change |

The shadow review clock starts at cutover, so phases 6 and 7 happen during it. Phases 0 to 3 already fix the snap-back and the cold-room behaviour, so they are worth deploying early if the outdoor work stalls.

## Deferred and open

### Deferred past v2

- **Learning the lean strength** (k, the "outdoor trust") from on/off reactions.
- **Dry mode as the rest state** on humid days, possibly driven by forecast dew point.
- **Fan and mode changes** as reactions.
- **Presence:** gating learning or acting on empty rooms.
- **Multi-unit rooms** in the donut. Every current room has one unit.
- **From the v1 roadmap:** HA add-on packaging, custom AC state profiles, custom sections, heater control.

### Open questions

- [ ] Which weather entity provides an hourly forecast? The doctor will list candidates.
- [ ] Knob hardware: detents, button, LED or ring, how many knobs, which rooms.
- [ ] Minimum run and rest times (1 and 2 heartbeats): confirm against how the units behave.
- [ ] Boost step and offset cap (defaults: 2° boost, offset capped at ±3): confirm in use.
- [ ] Shadow-mode review length before turning the block on (proposed two weeks).

## Decision log

From the review of this design against the v1 code, 27 Oct 2026:

1. **Hold:** the system no longer raises `ac_hold`, which is now only a manual pause. A per-unit base helper carries the section's setpoint, and person reactions reset it.
2. **Quorum:** v1's pooled count and trust-weighted direction, unchanged. Release uses the same pooled count.
3. **Guard:** maintenance raises `ac_active` around its commands, and changes that match the state helper are ignored. The container guards its own REST calls (phase 8).
4. **Deploy (phase 3b):** adds the `input_number` (and `input_boolean`) domains at initial 0 and sets every `ac_since_*` on creation. Leak automations write the state helper.
5. **Storage (phase 0):** `user_version` migrations, `almanac` and `event` rebuilt, and `section_run` written to CSV. CSV re-ingest is required for Gate A.
6. **Config (phase 0):** every v2 key is defined with defaults, and the loader migrates v1 configs.
7. **Forced-off sections:** kept as the `config_off` state; a leak still wins.
8. **Grouping:** the 120 s extending window applies to every reaction kind, the knob included, and `setpoint_before` keeps the first value (phase 0).
9. **Rounding:** toward the sign of S_chosen − S_before of the latest setpoint-moving reaction, using the raw chosen value. A seed alone rounds half up.
10. **Seed:** the earliest heartbeat with the unit on per unit, and the earliest reading per sensor. Provisional seeding is retired, and no seed means the unit is left alone.
11. **Comfort window:** 30–60 min after the reaction, in its section. Disqualified by any reaction, `boost`/`rest`/`leak`, or fewer than 2 heartbeats; applied by a replay at +65 min.
12. **Band:** deviation is measured against the comfort reading as replayed at the time, with half-life weighting over all history. Every kind is direction-matched, and the band stays symmetric.
13. **Confidence:** 1 − 0.5^(quiet / 3). Only setpoint-moving reactions make a section learned.
14. **On/off:** off → any non-off mode counts as on. Kind is stored per unit, and on plus a setpoint change in one window is one `on` reaction that still teaches the setpoint.
15. **State machine:** leak beats everything; commands are sent only on transitions; minimum times are counted in heartbeats; the ±3 cap excludes the boost step. The boost config keys move under `system.control`.

From phase 0 planning, 27 Oct 2026:

16. **Migration safety:** back up the database before migrating; one transaction per migration, checked with `foreign_key_check` and `integrity_check`, rolled back on failure; the backup path is logged.
17. **No deploy before phase 1:** phase 0 empties the almanac, and only phase 1's replay-on-first-heartbeat refills it from history.
18. **Outdoor entities:** `outdoor.weather_entity` and `outdoor.probe_entity` are defined now, nullable, each required by its source; phase 4 consumes them.
19. **Knob config:** `{id, name?}` is the placeholder shape until the hardware is settled.
20. **Room-level reaction kind** is stored on the reaction as well as per unit.

From phase 1, 27 Oct 2026. Where the phase brief and this document differed, the document won:

21. **Setpoint-moving is by kind** (setpoint or knob turn; a NULL kind is a pre-v2 setpoint change), not by `setpoint_after` ≠ `setpoint_before`. They differ only for a window that ends where it began (22 → 23 → 22): it still makes the section learned and opens a comfort window, but leaves S and the rounding direction alone.
22. **Rounding is to whole degrees.** The unit's `target_temp_step` is a Home Assistant attribute, in neither config nor CSV, so a deterministic replay can't know it; every unit is step 1. `round_toward` takes the step as a parameter for later.
23. **Comfort moves at `reaction_rate`** toward the window mean, as this document says; that is halfway at the default.

Decisions this document hadn't covered:

24. **The seed is the replay's starting state** for every reaction, even one recorded before the seed heartbeat (harmless: such a seed was taken after the person set the unit).
25. **The comfort window is inclusive**, 30 to 60 minutes after the recorded reaction time, and applies only once it has closed, so a replay at reaction time never uses part of one. A sensor with no seed isn't updated by it.
26. **Band recency weighting is by day**, as in v1. A reaction's band direction is the sign of its summed `setpoint_after − setpoint_before` across units; zero teaches no band. On (cooler) and off (warmer) are handled now and stay inert until phase 2 records those kinds.
27. **Confidence counts closed runs** that started after the room-section's last reaction, matching heartbeats and reactions to a run by time.
28. **A heartbeat replays the room** when the section has no almanac or lacks a unit or sensor the heartbeat could seed, not only for the section's first heartbeat.
29. **`learning.reactive_weight` is dropped** from the v2 schema; `migrate()` strips it from v1 configs, and the UI no longer shows it. Nothing reads it in v2, and no v2 config was ever saved.
30. **Comfort replays are re-armed on startup** for setpoint-moving reactions in the last 65 minutes.
31. **Section runs are closed by being open, not by date**, and dated by their planned start; migration 2 repairs the runs v1 left open (own commit, before phase 1).
32. **The deploy warning in CLAUDE.md is reworded** now that phase 1 has landed: never deploy a build with phase 0 but not phase 1; otherwise deploy follows this build plan.

From phase 2 planning, 27 Oct 2026:

33. **Setpoint-moving also means "a setpoint changed"**, whatever the kind; this replaces the definition in 21. An `on` (or `off`, or `manual`) reaction in which a unit's setpoint changed moves S, sets the rounding direction, makes the section learned and opens a comfort window; without a setpoint change it does none of these. It closes the hole where turning a unit on at a new setpoint moved S but kept an old rounding direction, so the next day applied a degree off what the person chose.

From phase 2, 27 Oct 2026:

34. **A setpoint changed and back in one window (22 → 23 → 22) is kind `setpoint`**: a unit's kind is `setpoint` if its setpoint was changed at all in the window. So, as in 21, it makes the section learned and opens a comfort window, but leaves S and the rounding direction alone.
35. **Power is judged first state against last.** Off → on → off → on in one window is one `on`; on → off → on is `manual` (or `setpoint` if the setpoint changed too).
36. **A unit's kind goes on/off, then setpoint, then manual**, and the room's off, on, setpoint, manual. So off → dry is `on`, and a mode change while on that comes with a setpoint change is `setpoint`. The hold is still raised for every kind until phase 3b.
37. **Kind decides an `on`'s band direction**: an `on` that also changes the setpoint learns its band as cooler wanted, even if the new setpoint is higher. Its rounding direction still follows how S moved.
38. **Unavailable/unknown transitions are not reactions.** A unit that comes back in a different power, mode, setpoint or fan from the one it dropped out in is logged as a `control` warning event (it may have been reset by a power cut, or changed on its remote while HA couldn't see it), still not a reaction. Nothing is logged if it was already out when the runtime started.
39. **Margin-learning inputs are stored now**: room and unit kind, `power_before/after`, the reaction time and section. Control state and `since` (phase 3), the outdoor value (phase 4) and the block decision (phase 5) fill columns that already exist.

From phase 3a, 27 Oct 2026:

40. **Phase 3 is built in two parts.** 3a is `app/control.py` alone, with no generator, runtime or HA change; 3b renders it as Jinja and does the deploy and leak work. The build plan rows are split to match.
41. **Vote direction is named for the side the room is on:** `too_warm`, `too_cold` or `none`. `app.trust` names what the room needs (`cool` for too warm, `warm` for too cold), so `app/control.py` maps one to the other in a single table, and the 3b Jinja and the scenario table use the control names.
42. **A vote is a position in band widths**, (reading − comfort) / band, against a cold edge of −1 and a warm edge of +1, which phase 5 leans. At those edges it is v1's |reading − comfort| ≥ band except for float rounding exactly on the boundary; the 3b Jinja does the same division, so the two match. `app.trust.evaluate_edges` shares the pooled count and trust-weighted direction with `evaluate_quorum`, whose behaviour is unchanged.
43. **Votes are read from an almanac-shaped section entry**, as HA reads it. A sensor with no comfort reading can't vote, and a missing band counts as 5.0, the low-trust width, as in the v1 template.
44. **A leak comes before the no-seed rule.** A leak drives the unit to dry even with no base, leaving its temperature as it is, as v1's leak automation does: safety doesn't wait for an almanac.
45. **A leak release is a full section change:** resets, state and command. A section change with the leak latched sends dry again, as v1's crossover safety fix does. A leak released before there is an almanac goes to `cool` at the unit's current setpoint rather than stay in dry.
46. **A section change with no `setpoint_applied`** still resets the offset, base, `since`, grace and unblock, but keeps the state and sends nothing.
47. **The minimum time also gates boost → rest on a block.** Where the phase brief and this document differed, the document won: minimum times apply to `boost`. A section change and a leak still override them.
48. **Release counts against at least one.** With no usable sensor the quorum is 0, so a rest would never wake; the baby room resting all night on a dead sensor is the risk. With the floor it wakes into `cool`, which is the plain setpoint.
49. **Grace has its own helper**, `input_datetime.ac_grace_until_<room>_<unit>` (created in 3b), set to the person's change + `person_grace_minutes` and cleared at a section change, instead of being counted from `since`, which minimum times and the boost cap also use. It only ever holds a unit in `cool`, because only a setpoint change or turn-on with the unit on starts it, and both leave the unit in `cool`. It holds against a cooling block as well as votes; a leak still wins.
50. **A setpoint change on a unit that is off keeps its state**, replacing "state `cool`" in Person reactions: `rest` stays `rest`, `user_off` stays `user_off`, and so on. Base = the new setpoint and offset 0, with no grace and `since` kept, so the new base applies the next time the unit cools. The manual-change automation therefore passes the unit's power after the change. In `user_off` or `config_off` the stored base rarely matters, since a turn-on sets the base to the unit's current setpoint and a section change resets it.
51. **The knob:** a turn counts as a setpoint change and a press changes nothing in control, ready for phase 8.

From phase 3b planning, 27 Oct 2026:

52. **A setpoint change in `manual` keeps `manual`**, as decision 50 does for a unit that is off: base = the new setpoint, offset 0, no grace, `since` kept. A person who chose dry or fan_only and then changed the temperature keeps the mode they chose until the section changes; before this, the unit went to `cool` and the next transition would have overridden their mode. A knob turn is treated the same.

From phase 3b, 27 Oct 2026:

53. **"Is this ours" (the expected-state check):** a change is our own command arriving late when the unit shows what its state helper implies. For `rest`, `user_off` and `config_off`, the unit is off and its setpoint didn't change (an off command sets no setpoint, so decision 50 still works end to end). For `cool`, `boost` and `leak`, the unit shows the command's mode and its setpoint to within `reactive_min_delta`, any setpoint if the command sets none. The fan is never compared, so a person's fan-only change while the unit sits exactly at our command isn't seen. In `manual` every change is a person's. One rule, `app.control.matches_command`, for HA's manual-change automation and the runtime.
54. **No `initial` on any helper.** Home Assistant resets a helper to its `initial` at every restart, which would wipe a person's base, a `user_off` or an unblock. A new helper starts at HA's default (base at its minimum, 0; grace today 00:00, in the past; unblocked off), and deploy sets the rest for the helpers it has just created only: `since` to now (an epoch timestamp), offset to 0 and state to `manual`. Existing helpers are never reset, so a running boost keeps its clock. A helper whose settings change (the offset range after an `offset_cap` change) is updated in place.
55. **A new unit starts in `manual`**, hands off until its room's next section change, so deploying sends no command. Control begins at each room's next section change, so deploy shortly before one (docs/DEPLOY.md).
56. **Times are whole seconds:** HA's `input_datetime` keeps whole seconds, so templates floor now to the second and the scenario table uses whole seconds.
57. **One classifier for a single change,** `app.control.classify_change`, used by the manual-change automation and by the runtime's relevance test (unchanged semantics), and checked against the runtime's `UnitWindow.kind`. The manual-change automation mirrors `app.control.on_climate_change`.
58. **The hold:** scenes no longer clear it. While it is on, maintenance doesn't run, and scenes and leak releases write the helpers but send nothing; a leak trip ignores it. Deploy reports any hold that is on, since v1 raised it on every reaction and nothing clears it now.
59. **The guard:** maintenance computes every unit from one snapshot and raises `ac_active` once around all of a heartbeat's transitions; the leak trip and release raise it around their commands too.
60. **Leak automations mirror `step`'s leak rules:** a trip moves a unit not already in `leak` into it and sends dry; a release treats a unit in `leak` as at a section change. Maintenance does the same when the latch is raised or cleared by hand, so it carries each unit's forced-off sections as a literal list.
61. **Retired:** the Warming/fan_only path, the correction clock, the almanac's never-set unit `off` flag, and `fan_only` among the doctor's required modes. `tools/maintenance_logic_check.py` is replaced by `tools/control_logic_check.py`; its scene and correction setpoint-template cases and its skip-condition cases are retired, and the scenario table covers what they protected. The vote fuzz is kept, against `control.evaluate_votes`.
62. **Phase 5 hooks:** the `blocked`, `cold_edge` and `warm_edge` template variables, false and ±1 until phase 5, each set in one place.
63. **Runtime:** no hold on reactions, so `suspended_maint` is recorded as 0; each heartbeat records `control_state` and `control_offset` from the helpers (NULL while a room has none).

Before cutover, 27 Oct 2026:

64. **The manual-change automation has three triggers:** a state trigger with `to: null` (mode changes only) and attribute triggers on `temperature` and `fan_mode`, so `current_temperature` updates no longer fire it. A change that moves the mode and an attribute fires it twice; the second run sees the first's helper writes and either finds the change is now ours or writes the same record again. `control_logic_check` asserts the triggers, matches them as Home Assistant does, and runs the automation once per trigger that fires.
65. **`tools/doctor.py --v2-preflight`** tries, on a real Home Assistant, the behaviour the generated templates rely on: an `input_datetime` timestamp round trip, an `ac_base_*`-shaped `input_number` taking 0 and 23.5, and HA's own Jinja (via the template API) rendering the timestamp attribute, the `s.attributes.get` snapshot and `now()`. It uses throwaway `ac_preflight_*` helpers, created and deleted through deploy's helper API, removed even when a step fails.
66. **Cutover and rollback** follow `docs/V2_CUTOVER.md`. Rolling back, v1's own deploy removes the v2 `input_select`, `input_boolean` and `input_datetime` helpers, but not the `input_number` ones (a domain v1 never lists) nor the `ac_manual_*` automations (the restored v1 manifest predates them); the runbook deletes those by hand. Checked by running v1.0-poc's deploy code against a stub holding a v2 deployment.

Rounding, 27 Oct 2026 (supersedes 9, and the rounding-direction parts of 21, 33, 34 and 37):

67. **`setpoint_applied` is tracked through the replay.** It is normally the whole step nearest S, an exact half going toward the latest reaction's direction (half up for a seed alone). After a setpoint-moving reaction whose chosen value differs from the `setpoint_applied` in force at that moment, the new value moves at least one step from that value toward the chosen one, and never past it; a chosen value equal to it keeps it. Nothing else moves it, so quiet days never change it. Rounding toward the latest reaction's direction over-applied: Main Room sleep had S 24.17 and applied 25; with 23 in force when the person chose 25 it now applies 24 (with 24 in force it would still apply 25). An `on` that sets 21 → 23 after a correction down to 21 now applies 22 (S 22.25), where the direction rule applied 23. Decision 22 stands: steps are whole degrees, and `nearest_step` and `next_applied` take the step as a parameter. With whole-degree setpoints the chosen value is always on a step; a fractional one is never passed, even if that means no step is taken.

From the first live run, 27 Oct 2026:

68. **Startup replays every room before any crossover.** The almanac is empty after a migration, and the first heartbeat's replay came 10 minutes after startup: the 22:00 Sleep crossover, three minutes after a 21:57 start, found no `setpoint_applied` and left every unit in `manual`. `start()` now replays every room and pushes its almanac before the catch-up crossover and before the scheduler starts.
69. **The UI is served with `Cache-Control: no-cache`.** The Almanac tab's Status column showed "undefined": the browser was running a cached v1 `app.js`, which reads the almanac's `state`, against v2's API, which sends `status` (v2's `app.js` reads `status`). Served without a cache lifetime, the old script could stay in use for days; now every load revalidates (a 304 when unchanged). A browser still holding v1's script needs one hard refresh.
70. **The deploy check is a real dry run.** `GET /api/deploy/check` returned every list empty with `ok: true`, while the deploy straight after created 18 helpers, removed 3 and wrote 29 automations: the check only verified entities. It now runs the same reconciliation plan with writing switched off and reports what Deploy would create, reuse, update, remove and write (`dry_run: true`). `deploy_reconcile_check` runs it before each deploy and requires it to write nothing and to list exactly what that deploy then does.
71. **A leak turns the unit off, not dry** (supersedes the dry parts of 44, 45 and 60, and moves `leak` into the off group of 53). Dry still runs the compressor, and a unit with a leak keeps leaking. The leak command is hvac off with no setpoint and no fan, from the trip, from maintenance when the latch is raised by hand, and re-sent by a section change while latched; `leak` implies off for the "is this ours" check. A leak still beats everything, and a release is still a section change. A leak released before there is an almanac goes to `rest` with no command (the unit is already off, and `leak` can't be kept), where it stays off until there is a setpoint to cool to.
72. **The fan waits for the new mode.** Releasing the Main Bedroom's leak sent cool, 22 and fan low, but the unit still reported dry, which offers no fan modes, so the fan guard skipped `set_fan_mode` and the unit came up in cool on its remembered fan, high. Every command sequence (scene, maintenance, leak release, through the generator's one `_send_command`) now waits after `set_hvac_mode` until the unit reports the new mode (`wait_template`, 30 s timeout, carrying on if it never comes) before reading the fan guard and sending `set_fan_mode`. A command with no fan (off) doesn't wait.

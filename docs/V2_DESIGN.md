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
- **Unchanged modules:** clock-only scheduler, HA client, doctor, and the offline test harnesses. Deploy reconcile and the leak latch keep their design but change in phase 3b: new helper domains and initial values, and leak automations that write the state helper.

Two v1 behaviours do not carry over:

- **Release.** When a correction's quorum lapsed, v1 left the unit in its corrected state (medium fan at −2°, or fan_only) until the next section change. v2 adds an explicit release step.
- **Hold on reaction.** v1 raised `ac_hold` on every reaction, which stopped maintenance for the rest of the section. v2 keeps controlling the unit after a person acts, starting from what they chose (see [Person reactions](#person-reactions)).

## Learning model

Targets move only when a person acts. A section with no reaction adds confidence and moves nothing, so the system can never learn from its own output.

### What counts as a reaction

A change is a person's when it carries no `context.parent_id`, `ac_active` is off, and it isn't what the unit's state helper already implies (`rest`, `user_off` or `config_off` → off; `cool` or `boost` → cool; `leak` → dry). The container's reaction detector and HA's manual-change automation apply the same test.

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

The reaction kind is stored per unit: `setpoint`, `on`, `off`, `manual`, `knob_turn` or `knob_press` (a knob long press records as `off`). The room-level kind is derived from the units' kinds, not stored separately. A unit turned on and given a new setpoint in the same window is one reaction of kind `on`; setpoint learning still applies to it, because its `setpoint_after` changed.

A **setpoint-moving reaction** is a setpoint change or a knob turn.

### Setpoint

Every reaction whose `setpoint_after` changed moves the section's learned setpoint halfway toward what the person chose. The learning rate is `learning.reaction_rate`, default 0.5.

```
S_new = S_before + rate × (S_chosen − S_before)
```

S_chosen is the person's raw value, whatever offset or boost was active at the time. Offsets never feed learning.

The stored value stays fractional. The applied value, `setpoint_applied`, rounds to a whole degree **toward the direction of the latest setpoint-moving reaction**, which is the sign of S_chosen − S_before. A correction from 22 to 23 stores 22.5 and applies 23, so a 1° correction always shows the next day. Plain rounding would send 22.5 back to 22, repeating the v1 bug.

- **The direction is how S moved, not the gesture.** If S is 23 and the unit is boosting at 21 when a person sets 22, the gesture is warmer but S moves down to 22.5, so 22 is applied.
- **If S didn't move,** the previous direction stands. On/off and manual reactions never change it.
- **A seed alone** has no direction yet and rounds half up.

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

The runtime's provisional seeding at crossover is retired. Instead, when the first heartbeat lands in a section with no almanac, the runtime runs the replay for that room.

**The replay runs:**

- after every reaction, so the UI updates immediately;
- 65 minutes after each setpoint-moving reaction, to apply its comfort window;
- on the first heartbeat in a section with no almanac;
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
| `leak` | dry, latched |

**Base** is the unit's `input_number.ac_base_<room>_<unit>`. At each section change the scene sets it to the almanac's `setpoint_applied`. A person's setpoint change or turn-on resets it (see [Person reactions](#person-reactions)). Almanac pushes never change it mid-section, so a new S takes effect from the section's next run. A base of 0 means unset, and templates fall back to `setpoint_applied`. Every command is base + offset, minus the boost step in `boost`. Offsets build up within a section, reset at the next section change, and never feed learning.

System transitions:

- `cool` → `boost` when the quorum is met and the direction is too warm.
- `boost` → `cool` when back at comfort or after the boost cap; offset −1.
- `cool` → `rest` when the quorum is met and the direction is too cold, or when cooling is blocked and the unit isn't unblocked.
- `rest` → `cool` when back at comfort; offset +1. A rest doesn't wake while blocked.
- Any state → `leak` when the leak sensor is wet. Leak beats everything, including a person turning the unit off. When the leak is fixed and confirmed, the unit is evaluated as at a section change.
- **Section change:** offsets reset to 0, base = `setpoint_applied`, and `ac_unblocked` is cleared. Each unit starts in `config_off` if the section's scene forces it off, otherwise in `rest` if cooling is blocked, otherwise in `cool`. `user_off` and `manual` end here; a unit in `leak` stays there. A section change overrides minimum times.

### Person reactions

The manual-change automation applies these per unit, straight away, so they hold even while the container is down. The container records the same change as a reaction for learning.

- **Setpoint change:** base = the chosen value, offset 0, state `cool`, `since` = now. The grace period applies.
- **Turned off:** state `user_off`.
- **Turned on:** state `cool`, base = the unit's current setpoint, offset 0, `since` = now. The grace period applies. `input_boolean.ac_unblocked_<room>_<unit>` turns on, so the cooling block is ignored for that unit for the rest of the section.
- **Mode or fan change while on:** state `manual`. Logged, not learned.
- **While a leak is latched,** none of these change the state: it stays `leak`.

The **grace period**, `system.control.person_grace_minutes` (default 30) counted from `since`, blocks any transition into `boost` or `rest` until it ends.

### Rules

- **Quorum, exactly as in v1:** one pooled count. A sensor votes when it is past either of its leaned trigger edges, and votes on both sides count toward the same tally. 1 usable sensor needs 1 vote, 2 need 2, 3 or more need at least half. When the quorum is met, the trust-weighted majority of the voters' sides picks the direction; an exact tie means no action.
- **Release:** a boost or rest ends when the pooled count of sensors still on the triggering side of their comfort readings falls below the quorum.
- **Boost cap:** `system.control.boost_max_minutes` (default 60). The unit then returns to `cool` at −1; if the room is still too warm next heartbeat, another boost can start, so it steps down gradually.
- **Offsets and limits:** the offset moves in whole steps and is capped at ±3 on its own. The boost step, `system.control.boost_degrees` (default 2), goes on top, and the final command is clamped to the unit's own range (18 to 30° for the baby room, 16 to 30° for the others).
- **Minimum times:** counted in heartbeats, to protect the compressor: at least 1 in `cool` or `boost`, at least 2 in `rest`. A minimum of n heartbeats is met when the time since `since` is at least (n − 0.5) × the heartbeat interval, which is 5 and 15 minutes at the default 10-minute interval. A section change overrides them.
- **Commands:** sent only on transitions and at section changes, never re-sent every heartbeat. Maintenance raises `ac_active` around every command, as scenes do.
- **Fan:** low in `cool`, medium in `boost`. A fan call the unit's current mode rejects is skipped, not fatal (as in v1).
- **Pause:** while `ac_hold_<room>` is on, scenes and maintenance send no commands. Only a person turns it on or off.

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

While blocked, the scene rests the units, "too warm" votes are ignored, and the amber arc shows a lock. Leak protection is unaffected, since that uses dry mode, not cooling.

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
- **On the first heartbeat in a section with no almanac:** replays that room.
- **Nightly:** full replay as a consistency check.
- **Its own commands** (phase 8, for the knob): the container's REST calls carry no `context.parent_id`, so it raises `ac_active` around any command it sends itself.

### Home Assistant (generated)

- **Scene automation, per room and section:** raises `ac_active`, records the section, and sets each unit's base, offset, `since`, `ac_unblocked` and state as at a section change, then sends the commands. A unit with no seed is left alone. While `ac_hold` is on, it sends no commands.
- **Maintenance automation, every heartbeat:** reads the almanac, the conditions entity and each unit's helpers. It counts the pooled quorum against the published edges and runs the state machine. On a transition it raises `ac_active`, writes the helpers, sends the commands and lowers `ac_active`.
- **Manual-change automation, per unit:** catches a person's change (no `context.parent_id`, `ac_active` off) and applies the [person reactions](#person-reactions) straight away. It ignores a change that matches what the state helper already implies: `rest`, `user_off` or `config_off` → off; `cool` or `boost` → cool; `leak` → dry. That keeps a late echo of our own command from looking like a person.
- **Leak automations, per unit:** set the state helper to `leak` when the sensor trips. On a confirmed release, they evaluate the unit as at a section change.
- **Heat sources:** the generator writes the room's heat-source entities into the maintenance template, which checks them directly. A heater switching on blocks cooling without waiting for the container.

### Helpers

| Helper | Scope | Purpose |
| --- | --- | --- |
| `input_select.ac_state_<room>_<unit>` | Unit, new | `cool`, `boost`, `rest`, `user_off`, `manual`, `config_off`, `leak` |
| `input_number.ac_base_<room>_<unit>` | Unit, new | The section's base setpoint: `setpoint_applied` at the section change, or what a person chose. Created at 0, which means unset |
| `input_number.ac_offset_<room>_<unit>` | Unit, new | Section-scoped offset, −3 to +3 in whole steps. Created at 0 |
| `input_datetime.ac_since_<room>_<unit>` | Unit, new | When the current state began (boost cap, minimum times, person grace). Set to now on creation |
| `input_boolean.ac_unblocked_<room>_<unit>` | Unit, new | A person turned the unit on during a block; the block is ignored for it until the section changes |
| `sensor.ac_conditions_<room>` | Room, new | Outdoor values, comfort point, block decision, per-sensor edges |
| `ac_active`, `ac_scene`, `ac_almanac` | Room, kept | As in v1 |
| `ac_hold_<room>` | Room, changed | A manual pause switch only; the system never sets or clears it |
| `ac_correction_started_<room>` | Room, removed | Replaced by the per-unit `since` helper |

### Almanac contract additions

- Per unit: `setpoint` (precise) plus `setpoint_applied` (already rounded toward the latest setpoint-moving reaction), so templates never round.
- Per section: `status` (`provisional` or `learned`) and `confidence`.
- Per room: the learned `margin`.

### One logic, checked twice

The state machine, including person reactions, leak precedence and minimum times, lives in a new pure-Python module, `app/control.py`. The generator renders the same logic as Jinja, and a check harness runs both on the same inputs and diffs them, extending today's `tools/maintenance_logic_check.py` pattern.

## UI

The Now view becomes the donut sketch: a donut per room, a correction scale and a sensor table. The visual layer replaces numbers as the first thing anyone reads.

### Now: donut

- **Fixed arcs:** blue (rest), green (comfortable), amber (cool). The comfort reading sits at the top.
- **One marker per sensor,** numbered to match its table row. Positions are in band widths, scaled so the leaned trigger edges land on the fixed arc boundaries. A change in the forecast therefore moves every marker, which is the lean made visible.
- **Markers pin at the ends** of the scale with an overflow tick, since a trusted sensor can travel far past its band.
- **Centre fan:** stopped, slow or fast for off, low and medium. The bottom segment names the state: Cool · Low, Boost · Medium, Resting, Off (by you), Manual, Off (scheduled), Leak · Dry.
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
- `system.control`: `boost_degrees` 2, `boost_max_minutes` 60, `person_grace_minutes` 30
- `outdoor`: `source` none, `block_mode` shadow, `lean_per_degree` 0.1, `initial_margin` 6
- `rooms[].heat_sources` and `rooms[].knobs`: empty

The loader moves `system.max_step_degrees` to `system.control.boost_degrees` and `system.correction_max_minutes` to `system.control.boost_max_minutes`. `bootstrap_min_days`, `validity_delay_days` and `analysis_window_days` are dropped. A scene's per-unit `mode: off` is kept and maps to the `config_off` state.

### Storage

Phase 0 gives the database a real migration path:

- **Versioned migrations:** `PRAGMA user_version`, with numbered migrations run at startup.
- **Rebuilt tables:** SQLite can't change a column's allowed values in place, so tables whose lists change are rebuilt. The almanac is derived, so it is dropped and recreated; `event` is rebuilt by copying its rows.
- **New columns:** heartbeat outdoor value; per-unit control state and offset; per-unit reaction kind and outdoor value; section-run outdoor mean, max, comfort point, lean and block decision.
- **Older rows:** treated as `kind = setpoint` with no outdoor value.
- **CSV:** new columns are appended at the end, so old files still ingest. `section_run` is now written to CSV as well.
- **Re-ingest:** rebuilding SQLite from CSV is a prerequisite for Gate A, not for phase 1.

### Seeding from v1 data

Replay the archive's existing reactions through the v2 learner, starting from each section's seed (see [Rebuilding](#rebuilding)), and ignore quiet heartbeats. Finish `tools/compare_analyser.py` (currently a skeleton) to diff the v1 and v2 almanacs before cutover.

### Cutover

CSV re-ingest must work first. Then deploy v2 with the block in shadow mode. Deploy creates the new helpers, rewrites the automations and removes the old correction timer, all inside the `ac_` namespace. Rolling back means redeploying from the `v1.0-poc` tag.

## Build plan

Phases 0 to 5 are built and checked offline against the existing harnesses, then deployed together. The block only goes live after a shadow review.

| Phase | Scope | Check |
| --- | --- | --- |
| 0 · Foundations | Storage migrations (`user_version`; `almanac` and `event` rebuilt); `section_run` written to CSV; every v2 config key with defaults, and the v1 config migration; `setpoint_before` keeps the first value in a window | `store_check` migrates a v1 database and keeps its events; a v1 config loads migrated and a v2 config saves; a two-step setpoint change records its first `setpoint_before` |
| 1 · Learning flip | Analyser becomes a replay of reactions from per-unit and per-sensor seeds; rounding toward the latest setpoint-moving reaction; comfort from accepted readings; confidence; the runtime's provisional seeding retired | `analyser_demo` applies a 1° correction the next day; `compare_analyser` diffs v1 and v2 almanacs |
| 2 · On/off as reactions | Runtime detects a person turning units on or off, or changing mode or fan; reaction kind stored per unit | `runtime_check` records person on/off and still ignores the system's own changes; on plus a setpoint change in one window is one `on` reaction |
| 3a · Cool-or-rest control | `app/control.py`, generator Jinja, the new unit helpers and the manual-change automation; maintenance raises the guard; the Warming/fan_only path is removed | Jinja matches `app/control.py` for every state, edge and person reaction |
| 3b · Deploy and leak | `input_number` (and `input_boolean`) helper domains, created at 0; every `ac_since_*` set to now on creation; leak automations write the state helper | `deploy_reconcile_check` creates the new helpers with the right initial values; `leak_response_check` still passes |
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

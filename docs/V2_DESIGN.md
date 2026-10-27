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

The v1 architecture stays; v2 changes what flows through it. These hold unchanged:

- **Config is the single source of truth.** Everything downstream is a pure function of it; learned data lives in the almanac store.
- **Home Assistant owns behaviour.** The maintenance loop runs as generated HA templates, so control survives container downtime.
- **CSV is the archive, SQLite the query layer.** The almanac must stay rebuildable from CSV alone.
- **We own only the `ac_` namespace**, named from stable room and unit ids.
- **Two booleans, two meanings:** `ac_active_<room>` (mid-change, ignore) and `ac_hold_<room>` (a human intervened).
- **Reactions ignore our own changes**, via `context.parent_id` and the guard boolean.
- **The event log is a feature.** Every decision is recorded with its reason.
- **Unchanged modules:** clock-only scheduler, HA client, deploy reconcile, leak latch, doctor, and the offline test harnesses.

One v1 behaviour does not carry over. When a correction's quorum lapsed, v1 left the unit in its corrected state (medium fan at −2°, or fan_only) until the next section change. v2 adds an explicit release step.

## Learning model

Targets move only when a person acts. A section with no reaction adds confidence and moves nothing, so the system can never learn from its own output.

### What counts as a reaction

| Event | Detected by | Teaches |
| --- | --- | --- |
| Setpoint change (remote, app, HA) | `temperature` attribute change with no `context.parent_id`, guard off | Setpoint moves toward the chosen value; drifted sensors learn band and trust |
| Unit turned on by a person | State goes from off to cool, not caused by us | "Too warm": drifted sensors learn; if cooling was blocked, the margin learns |
| Unit turned off by a person | State goes to off, not caused by us | "Too cold or unwanted": drifted sensors learn; if we started cooling under 30 min ago, the margin learns |
| Knob turned warmer or cooler | ESPHome event | Same as a setpoint change of one step per detent |
| Knob pressed ("this is right") | ESPHome event | Comfort readings move toward current readings; confidence rises |

Fan and mode changes are logged as events but not learned in v2.

### Setpoint

Each reaction moves the section's learned setpoint halfway toward what the person chose. The learning rate is `learning.reaction_rate`, default 0.5.

```
S_new = S + rate × (S_chosen − S)
```

The stored value stays fractional; the applied value rounds **toward the direction of the most recent reaction**. A correction from 22 to 23 stores 22.5 and applies 23, so a 1° correction always shows the next day. Plain rounding would send 22.5 back to 22, repeating the v1 bug.

### Per-sensor comfort readings

A comfort reading moves only from evidence that a person accepted the room:

- **After a setpoint or knob correction:** the mean reading 30 to 60 minutes later, if nobody reacted again in that window. The comfort reading moves halfway toward it.
- **A knob press:** the current reading, applied immediately at the same rate.

Quiet heartbeats no longer feed comfort readings. This keeps heater evenings and the system's own corrections out of the model.

### Band and trust

Unchanged from v1. A sensor's band is the recency-weighted mean of the deviations at which a person reacted, counting only sensors that had drifted, clamped to ±0.5° to ±5°. Trust follows from the band. On/off reactions now contribute too, with their direction.

### Confidence

Each quiet section run since the last reaction raises the section's confidence. It drives the UI and the knob's feedback, not control.

### Rebuilding

The almanac is a deterministic replay of every reaction, in time order, from a seed: the section's earliest heartbeat (in effect v1's first-crossover sample), so the almanac rebuilds from CSV alone. There is no rolling window; older reactions fade because each new one moves the value halfway. The replay runs right after every reaction, so the UI updates immediately, and again nightly at 00:15 as a consistency check. The bootstrap state and validity delay are removed. A section is **provisional** until its first reaction, then **learned**.

The hold behaviour is unchanged: a reaction stands until the next section change, and the new target applies from the same section's next run.

## Control: cool or rest

A unit is only ever cooling or off, apart from leak protection. Triggers fire at a band edge and release at the comfort reading, and that gap is what stops it flapping.

### States and transitions

| State | Unit does |
| --- | --- |
| Cool | cool, fan low, S + offset |
| Boost | cool, fan medium, S + offset − 2 |
| Rest | off, waiting |
| Off (by a person) | off; the system won't restart it |
| Leak | dry, latched |

- Cool → Boost when a "too warm" quorum is met.
- Boost → Cool when back at comfort or after 60 min; offset −1.
- Cool → Rest when a "too cold" quorum is met, or when cooling is blocked.
- Rest → Cool when back at comfort; offset +1. A rest doesn't wake while blocked.
- Any state → Off (by a person) when a person turns the unit off. → Cool when a person turns it on, or at the next section change.
- Any state → Leak when the leak sensor is wet. → Cool when the leak is fixed and confirmed.
- Every section change resets offsets to 0 and starts in Cool, or Rest when blocked.

S is the section's applied setpoint from the almanac. Offsets build up within a section and reset at the next section change; they never feed learning.

### Rules

- **Quorum is unchanged:** 1 sensor needs 1 vote, 2 need 2, 3 or more need at least half. A sensor votes when it is past its leaned trigger edge.
- **Release:** a boost or rest ends when fewer than quorum sensors remain on the triggering side of their comfort readings.
- **Boost cap:** 60 minutes. The unit then returns to Cool at −1; if the room is still too warm next heartbeat, another boost can start, so it steps down gradually.
- **Offsets:** whole steps, capped at ±3, and always clamped to the unit's own range (18 to 30° for the baby room, 16 to 30° for the others).
- **Minimum times:** a unit stays in Cool or Boost for at least 10 minutes and in Rest for at least 20, to protect the compressor.
- **Fan:** low in Cool, medium in Boost. A fan call the unit's current mode rejects is skipped, not fatal (as in v1).
- **Off by a person:** it holds until the next section change, when the scene decides again. Turning it on (or the knob going cooler) returns it to Cool.
- **Mixed votes:** if warm and cold quorums are both met, the trust-weighted majority picks the direction.

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

If a person turns a unit on during a block, the system respects it until the next section change. The margin is per room: it starts at 6 °C, stays between 2 and 10 °C, and learns from reactions:

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
- **On every reaction:** records it with its kind and the outdoor value, replays the almanac and pushes it.
- **Nightly:** full replay as a consistency check.

### Home Assistant (generated)

- **Maintenance automation, every heartbeat:** reads the almanac, the conditions entity and each unit's state helpers. It counts quorum against the published edges, runs the state machine and writes the helpers.
- **Manual-change automation, per unit:** a person turning a unit off (no `context.parent_id`, guard off) sets that unit to `user_off` straight away, so maintenance never turns it back on.
- **Heat sources:** the generator writes the room's heat-source entities into the maintenance template, which checks them directly. A heater switching on blocks cooling without waiting for the container.

### Helpers

| Helper | Scope | Purpose |
| --- | --- | --- |
| `input_select.ac_state_<room>_<unit>` | Unit, new | `cool`, `boost`, `rest`, `user_off`, `leak` |
| `input_number.ac_offset_<room>_<unit>` | Unit, new | Section-scoped offset, −3 to +3 in whole steps |
| `input_datetime.ac_since_<room>_<unit>` | Unit, new | When the current state began (boost cap, minimum run and rest) |
| `sensor.ac_conditions_<room>` | Room, new | Outdoor values, comfort point, block decision, per-sensor edges |
| `ac_active`, `ac_hold`, `ac_scene`, `ac_almanac` | Room, kept | As in v1 |
| `ac_correction_started_<room>` | Room, removed | Replaced by the per-unit `since` helper |

### Almanac contract additions

- Per unit: `setpoint` (precise) plus `setpoint_applied` (already rounded toward the last reaction), so templates never round.
- Per section: `status` (`provisional` or `learned`) and `confidence`.
- Per room: the learned `margin`.

### One logic, checked twice

The state machine lives in a new pure-Python module, `app/control.py`. The generator renders the same logic as Jinja, and a check harness runs both on the same inputs and diffs them, extending today's `tools/maintenance_logic_check.py` pattern.

## UI

The Now view becomes the donut sketch: a donut per room, a correction scale and a sensor table. The visual layer replaces numbers as the first thing anyone reads.

### Now: donut

- **Fixed arcs:** blue (rest), green (comfortable), amber (cool). The comfort reading sits at the top.
- **One marker per sensor,** numbered to match its table row. Positions are in band widths, scaled so the leaned trigger edges land on the fixed arc boundaries. A change in the forecast therefore moves every marker, which is the lean made visible.
- **Markers pin at the ends** of the scale with an overflow tick, since a trusted sensor can travel far past its band.
- **Centre fan:** stopped, slow or fast for off, low and medium. The bottom segment names the state: Cool · Low, Boost · Medium, Resting, Off (by you), Leak · Dry.
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

Detents within 3 seconds are grouped into one reaction, matching how setpoint changes are consolidated today.

**Wiring:** ESPHome `rotary_encoder` and a button `binary_sensor` fire HA events carrying the action and the room. The container records the knob event as the reaction, then makes the change itself. That change carries our context, so it isn't counted twice.

**Scope:** each knob is assigned to one room in config (`rooms[].knobs`) and drives every unit in it.

**Feedback:** if the knob has an LED, it shows blue for resting, green for cooling, amber for a boost, and a short flash to confirm input. The exact hardware is an open question.

## Migration

No new repo. Tag the current state `v1.0-poc`, build on a `v2` branch, and merge once the phase checks pass.

A fresh repo would throw away the parts that already work, all of which carry over: the HA client, scheduler, deploy reconcile, leak latch and test harnesses. The live install's `ac_*` entities are also reconciled in place by deploy; a separate project would either collide with them or need a manual teardown. And the existing archive holds real reactions worth seeding from.

The clean start comes from removing dead code instead:

- the Warming/fan_only path
- bootstrap and validity-delay logic
- the room-level correction timer
- rewritten `docs/TRUST_MODEL.md`, `docs/DESIGN.md` (D3, D4) and `README.md`

### Config v2

The loader migrates v1 forward and adds defaults:

- `learning.reaction_rate`: 0.5
- `outdoor`: `source` none, `block_mode` shadow, `lean_per_degree` 0.1, `initial_margin` 6
- `rooms[].heat_sources` and `rooms[].knobs`: empty

`bootstrap_min_days`, `validity_delay_days` and `analysis_window_days` are dropped. `recency_half_life_days` stays for band learning.

### Storage

All changes are additive:

- **New columns:** heartbeat outdoor value; per-unit control state and offset; reaction kind and outdoor value; section-run outdoor mean, max, comfort point, lean and block decision.
- **Older rows:** treated as `kind = setpoint` with no outdoor value.
- **CSV:** new columns are appended at the end, so old files still ingest.

### Seeding from v1 data

Replay the archive's existing reactions through the v2 learner, starting from each section's earliest heartbeat, and ignore quiet heartbeats. Finish `tools/compare_analyser.py` (currently a skeleton) to diff the v1 and v2 almanacs before cutover.

### Cutover

Deploy v2 with the block in shadow mode. Deploy creates the new helpers, rewrites the automations and removes the old correction timer, all inside the `ac_` namespace. Rolling back means redeploying from the `v1.0-poc` tag.

## Build plan

Phases 1 to 5 are built and checked offline against the existing harnesses, then deployed together. The block only goes live after a shadow review.

| Phase | Scope | Check |
| --- | --- | --- |
| 1 · Learning flip | Analyser becomes a replay of reactions; rounding toward the last reaction; comfort from accepted readings | `analyser_demo` applies a 1° correction the next day; `compare_analyser` diffs v1 and v2 almanacs |
| 2 · On/off as reactions | Runtime detects a person turning units on or off; reaction kind stored | `runtime_check` records person on/off and still ignores the system's own changes |
| 3 · Cool-or-rest control | `app/control.py`, generator Jinja and the new unit helpers; the Warming/fan_only path is removed | Jinja matches `app/control.py` for every state and edge; `leak_response_check` still passes |
| 4 · Outdoor input | Config v2 migration; forecast or probe; outdoor value stored on heartbeats, reactions and section runs | The doctor lists weather entities with an hourly forecast; `store_check` sees the new columns |
| 5 · Lean and block, in shadow | Conditions entity with per-sensor edges; block decisions and margin learning logged, not applied | Published edges reproduce the lean table; shadow decisions appear in the log |
| **Gate A** | Cutover: deploy v2 to HA, block in shadow | |
| 6 · Now view | Donut, correction scale, sensor table and the Leak fixed button | Every state renders: Cool, Boost, Rest, Off, Leak, blocked and shadow |
| 7 · Usability | Config hot reload; one Apply button with a change preview | A saved config reaches the running observer without a restart |
| **Gate B** | Two-week shadow review: the block goes active | |
| 8 · Knob | ESPHome events, the four gestures and LED feedback, once the hardware is settled | One detent produces exactly one reaction and one setpoint change |

The shadow review clock starts at cutover, so phases 6 and 7 happen during it. Phases 1 to 3 already fix the snap-back and the cold-room behaviour, so they are worth deploying early if the outdoor work stalls.

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
- [ ] Minimum run and rest times (proposed 10 and 20 minutes): confirm against how the units behave.
- [ ] Boost step and offset cap (proposed −2° boost, offsets capped at ±3).
- [ ] Shadow-mode review length before turning the block on (proposed two weeks).

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
- **Household input:** two buttons per room, "too cold" and "too hot", which a dashboard, a physical button or the knob can press, so learning isn't limited to one person. The person is a sensor that always wins.

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

**A person's correction is a vote.** "Too cold" or "too hot" is a room-level vote that always wins: it forces the room's quorum its way (a rest or a boost), steps the target one degree, and moves the room's comfort readings (see [Person reactions](#person-reactions) and the learning sections below). Every human correction is meant to come through the room's two buttons, `input_button.ac_too_cold_<room>` and `input_button.ac_too_hot_<room>`, which deploy creates; a dashboard, a physical button's automation or the knob (phase 8) presses them, through automations that aren't ours. The remote is the fallback: a setpoint change on it is a vote too, too cold if raised and too hot if lowered, with the value chosen as that unit's new target.

**One correction per episode.** A vote is accepted only while no unit in the room is in a forced rest or boost (one a vote started). While one is, a press of either button, or a setpoint change on a remote, is an **ignored press**: logged, changing and teaching nothing. Once the forced state ends, the next vote counts.

**Home Assistant alone decides.** The generated vote automation (a press) and manual-change automation (a remote setpoint change) judge accept or ignore, and report it in an `ac_person_vote` event, `{room, direction, accepted, source}`, plus `unit` and `chosen` for a remote. The runtime records a `too_cold` / `too_hot` reaction, with its sensor snapshot, only from an accepted event, and logs an ignored one; it never judges the episode itself, and never records a remote setpoint change through its own detection window. One physical action, one reaction.

| Event | Detected by | Teaches |
| --- | --- | --- |
| Too cold / too hot button pressed | The room's `input_button`, through HA's `ac_person_vote` event | A vote: every unit's setpoint a step warmer or cooler; comfort readings toward reading ± `person_step_degrees`; bands; the margin, for too hot while cooling was blocked or too cold just after we started cooling |
| Setpoint change (remote, app, HA) | `temperature` attribute change by a person, through HA's `ac_person_vote` event | A vote, too cold if raised and too hot if lowered, with the value chosen as that unit's setpoint; the margin as for a press |
| Unit turned on by a person | State goes from off to any non-off mode | "Too warm" (cooler wanted): bands; if cooling was blocked, the margin learns |
| Unit turned off by a person | State goes to off | "Too cold or unwanted" (warmer wanted): bands; if we started cooling under 30 min ago, the margin learns |
| Mode or fan change by a person | `hvac_mode` or `fan_mode` changes while the unit stays on | Nothing: logged, not learned. The unit goes to `manual` |
| Knob turned warmer or cooler (phase 8) | Presses the room's too cold or too hot button | A vote, as any press |

### Grouping and kind

A vote is recorded on its own, the moment HA's event arrives, as one reaction of kind `too_cold` or `too_hot` (room and unit kind alike): with no unit rows for a button, and for a remote one row for the unit changed, `setpoint_after` the value chosen. The episode rule already keeps a vote from repeating while its forced state runs.

Turning a unit on or off, and a mode or fan change, are grouped by the existing 120-second extending window (`system.reactive_window_seconds`): each further change extends the window, and the whole window is recorded as one reaction. A setpoint change on its own is a vote and never enters a window; a window's `setpoint_after` moves only with a change that moved the setpoint itself (an `on` at a new setpoint), so a vote's setpoint never rides on it. Per unit, the record keeps the **first** `setpoint_before` in the window and the last `setpoint_after`. (v1 overwrites `setpoint_before` on every change; phase 0 fixes this.)

The reaction kind is stored per unit: `too_cold`, `too_hot`, `on`, `off`, `manual`, or on rows recorded before the votes `setpoint`, `knob_turn` or `knob_press`. In a window a unit's kind is `on` or `off` if its power changed between the first and last state (off → any mode is on), else `setpoint` if its setpoint was changed at all in the window (an on at a new setpoint, then off), else `manual` (a mode or fan change while on). Its `setpoint_before/after` and `power_before/after` keep the first and last values. The room-level kind is derived from the units' kinds when the reaction is recorded, and stored with it: `off` if any unit went off, else `on`, else `setpoint`, else `manual`. A unit turned on at a new setpoint (one change) is one reaction of kind `on`; setpoint learning still applies to it, because its `setpoint_after` changed.

A **setpoint-moving reaction** is a vote, a setpoint change or knob turn (rows recorded before the votes), or a reaction of any kind in which a unit's setpoint changed (an `on` that comes with a new setpoint, say). It moves S and the applied setpoint, makes the section learned and opens a comfort window.

### Setpoint

Every reaction whose `setpoint_after` changed moves the section's learned setpoint halfway toward what the person chose. The learning rate is `learning.reaction_rate`, default 0.5.

```
S_new = S_before + rate × (S_chosen − S_before)
```

S_chosen is the person's raw value, whatever offset or boost was active at the time. Offsets never feed learning.

**A vote** moves every unit with a seed, whatever its control state (control leaves some units alone, but the room's target still moved). S_chosen is the value chosen on a remote for the unit changed there; for every other unit, and for a button, it is the `setpoint_applied` in force ± one step, warmer for too cold. Through the rule below, that steps the applied setpoint exactly one degree.

The stored value stays fractional. The applied value, `setpoint_applied`, is on whole degrees and is tracked through the replay, reaction by reaction:

- **Normally it is the whole step nearest S.** An exact half goes toward the latest reaction's direction, the sign of S_chosen − S_before. A seed alone has no direction yet and rounds half up.
- **A correction always shows.** After a setpoint-moving reaction whose chosen value differs from the `setpoint_applied` in force at that moment, the new value moves at least one step from that previous value toward the chosen value, and never past it. A correction from 22 to 23 stores 22.5 and applies 23, so a 1° correction shows the next day. Plain rounding would send 22.5 back to 22, repeating the v1 bug.
- **A chosen value equal to the applied value keeps it.**
- **Nothing else moves it.** Quiet days, and reactions that change no setpoint (on/off or manual alone, or 22 → 23 → 22 in one window), leave it where it is.

This replaced rounding toward the direction of the latest reaction, which over-applied: Main Room sleep had S 24.17 and applied 25. In the worked example in `tools/analyser_demo.py` (23 in force when the person chose 25), 24.17 now applies 24: the nearest step, and still one step up. Had 24 been in force, the correction would still have to show, and 25 would apply. If S is 23 and the unit is boosting at 21 when a person sets 22, the gesture is warmer, but 22 is below the applied 23, so 22 is applied.

### Per-sensor comfort readings

A comfort reading moves only from what a person says:

- **A vote:** straight away, for each usable sensor with reading r at the vote, toward r + `system.control.person_step_degrees` (default 1.0) for too cold, or r − it for too hot, at `reaction_rate`. The person felt cold at r, so this sensor's comfort sits a step warmer. The runtime replays the room the moment it records the vote, so HA's next heartbeat already sees the moved comfort.
- **After a setpoint-moving reaction:** the mean of the sensor's heartbeat readings from 30 to 60 minutes after the reaction. The comfort reading moves toward it at `reaction_rate`.

The 30-to-60-minute window belongs to the reaction's section, even if a section boundary falls inside it. It is disqualified by:

- any reaction of any kind in the window,
- the unit being in `boost`, `rest` or `leak` at any heartbeat in the window (a vote's own forced rest or boost included, so a vote's window usually is), or
- fewer than 2 heartbeats in the window.

The update is applied at the first replay after the window closes; the runtime schedules a replay 65 minutes after each setpoint-moving reaction for this.

Quiet heartbeats no longer feed comfort readings. This keeps heater evenings and the system's own corrections out of the model.

### Band and trust

Each reaction nudges each sensor's band; quiet periods never move it. A band starts at the low-trust deviation (5°). At every reaction with a direction (a vote, on, off, or an older setpoint change), each usable sensor's deviation d = reading − comfort is taken against its comfort reading as it stood in the replay just before the reaction, and its band moves halfway (`reaction_rate`) toward:

- **|d|**, when the sensor is on the person's side (below comfort when they want it warmer, above when cooler) and inside its band, so it would have spoken;
- **0.8 × band**, when it is on their side and already past its band (it did speak, so it tightens a little);
- **band + |d|**, when it is on the other side (it said the opposite, so it widens).

A sensor exactly at comfort says nothing either way. The band is clamped to ±0.5° to ±5° (`system.trust`), and trust follows from it. Warmer is wanted by too cold, a unit turned off or a setpoint raised; cooler by too hot, a unit turned on or a setpoint lowered. Manual changes teach no band. The band itself stays symmetric.

### Confidence

```
confidence = 1 − 0.5^(quiet / 3)
```

*quiet* counts the section's completed runs since its last reaction that had at least one heartbeat and no reaction of any kind. Any reaction, a vote included (an ignored press is no reaction), resets it to 0. Confidence drives the UI, not control.

### Rebuilding

The almanac is a deterministic replay of every reaction, in time order, from a seed, so it rebuilds from CSV alone. There is no rolling window; older reactions fade because each new one moves the value halfway.

**Seed:**

- **Per unit:** the earliest heartbeat in that room and section with the unit on and a non-null setpoint.
- **Per sensor:** its earliest non-null reading in that section.
- **No seed:** the unit has no setpoint to apply, so it is left alone. The scene and maintenance send it no commands.

The runtime's provisional seeding at crossover is retired. Instead, the runtime replays every room at startup, before any crossover can fire, and again when the first heartbeat lands in a section with no almanac.

**The replay runs:**

- at startup, for every room, and pushed to HA before any crossover (catch-up included), so a crossover just after a start or a migration finds `setpoint_applied`;
- after every reaction, so the UI updates immediately (a vote's at once, so HA's next heartbeat sees its comfort move);
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
| `boost` | cool, fan medium, base + offset − boost step; forced by a too-hot vote, or the system's |
| `rest` | off, waiting; forced by a too-cold vote, started by a section change, or the system's |
| `user_off` | off, because a person turned it off; hands off until the section changes |
| `manual` | the mode and fan a person chose; hands off until the section changes |
| `config_off` | off, because the section's scene config forces it off; maintenance never touches it |
| `leak` | off, latched (no setpoint, no fan: dry would still run the compressor) |

A **forced** rest or boost is one a person's vote started, marked by `input_boolean.ac_forced_<room>_<unit>`. It runs and releases by the same rules as the system's, except that its release returns to `cool` at the base, without the ±1 offset step.

A **section rest** is a rest a section change started because the room starts cold (see Section change below), marked by `input_boolean.ac_section_rest_<room>_<unit>`. It also runs and releases by the system's rules and wakes into `cool` at the base, offset 0 rather than +1, because nobody said too cold. It is not an episode: a vote during it counts, and becomes a forced state like any other.

**Base** is the unit's `input_number.ac_base_<room>_<unit>`. At each section change the scene sets it to the almanac's `setpoint_applied`. A person's vote or turn-on resets it (see [Person reactions](#person-reactions)). Almanac pushes never change it mid-section, so a new S takes effect from the section's next run. A base of 0 means unset, and templates fall back to `setpoint_applied`. Every command is base + offset, minus the boost step in `boost`. Offsets build up within a section, reset at the next section change, and never feed learning.

System transitions:

- `cool` → `boost` when the quorum is met and the direction is too warm.
- `boost` → `cool` when back at comfort or after the boost cap; offset −1, or for a forced boost back to the base (offset 0).
- `cool` → `rest` when the quorum is met and the direction is too cold, or when cooling is blocked and the unit isn't unblocked.
- `rest` → `cool` when back at comfort; offset +1, or for a forced rest or a section rest back to the base (offset 0). A rest doesn't wake while blocked.
- Every transition clears the forced and section-rest markers; only a vote sets the first, only a section change the second.
- Any state → `leak` when the leak sensor is wet. Leak beats everything, including a person turning the unit off. When the leak is fixed and confirmed, the unit is evaluated as at a section change.
- **Section change:** offsets reset to 0, base = `setpoint_applied`, `since` = now, and `ac_unblocked`, `ac_grace_until`, `ac_forced` and `ac_section_rest` are cleared. Each unit starts in `config_off` if the section's scene forces it off, otherwise in `rest` if cooling is blocked, otherwise in a section rest if the room starts cold, otherwise in `cool`. `user_off` and `manual` end here; a unit in `leak` stays there, and off is sent again. A section change overrides minimum times.
- **The room starts cold** when both hold: at least a quorum (counted as at least 1, as for a release) of its usable sensors read below their comfort readings in the new section's almanac, so a rest started now wouldn't release at once; and the section's forecast mean is below the room comfort point (Δ of the mean > 0, from the conditions entity). The sensors needn't be past their trigger edges: below the comfort reading is enough. With no conditions for the section, no outdoor value or no comfort point (fail open), it doesn't, and the unit starts in `cool` as before. A confirmed leak release, evaluated as at a section change, follows the same rule. The crossover's log event says whether the room started in `rest`, and why.

### Person reactions

HA applies these straight away, so they hold even while the container is down. The container records them as reactions for learning: votes from HA's `ac_person_vote` event, the rest from its own detection.

**A vote** (too cold or too hot, from the room's button through the vote automation `ac_vote_<room>`, or a setpoint change on a remote through the manual-change automation) is for the whole room. If any unit is in a forced rest or boost, it is ignored: nothing changes. Otherwise each unit takes its part:

| Unit in | Too cold | Too hot |
| --- | --- | --- |
| `cool` | `rest`, forced, off | `boost`, forced, cool at the new base − boost step, fan medium |
| `boost` | `rest`, forced, off | stays `boost`, forced, `since` kept, the new command |
| `rest` | stays `rest`, forced, `since` kept, nothing sent (already off) | `boost`, forced |
| `user_off` | stays `user_off` (the person's off stands), not forced | `boost`, forced |
| `manual`, `config_off`, `leak` (or its latch on) | left alone | left alone |

Every unit it moves gets base = its target (base, or `setpoint_applied` while unset) one step (1°) warmer for too cold or cooler for too hot, or for the unit changed on a remote the value chosen; offset 0; and no grace. The base is clamped to the unit's range. A too hot vote while cooling is blocked (an active block, or a heat source) also turns on each moved unit's `ac_unblocked`, as a person's turn-on does, so the block doesn't send its forced boost straight back to rest. A unit with neither a base nor an almanac is left alone. A unit already in the state the vote forces keeps its `since`, so its minimum time and the boost cap count from when that state began; any other gets `since` = now. The reaction is recorded whichever units moved. A leak beats a vote.

A forced state releases by the normal rules (back at comfort, the boost cap, minimum times), into `cool` at its base. The marker clears on release, at a section change, on a leak, and when a person turns the unit on or off or changes its mode.

- **Turned off:** state `user_off`.
- **Turned on:** state `cool`, base = the unit's current setpoint, offset 0, `since` = now. The grace period starts. `input_boolean.ac_unblocked_<room>_<unit>` turns on, so the cooling block is ignored for that unit for the rest of the section.
- **Mode or fan change while on:** state `manual`. Logged, not learned.
- **While a leak is latched,** none of these change that unit: it stays `leak`. A setpoint change on its remote is still a vote for the room's other units.
- **While `ac_hold` is on,** a vote is still judged and reported (and so learned from), but HA writes no helper and sends nothing.

The **grace period** runs until `input_datetime.ac_grace_until_<room>_<unit>`, which a person's turn-on sets to now + `system.control.person_grace_minutes` (default 30). Until then the unit is held in `cool`: no transition into `boost` or `rest`, a cooling block included. A leak still wins, a vote clears it, and so does a section change.

### Rules

- **Stale sensors:** a sensor Home Assistant last reported longer ago than `system.sensor_stale_minutes` (default 90) is treated as unavailable, even though its state still shows a value: no vote, not counted toward the quorum, and left out of heartbeats, reaction snapshots and so of seeds, comfort windows and bands. A virtual sensor goes stale with its unit. The maintenance template reads each entity's `last_reported`, the runtime the same field from the states it fetches (`app.control.is_stale`), and the runtime logs one `validation` warning when a sensor goes stale and one event when it reports again. The doctor flags it.
- **Quorum, exactly as in v1:** one pooled count. A sensor votes when it is past either of its leaned trigger edges, and votes on both sides count toward the same tally. 1 usable sensor needs 1 vote, 2 need 2, 3 or more need at least half. When the quorum is met, the trust-weighted majority of the voters' sides picks the direction, `too_warm` or `too_cold`; an exact tie is `none`, which means no action.
- **Release:** a boost or rest ends when the pooled count of sensors still on the triggering side of their comfort readings falls below the quorum, counted as at least 1. With no usable sensor, a rest wakes into `cool` rather than wait for ever.
- **Boost cap:** `system.control.boost_max_minutes` (default 60). The unit then returns to `cool` at −1; if the room is still too warm next heartbeat, another boost can start, so it steps down gradually.
- **Offsets and limits:** the offset moves in whole steps and is capped at ±3 on its own. The boost step, `system.control.boost_degrees` (default 2), goes on top, and the final command is clamped to the unit's own range (18 to 30° for the baby room, 16 to 30° for the others).
- **Minimum times:** counted in heartbeats, to protect the compressor: at least 1 in `cool` or `boost` (a boost that a block sends to `rest` included), at least 2 in `rest`. A minimum of n heartbeats is met when the time since `since` is at least (n − 0.5) × the heartbeat interval, which is 5 and 15 minutes at the default 10-minute interval. A section change, a leak and a person's vote override them.
- **No seed:** while a unit has neither a base nor a `setpoint_applied`, maintenance leaves it alone and a section change resets its fields without a command. A leak still turns it off, and a leak released before there is an almanac goes to `rest`: still off, and woken by maintenance once there is a setpoint to cool to.
- **Commands:** sent only on transitions, at section changes and on a vote, never re-sent every heartbeat. Maintenance, the vote and manual-change automations and the leak automations raise `ac_active` around every command, as scenes do.
- **Fan:** low in `cool`, medium in `boost`. Every command sets the mode first and the fan last, and the fan waits (up to 30 s, then carries on) until the unit reports the new mode, because a unit offers no fan modes in dry or off. A fan call the unit's mode still rejects is skipped, not fatal (as in v1).
- **Pause:** while `ac_hold_<room>` is on, scenes, maintenance and votes send no commands. Maintenance doesn't run at all; scenes and a leak release still write the helpers; a vote writes nothing but is still reported. A leak trip ignores the pause. Only a person turns it on or off.

## Outdoor temperature

Outdoor temperature never sets a target. It leans each sensor's trigger edges and, when it is cold enough, blocks cooling for the section.

### Source and values

`outdoor.source` is `forecast`, `probe` or `none`.

- **Forecast:** a weather entity's hourly forecast (`weather.get_forecasts`), fetched at startup and hourly, and read at each section change: the cached one if it is under 2 hours old, otherwise fetched first. The section's window runs from its planned start to the next section's planned start, across midnight for sleep. The section gets two values from the hourly entries whose times fall in its window: the **mean** (drives the lean) and the **max** (drives the block). A window the forecast covers only in part uses what is covered.
- **Probe:** a temperature sensor, smoothed as a 30-minute rolling mean. The same value serves as mean and max. Not read yet (decision 89).

Every heartbeat and reaction records the outdoor value: with the forecast, the weather entity's current temperature, or none if the entity is unavailable or hasn't updated for 3 hours. Every section run records its mean, max, lean and block decision. That history is what later tuning replays against.

### Comparing outside with inside

The **room comfort point** is the trust-weighted mean of the usable sensors' comfort readings (those reporting now, and not stale), or a plain mean until trust is earned. It is not the AC setpoint, which can sit several degrees from the room's air temperature. With no usable sensor there is no comfort point, and the section fails open.

Δ = room comfort point − outdoor value. A positive Δ means colder outside. The lean uses Δ of the forecast mean, the block Δ of its max.

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

Cooling is blocked when the section's warmest forecast hour is at or below the room comfort point − **margin** M (Δ of the max ≥ M). M may be negative: at the default −1, a room stops actively cooling a section whose warmest forecast hour is at or below its comfort point + 1 °C. A room blocked last section (its decision, shadow's included) stays blocked until Δ of the max ≤ M − 1: at −1, until the max reaches comfort + 2 °C. It is decided at each section change and locked for the section; a restart inside it keeps the decision.

While blocked, the scene rests the units, "too warm" votes are ignored, and the amber arc shows a lock. Leak protection is unaffected, since a leak turns the unit off.

If a person turns a unit on during a block, or votes too hot, that unit's `ac_unblocked` helper turns on, and the block is ignored for it until the section changes. The margin is per room: it starts at `outdoor.initial_margin` (−1 °C by default), stays between −3 and 10 °C (crossing 0 either way), and learns from reactions, halfway (`reaction_rate`) each time, by the replay (a step that would take it below −3 stops at −3):

- **Too hot while blocked:** a too hot vote, or a person turning a unit on, in a section decided blocked: M moves toward Δ of the max + 0.5, so that section would not have been blocked.
- **Too cold just after cooling started, unblocked:** a too cold vote, or a person turning a unit off, within 30 minutes of the system starting to cool (the scene, or maintenance waking a unit from rest) in a section decided unblocked: M moves toward that section's Δ of the max.

Sections decided with `block_mode` off, or with no comfort point or forecast max, teach nothing. When the system started cooling is read from the heartbeats' control state, so the margin rebuilds from CSV (decision 109).

### Shadow mode

`outdoor.block_mode` is `off`, `shadow` or `active`, defaulting to `shadow`. In shadow mode the block is computed and logged ("would block Night: forecast max 17.0, comfort 23.9, margin 6.0") but not applied: the conditions entity tells control `blocked` false, with `would_block` true. Margin learning still runs against the decisions it would have made. With `off` nothing is blocked; the lean applies in every mode. Switch to `active` after reviewing about two weeks of shadow decisions; the change takes effect from each room's next section.

### Heat sources

`rooms[].heat_sources` optionally lists entities such as heater plugs. While any is on, cooling is blocked immediately, regardless of the forecast. This applies in `shadow` and `active` modes. The templates check them live, so a heater blocks with the container down; deploy refuses a heat source Home Assistant doesn't have. A heat source teaches no margin.

### No outdoor value

Fail open: no lean and no block (edges −1 and +1). The status strip and the log show a warning: a failing forecast logs one `outdoor` warning per failure streak and one event when it works again, and `outdoor.source` none does the same once. A missing or unknown conditions entity also fails open in the templates. A mistaken block in a Hong Kong summer, especially in the baby room, is worse than a possible heater fight. Heat sources still block.

## Where it runs

The container decides once per section; Home Assistant acts at every heartbeat. Everything HA needs is published to state entities, so control continues if the container is down.

### Container

- **At each section change:** computes the room comfort point, Δ, the lean, the block decision and each sensor's trigger edges, then publishes them to a new `sensor.ac_conditions_<room>`: before the scene fires whenever the cached forecast is under 2 hours old (or there is nothing to fetch), so the scene starts from them; otherwise the scene fires on fail-open conditions and the real ones follow once the forecast is in, for the next heartbeat's maintenance. It is re-pushed on a timer, on every websocket reconnect and on `homeassistant_start`, like the almanac. The section run records the comfort point, the lean, the decision and the block mode, and the log one `block` event. The `crossover` event says how the scene starts the room and why (`control.section_start` from the sensors it reads, the section's almanac and the conditions just published): "Main Room -> sleep: starts in rest: 3 of 3 sensors below their comfort readings (2 needed), and the forecast mean 17.8 is below the comfort point 23.9", or in `cool` because the forecast isn't cooler, the room isn't cold, or there is no forecast yet (fail open).
- **On every reaction:** records it with its per-unit kind and the outdoor value, replays the almanac and pushes it. A vote comes from HA's `ac_person_vote` event: an accepted one is recorded at once with every sensor's reading, an ignored one is logged as an "ignored press". After a setpoint-moving reaction, it schedules another replay 65 minutes later for the comfort window.
- **At startup:** replays every room and pushes its almanac before any crossover.
- **On the first heartbeat in a section with no almanac:** replays that room.
- **Nightly:** full replay as a consistency check.
- **No commands of its own:** the knob presses the room's buttons in HA, so the container never drives a unit itself.

### Home Assistant (generated)

- **Scene automation, per room and section:** raises `ac_active`, records the section, and sets each unit's base, offset, `since`, grace, `ac_unblocked`, `ac_forced`, `ac_section_rest` and state as at a section change (rest if blocked: the conditions entity's block, or a heat source; a section rest if the room starts cold, counting the sensors as maintenance does, against the new section's almanac and the conditions entity's edges and Δ of the mean), then sends the commands. A unit with no seed is left alone. While `ac_hold` is on, it sends no commands.
- **Maintenance automation, every heartbeat:** reads the almanac, the conditions entity and each unit's helpers. It counts the pooled quorum against the published edges and runs the state machine, a forced state's release included. On a transition it raises `ac_active`, writes the helpers, sends the commands and lowers `ac_active`.
- **Vote automation, per room (`ac_vote_<room>`):** triggered by a press of either button (a state trigger with `to: null`, from a state that isn't unavailable, to one that is neither unavailable nor unknown, so a new button's first press counts). It judges the episode, fires `ac_person_vote`, then, if accepted and the hold is off, raises `ac_active`, writes every unit's helpers, sends the commands and lowers `ac_active`. Mode `queued`, so a second press waits for the first and finds its episode.
- **Manual-change automation, per unit:** triggered by the unit's mode changing (a state trigger with `to: null`) or its `temperature` or `fan_mode` attribute changing, never by a `current_temperature` update; catches a person's change (no `context.parent_id`, `ac_active` off) and applies the [person reactions](#person-reactions) straight away. A setpoint change is a vote for the room, judged, reported and applied as a press is, with the value chosen as that unit's base; on, off and a mode or fan change go to that unit's helpers alone, with no command. One change can fire two triggers (a mode and a setpoint at once); only the first that applies, in the order mode, temperature, fan, goes on, so each change runs once and a vote is never counted twice. It ignores our own command arriving late, by the test in [What counts as a reaction](#what-counts-as-a-reaction), and classifies the change's kind as the runtime does.
- **Leak automations, per unit:** set the state helper to `leak` when the sensor trips. On a confirmed release, they evaluate the unit as at a section change.
- **Conditions and heat sources:** every template that reads the block or the edges (scene, maintenance, leak release, vote, manual change) reads them from `sensor.ac_conditions_<room>` only while its state is the section in force, falling back to −1/+1 and not blocked; the generator writes the room's heat-source entities into the same templates, which check them directly. A heater switching on blocks cooling without waiting for the container.

### Helpers

| Helper | Scope | Purpose |
| --- | --- | --- |
| `input_select.ac_state_<room>_<unit>` | Unit, new | `cool`, `boost`, `rest`, `user_off`, `manual`, `config_off`, `leak`. Deploy sets a new one to `manual`: hands off until the room's next section change |
| `input_number.ac_base_<room>_<unit>` | Unit, new | The section's base setpoint: `setpoint_applied` at the section change, or what a person chose. 0 to 100 in steps of 0.5; created at 0, its minimum, which means unset |
| `input_number.ac_offset_<room>_<unit>` | Unit, new | Section-scoped offset, −`offset_cap` to +`offset_cap` in whole steps. Deploy sets a new one to 0 |
| `input_datetime.ac_since_<room>_<unit>` | Unit, new | When the current state began (boost cap, minimum times). Deploy sets a new one to now |
| `input_datetime.ac_grace_until_<room>_<unit>` | Unit, new | When a person's grace period ends: their turn-on + `person_grace_minutes`. Cleared at a section change or by a vote (set to now); a past or unset value means no grace. Created at HA's default, today 00:00 |
| `input_boolean.ac_unblocked_<room>_<unit>` | Unit, new | A person turned the unit on during a block; the block is ignored for it until the section changes |
| `input_boolean.ac_forced_<room>_<unit>` | Unit, new | This unit's rest or boost was forced by a person's vote: an episode is running while it is on and the unit is in `rest` or `boost`. Cleared on release, at a section change, on a leak and by a person's on, off or mode change. Created off |
| `input_boolean.ac_section_rest_<room>_<unit>` | Unit, new | This unit's rest began at the section change because the room started cold, so it wakes at the base (offset 0). Not an episode. Cleared on wake, at the next section change, by a vote, on a leak and by a person's on, off or mode change. Created off |
| `input_button.ac_too_cold_<room>`, `input_button.ac_too_hot_<room>` | Room, new | The person's vote. Anything may press them: a dashboard, a physical button's automation, the knob. Created never pressed (unknown) |
| `sensor.ac_conditions_<room>` | Room, new; pushed by the container, not created by deploy | State: the section. Attributes: `cold_edge`, `warm_edge` (band widths), `blocked` (as control sees it), `would_block`, `block_mode`, `margin`, `comfort_point`, `outdoor_mean`, `outdoor_max`, `delta_mean`, `delta_max`, `lean`, `fail_open`, `edges` (each sensor's trigger edges in degrees) and `computed_at` |
| `ac_active`, `ac_scene`, `ac_almanac` | Room, kept | As in v1 |
| `ac_hold_<room>` | Room, changed | A manual pause switch only; the system never sets or clears it |
| `ac_correction_started_<room>` | Room, removed | Replaced by the per-unit `since` helper; the first v2 deploy prunes it |

No helper has an `initial`: Home Assistant would reset it to that at every restart. Deploy sets the starting values of the helpers it has just created, and only those.

### Almanac contract additions

- Per unit: `setpoint` (precise) plus `setpoint_applied` (already on a whole degree, tracked through the replay as in [Setpoint](#setpoint)), so templates never round.
- Per section: `status` (`provisional` or `learned`) and `confidence`.
- Per room: the learned `margin` (the almanac entity's `margin` attribute; replayed, not stored).

### One logic, checked twice

The state machine, including person reactions and votes, leak precedence and minimum times, lives in a pure-Python module, `app/control.py` (phase 3a). The generator renders the same logic as Jinja (phase 3b). `tools/control_check.py` holds the scenario table, as plain values in vote, step, section-change, person-change, button-press and climate-change steps, over a room of one unit or more; the check also removes each rule in turn and requires the scenarios that guard it to fail. `tools/control_logic_check.py` (phase 3b, replacing v1's `maintenance_logic_check`) runs the generated automations as Home Assistant would, replays every scenario through the maintenance, scene, vote, manual-change and leak automations, and compares every unit's helpers and commands with `app/control.py`, and each `ac_person_vote` event with control's vote: accepted exactly when `control.in_episode` found no forced state.

## UI

The Now view becomes the donut sketch: a donut per room, a correction scale and a sensor table. The visual layer replaces numbers as the first thing anyone reads. It is built (phase 6): `app/static/now.js` draws, in SVG, what one poll of `GET /api/now` returns every 15 s, and `app/nowview.py` computes everything in that payload through `app.control`, reading Home Assistant's states the way the generated templates do (decision 114). It is a desktop view (decision 126), in light and dark mode, and no zone or state is told by colour alone. Screenshots of every state are in `docs/screenshots/`.

### Now: donut

- **Fixed arcs:** blue (rest), green (comfortable), amber (cool), each a third of a 270° sweep, labelled "rest" and "cool" inside the ring. The comfort reading sits at the top, with the room comfort point under it.
- **One marker per sensor,** a numbered pointer outside the ring, numbered to match its table row. Positions are in band widths, scaled piecewise so the leaned trigger edges land on the fixed arc boundaries. A change in the forecast therefore moves every marker, which is the lean made visible. Markers close together step out to a second or third orbit.
- **Markers pin at the ends** of the scale, two band widths past each edge, with a double-chevron overflow tick, since a trusted sensor can travel far past its band.
- **A stale or unavailable sensor** (or one with no comfort reading yet) has no position: a hollow, numbered marker under the label strip.
- **Centre fan:** stopped, slow or fast for off, low and medium; it stands still for anyone who asks for reduced motion. The bottom segment is the label strip and names the state: Cool · Low, Boost · Medium, Resting, Off (by you), Manual, Off (scheduled), Leak · Off, with a second line for a forced state ("you said too cold", "you said too hot"), a manual unit's mode and fan, a turn-on's grace ("you turned it on") or a hold ("paused (hold)").
- **Block:** the amber arc greys out with a lock reading "blocked", or "heater on" for a heat source. In shadow mode it gets a dashed lock reading "would block", and the arc keeps its colour.

### Now: correction scale

- **Centre number:** what the unit is set to now: our command's target in cool or boost (base + offset, minus the boost step in boost), the person's own setpoint in manual, or "Off" while the unit is off (rest, Off (by you), Off (scheduled), leak).
- **Chevrons:** the correction, the difference between that and the learned `setpoint_applied`: one per degree up to three, up for warmer and down for cooler, with the exact difference beside them. The section offset, a vote's moved base and a boost all show. None while the unit is off.
- **Learned:** the learned `setpoint_applied`, small, under the scale ("learned 23°").
- **Magenta marker:** outdoor value against the room comfort point, labelled with the forecast mean and max, on a ±5 °C scale (warmer up). Past the end it turns into an arrow. Shown while the conditions entity is live for the section.

### Now: sensor table

- **Sorted by position in band,** warmest first, so the table reads in the same order as the donut. Raw readings aren't comparable across sensors. Sensors with no position come last.
- **Columns:** reading, name, band (shown as ±°) and a trust bar.
- **Zone bar** on the left, coloured blue, green or amber, with a glyph (▼ ● ▲, ○ for no reading), which shows the quorum at a glance; a line above the table spells it out ("Quorum met, too warm: 2 votes (2 needed)").
- **Stale rows** are greyed with "stale since hh:mm".

**Too cold** and **Too hot** buttons under each room press the room's vote buttons in Home Assistant, which still judges each vote; the card then shows what Home Assistant said ("Got it, resting", "Already correcting, ignored", or "Sent" if no answer came within a few seconds). A **"Leak fixed"** button appears on the room card while a leak is latched, with a confirm step. It replaces the trip to Developer Tools. A **status strip** at the top lists what needs attention: a config that doesn't validate, an outdoor failure, a latched leak, a unit or sensor not reporting or stale, a hold left on, and conditions failing open. In a room with several units the first is drawn and the others are chips; the full multi-unit drawing stays deferred.

### Almanac and Log

- **Almanac:** per section and unit, the learned and applied setpoint, status and confidence, plus a reaction timeline ("went cooler 3× this week") and the margin's history.
- **Log:** new event types for each reaction kind, state changes (boost, rest, release), block decisions (including shadow), and outdoor-data failures.

### Usability fixes

- **Config hot reload:** a saved config reaches the running observer without a container restart.
- **One Apply button:** it saves, previews the changes, and deploys only when HA entities change. This replaces the unclear split between Save and Deploy.

## Household input: the knob

The knob says "warmer" or "cooler", never a number, so anyone in the house can teach the system without understanding setpoints or sensors. It does that by pressing the room's buttons, and nothing else: a turn warmer presses `input_button.ac_too_cold_<room>`, a turn cooler presses `input_button.ac_too_hot_<room>`. Everything a press does (the vote, the episode rule, the event, the learning) is the buttons'.

| Gesture | Presses | Result |
| --- | --- | --- |
| Turn one detent warmer | too cold | A too-cold vote: a forced rest, the target a degree warmer |
| Turn one detent cooler | too hot | A too-hot vote: a forced boost, the target a degree cooler |

A turn of several detents is one vote: the first press starts the episode, and the rest are ignored presses until its forced state ends.

**Wiring:** ESPHome `rotary_encoder` (and, if it has one, a button `binary_sensor`) fire HA events; a small HA automation, which isn't ours, presses the matching button. A physical too-cold / too-hot button pair is wired the same way. The container sees only the votes, through `ac_person_vote`.

**Scope:** each knob is assigned to one room in config (`rooms[].knobs`), and its presses are that room's votes.

**Open:** what a knob press and an LED would do (see [Open questions](#open-questions)).

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
- `system.control`: `boost_degrees` 2, `boost_max_minutes` 60, `cool_min_heartbeats` 1, `rest_min_heartbeats` 2, `person_grace_minutes` 30, `offset_cap` 3, `person_step_degrees` 1.0 (added with the votes)
- `system.sensor_stale_minutes`: 90 (added after the first live run)
- `outdoor`: `source` none, `weather_entity` null, `probe_entity` null, `block_mode` shadow, `lean_per_degree` 0.1, `initial_margin` −1 (6 until decision 112, 0 until decision 113). `weather_entity` (a `weather.*` entity) must be set when `source` is `forecast`, and `probe_entity` (a `sensor.*` entity) when it is `probe`. Phase 4 consumes them.
- `rooms[].heat_sources` (entity ids) and `rooms[].knobs` (`{id, name?}`, a placeholder until the hardware is settled): empty

The loader moves `system.max_step_degrees` to `system.control.boost_degrees` and `system.correction_max_minutes` to `system.control.boost_max_minutes`. `bootstrap_min_days`, `validity_delay_days`, `analysis_window_days` and `reactive_weight` are dropped, and so is `learning.recency_half_life_days` (from a config of either version: an earlier v2 build filled it in, and bands no longer use it). A scene's per-unit `mode: off` is kept and maps to the `config_off` state.

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
- **Migration 3 (votes):** rebuilds `reactive` and `reactive_unit` with their rows, so their kind checks allow `too_cold` and `too_hot`. Backed up first as `<name>.pre-votes-backup-<timestamp>`.

### Seeding from v1 data

Replay the archive's existing reactions through the v2 learner, starting from each section's seed (see [Rebuilding](#rebuilding)), and ignore quiet heartbeats. Finish `tools/compare_analyser.py` (currently a skeleton) to diff the v1 and v2 almanacs before cutover.

### Cutover

CSV re-ingest must work first. Then deploy v2 with the block in shadow mode. Deploy creates the new helpers, rewrites the automations and removes the old correction timer, all inside the `ac_` namespace. Rolling back means redeploying from the `v1.0-poc` tag.

## Build plan

Phases 0 to 5 are built and checked offline against the existing harnesses, then deployed together. The block only goes live after a shadow review.

| Phase | Scope | Check | Status |
| --- | --- | --- | --- |
| 0 · Foundations | Storage migrations (`user_version`, backup first, checked transaction, rollback; `almanac` and `event` rebuilt); `section_run` written to CSV; every v2 config key with defaults, and the v1 config migration; `setpoint_before` keeps the first value in a window | `store_check` migrates a v1 database keeping every row, finds it identical to a fresh one, and sees a forced failure leave it untouched; `config_check` migrates the v1 example to exactly the v2 example; a three-step setpoint change records its first `setpoint_before` | Built |
| 1 · Learning flip | Analyser becomes a replay of reactions from per-unit and per-sensor seeds; `setpoint_applied` tracked through the replay (see [Setpoint](#setpoint)); comfort from accepted readings; confidence; the runtime's provisional seeding retired | `analyser_demo` applies a 1° correction the next day; `compare_analyser` diffs v1 and v2 almanacs | Built |
| 2 · On/off as reactions | Runtime detects a person turning units on or off, or changing mode or fan; reaction kind stored per unit and room; a unit coming back from unavailable changed is logged | `runtime_check` records person on/off and still ignores the system's own changes; on plus a setpoint change in one window is one `on` reaction | Built |
| 3a · Control core | `app/control.py`: votes, the state machine, section changes and person reactions as pure functions; `app.trust.evaluate_edges` | `control_check` runs the scenario table, and each rule's scenarios fail when it is removed | Built |
| 3b · Cool-or-rest in HA, deploy and leak | Generator Jinja for maintenance, scenes and the manual-change automation, with the new unit helpers; maintenance raises the guard; the Warming/fan_only path is removed. `input_number` (and `input_boolean`) helper domains, created at 0; every `ac_since_*` set to now on creation; leak automations write the state helper; the runtime's reaction detector also ignores a change that matches what the unit's state helper implies (`rest` → off, and so on), as the manual-change automation does | `control_logic_check` replays every `control_check` scenario through the generated automations with zero differences; `deploy_reconcile_check` creates the new helpers with the right initial values; `leak_response_check` still passes | Built |
| 3c · The person's vote | Two buttons per room and a forced marker per unit; the vote automation and the manual-change vote path, HA judging one correction per episode and reporting it in `ac_person_vote`; forced release to base; votes as reactions with an immediate comfort move; the per-reaction band nudge; storage migration 3 | `control_check` and `control_logic_check` replay the vote scenarios (the event's accepted flag matches `control.in_episode`); `runtime_check` records only HA's accepted events; `analyser_demo` moves comfort and bands by the new rules | Built |
| 4 · Outdoor input | Forecast (`weather.get_forecasts`, hourly, cached) or probe; outdoor value stored on heartbeats, reactions and section runs (the forecast's mean and max over the section's window) | The doctor lists weather entities with an hourly forecast and shows the configured one's next hours (`doctor_check`); `runtime_check` records a section's mean and max (a normal window, sleep across midnight, a partial window, UTC forecast times matched by local time on windows crossing UTC midnight but not local midnight and the reverse), reuses or refetches the cache, fails open on each failure with one warning per streak, and records heartbeat and reaction values; `store_check` round-trips the outdoor columns through CSV and SQLite | Built: forecast only, probe deferred (decision 89) |
| 5 · Lean and block, in shadow | Conditions entity with per-sensor edges; block decisions and margin learning logged, not applied | `control_check` reproduces the lean table and its caps, the block at the margin, its hysteresis, shadow, active and off, heat sources, a too hot vote unblocking, the comfort point and failing open, each rule pinned by a mutant; `control_logic_check` replays the table with the conditions published to the entity, and reads it back missing, unknown, unavailable and another section's; `runtime_check` publishes before the scene from a fresh cache and after it from a stale one, holds a block across sections, logs shadow's "would block", keeps a decision across a restart, learns the margin in each case, re-pushes, and fails open | Built: the block in shadow |
| **Gate A** | CSV re-ingest works; then cutover: deploy v2 to HA, block in shadow | SQLite rebuilt from CSV alone gives the same almanac | To do |
| 6 · Now view | Donut, correction scale, sensor table and the Leak fixed button | Every state renders: Cool, Boost, Rest, Off, Manual, Off (scheduled), Leak, blocked and shadow. `now_render_check` renders 20 cases built through the real `app.nowview` in headless Chromium at desktop width (the forced states, a heat-source block, a stale sensor, a pinned marker, a strong lean whose edges land on the arc boundaries, two units, light mode, the vote buttons' replies) and saves each to `docs/screenshots/`; `now_api_check` pins `/api/now`'s shape and the vote and leak-fixed endpoints | Built |
| 7 · Usability | Config hot reload; one Apply button with a change preview | A saved config reaches the running observer without a restart | To do |
| **Gate B** | Two-week shadow review: the block goes active | | To do |
| 8 · Knob | ESPHome events pressing the room's buttons, once the hardware is settled | A turn of several detents produces exactly one vote | To do |

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

- [x] Which weather entity provides an hourly forecast? `weather.forecast_home` (Met.no). `weather.hong_kong_observatory` has none (decision 98).
- [ ] Knob hardware: detents, how many knobs, which rooms. Whether its press and an LED or ring do anything, now that a turn only presses a button: a "this is right" press would need its own learning rule.
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

From the live install, 28 Oct 2026:

73. **Ids must be unique:** room ids, and unit and sensor ids within a room. The schema accepted two sensors with the id `baby` in one room, and the second overwrote the first in every heartbeat, so a live sensor was never seen. `validate()` rejects a repeated id, naming both places. The UI save path is covered twice: the server's save runs `validate()`, and the Config page refuses to post a duplicate (in raw-JSON mode too, where Save now saves the text box). The page also generates a unique id (`<room>_sensor`, `_2`, ...) when a sensor is added, and de-duplicates a typed unit id. Because a config already on disk is loaded without validation, the deploy check now validates the config and the deploy aborts on any check problem, and the runtime logs an error event at startup for a config that doesn't validate.
74. **A stale sensor is treated as unavailable.** A sensor put away kept its last value in Home Assistant for three days without going unavailable (`last_reported` stayed at 2026-10-25T20:03), so it kept voting and feeding learning with a frozen reading. Past `system.sensor_stale_minutes` (default 90, a new v2 key) since its `last_reported` - its unit's, for a virtual sensor - a sensor reads as unavailable: no vote and no quorum place in the maintenance template (`now() - states.<entity>.last_reported`), and in the runtime a None in heartbeats and reaction snapshots, which keeps it out of seeds, comfort windows and band samples. Exactly at the limit it is not stale. The runtime logs one `validation` warning when a sensor goes stale and one info event when it reports again, and reads back what it last logged at startup so a restart doesn't repeat the warning; the doctor flags it. Readings recorded before this change keep their frozen values.

The person's vote, 28 Oct 2026 (supersedes 50 and 52 for setpoint changes, 51, the knob parts of 8 and 19, the band weighting of 12 and 26, the double run of 64, and grace from a setpoint change in 49):

75. **The person is a sensor that always wins.** A correction is a room-level "too cold" or "too hot" vote: it forces the room's quorum its way, steps the target one degree and moves the room's comfort readings. Every human correction is meant to come through two buttons per room, `input_button.ac_too_cold_<room>` and `input_button.ac_too_hot_<room>`, created by deploy, which a dashboard, a physical button's automation or the knob presses (those automations aren't ours). A press is a reaction of kind `too_cold` / `too_hot`. The remote is the fallback: a setpoint change counts as too cold (raised) or too hot (lowered), with the value chosen as that unit's new target. Turning a unit off or on keeps its `user_off` / on behaviour.
76. **Home Assistant is the single authority for the episode rule.** The vote automation `ac_vote_<room>`, and the manual-change automation when it treats a setpoint change as a vote, decide accept or ignore and fire `ac_person_vote` with `{room, direction, accepted, source: button|remote}`, plus `chosen` and `unit` for a remote, before acting on it. The runtime records a too_cold / too_hot reaction, with its sensor snapshot, only from an accepted event, logs an "ignored press" event from an ignored one, never judges the episode itself, and no longer records a remote setpoint change through its own detection window: one physical action, one reaction. Its own detection stays for on, off and manual. `runtime_check` shows a vote HA reports as ignored teaches nothing even when the runtime's cache shows no forced state (and fails against a runtime that judges from its cache); `control_logic_check` asserts every event's accepted flag is `not control.in_episode` over the room as it was.
77. **One correction per episode.** A vote is accepted only if no unit in the room is in a forced rest or boost: its `input_boolean.ac_forced_<room>_<unit>` marker on and its state `rest` or `boost` (a leftover marker on a unit that has moved on is no episode). While one is, a press of either button, or a remote's setpoint change, changes and teaches nothing. Once the forced state ends the next vote counts.
78. **A forced state releases by the normal rules** (back at comfort, the boost cap, minimum times) but returns to `cool` at its base, with no ±1 offset step. Every transition clears the marker; so do a section change, a leak, and a person's on, off or mode change.
79. **What a vote does to each unit** (the brief's table, plus the cases it left open): too cold rests a cooling or boosting unit (off) and keeps a system rest resting, both forced; too hot boosts a unit cooling, resting, in `user_off` or already boosting, forced. A unit already in the state the vote forces keeps its `since` (a system boost's cap and a system rest's minimum time still count from when it began), and a resting unit is sent nothing. Too cold on `user_off` sets base + 1 and offset 0 but leaves it off and unforced, so the person's off stands and a later press still counts. A unit with no base and no almanac is left alone, as are `manual`, `config_off` and `leak` (or its latch on); the reaction is still recorded. Every unit a vote moves gets offset 0 and no grace, its base clamped to its range.
80. **The remote's vote:** the unit changed gets the value chosen, the room's other units a step; a unit in `manual` or `leak` changed on its remote is left alone, but its change still votes for the others. Grace now comes only from a turn-on.
81. **While `ac_hold` is on,** a vote is still judged and reported, so it is learned, but HA writes no helper and sends nothing.
82. **The manual-change automation runs once per change.** Its triggers carry ids (`mode`, `temperature`, `fan_mode`), and a condition lets only the first that applies in that order go on. Replaying a vote twice would have stepped the other units twice; a change that moves the mode and the setpoint at once now gives one vote. Supersedes the double run of 64.
83. **Learning from a vote.** Comfort moves straight away: each usable sensor with reading r toward r + `system.control.person_step_degrees` (too cold) or r − it (too hot), at `reaction_rate`. S moves toward the value chosen on a remote, else toward the `setpoint_applied` in force ± one step, through `next_applied`, so the applied setpoint steps exactly one. Every unit with a seed moves, whatever its control state. Votes are setpoint-moving (they make the section learned and open a comfort window, which their forced state usually disqualifies) and reset the quiet count. Older `setpoint`, `knob_turn` and NULL rows keep their setpoint and comfort-window handling; the immediate comfort move is the votes' alone. The runtime replays the room the moment it records a vote, so HA's next heartbeat sees the moved comfort.
84. **The band is nudged by each reaction**, replacing the recency-weighted mean: with d the sensor's deviation from comfort just before the reaction, on the person's side and |d| < band it moves halfway (`reaction_rate`) toward |d|; on their side and |d| ≥ band, toward 0.8 × band; on the other side, toward band + |d|; clamped to [high, low] trust deviation, from the low one; trust follows. A sensor exactly at comfort, or a reaction with no direction (manual), leaves it. Every reaction with a direction nudges (votes, on, off, older setpoint rows); quiet periods never do. `learning.recency_half_life_days` is dropped: the schema refuses it and `migrate()` strips it from configs of either version. **The replay recomputes every band from the full history under this rule, so learned bands, Main Room's especially (three sensors and the most reactions), change on the first replay after the redeploy.**
85. **Storage migration 3** rebuilds `reactive` and `reactive_unit` so their kind checks allow `too_cold` and `too_hot` (`knob_turn` and `knob_press` stay valid for older rows). A button vote is recorded with no unit rows; a remote's with one row, `setpoint_before` NULL and `setpoint_after` the value chosen. Both are recorded at receipt time with window 0; two reactions in the same second still collide on `UNIQUE(room_id, ts)`, and the second vote is then logged, not recorded.
86. **The knob just presses the buttons**: a turn warmer presses too cold, cooler presses too hot, through an HA automation that isn't ours, so a turn of several detents is one vote. The container sends no commands of its own. What a knob press or an LED would do is an open question.
87. **`doctor.py --v2-preflight`** also creates a throwaway `ac_preflight_*` `input_button`, presses it and checks its state moves, and checks a custom event fired on HA's bus reaches the token's websocket subscription, which HA allows only for an administrator's token, as the runtime's `ac_person_vote` subscription needs.
88. **Rollback** (docs/V2_CUTOVER.md): v1's deploy removes the forced markers (`input_boolean`), but not the `input_button` helpers (a domain v1 never lists) nor the `ac_vote_*` automations (the restored v1 manifest predates them); the runbook deletes those by hand.

From phase 4, 29 Oct 2026:

89. **Forecast only.** Phase 4 reads `outdoor.source: forecast`. `probe` stays in the schema and in this design, but isn't read: the outdoor sensor has been moved. With `probe` the runtime records no outdoor value and logs one `outdoor` warning at startup. Nothing in control reads the outdoor values until phase 5, and no Home Assistant helper or automation changes (the full render with `source: forecast` is identical), so phase 4 needs a rebuild and restart, not a deploy.
90. **Fetching:** `weather.get_forecasts` (type hourly) for `outdoor.weather_entity`, through REST with `?return_response` (Home Assistant 2024.8 or later), which returns its `service_response`. Fetched at startup, then by an hourly loop whenever the cached forecast is an hour old, and cached with its fetch time. A crossover uses the cache if it is under 2 hours old, otherwise fetches first. One fetch runs at a time, and an attempt within 60 s of a failed one shares its failure, so rooms crossing together make one attempt between them.
91. **A crossover is never held up.** The scene fires first; a follow-up task then takes the forecast, records the run's `outdoor_mean` and `outdoor_max` and logs them as an `outdoor` event ("Main Room -> sunset: forecast mean 24.1, max 25.3 over 16:00-20:30 (weather.x)").
92. **The window** is [planned start, next section's planned start), from `app.scheduler.section_window`; sleep's crosses midnight. An hourly entry counts if its time falls in it, so a 05:30 start to 08:00 takes the 06:00 and 07:00 entries and leaves out 05:00. A window is partial when fewer entries fall in it than it holds on the forecast's hourly grid: the values use what is covered and the event says how many hours of how many. With none, the run keeps no values and the event is a warning. The mean is stored to two decimals.
93. **A catch-up keeps a run's values.** A restart inside a section run that already has its outdoor values keeps them: they were decided at its start, and the forecast no longer holds the hours already past. A run without them (the container was down when it started) records what is still forecast, which the event calls partial.
94. **Failures fail open.** A missing or unavailable weather entity (read from the state cache, so no call is made), a service error, a timeout (20 s overall, or the REST client's own 10 s) and an empty forecast all give no value, and the cache keeps the last good forecast. Each failure streak logs one `outdoor` warning naming the failure (`detail.failing` true, with its kind), and the forecast working again logs one info event. As with stale sensors, start() reads the last of these back, so a restart inside a streak doesn't repeat the warning. A run's own event when there is no forecast is info, not a second warning.
95. **Point values:** heartbeats, votes and reaction windows record the weather entity's `temperature` attribute as `outdoor_temp`. It is null if the entity is unavailable or unknown, if its `last_updated` is more than 3 hours old (exactly 3 is still fresh), or if the source isn't the forecast.
96. **CSV:** a run's outdoor values are a row of their own in the run's day file, carrying only `section`, `outdoor_mean` and `outdoor_max`; re-ingest merges it into the run as it does the close row. No storage migration: the columns exist from phase 0.
97. **Doctor step 7 runs with or without `--v2-preflight`** (which becomes step 8). It lists every `weather.*` entity with its current temperature and whether it offers an hourly forecast (`supported_features` bit 2), and shows the configured entity's next 6 hourly entries. A configured entity that is missing, has no hourly forecast or returns an empty one is a hard failure when `outdoor.source` is `forecast`, and a warning otherwise. `tools/doctor_check.py` runs the doctor against a stub Home Assistant. The Log view's category filter now lists v2's `control`, `block`, `outdoor` and `storage` categories. The status strip's warning is left to the Now view (phase 6).
98. **Forecast times are converted to Home Assistant's time zone before matching.** On the live install the forecast is `weather.forecast_home` (Met.no, hourly); `weather.hong_kong_observatory` has no hourly forecast, and the doctor shows it so. Met.no returns each entry's time in UTC (`2026-10-29T04:00:00+00:00` is 12:00 in Hong Kong), so `app.outdoor.parse_forecast` converts every entry to Home Assistant's time zone, the one section windows are planned in, and an entry falls in a window by its local time. The doctor shows the entries in that time zone too. In Hong Kong, UTC midnight is 08:00 local. `runtime_check` moves Day to 07:00, so Day (07:00 to 14:00, 23:00 to 06:00 UTC) crosses UTC midnight but not local midnight. Sleep (22:00 to 05:30, 14:00 to 21:30 UTC) crosses local midnight but not UTC midnight, and Sunrise's one hour, 06:00, falls on the previous UTC date. Every other hour is forecast at 40, so an entry matched by its UTC date or hour would show in the max.

From phase 5, 29 Oct 2026. Where the phase brief and this document differed, the document won (the per-sensor edges, a person turning a unit on teaching the margin):

99. **The conditions entity**, `sensor.ac_conditions_<room>`, is pushed by the container like the almanac: re-pushed every 5 minutes, on every websocket reconnect (Home Assistant restarting drops the socket, and the states we set with it) and on `homeassistant_start`. Its state is the section; its attributes are `cold_edge` and `warm_edge` (band widths), `blocked` (as control sees it: the decision, in active mode only), `would_block`, `block_mode`, `margin`, `comfort_point`, `outdoor_mean`, `outdoor_max`, `delta_mean`, `delta_max`, `lean`, `fail_open` (`no_outdoor` or `no_comfort_point`), `computed_at`, and `edges`, each sensor's trigger edges in degrees against the section's almanac (the per-sensor edges of [Where it runs](#where-it-runs), worked out again at each push from the locked band-width edges, since a vote can move a comfort reading mid-section). It isn't a helper: deploy neither creates nor prunes it.
100. **Read only while it is the section's.** One generator function, `_conditions_variables`, sets `conditions`, `blocked` and, for maintenance, `cold_edge` and `warm_edge`, in the scene, maintenance, leak release, vote and manual-change automations (decision 62's one place). The entity counts only while its state is that automation's section: the scene's own, the scene select's for the others. Missing, `unknown`, `unavailable` or another section's falls back to −1/+1 and not blocked. So the scene for a new section reads conditions published just before it fires, while maintenance, still in the old section, doesn't; and conditions left over from a section the container missed never block.
101. **Ordering.** A crossover publishes the section's conditions before the scene fires whenever they need no fetch: the cached forecast is under 2 hours old, the source isn't the forecast, or a restart keeps what the run already has. Otherwise it publishes fail-open conditions for the new section (`fail_open: no_outdoor`), fires the scene, and a follow-up fetches the forecast and publishes the real ones, which the next heartbeat's maintenance reads: a block then rests cooling units through maintenance, after the minimum time, rather than at the scene. This replaces decision 91's "the scene fires first" whenever the cache is fresh. One `block` event per room per crossover logs the decision, never the placeholder.
102. **Numbers.** The comfort point is kept to 2 decimals, Δ to 2, the lean (k × Δ of the mean) to 3, and the edges, from the rounded lean, to 3. The decision uses the rounded values, so a run's stored comfort point and max reproduce it, and margin learning's Δ of the max, exactly. The lean applies in every `block_mode`; `off` turns off only the block.
103. **Hysteresis reads the room's run before** (the latest by planned start, whatever its section) and its stored decision, shadow's "would block" included. A run with no decision, failed open, or decided in `block_mode` off (stored as 0) reads as not blocked.
104. **Locked for the section.** A restart inside a run that has its decision (a stored `block_mode`) re-publishes it unchanged, `block_mode` included, with the margin as it stood at the run's start (replayed as of then), and logs its block event "(kept from the run's start)". So a `block_mode` change in the config (Gate B's shadow → active) takes effect from each room's next section. A run with outdoor values but no decision yet (started before phase 5) decides at once from the values it kept.
105. **Failing open** stores the run's comfort point (if any), `lean` NULL, `blocked` 0 and the `block_mode`. `outdoor.source` none, or probe, logs one `outdoor` warning per streak, like a failing forecast (start() reads it back, so a restart doesn't repeat it); it replaces probe's warning at every start.
106. **Heat sources** are read live by the same templates: any listed entity `on` blocks in shadow and active modes. The `block_mode` comes from the conditions entity, or the config's (rendered in at deploy) while there is none, so a heater blocks with the container down, and going active needs no deploy. Deploy refuses a heat source Home Assistant doesn't have. A heat source isn't recorded and teaches no margin.
107. **A too hot vote while blocked unblocks** (`control.unblock_for_vote`): each unit it moves into its forced boost gets `ac_unblocked` on when the room is blocked as control sees it (an active block or a heat source), as a person's turn-on does, so the block doesn't rest the boost at the next heartbeat. Not in shadow mode, where control sees nothing blocked. A too cold vote leaves the block alone.
108. **Margin learning** (`app.analyser.learn_margin`, part of every replay, from `outdoor.initial_margin`, at `reaction_rate`, within 2 to 10): the document's turned on while blocked and turned off just after cooling started, plus the votes. A too hot vote or a person's `on` in a run decided blocked (shadow included) moves M toward Δ of the max + 0.5; a too cold vote or a person's `off` within 30 minutes after the system started cooling, in a run decided unblocked, toward Δ of the max. Runs decided in `block_mode` off, or with no comfort point or forecast max, teach nothing. A reaction belongs to the room's latest run that started (by plan) at or before it.
109. **When the system started cooling** is read from the heartbeats' `control_state`, so the margin rebuilds from CSV with no new column. It is the run's planned start (the scene) if every heartbeat of the unit since showed `cool` or `boost`; or, after a heartbeat that showed `rest`, that heartbeat's time: maintenance woke the unit after it, and the earliest the wake can have been means a reaction counted is always within 30 minutes of it, while one up to a heartbeat interval later than that is missed. A person's `off` while the last heartbeat still showed `rest` dates the wake by that heartbeat; with no heartbeat yet in the run, it is the scene, unless the unit was off. A too cold vote checks every unit the section's scene doesn't force off; an `off`, the units turned off. Cooling a person started (from `user_off`), or a leak release, doesn't count.
110. **The margin is replay output, not stored:** the runtime keeps the latest replay's (startup replays every room before any crossover), pushes it as the almanac entity's `margin` attribute, and `/api/almanac/<room>` serves it. A reaction that moves it logs a `block` event ("block margin 6.0 -> 6.7: a too_hot vote in Night 2026-08-03, which would have been blocked (shadow) (delta max 6.9, so toward 7.4)"). No storage migration: the section run's columns exist from phase 0.
111. **Phase 5 needs a deploy**, since the scene, maintenance, leak release, vote and manual-change templates change (they read the conditions entity and the heat sources). The helpers are unchanged. Deployed before the container is updated, the new templates find no conditions entity and fail open, as phase 4 behaved.

The margin's floor, 29 Oct 2026 (supersedes "within 2 to 10" in 108, and the 6 °C start of The cooling block):

112. **The margin's floor is 0.** `control.MARGIN_MIN` is 0, and `outdoor.initial_margin` defaults to 0 (minimum 0, maximum still 10). This climate rarely sees a section's warmest forecast hour 2 °C or more below the comfort point, so a floor of 2 meant the block would almost never fire. At M = 0 a section is blocked when its warmest forecast hour is at or below the room comfort point, which is the intended rule. The hysteresis stays at `BLOCK_RELEASE` = 1: a room blocked last section stays blocked until the max is a degree or more above the comfort point. The replay still learns within 0 to 10, and a step toward a target below 0 (an unblocked section whose max was above the comfort point) stops at 0 rather than going negative (`analyser_demo`: learned down from 1 by a too_cold vote at Δ of the max −3, it stops at 0, and a too_hot vote in a section blocked at 0.4 takes it back up to 0.45). The margin is replayed from `initial_margin` every time, so a changed starting value re-bases the whole learned margin, while past section runs keep the decisions they stored. The loader fills only missing keys, so a saved config that already holds `initial_margin` 6 keeps it until it is edited. No template changes: the templates read the block from the conditions entity, never the margin (render diff identical).

A negative margin, 29 Oct 2026 (supersedes the floor and default of 112; its hysteresis, re-basing and no-template-change notes stand):

113. **The margin may be negative, down to −3, and starts at −1.** `control.MARGIN_MIN` is −3, and `outdoor.initial_margin` defaults to −1 (minimum −3, maximum 10); the example config holds −1. The rule is unchanged (blocked when Δ of the max ≥ M, and after a blocked section while Δ of the max > M − 1), so at −1 a room stops actively cooling a section whose warmest forecast hour is at or below its comfort point + 1 °C, and once blocked stays so until the max reaches comfort + 2 °C (`BLOCK_RELEASE` stays 1). The learning targets (Δ of the max, Δ of the max + 0.5) are negative whenever the max was above the comfort point, and the margin now follows them below 0, stopping at −3 (blocking at a max up to comfort + 3). Nothing assumed a non-negative margin except one display detail: a learned margin that rounds to zero from below (−0.004, say) showed as "-0.0" in the log and was published as −0.0; it is now 0.0 (`runtime._round2`). Checked: `control_check` (at M = −1, Δ of the max −1.0 blocks and −1.1 doesn't; after a block, −1.9 holds and −2.0 lifts), `analyser_demo` (the clamp at −3 and 10; from −1 a too hot vote in a section blocked at Δ 2 takes it to 0.75, then a too cold vote at Δ −2 back to −0.625; two too cold votes at Δ −5 stop it at −3), `config_check` (−3 accepted, −3.5 rejected), `runtime_check` (the log and the entities at −1). A saved config keeps whatever `initial_margin` it holds until it is edited; the replay then re-bases the learned margin on the new start. No template changes (render diff identical).

From phase 6, 29 Oct 2026. Where the phase brief and this document differed, the document won (the state labels "Off (by you)" and "Off (scheduled)"; the correction scale's centre number and chevrons):

114. **One poll, computed where the rules live.** `GET /api/now` returns everything the Now view draws, per room: the section, each unit's control helpers and what it is commanded to, the almanac section, each sensor's reading, `last_reported`, staleness and place in its band, the conditions entity, the block, the heat sources, the quorum, leak latches, holds, the margin and the latest vote; and the status strip's warnings. `app/nowview.py` builds it, pure, through `app.control`, reading Home Assistant's states the way the generated templates do: the conditions entity counts only while its state is the section in force (else edges −1/+1 and no block), a sensor past `sensor_stale_minutes` since its (or its unit's) `last_reported` is stale (`control.is_stale`), positions and the quorum are `control.evaluate_votes` against the edges in use, the block is `control.room_blocked` with the heat sources, and the commanded temperature is `control.command_for`. `now.js` only draws. The view polls every 15 s while its tab is open; with Home Assistant out of reach, or no credentials, the endpoint still answers 200, with that as the warning.
115. **The gauge.** A 270° sweep from 12 o'clock, fixed thirds of 90° (blue −135° to −45°, green −45° to +45°, amber +45° to +135°), the bottom 90° the label strip. A sensor at position p (band widths) sits at 45° × p / warm_edge above comfort and 45° × p / |cold_edge| below it, so comfort is at the top and the leaned edges land on ±45°; past an edge, two band widths take it to ±135°, where it pins with a double-chevron tick. Markers within 15° of each other step out to a second or third orbit. A sensor with no position (stale, unavailable, or no comfort reading yet) is a hollow numbered marker under the strip. Numbers are the table's rows: warmest first by place in band, no position last.
116. **The label strip** keeps this document's wording, including Off (by you) and Off (scheduled) where the brief said "Off · by you" and "Off · by config", with a second line: "you said too cold" / "you said too hot" for a forced rest or boost (the brief's additions), a manual unit's mode and fan, "you turned it on" in a turn-on's grace, "paused (hold)". A unit with no state helper yet reads "Not controlled"; one not reporting, "Unavailable".
117. **The correction scale** keeps this document's centre number (the applied setpoint) and chevrons (the section offset, up to three), where the brief had the commanded temperature in the centre and chevrons for its distance from the applied setpoint. The commanded temperature (base + offset, minus `boost_degrees` in boost: what the brief asked for) is shown under the scale as "driving N°", so a vote's moved base and a boost still show. The magenta marker is the forecast mean minus the room comfort point on a ±5 °C scale, warmer up, labelled with the mean and max, and turns into an arrow past either end; it is drawn only while the conditions entity is live.
118. **The lock** shows the block as control sees it: an active decision ("blocked") or a heat source that is on ("heater on"), with the amber arc greyed; shadow mode's `would_block` is a dashed lock, "would block", with the arc left amber.
119. **The vote buttons** call `POST /api/rooms/{room}/vote` `{direction}`, which presses `input_button.ac_<direction>_<room>` through Home Assistant's REST API, as a dashboard would: Home Assistant's vote automation still judges the vote, and the container still learns only from `ac_person_vote`. The runtime keeps the latest `ac_person_vote` per room, numbered (`Runtime.last_votes`); the endpoint waits up to 4 s for the press's own report and returns it, and the card says "Got it, resting", "Got it, cooling harder", "Already correcting, ignored", or "Sent" when none came. An unknown or disabled room is a 404, an unknown direction a 400, and nothing is pressed.
120. **Leak fixed** calls `POST /api/rooms/{room}/units/{unit}/leak-fixed`, which turns on `input_boolean.ac_leak_confirmed_<room>_<unit>`; Home Assistant's release automation still refuses while the leak sensor reads wet. The button shows only while the latch is on, and asks "Is the leak fixed and dry?" before posting. A unit without leak detection is a 400. Both endpoints touch only our own `ac_` helpers, so phase 6 needs a rebuild and restart, not a deploy.
121. **The status strip** sits at the top of the Now view: a config that doesn't validate, an outdoor failure streak (the latest `outdoor` streak event), a latched leak, a unit not reporting, a hold left on, a room's conditions failing open (none published, unknown, another section's, no outdoor value, no comfort point) and stale or unavailable sensors; "All clear" otherwise. The header keeps its connection status.
122. **Look.** Light and dark follow the device (`prefers-color-scheme`), through CSS tokens; the other tabs' hard-coded dark colours became tokens too. No colour stands alone: the ring's zones are labelled, the table's zone column has glyphs, locks and states have words, and the SVGs carry aria labels. On a phone the donut and the scale sit side by side above the table, the vote buttons share the width, and nothing scrolls sideways. The fan turns once in 3 s on low and 0.8 s on medium, and not at all with `prefers-reduced-motion`.
123. **Checks.** `tools/now_api_check.py` drives the endpoints through FastAPI's TestClient against a stubbed Home Assistant: `/api/now`'s keys one by one, its values for a boosting room, warnings, Home Assistant out of reach and no credentials, the vote and leak-fixed endpoints (the right button, refusals, the outcome coming back). `tools/now_render_check.py` builds 20 cases' payloads through the real `app.nowview` and `tools/now_render.js` renders each from the real `app/static` in headless Chromium (the environment's Node Playwright; a Python one isn't installed), asserting the label strip, the lock, the fan, the markers (placed, hollow, pinned, the leaned edges at ±45°), the table's order and notes, the buttons' flows, the fan turning about its own centre and Now → Log → Now, and saving each screenshot to `docs/screenshots/`.

Phase 6 follow-ups, 29 Oct 2026 (the sketch is the authority over this document for the correction scale):

124. **The correction scale follows the sketch** (supersedes 117, and this document's earlier "centre number: the applied setpoint; chevrons: the section offset"): the sketch's "Current AC setting" arrow points at the centre number and "Correction" at the chevrons. The centre is what the unit is set to now: our command's target in cool or boost (base + offset, minus `boost_degrees` in boost), the person's own setpoint in manual, "Off" in rest, user_off, config_off and leak (or a manual unit switched off), "—" with no state helper or not reporting. The chevrons are the difference from the learned `setpoint_applied`: one per degree (rounded, half away from zero) up to three, up for warmer and down for cooler, stacked out from the centre box, with the exact difference beside them (so "−4°" when there are more than three); none while off. The learned value moves to a small line under the scale, "learned 23°". The payload's `driving` became `set_to`, with `off` and `correction` (`app.nowview`). A forced too-hot boost at base 22 used to show 23° large while set to 20°; it now shows 20°, three chevrons down and "−3°".
125. **The vote reply is Home Assistant's report of that press.** The endpoint now waits for the room's next `ac_person_vote` in the direction it pressed (a report for the other button meanwhile is someone else's, and is not taken for this one), and the card says "Got it, cooling harder" for an accepted too hot, "Got it, resting" for an accepted too cold, "Already correcting, ignored" when ignored, and "Sent" when no report came. The 67f71c5 screenshot pairing "Boost · Medium / you said too hot" with "Got it, resting" was the fixture, not the mapping: that case pressed Too hot (stubbed as ignored) then Too cold (stubbed as accepted) and was photographed after both, against a stubbed `/api/now` that never moved. `now_render_check` now presses the buttons at desktop width against a stub that moves to the state each accepted vote leads to, and asserts every reply, with the direction pressed and the card after it (`vote-too-hot`: accepted, then Boost · Medium / you said too hot at 20°; `vote-too-cold`: accepted, then Resting / you said too cold, Off; an ignored too hot; no report, "Sent"), and checks the reply mapping on its own.
126. **Desktop only** (supersedes the phone parts of 122 and 123): there is no phone use case. The phone render cases and their screenshots are gone, and so is the phone layout (the narrow-width media query). What is harmless stays: the header wraps, and the buttons are 44 px tall. Light and dark mode stay. The render check now also clears `docs/screenshots/now-*.png` before it runs, so a case removed takes its screenshot with it.

Section start, 29 Oct 2026:

127. **A cold room with a cooler forecast starts the section resting** (extends 45, the section-change part of Control, and 101's ordering). Before this, every unblocked section started in `cool`, so in cool weather each crossover switched units on and maintenance rested them again a heartbeat or two later. Now `control.section_start` starts a unit in `rest` when the room starts cold: `cold_side >= max(required, 1)` over its usable sensors against the new section's almanac (a rest started now wouldn't release at once; below the comfort reading is enough, no vote past an edge is needed), and the section's Δ of the mean > 0 (the forecast mean below the room comfort point). The order is leak, the scene's forced off, no almanac, blocked, then this, then `cool`: a blocked room still rests for the block, unmarked, and wakes at +1 as before once a heat source goes off. No conditions for the section, no outdoor value or no comfort point (Δ of the mean unknown) fails open to `cool`, as does a scene that fires on fail-open conditions while the forecast is fetched (101); the block mode plays no part. Such a rest is marked by a new per-unit helper, `input_boolean.ac_section_rest_<room>_<unit>`, so `release_offset` wakes it at the base, offset 0 rather than +1, since nobody said too cold. Every transition clears it, as do the next section change, a vote (it is no episode, so a too hot or too cold vote counts and forces a state as usual) and a person's on, off or mode change. A confirmed leak release, a full section change (45), follows the same rule, in the leak release automation and in maintenance. The scene now counts the room's votes, from the same `_votes_variables` as maintenance and the leak release, against its own section's almanac and the conditions entity's edges, and reads Δ of the mean from it (`_conditions_variables` gained `delta_mean`, none unless live). The runtime's `crossover` event records the start and why (`start`, `why`, `cold_side`, `needed`, `delta_mean` in its detail), from `control.section_start` over the readings it fetches after firing the scene, the almanac and the conditions it published; the scene decides again in Home Assistant from the same values. `tools/control_check.py` adds eleven scenarios (cold room and cooler forecast, a quorum just under comfort, a forecast at or above comfort, a warm room, fail open three ways, blocked, a heat source's rest, too hot and too cold votes during a section rest, a person's on, a leak release) and ten rules removed in turn; `tools/control_logic_check.py` replays them through the generated scene, maintenance and leak release. It needs a deploy: the new helper, and every automation that reads or writes a unit's helpers.

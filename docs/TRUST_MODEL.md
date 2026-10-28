# The sensor trust model

This is the one part of Adaptive Climate with no analogue in Adaptive Light, and
it is the reason the project exists. Read this before the analyser or the schema,
because both derive from it. On branch `v2` it describes the reaction-driven
model of `docs/V2_DESIGN.md` (Learning model), which `app/analyser.py`
implements; where the two differ, V2_DESIGN.md wins.

## The premise

An air conditioner's own setpoint, and any single temperature probe, are
unreliable descriptions of comfort. A unit reporting "21°C" tells you what it is
*trying* to do, not what the room *feels* like, and a wall sensor reading "25°C"
is only meaningful relative to the point at which *you* stop being comfortable.

So the system learns two things that are deliberately kept separate:

1. **The setpoint** — the number the thermostat should sit on in a section.
   This is the value stored per unit in the almanac and driven to the AC.
2. **The comfort reading, per sensor** — the temperature each sensor *shows*
   while you are content. These need not agree with each other or with the
   setpoint. One sensor by the window reads 25°C; one in the hallway reads
   24°C; the AC says 21°C. All three are "comfortable" as long as you do not
   touch anything.

The setpoint is what we *act on*. The per-sensor comfort readings are what we
*watch* to decide when to act.

## Only a person moves a target

v1 learned the setpoint as a rolling average of what the unit sat at, so it
learned mostly from its own output and a correction barely moved it. v2 flips
that: **targets move only when a person acts.** A quiet section adds confidence
and moves nothing, so the system can never learn from itself.

The almanac is a deterministic **replay** of every reaction, in time order,
from a **seed**:

- **Seed.** Per unit, the setpoint at the earliest heartbeat in that room and
  section with the unit on. Per sensor, its earliest reading in that section.
  The seed is the replay's starting state. A unit or sensor with no seed has no
  almanac entry, and the generated automations leave it alone.
- **Setpoint.** Each reaction that changed a unit's setpoint moves it partway
  toward what the person chose (`learning.reaction_rate`, default 0.5):

  ```
  S = S + rate × (chosen − S)
  ```

  `chosen` is the raw value the person set, whatever offset or boost was active.
  A **vote** (too cold or too hot, from a room's button or a setpoint change on a
  remote) moves every unit that has a seed: `chosen` is the value set on the
  remote for the unit changed there, and for every other unit (and for a button)
  the `setpoint_applied` in force one step warmer (too cold) or cooler (too hot).
- **Applied setpoint.** The unit takes whole degrees, so `setpoint_applied` is
  a whole degree tracked through the replay, reaction by reaction:
  - normally the whole step **nearest `S`**, an exact half going toward the
    latest reaction's direction (half up before any reaction);
  - after a setpoint-moving reaction whose chosen value differs from the
    `setpoint_applied` in force at that moment, the new value moves **at least
    one step** from that value toward the chosen one, and **never past it**;
  - a chosen value equal to the applied value keeps it;
  - nothing else moves it, so quiet days never change it.

  A correction from 22 to 23 stores 22.5 and applies 23, so it shows the next
  day; plain rounding (Python's and Jinja's, which round halves to even) would
  send 22.5 back to 22. Earlier v2 rounded toward the latest reaction's
  direction instead, which over-applied: an `S` of 24.17 after a correction up
  to 25 applied 25. With 23 in force when that correction was made, it now
  applies 24, the nearest step and still a step up.
- **Comfort readings** move only on what a person says. A **vote** moves them
  straight away: each sensor reading r at the vote moves its comfort at the same
  rate toward r + `person_step_degrees` (too cold) or r − it (too hot), default
  1°: you felt cold at r, so this sensor's comfort sits a step warmer. Then the
  mean reading 30 to 60 minutes after a setpoint-moving reaction, if nobody
  reacted in that window, no unit was boosting, resting or in leak mode (a
  vote's own forced rest or boost included), and it holds at least two
  heartbeats. Comfort moves toward that mean at the same rate. The window
  belongs to the reaction's section even if it runs into the next one.

A **setpoint-moving reaction** is a vote, a setpoint change or knob turn
(recorded before the votes), or a reaction of any kind in which a unit's
setpoint changed (an `on` with a new setpoint). A section is **provisional**
until its first one, then **learned**.

**Kinds of reaction.** The person is a sensor that always wins: a **vote**,
`too_cold` or `too_hot`, is the main one (docs/V2_DESIGN.md, Person reactions).
Home Assistant judges it (one per episode: a press during a forced rest or boost
is ignored and teaches nothing) and reports it; the container learns only from
the votes HA reports as accepted. A person turning a unit **on** or **off**, or
changing its mode or fan while it stays on (**manual**), is a reaction too. On
says *too warm* and off *too cold*: both teach bands (below), but neither moves
the setpoint unless the person changed the setpoint too. Manual teaches nothing;
it is logged. Every kind, manual included, resets a section's quiet count and
disqualifies a comfort window it falls inside. A unit dropping offline and
coming back is never a reaction.

## Trust and the comfort band

A sensor's **trust** is inferred from how small a deviation makes you react. If
you reach for the thermostat the moment a sensor slips half a degree, that sensor
is a faithful proxy for your comfort and deserves a tight band. If a sensor has
to swing five degrees before you care, it is noisy or badly placed, and needs a
wide band before we listen to it.

The calibration endpoints, from testing:

| | Reaction deviation | Comfort band (half-width) | Trust |
|---|---|---|---|
| High trust | 0.5°C | ±0.5°C | 1.0 |
| Low trust | 5.0°C | ±5.0°C | 0.0 |

Let `D_HIGH = 0.5` and `D_LOW = 5.0` (both configurable under
`system.trust` in the config).

**Band half-width.** Every sensor starts at `D_LOW`, and **each reaction nudges
each sensor's band**; quiet periods never move it. When you react, the system
snapshots every sensor. For each, `d = reading − comfort`, against its comfort
reading **as it stood in the replay just before the reaction**, not today's
value. A reaction that wants it **warmer** (too cold, a unit turned off, a
setpoint raised) is on the side of sensors **below** their comfort; one that
wants it **cooler** (too hot, a unit turned on, a setpoint lowered), of those
**above**. Then, at `reaction_rate` (halfway by default):

```
same side, |d| <  band   ->  band moves toward |d|          (it should have spoken: tighten)
same side, |d| >= band   ->  band moves toward 0.8 x band   (it did speak: tighten a little)
other side               ->  band moves toward band + |d|   (it said the opposite: widen)
d = 0                    ->  no change                      (it says nothing either way)
band = clamp(band, D_HIGH, D_LOW)
```

A manual change teaches no band. The band itself stays symmetric. Because the
band is the path of these nudges, an old reaction counts as much as a new one,
until later reactions move it on; there is no recency weighting.

**Trust** is the normalised inverse of the band, purely for display and
tie-breaking (the quorum itself is vote-count based, below):

```
trust = (D_LOW - band) / (D_LOW - D_HIGH)      # band 0.5 -> 1.0 ; band 5.0 -> 0.0
```

**Confidence** tells the UI how settled a section is; it never drives control:

```
confidence = 1 − 0.5^(quiet / 3)
```

where *quiet* counts the section's completed runs since its last reaction that
had at least one heartbeat and no reaction. Any reaction resets it to 0.

## A worked example

`tools/analyser_demo.py` asserts this one.

1. For eight days the lounge unit sits at **21°C** in the day section and
   sensor A reads a steady **25°C**. The seed is setpoint **21**, comfort
   **25**; the section is provisional and nothing has moved, however many
   heartbeats arrive.
2. Sensor A drifts down to **24°C**. You react: *"too cold now"*, and set
   21 → **22°C**.
3. Two lessons land at once:
   - **The setpoint was wrong.** `S = 21 + 0.5 × (22 − 21) = 21.5`. You chose
     22 while 21 was applied, so the applied setpoint moves a step toward it,
     to **22°C** (21.5 is a tie, and the tie goes your way too). The section
     is now learned.
   - **Sensor A is trustworthy.** You wanted it warmer and A was `1.0°C` below
     its comfort, inside its ±5° band, so the band moves halfway toward 1.0, to
     **±3.0°C**, and its trust rises to **0.44**. A second sensor sitting at its
     comfort learns nothing. A second reaction like it would take A to ±2.0°C.
4. Half an hour to an hour later, if you left the room alone, sensor A's
   readings over that window pull its comfort halfway toward them.
5. From the next day's run, the unit drives **22°C**, and the moment sensor A
   reads below about 22°C (comfort − band) it votes.

The same moment through the room's **too cold** button instead: the unit rests
at once (forced), the applied setpoint steps a degree to 22, and A's comfort
moves straight away halfway toward 24 + 1 = 25, so it stays at 25; its band
tightens as above.

v1, given the same data, learned 21.02 and applied 21: the correction vanished.
`tools/compare_analyser.py` shows that difference on any data set.

## Voting and quorum

At each heartbeat, every sensor in the room casts a vote if its reading has left
its comfort band:

```
votes(sensor) = 1  if  |reading - comfort| >= band   else 0
```

A sensor that is unavailable, has nothing learned yet, or that Home Assistant
last reported longer ago than `system.sensor_stale_minutes` (default 90: a
sensor put away can keep its last value for days without going unavailable)
casts no vote and isn't usable. The number of votes required (the **quorum**)
depends on how many usable sensors the room has:

```
n == 1        -> 1 vote
n == 2        -> 2 votes
n >= 3        -> ceil(n / 2)        # "50% or more"
```

Votes on both sides count toward one pooled tally. When the quorum is met, the
trust-weighted majority of the voters' sides picks the direction; an exact tie
means no action. What happens next is the control state machine in
`docs/V2_DESIGN.md` (Control: cool or rest); until phase 3 lands, the v1
maintenance automation still corrects by ±2° for at most
`system.control.boost_max_minutes`. Phase 5 leans the vote edges with the
outdoor temperature; the quorum count is unchanged.

## How this shapes the almanac

Per section, two long-format tables (see `docs/ALMANAC_FORMAT.md`):

- **per unit** — the learned `setpoint` (precise), the `setpoint_applied` that
  is driven, and an `off` flag.
- **per sensor** — `comfort`, `band`, `trust`.

Plus the section's `status` (`provisional` or `learned`) and `confidence`. The
maintenance quorum loop runs in Home Assistant templates, so the almanac carries
everything they need.

## Virtual sensors

A sensor's reading can come from a `source: {type: unit_attribute, unit_id, attribute}` in config instead of a standalone `sensor.*` entity - typically the AC's own `current_temperature`. The trust model treats it identically to a physical sensor; only where the number is read from changes. Worth knowing about it in practice: a unit-internal sensor isn't independent of the AC's *behavior*. When the unit is idle/steady it's a fair proxy for the room; while the unit is boosting or correcting, its reading partly responds to that rather than to the room. That correlation isn't hidden anywhere - it'll show up as slower trust convergence for that sensor, since you'll react to it less specifically. Documented rather than special-cased, because the model handles it correctly on its own.

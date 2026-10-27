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
- **Applied setpoint.** The unit takes whole degrees, so the stored `S` is
  rounded to `setpoint_applied` **toward the direction of the latest
  setpoint-moving reaction**: up after a warmer correction, down after a cooler
  one. A correction from 22 to 23 stores 22.5 and applies 23, so it shows the
  next day; plain rounding (Python's and Jinja's, which round halves to even)
  would send 22.5 back to 22. The direction is how `S` moved, not the gesture;
  it stands while `S` doesn't move; before any reaction it rounds half up.
- **Comfort readings** move only on evidence that you accepted the room: the
  mean reading 30 to 60 minutes after a setpoint-moving reaction, if nobody
  reacted in that window, no unit was boosting, resting or in leak mode, and it
  holds at least two heartbeats. Comfort moves toward that mean at the same
  rate. The window belongs to the reaction's section even if it runs into the
  next one.

A **setpoint-moving reaction** is a setpoint change or a knob turn. A section is
**provisional** until its first one, then **learned**.

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

**Band half-width** for a sensor is the recency-weighted mean of the deviations
at which you reacted, clamped to `[D_HIGH, D_LOW]`:

```
band = clamp( weighted_mean(reaction_deviations), D_HIGH, D_LOW )
weight = 0.5 ^ (age_in_days / recency_half_life_days)      # over all history
```

The deviation is measured against the sensor's comfort reading **as it stood in
the replay at the moment of the reaction**, not today's value.

**Attribution — which sensor a reaction teaches.** When you react, the system
snapshots *every* sensor, but a reaction only teaches the sensors that had
drifted, and drifted **the way the reaction says**:

- A reaction that wants it **warmer** (setpoint raised, unit turned off, knob
  warmer) teaches only sensors that were at least `D_HIGH` **below** their
  comfort reading.
- A reaction that wants it **cooler** (setpoint lowered, unit turned on, knob
  cooler) teaches only sensors at least `D_HIGH` **above** it.

A sensor sitting at its comfort, or off on the other side, is evidence of
nothing about that sensor, so folding its deviation in would wrongly make it
look trusted. A sensor with no qualifying reactions keeps the widest band (low
trust). The band itself stays symmetric.

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
   - **The setpoint was wrong.** `S = 21 + 0.5 × (22 − 21) = 21.5`, rounded up
     (you asked for warmer) to an applied **22°C**. The section is now learned.
   - **Sensor A is trustworthy.** You wanted it warmer and A was `1.0°C` below
     its comfort, so its band tightens to **±1.0°C** and its trust rises to
     **0.89**. A second sensor sitting at its comfort learns nothing.
4. Half an hour to an hour later, if you left the room alone, sensor A's
   readings over that window pull its comfort halfway toward them.
5. From the next day's run, the unit drives **22°C**, and the moment sensor A
   reads below about 24°C (comfort − band) it votes.

v1, given the same data, learned 21.02 and applied 21: the correction vanished.
`tools/compare_analyser.py` shows that difference on any data set.

## Voting and quorum

At each heartbeat, every sensor in the room casts a vote if its reading has left
its comfort band:

```
votes(sensor) = 1  if  |reading - comfort| >= band   else 0
```

The number of votes required (the **quorum**) depends on how many usable
sensors the room has:

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

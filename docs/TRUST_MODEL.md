# The sensor trust model

This is the one part of Adaptive Climate with no analogue in Adaptive Light, and
it is the reason the project exists. Everything else is a rename of the Light
machinery; this is genuinely new. Read this before the analyser or the schema,
because both derive from it.

## The premise

An air conditioner's own setpoint, and any single temperature probe, are
unreliable descriptions of comfort. A unit reporting "21°C" tells you what it is
*trying* to do, not what the room *feels* like, and a wall sensor reading "25°C"
is only meaningful relative to the point at which *you* stop being comfortable.

So the system learns two things that are deliberately kept separate:

1. **The setpoint** — the number the thermostat sits on when you are content.
   This is the value stored per unit in the almanac and pushed to the AC. It is
   learned exactly the way Adaptive Light learns brightness: a recency-weighted
   rolling average of what the unit sat at during settled, non-reactive periods
   in that section.

2. **The comfort reading, per sensor** — the temperature each sensor *shows*
   while you are content at that setpoint. These need not agree with each other
   or with the setpoint. One sensor by the window reads 25°C; one in the hallway
   reads 24°C; the AC says 21°C. All three are "comfortable" as long as you do
   not touch anything.

The setpoint is what we *act on*. The per-sensor comfort readings are what we
*watch* to decide when to act.

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

**Band half-width** for a sensor is the recency-weighted average of the
deviations at which the user has reacted on that sensor, clamped to
`[D_HIGH, D_LOW]`:

```
band = clamp( weighted_mean(reaction_deviations), D_HIGH, D_LOW )
```

**Attribution — which sensor a reaction teaches.** When you react, the system
snapshots *every* sensor, but a reaction only tells us about the sensors that
had actually *drifted*. A sensor sitting at its comfort reading when you reacted
is evidence of nothing about that sensor (if anything, that it failed to predict
your discomfort), so its ~0 deviation must not be folded in — doing so would
wrongly make an unmoved sensor look maximally trusted. So a reaction updates a
sensor's band only when that sensor's deviation at reaction time is at least
`D_HIGH`; sensors at or near comfort are skipped for that reaction. A sensor
with no qualifying reactions keeps the widest band (low trust), exactly like one
with no reactions at all. In the worked example below, sensor A drifted 1.0 and
is taught; a second sensor sitting at its comfort would learn nothing from the
same reaction.

**Trust** is the normalised inverse of the band, purely for display and
tie-breaking (the quorum itself is vote-count based, below):

```
trust = (D_LOW - band) / (D_LOW - D_HIGH)      # band 0.5 -> 1.0 ; band 5.0 -> 0.0
```

A sensor with no reactions yet starts at low trust (wide band): we assume nothing
until you teach us. Its comfort reading is still seeded from observation so it
can display and participate, but its wide band means it rarely votes on its own.

## A worked example

Straight from the design conversation:

1. The room has settled. The almanac has learned **setpoint 21°C** for this unit.
   Sensor A has learned a **comfort reading of 25°C** with a wide band (say
   ±4°C, low trust) because it has never triggered a reaction.
2. Sensor A drifts down to **24°C**. The AC does not change. You react: *"too
   cold now."*
3. Two lessons land at once:
   - **The setpoint was wrong.** Correction nudges it up: 21 → **22°C** (capped
     at the 2°C maintenance step).
   - **Sensor A is trustworthy.** You reacted at a deviation of
     `|25 − 24| = 1.0°C`. Its band tightens toward **±1.0°C** and its trust
     rises. Its comfort reading stays at **25°C** — 25 is still where you are
     happy; the *drift below it* was the problem.
4. The almanac now records: unit setpoint **22°C**; sensor A comfort **25°C**,
   band **±1.0°C**, trust up. From now on, the moment sensor A reads below ~24°C
   (comfort − band) it votes — the system *knows* to correct, because it has
   learned that this sensor drifting even a little predicts your discomfort.

The setpoint moved; the sensor's comfort point did not. That separation is the
whole point.

## Voting and quorum

At each heartbeat, every sensor in the room casts a vote if its reading has left
its comfort band:

```
votes(sensor) = 1  if  |reading - comfort| >= band   else 0
```

The number of votes required (the **quorum**) depends on how many sensors the
room has:

```
n == 1        -> 1 vote
n == 2        -> 2 votes
n >= 3        -> ceil(n / 2)        # "50% or more"
```

(`ceil(n/2)` makes the 50% rule unambiguous for both even and odd sensor counts:
4 sensors need 2, 5 sensors need 3.)

**Corrective action starts** at the first heartbeat where `tally >= quorum`.
The *direction* of the correction is the sign of the breaching sensors' drift:
if the high-trust majority is reading below comfort, warm up (raise setpoint);
if above, cool down (lower setpoint). Each correction step is capped at the
2°C maintenance limit and only touches units that are already on.

**Corrective action stops** at the first heartbeat where `tally < quorum`, or
after `system.correction_max_minutes` (default 60) — whichever comes first. The
one-hour cap is a safety valve against a stuck sensor driving the AC forever.

## How this changes the almanac shape

Adaptive Light stored one `lux_target` plus a per-group brightness. Adaptive
Climate splits the almanac into two long-format tables per section:

- **per unit** — the learned `setpoint` (and an `off` flag for forced-off).
- **per sensor** — `comfort`, `band`, `trust`.

The state machine in the generator reads the unit setpoint; the maintenance /
quorum logic reads the per-sensor comfort and band. See `docs/DESIGN.md` for
where each of these runs (Home Assistant vs. the container) — that split is the
one open implementation decision still to settle before the generator is written.

## Open question carried forward

The quorum evaluation, the "stops below quorum or after an hour" latch, and the
direction-of-correction logic are more stateful than Light's simple
threshold-nudge. Adaptive Light kept maintenance *inside Home Assistant* so it
survives container downtime. We need to decide whether the quorum loop stays in
HA (richer generated templates, resilient to container loss) or moves into the
container (simpler templates, but correction pauses if the container is down).
The current lean is **HA-owns-maintenance**, preserving the Light principle,
with the almanac carrying everything the template needs. Flagged in
`docs/ROADMAP.md` as the first decision of Phase 4.

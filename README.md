# Adaptive Climate

Adaptive Climate targets a **comfort level**, not a temperature value. The basic
premise is that air-conditioner settings, thermostat targets and temperature
probes are untrustworthy sources of information on their own, and the only
source of real truth is how the user feels. It learns which climate settings
correspond to which sensor readings, and what you actually prefer in each part
of the day, and drives your climate controller toward it — so you stop reaching
for the thermostat.

It is a back-end system, meant to run largely unseen, with a web UI for
configuration and insight. It is not a replacement for Home Assistant's own
thermostat controls; your switches keep working exactly as before.

> **Status: planning.** The concept was tested with n8n with limited success.
> The concept is sound, but implementation was a sore point. The evidence points
> to a full all-in-one deployment via a custom Docker container rather than n8n
> as a middle-man. Enough data has been observed and gathered to make educated
> design decisions; see `docs/ROADMAP.md` and `docs/DESIGN.md`. This repository
> is that rebuild, adapted from the proven Adaptive Light system.

## How it works

A day is divided into six **sections**. Their times come from a **time
profile**, which a room selects — several rooms can share one profile, so a
single edit updates them all. The default profile:

| Section | Fires at |
|---|---|
| Sunrise | 05:30 |
| Day | 08:00 |
| Afternoon | 14:00 |
| Sunset | 16:00 |
| Night | 20:30 |
| Sleep | 22:00 |

Each section's trigger is a fixed clock time, adjustable by the user. There are
no sun-relative triggers — climate follows your routine, not the sun.

The standard temperature unit is **Celsius**; this is adjustable by the user.

At each crossover, the thermostat setpoint is taken from the room's **almanac** —
the learned model of your preferences. Between crossovers a **maintenance** loop
compares measured temperature against the section's learned comfort band every
ten minutes and nudges the thermostats that are already on, by no more than
2 degrees at a time.

Every ten minutes a **heartbeat** records the reading of every temperature
sensor in the room and the state of every AC unit. When you adjust a thermostat
by hand, that **reactive** event is captured at five times the weight of a
heartbeat — you correcting the system is the most valuable signal it gets.
Overnight, a rolling three-week weighted analysis rebuilds the almanac,
favouring recent days and high-confidence samples.

The goal is convergence: a system that has learned enough to provoke no
reactions at all.

## Learning states

A room's almanac moves through three states, so the system is useful
immediately without pretending to know more than it does:

- **Provisional** — on the first crossover into a section, the current sensor
  readings and setpoints are sampled and used as targets straight away, but
  maintenance stays out. This is an observation, not learning.
- **Bootstrap** — under seven days of data; the almanac is published immediately
  so behaviour appears quickly.
- **Learning** — seven days or more; new almanacs take effect after a short
  validity delay, so a single odd day cannot swing behaviour.

A unit can also be set to **Off** for a section in the Config UI. That is an
explicit override, not something the learner decides — it is baked directly into
the generated automation and takes effect from the moment it is deployed,
regardless of whether an almanac exists yet.

## The sensor trust model

This is the core of Adaptive Climate. See `docs/TRUST_MODEL.md` for the full
design and a worked example; the short version:

The system does not try to hold a sensor at a fixed number. Instead it learns
the setpoint the thermostat sits on when you are comfortable, and — separately,
per sensor — the reading that *corresponds* to that comfort. A thermostat may
report it is pushing out 21°C while one sensor reads 25°C and another reads 24°C;
as long as you do not react, all of those are "comfortable".

When a sensor drifts and you then react, two things are learned at once: the
setpoint was wrong (so it is corrected), and that sensor is trustworthy (a small
drift on it predicted real discomfort). That sensor keeps its comfort reading,
gains **trust**, and gets a **tighter comfort band** — so next time it drifts by
even a little, the system already knows to correct.

- **High trust ≈ a 0.5°C band** — you react to small deviations on this sensor.
- **Low trust ≈ a 5°C band** — this sensor has to drift a long way before it
  means anything.

Each sensor votes into a **quorum** when its reading leaves its comfort band.
Corrective action starts once the quorum is met at a heartbeat:

- 1 sensor — 1 vote required.
- 2 sensors — 2 votes required.
- 3 or more sensors — 50% or more of sensors voting.

Corrective action stops at the next heartbeat where the tally drops below quorum,
or after one hour, whichever comes first.

## AC unit control

Air-conditioner units are driven through Home Assistant's standard `climate`
(thermostat) entity, set over the API. Each unit is a small state machine; the
active state depends on where the measured temperature sits relative to the
learned comfort band:

| State | Trigger | Fan speed | Setpoint | AC mode |
|---|---|---|---|---|
| **Normal** | within the comfort band | LOW | almanac | COOL |
| **Cooling** | below the comfort band | MEDIUM | almanac − 2°C | COOL |
| **Warming** | above the comfort band | LOW | almanac | FAN ONLY |
| **Leak** | leak detected | LOW | almanac | DRY |

State profiles are fixed for now; making them user-customisable is on the
roadmap.

## Leak detection

Optional, per unit. When enabled, the system generates a dedicated boolean
helper (`input_boolean.ac_leak_<room>_<unit>`) and a stub automation in Home
Assistant; you wire whatever leak sensor you like to that automation to raise
the boolean. While it is on, the unit runs in **Leak** mode. Leak mode persists
until the leak is no longer detected **and** the user confirms the leak is fixed
(a latch, so a momentarily-dry sensor cannot silently release it).

## Architecture

**Home Assistant owns behaviour. The container owns observation.**

Home Assistant runs the AC automations and the maintenance loop, so the parts
you tune and debug stay visible in the UI with traces intact. The container
observes over the WebSocket API, learns, schedules the crossovers, and pushes
almanacs back over REST. It never touches Home Assistant's filesystem, reaching
it only over the network API.

Scheduling is **level-triggered**: rather than a timer per boundary, a slow loop
asks "which section should be active now?" and fires a crossover when the answer
changes. Startup after downtime, a dropped WebSocket, a clock change and a paused
VM all take the same path, so there is no separate catch-up routine to get wrong.

Home Assistant needs a small number of helpers per room, all generated:

- `input_select.ac_scene_<room>` — the active section
- `input_boolean.ac_active_<room>` — an AC automation is mid-change; ignore
- `input_boolean.ac_hold_<room>` — the user intervened; maintenance stands down
- `input_boolean.ac_leak_<room>_<unit>` — leak latch, per leak-enabled unit
- `sensor.ac_almanac_<room>` — the learned model, pushed over REST

Helpers are named from the room's stable **id**, not its display name, so the
object id Home Assistant derives always matches what the generated automations
reference. Deployment verifies this and refuses rather than deploy a broken
reference.

## The web UI

Served by the container at `http://<host>:8098`. Dark, desktop-oriented, no login
(it lives on your LAN).

- **Now** — the live picture per room: measured temperature (each sensor)
  against its comfort band, each thermostat's actual setting versus its learned
  target, the current AC state, the section timeline and a countdown to the next
  crossover.
- **Analysis** — one day on a shared time axis: each sensor's measured
  temperature, the target comfort band per section, per-unit setpoint, section
  boundaries and markers wherever you intervened. A day with no markers is a day
  the system got right. Step back through history with the date controls.
- **Almanac** — the learned model per room: one row per section showing the
  learned setpoint per unit and, per sensor, the comfort reading, its trust and
  band, with a compact sparkline of how they have settled over recent nights. A
  rising, then flattening line is convergence. A **Re-run analysis** button
  rebuilds the almanac on demand rather than waiting for the nightly job.
- **Config** — rooms, units and sensors chosen from live entity pickers (a typo
  cannot silently resolve to `unknown`); the scene matrix of auto/off per unit
  per section; per-unit leak-detection toggles; and **Time Profiles**, with a
  per-section trigger editor. Rooms and profiles are collapsible.
- **Log** — everything the container did and when, filterable by room, category
  and severity. Heartbeats are recorded at debug level so they do not bury the
  rest.

Saving configuration starts observation. Deploying creates the helpers and
automations in Home Assistant. A change to section *times* is live from Save
alone; a change to what a scene *does* (a unit's auto/off mode, transition time)
needs a redeploy.

## Layout

```
app/          scheduler, analyser, generator, trust, runtime, deploy, FastAPI entry
app/static/   the web UI (vanilla JS, vendored uPlot; no build step)
schema/       config JSON Schema, SQLite DDL
examples/     worked configuration
tools/        offline validation harnesses
docs/         design decisions and roadmap
```

## Requirements

A **Docker host** on the same network as Home Assistant. Home Assistant OS
cannot run arbitrary containers, so this needs a separate machine or VM. The
three roles — the Docker host, Home Assistant, and (optionally) a git remote —
can be entirely separate machines; nothing assumes they are co-located.

## Quick start

```bash
git clone https://github.com/Raven-git-hub/ha-adaptive-climate.git
cd ha-adaptive-climate

cp .env.example .env && $EDITOR .env    # HA URL and long-lived token

mkdir -p data && sudo chown 10001 data  # container runs unprivileged
docker compose up -d

curl -s localhost:8098/healthz
```

The container comes up **idle** with no rooms and stays healthy — it does
nothing until you add one. Then open `http://<host>:8098`, add your room, its AC
units, and its temperature sensors from the pickers, optionally enable leak
detection per unit, and press **Deploy to Home Assistant**.

If you are replacing an existing climate system, disable or delete it first —
two systems driving the same thermostats will fight. Adaptive Climate
distinguishes its own changes (and, during a transition, another system's) from
your manual ones, but only one should be actively driving the thermostats.

The first almanac builds from the nightly analysis at 00:15; until then the
system observes and seeds provisional targets, and maintenance begins the
following day.

## Validation

The harnesses run without Docker or Home Assistant:

```bash
python tools/doctor.py                              # check a real Home Assistant
python tools/sweep.py                               # a year of section boundaries
python tools/compare_analyser.py heartbeat.csv      # diff two analyser versions
```

## Roadmap and future ideas

Planned next: **Home Assistant add-on packaging**, which would remove the need
for a separate Docker host. Also on the list: **user-customisable AC state
profiles**.

See `docs/ROADMAP.md` for status and `docs/DESIGN.md` for the reasoning behind
the key decisions.

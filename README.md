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
thermostat controls; your switches keep working exactly as before, and a manual
change is treated as the most valuable signal the system gets, not a fault.

> **Status: running.** Built out from an n8n prototype into a self-contained
> Docker container. The full observe-learn-control loop is live: the container
> schedules the day, samples every ten minutes, learns per-sensor comfort and
> trust, and drives the air conditioners through generated Home Assistant
> automations. All five UI views are in. What is still rough or deferred is
> listed under [Limitations](#limitations); see `docs/ROADMAP.md` for the full
> phase-by-phase status and `docs/DESIGN.md` for the reasoning behind the key
> decisions.

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
no sun-relative triggers — climate follows your routine, not the sun. The
standard temperature unit is **Celsius**; this is adjustable.

At each crossover, the thermostat setpoint is taken from the room's **almanac** —
the learned model of your preferences. Between crossovers a **maintenance** loop
(running in Home Assistant, on its own clock) compares measured temperature
against the section's learned comfort band every ten minutes and nudges the
thermostats that are already on, by no more than 2 degrees at a time.

Every ten minutes a **heartbeat** records the reading of every temperature
sensor in each room and the state of every AC unit. Only a person moves a
target: **you are a sensor that always wins.** Each room has two buttons in
Home Assistant, **too cold** and **too hot** (`input_button.ac_too_cold_<room>`,
`input_button.ac_too_hot_<room>`), which a dashboard, a physical button or a
knob can press. A press rests or boosts the room's units at once, steps the
target a degree, and teaches every sensor where your comfort sits. Changing a
setpoint on the remote counts as the same vote, with the value you chose. One
correction per episode: while a press's rest or boost is running, further
presses are logged and ignored. The almanac is a replay of every reaction,
rebuilt straight after each one and nightly at 00:15.

The goal is convergence: a system that has learned enough to provoke no
reactions at all.

## Learning states

A room's almanac moves through three states, so the system is useful
immediately without pretending to know more than it does:

- **Provisional** — on the first crossover into a section with nothing learned,
  the current sensor readings and setpoints are sampled and used as targets
  straight away. This is an observation, not learning.
- **Bootstrap** — under seven days of data; the almanac is published immediately
  so behaviour appears quickly.
- **Learning** — seven days or more; new almanacs take effect after a short
  validity delay, so a single odd day cannot swing behaviour.

A unit can also be set to **Off** for a section in the Config UI. That is an
explicit override, baked directly into the generated automation, and it is the
*only* thing that commands a unit off — a section with no learned setpoint is a
genuine no-op that leaves the unit exactly as it is (see `docs/DEPLOY.md`).

## The sensor trust model

This is the core of Adaptive Climate. See `docs/TRUST_MODEL.md` for the full
design and a worked example; the short version:

The system does not try to hold a sensor at a fixed number. It learns the
setpoint the thermostat sits on when you are comfortable, and — separately, per
sensor — the reading that *corresponds* to that comfort. A thermostat may report
21°C while one sensor reads 25°C and another 24°C; as long as you do not react,
all of those are "comfortable".

When a sensor drifts and you then react, two things are learned at once: the
setpoint was wrong (so it is corrected), and that sensor is trustworthy (a small
drift on it predicted real discomfort). Each reaction nudges every sensor's
**comfort band**: one that had drifted your way tightens (gaining **trust**),
one on the other side widens, and one sitting exactly at its comfort learns
nothing. A press also moves each sensor's comfort reading toward its reading
a degree your way.

- **High trust ≈ a 0.5°C band** — you react to small deviations on this sensor.
- **Low trust ≈ a 5°C band** — this sensor must drift a long way to mean anything.

Each sensor votes into a **quorum** when its reading leaves its comfort band.
A sensor that is unavailable, or that Home Assistant hasn't heard from for 90
minutes (`system.sensor_stale_minutes`), has no vote and teaches nothing.
Corrective action starts once the quorum is met at a heartbeat (1 sensor → 1
vote; 2 → 2; 3+ → 50% or more): too warm boosts the unit, too cold rests it.
It ends when the sensors still on that side fall below the quorum, or after
an hour of boost.

## AC unit control

Air-conditioner units are driven through Home Assistant's standard `climate`
(thermostat) entity. Each unit is a small state machine kept in Home Assistant
helpers, so it carries on while the container is down. The system only ever
cools a unit or turns it off, apart from leak protection:

| State | When | AC mode | Fan | Setpoint |
|---|---|---|---|---|
| **cool** | the default while a section is running | COOL | LOW | base + offset |
| **boost** | the quorum says too warm, or you pressed **too hot** | COOL | MEDIUM | base + offset − 2°C |
| **rest** | the quorum says too cold, you pressed **too cold**, or cooling is blocked | OFF | — | — |
| **user_off** | a person turned the unit off | OFF | — | — |
| **manual** | a person chose another mode or fan | theirs | theirs | theirs |
| **config_off** | the section's scene forces the unit off | OFF | — | — |
| **leak** | leak detected (latched) | OFF | — | — |

The **base** is the section's learned setpoint, set at each section change, or
what a person chose since. The **offset** moves in whole steps within a section:
−1 when a boost ends, +1 when a rest wakes, capped at ±3, and reset at the next
section change. Commands are sent only on transitions, clamped to the unit's own
range, and never re-sent every heartbeat. A unit stays in a state for at least
one heartbeat (two when resting), and a person's turn-on holds it in **cool**
for 30 minutes. **user_off** and **manual** last until the section changes. A
rest or boost you forced with a press (or a setpoint change on the remote) ends
by the same rules, but returns to **cool** at the new base, without the offset
step; until it does, further presses are ignored.

Fan speed availability and min/max range are read from each unit at runtime, so
one generated automation adapts per unit — including units that hide their fan
modes in some HVAC modes (see `docs/HARDWARE.md`). State profiles are fixed for
now; making them user-customisable is on the roadmap. `docs/V2_DESIGN.md`
(Control) has the full rules.

## Leak detection

Optional, per unit, picked from a live `binary_sensor` list on the Config
page - like every other entity picker, so a typo can't silently resolve to
nothing. Populate it and the generator wires the trigger directly to that
sensor; leave it blank and the container still creates the boolean helper
(`input_boolean.ac_leak_<room>_<unit>`) with an empty trigger for you to wire
by hand in Home Assistant instead.

The full cycle: when the sensor (or your own wiring) trips, the leak
automation raises `input_boolean.ac_leak_<room>_<unit>` **and immediately puts
the unit in its leak state** (turned OFF: dry would still run the compressor),
whatever else is going on - a person having turned it off or paused the room
included. While the boolean is on, the unit stays in leak, and **any scheduled
crossover that lands mid-leak re-asserts OFF instead of reverting to cooling** -
leak safety overrides the schedule, not the other way round. Release is
latched: it fires when the user confirms via
`input_boolean.ac_leak_confirmed_<room>_<unit>`, blocked while the sensor
still reads wet if the sensor is known, and on release the unit is treated as
at a section change immediately rather than sitting off until the next
scheduled boundary.

## Architecture

**Home Assistant owns behaviour. The container owns observation.**

Home Assistant runs the AC automations and the maintenance/quorum loop, so the
parts you tune and debug stay visible in the UI with traces intact, and they
keep working even if the container is down. The container observes over the
WebSocket API, learns, schedules the crossovers, and pushes almanacs back over
REST. It never touches Home Assistant's filesystem, reaching it only over the
network API.

Scheduling is **level-triggered**: a slow loop asks "which section should be
active now?" and fires a crossover when the answer changes. Startup after
downtime, a dropped WebSocket, a clock change and a paused VM all take the same
path — a plain restart that is already in the right section does not re-fire (so
your ACs don't beep on every restart), while a genuinely missed crossover does.

Generated per room, all named from the room's stable **id** (never its display
name), so deploy can own and prune its own artifacts without ever touching a
foreign automation:

- `input_select.ac_scene_<room>` — the active section
- `input_boolean.ac_active_<room>` — an AC automation is mid-change; ignore
- `input_boolean.ac_hold_<room>` — a manual pause: while it is on, no commands
  are sent (only a person turns it on or off)
- per unit, its control state: `input_select.ac_state_<room>_<unit>`,
  `input_number.ac_base_…` and `ac_offset_…`, `input_datetime.ac_since_…` and
  `ac_grace_until_…`, `input_boolean.ac_unblocked_…`
- `input_boolean.ac_leak_<room>_<unit>` / `_confirmed_...` — leak latch (per leak unit)
- `sensor.ac_almanac_<room>` — the learned model, pushed over REST
- automations: `ac_scene_<room>_<section>` ×6, `ac_maintenance_<room>`,
  `ac_manual_<room>_<unit>` per unit (a person's change, applied straight away),
  `ac_watchdog_<room>`, and leak automations for leak-enabled units

## The web UI

Served by the container at `http://<host>:8098`. Dark, desktop-oriented, no
login (it lives on your LAN). All five views are built:

- **Now** — live per room: each thermostat's state, current temperature and
  setpoint, each sensor's reading, and the guard/hold/scene status.
- **Analysis** — a three-day chart per room (vendored uPlot): each sensor's
  measured temperature, the learned comfort band per section as a shaded region,
  each unit's setpoint, section boundaries, and a red mark wherever you
  intervened. A window with no red marks is time the system got right. Step
  through history with the date controls.
- **Almanac** — the learned model per room: per section, the learned setpoint per
  unit and, per sensor, the comfort reading with its trust and band.
- **Config** — structured editor: collapsible rooms (units, sensors, leak
  toggles, the per-section scene auto/off matrix) and collapsible time
  profiles, plus system/learning/connection settings. Entity pickers are
  backed by a live list from Home Assistant with a found/not-found indicator,
  degrading to free-text entry while idle. A **Raw JSON** toggle exposes the
  same document directly for anything the form doesn't cover yet. **Validate &
  Save**, **Check deploy** (a dry run listing what Deploy would do, no writes), and **Deploy
  to Home Assistant** all validate against the same schema the backend
  enforces.
- **Log** — everything the container did and when, filterable by room, category
  and severity, with expandable detail. Heartbeats sit at debug level so they do
  not bury the rest.

## Layout

```
app/          config, store, scheduler, analyser, generator, trust,
              runtime, deploy, ha, FastAPI entry (main)
app/static/   the web UI (vanilla JS, vendored uPlot; no build step)
schema/       config JSON Schema, SQLite DDL
examples/     worked configuration (real-entity example)
tools/        offline validation harnesses + the HA doctor
docs/         design decisions, trust model, hardware notes, almanac
              format, deploy notes, roadmap
```

## Requirements

A **Docker host** on the same network as Home Assistant. Home Assistant OS
cannot run arbitrary containers, so this needs a separate machine or VM. The
Docker host, Home Assistant, and (optionally) a git remote can be entirely
separate machines; nothing assumes they are co-located. All Python dependencies
are baked into the image — nothing to install on the host.

## Quick start

```bash
git clone https://github.com/Raven-git-hub/ha-adaptive-climate.git
cd ha-adaptive-climate

cp .env.example .env && $EDITOR .env    # HA URL and long-lived token; set TZ

mkdir -p data && sudo chown 10001 data  # container runs unprivileged
docker compose up -d --build

curl -s localhost:8098/healthz
```

The container comes up **idle** with no rooms and stays healthy — it does
nothing until you configure one. Before deploying, it's worth pointing the
doctor at your Home Assistant to confirm the connection, token, and that your
units support the modes the state machine needs:

```bash
docker compose run --rm \
  -e AC_HA_URL -e AC_HA_TOKEN -e AC_HA_VERIFY_SSL \
  -e AC_CONFIG=/data/config/config.json \
  adaptive-climate python tools/doctor.py
```

Then open `http://<host>:8098`, go to **Config**, describe your rooms, AC units
and temperature sensors (start from `examples/config.example.json`), **Validate
& Save**, run **Check deploy**, and **Deploy to Home Assistant**. The runtime
starts on the next container start when rooms and credentials are present; a
config change needs a `docker compose restart adaptive-climate` for the running
observer to pick it up.

If you are replacing an existing climate system, disable or delete it first —
two systems driving the same thermostats will fight. The first learned almanac
builds from the nightly analysis at 00:15; until then the system observes and
seeds provisional targets.

## Validation

The harnesses run without Docker or Home Assistant (the doctor needs a live HA).
Each one cross-checks a real part of the system:

```bash
python tools/doctor.py                     # check a real Home Assistant (7 steps, the weather entities included)
python tools/doctor_check.py               # the doctor against a stub Home Assistant
python tools/sweep.py                       # a year of section boundaries
python tools/store_check.py                 # store writes + reads, into the analyser
python tools/analyser_demo.py               # the trust-model worked example
python tools/render_generated.py            # render helpers + automations
python tools/control_check.py               # the control state machine's scenarios, the votes included
python tools/control_logic_check.py         # the generated Jinja replayed against them
python tools/deploy_reconcile_check.py      # deploy create / reuse / update / prune
python tools/leak_response_check.py         # the leak latch: off when wet, released when dry
python tools/runtime_check.py               # runtime observer + control loop
```

## Documentation

- `docs/DESIGN.md` — inherited principles and climate-specific departures
- `docs/TRUST_MODEL.md` — the sensor trust model and quorum, with a worked example
- `docs/HARDWARE.md` — what the real units and sensors do, and its consequences
- `docs/ALMANAC_FORMAT.md` — the almanac JSON contract shared across the system
- `docs/DEPLOY.md` — reconciliation, ownership, and safety notes
- `docs/ROADMAP.md` — phase-by-phase status and future ideas

## Limitations

Honest about what is not finished:

- **Config editing is raw JSON.** It validates against the schema, but a
  structured picker is still to come.
- **Config changes need a container restart** for the running observer to pick
  them up (deploy and the UI see them immediately).
- **Foreign-automation conflict detection is manual.** Deploy owns and prunes
  its own `ac_*` artifacts but will not delete anything else; it reminds you to
  check that nothing else drives the same thermostats. See `docs/DEPLOY.md`.
- **CSV re-ingest is not implemented.** The forward CSV+SQLite write path is; the
  recovery path that rebuilds SQLite from the CSV archive is still to come.
- **The full config loader (validation + cross-reference checks) is partial** —
  the runtime reads a lightweight view of the config today.

## Roadmap and future ideas

Planned next: **Home Assistant add-on packaging** (removing the separate Docker
host), **user-customisable AC state profiles**, and the structured Config
picker. See `docs/ROADMAP.md`.

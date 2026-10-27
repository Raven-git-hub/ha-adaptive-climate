# CLAUDE.md

Guidance for Claude Code (or any AI assistant) working in this repository.

## What this project is

**Adaptive Climate** is a Home Assistant companion container that learns the
temperature setpoint and per-sensor "comfort reading" for each room, then
drives AC units through generated HA helpers/automations to hold that comfort
level automatically. It's a rebuild of an earlier project, **Adaptive Light**
(which reached production), reusing its architecture but replacing Light's
single measured-lux model with a genuinely new **sensor trust model** — see
`docs/TRUST_MODEL.md` before touching the analyser, schema, or generator.

Read `docs/DESIGN.md` for inherited architectural principles and the specific
departures Climate makes from Light. Read `docs/ROADMAP.md` for what's done,
what's in progress, and what's intentionally deferred.

## v2 (branch `v2`)

On branch `v2`, **`docs/V2_DESIGN.md` is the source of truth.** Read it first.
Wherever it conflicts with `docs/DESIGN.md`, `docs/TRUST_MODEL.md`,
`docs/ALMANAC_FORMAT.md` or `README.md`, V2_DESIGN.md wins. Those documents
still describe v1 and are rewritten as each v2 phase lands, so treat any
v1-only detail in them (heartbeat-fed learning, the Warming/fan_only path,
bootstrap and validity delay, the room-level correction timer) as outgoing.
The v1 state is tagged `v1.0-poc`.

**Never deploy a v2 build that has phase 0 but not phase 1: phase 0 empties
the almanac and only phase 1's replay-on-first-heartbeat refills it from
history.** Otherwise deploy follows the build plan in V2_DESIGN.md (Gate A).

## Architecture at a glance

- **CSV is the archive; SQLite is the query layer.** Every observation is
  written to CSV first, then SQLite. SQLite must always be rebuildable from
  CSV — the container holds no irreplaceable state.
- **The container is the brain; Home Assistant is the actuator.** HA owns
  behavior (the maintenance/quorum loop runs as generated HA templates, not
  in-container), so correction logic survives container downtime.
- **We own only our own namespace.** Every helper/automation we create is
  prefixed `ac_`, derived from the room's *stable id*, not its display name.
  Deploy only ever creates, updates, or deletes entities in that namespace —
  never anything else. See `docs/DEPLOY.md`.
- **The event log is a feature, not debug output.** Every action the
  container takes is recorded and surfaced in the UI, not just logged.
- **Reactive detection ignores our own automation's changes** (guard boolean,
  `context.parent_id`, or the unit showing the command its state helper
  implies - `app.control.matches_command`), so the maintenance loop doesn't
  learn from itself.

### Core modules (`app/`)

| Module | Responsibility |
|---|---|
| `app/trust.py` | Pure-function core: sensor trust, comfort band, quorum evaluation |
| `app/control.py` | Pure-function control core: votes, the cool/boost/rest state machine, section changes, person reactions, the "is this ours" check (rendered as Jinja by the generator) |
| `app/analyser.py` | Deterministic replay of reactions from a seed: setpoints, applied setpoints, per-sensor comfort/band/trust, confidence |
| `app/generator.py` | Builds HA helpers (incl. six per-unit control helpers) and automations (scene, maintenance, manual change, leak, watchdog) from config, rendering `app/control.py` as Jinja |
| `app/scheduler.py` | Clock-only section-boundary computation (no sun-based triggers) |
| `app/ha.py` | Home Assistant REST + WebSocket client |
| `app/deploy.py` | Reconciles generated helpers/automations into HA; owns the `ac_*` namespace; manifest-based automation pruning |
| `app/runtime.py` | Observer + control loop: scheduler, reactive detector, replay triggers, almanac push |
| `app/store.py` | CSV + SQLite storage, event log, almanac persistence |
| `app/main.py` | FastAPI app, lifespan wiring |
| `app/config.py` | Config loading: v1 → v2 migration, v2 defaults, schema validation (cross-reference checks still to come — Roadmap Phase 9) |
| `app/static/` | Vanilla-JS UI (no build step) — Now / Analysis / Almanac / Config / Log tabs |

### Key docs

- `docs/V2_DESIGN.md` — v2 design and build plan; on branch `v2` it overrides the docs below where they conflict
- `docs/V2_CUTOVER.md` — the v1 → v2 cutover runbook, with checks and rollback
- `docs/DESIGN.md` — inherited principles + climate-specific departures (read after V2_DESIGN.md)
- `docs/TRUST_MODEL.md` — the sensor trust model and quorum, with a worked example
- `docs/ALMANAC_FORMAT.md` — the almanac JSON contract shared across modules
- `docs/DEPLOY.md` — reconciliation strategy, ownership model, safety notes
- `docs/HARDWARE.md` — what the real units/sensors do and its consequences
- `docs/ROADMAP.md` — phase-by-phase status; check before assuming something is unbuilt

## Working conventions

- **The config schema is the contract.** `schema/config.schema.json` and
  `schema/storage.schema.sql` are load-bearing — changes there ripple through
  the generator, analyser, and deploy. Check `docs/ALMANAC_FORMAT.md` too if
  touching almanac shape.
- **Storage changes go through a numbered migration.** `storage.schema.sql`
  is the current shape; `app/store.py` `MIGRATIONS` brings older databases to
  it (backup first, one checked transaction each, rollback on failure).
  Changing a CREATE there needs a new migration, and `tools/store_check.py`
  must still find fresh and migrated databases identical.
- **Config changes keep v1 loadable.** `app/config.py` migrates version 1 to
  2 and fills defaults only for keys v2 added; `tools/config_check.py` pins
  the v1 example migrating to exactly the v2 example.
- **Helper/automation ids derive from stable room/unit ids, not display
  names.** The friendly name must slugify to the object id referenced by
  generated automations; deploy verifies this and refuses to deploy a broken
  reference rather than guessing.
- **Never widen deploy's blast radius.** Anything under `app/deploy.py` must
  stay inside the `ac_*` prefix. Do not add logic that enumerates, edits, or
  deletes entities outside that namespace.
- **New generator logic needs a template-vs-core cross-check**, the way
  `tools/control_logic_check.py` runs the automations the generator emits
  (real Jinja2, HA's variable/condition/choose semantics) through every
  `tools/control_check.py` scenario and diffs them against `app/control.py`.
  Don't trust a hand-inspected template.
- **"No almanac yet" and "deliberately forced off" are different states** —
  don't conflate them in generated scene logic (see the safety fix described
  in `docs/DEPLOY.md`).

## Running validation (no Docker or live HA required, except `doctor.py`)

```bash
python tools/doctor.py                     # check a real Home Assistant (6-step diagnostic; needs live HA)
python tools/doctor.py --v2-preflight      # ...plus v2's helper/template behaviour, on throwaway ac_preflight_* helpers
python tools/sweep.py                       # a year of section-boundary computation
python tools/store_check.py                 # store writes + reads, into the analyser; migrations
python tools/config_check.py                # config v1 -> v2 migration, defaults, validation
python tools/analyser_demo.py               # reaction replay: worked examples + acceptance checks
python tools/compare_analyser.py DATA_DIR   # frozen v1 vs v2 almanacs on a COPY of a data dir (migrates it)
python tools/render_generated.py            # render helpers + automations
python tools/control_check.py               # control state machine scenarios; each rule removed must fail
python tools/control_logic_check.py         # the generated automations replayed against app/control.py
python tools/deploy_reconcile_check.py      # deploy create / reuse / update / prune
python tools/runtime_check.py               # runtime observer + control loop
python tools/leak_response_check.py         # leak detection / OFF / release automations
```

Run `PYTHONPATH=.` before these if invoking from outside the repo root (most
scripts also self-insert the repo root via `sys.path`). These harnesses are
the test suite in the absence of a live HA instance — run the relevant ones
after touching `app/generator.py`, `app/trust.py`, `app/control.py`, `app/deploy.py`,
`app/scheduler.py`, `app/store.py`, or `app/runtime.py`.

## Local dev / Docker

```bash
docker compose run --rm \
  -e AC_HA_URL -e AC_HA_TOKEN -e AC_HA_VERIFY_SSL \
  -e AC_CONFIG=/data/config/config.json \
  adaptive-climate python tools/doctor.py
```

The app serves on `:8098`. A config *time* change is live from Save alone; a
change to scene *behavior* (unit mode, transition time, room/unit/sensor
definitions) needs Deploy. A config change needs
`docker compose restart adaptive-climate` for the running observer to pick it
up — deploy and the UI see it immediately.

## Known gaps (don't be surprised by these; check `docs/ROADMAP.md` first)

- Config editing in the UI is raw JSON; a structured picker is still to come.
- CSV re-ingest (rebuilding SQLite from the CSV archive) is not implemented;
  it is a prerequisite for v2's Gate A.
- Foreign-automation conflict detection is manual — deploy prunes only its own
  `ac_*` artifacts and reminds the user to check for conflicts by hand.
- The config loader migrates, defaults and schema-validates, but the
  cross-reference checks (Phase 9) are still missing; the runtime reads the
  migrated config as a plain dict.
- User-customisable AC state profiles and HA add-on packaging are future work
  (Phases 16–17).

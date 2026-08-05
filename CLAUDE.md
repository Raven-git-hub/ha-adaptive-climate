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
- **Reactive detection ignores our own automation's changes** (guard boolean
  or `context.parent_id`), so the maintenance loop doesn't learn from itself.

### Core modules (`app/`)

| Module | Responsibility |
|---|---|
| `app/trust.py` | Pure-function core: sensor trust, comfort band, quorum evaluation |
| `app/analyser.py` | Learns setpoints + per-sensor comfort/trust/band from history |
| `app/generator.py` | Builds HA helpers and automations (scene, maintenance, leak, watchdog) from config + almanac |
| `app/scheduler.py` | Clock-only section-boundary computation (no sun-based triggers) |
| `app/ha.py` | Home Assistant REST + WebSocket client |
| `app/deploy.py` | Reconciles generated helpers/automations into HA; owns the `ac_*` namespace; manifest-based automation pruning |
| `app/runtime.py` | Observer + control loop: scheduler, reactive detector, almanac push, leak latch |
| `app/store.py` | CSV + SQLite storage, event log, almanac persistence |
| `app/main.py` | FastAPI app, lifespan wiring |
| `app/config.py` | Config loading (validation/cross-reference checks still partial — see Roadmap Phase 9) |
| `app/static/` | Vanilla-JS UI (no build step) — Now / Analysis / Almanac / Config / Log tabs |

### Key docs

- `docs/DESIGN.md` — inherited principles + climate-specific departures (read first)
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
- **Helper/automation ids derive from stable room/unit ids, not display
  names.** The friendly name must slugify to the object id referenced by
  generated automations; deploy verifies this and refuses to deploy a broken
  reference rather than guessing.
- **Never widen deploy's blast radius.** Anything under `app/deploy.py` must
  stay inside the `ac_*` prefix. Do not add logic that enumerates, edits, or
  deletes entities outside that namespace.
- **New generator logic needs a template-vs-core cross-check**, the way
  `tools/maintenance_logic_check.py` renders the real Jinja2 the generator
  emits and diffs it against `app.trust`'s pure-Python reference. Don't trust
  a hand-inspected template.
- **"No almanac yet" and "deliberately forced off" are different states** —
  don't conflate them in generated scene logic (see the safety fix described
  in `docs/DEPLOY.md`).

## Running validation (no Docker or live HA required, except `doctor.py`)

```bash
python tools/doctor.py                     # check a real Home Assistant (6-step diagnostic; needs live HA)
python tools/sweep.py                       # a year of section-boundary computation
python tools/store_check.py                 # store writes + reads, into the analyser
python tools/analyser_demo.py               # the trust-model worked example
python tools/render_generated.py            # render helpers + automations
python tools/maintenance_logic_check.py     # quorum Jinja vs the app.trust core
python tools/deploy_reconcile_check.py      # deploy create / reuse / prune
python tools/runtime_check.py               # runtime observer + control loop
python tools/leak_response_check.py         # leak detection / DRY / release automations
```

Run `PYTHONPATH=.` before these if invoking from outside the repo root (most
scripts also self-insert the repo root via `sys.path`). These harnesses are
the test suite in the absence of a live HA instance — run the relevant ones
after touching `app/generator.py`, `app/trust.py`, `app/deploy.py`,
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
- CSV re-ingest (rebuilding SQLite from the CSV archive) is not implemented.
- Foreign-automation conflict detection is manual — deploy prunes only its own
  `ac_*` artifacts and reminds the user to check for conflicts by hand.
- The full config loader (validation + cross-reference checks, Phase 9) is
  partial; the runtime currently reads a lightweight view of the config.
- User-customisable AC state profiles and HA add-on packaging are future work
  (Phases 16–17).

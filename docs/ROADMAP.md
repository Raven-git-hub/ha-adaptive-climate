# Roadmap

Adapted from the Adaptive Light roadmap. Light reached "deployed"; Climate is at
the start of the rebuild, with the design settled enough to begin the contract.

| Phase | Scope | Status |
|---|---|---|
| 0 | n8n prototype: proved the concept, exposed the implementation pain | superseded by this rebuild |
| 1 | Config schema — the contract everything else derives from | **in progress** — `schema/config.schema.json` |
| 2 | Storage: SQLite DDL, CSV layout, event log | **in progress** — `schema/storage.schema.sql` |
| 3 | Trust model + quorum design and the pure-function core | **drafted** — `docs/TRUST_MODEL.md`, `app/trust.py` |
| 4 | Analyser: setpoint learning + per-sensor comfort/trust/band | **in progress** — `app/analyser.py` (built + tested), almanac contract in `docs/ALMANAC_FORMAT.md` |
| 5 | Generator: helpers, AC state machine, maintenance/quorum, leak | **done** — helpers/scene/leak/watchdog/maintenance all built + tested (maintenance logic verified against `app.trust` via real Jinja2, 45/45) |
| 6 | Scheduler: clock-only boundary computation (no sun) | todo — `app/scheduler.py` |
| 7 | HA client (REST + WebSocket) and connection doctor | **done** — `app/ha.py` (fake-HA integration-tested), `tools/doctor.py` (6-step diagnostic) |
| 8 | Storage layer: dual CSV/SQLite writes, event log, CSV re-ingest | todo — `app/store.py` |
| 9 | Config loader: schema validation, defaults, cross-reference checks | todo — `app/config.py` |
| 10 | Runtime: scheduler, observer, reactive detector, almanac push, leak latch | todo — `app/runtime.py` |
| 11 | Config API and deployment to Home Assistant | todo — `app/deploy.py`, `app/main.py` |
| 12 | UI shell, status strip, Config and Log | todo — `app/static/` |
| 13 | UI — Now (live dashboard, per-sensor bands, AC state) | todo |
| 14 | UI — Analysis (day chart, uPlot) | todo |
| 15 | UI — Almanac (setpoint + per-sensor trust/band, settling sparkline) | **started** — Almanac view + `/api/almanac`, `/api/analysis/run` live |
| 16 | Home Assistant add-on packaging | later |
| 17 | User-customisable AC state profiles | later |

## First decisions to make (Phase 4 gate)

1. **Where the quorum maintenance loop runs** — DECIDED: Home Assistant
   templates, preserving "HA owns behaviour". See `docs/DESIGN.md` D4 and the
   almanac contract in `docs/ALMANAC_FORMAT.md`.
2. **Occupancy** — whether presence gates learning or drives an empty-room
   action, or is ignored. Schema reserves an optional slot so this is not a
   migration. See `docs/DESIGN.md` open decisions.

## Lessons carried from the Light cutover

These bit us in Light and the equivalents are pre-empted here:

- Start the WebSocket read loop *before* any subscription, or the event stream is
  dead while the log says "connected".
- Ignore automation-caused changes in reactive detection (guard boolean,
  `context.parent_id`, configurable `external_guards`) so a coexisting system's
  nudges are not learned as user interventions during cutover.
- Bake forced-off units into the automation directly so "off" holds from first
  deploy instead of waiting on an almanac.
- Close each `section_run` when the next fires, so Analysis target bands stop at
  the real boundary.

## Future ideas

- **User-customisable AC state profiles** — let a room define its own (fan, mode,
  offset) triples per state rather than the fixed four.
- **Presence rules** — configurable actions when a room is empty for a section.
- **Custom time sections** — let a profile add or remove sections rather than the
  fixed six (e.g. a nap window, a focus block).

# Deploy: reconciliation, ownership, and what's not done yet

How `app/deploy.py` gets the generator's output into Home Assistant, and the
boundaries of what it will and won't touch.

## Two reconciliation strategies, because HA has two different APIs

**Helpers** (`input_boolean`, `input_select`, `input_number`, `input_datetime`,
`input_button`) live in HA's storage collections, which expose a reliable `{domain}/list` over
WebSocket. So helpers are reconciled by **list-diff**: list what exists, create
what's missing, update ours whose settings changed (`{domain}/update`: the
offset helper's range after an `offset_cap` change, say, or a select's
options), delete our own (`ac_*`) ones no longer desired. Straightforward and
safe, because we can always see the ground truth. The v1 helper
`input_datetime.ac_correction_started_<room>` is pruned this way on the first
v2 deploy.

**Automations** use HA's config-editor REST API
(`GET`/`POST`/`DELETE /api/config/automation/config/<id>`), which has no
equivalent reliable "list everything" call. Rather than build reconciliation
on an enumeration path we can't fully verify without a live instance, deploy
keeps its **own ledger** — `data/config/deploy_manifest.json` — recording the
automation ids it wrote last time. Each deploy diffs desired-now against
deployed-last-time and deletes only what dropped out, by an id we know we
wrote ourselves. This is why `docker compose run` state and the container's
`/data` volume both matter: the manifest is part of the container's state,
like the config and the database.

Every desired automation is also **read back** by its own id
(`GET /api/config/automation/config/<id>`) and compared with what the
generator renders. One Home Assistant already holds exactly as rendered, and
that the manifest records as ours, is *unchanged*: deploy doesn't write it
again, and the report lists it under `automations_unchanged`. Anything that
can't be confirmed (an id the manifest lacks, no copy in Home Assistant, a copy
that differs, a read that fails) is written. So the report's `changes` count
says whether a deploy would change Home Assistant at all, which is what the
Config page's Apply needs (below), and one of our automations edited or deleted
by hand in Home Assistant is put back by the next deploy.

## Apply: one button saves, reloads and deploys

The Config page has one **Apply** button (phase 7, docs/V2_DESIGN.md, Usability
fixes). It replaces the old manual steps (Validate & Save, Check deploy, Deploy
to Home Assistant, and a container restart for the running observer):

1. Apply migrates the config to version 2 and validates it, then runs the dry
   run below against it. Problems (the config doesn't validate, a configured
   entity Home Assistant doesn't have, Home Assistant out of reach, no
   credentials) refuse it: the page lists them, and nothing is written.
2. If nothing in Home Assistant would change (a learning rate, a margin, a lean,
   the weather entity, a section time), Apply backs up the config file, saves
   the new one and hot-reloads it into the running container: "Applied, no Home
   Assistant changes". Nothing is deployed.
3. If something would (a room, unit or sensor, a scene's auto/off, a heat
   source, `sensor_stale_minutes`, the boost and minimum-time settings, the
   offset cap, `block_mode`), Apply shows a **preview** first: the helpers it
   would create, update and remove, the automations it would write and remove,
   any problems and notes (a hold left on). **Confirm** backs up, saves,
   hot-reloads and deploys, then shows the deploy's result; **Cancel** changes
   nothing. Confirm deploys exactly the plan that was shown: if Home Assistant
   changed in between, you get a new preview instead.

Every Apply that goes through first copies the config file to
`data/config/backups/config-<timestamp>.json` (the newest 20 are kept; restore
one by applying its contents in the Raw JSON view). The config is always
written as version 2. The Raw JSON view applies through the same path.

**Hot reload** puts the saved config in force without a restart: rooms,
sensors, units, the schedule, and the learning, control and outdoor settings.
What a room already decided for the section it is in (its conditions and block
decision) stays, so changed outdoor and block settings apply from each room's
next section, as after a restart; a moved section time takes effect at once
(a start that has already passed crosses straight away). If the reload fails,
the file and the running config stay as they were and the error is shown. A
container that started with no rooms starts its runtime when an Apply gives it
some.

**A container restart is now needed only for a code update** (a rebuild).
`GET /api/deploy/check`, `POST /api/deploy` and `POST /api/config` (save only,
no reload) remain as API endpoints; the page no longer uses them.

## Ownership: we own `ac_*` completely; we never touch anything else

Both reconciliation paths only ever create, update, or delete entities whose
id starts with `ac_` — our namespace, derived from the room's stable id
(docs/DESIGN.md 13). Deploy never enumerates or deletes anything outside that
prefix. The Now view's buttons (phase 6) stay inside it too: **Too cold** /
**Too hot** call `input_button.press` on the room's own
`input_button.ac_too_cold_<room>` / `ac_too_hot_<room>`, and **Leak fixed**
calls `input_boolean.turn_on` on the unit's own
`input_boolean.ac_leak_confirmed_<room>_<unit>`. They add no helper or
automation, so the Now view needed a rebuild and restart (a code update), not a deploy. This is the ownership model from the Phase 5 design discussion:
free-prune our own artifacts, never touch the user's or another integration's.

**Not implemented yet:** detecting a *foreign* automation that also drives one
of our thermostats (so you could review and resolve the conflict). That needs
a live-verified way to enumerate and inspect existing automations that this
sandbox can't confirm without a real Home Assistant to test against. Every
deploy report currently ends with a manual-review reminder instead of a
possibly-wrong automatic scan. If you're migrating off another climate system,
disable or delete it first, per the README.

## The safety fix: "no almanac yet" is a no-op, not an off command

(v2: the scene now runs `app.control.on_section_change`. With no
`setpoint_applied` it still resets the unit's section-scoped helpers but keeps
its state and sends nothing; forced-off stays the only path that commands off
from config. The rest of this section is the v1 history of that rule.)

`app/generator.py`'s scene automation used to treat two different things the
same way: a unit deliberately forced off by your scene config, and a unit with
no learned setpoint yet (true of every room before Phase 10's observer exists,
and true of any section a room hasn't reached bootstrap on). Both used to
issue `climate.set_hvac_mode: off`.

That's wrong for the second case. Before there's a runtime to seed a
provisional almanac at first crossover, EVERY section starts with no learned
setpoint — so on a fresh deploy, every unit in every room would be forced off
at every one of the six daily crossovers, overriding whatever you'd set
manually. For a live home, that's a real hazard, not a cosmetic bug.

The fix: forced-off (from your scene config) is the only path that commands
off, and it's entirely static — decided from config, not from almanac state.
"No almanac yet" now does nothing at all and leaves the unit exactly as it
was. The maintenance automation already had the correct behaviour (it never
acts without a learned setpoint); this brought the scene automation in line.

## Starting values: no `initial`, and new units start hands off

Each unit has eight control helpers (`ac_state`, `ac_base`, `ac_offset`,
`ac_since`, `ac_grace_until`, `ac_unblocked`, `ac_forced`, `ac_section_rest`), and each room two
vote buttons (`input_button.ac_too_cold_<room>`, `input_button.ac_too_hot_<room>`).
None is created with an `initial`: in Home Assistant, a helper's `initial` overrides its restored value
at **every** restart, which would wipe a person's chosen base, a `user_off` or
an unblock each time HA restarts. So a new helper starts at HA's default (an
`input_number` at its minimum, an `input_select` at its first option, an
`input_datetime` at today 00:00, a boolean off, a button never pressed), and
deploy then sets the
starting values of the helpers **it has just created**, and only those:

| Helper | Starts at | How |
| --- | --- | --- |
| `ac_base_<room>_<unit>` | 0 (unset: templates fall back to `setpoint_applied`) | its minimum is 0 |
| `ac_grace_until_…` | today 00:00, in the past: no grace | HA's default |
| `ac_unblocked_…` | off | HA's default |
| `ac_forced_…` | off: no forced state | HA's default |
| `ac_section_rest_…` | off: no section rest | HA's default |
| `ac_too_cold_<room>`, `ac_too_hot_<room>` | never pressed (`unknown`); the first press counts | HA's default |
| `ac_offset_…` | 0 | deploy sets it (its minimum is −`offset_cap`) |
| `ac_since_…` | now, as an epoch timestamp | deploy sets it |
| `ac_state_…` | `manual` | deploy sets it |

Helpers that already exist are never reset, so a redeploy leaves a running
boost or rest, and its clock, alone. (v1 reset its one-hour correction clock on
every deploy; that helper is gone.)

A new unit starts in **`manual`: hands off until its room's next section
change**, when the scene sets its base, offset and state properly. Deploying
therefore sends no command to any unit. The flip side is that **control begins
at each room's next section change, so deploy shortly before one** (or trigger
the current section's scene automation in HA to start a room at once).

## Before you deploy to a live, occupied home

- **For the v1 → v2 cutover, follow `docs/V2_CUTOVER.md`**, including
  `tools/doctor.py --v2-preflight` and the rollback steps.
- **Check the weather entity with the doctor before setting the outdoor
  source.** Its outdoor step (step 7, run with or without `--v2-preflight`,
  which is now step 8) lists every `weather.*` entity with its current
  temperature and whether it offers an hourly forecast, and shows the
  configured `outdoor.weather_entity`'s next 6 hourly entries. With
  `outdoor.source: forecast`, a configured entity that is missing, has no
  hourly forecast or returns an empty one fails the doctor; with another
  source it is a warning. The outdoor input adds no helper or automation, and
  Apply puts an `outdoor.*` change in force without a restart, from each room's
  next section. A `block_mode` change needs no deploy to take effect, but the
  templates carry the config's `block_mode` as a fallback for when there is no
  conditions entity, so Apply previews those automations and Confirm keeps them
  in step. Changing a room's `heat_sources` changes the templates too: the
  preview shows it.
- **Phase 5 (the lean and the block) needs a deploy.** The scene, maintenance,
  leak release, vote and manual-change automations now read
  `sensor.ac_conditions_<room>` and the room's heat sources; no helper
  changes. The conditions entity is not a helper and not in deploy's
  reconciliation: the container pushes it (like `sensor.ac_almanac_<room>`)
  at each crossover, every 5 minutes and after Home Assistant restarts, so
  there is nothing to create, prune or roll back. While it is missing (before
  the new container's first crossover, or after an HA restart until the next
  push) the templates fail open: edges ±1, not blocked. The check lists a heat
  source Home Assistant doesn't have as a missing entity, and the deploy then
  aborts.
- **Apply previews before it deploys.** Its preview is the dry run
  (`/api/deploy/check` runs the same thing): it verifies entity existence,
  names any hold that is on, and lists exactly what the deploy would create,
  update, remove and write, from the same plan with writing switched off
  (`"dry_run": true`), and how much is unchanged. It writes nothing and is safe
  to run any time; Cancel leaves everything as it was.
- Deploying installs real automations that call real `climate.*` services on
  your real thermostats from the next crossover on. Deploy itself sends no
  command, and new units are hands off (`manual`) until their room's next
  section change (above); from then on scenes and maintenance drive them.
  A room with no almanac yet is still left alone: the scene sends nothing
  without a `setpoint_applied`, and maintenance never acts without a base.
- Never deploy a v2 build that has phase 0 but not phase 1 (see CLAUDE.md).
- **The block ships in shadow.** With `outdoor.block_mode: shadow` (the
  default) the container decides and logs each section's block ("would block
  Night: ...") and learns the margin from it, but tells control `blocked` false;
  only heat sources block. Turning it `active` (Gate B) is an Apply: it takes
  effect from each room's next section with no restart, and the preview shows
  the templates' `block_mode` fallback being updated.
- **The vote buttons are ours, the things that press them aren't.** Deploy
  creates each room's `input_button.ac_too_cold_<room>` and
  `ac_too_hot_<room>` and the `ac_vote_<room>` automation that acts on them. A
  dashboard card, a physical button's automation or the knob's that presses them
  is yours, outside the `ac_` namespace, and deploy never touches it; if a room
  is removed, its buttons are pruned and those presses go nowhere.
- **After the first v2 deploy, check the holds.** v1 raised
  `input_boolean.ac_hold_<room>` on every reaction and its scenes cleared it;
  in v2 it is only a manual pause that nothing clears automatically, so one
  left on by v1 pauses that room until a person turns it off. The check and
  deploy reports name every hold that is on.
- The manifest-based prune only knows about automations *this container*
  deployed. One of our `ac_*` automations hand-edited or deleted in HA is
  noticed by the next Apply (its copy no longer matches the render) and written
  back; treat the generator + deploy as the source of truth once you start
  using it, the same way the README already asks for the config.

# Deploy: reconciliation, ownership, and what's not done yet

How `app/deploy.py` gets the generator's output into Home Assistant, and the
boundaries of what it will and won't touch.

## Two reconciliation strategies, because HA has two different APIs

**Helpers** (`input_boolean`, `input_select`, `input_datetime`) live in HA's
storage collections, which expose a reliable `{domain}/list` over WebSocket.
So helpers are reconciled by **list-diff**: list what exists, create what's
missing, delete our own (`ac_*`) ones no longer desired. Straightforward and
safe, because we can always see the ground truth.

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

## Ownership: we own `ac_*` completely; we never touch anything else

Both reconciliation paths only ever create, update, or delete entities whose
id starts with `ac_` — our namespace, derived from the room's stable id
(docs/DESIGN.md 13). Deploy never enumerates or deletes anything outside that
prefix. This is the ownership model from the Phase 5 design discussion:
free-prune our own artifacts, never touch the user's or another integration's.

**Not implemented yet:** detecting a *foreign* automation that also drives one
of our thermostats (so you could review and resolve the conflict). That needs
a live-verified way to enumerate and inspect existing automations that this
sandbox can't confirm without a real Home Assistant to test against. Every
deploy report currently ends with a manual-review reminder instead of a
possibly-wrong automatic scan. If you're migrating off another climate system,
disable or delete it first, per the README.

## The safety fix: "no almanac yet" is a no-op, not an off command

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

## Before you deploy to a live, occupied home

- **Run `/api/deploy/check` (or the Config page's "Check deploy" button)
  first.** It only verifies entity existence — no writes — and is safe to run
  any time.
- Deploying starts real automations that will call real `climate.*` services
  on your real thermostats the moment a crossover or maintenance tick fires.
  Until Phase 10 (the observer) exists, almanacs stay empty, so scene
  automations will do nothing (the safety fix above) and maintenance will
  never act (it already required a learned setpoint) — deploying now is inert
  in terms of driving temperature, but it DOES install real automations and
  helpers in your instance.
- The manifest-based prune only knows about automations *this container*
  deployed. If you hand-edit or delete one of our `ac_*` automations directly
  in HA, the next deploy will not necessarily notice or restore it correctly —
  treat the generator + deploy as the source of truth once you start using it,
  the same way the README already asks for the config.

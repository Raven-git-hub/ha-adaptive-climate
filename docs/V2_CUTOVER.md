# v2 cutover runbook

How to move the live install from v1 (`v1.0-poc`) to v2 (branch `v2`, phases 0 to 3),
check it, and roll back if needed. Worked times use the example config's
boundaries: **Sunrise 05:30, Day 08:00, Afternoon 14:00, Sunset 16:00, Night 20:30,
Sleep 22:00**, with heartbeats every 10 minutes. The example cutover deploys at
15:40 for the **16:00 Sunset** boundary; shift every time by the same amount to use
another boundary.

Commands assume you are in the repository folder on the Docker host, with `.env`
holding `AC_HA_URL` and `AC_HA_TOKEN` (a token for an admin user). Entity ids are
the example config's: rooms `main_room`, `main_bedroom`, `baby_room`, each with one
unit, `main_room_ac`, `main_bedroom_ac` and `baby_room_ac`. Substitute your own
room and unit ids where they differ.

## Before you start

- Pick a time when someone is home for the boundary and an hour after it.
- Don't let a section boundary fall between stopping v1 and deploying v2. In the
  example, nothing crosses 15:00 to 15:40.
- From the moment v2's container starts until you deploy, v1's automations are
  still in Home Assistant and in charge. v2's container no longer raises the hold
  when someone reacts, so keep that window short (the timeline below keeps it
  under 30 minutes).

## 1. Stop v1 and back up its data (15:00)

```bash
docker compose down
sudo tar czpf ~/adaptive-climate-v1-data-$(date +%Y%m%d-%H%M).tgz data
ls -l ~/adaptive-climate-v1-data-*.tgz        # note the file name: rollback restores it
```

`data/` holds the config, the deploy manifest, the SQLite database and the CSV
archive. `sudo` and `p` keep the ownership (uid 10001) the container needs.

## 2. Pull v2 and build (15:05)

```bash
git fetch origin
git checkout v2
git pull origin v2
git log --oneline -1                          # the commit you are deploying
docker compose build
```

## 3. Start v2 and confirm health (15:15)

```bash
docker compose up -d
docker compose ps                             # STATUS should become "healthy" within ~1 min
curl -s localhost:8098/healthz; echo
docker compose logs adaptive-climate | grep -i -E "migrat|runtime started|error"
ls -l data/db/
```

Expect:

- `/healthz`: `"status": "ok"`, `"idle": false`, `"ha_connected": true`,
  `"error": null`.
- The log line `migrated /data/db/adaptive_climate.db from storage version 0 to 2
  (backup /data/db/adaptive_climate.db.v1-backup-<timestamp>)`, and that backup
  file in `data/db/`. The migration is one checked transaction per step; if one
  fails it rolls back, the database is untouched, and `/healthz` reports the
  error with the container idle. In that case, stop here and roll back (step 9).
- `runtime started (3 room(s) live)`.

In the UI at `http://<host>:8098`:

- **Log** tab, category *storage*: "database migrated from storage version 0 to 2;
  backup at ...".
- Straight away, category *analysis*: "replayed almanac for Main Room (startup):
  N section(s)", one per room, before any crossover. Phase 0 emptied the almanac;
  this refills it from history and pushes it to HA.
- **Almanac** tab: for each room, the **Sunset** section should list every unit with
  a `setpoint_applied`. A unit missing there has no seed for Sunset yet: at 16:00
  the scene will leave it alone (it stays `manual`), and it starts at the next
  boundary after a Sunset heartbeat seeds it.

## 4. Doctor preflight (15:20)

```bash
docker compose run --rm \
  -e AC_HA_URL -e AC_HA_TOKEN -e AC_HA_VERIFY_SSL \
  -e AC_CONFIG=/data/config/config.json \
  adaptive-climate python tools/doctor.py --v2-preflight
```

Everything should be a tick, ending with the four preflight items:

- `input_datetime: set_datetime with a timestamp round-trips`
- `input_number (0-100, step 0.5, as ac_base_*): accepts 0 and 23.5`
- `template API: timestamp attribute, .attributes.get, now()` (its detail shows the
  clock skew between the host and HA; under a few seconds is normal)
- `throwaway helpers deleted, nothing left behind`

The preflight creates `input_datetime.ac_preflight_since` and
`input_number.ac_preflight_base` and deletes them again, even when a step fails.
If any item fails, **don't deploy**: v2's helpers or templates depend on exactly
that behaviour. If the last item fails, delete any `ac_preflight_*` helper by hand
(Settings → Devices & services → Helpers).

## 5. Deploy, and turn off any hold (15:40, about 20 minutes before 16:00)

UI: **Config** → **Check deploy** (a dry run, no writes), then **Deploy to Home
Assistant**. Or:

```bash
curl -s localhost:8098/api/deploy/check | python3 -m json.tool
curl -s -X POST localhost:8098/api/deploy | python3 -m json.tool
```

For the example config the report reads `"ok": true` with:

- `helpers_created`: 18, six per unit (`ac_state_`, `ac_base_`, `ac_offset_`,
  `ac_since_`, `ac_grace_until_`, `ac_unblocked_`)
- `helpers_removed`: the three `input_datetime.ac_correction_started_<room>`
- `automations_written`: 27 (6 scenes, maintenance and watchdog per room, and the
  new `ac_manual_<room>_<unit>` per unit)
- `notes`: one line per room whose `input_boolean.ac_hold_<room>` is on.

**Turn off every hold the notes name** (Settings → Devices & services → Helpers →
`ac_hold_<room>`, or the toggle in Developer tools → States). v1 raised the hold on
every reaction and its scenes cleared it; in v2 it is a manual pause that nothing
clears, so a hold left on keeps that room paused indefinitely.

Deploying sends no command to any unit. New units start in `manual` (hands off),
so v2 control begins for each room at its next section change, 16:00 here.
Developer tools → States, filter `ac_state_`: all three should read `manual`.
Before 16:00 nothing should change. The 15:50 maintenance run does nothing for a
unit in `manual`.

## 6. At the boundary (16:00 to 16:15)

The container fires each room's **Sunset** scene within about 20 seconds of 16:00.

**Each unit leaves `manual`.** Developer tools → States, per unit:

| Helper | Expect just after 16:00 |
| --- | --- |
| `input_select.ac_state_main_room_main_room_ac` | `cool` (or `config_off` where a scene forces the unit off; `manual` only if Sunset has no `setpoint_applied` for it) |
| `input_number.ac_base_main_room_main_room_ac` | the Almanac tab's Sunset `setpoint_applied` for the unit |
| `input_number.ac_offset_main_room_main_room_ac` | `0.0` |
| `input_datetime.ac_since_main_room_main_room_ac` | about 16:00 today |
| `input_datetime.ac_grace_until_main_room_main_room_ac` | about 16:00 today (cleared) |
| `input_boolean.ac_unblocked_main_room_main_room_ac` | off |
| `input_boolean.ac_active_main_room` | off again within seconds |

The unit itself should be in cool, fan low, at the base setpoint (clamped to its
range; never below 18 in the baby room).

**HA traces.** Settings → Automations & scenes:

- **AC Main Room — Sunset** → ⋮ → Traces: one run at ~16:00. The steps raise
  `ac_active_main_room`, select `sunset`, write the six helpers, send
  `set_hvac_mode cool`, `set_temperature`, `set_fan_mode low`, and lower the guard.
  In the trace's *Changed variables*, `unit_main_room_ac` has `go: true`,
  `state: cool` and a `cmd`.
- **AC Main Room — maintenance**, the 16:10 run: no errors. `votes` shows
  `met`, `tally`, `required`, `direction`; `unit_main_room_ac` has `go: false`
  unless the room is too warm or too cold (then `boost` or `rest`, with the guard
  raised and a command sent).

**Event log.** Log tab: category *crossover*, "Main Room -> sunset" (and the other
two rooms) at ~16:00, then *heartbeat* entries every 10 minutes.

## 7. Live test (16:20, while a unit is cooling)

On the remote, change the Main Room unit's setpoint by one or two degrees, say 24
to 23. Then:

- **Straight away**, in Developer tools → States:
  - `ac_base_main_room_main_room_ac` becomes 23.0;
  - `ac_state_main_room_main_room_ac` stays (or becomes) `cool`;
  - `ac_offset_…` is 0.0;
  - `ac_since_…` is the time of your change;
  - `ac_grace_until_…` is 30 minutes later.
- **AC Main Room — manual change (Main Room AC)** → Traces: one run at the change
  whose conditions passed, with `kind: setpoint` and `ours: false` in
  `unit_main_room_ac`. A temperature-only change fires just the temperature
  trigger. A remote that also switches mode can fire twice; the second run finds
  the change is now ours and stops at its conditions.
- **Within about 2 minutes** (the 120-second reaction window), the Log shows
  category *reactive*: "Main Room: a person set main_room_ac 24.0 -> 23.0",
  followed by "replayed almanac for Main Room (reaction)". The Almanac tab's
  Sunset setpoint moves halfway toward 23, and the section reads learned.
- **About 65 minutes later**, "replayed almanac for Main Room (comfort window)":
  the comfort readings update.
- For 30 minutes (the grace period), maintenance keeps the unit in `cool` whatever
  the votes say.

Optional second test: turn the unit off on the remote. `ac_state` becomes
`user_off` straight away, a *reactive* "turned main_room_ac off" appears within 2
minutes, and maintenance leaves the unit off until 20:30 (Night). Turn it back on
to see `cool`, `ac_unblocked` on, and a fresh grace period.

## 8. The first 24 hours

| When | What to check |
| --- | --- |
| Every boundary: 20:30 Night, 22:00 Sleep, 05:30 Sunrise, 08:00 Day, 14:00 Afternoon, 16:00 Sunset | A *crossover* event per room. `ac_offset_*` back to 0, `user_off` and `manual` units back in `cool` (or `rest`, `config_off`). The scene trace has no errors |
| Every 10 minutes | Maintenance traces have no template errors |
| Through the evening and night | Boosts end within 60 minutes or when the room settles, then another may start a degree lower. Rests wake when the room warms back to comfort. No unit flips state more often than every 10 to 20 minutes |
| Any time | `ac_state` becomes `manual` or `user_off` only when someone actually touched that unit. If it changes with nobody touching it, note the time and open the manual-change trace at that time (`kind`, `ours`): a late echo of our own command got through |
| Any time | *reactive* events appear only for real changes on a remote, app or HA |
| Any time | `ac_active_<room>` is on for seconds only. A **guard watchdog** run means a guard stuck for 5 minutes |
| Any time | `ac_hold_<room>` stays off unless you paused the room |
| 00:15 | The nightly replay for each room (*analysis*) |
| Baby room | Its unit is never commanded below 18 |
| Container | `docker compose ps` healthy. `docker compose logs adaptive-climate` has no tracebacks. Settings → System → Logs has no "Error rendering" or "Template variable error" lines for `ac_` automations |

## 9. Rollback

Do this if anything above goes wrong in a way you don't want to debug live.

```bash
docker compose down
sudo tar czpf ~/adaptive-climate-v2-data-$(date +%Y%m%d-%H%M).tgz data   # keep v2's data for later
```

**Delete what v1's deploy won't remove.** This was checked by running v1.0-poc's own
deploy code against a stub holding a v2 deployment:

- v1's deploy **does** remove the v2-only `input_select.ac_state_*`,
  `input_boolean.ac_unblocked_*`, `input_datetime.ac_since_*` and
  `input_datetime.ac_grace_until_*`. It also re-creates
  `input_datetime.ac_correction_started_<room>`.
- It **does not** remove:
  - the `input_number` helpers, a domain v1 never lists;
  - the `ac_manual_*` automations, because the restored v1 manifest
    (`data/config/deploy_manifest.json`) predates them.

Delete these by hand, one of each per unit:

```bash
set -a; . ./.env; set +a
for id in ac_manual_main_room_main_room_ac ac_manual_main_bedroom_main_bedroom_ac \
          ac_manual_baby_room_baby_room_ac; do
  curl -s -X DELETE -H "Authorization: Bearer $AC_HA_TOKEN" \
       "$AC_HA_URL/api/config/automation/config/$id"; echo " $id"
done
```

(or Settings → Automations & scenes → search "manual change" → ⋮ → Delete, for
each), and these six helpers (Settings → Devices & services → Helpers → search
`ac_base_` and then `ac_offset_` → open each → ⚙ → Delete):

```
input_number.ac_base_main_room_main_room_ac
input_number.ac_base_main_bedroom_main_bedroom_ac
input_number.ac_base_baby_room_baby_room_ac
input_number.ac_offset_main_room_main_room_ac
input_number.ac_offset_main_bedroom_main_bedroom_ac
input_number.ac_offset_baby_room_baby_room_ac
```

**Restore v1 and redeploy it:**

```bash
sudo rm -rf data
sudo tar xzpf ~/adaptive-climate-v1-data-<the stamp from step 1>.tgz
git checkout v1.0-poc
docker compose build
docker compose up -d
docker compose ps; curl -s localhost:8098/healthz; echo
```

Then in the v1 UI: **Config** → **Deploy to Home Assistant**. Expect
`helpers_created` to be the three `ac_correction_started_*`, `helpers_removed` the
twelve v2 helpers listed above, and `automations_written` 24. Settings →
Automations should show no `ac_manual_*`, and Helpers no `ac_state_`, `ac_base_`,
`ac_offset_`, `ac_since_`, `ac_grace_until_` or `ac_unblocked_` entries. v1's next
scene clears any hold.

The restored database is v1's, from step 1. The reactions recorded while v2 ran
stay only in the v2 tarball, which v1 can't read (its database is at storage
version 2).

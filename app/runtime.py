"""
Adaptive Climate - runtime.

Owns the moving parts: the HA connection, section scheduling, heartbeat
observation, reactive detection, nightly analysis, almanac publication,
and the leak latch.

Three things worth knowing (carried from Light):

  * Scheduling is level-triggered, not edge-triggered.
  * Two booleans, two meanings: ac_active_<room> (an AC automation is
    mid-change; lives seconds) vs ac_hold_<room> (a human intervened;
    maintenance stands down until the next crossover; lives hours).
  * Heartbeats defer, they do not skip: a heartbeat during a guard window
    waits for the guard to clear rather than sampling a transition.

New for climate:
  * A heartbeat records every sensor's temperature and every unit's full
    climate state (setpoint, hvac_mode, fan_mode, ac_state).
  * Reactive detection snapshots every sensor at reaction time so the
    analyser can learn per-sensor trust.
  * The leak latch: while a unit's leak boolean is on, it runs Leak mode;
    release requires (no leak) AND (user confirmed).

STATUS: skeleton.
"""

from __future__ import annotations

SCHEDULER_TICK_SECONDS = 20
PROVISIONAL_SAMPLES = 3
PROVISIONAL_SPACING_SECONDS = 40


class Runtime:
    def __init__(self, config, store) -> None:
        self.config = config
        self.store = store
        self.rooms: dict = {}
        self.events_seen = 0
        self.last_event_at = None

    async def start(self): raise NotImplementedError("Phase 10")
    async def stop(self):  raise NotImplementedError("Phase 10")
    async def run_analysis(self): raise NotImplementedError("Phase 10")

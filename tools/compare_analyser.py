"""
Adaptive Climate - analyser diff harness.

Runs two analyser versions against the same heartbeat/reactive CSVs and
diffs the resulting almanacs (setpoints, and per-sensor comfort/band/
trust), so a change to the learning maths can be inspected before it
ships.

    python tools/compare_analyser.py heartbeat.csv

STATUS: skeleton (Phase 4).
"""
import sys
if __name__ == "__main__":
    print("[skeleton] analyser comparison not yet implemented (Phase 4).")
    sys.exit(0)

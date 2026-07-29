"""
Adaptive Climate - connection doctor.

Points at a real Home Assistant and reports whether everything the runtime
depends on works there: REST reachable + token accepted; helper creation
over WebSocket; automation creation over REST; and that at least one
climate entity exists. NO sun checks (climate has no sun-relative
sections).

    export AC_HA_URL=http://192.168.1.251:8123
    export AC_HA_TOKEN=...
    PYTHONPATH=. python tools/doctor.py

STATUS: skeleton.
"""
import os, sys

def main() -> int:
    url = os.environ.get("AC_HA_URL")
    token = os.environ.get("AC_HA_TOKEN")
    if not url or not token:
        print("Set AC_HA_URL and AC_HA_TOKEN first.")
        return 2
    print(f"\nAdaptive Climate doctor -> {url}\n" + "=" * 62)
    print("[skeleton] checks not yet implemented (Phase 7).")
    return 0

if __name__ == "__main__":
    sys.exit(main())

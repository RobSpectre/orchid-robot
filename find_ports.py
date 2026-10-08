#!/usr/bin/env python3
"""Show which USB serial port is the SO101 follower and which is the leader (read-only).

    python find_ports.py          # one line per arm: port, role, bus voltage, motor IDs
    python find_ports.py --json   # the same as JSON

Uses the console's discovery (orchid_demo/discovery.py): the follower runs on ~12 V, the leader on ~5 V.
Exits 1 when no arm answers.
"""
from __future__ import annotations

import argparse
import json
import sys

from orchid_demo.discovery import discover_arms


def describe(arm: dict) -> str:
    voltage = "voltage unavailable" if arm["voltage"] is None else f"{arm['voltage']:.1f} V"
    note = "" if len(arm["motor_ids"]) == 6 else f"  (expected 6 motors, {len(arm['motor_ids'])} answered)"
    return f"{arm['port']}: {arm['role'] or 'unknown':<8} {voltage}  motors {arm['motor_ids']}{note}"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--json", action="store_true", help="print JSON")
    as_json = parser.parse_args().json
    found = discover_arms()
    for warning in found["warnings"]:
        print(warning, file=sys.stderr)
    if as_json:
        print(json.dumps(found["arms"], indent=2))
    elif found["arms"]:
        print("\n".join(describe(arm) for arm in found["arms"]))
    else:
        print("No arm answered on any USB serial port.")
    sys.exit(0 if found["arms"] else 1)


if __name__ == "__main__":
    main()

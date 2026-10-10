"""Set Orchid's voicing dial to a position, by turning it with the Chord Arm and listening to where it lands.

Orchid reports its voicing dial on channel-1 CC115 as an absolute position, one step per click, and Orchid Studio's
key monitor records it (status key_monitor.last_voicing, and each dial play's key check: clicks moved and the value
it reached). The Chord Arm turns the dial with its taught voicing.cw / voicing.ccw motions; a turn's angle is chosen
per turn (keep_default: the taught default angle is left alone).

Closed loop: turn toward the target, see where the dial landed, and size the next turn from how many clicks a degree
gave (learned and remembered, as is which direction raises the value). From an unknown position the first turn finds
it. At most MAX_TURNS turns; two turns that do not click stop it (the gripper is not catching the dial).
"""
from __future__ import annotations

import json
from pathlib import Path

MAX_TURNS, MIN_DEG, MAX_DEG = 8, 5.0, 90.0
DEFAULTS = {"up": "voicing.cw", "clicks_per_degree": 0.1, "first_degrees": 20.0}  # a guess until a turn is heard
OTHER = {"voicing.cw": "voicing.ccw", "voicing.ccw": "voicing.cw"}
VALUE_RANGE = (0, 127)  # CC115


def load(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        data = {}
    return {**DEFAULTS, **{k: v for k, v in data.items() if k in DEFAULTS}}


def save(path: Path, learned: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(learned, indent=1))
    temp.replace(path)


def set_voicing(target: int, current, turn, learned: dict) -> dict:
    """Turn the dial until it reports target. current: its last reported position (None: unknown). turn(control,
    degrees) -> (clicks moved, position reached or None); degrees is signed as the direction's taught turn (cw +).
    learned is updated in place (clicks per degree, which direction raises the value)."""
    turns, misses = [], 0
    while current != target and len(turns) < MAX_TURNS:
        need = None if current is None else target - current
        control = learned["up"] if need is None or need > 0 else OTHER[learned["up"]]
        degrees = learned["first_degrees"] if need is None else \
            max(MIN_DEG, min(MAX_DEG, abs(need) / max(1e-3, learned["clicks_per_degree"])))
        clicks, value = turn(control, degrees if control == "voicing.cw" else -degrees)
        if value is not None and current is not None:
            clicks = value - current  # positions are absolute: trust them over a click count
        turns.append({"control": control, "degrees": round(degrees, 1), "clicks": clicks, "value": value})
        if value is not None and current is None and not clicks:  # the first report only says where the dial is
            current = value
            continue
        if not clicks:
            misses += 1
            if misses >= 2:
                break
            continue
        misses = 0
        rate = abs(clicks) / degrees
        learned["clicks_per_degree"] = round(0.5 * learned["clicks_per_degree"] + 0.5 * rate, 4)
        raised = clicks > 0
        if (control == learned["up"]) != raised:  # this direction moves the value the other way: remember
            learned["up"] = OTHER[learned["up"]]
        current = value if value is not None else (None if current is None else current + clicks)
    reached = current == target
    reason = None if reached else ("the dial did not click on two turns: the gripper is not catching it" if misses >= 2 else
                                   f"still at {current} after {len(turns)} turns")
    return {"target": target, "reached": current, "ok": reached, "turns": turns, **({"reason": reason} if reason else {})}

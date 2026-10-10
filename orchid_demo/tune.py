"""Correct keys: press each key the way it is played, hear which key Orchid reports, and move the taught motion until
it presses the right key, cleanly, every time.

0. Straighten  (no press) a stroke that drifts sideways along the row (touch or hover more than STRAIGHT_MM to the side
           of the press, in the keyboard model: keyboard.py) is rebuilt to come straight down onto the press.
1. Test    the key played TEST_PRESSES times in a row from home at the operator's Arm speed and press hardness, as in
           playing. Every press is heard: clean, a neighbour, two keys at once, a double trigger, or nothing.
2. Move    a neighbour sounding moves hover, touch and press together, away from it (keyboard.Keyboard.correction):
           along the row toward a black key that hit a white neighbour, or two white keys; forward, onto the wide front,
           for a white key that hit a black key. By half the way between the two keys for every press it took (two
           keys at once count half), on the first move at least as far as the keyboard pattern says the key sits toward
           that neighbour, and at most half a key's width a round (MAX_STEP_KEYS). Test again.
3. Depth   the right key every time but missed or double-triggered: find the trigger with gentle presses (FIND_SPEED,
           FIND_HARDNESS; FINDS presses agreeing within AGREE_DEG; no note: STEP_DEG deeper, never LIMIT_DEG past what
           was taught), set touch TOUCH_MARGIN before it and press PRESS_DEPTH past it; later rounds step those by
           MARGIN_STEP (double triggers) or DEPTH_STEP (misses). A neighbour sounding while finding moves away from it
           (as half a test's worth, FIND_NEIGHBOUR_SHARE) and tests again. Test again.
Done when a test is TEST_PRESSES clean presses out of TEST_PRESSES, within MAX_ROUNDS tests. A key further off than its
neighbours, a move over a key's width (or MAX_SHIFT_MM) from where it was taught, or a move the arm could only make by
swinging (LINK_FACTOR, MAX_JOINT_DEG) stops for a re-teach.

Depth distances are the largest joint change (deg); sideways ones are fingertip millimetres from the arm model (exact
for small moves; only its absolute position is uncertain), keeping the finger's pitch. Gripper opening and wrist roll
keep their taught values. Nothing is saved until a test passes; the taught points are kept.
"""
from __future__ import annotations

import math

FIND_SPEED, FIND_HARDNESS = 0.5, 0.25
FINDS, MAX_FINDS, AGREE_DEG = 2, 5, 0.5
STEP_DEG, LIMIT_DEG = 0.75, 3.0
TOUCH_MARGIN, MARGIN_STEP, MAX_MARGIN = 1.0, 0.5, 2.5
PRESS_DEPTH, DEPTH_STEP, MAX_DEPTH = 0.75, 0.25, 1.5
TEST_PRESSES, MAX_ROUNDS = 5, 6
PAUSE_S = 1.5
JOINTS = ("shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_flex")
STRAIGHT_MM = 1.5
MAX_SHIFT_MM = 20.0  # never more than this, nor more than one key's width (the fitted pitch), from where it was taught
MAX_STEP_KEYS = 0.5  # a round moves at most half a key's width; the next test shows how much further to go
FIND_NEIGHBOUR_SHARE = 0.5  # a neighbour sounding on one gentle, deeper press: the finger is at its edge
# Sanity bounds on a sideways move (the fingertip itself is placed exactly): no part of the arm may move more than
# LINK_FACTOR times the fingertip's move plus LINK_SLACK_MM (no swinging the arm somewhere else to get there), and no
# joint more than MAX_JOINT_DEG. Near the edge of its reach (C on the right mount) a key's width takes about 25 deg of
# elbow while every link moves under twice as far as the finger.
LINK_FACTOR, LINK_SLACK_MM, MAX_JOINT_DEG = 3.0, 5.0, 30.0
MIN_DIRECTION_DEG = 0.3


def tally(steps: list, target: str) -> dict:
    """What a test's presses heard: clean, missed, repeated (the key twice), and each neighbour's share of the presses
    (a neighbour alone counts 1, two keys at once 1/2 each)."""
    count = {"clean": 0, "missed": 0, "repeated": 0, "wrong": 0, "double": 0}
    neighbours = {}
    for step in steps:
        status = step["status"]
        count["clean" if status == "ok" else status if status in count else "missed"] += 1
        others = [n for n in step.get("heard") or step.get("notes") or [] if n != target]
        if status in ("wrong", "double") and others:
            weight = 1.0 / len(others) / (2 if target in (step.get("heard") or []) else 1)
            for name in others:
                neighbours[name] = neighbours.get(name, 0.0) + weight / len(steps)
    return {**count, "neighbours": {k: round(v, 3) for k, v in neighbours.items()}, "presses": len(steps)}


def describe(tally: dict, target: str) -> str:
    parts = [f"{tally['clean']}/{tally['presses']} clean"]
    for name, share in tally["neighbours"].items():
        parts.append(f"{name} {round(share * tally['presses'], 1):g}×")
    if tally["missed"]:
        parts.append(f"{tally['missed']} missed")
    if tally["repeated"]:
        parts.append(f"{tally['repeated']} sounded twice")
    return f"{target}: " + ", ".join(parts)


def gap(a, b):
    return max(abs(a[j] - b[j]) for j in JOINTS)


def goal_at(frames, t):
    """The commanded goal at recording time t (linear between frames)."""
    if t <= frames[0]["t"]:
        return dict(frames[0]["goal"])
    for before, after in zip(frames, frames[1:]):
        if after["t"] >= t:
            u = (t - before["t"]) / ((after["t"] - before["t"]) or 1)
            return {k: before["goal"][k] + (after["goal"][k] - before["goal"][k]) * u for k in before["goal"]}
    return dict(frames[-1]["goal"])


def around(frames, trigger_t, bottom_t, margin, depth):
    """Touch and press goals on the commanded path: margin deg before the trigger, depth deg past it (extended along
    the direction of travel when the taught press is closer than that)."""
    trigger = goal_at(frames, trigger_t)
    before = [f["t"] for f in reversed(frames) if f["t"] < trigger_t]
    touch = _crossing(frames, trigger, margin, [trigger_t] + before) or dict(frames[0]["goal"])
    after = [f["t"] for f in frames if trigger_t < f["t"] <= bottom_t + 1e-9]
    press = _crossing(frames, trigger, depth, [trigger_t] + after)
    if press is None:
        travel = {j: trigger[j] - touch[j] for j in JOINTS}
        size = max(abs(v) for v in travel.values())
        if size < 1e-6:
            raise ValueError("The press path does not move; re-teach this key with the leader.")
        press = {**trigger, **{j: trigger[j] + travel[j] * depth / size for j in JOINTS}}
    return trigger, dict(touch), dict(press)


def _crossing(frames, trigger, distance, times):
    """Walking the path through times (away from the trigger), the pose exactly distance deg from the trigger."""
    for near, far in zip(times, times[1:]):
        if gap(goal_at(frames, far), trigger) >= distance:
            for _ in range(40):  # the distance grows monotonically along this short straight piece
                mid = (near + far) / 2
                near, far = (mid, far) if gap(goal_at(frames, mid), trigger) < distance else (near, mid)
            return goal_at(frames, far)
    return None


def point(taught, goal):
    """A taught point moved to goal: only the four joints change; gripper and roll stay as taught."""
    return {**taught, "goal": {**taught["goal"], **{j: round(goal[j], 3) for j in JOINTS}}}


def deeper(points, taught_press):
    """No note while finding: press STEP_DEG further along the press direction, never LIMIT_DEG past the taught press."""
    down = {j: points["press"]["goal"][j] - points["touch"]["goal"][j] for j in JOINTS}
    if max(abs(v) for v in down.values()) < MIN_DIRECTION_DEG:
        down = {j: points["touch"]["goal"][j] - points["hover"]["goal"][j] for j in JOINTS}
    size = max(abs(v) for v in down.values())
    if size < MIN_DIRECTION_DEG:
        return None, "it did not sound and its points give no press direction"
    goal = {j: points["press"]["goal"][j] + down[j] * STEP_DEG / size for j in JOINTS}
    if beyond(goal, taught_press, down) > LIMIT_DEG + 1e-9:
        return None, f"it still did not sound {LIMIT_DEG:g}° past where it was taught"
    return {**points, "press": point(points["press"], goal)}, f"press {STEP_DEG:g}° deeper"


def beyond(goal, taught_press, direction):
    """How far goal lies past the taught press along direction (deg, largest joint); 0 when it is shallower."""
    size = math.sqrt(sum(direction[j] ** 2 for j in JOINTS)) or 1
    along = sum((goal[j] - taught_press["goal"][j]) * direction[j] for j in JOINTS) / size
    return max(0.0, along) * max(abs(direction[j]) for j in JOINTS) / size

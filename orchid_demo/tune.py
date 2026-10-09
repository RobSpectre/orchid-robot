"""Key calibration regime: find each key's trigger point by listening, set touch and press around it, verify at playing speed.

1. Find    gentle presses (FIND_SPEED, FIND_HARDNESS). The note-on time Orchid reports, placed on the commanded
           path of that press, is the key's trigger pose. FINDS presses must agree within AGREE_DEG.
           No note: press a little deeper (STEP_DEG a time, never more than LIMIT_DEG past the taught press).
2. Set     touch = the point on that path TOUCH_MARGIN deg before the trigger; press = PRESS_DEPTH deg past it.
           Every key then gets the same room to approach and the same pressure. Measured on the commanded path,
           so the gap between command and encoder is the same as during playback.
3. Verify  plays at the operator's Arm speed and press hardness until VERIFY_PASSES clean presses in a row.
           Pressed twice: MARGIN_STEP more room above the trigger. No note: DEPTH_STEP deeper. Wrong key: stop.

Distances are the largest joint change (deg). Gripper opening and wrist roll keep their taught values.
Nothing is saved until verify passes; the taught points are kept.
"""
from __future__ import annotations

import math

FIND_SPEED, FIND_HARDNESS = 0.5, 0.25
FINDS, MAX_FINDS, AGREE_DEG = 2, 5, 0.5
STEP_DEG, LIMIT_DEG = 0.75, 3.0
TOUCH_MARGIN, MARGIN_STEP, MAX_MARGIN = 1.0, 0.5, 2.5
PRESS_DEPTH, DEPTH_STEP, MAX_DEPTH = 0.75, 0.25, 1.5
VERIFY_PASSES, MAX_VERIFY = 2, 6
PAUSE_S = 1.5
JOINTS = ("shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_flex")
MIN_DIRECTION_DEG = 0.3


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

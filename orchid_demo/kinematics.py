"""SO101 forward kinematics on the registration plate, for the two-arm chord clearance check.

The same URDF transforms as static/arm-model.js (static/arm-geometry.js), in plate millimetres (layout.MOUNTS). Joint
zeros come from each arm's hand-centred calibration midpoints, so a modelled position can be off by about 2 cm: callers
compare against a clearance that covers the links' thickness plus that error, and refuse rather than guess.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
import re

from . import layout

_GEOMETRY = json.loads(re.search(r"const joints = (\[.*?\]);", (Path(__file__).with_name("static") / "arm-geometry.js")
                                 .read_text(), re.S).group(1))
JOINTS = ("shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_flex", "wrist_roll", "gripper")
# Centre-line distance below which a chord is refused. The taught chord-button motions were checked by eye to stay clear
# of Orchid and the Keys Arm, where the model puts the closest pair (Sus with C) 56 mm apart; this floor only catches a
# pose taught into the other arm's way (e.g. after a re-teach). The links are about 45 mm wide and the model can be 2 cm off.
CLEARANCE_MM = 40.0


def _matmul(a, b):
    return [[sum(a[r][k] * b[k][c] for k in range(4)) for c in range(4)] for r in range(4)]


def _transform(xyz, rpy):
    r, p, y = rpy
    cr, sr, cp, sp, cy, sy = math.cos(r), math.sin(r), math.cos(p), math.sin(p), math.cos(y), math.sin(y)
    return [[cy * cp, cy * sp * sr - sy * cr, cy * sp * cr + sy * sr, xyz[0]],
            [sy * cp, sy * sp * sr + cy * cr, sy * sp * cr - cy * sr, xyz[1]],
            [-sp, cp * sr, cp * cr, xyz[2]], [0, 0, 0, 1]]


def _point(frame, p=(0.0, 0.0, 0.0)):
    return tuple(frame[i][0] * p[0] + frame[i][1] * p[1] + frame[i][2] * p[2] + frame[i][3] for i in range(3))


def urdf_angles(pose: dict, calibration: dict) -> dict:
    """LeRobot degrees (gripper 0..100) -> the model's joint radians, as telemetry.pose_angles does for raw ticks."""
    angles = {}
    for name in JOINTS:
        low, high = calibration[name]["range_min"], calibration[name]["range_max"]
        if name == "gripper":
            angles[name] = -0.174533 + max(0.0, min(100.0, pose[name])) / 100 * (1.74533 + 0.174533)
        else:
            raw = pose[name] * 4095 / 360 + (low + high) / 2
            angles[name] = (raw - 2047) * math.tau / 4096
    return angles


def skeleton(pose: dict, calibration: dict, arm: str) -> list:
    """The arm's centre line in plate millimetres: shoulder lift, elbow, wrist, wrist roll, fingertip, and jaw tip."""
    angles = urdf_angles(pose, calibration)
    mount = layout.MOUNTS[arm]
    base = _transform([v / 1000 for v in mount["xyz"]], [0.0, 0.0, math.radians(mount["yaw_deg"])])
    frames, points = {"base_link": base}, {}
    for spec in _GEOMETRY:
        origin = _matmul(frames[spec["parent"]], _transform(spec["xyz"], spec["rpy"]))
        q = angles.get(spec["name"], 0.0) if spec["limits"] else 0.0
        frames[spec["child"]] = _matmul(origin, _transform([0, 0, 0], [0, 0, q]))
        points[spec["name"]] = _point(origin)
    mm = lambda p: tuple(v * 1000 for v in p)  # noqa: E731
    line = [mm(points[n]) for n in ("shoulder_lift", "elbow_flex", "wrist_flex", "wrist_roll")]
    tip, jaw = mm(_point(frames["gripper_frame_link"])), mm(_point(frames["moving_jaw_so101_v1_link"], (0, -0.075, 0)))
    return [(line[0], line[1]), (line[1], line[2]), (line[2], line[3]), (line[3], tip), (line[3], jaw)]


def _segment_distance(p1, q1, p2, q2):
    """Shortest distance between segments p1-q1 and p2-q2 (Ericson, Real-Time Collision Detection 5.1.9)."""
    sub = lambda a, b: [a[i] - b[i] for i in range(3)]  # noqa: E731
    dot = lambda a, b: sum(a[i] * b[i] for i in range(3))  # noqa: E731
    d1, d2, r = sub(q1, p1), sub(q2, p2), sub(p1, p2)
    a, e, f = dot(d1, d1), dot(d2, d2), dot(d2, r)
    if a <= 1e-12 and e <= 1e-12:
        s = t = 0.0
    elif a <= 1e-12:
        s, t = 0.0, max(0.0, min(1.0, f / e))
    else:
        c = dot(d1, r)
        if e <= 1e-12:
            t, s = 0.0, max(0.0, min(1.0, -c / a))
        else:
            b = dot(d1, d2)
            denom = a * e - b * b
            s = max(0.0, min(1.0, (b * f - c * e) / denom)) if denom > 1e-12 else 0.0
            t = (b * s + f) / e
            if t < 0:
                t, s = 0.0, max(0.0, min(1.0, -c / a))
            elif t > 1:
                t, s = 1.0, max(0.0, min(1.0, (b - c) / a))
    c1 = [p1[i] + d1[i] * s for i in range(3)]
    c2 = [p2[i] + d2[i] * t for i in range(3)]
    return math.dist(c1, c2)


def distance(a: list, b: list) -> float:
    """Closest approach between two skeletons, in millimetres."""
    return min(_segment_distance(*s, *t) for s in a for t in b)


def clearance(path_a: list, calibration_a: dict, path_b: list, calibration_b: dict) -> float:
    """Closest approach of Keys Arm over every pose of path_a to Chord Arm over every pose of path_b (each a list of
    LeRobot-degree poses), in millimetres. Both paths are taken as happening at once, which is conservative."""
    a = [skeleton(pose, calibration_a, "a") for pose in path_a]
    b = [skeleton(pose, calibration_b, "b") for pose in path_b]
    return min(distance(x, y) for x in a for y in b)


# --- Small fingertip moves, for correcting keys (tune.py): the model's absolute position can be 2 cm off, but a move of
# a few millimetres from a real pose is accurate, because the joint geometry is exact; only the zeros are estimated.
ARM_JOINTS = ("shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_flex")


def fingertip(pose: dict, calibration: dict, arm: str) -> tuple:
    """(tip, wrist) in plate millimetres: the finger's end (between the jaws) and the wrist-rotation joint behind it."""
    wrist, tip = skeleton(pose, calibration, arm)[3]
    return tip, wrist


def joints(pose: dict, calibration: dict, arm: str) -> list:
    """The arm's joint positions and fingertip, plate mm: shoulder lift, elbow, wrist flex, wrist roll, fingertip."""
    seg = skeleton(pose, calibration, arm)
    return [seg[0][0], seg[0][1], seg[1][1], seg[2][1], seg[3][1]]


def pitch(tip, wrist) -> float:
    """How steeply the finger points, in degrees (negative: tip below wrist)."""
    return math.degrees(math.atan2(tip[2] - wrist[2], math.hypot(tip[0] - wrist[0], tip[1] - wrist[1])))


def reach(pose: dict, calibration: dict, arm: str, target, keep_pitch=None, tolerance_mm=0.05) -> dict:
    """pose with its four arm joints changed so the fingertip is at target (plate mm), the finger keeping its pitch
    (keep_pitch, default the pose's own). Wrist roll and gripper stay. Raises ValueError if it cannot get there."""
    tip, wrist = fingertip(pose, calibration, arm)
    want = pitch(tip, wrist) if keep_pitch is None else keep_pitch
    q = dict(pose)

    def error(q):
        tip, wrist = fingertip(q, calibration, arm)
        return [target[0] - tip[0], target[1] - tip[1], target[2] - tip[2], want - pitch(tip, wrist)]
    for _ in range(25):
        e = error(q)
        if max(abs(v) for v in e) < tolerance_mm:
            return q
        columns = []
        for j in ARM_JOINTS:  # numerical Jacobian, 0.01 deg steps
            nudged = error({**q, j: q[j] + 0.01})
            columns.append([(e[i] - nudged[i]) / 0.01 for i in range(4)])
        rows = [[columns[c][r] for c in range(4)] for r in range(4)]
        step = _solve([[sum(rows[k][i] * rows[k][j] for k in range(4)) + (1e-4 if i == j else 0) for j in range(4)]
                       for i in range(4)], [sum(rows[k][i] * e[k] for k in range(4)) for i in range(4)])
        scale = min(1.0, 2.0 / max(1e-9, max(abs(s) for s in step)))  # at most 2 deg a joint per iteration
        q = {**q, **{j: q[j] + s * scale for j, s in zip(ARM_JOINTS, step)}}
    raise ValueError(f"the arm cannot put its finger there (still {max(abs(v) for v in error(q)[:3]):.1f} mm off)")


def _solve(a, b):
    """a x = b for a small square system (Gaussian elimination with partial pivoting)."""
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(m[r][c]))
        m[c], m[p] = m[p], m[c]
        if abs(m[c][c]) < 1e-12:
            raise ValueError("the arm cannot move its finger that way here")
        for r in range(c + 1, n):
            f = m[r][c] / m[c][c]
            m[r] = [x - f * y for x, y in zip(m[r], m[c])]
    x = [0.0] * n
    for r in reversed(range(n)):
        x[r] = (m[r][n] - sum(m[r][k] * x[k] for k in range(r + 1, n))) / m[r][r]
    return x

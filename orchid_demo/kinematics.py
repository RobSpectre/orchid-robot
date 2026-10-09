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

"""Supervised SO101 motion in raw servo ticks: taught-path checks, bounded plans, the Controller that plays
them, and the LeRobot arm adapter.

No force sensor or Cartesian collision model is assumed. A taught path is a short, closely sampled
downstroke with a padded, fixed gripper; retraction reverses that path. Hardware imports are lazy, so tests
work with standard Python.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import math
import time

KEYS = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")
MOTORS = ("shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_flex", "wrist_roll", "gripper")
# STS3215 native position ticks. No LeRobot normalized dataset units are used.
MAX_POINT_GAP = 24       # ~2.1 degrees per arm joint between taught samples
MAX_EXCURSION = 120      # local stroke only, never a cross-key transfer
MAX_CONTACT_EXCURSION = 36
START_TOLERANCE = 6
TRACKING_TOLERANCE = 8
SETTLE_TOLERANCE = 4
GRIPPER_TOLERANCE = 3
PERIOD = 0.05
MAX_IO_TIME = 0.25
MAX_RUN_TIME = 45.0
SPEED = 24.0             # commanded ticks/s, not a force limit
ACCELERATION = 80.0      # commanded ticks/s^2
UNITS = "sts3215_raw_ticks"
AUTO_SAMPLE_GAP = 12
JOINT_MARGIN = 16


class SafetyError(RuntimeError):
    pass


def require(condition, message):
    if not condition:
        raise SafetyError(message)


def stamp():
    return datetime.now(timezone.utc).isoformat()


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


def pose_valid(pose):
    require(isinstance(pose, dict) and set(pose) == set(MOTORS), "Every pose must contain exactly six motor positions.")
    require(all(type(v) is int and 0 <= v <= 4095 for v in pose.values()), "Positions must be integer STS3215 ticks in 0..4095.")


def distance(a, b):
    return max(abs(a[m] - b[m]) for m in MOTORS[:-1])


def validate_entry(entry, complete=False):
    require(isinstance(entry, dict), "This key has not been taught. Run teach first.")
    path = entry.get("path")
    require(isinstance(path, list) and 1 <= len(path) <= 64, "Teach between 1 and 64 local path samples.")
    for pose in path:
        pose_valid(pose)
        require(abs(pose["gripper"] - path[0]["gripper"]) <= GRIPPER_TOLERANCE, "Gripper opening changed; keep the padded jaw fixed.")
        require(distance(path[0], pose) <= MAX_EXCURSION, "Path extends beyond the local key workspace; re-teach closer to the key.")
    for a, b in zip(path, path[1:]):
        require(distance(a, b) <= MAX_POINT_GAP, "Samples are too far apart. Capture smaller steps; do not raise the limit to bypass this check.")
    touch = entry.get("touch_index")
    if touch is not None:
        require(type(touch) is int and 1 <= touch < len(path), "Invalid first-contact sample.")
        for pose in path[touch:]:
            require(distance(path[touch], pose) <= MAX_CONTACT_EXCURSION, "Contact stroke is too large. Recheck geometry and key travel.")
    require(type(entry.get("complete")) is bool, "Missing complete flag.")
    if entry["complete"] or complete:
        require(entry["complete"] and touch is not None and touch < len(path) - 1, "Teach hover, first contact, and just-triggered key before playback.")
        require(distance(path[touch], path[-1]) > 0, "Pressed position must differ from first contact.")
        require(entry.get("padded_tip") is True, "Contact teaching requires a padded contact surface.")
    require(isinstance(entry.get("calibration_sha256"), str) and len(entry["calibration_sha256"]) == 64, "Missing arm calibration fingerprint.")
    require(isinstance(entry.get("lerobot_version"), str), "Missing LeRobot version.")


def blend(a, b, fraction):
    return {m: round(a[m] + (b[m] - a[m]) * fraction) for m in MOTORS}


def plan(entry, fraction=1.0, hover_only=False):
    require(math.isfinite(fraction) and 0 < fraction <= 1, "Press fraction must be in (0, 1].")
    validate_entry(entry, complete=not hover_only)
    path = [dict(p) for p in entry["path"]]
    # All commanded samples keep exactly the hover gripper opening.
    for pose in path:
        pose["gripper"] = path[0]["gripper"]
    if hover_only:
        return path[:1]
    touch = entry["touch_index"]
    remaining = fraction * sum(distance(a, b) for a, b in zip(path[touch:], path[touch + 1:]))
    selected = path[:touch + 1]
    for a, b in zip(path[touch:], path[touch + 1:]):
        length = distance(a, b)
        if length == 0:
            continue
        if remaining >= length:
            selected.append(b)
            remaining -= length
        else:
            if remaining > 0:
                selected.append(blend(a, b, remaining / length))
            break
    return selected


def segment(a, b):
    """Quintic interpolation with zero endpoint velocity/acceleration, fixed-rate samples."""
    delta = distance(a, b)
    duration = max(0.6, 1.875 * delta / SPEED, math.sqrt(5.774 * delta / ACCELERATION))
    count = math.ceil(duration / PERIOD)
    for index in range(1, count + 1):
        u = index / count
        yield blend(a, b, 10 * u**3 - 15 * u**4 + 6 * u**5)


def routine_budget(path, hold, midi=False):
    # Allow a full one-second settle at each endpoint and worst-case start
    # alignment. Reject a predictably overlong path BEFORE enabling torque.
    alignment = dict(path[0])
    alignment["wrist_flex"] += START_TOLERANCE
    return (
        3.0 + len(list(segment(alignment, path[0]))) * PERIOD + 1.0
        + 2 * sum(len(list(segment(a, b))) * PERIOD + 1.0 for a, b in zip(path, path[1:]))
        + hold + (0.5 if midi else 0.0)
    )


class Arm:
    """Minimal bus adapter: connecting only reads; never calls robot.configure()."""

    def __init__(self, port, calibration_path):
        from lerobot.motors import Motor, MotorCalibration, MotorNormMode
        from lerobot.motors.feetech import FeetechMotorsBus

        self.calibration = json.loads(calibration_path.read_text())
        require(set(self.calibration) == set(MOTORS), "Calibration must describe six SO101 motors.")
        for index, motor in enumerate(MOTORS, 1):
            c = self.calibration[motor]
            require(c["id"] == index and 0 <= c["range_min"] < c["range_max"] <= 4095, f"Invalid calibration for {motor}.")
        self.signature = fingerprint(self.calibration)
        self.version = importlib.metadata.version("lerobot")
        self.bus = FeetechMotorsBus(
            port=port,
            motors={m: Motor(i, "sts3215", MotorNormMode.DEGREES) for i, m in enumerate(MOTORS, 1)},
            calibration={m: MotorCalibration(**c) for m, c in self.calibration.items()},
        )

    def open(self, release_only=False, teaching=False):
        self.bus.connect()
        require(all(v >= 80 for v in self.bus.sync_read("Present_Voltage", normalize=False).values()), "Expected a powered 12 V follower; refusing a possible leader/undervoltage arm.")
        if release_only:
            return  # A joint-limit/calibration fault must not prevent supported torque release.
        require(self.bus.is_calibrated, "Motor calibration differs from the saved file; calibrate separately, then re-teach.")
        require(all(v == 0 for v in self.bus.sync_read("Operating_Mode", normalize=False).values()), "All motors must already be in position mode.")
        if teaching:
            # Read-only inspection of a parked arm is allowed near an end stop.
            # Actual capture/commands still use check_pose after supported release.
            issues = self.limit_issues(self.read_raw())
            if issues:
                print("Resting pose is outside the capture margin. Teaching will wait for supported, torque-off repositioning:", flush=True)
                print("  " + "; ".join(issues), flush=True)
        else:
            self.read()

    def limit_issues(self, pose):
        pose_valid(pose)
        issues = []
        for motor, value in pose.items():
            c = self.calibration[motor]
            low, high = c["range_min"] + JOINT_MARGIN, c["range_max"] - JOINT_MARGIN
            if not low <= value <= high:
                issues.append(f"{motor}: measured {value} ticks; permitted {low}..{high} (calibrated {c['range_min']}..{c['range_max']})")
        return issues

    def check_pose(self, pose):
        issues = self.limit_issues(pose)
        require(not issues, "Outside the calibrated working margin: " + "; ".join(issues))

    def read_raw(self):
        """Validated encoder readings for diagnostics; never authorizes a goal."""
        pose = self.bus.sync_read("Present_Position", normalize=False, num_retry=0)
        pose_valid(pose)
        return pose

    def read(self):
        pose = self.read_raw()
        self.check_pose(pose)
        return pose

    def send(self, pose):
        self.check_pose(pose)
        self.bus.sync_write("Goal_Position", pose, normalize=False, num_retry=0)

    def torque_status(self):
        values = self.bus.sync_read("Torque_Enable", normalize=False, num_retry=0)
        require(set(values) == set(MOTORS), "Incomplete torque readback: all six motors must respond.")
        require(all(type(v) is int for v in values.values()), "Invalid torque readback.")
        return values

    def require_torque(self, enabled):
        values = self.torque_status()
        unexpected = {m: v for m, v in values.items() if v != int(enabled)}
        require(not unexpected, f"Expected every motor torque {'on' if enabled else 'off'}; unexpected Torque_Enable values: {unexpected}.")
        return values

    def arm_at_current(self, current):
        self.require_torque(False)
        self.send(current)  # Seed and verify the target BEFORE enabling any motor.
        actual = self.bus.sync_read("Goal_Position", normalize=False, num_retry=0)
        require(actual == current, "Could not verify seeded motor targets; torque remains off.")
        measured = self.read()
        require(max(abs(measured[m] - current[m]) for m in MOTORS) <= 2, "Arm moved while arming. Support it steadily and retry.")
        self.bus.enable_torque(num_retry=0)
        self.require_torque(True)

    def release(self):
        # LeRobot's disable_torque also unlocks EEPROM. Teaching only needs the
        # RAM torque switch, so leave EEPROM lock/protection settings untouched.
        for motor in MOTORS:
            self.bus.write("Torque_Enable", motor, 0, normalize=False, num_retry=0)
        values = self.require_torque(False)
        print_torque(values)

    def close(self):
        if self.bus.is_connected:
            self.bus.disconnect(disable_torque=False)


def stable_pose(arm, sleep=time.sleep):
    samples = []
    for _ in range(3):
        arm.require_torque(False)
        samples.append(arm.read())
        sleep(0.1)
    arm.require_torque(False)
    require(all(max(abs(p[m] - samples[0][m]) for m in MOTORS) <= 2 for p in samples), "Arm moved during capture; support it and capture again.")
    return samples[-1]


class Controller:
    def __init__(self, arm, path, log, midi=None, clock=time.monotonic, sleep=time.sleep, max_run_time=MAX_RUN_TIME):
        self.arm, self.path, self.log, self.midi = arm, path, log, midi
        self.clock, self.sleep = clock, sleep
        self.previous = dict(path[0])
        self.last_tick = None
        self.enabled = False
        self.started = None
        self.max_run_time = max_run_time
        self.last_sample = {}
        self.error_reporter = None

    def record(self, **event):
        self.log.write(json.dumps({"time": self.clock(), **event}, allow_nan=False) + "\n")
        self.log.flush()

    def inside(self, current):
        for m in MOTORS:
            margin = GRIPPER_TOLERANCE if m == "gripper" else TRACKING_TOLERANCE
            require(min(p[m] for p in self.path) - margin <= current[m] <= max(p[m] for p in self.path) + margin, f"{m}: measured position left the taught local envelope.")

    def tick(self, target, stage):
        start = self.clock()
        self.last_sample = {"time": start, "stage": stage, "target": dict(target),
                            "previous_target": dict(self.previous), "command_sent": False}
        require(self.started is None or start - self.started <= self.max_run_time, "Motion time limit exceeded.")
        if self.last_tick is not None:
            require(start - self.last_tick <= MAX_IO_TIME, "Control loop stalled; refusing to catch up with a jump.")
        current = self.arm.read()
        self.last_sample["actual"] = dict(current)
        self.arm.require_torque(True)
        self.last_sample["torque_verified"] = True
        require(self.clock() - start <= MAX_IO_TIME, "Stale position feedback; no new target sent.")
        self.inside(current)
        require(distance(current, self.previous) <= TRACKING_TOLERANCE, "Arm is not following the preceding target.")
        require(abs(current["gripper"] - self.path[0]["gripper"]) <= GRIPPER_TOLERANCE, "Gripper opening changed.")
        require(distance(current, target) <= TRACKING_TOLERANCE, "Next target is too far from measured position.")
        self.arm.send(target)
        self.last_sample.update(command_sent=True, send_returned_at=self.clock())
        require(self.clock() - start <= MAX_IO_TIME, "Motor command took too long.")
        self.previous = dict(target)
        events = self.midi.poll() if self.midi else []
        self.record(stage=stage, actual=current, target=target, midi=events)
        self.last_tick = start
        # Slow I/O stretches the trajectory. Never skip samples to catch up.
        self.sleep(max(0, PERIOD - (self.clock() - start)))
        return current

    def settle(self, target, stage):
        consecutive = 0
        for _ in range(math.ceil(1.0 / PERIOD)):
            current = self.tick(target, stage)
            consecutive = consecutive + 1 if distance(current, target) <= SETTLE_TOLERANCE else 0
            if consecutive >= 3:
                return
        raise SafetyError("Waypoint did not settle within one second.")

    def check_start(self, current):
        self.inside(current)
        require(distance(current, self.path[0]) <= START_TOLERANCE, "Not at the taught hover pose. Position the supported, unpowered arm manually; no entry motion is attempted.")
        require(abs(current["gripper"] - self.path[0]["gripper"]) <= GRIPPER_TOLERANCE, "Set the gripper to its taught opening before arming.")

    def arm_here(self):
        """Hold the supported starting pose without beginning the stroke."""
        current = stable_pose(self.arm, self.sleep)
        self.check_start(current)
        # Set before the call: partial torque enable must also enter fault handling.
        self.enabled = True
        self.arm.arm_at_current(current)
        self.previous = current
        self.last_tick = None
        self.started = None
        return current

    def run(self, hold, already_holding=False):
        require(math.isfinite(hold) and 0 <= hold <= 0.5, "Hold must be between 0 and 0.5 seconds.")
        require(routine_budget(self.path, hold, self.midi is not None) <= MAX_RUN_TIME, "Taught path is too long for one bounded trial. Re-teach a shorter local stroke.")
        if already_holding:
            require(self.enabled, "Controller has not established a hold.")
            # tick checks fresh torque/position feedback and the preceding target.
            current = self.tick(self.previous, "pre_trial_hold")
            self.check_start(current)
        else:
            current = self.arm_here()
        self.started = self.clock()
        message = "stroke starts in 3 seconds" if len(self.path) > 1 else "checking hover for 3 seconds"
        print(f"Holding position. Clear hands from the motion path; {message}.", flush=True)
        for _ in range(math.ceil(3.0 / PERIOD)):
            self.tick(current, "armed_hold")
        for target in segment(current, self.path[0]):
            self.tick(target, "hover_alignment")
        self.settle(self.path[0], "hover")
        for a, b in zip(self.path, self.path[1:]):
            for target in segment(a, b):
                self.tick(target, "down")
            self.settle(b, "down_settle")
        for _ in range(math.ceil(hold / PERIOD)):
            self.tick(self.path[-1], "hold")
        for a, b in zip(reversed(self.path), reversed(self.path[:-1])):
            for target in segment(a, b):
                self.tick(target, "up")
            self.settle(b, "up_settle")
        if self.midi:
            for _ in range(10):
                self.tick(self.path[0], "midi_wait")
            self.midi.verify()
        self.record(stage="complete", midi_verified=self.midi is not None)

    def stop(self):
        """Best-effort measured-position hold; never blind-retract or drop torque."""
        if not self.enabled:
            return
        try:
            start = self.clock()
            current = self.arm.read()
            require(self.clock() - start <= MAX_IO_TIME, "Cannot trust stale feedback during stop.")
            self.inside(current)
            self.arm.send(current)
            print("Stopped issuing the stroke; requested a hold at measured position. Torque has not been disabled; support the arm before release.", flush=True)
        except Exception as exc:
            if self.error_reporter:
                self.error_reporter("hold_failed", exc)
            else:
                print(f"Could not establish a hold: {exc}. Last motor target may remain active. Support the arm and use the hardware power stop.", flush=True)


def print_torque(values):
    for index, motor in enumerate(MOTORS, 1):
        value = values[motor]
        state = "OFF" if value == 0 else "ON" if value == 1 else "UNEXPECTED"
        print(f"  id={index} {motor:14s} Torque_Enable={value} ({state})", flush=True)
    if all(v == 0 for v in values.values()):
        print("All six report torque OFF. This does not remove mechanical gear resistance.", flush=True)


def teaching_entry(arm, contact_ready, fixture_note, hover):
    return {"calibration_sha256": arm.signature, "lerobot_version": arm.version, "taught_at": stamp(), "fixture_note": fixture_note, "padded_tip": contact_ready, "path": [hover], "touch_index": None, "complete": False}


def append_capture(entry, current, force=False, previous_observation=None):
    """Sample the actual hand-guided path; never fill a missing span synthetically."""
    previous = entry["path"][-1]
    observed = previous if previous_observation is None else previous_observation
    pose_valid(observed)
    pose_valid(current)
    motor = max(MOTORS[:-1], key=lambda m: abs(current[m] - observed[m]))
    gap = abs(current[motor] - observed[motor])
    require(gap <= MAX_POINT_GAP,
            f"Samples are too far apart between encoder reads: {motor} moved {gap} ticks "
            f"({observed[motor]} -> {current[motor]}); maximum {MAX_POINT_GAP}. "
            "Nothing saved. Restart with hover just above the key and lower more slowly; --stage-seconds 15 gives more time.")
    # Decimation can skip an 11-tick movement, then see another 14 ticks. The
    # actual gaps (11, 14) are valid even though the saved-to-current gap is 25.
    # Retain the real skipped observation rather than inventing interpolated data.
    bridge = [dict(observed)] if distance(previous, current) > MAX_POINT_GAP else []
    # Validate every observation, even ones that won't become a stored sample.
    candidate = {**entry, "path": entry["path"] + bridge + [current]}
    validate_entry(candidate)
    if current != previous and (force or distance(previous, current) >= AUTO_SAMPLE_GAP):
        entry["path"].extend(bridge)
        entry["path"].append(dict(current))

#!/usr/bin/env python3
"""Teach and preview one supervised SO101 keypress; live playback requires --execute.

No force sensor or Cartesian collision model is assumed. Teach a short, closely
sampled downstroke with a padded, fixed gripper. Retraction reverses that path.
Hardware imports are lazy: init, preview, and tests work with standard Python.
"""

from __future__ import annotations

import argparse
from collections import deque
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import math
from pathlib import Path
import sys
import time
import uuid

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
AUTO_SETTLE_SECONDS = 0.6
AUTO_SETTLE_TIMEOUT = 5.0
JOINT_MARGIN = 16
TEACH_POSITION_TIMEOUT = 60.0


class SafetyError(RuntimeError):
    pass


def require(condition, message):
    if not condition:
        raise SafetyError(message)


def stamp():
    return datetime.now(timezone.utc).isoformat()


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


def empty_store():
    return {"schema_version": 1, "units": UNITS, "keys": dict.fromkeys(KEYS)}


def load_store(path):
    store = json.loads(path.read_text())
    require(store.get("schema_version") == 1 and store.get("units") == UNITS, "Unsupported key file format/units.")
    require(isinstance(store.get("keys"), dict) and set(store["keys"]) == set(KEYS), "Key file must contain exactly 12 key slots: C through B.")
    return store


def save_store(path, store):
    """Preserve the previous calibration, then replace atomically on this filesystem."""
    data = json.dumps(store, indent=2, allow_nan=False) + "\n"
    if path.exists():
        backup = path.with_name(f"{path.name}.{uuid.uuid4().hex}.bak")
        with backup.open("xb") as output:
            output.write(path.read_bytes())
        print(f"Previous key map: {backup}")
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(data)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


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


class MidiMonitor:
    """Optional input only. No MIDI notes are sent to Orchid."""

    def __init__(self, name, note, channel):
        import mido
        require(name in mido.get_input_names(), "MIDI input name must exactly match midi-ports output.")
        self.port = mido.open_input(name)
        self.note, self.channel = note, channel - 1
        self.events = []
        self.poll()  # Discard queued events from before this attempt.
        self.events.clear()

    def poll(self):
        events = []
        for msg in self.port.iter_pending():
            if msg.type in ("note_on", "note_off") and msg.channel == self.channel:
                event = {"type": "note_on" if msg.type == "note_on" and msg.velocity else "note_off", "note": msg.note, "velocity": msg.velocity, "time": time.monotonic()}
                self.events.append(event)
                events.append(event)
        return events

    def verify(self):
        types = [(e["type"], e["note"]) for e in self.events]
        require(types == [("note_on", self.note), ("note_off", self.note)], f"MIDI result failed: expected one note-on/off for {self.note}, received {types}. No retry was attempted.")

    def close(self):
        self.port.close()


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


def require_human(text, word):
    require(sys.stdin.isatty(), "This operation needs an attended terminal; do not pipe answers into it.")
    require(input(f"{text}\nType {word!r}: ").strip() == word, "Cancelled.")


def print_torque(values):
    for index, motor in enumerate(MOTORS, 1):
        value = values[motor]
        state = "OFF" if value == 0 else "ON" if value == 1 else "UNEXPECTED"
        print(f"  id={index} {motor:14s} Torque_Enable={value} ({state})", flush=True)
    if all(v == 0 for v in values.values()):
        print("All six report torque OFF. This does not remove mechanical gear resistance.", flush=True)


def announce(message):
    # Terminal bell is a convenience, not a required audio device or guarantee.
    print(f"\a{message}", flush=True)


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


def wait_for_teaching_pose(arm, clock=time.monotonic, sleep=time.sleep, cue=announce):
    """Permit manual recovery from a resting end-stop pose, with torque off.

    No out-of-margin reading is captured, and no motor goal is ever sent here.
    """
    deadline = clock() + TEACH_POSITION_TIMEOUT
    last_report = None
    next_report = 0.0
    while True:
        tick = clock()
        arm.require_torque(False)
        current = arm.read_raw()
        arm.require_torque(False)
        require(clock() - tick <= MAX_IO_TIME, "Stale feedback during manual repositioning; teaching stopped.")
        issues = arm.limit_issues(current)
        if not issues:
            if last_report is not None:
                cue("All joints are within the capture margin. Starting the hover countdown.")
            return
        if last_report is None:
            cue("Capture is paused. Keep supporting the arm, clear of the keys. Gently move the listed joint back into its permitted range; do not force an end stop.")
        if clock() >= next_report:
            print("  " + "; ".join(issues), flush=True)
            next_report = clock() + 1.0
        last_report = issues
        require(clock() < deadline, "Joint remained outside the capture margin for 60 seconds; no poses saved. Check positioning/calibration before retrying.")
        sleep(0.1)


def timed_capture(arm, label, seconds, entry=None, clock=time.monotonic,
                  sleep=time.sleep, cue=announce):
    """Capture a stable endpoint after a countdown; record the intervening path.

    Time is an operator cue, not contact detection. Each endpoint must later be
    confirmed by the operator, with the arm safely resting, before saving.
    """
    cue(f"{label}. You have {seconds:g} seconds. Move gently, then hold still until CAPTURED.")
    deadline = clock() + seconds
    recent = deque()
    last_count = None
    warned = False
    while True:
        tick = clock()
        arm.require_torque(False)
        current = arm.read()
        arm.require_torque(False)
        now = clock()
        require(now - tick <= MAX_IO_TIME, "Stale feedback during teaching; capture discarded.")
        if recent:
            require(now - recent[-1][0] <= MAX_IO_TIME, "Teaching feedback gap; capture discarded. Move more slowly and re-teach.")
        if entry is not None:
            previous_observation = recent[-1][1] if recent else entry["path"][-1]
            append_capture(entry, current, previous_observation=previous_observation)
        recent.append((now, dict(current)))
        # Keep one sample just before the start of the stable window, so we
        # require actual continuous coverage instead of a count of fast reads.
        while len(recent) > 1 and recent[1][0] <= now - AUTO_SETTLE_SECONDS:
            recent.popleft()
        stable = (
            now - recent[0][0] >= AUTO_SETTLE_SECONDS
            and all(max(p[m] for _, p in recent) - min(p[m] for _, p in recent) <= 2 for m in MOTORS)
        )
        if now >= deadline:
            if stable:
                if entry is not None:
                    append_capture(entry, current, force=True)
                cue(f"CAPTURED: {label}.")
                return dict(current)
            if not warned:
                cue("Still moving. Hold still; capture will wait up to five more seconds.")
                warned = True
            require(now - deadline < AUTO_SETTLE_TIMEOUT, "Could not capture a stable endpoint; nothing saved.")
        else:
            count = math.ceil(deadline - now)
            if count != last_count:
                print(f"  {count} seconds remaining; all six torque flags OFF", flush=True)
                last_count = count
        sleep(max(0, PERIOD - (clock() - tick)))


def teach_hands_free(arm, existing, contact_ready, fixture_note, setup_seconds,
                     stage_seconds, clock=time.monotonic, sleep=time.sleep,
                     cue=announce):
    require(math.isfinite(setup_seconds) and 5 <= setup_seconds <= 60, "Setup countdown must be 5..60 seconds.")
    require(math.isfinite(stage_seconds) and 3 <= stage_seconds <= 30, "Each teaching stage must be 3..30 seconds.")
    require_human(
        f"Rest the arm securely clear of Orchid. After you type ready, you have {setup_seconds:g} seconds to support it with BOTH hands before torque releases.\n"
        "Watch the printed countdowns (terminal bells may be muted). Then follow HOVER, FIRST CONTACT, and JUST TRIGGERED cues. No keys are needed during capture.",
        "ready",
    )
    deadline = clock() + setup_seconds
    last_count = None
    while clock() < deadline:
        count = math.ceil(deadline - clock())
        if count != last_count:
            print(f"Support the arm: torque releases in {count} seconds.", flush=True)
            last_count = count
        sleep(min(0.1, max(0, deadline - clock())))
    arm.release()  # Only torque-off writes; no position goals or torque-enable.
    try:
        wait_for_teaching_pose(arm, clock, sleep, cue)
        hover = timed_capture(arm, "HOVER: pad clear above the selected key", stage_seconds,
                              clock=clock, sleep=sleep, cue=cue)
        entry = teaching_entry(arm, contact_ready, fixture_note, hover)
        entry["teaching_mode"] = "hands_free_countdown"
        entry["stage_seconds"] = stage_seconds
        if contact_ready:
            timed_capture(arm, "FIRST CONTACT: barely touch the key without pressing it", stage_seconds,
                          entry, clock, sleep, cue)
            require(len(entry["path"]) > 1 and distance(hover, entry["path"][-1]) > 0, "No motion between hover and first contact; discard and try again.")
            entry["touch_index"] = len(entry["path"]) - 1
            timed_capture(arm, "JUST TRIGGERED: gently press until the key just sounds, then hold", stage_seconds,
                          entry, clock, sleep, cue)
            entry["complete"] = True
        validate_entry(entry, complete=contact_ready)
        require(routine_budget(plan(entry, hover_only=not contact_ready), 0.2, midi=True) <= MAX_RUN_TIME, "Captured path is too long for playback. Start closer and teach a shorter stroke.")
        arm.require_torque(False)
    finally:
        cue("Capture ended. Continue supporting the arm, lift clear of the key, and rest it safely. This teaching mode never enables torque.")
    require(sys.stdin.isatty(), "Review requires an attended terminal.")
    print(f"Captured {len(entry['path'])} path samples. Countdown timing does NOT detect contact or musical success.")
    response = input("Once the arm is resting safely: if each cue matched your actual hover/contact/just-triggered pose, type 'save'. Anything else discards: ").strip().lower()
    return entry if response == "save" else existing


def align(arm, hover, clock=time.monotonic, sleep=time.sleep):
    """Read-only alignment feedback for a supported, torque-off arm."""
    arm.require_torque(False)
    print("Support and manually position the arm. Deltas are target minus current motor ticks; no commands are sent. Ctrl+C exits.")
    consecutive = 0
    deadline = clock() + TEACH_POSITION_TIMEOUT
    while consecutive < 10:
        tick = clock()
        arm.require_torque(False)
        current = arm.read_raw()
        arm.require_torque(False)
        require(clock() - tick <= MAX_IO_TIME, "Stale feedback during hover alignment; no motors enabled.")
        delta = {m: hover[m] - current[m] for m in MOTORS}
        issues = arm.limit_issues(current)
        ready = not issues and distance(current, hover) <= START_TOLERANCE and abs(delta["gripper"]) <= GRIPPER_TOLERANCE
        consecutive = consecutive + 1 if ready else 0
        state = "  aligned" if ready else "  move gently back within the working margin" if issues else "         "
        print("\r" + " ".join(f"{m}={v:+5d}" for m, v in delta.items()) + state, end="", flush=True)
        require(clock() < deadline, "Hover alignment timed out; no motors enabled. Rest the supported arm safely before retrying.")
        sleep(0.1)
    print("\nHover aligned. Keep supporting the arm until playback has enabled torque.")


def prepare_hands_free_start(arm, hover, seconds, clock=time.monotonic, sleep=time.sleep):
    """No writes: give the operator time to support/align after the single prompt."""
    require(math.isfinite(seconds) and 5 <= seconds <= 60, "Setup countdown must be 5..60 seconds.")
    deadline = clock() + seconds
    last_count = None
    while clock() < deadline:
        arm.require_torque(False)
        count = math.ceil(deadline - clock())
        if count != last_count:
            print(f"Use both hands to support the arm. Hover alignment starts in {count} seconds; torque stays OFF.", flush=True)
            last_count = count
        sleep(min(0.1, max(0, deadline - clock())))
    align(arm, hover, clock, sleep)
    print("Alignment complete. Rechecking stability before enabling motors; keep supporting until the holding-position message.", flush=True)


def teach(arm, existing, contact_ready, fixture_note):
    require_human("Support the follower before releasing torque. Keep the keyboard clear until the arm is supported.", "supported")
    arm.release()
    wait_for_teaching_pose(arm)
    input("Place the padded/fixed jaw just above the selected key, with clearance. Hold the arm still; press Enter to capture hover. ")
    entry = teaching_entry(arm, contact_ready, fixture_note, stable_pose(arm))
    if not contact_ready:
        print("Hover captured. Contact teaching needs a soft pad and --contact-ready.")
        return entry
    print("Lower by small steps, approximately vertically. Enter = sample; touch = first contact; pressed = key just sounds; q = discard. Keep supporting the arm.")
    while True:
        command = input("Capture [Enter/touch/pressed/q]: ").strip().lower()
        if command == "q":
            return existing
        require(command in ("", "touch", "pressed"), "Unknown capture command; previous key file remains unchanged.")
        require(command != "touch" or entry["touch_index"] is None, "First contact already captured.")
        require(command != "pressed" or entry["touch_index"] is not None, "Capture first contact with 'touch' before 'pressed'.")
        candidate = {**entry, "path": entry["path"] + [stable_pose(arm)]}
        if command == "touch":
            candidate["touch_index"] = len(candidate["path"]) - 1
        candidate["complete"] = command == "pressed"
        try:
            validate_entry(candidate)
        except SafetyError as exc:
            print(f"Sample rejected: {exc} Return closer to the last captured sample and try again.")
            continue
        entry = candidate
        print(f"Captured sample {len(entry['path']) - 1}.")
        if entry["complete"]:
            print("Lift off the key manually while supporting the arm. Torque stays off.")
            return entry


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("init", "teach", "align", "play", "release", "status", "midi-ports"):
        p = sub.add_parser(command)
        if command in ("init", "teach", "align", "play"):
            p.add_argument("--file", type=Path, default=Path(__file__).resolve().parent.parent / "orchid_keys.json")
        if command in ("teach", "align", "play"):
            p.add_argument("--key", choices=KEYS, default="C")
        if command in ("teach", "align", "play", "release", "status"):
            p.add_argument("--port", help="explicit follower port; no automatic selection")
            p.add_argument("--calibration", type=Path, help="existing SO101 follower calibration JSON")
        if command == "teach":
            p.add_argument("--contact-ready", action="store_true", help="soft pad fitted, fixed jaw, ready for supported contact teaching")
            p.add_argument("--fixture-note", required=True, help="identify arm/Orchid placement and padded contact jaw")
            p.add_argument("--hands-free", action="store_true", help="countdowns and automatic samples; type only before/after guiding the arm")
            p.add_argument("--setup-seconds", type=float, default=10, help="time to support the arm before torque release (5..60; hands-free only)")
            p.add_argument("--stage-seconds", type=float, default=8, help="time for each hover/contact/press stage (3..30; hands-free only)")
        if command == "play":
            p.add_argument("--execute", action="store_true", help="open hardware and offer one supervised press; otherwise preview only")
            p.add_argument("--hands-free-start", action="store_true", help="after one prompt, count down and wait for manual hover alignment before enabling motors")
            p.add_argument("--setup-seconds", type=float, default=10, help="time to support the arm before hands-free hover alignment (5..60)")
            p.add_argument("--hover-only", action="store_true", help="check/hold the starting pose, without descending")
            p.add_argument("--fraction", type=float, default=0.25, help="fraction of the taught contact stroke, not millimetres (default .25)")
            p.add_argument("--hold", type=float, default=0.2)
            p.add_argument("--midi-port", help="exact input name; requires mido and python-rtmidi")
            p.add_argument("--midi-note", type=int, choices=range(128))
            p.add_argument("--midi-channel", type=int, choices=range(1, 17), default=1)
            p.add_argument("--log-dir", type=Path, default=Path(__file__).resolve().parent.parent / "orchid_runs")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    if args.command == "init":
        with args.file.open("x") as output:
            json.dump(empty_store(), output, indent=2)
            output.write("\n")
        print(f"Created 12 empty key slots in {args.file}; no invented poses.")
        return
    if args.command == "midi-ports":
        import mido
        print("\n".join(mido.get_input_names()) or "No MIDI inputs found.")
        return
    if args.command in ("teach", "align", "play"):
        store = load_store(args.file)
        entry = store["keys"][args.key]
    if args.command == "play":
        require(math.isfinite(args.hold) and 0 <= args.hold <= 0.5, "Hold must be between 0 and 0.5 seconds.")
        if args.hands_free_start:
            require(math.isfinite(args.setup_seconds) and 5 <= args.setup_seconds <= 60, "Setup countdown must be 5..60 seconds.")
        require(bool(args.midi_port) == (args.midi_note is not None), "Provide both --midi-port and --midi-note, or neither.")
        require(not args.hover_only or not args.midi_port, "MIDI note verification requires a contact stroke.")
        path = plan(entry, args.fraction, args.hover_only)
        require(routine_budget(path, args.hold, bool(args.midi_port)) <= MAX_RUN_TIME, "Taught path exceeds the single-trial time budget. Re-teach a shorter local stroke.")
        print(json.dumps({"key": args.key, "fixture_note": entry.get("fixture_note"), "units": UNITS, "fraction": args.fraction, "hover_only": args.hover_only, "downstroke": path, "release": "same path in reverse", "hold_seconds": args.hold}, indent=2))
        if not args.execute:
            print("Preview only: no serial port, motors, or MIDI input opened.")
            return
    require(args.port and args.calibration, "Live operations require --port and --calibration explicitly.")
    require(args.command == "status" or sys.stdin.isatty(), "Live operations require an attended terminal.")
    arm = Arm(args.port, args.calibration)
    midi = controller = None
    try:
        if args.command in ("play", "align"):
            validate_entry(entry)
            require(arm.signature == entry["calibration_sha256"], "Arm calibration changed. Re-teach this key.")
            require(arm.version == entry["lerobot_version"], "LeRobot version changed. Re-teach after checking the hardware adapter.")
            for pose in entry["path"]:
                arm.check_pose(pose)
        if args.command in ("teach", "align") or (args.command == "play" and args.hands_free_start):
            arm.open(teaching=True)
        else:
            arm.open(release_only=args.command in ("release", "status"))
        if args.command == "status":
            print("Read-only torque status; no settings changed:")
            print_torque(arm.torque_status())
            current = arm.read_raw()
            print("Encoder positions (ticks): " + json.dumps(current))
            issues = arm.limit_issues(current)
            print("Outside capture margin: " + "; ".join(issues) if issues else "All joints are within the capture margin.")
        elif args.command == "align":
            align(arm, entry["path"][0])
        elif args.command == "release":
            require_human("Support the arm clear of Orchid before turning off torque.", "supported")
            arm.release()
            print("Verified torque off. Keep supporting the arm.")
        elif args.command == "teach":
            if args.hands_free:
                taught = teach_hands_free(arm, entry, args.contact_ready, args.fixture_note,
                                         args.setup_seconds, args.stage_seconds)
            else:
                taught = teach(arm, entry, args.contact_ready, args.fixture_note)
            if taught is not entry:
                store["keys"][args.key] = taught
                save_store(args.file, store)
                print(f"Saved {args.key} in {args.file}.")
            else:
                print("Capture discarded; the saved key map is unchanged.")
        else:
            arm.require_torque(False)
            if args.hands_free_start:
                require_human(
                    f"Confirm unchanged fixture/pad and an accessible hardware stop. Rest the arm securely clear of Orchid. After typing play, you have {args.setup_seconds:g} seconds to support it with both hands.\n"
                    "Follow the hover alignment display. Motors will enable automatically only after alignment and stability checks; keep supporting until the holding-position message, then clear hands. No force sensor or collision model is available.",
                    "play",
                )
                prepare_hands_free_start(arm, path[0], args.setup_seconds)
            else:
                require_human("Confirm unchanged fixture/pad, a supported arm at the printed hover pose, and an accessible hardware stop. Clear hands from the motion path after arming. This has no force sensor or collision model.", "play")
            if args.midi_port:
                midi = MidiMonitor(args.midi_port, args.midi_note, args.midi_channel)
            args.log_dir.mkdir(parents=True, exist_ok=True)
            log_path = args.log_dir / f"{args.key.replace('#', 'sharp')}-{uuid.uuid4().hex}.jsonl"
            print(f"Run log: {log_path}", flush=True)
            with log_path.open("x") as log:
                controller = Controller(arm, path, log, midi)
                controller.record(stage="start", key=args.key, calibration_sha256=arm.signature, entry=entry, fraction=args.fraction)
                try:
                    controller.run(args.hold)
                except BaseException as exc:
                    controller.stop()
                    controller.record(stage="fault", error=str(exc) or type(exc).__name__)
                    raise
            print("Returned to hover; torque remains ON. Support the arm and run release before repositioning or leaving it.")
            print("MIDI note-on/off verified." if midi else "Motion completed; musical success has not been automatically verified.")
    finally:
        try:
            if midi:
                midi.close()
        finally:
            arm.close()  # Never releases torque implicitly, including on faults.


if __name__ == "__main__":
    try:
        main()
    except (Exception, KeyboardInterrupt) as exc:
        print(f"Stopped: {exc or type(exc).__name__}", file=sys.stderr)
        sys.exit(1)

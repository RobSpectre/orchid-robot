"""Leader record -> replay in LeRobot SO101 joint units (degrees; gripper 0-100).

Shared by teach_key.py and the web console, so both move the arm identically.
One tick reads the follower (and leader), then sends one goal:

* holding   resend the held goal
* aligning  rate-limited ramp of the goal toward the leader's pose
* following leader pose 1:1, clipped to FOLLOW_CAP from the measured pose
            (LeRobot's max_relative_target rule); frames are recorded if armed
* ramping   rate-limited ramp to a recording's first goal, then settle
* playing   the recorded goals at their recorded timing (/speed), clipped as above

Waypoint teaching captures commanded goals at home, hover, touch and press while
following; waypoint_recording() turns them into a recording that plays
home -> hover -> touch -> press -> (dwell) -> touch -> hover -> home with smooth,
speed-limited joint moves, so playback reuses the same ramp/settle/clip machinery.

Faults are left to the caller: any exception from read/send propagates.
No tracking, settle or stall tolerances: clipping limits a goal, it never stops one.
"""
from __future__ import annotations

import math
import time

FPS = 30
PERIOD = 1.0 / FPS
RAMP_SPEED = 30.0  # deg/s (gripper: %/s) for moves to a starting pose
FOLLOW_CAP = 15.0  # max goal-minus-measured per step while following/replaying
START_TOLERANCE = 8.0  # deg; warn before playback if the arm did not reach where it was taught
SETTLE_S = 1.0
MIN_RECORDING_S = 0.5
MAX_STEP_DT = 0.25  # a stalled loop must not turn into one large ramp step
POINTS = ("home", "hover", "touch", "press")
DIAL_POINTS = ("home", "hover", "open", "lower", "grip")  # all shared by CW and CCW; the turn is computed
TURN_LIMIT = 90.0  # deg: largest wrist turn a dial direction may use
GRIP_DWELL_S = 0.2
TRAVEL_SPEED = 45.0  # peak deg/s between home and hover
STROKE_SPEED = 20.0  # peak deg/s between hover, touch and press
MIN_SEGMENT_S = 0.4
PRESS_DWELL_S = 0.3
PRESS_HARDNESS = 0.5  # touch -> press runs at this fraction of the stroke speed (1.0 = as fast as the strokes)
MIN_PRESS_HARDNESS = 0.1
RETURN_SETTLE_S = 0.3
MAX_PLAY_SPEED = 3.0  # playback speed multiplier (1.0 = the taught/planned timing)
MAX_PRESS_S = 5.0  # longest hold at the bottom of a press
ROLL_JUMP_DEG = 90.0  # a leader wrist-roll change this large in one tick is the -180/+180 wrap, not a real turn
ROLL_RESUME_DEG = 10.0  # after the wrap, follow the wrist roll again once the leader is back this close
SETTLED_DEG = 1.0  # the settle before playback ends as soon as the arm is this close to the start
STROKE_LIMIT = 30.0  # deg: touch is just below hover, press just past touch; farther means a point is wrong  # pause at the press before the automatic return after capturing it


def step_toward(current: dict, target: dict, max_step: float) -> dict:
    return {k: current[k] + max(-max_step, min(max_step, target[k] - current[k])) for k in target}


def clip_to_measured(goal: dict, measured: dict, cap: float) -> tuple[dict, bool]:
    """Same rule as lerobot.robots.utils.ensure_safe_goal_position, without its per-step log spam."""
    clipped = {k: measured[k] + max(-cap, min(cap, goal[k] - measured[k])) for k in goal}
    return clipped, any(abs(clipped[k] - goal[k]) > 1e-6 for k in goal)


def max_gap(a: dict, b: dict) -> tuple[str, float]:
    name = max(a, key=lambda k: abs(a[k] - b[k]))
    return name, abs(a[name] - b[name])


def rounded(pose: dict) -> dict:
    return {k: round(v, 3) for k, v in pose.items()}


def _segment(start: dict, end: dict, peak_speed: float, stretch: float = 1.0) -> tuple[float, list]:
    """Minimum-jerk joint move. Its peak velocity is 1.875x the average, so size it by the peak.
    stretch > 1 slows the whole move, including the shortest-move floor."""
    _, delta = max_gap(start, end)
    duration = max(MIN_SEGMENT_S, 1.875 * delta / peak_speed) * stretch
    steps = max(1, math.ceil(duration / PERIOD))
    poses = []
    for i in range(1, steps + 1):
        u = i / steps
        s = 10 * u**3 - 15 * u**4 + 6 * u**5
        poses.append((duration * u, {k: start[k] + (end[k] - start[k]) * s for k in start}))
    return duration, poses


def _frames(points: dict, legs: list, marks: dict | None = None) -> list:
    """marks, if given, receives when each point is first reached and when a dwell there ends ("<point>_held")."""
    first = legs[0][0]
    frames = [{"t": 0.0, "goal": rounded(points[first]["goal"]), "follower": points[first]["measured"]}]
    t = 0.0
    marks = {} if marks is None else marks
    for a, b, speed, *stretch in legs:
        if a == b:  # dwell
            t += speed
            frames.append({"t": round(t, 4), "goal": rounded(points[a]["goal"])})
            marks.setdefault(a + "_held", round(t, 4))
            continue
        duration, poses = _segment(points[a]["goal"], points[b]["goal"], speed, *stretch)
        frames += [{"t": round(t + dt, 4), "goal": rounded(pose)} for dt, pose in poses]
        t += duration
        marks.setdefault(b, round(t, 4))
    return frames


def waypoint_recording(points: dict, key: str = "", press_s: float = PRESS_DWELL_S,
                       hardness: float = PRESS_HARDNESS) -> dict:
    """Full playback: home -> hover -> touch -> press, dwell, then the same points in reverse.
    hardness (0.1-1) slows only the touch -> press stroke, relative to the other strokes."""
    missing = [name for name in POINTS if name not in points]
    if missing:
        raise ValueError("Capture " + ", ".join(missing) + " first.")
    if not MIN_PRESS_HARDNESS <= hardness <= 1.0:
        raise ValueError(f"Press hardness must be between {MIN_PRESS_HARDNESS:.0%} and 100%.")
    marks = {}
    frames = _frames(points, [
        ("home", "hover", TRAVEL_SPEED), ("hover", "touch", STROKE_SPEED), ("touch", "press", STROKE_SPEED, 1 / hardness),
        ("press", "press", press_s),
        ("press", "touch", STROKE_SPEED), ("touch", "hover", STROKE_SPEED), ("hover", "home", TRAVEL_SPEED)], marks)
    # When the stroke starts (touch), reaches the bottom (press) and lifts off: what a key check times against.
    return {"key": key, "frames": frames, "marks": {"touch": marks["touch"], "press": marks["press"], "lift": marks["press_held"]}}


def _turned(points: dict, degrees: float) -> dict:
    """The turn is the grip pose with only the wrist rotated; then let go in place and raise to the open pose."""
    grip, opened = points["grip"], points["open"]
    roll, width = grip["goal"]["wrist_roll"] + degrees, opened["goal"]["gripper"]
    return {**points,
            "turn": {"goal": {**grip["goal"], "wrist_roll": roll}, "measured": grip["measured"]},
            "release": {"goal": {**grip["goal"], "wrist_roll": roll, "gripper": width}, "measured": grip["measured"]},
            "raise": {"goal": {**opened["goal"], "wrist_roll": roll}, "measured": opened["measured"]}}


def check_turn(points: dict, degrees: float) -> None:
    if not 0 < abs(degrees) <= TURN_LIMIT:
        raise ValueError(f"Choose a turn between 1 and {TURN_LIMIT:.0f} degrees (negative turns the other way).")
    if abs(points["grip"]["goal"]["wrist_roll"] + degrees) >= 179:
        raise ValueError("That turn would cross the wrist sensor's -180/+180 edge. Re-centre wrist rotation, or use a smaller turn.")


def dial_recording(points: dict, degrees: float, key: str = "") -> dict:
    """home -> hover -> open -> lower -> grip -> turn -> let go -> raise -> hover -> home. Never turns back while gripping."""
    missing = [name for name in DIAL_POINTS if name not in points]
    if missing:
        raise ValueError("Capture " + ", ".join(missing) + " first.")
    check_turn(points, degrees)
    return {"key": key, "frames": _frames(_turned(points, degrees), [
        ("home", "hover", TRAVEL_SPEED), ("hover", "open", STROKE_SPEED), ("open", "lower", STROKE_SPEED),
        ("lower", "grip", STROKE_SPEED), ("grip", "grip", GRIP_DWELL_S), ("grip", "turn", STROKE_SPEED),
        ("turn", "release", STROKE_SPEED), ("release", "release", GRIP_DWELL_S), ("release", "raise", STROKE_SPEED),
        ("raise", "hover", STROKE_SPEED), ("hover", "home", TRAVEL_SPEED)])}


def dial_return_recording(points: dict, key: str = "") -> dict:
    """Right after capturing the grip (no turn while teaching): let go, raise to open, hover, home."""
    return {"key": key, "frames": _frames(_turned(points, 0.0), [
        ("grip", "release", STROKE_SPEED), ("release", "release", GRIP_DWELL_S), ("release", "raise", STROKE_SPEED),
        ("raise", "hover", STROKE_SPEED), ("hover", "home", TRAVEL_SPEED)])}


def sequence_recording(recordings: list, key: str = "sequence") -> dict:
    """Recordings that each start and end at home, played back to back on one timeline.
    steps says where each one starts and ends on that timeline, with its marks shifted to match."""
    frames, offset, steps = [], 0.0, []
    for recording in recordings:
        part = recording["frames"]
        frames += [{**f, "t": round(offset + f["t"], 4)} for f in (part if not frames else part[1:])]
        steps.append({"key": recording.get("key"), "start": offset, "end": frames[-1]["t"],
                      "marks": {k: round(offset + v, 4) for k, v in recording.get("marks", {}).items()}})
        offset = frames[-1]["t"]
    return {"key": key, "frames": frames, "steps": steps}


def return_recording(points: dict, key: str = "") -> dict:
    """The reverse half only, starting at the captured press: press -> touch -> hover -> home."""
    return {"key": key, "frames": _frames(points, [
        ("press", "touch", STROKE_SPEED), ("touch", "hover", STROKE_SPEED), ("hover", "home", TRAVEL_SPEED)])}


class Session:
    """One powered follower (already holding its measured pose) plus an optional leader."""

    def __init__(self, read_follower, send_follower, read_leader=None, *, lock_gripper=False,
                 clock=time.monotonic, ramp_speed=RAMP_SPEED, follow_cap=FOLLOW_CAP,
                 start_tolerance=START_TOLERANCE, settle_s=SETTLE_S):
        self.read_follower, self.send_follower, self.read_leader = read_follower, send_follower, read_leader
        self.clock = clock
        self.ramp_speed, self.follow_cap = ramp_speed, follow_cap
        self.start_tolerance, self.settle_s = start_tolerance, settle_s
        self.measured = read_follower()
        self.goal = dict(self.measured)
        self.leader_pose = None
        self.gripper_hold = self.measured["gripper"] if lock_gripper else None
        self.roll_hold = None  # wrist roll held still while following (lock_wrist_roll; not used by the console)
        self.roll_guard = False  # the leader's wrist roll crossed the -180/+180 edge: hold ours until it comes back
        self.mode = "holding"
        self.frames = None
        self.record_started = None
        self.recording = self.speed = None
        self.index = 0
        self.phase_until = self.play_started = None
        self.clipped_steps = 0
        self.warning = None
        self.last_tick = None

    # --- operator commands ------------------------------------------------------------------------

    def capture(self) -> dict:
        """The commanded goal (what the follower is being driven to) and the measured pose, right now."""
        if self.mode not in ("holding", "following"):
            raise RuntimeError("Capture while holding or following (wait for FOLLOWING).")
        return {"goal": rounded(self.goal), "measured": rounded(self.measured)}

    def hold(self):
        """Hold at a fresh measured pose (relieves any push) and drop any recording/playback."""
        self.measured = self.read_follower()
        self.goal = dict(self.measured)
        self.send_follower(self.goal)
        self.mode, self.frames, self.recording = "holding", None, None

    def follow(self):
        if self.read_leader is None:
            raise RuntimeError("Connect the leader to follow it.")
        self.mode, self.frames, self.recording, self.warning = "aligning", None, None, None

    def lock_wrist_roll(self, locked: bool):
        """Hold the wrist roll where it is now, or release it; releasing ramps it to the leader instead of jumping."""
        if locked and self.roll_hold is None:
            self.roll_hold = self.goal["wrist_roll"]
        elif not locked and self.roll_hold is not None:
            self.roll_hold = None
            if self.mode == "following":
                self.mode = "aligning"

    def start_recording(self):
        if self.mode != "following":
            raise RuntimeError("Wait until the follower is following the leader before recording.")
        self.frames, self.record_started = [], None

    def stop_recording(self) -> list:
        frames, self.frames = self.frames, None
        if not frames or frames[-1]["t"] < MIN_RECORDING_S:
            raise RuntimeError("Recording too short; nothing was saved. Record again.")
        return frames

    def play(self, recording: dict, speed=1.0, *, force=False, settle_s=None, ramp_speed=None):
        if not 0.1 <= speed <= MAX_PLAY_SPEED:
            raise ValueError(f"Playback speed must be between 0.1 and {MAX_PLAY_SPEED:g}.")
        if not recording.get("frames"):
            raise ValueError("This recording has no frames.")
        self.recording, self.speed, self.force = recording, speed, force
        self.play_settle_s = self.settle_s if settle_s is None else settle_s
        self.play_ramp_speed = self.ramp_speed if ramp_speed is None else ramp_speed
        self.mode, self.frames, self.warning, self.clipped_steps = "ramping", None, None, 0
        self.index = 0

    @property
    def recorded_seconds(self) -> float:
        return self.frames[-1]["t"] if self.frames else 0.0

    @property
    def progress(self) -> float | None:
        if self.mode != "playing":
            return None
        frames = self.recording["frames"]
        return self.index / max(1, len(frames) - 1)

    # --- control loop -----------------------------------------------------------------------------

    def wrap_guard(self, target: dict) -> dict:
        """Wrist roll spans a full turn, so its reading wraps between +180 and -180 deg. Never chase that wrap: it
        would spin the follower almost a whole turn the other way. Hold our roll until the leader comes back."""
        gap = abs(target["wrist_roll"] - self.goal["wrist_roll"])
        if self.roll_guard and gap <= ROLL_RESUME_DEG:
            self.roll_guard = False
        elif not self.roll_guard and gap > (180.0 if self.mode == "aligning" else ROLL_JUMP_DEG):
            self.roll_guard = True
        return {**target, "wrist_roll": self.goal["wrist_roll"]} if self.roll_guard else target

    def tick(self) -> str | None:
        """One read + one goal. Returns 'aligned', 'start_mismatch', 'playing' or 'played' on a change."""
        now = self.clock()
        dt = min(MAX_STEP_DT, now - self.last_tick) if self.last_tick is not None else PERIOD
        self.last_tick = now
        self.measured = measured = self.read_follower()
        if self.read_leader is not None:  # every tick, so leader feedback stays live while holding/playing
            self.leader_pose = self.read_leader()
            if self.gripper_hold is not None:
                self.leader_pose["gripper"] = self.gripper_hold
            if self.roll_hold is not None:
                self.leader_pose["wrist_roll"] = self.roll_hold
        event = None
        if self.mode in ("aligning", "following"):
            target = self.wrap_guard(self.leader_pose)
            if self.mode == "aligning":
                self.goal = step_toward(self.goal, target, self.ramp_speed * dt)
                if max_gap(self.goal, target)[1] < 1e-9:
                    self.mode, event = "following", "aligned"
            else:
                self.goal, _ = clip_to_measured(target, measured, self.follow_cap)
            if self.frames is not None:
                if self.record_started is None:
                    self.record_started = now
                self.frames.append({"t": round(now - self.record_started, 4), "goal": rounded(self.goal),
                                    "leader": rounded(target), "follower": rounded(measured)})
        elif self.mode == "ramping":
            first = self.recording["frames"][0]["goal"]
            self.goal = step_toward(self.goal, first, self.play_ramp_speed * dt)
            if max_gap(self.goal, first)[1] < 1e-9:
                self.mode, self.phase_until = "settling", now + self.play_settle_s
        elif self.mode == "settling" and (now >= self.phase_until or
                                          max_gap(measured, self.recording["frames"][0]["follower"])[1] <= SETTLED_DEG):
            joint, gap = max_gap(measured, self.recording["frames"][0]["follower"])
            if gap > self.start_tolerance and not self.force:
                self.warning = (f"{joint} is {gap:.1f} deg from where it was when taught; something may be "
                                "in the way. Clear it, or play anyway.")
                self.mode, event = "holding", "start_mismatch"
            else:
                self.mode, self.play_started, self.index, event = "playing", now, 0, "playing"
        if self.mode == "playing":
            frames = self.recording["frames"]
            elapsed = (now - self.play_started) * self.speed
            while self.index + 1 < len(frames) and frames[self.index + 1]["t"] <= elapsed + PERIOD / 2:
                self.index += 1
            self.goal, clipped = clip_to_measured(frames[self.index]["goal"], measured, self.follow_cap)
            self.clipped_steps += clipped
            if self.index == len(frames) - 1:
                self.mode, event = "holding", "played"
        self.send_follower(self.goal)
        return event

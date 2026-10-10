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
# Soft hold: once a press has had HOLD_SETTLE_S to bottom out, hold where the arm actually stopped plus at most
# HOLD_PUSH_DEG toward the taught press, not the full taught depth (which can sit past the key's bottom and keep the
# motor pushing for as long as the note is held). The small preload keeps the key down.
STRIKE_FRACTION = 0.5  # where in the touch -> press stroke a key is assumed to sound, before it has been heard
HOLD_SETTLE_S = 0.15
HOLD_PUSH_DEG = 0.5
MAX_PRESS_S = 60.0  # the longest press length in seconds (console); note values (music.py) have no limit
ROLL_JUMP_DEG = 90.0  # a leader wrist-roll change this large in one tick, other than its -180/+180 wrap, is a glitch
ROLL_LIMIT_DEG = 168.0  # the follower's wrist rotation reaches about +-169 deg (its encoder's ends); never ask past this
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
        marks.setdefault(f"{a}>{b}", round(t, 4))  # the end of this leg, e.g. "press>touch" on the way back up
    return frames


def waypoint_recording(points: dict, key: str = "", press_s: float = PRESS_DWELL_S,
                       hardness: float = PRESS_HARDNESS, strike_fraction: float = STRIKE_FRACTION,
                       strike_delay: float = 0.0, approach: tuple = ("home",), stay: bool = False) -> dict:
    """Full playback: home -> hover -> touch -> press, dwell, then the same points in reverse.
    hardness (0.1-1) slows only the touch -> press stroke, relative to the other strokes.
    approach: the points travelled through to the hover, the first where it starts (("from",): straight from the hover
    the arm waits at after the note before, a point named "from" in points). stay: end back up at the hover, ready for
    the next key, instead of at home."""
    missing = [name for name in (*POINTS, *approach) if name not in points]
    if missing:
        raise ValueError("Capture " + ", ".join(missing) + " first.")
    if not MIN_PRESS_HARDNESS <= hardness <= 1.0:
        raise ValueError(f"Press hardness must be between {MIN_PRESS_HARDNESS:.0%} and 100%.")
    marks = {}
    frames = _frames(points, [
        *[(a, b, TRAVEL_SPEED) for a, b in zip(approach, (*approach[1:], "hover"))],
        ("hover", "touch", STROKE_SPEED), ("touch", "press", STROKE_SPEED, 1 / hardness),
        ("press", "press", press_s),
        ("press", "touch", STROKE_SPEED), ("touch", "hover", STROKE_SPEED), *([] if stay else [("hover", "home", TRAVEL_SPEED)])],
        marks)
    # When the stroke starts (touch), reaches the bottom (press) and lifts off: what a key check times against.
    # strike: when the key sounds: a fraction of the touch -> press stroke (STRIKE_FRACTION until the engine has learned
    # it for this key from Orchid's notes) plus the rig's delay (strike_delay, in recording time). release: halfway up. hover: where a timed play waits so its
    # strike lands on the beat.
    down, up = marks["press"] - marks["touch"], marks["press>touch"] - marks["press_held"]
    return {"key": key, "frames": frames, "holds": [_hold(points, marks["press"], marks["press>touch"])], "marks": {
        "hover": marks["hover"], "touch": marks["touch"], "press": marks["press"], "lift": marks["press_held"],
        "strike": round(marks["touch"] + down * strike_fraction + strike_delay, 4),
        "release": round(marks["press_held"] + up / 2, 4)}}


def press_for(sound_s: float, recording: dict, speed: float) -> float:
    """The press dwell that makes a key sound for sound_s seconds at this speed: the note runs from the strike, halfway
    down the stroke, to the release, halfway back up. The dwell is in the recording's time, which speed also scales.
    No upper limit: a note value can be held for as many bars as written; Stop motion ends it any time."""
    marks = recording["marks"]
    strokes = (marks["press"] - marks["strike"]) + (marks["release"] - marks["lift"])
    return max(0.0, sound_s * speed - strokes)


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
    frames, offset, steps, holds = [], 0.0, [], []
    for recording in recordings:
        part = recording["frames"]
        frames += [{**f, "t": round(offset + f["t"], 4)} for f in (part if not frames else part[1:])]
        holds += [{**h, "start": round(offset + h["start"], 4), "until": None if h["until"] is None else round(offset + h["until"], 4)}
                  for h in recording.get("holds", ())]
        steps.append({"key": recording.get("key"), "start": offset, "end": frames[-1]["t"],
                      "marks": {k: round(offset + v, 4) for k, v in recording.get("marks", {}).items()}})
        offset = frames[-1]["t"]
    return {"key": key, "frames": frames, "steps": steps, "holds": holds}


def hold_recording(points: dict, key: str = "", hardness: float = PRESS_HARDNESS) -> dict:
    """A chord button pressed and held: home -> hover -> touch -> press, ending held down (no return)."""
    missing = [name for name in POINTS if name not in points]
    if missing:
        raise ValueError("Capture " + ", ".join(missing) + " first.")
    marks = {}
    frames = _frames(points, [("home", "hover", TRAVEL_SPEED), ("hover", "touch", STROKE_SPEED),
                              ("touch", "press", STROKE_SPEED, 1 / hardness)], marks)
    return {"key": key, "frames": frames, "holds": [_hold(points, marks["press"], None)],
            "marks": {"touch": marks["touch"], "press": marks["press"]}}


def _soften(goal: dict, measured: dict, hold: dict) -> dict:
    """Where the arm stopped, plus at most HOLD_PUSH_DEG toward the taught press, for the joints that press."""
    return {j: measured[j] + max(-HOLD_PUSH_DEG, min(HOLD_PUSH_DEG, goal[j] - measured[j])) if hold["into"].get(j) else goal[j]
            for j in goal}


def no_deeper(recording: dict, goal: dict, points: dict) -> dict:
    """A recording that starts at the bottom of a press (letting a held chord button go), never asking for more depth
    than the soft hold the arm is at (goal) while it lifts off, so it does not first push back down to the taught
    press. Only up to touch: hover and home are where they are, whichever side of the press a joint's home lies."""
    into = _hold(points, 0.0, None)["into"]
    lifted = recording.get("marks", {}).get("touch", float("inf"))

    def capped(f):
        if f["t"] > lifted:
            return f
        return {**f, "goal": {j: goal[j] if into.get(j) and (v - goal[j]) * into[j] > 0 else v for j, v in f["goal"].items()}}
    return {**recording, "frames": [capped(f) for f in recording["frames"]]}


def _hold(points: dict, start: float, until) -> dict:
    """A soft hold at the bottom of a press (Session): from start (the press reached) until `until` (back up at touch;
    None: for as long as the arm holds there). into: which way each joint moves to press deeper."""
    touch, press = points["touch"]["goal"], points["press"]["goal"]
    return {"start": start, "until": until,
            "into": {j: (press[j] > touch[j]) - (press[j] < touch[j]) for j in press if j in touch}}


def return_recording(points: dict, key: str = "") -> dict:
    """The reverse half only, starting at the captured press: press -> touch -> hover -> home.
    marks["touch"]: when the lift off the button is done (no_deeper caps only up to there)."""
    marks = {}
    frames = _frames(points, [("press", "touch", STROKE_SPEED), ("touch", "hover", STROKE_SPEED),
                              ("hover", "home", TRAVEL_SPEED)], marks)
    return {"key": key, "frames": frames, "marks": {"touch": marks["touch"]}}


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
        self.roll_guard = False  # the leader's wrist is turned past where the follower's can go: ours waits at its end
        self.roll_prev, self.roll_turns = None, 0  # the leader's last wrist reading, and whole turns across its +-180 edge
        self.mode = "holding"
        self.frames = None
        self.record_started = None
        self.recording = self.speed = None
        self.index = 0
        self.phase_until = self.play_started = None
        self.clipped_steps = 0
        self.warning = None
        self.last_tick = None
        self.sent = None  # the goal last written to the follower
        # A timed play (rhythm): the recording waits at each gate (a recording time, at a hover) until timer(i, now)
        # says, so its strike lands on the beat. play_began is when playback started, before any wait.
        self.gates, self.timer, self.gate_index, self.gate_until, self.play_began = [], None, 0, None, None
        self.soft = None  # (hold, goal): the soft hold in force at the bottom of a press
        self.soft_due = None  # (hold, time): a press the recording ends on (a held chord button) softens once settled

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
        self.send(self.goal)
        self.mode, self.frames, self.recording = "holding", None, None
        self.soft = self.soft_due = None

    def send(self, goal):
        self.send_follower(goal)
        self.sent = dict(goal)

    def follow(self):
        if self.read_leader is None:
            raise RuntimeError("Connect the leader to follow it.")
        self.mode, self.frames, self.recording, self.warning = "aligning", None, None, None
        self.soft = self.soft_due = None
        self.roll_prev = None  # pick up the leader's wrist afresh, the way nearest the follower's

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

    def play(self, recording: dict, speed=1.0, *, force=False, settle_s=None, ramp_speed=None, gates=(), timer=None):
        if not 0.1 <= speed <= MAX_PLAY_SPEED:
            raise ValueError(f"Playback speed must be between 0.1 and {MAX_PLAY_SPEED:g}.")
        if not recording.get("frames"):
            raise ValueError("This recording has no frames.")
        self.recording, self.speed, self.force = recording, speed, force
        self.play_settle_s = self.settle_s if settle_s is None else settle_s
        self.play_ramp_speed = self.ramp_speed if ramp_speed is None else ramp_speed
        self.mode, self.frames, self.warning, self.clipped_steps = "ramping", None, None, 0
        self.index = 0
        self.gates, self.timer, self.gate_index, self.gate_until = sorted(gates), timer, 0, None
        self.soft = self.soft_due = None

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
        """Follow the leader's wrist rotation as one continuous angle. Its reading wraps between +180 and -180 deg, but
        the follower's wrist cannot turn through that edge, so a wrap counts as a turn past it, not a jump to the other
        side. The follower's wrist goes as far as it can (ROLL_LIMIT_DEG) and waits there while the leader is turned
        further (roll_guard), and follows again as soon as the leader comes back within reach, whichever way."""
        roll = target["wrist_roll"]
        if self.roll_prev is None:  # starting to follow: the turn nearest the follower's wrist
            self.roll_turns = min((-1, 0, 1), key=lambda n: abs(roll + 360 * n - self.goal["wrist_roll"]))
        else:
            step = min((roll - self.roll_prev + 360 * n for n in (-1, 0, 1)), key=abs)
            if abs(step) > ROLL_JUMP_DEG:  # not a real turn in one tick: ignore this reading
                roll = self.roll_prev
            elif abs(roll - self.roll_prev) > 180:  # across the +-180 edge
                self.roll_turns += 1 if self.roll_prev > roll else -1
        self.roll_prev = roll
        continuous = roll + 360 * self.roll_turns
        reachable = max(-ROLL_LIMIT_DEG, min(ROLL_LIMIT_DEG, continuous))
        self.roll_guard = reachable != continuous
        return {**target, "wrist_roll": reachable}

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
                self.play_began = now
        if self.mode == "playing":
            frames = self.recording["frames"]
            elapsed = (now - self.play_started) * self.speed
            if self.gate_index < len(self.gates) and elapsed >= self.gates[self.gate_index]:
                gate = self.gates[self.gate_index]
                if self.gate_until is None:  # just arrived: when may it go on?
                    self.gate_until = self.timer(self.gate_index, now) if self.timer else now
                if now < self.gate_until:  # wait here, the recording's clock stopped at the gate
                    self.play_started, elapsed = now - gate / self.speed, gate
                else:
                    self.gate_index, self.gate_until = self.gate_index + 1, None
            while self.index + 1 < len(frames) and frames[self.index + 1]["t"] <= elapsed + PERIOD / 2:
                self.index += 1
            self.goal, clipped = clip_to_measured(self.softened(frames[self.index]["goal"], elapsed, measured),
                                                  measured, self.follow_cap)
            self.clipped_steps += clipped
            if self.index == len(frames) - 1:
                self.mode, event = "holding", "played"
                hold = next((h for h in self.recording.get("holds", ()) if h["until"] is None and h["start"] <= elapsed), None)
                if hold is not None and (self.soft is None or self.soft[0] is not hold):
                    self.soft_due = (hold, now + max(0.0, hold["start"] + HOLD_SETTLE_S * self.speed - elapsed) / self.speed)
        if self.mode == "holding" and self.soft_due and now >= self.soft_due[1]:
            hold, self.soft_due = self.soft_due[0], None
            self.soft = (hold, _soften(self.goal, measured, hold))
            self.goal = self.soft[1]
        # Write a goal once, not every tick. Rewriting an unchanged goal 30 times a second restarts the servos' motion
        # profile each time, which shakes a joint held against its end of travel or a key's bottom.
        if self.sent != self.goal:
            self.send(self.goal)
        return event

    def softened(self, goal: dict, elapsed: float, measured: dict) -> dict:
        """At the bottom of a press, once it has settled: hold where the arm stopped, plus at most HOLD_PUSH_DEG toward
        the taught press, and on the way back up never ask for more depth than that."""
        hold = next((h for h in self.recording.get("holds", ()) if h["start"] <= elapsed
                     and (h["until"] is None or elapsed < h["until"])), None)
        if hold is None:
            self.soft = None
            return goal
        if (self.soft is None or self.soft[0] is not hold) and elapsed >= hold["start"] + HOLD_SETTLE_S * self.speed:
            self.soft = (hold, _soften(goal, measured, hold))
        if self.soft is None or self.soft[0] is not hold:
            return goal
        soft = self.soft[1]
        return {j: soft[j] if hold["into"].get(j) and (goal[j] - soft[j]) * hold["into"][j] > 0 else goal[j] for j in goal}

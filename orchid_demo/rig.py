"""Two followers on one Orchid: The Keys Arm plays the keys, Chord Arm the chord buttons and voicing dial.

The app always has both; The Chord Arm shows up once a second follower is detected, connected or remembered. Each arm is only
ever taught and plays its own controls. Each follower is recognised by the calibration in its motors (discovery.matches),
so Keys Arm's hardware is never connected, or recalibrated, as the Chord Arm.

Each follower has its own Engine (connection, calibration, taught controls, home, worker thread); the rig holds
what they share. Their reach overlaps, and nothing here models the arms' geometry, so the interlock keeps them
apart by rule: an arm may leave its home only while the other is parked (holding still within PARKED_DEG of its
own taught home or rest, or not connected), and one claim, taken under a single lock, decides which arm may be away from
home. A move back to home needs only the other arm to be still, so neither can be stranded away from home.

A chord is the one time both arms are away together (play_chord): The Chord Arm presses and holds the chord button, Keys Arm plays
the key, and once the key is down the Chord Arm lets go and returns home while the Keys Arm finishes. The rig runs that order itself, allows
the overlap only for that chord's own commands, and first checks the two arms' modelled paths stay apart
(kinematics.CLEARANCE_MM).
"""
from __future__ import annotations

import threading
import time
import uuid

from . import kinematics
from . import motion as m
from .controls import CATALOG, EXTRA_CONTROLS
from .discovery import follower_problem
from .motion import KEYS

ARMS = ("a", "b")
ROLES = {"a": tuple(KEYS), "b": tuple(EXTRA_CONTROLS)}
NAMES = {"a": "Keys Arm", "b": "Chord Arm"}  # what the operator sees; "a" and "b" stay the ids (and DATA_DIR/arm-b)
PARKED_DEG = 5.0
# Actions that drive the motors away from where the arm is.
LEAVES_HOME = ("teach_follow", "teach_play", "teach_sequence", "teach_go_rest", "tune_start", "leader_resume",
               "move_home", "control_start", "test", "retry", "next", "home_start", "leader_hold", "dial_capture_start",
               "chord_press")
GOES_HOME = ("teach_go_home", "chord_release")
CHORD_TIMEOUT_S = 60.0
RELEASE_AFTER_S = 0.1  # recording seconds past the key reaching its press before the chord button lets go


def owner(control):
    return next(arm for arm, controls in ROLES.items() if control in controls)


class LeaderStore:
    """One leader calibration for both followers, kept in the Keys Arm's store."""

    def __init__(self, repo):
        self.repo = repo

    def get(self):
        return self.repo.get("leader_calibration")

    def put(self, value):
        self.repo.put("leader_calibration", value)


class Rig:
    def __init__(self):
        self.engines = {}
        self.lock = threading.Lock()
        self.holder = None  # the arm allowed to be away from home
        self.detected = {}  # port -> which arm it is ("a", "b" or None), from the latest scan
        self.scan = []  # that scan's ports
        self.released_leader = None  # the leader port one arm just handed over (rig.Rig: move the leader)
        self.chord = None  # the chord being played: only its own commands may have both arms away from home

    def add(self, arm, engine):
        self.engines[arm] = engine
        engine.rig, engine.arm_id = self, arm
        if engine.mode == "simulation":  # practice arms are never scanned
            self.note_scan(engine.discovery["ports"])

    def other(self, arm):
        return next((e for a, e in self.engines.items() if a != arm), None)

    def claim(self, arm, action, args):
        """Raises unless this arm may start this motion now. Called on the arm's own worker thread."""
        if action not in LEAVES_HOME + GOES_HOME and not (action == "teach_begin" and args.get("follow") is True):
            return
        other = self.other(arm)
        if other is None:
            return
        with self.lock:
            run = self.chord
            if run and args.get("chord_run") == run["id"]:
                if action == "teach_play" and arm == run["key_arm"]:
                    m.require(other.phase == "teach_hold" and other.chord_held == run["chord"],
                              f"{NAMES[other.arm_id]} is not holding {CATALOG[run['chord']]['name']} down.")
                    self.holder = arm
                    return
                if action == "chord_release" and arm == run["chord_arm"]:
                    return
            parked, why = other.parked_now  # published by the other arm's own thread; never its lock
            name = NAMES[other.arm_id]
            if self.holder not in (None, arm) and self.holder == other.arm_id and not parked:
                raise m.SafetyError(f"{name} is away from its home ({why}). Send it home before moving this arm.")
            if action in GOES_HOME:
                m.require(why != "moving", f"{name} is moving. Wait until it holds still before sending this arm home.")
            else:
                m.require(parked, f"{name} is not parked at its home or rest ({why}). The arms can touch, so send it home or to rest first.")
            self.holder = arm

    def update(self, arm, parked):
        """Each arm reports after every publish; a parked holder gives up its claim."""
        if parked and self.holder == arm:
            with self.lock:
                if self.holder == arm:
                    self.holder = None

    def split(self):
        """Chord Arm connected: the keys / chords-and-dial split applies."""
        return any(engine.arm is not None for arm, engine in self.engines.items() if arm != "a")

    def owned(self, arm):
        """Each arm is taught and plays only its own controls: The Keys Arm the keys, Chord Arm the chord buttons and dial."""
        return ROLES[arm]

    def ports_in_use(self, arm):
        """Serial ports the other followers have open (never probe them); arm=None: every follower's."""
        return tuple(p for a, e in self.engines.items() if a != arm
                     for p in (e.follower_port if e.arm is not None else None, e.leader_port if e.leader is not None else None) if p)

    def calibrations(self):
        return {arm: engine.calibration for arm, engine in self.engines.items()}

    def note_scan(self, ports, by=None):
        """One scan serves every follower: the others that are not connected get it too, from their own view."""
        followers = [p for p in ports if p.get("role") in ("follower", "simulator")]
        self.detected = {p["path"]: p.get("arm") for p in followers}
        self.scan = list(ports)
        for arm, engine in self.engines.items():
            if by is not None and arm != by and engine.arm is None and engine.mode == "hardware":
                engine.discovery = {**engine.discovery, "ports": [engine.port_entry(p) for p in ports], "scanned_at": time.time()}

    def plan(self, teaching_mode="leader"):
        """Which detected follower each disconnected arm should connect to, and the leader for the Keys Arm.
        A follower recognised by its motors' calibration goes to its own arm; an unrecognised one fills an arm with
        no match, Keys Arm first. Never a port in use, never one arm's follower for the other."""
        in_use = set(self.ports_in_use(None))
        usable = [p for p in self.scan if p.get("role") in ("follower", "simulator") and p["path"] not in in_use
                  and (p.get("role") == "simulator" or follower_problem(p) is None)]
        free = [arm for arm, engine in self.engines.items() if engine.arm is None]
        plan, taken = {}, set()
        for arm in free:
            mine = next((p for p in usable if p.get("arm") == arm), None)
            if mine:
                plan[arm], taken = {"port": mine["path"], "recognised": True}, taken | {mine["path"]}
        for arm in free:
            if arm not in plan:
                spare = next((p for p in usable if p.get("arm") is None and p["path"] not in taken), None)
                if spare:
                    plan[arm], taken = {"port": spare["path"], "recognised": False}, taken | {spare["path"]}
        leader = next((p for p in self.scan if p.get("leader_connectable") and p["path"] not in in_use), None)
        gets_leader = "a" if "a" in plan else next(iter(plan), None)  # Keys Arm when it connects, else the arm that does
        for arm, step in plan.items():
            use_leader = arm == gets_leader and teaching_mode == "leader" and leader is not None
            step.update(teaching_mode="leader" if use_leader else "manual", leader_port=leader["path"] if use_leader else None)
        return plan

    def available(self, arm, engine):
        """Show this arm in the console: The Keys Arm always; The Chord Arm once it is connected, known or a second follower is seen."""
        if arm == "a" or engine.arm is not None or engine.calibration:
            return True
        followers = set(self.detected) | {e.follower_port for e in self.engines.values() if e.follower_port}
        return len(followers) >= 2 or arm in self.detected.values()

    def summary(self):
        return {arm: {**engine.arm_summary(), "available": self.available(arm, engine)} for arm, engine in self.engines.items()}

    def chord_clearance(self, key, chord):
        """The closest the two arms' modelled paths come during this chord, in millimetres."""
        keys, chords = self.engines["a"], self.engines["b"]
        return kinematics.clearance(keys.chord_path(key), keys.calibration, chords.chord_path(chord), chords.calibration)

    def play_chord(self, key, chord, speed=None, play=None):
        """The Chord Arm presses and holds the chord button; the Keys Arm plays the key; once the key is down, the Chord Arm
        lets go and returns home while the Keys Arm finishes. play: more of the key's play arguments (how long it sounds,
        when it strikes). Waits until both hold at home. Stop motion (or a fault) leaves both arms where they stopped; any
        other refusal along the way lets the chord button go."""
        m.require(key in KEYS, "Choose a key (C to B) for the chord.")
        m.require(chord in CATALOG and CATALOG[chord]["kind"] == "button", "Choose a chord button (dim, min, maj, sus, 6, m7, M7, 9).")
        keys, chords = self.engines["a"], self.engines["b"]
        for engine in (keys, chords):
            m.require(engine.arm is not None, f"{NAMES[engine.arm_id]} is not connected. A chord needs both arms.")
            m.require(engine.phase == "teach_hold", f"{NAMES[engine.arm_id]} is not holding (phase: {engine.phase}). "
                      "In the operator console, send it home first.")
        try:
            gap = self.chord_clearance(key, chord)
        except ValueError as exc:
            raise m.SafetyError(str(exc)) from exc
        m.require(gap >= kinematics.CLEARANCE_MM, f"{key} with {CATALOG[chord]['name']} would bring the arms within {gap:.0f} mm of "
                  f"each other (modelled; at least {kinematics.CLEARANCE_MM:.0f} mm is needed), so it is not played.")
        with self.lock:
            m.require(self.chord is None, "A chord is already playing.")
            run = self.chord = {"id": uuid.uuid4().hex, "key": key, "chord": chord, "key_arm": "a", "chord_arm": "b"}
        stops = (keys.stops, chords.stops)
        stopped = lambda: (keys.stops, chords.stops) != stops or "fault" in (keys.phase, chords.phase)  # noqa: E731
        pace = {"chord_run": run["id"], **({"speed": speed} if speed is not None else {})}
        name = CATALOG[chord]["name"]
        # A held note (bars of it) and a wait for its beat lengthen the chord; the timeouts allow for both.
        held = float((play or {}).get("sound_s") or 0.0)
        due = max(0.0, float(((play or {}).get("rhythm") or {}).get("at") or 0.0) - time.monotonic())
        try:
            self.command(chords, "chord_press", {"control": chord, **pace})
            self.wait(lambda: chords.phase != "teach_play")
            m.require(not stopped(), "Stopped while pressing the chord button.")
            m.require(chords.chord_held == chord and not chords.chord_pressing,
                      f"Chord Arm did not get {name} held down: {chords.message}")
            press = float("inf")
            try:
                self.command(keys, "teach_play", {**(play or {}), "control": key, "chord": chord, **pace})
                press = keys.teach.recording["marks"]["press"] + RELEASE_AFTER_S
                self.wait(lambda: keys.phase != "teach_play" or (keys.play_t or 0) >= press, CHORD_TIMEOUT_S + due)
            finally:  # never while the Keys Arm is still on its way down to the key
                if not stopped() and chords.phase == "teach_hold" and chords.chord_held == chord and \
                        (keys.phase != "teach_play" or (keys.play_t or 0) >= press):
                    self.command(chords, "chord_release", pace)
            self.wait(lambda: "teach_play" not in (keys.phase, chords.phase), CHORD_TIMEOUT_S + held)
            m.require(not stopped(), "Stopped during the chord; both arms hold where they are.")
            m.require(keys.teach_played == key, f"Keys Arm did not play {key}: {keys.message}")
            self.wait(lambda: keys.parked_now[0] and chords.parked_now[0], 2.0)  # so the next chord sees both parked
        finally:
            with self.lock:
                self.chord = None
        return {"key": key, "chord": chord, "clearance_mm": round(gap)}

    @staticmethod
    def command(engine, action, args):
        """One step of a chord, through the console's live session like any API command."""
        receipt = engine.api_submit(action, args)
        Rig.wait(lambda: engine.receipts.get(receipt["id"], {}).get("status") != "queued")
        done = engine.receipts.get(receipt["id"], {})
        if done.get("status") != "complete":
            raise m.SafetyError(f"{NAMES[engine.arm_id]}: {done.get('message') or 'did not finish'}")

    @staticmethod
    def wait(done, seconds=CHORD_TIMEOUT_S):
        deadline = time.monotonic() + seconds
        while not done():
            m.require(time.monotonic() < deadline, "The chord took too long; stop motion and check both arms.")
            time.sleep(0.01)

    CONTROLS = tuple(CATALOG)

    @staticmethod
    def control_arm(control):
        m.require(control in CATALOG, "Unknown control.")
        return owner(control)

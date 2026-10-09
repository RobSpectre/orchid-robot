"""Two followers on one Orchid: arm A plays the keys, arm B the chord buttons and voicing dial.

The app always has both; arm B shows up once a second follower is detected, connected or remembered. Each arm is only
ever taught and plays its own controls. Each follower is recognised by the calibration in its motors (discovery.matches),
so arm A's hardware is never connected, or recalibrated, as arm B.

Each follower has its own Engine (connection, calibration, taught controls, home, worker thread); the rig holds
what they share. Their reach overlaps, and nothing here models the arms' geometry, so the interlock keeps them
apart by rule: an arm may leave its home only while the other is parked (holding still within PARKED_DEG of its
own taught home or rest, or not connected), and one claim, taken under a single lock, decides which arm may be away from
home. A move back to home needs only the other arm to be still, so neither can be stranded away from home.
"""
from __future__ import annotations

import threading

import time

from . import motion as m
from .controls import CATALOG, EXTRA_CONTROLS
from .discovery import follower_problem
from .motion import KEYS

ARMS = ("a", "b")
ROLES = {"a": tuple(KEYS), "b": tuple(EXTRA_CONTROLS)}
LABELS = {"a": "Arm A · keys", "b": "Arm B · chords & dial"}
PARKED_DEG = 5.0
# Actions that drive the motors away from where the arm is.
LEAVES_HOME = ("teach_follow", "teach_play", "teach_sequence", "teach_go_rest", "tune_start", "leader_resume",
               "move_home", "control_start", "test", "retry", "next", "home_start", "leader_hold", "dial_capture_start")
GOES_HOME = ("teach_go_home",)


def owner(control):
    return next(arm for arm, controls in ROLES.items() if control in controls)


class LeaderStore:
    """One leader calibration for both followers, kept in arm A's store."""

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
            parked, why = other.parked_now  # published by the other arm's own thread; never its lock
            name = LABELS[other.arm_id].split(" ·")[0]
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
        """Arm B connected: the keys / chords-and-dial split applies."""
        return any(engine.arm is not None for arm, engine in self.engines.items() if arm != "a")

    def owned(self, arm):
        """Each arm is taught and plays only its own controls: arm A the keys, arm B the chord buttons and dial."""
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
        """Which detected follower each disconnected arm should connect to, and the leader for arm A.
        A follower recognised by its motors' calibration goes to its own arm; an unrecognised one fills an arm with
        no match, arm A first. Never a port in use, never one arm's follower for the other."""
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
        gets_leader = "a" if "a" in plan else next(iter(plan), None)  # arm A when it connects, else the arm that does
        for arm, step in plan.items():
            use_leader = arm == gets_leader and teaching_mode == "leader" and leader is not None
            step.update(teaching_mode="leader" if use_leader else "manual", leader_port=leader["path"] if use_leader else None)
        return plan

    def available(self, arm, engine):
        """Show this arm in the console: arm A always; arm B once it is connected, known or a second follower is seen."""
        if arm == "a" or engine.arm is not None or engine.calibration:
            return True
        followers = set(self.detected) | {e.follower_port for e in self.engines.values() if e.follower_port}
        return len(followers) >= 2 or arm in self.detected.values()

    def summary(self):
        return {arm: {**engine.arm_summary(), "available": self.available(arm, engine)} for arm, engine in self.engines.items()}

    CONTROLS = tuple(CATALOG)

    @staticmethod
    def control_arm(control):
        m.require(control in CATALOG, "Unknown control.")
        return owner(control)

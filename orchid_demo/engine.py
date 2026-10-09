"""One local worker owns the motors. HTTP handlers never drive the bus."""
from __future__ import annotations

from collections import OrderedDict, deque
from copy import deepcopy
from pathlib import Path
import queue
import threading
import time
import uuid

from . import motion as m
from . import dial
from . import home
from .controls import CATALOG, group_members
from .devices import HardwareArm, HardwareLeader, SimulatedArm, validate_calibration
from .discovery import discover_arms, follower_problem, leader_problem, matches
from .diagnostics import EventLogger
from .incidents import ERROR_KINDS, IncidentStore
from . import leader as leader_motion
from .leader import LeaderController, TEACH_PHASES, FOLLOW_LEASE
from .storage import Repository
from . import teach as teach_motion
from . import tune as tune_rules
from .telemetry import motor_status, pose_angles

RANGE_MOTORS = tuple(name for name in m.MOTORS if name != "wrist_roll")
REQUIRED_TRIALS = 3
LEASE_SECONDS = 5.0
TEACHING_WORKFLOW_VERSION = "leader-record-replay-v1"
CALIBRATION_SETUP_PHASES = ("connected", "ready", "calibration_midpoint", "calibration_range", "calibration_review")
TEACH_SESSION_PHASES = ("teach_hold", "teach_follow", "teach_record", "teach_play")
RECORDING_FORMAT = "leader_recording_v1"
WAYPOINT_FORMAT = "leader_waypoints_v1"
RECORDING_FORMATS = (RECORDING_FORMAT, WAYPOINT_FORMAT)
# Neighbouring steps must stay close (a far one means it was captured in the wrong place). The listed joints
# are expected to change: the jaws open and close on the dial, and the turn is a wrist roll.
STROKE_PAIRS = {"key": (("hover", "touch", ()), ("touch", "press", ())),
                "dial": (("hover", "open", ("gripper",)), ("open", "lower", ("gripper",)), ("lower", "grip", ("gripper",)))}
DIAL_SHARED = ("hover", "open", "lower", "grip")  # one knob: CW and CCW share every taught step
DIAL_DIRECTIONS = {"voicing.cw": 20.0, "voicing.ccw": -20.0}
DEFAULT_TEACH_SETTINGS = {"speed": 1.0, "press_s": teach_motion.PRESS_DWELL_S, "press_hardness": teach_motion.PRESS_HARDNESS}
API_ACTIONS = ("teach_play", "teach_sequence", "teach_settings", "teach_configure", "teach_hold", "teach_go_home", "teach_go_rest")  # default wrist turn per direction (deg); set per direction
STEP_HINTS = {"touch": " (the key just touched, not pressed)", "press": " (press only until it sounds)",
              "open": " (jaws open, still above the knob)", "lower": " (lowered around the knob, not touching it)",
              "grip": " (jaws closed on the knob)"}


def shift_roll(deg, ticks):
    """A wrist-roll angle (LeRobot degrees) after its encoder zero moves by `ticks`: same physical angle, new number."""
    raw = (deg * 4095 / 360 + 2047.5 + ticks) % 4096
    return round((raw - 2047.5) * 360 / 4095, 3)


def stroke_gap(a, b, ignore=()):
    return teach_motion.max_gap({k: v for k, v in a.items() if k not in ignore}, {k: v for k, v in b.items() if k not in ignore})


class GuardedController(m.Controller):
    def __init__(self, *args, guard, feedback, **kwargs):
        super().__init__(*args, **kwargs)
        self.guard, self.feedback = guard, feedback

    def tick(self, target, stage):
        self.guard()
        current = super().tick(target, stage)
        self.feedback(current, stage)
        return current

    def arm_here(self):
        # A stop or expired lease during the stability check must precede enable.
        self.guard()
        if prepare := getattr(self.arm, "prepare_motion", None):
            prepare(guard=self.guard)
        current = m.stable_pose(self.arm, self.sleep)
        self.check_start(current)
        self.guard()
        self.enabled = True
        self.arm.arm_at_current(current)
        self.previous, self.last_tick, self.started = current, None, None
        return current

    def retreat(self):
        """Follow this reversed stroke once, starting at the measured press.

        Never align back to the press or run the downstroke again. The operator
        has already taught and captured it; only contact then hover remain.
        """
        self.guard()
        m.require(self.enabled, "Establish the captured press hold before retreating.")
        current = self.arm.read()
        self.arm.require_torque(True)
        self.check_start(current)
        self.previous, self.last_tick, self.started = dict(current), None, self.clock()
        self.record(stage="retreat_begin", actual=current)
        for pose in self.path[1:]:
            for target in m.segment(current, pose):
                self.tick(target, "retreat")
            self.settle(pose, "retreat_settle")
            current = pose
        self.started = None
        self.record(stage="retreat_complete", actual=current)


class Engine:
    def __init__(self, directory: Path, mode="simulation", *, clock=time.monotonic, sleep=time.sleep,
                 hardware_factory=HardwareArm, leader_factory=HardwareLeader, port_scanner=discover_arms,
                 key_checker=None, owns=None, leader_store=None):
        m.require(mode in ("simulation", "hardware"), "Unknown operating mode")
        self.mode, self.clock, self.sleep = mode, clock, sleep
        self.hardware_factory = hardware_factory
        self.leader_factory = leader_factory
        self.port_scanner = port_scanner
        self.discovery = {"ports": [], "scanned_at": None, "scanning": False, "warnings": [], "error": None}
        if mode == "simulation":
            self.discovery["ports"] = [{"path": "simulator", "description": "Practice arm · no hardware",
                                        "role": "simulator", "motor_ids": list(range(1, 7)), "voltage": 12.0,
                                        "connectable": True, "leader_connectable": False, "problem": None},
                                       {"path": "simulator-2", "description": "Second practice arm · no hardware",
                                        "role": "simulator", "motor_ids": list(range(1, 7)), "voltage": 12.0,
                                        "connectable": True, "leader_connectable": False, "problem": None},
                                       {"path": "simulator-leader", "description": "Practice leader", "role": "leader",
                                        "motor_ids": list(range(1, 7)), "voltage": 5.2,
                                        "connectable": False, "leader_connectable": True, "problem": "Leader input"}]
        self.repo = Repository(directory, mode)
        self.event_log = EventLogger(self.repo.directory)
        self.calibration = self.repo.get("calibration")
        self.home = self.repo.get("home")
        # Two followers share one leader, so its calibration can live in the other arm's store (rig.LeaderStore).
        self.leader_store = leader_store
        self.leader_calibration = leader_store.get() if leader_store else self.repo.get("leader_calibration")
        self.fixed_owns = tuple(owns) if owns else None  # None: the rig decides (rig.Rig.owned)
        self.rig, self.arm_id, self.parked_now = None, "a", (True, "not connected")
        self.leader_port = None  # the leader's serial port while it is open here (the rig never probes it)
        # The connected follower's motors do not carry this arm's saved calibration: it may be the other arm.
        self.calibration_foreign = False
        self.leader = None
        self.leader_calibrated = False
        self.leader_current = self.leader_torque = self.leader_feedback_at = None
        self.teaching_mode = "manual"
        self.calibration_target = "follower"
        self.leader_visible_until = 0.0
        self.simulated_leader_input = None
        self.fixture = self.repo.get("fixture", {"label": "Orchid demo", "id": ""})
        self.notes = self.repo.notes()
        self.controls = self.repo.controls()
        self.events = self.repo.events()
        self.instance_id = uuid.uuid4().hex
        self.revision, self.phase = 0, "disconnected"
        self.message = "Connect the practice arm to explore the complete workflow." if mode == "simulation" else "Secure the arm clear of Orchid, then choose its follower port."
        self.arm = self.controller = self.log = None
        self.teach = None  # teach_motion.Session: leader record -> replay, same loop as teach_key.py
        self.teach_played = None
        self.teach_points, self.teach_points_for = {}, None  # waypoints being taught, and for which control
        self.teach_returning = False  # the automatic press -> home return after capturing a press
        self.teach_going_home = False  # an operator "Go to home" move
        self.teach_home_doc = self.repo.get("teach_home")  # cached: published every control tick
        self.teach_rest_doc = self.repo.get("teach_rest")  # a parking pose (Go to rest)
        self.teach_dial_doc = self.repo.get("teach_dial")  # hover/open/lower/grip shared by both dial directions
        self.teach_settings = {**DEFAULT_TEACH_SETTINGS, **(self.repo.get("teach_settings") or {})}
        self.teach_sequence_label = None
        self.teach_going_rest = None  # "rest" during an operator Go to rest
        # What Orchid sent while a control played (keycheck.KeyChecker, via Orchid Studio); None = not checked.
        self.key_checker, self.key_check, self.plays, self.play_steps = key_checker, None, 0, None
        self.tune = None  # a MIDI-guided tune-up of one key (tune.py); its candidate points are never saved until it passes
        self.tune_queue, self.tune_results, self.tune_next_at = [], {}, 0.0  # keys still to tune; each key's last result
        self.follower_port = None
        self.current = self.torque = None
        self.feedback_at = None
        self.diagnostics = None
        self.diagnostics_error = None
        self.selected = "C"
        self.capture = self.draft = None
        self.route_approach = self.route_return = None
        self.home_move_progress = None
        self.home_arrival_samples = 0
        self.recording_error = None
        self.touch_index = None
        self.trials = 0
        self.range_index = 0
        self.ranges = {}
        self.offsets = None
        self.calibrating = False
        self.calibrated = False
        self.recent = deque()
        self.last_read = None
        self.stage = None
        self.error = None
        self.actuating = False
        self.pending = False
        self.owner = None
        self.lease_until = 0.0
        self.lock = threading.RLock()
        self.commands = queue.Queue(maxsize=1)
        self.receipts = OrderedDict()
        self.stop_event = threading.Event()
        self.shutdown = threading.Event()
        self.thread = None
        self.public = {}
        self.publish()
        self.incidents = IncidentStore(self.repo.directory, error_logger=self.event_log, clock=clock)

    def record_telemetry(self):
        self.incidents.record({"phase": self.phase, "stage": self.stage,
                               "follower": self.current, "leader": self.leader_current,
                               "torque": self.torque, "leader_torque": self.leader_torque,
                               "feedback_at": self.feedback_at, "leader_feedback_at": self.leader_feedback_at,
                               "controller": getattr(self.controller, "last_sample", {})})

    def error_context(self):
        controller = self.controller
        return {"mode": self.mode, "instance_id": self.instance_id, "phase": self.phase,
                "control": self.selected, "follower_port": self.follower_port,
                "motion_log": str(getattr(self.log, "name", "")),
                "last_cached_follower": deepcopy(self.current), "last_cached_leader": deepcopy(self.leader_current),
                "last_cached_torque": deepcopy(self.torque), "last_cached_leader_torque": deepcopy(self.leader_torque),
                "feedback_at": self.feedback_at, "leader_feedback_at": self.leader_feedback_at,
                "sample": deepcopy(getattr(controller, "last_sample", {})),
                "motor_setup": deepcopy(getattr(self.arm, "position_control_setup", {})),
                "calibration": deepcopy(self.calibration), "leader_calibration": deepcopy(self.leader_calibration),
                "fixture": deepcopy(self.fixture), "home": deepcopy(self.home),
                "diagnostics": deepcopy(self.diagnostics),
                "recent_events": [{k: e[k] for k in ("created", "kind", "message") if k in e} for e in self.events[:40]],
                "leader_port": self.leader_port if self.leader else None, "teaching_mode": self.teaching_mode,
                "follower_voltage_at_connect": getattr(self.arm, "voltage", None),
                "leader_voltage_at_connect": getattr(self.leader, "voltage", None),
                "policy": {"motion": {k: v for k, v in vars(m).items() if k.isupper() and isinstance(v, (int, float, str))},
                           "leader": {k: v for k, v in vars(leader_motion).items() if k.isupper() and isinstance(v, (int, float, str))},
                           "leader_driver": deepcopy(getattr(controller, "settings", {}))},
                "boundary_joints": list(getattr(controller, "boundary_joints", []))}

    def report_error(self, kind, exc, detail=None):
        # Cached data only. Reporting must never open ports or add motor reads.
        context = {**self.error_context(), **(detail or {})}
        message = str(exc) or type(exc).__name__
        self.incidents.capture(kind, message, context, exc=exc)
        self.event_log.emit(kind, message, context, exc)

    def event(self, kind, message, detail=None, *, exc=None):
        context = self.error_context() if kind in ERROR_KINDS else {"mode": self.mode, "phase": self.phase}
        context.update(detail or {})
        if kind in ERROR_KINDS:
            self.incidents.capture(kind, message, context, exc=exc)
        # Emit before SQLite so a database failure cannot swallow the cause.
        self.event_log.emit(kind, message, context, exc)
        self.repo.event(kind, message, context)
        self.events = self.repo.events()

    def transition(self, phase, message):
        self.phase, self.message = phase, message
        self.revision += 1
        self.error = None
        self.event("workflow", message, {"phase": phase, "key": self.selected})
        self.publish()

    def key_statuses(self):
        return self.statuses(self.notes)

    def statuses(self, entries):
        signature = m.fingerprint(self.calibration or {})
        result = {}
        for name, entry in entries.items():
            if entry and entry.get("format") in RECORDING_FORMATS:
                valid = (entry.get("calibration_sha256") == signature and entry.get("mode") == self.mode
                         and not self.waypoint_problem(entry))
                trials = entry.get("verification", {}).get("successful_trials", 0) if valid else 0
                result[name] = {"status": "registered" if valid else "needs_reteach",
                                "trials": trials, "saved_at": entry.get("saved_at"), "recorded": valid,
                                **({"turn_degrees": entry["turn_degrees"]} if "turn_degrees" in entry else {}),
                                **({"press_s": entry["press_s"]} if "press_s" in entry else {})}
                continue
            valid = bool(entry and entry.get("calibration_sha256") == signature
                         and entry.get("fixture_id") == self.fixture["id"]
                         and entry.get("mode") == self.mode)
            if valid and (self.teaching_mode == "leader" or entry.get("home_motion")):
                valid = bool(self.home_ready and entry.get("home_motion", {}).get("home_id") == self.home["id"])
            trials = entry.get("verification", {}).get("successful_trials", 0) if valid else 0
            result[name] = {"status": "registered" if trials >= REQUIRED_TRIALS else "testing" if valid else "needs_reteach" if entry else "empty",
                            "trials": trials, "saved_at": entry.get("saved_at") if entry else None, "recorded": False}
        return result

    @property
    def home_ready(self):
        return bool(self.home and self.calibrated and self.home.get("calibration_sha256") == m.fingerprint(self.calibration)
                    and self.home.get("fixture_id") == self.fixture["id"] and self.home.get("mode") == self.mode)

    @property
    def control(self):
        return CATALOG[self.selected]

    @property
    def is_dial(self):
        return self.control["kind"] == "dial"

    def save_entry(self, entry):
        if self.selected in m.KEYS:
            self.repo.save_note(self.selected, entry)
            self.notes = self.repo.notes()
        else:
            self.repo.save_control(self.selected, entry)
            self.controls = self.repo.controls()
        self.events = self.repo.events()

    def ready_message(self):
        if self.teaching_mode == "leader":
            return "Torque OFF. Start at the taught home, support the follower, and establish its hold. Use the leader to teach the approach."
        if self.is_dial:
            return "Torque OFF. Start just clear of the large voicing dial; teach a small turn, lift-off, and clear return."
        if self.control["kind"] == "button":
            return f"Torque OFF. Support the arm and start with the pad hovering above {self.control['label']}."
        return f"Torque OFF. Support the arm and start with the pad hovering above {self.selected}."

    MOVING_PHASES = ("teach_play", "teach_follow", "teach_record", "home_moving", "home_positioning", "home_arrival",
                     "home_approach", "home_return", "testing", "retreating", "note_hover", "note_pressed", "note_touch",
                     "dial_approach", "dial_contact", "dial_turned", "dial_lifted",
                     "calibration_midpoint", "calibration_range", "calibration_review")  # hand-moved: not parked either

    def parked(self):
        """(parked, why not): out of the other arm's way, i.e. still and within rig.PARKED_DEG of its home or its rest
        (both are poses taught for parking the arm clear of the other one)."""
        if self.arm is None:
            return True, "not connected"
        if self.phase in self.MOVING_PHASES or (self.teach and self.teach.mode != "holding"):
            return False, "moving"
        poses = {"home": self.shared_teach_home(), "rest": self.shared_teach_rest()}
        try:
            pose = (self.teach.measured if self.teach else
                    self.arm.joint_action(self.current) if self.current and self.calibrated else None)
        except Exception:  # a conversion failure only means the position is unknown
            pose = None
        if not poses["home"] or pose is None:
            return False, "position unknown: no home set or not calibrated"
        from .rig import PARKED_DEG
        joints = ("shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_flex")
        off = {name: max(abs(pose[j] - point["measured"][j]) for j in joints) for name, point in poses.items() if point}
        name = min(off, key=off.get)
        return (True, f"at {name}") if off[name] <= PARKED_DEG else (False, f"{off['home']:.0f}° from home")

    def publish(self):
        if self.selected not in self.owns:  # e.g. arm B's first start: never default to the other arm's control
            self.selected = self.owns[0]
        parked = self.parked()
        with self.lock:
            reference_ready = self.calibrated or (self.calibrating and self.calibration_target == "follower" and self.offsets is not None
                                                   and self.phase != "calibration_midpoint")
            leader_recording = self.calibrating and self.calibration_target == "leader"
            leader_reference = self.leader_calibrated or (leader_recording and self.offsets is not None)
            target = (self.controller.previous if self.controller and self.controller.enabled
                      and self.phase != "fault" else None)
            self.public = {
                "instance_id": self.instance_id, "revision": self.revision, "mode": self.mode,
                "teaching_workflow_version": TEACHING_WORKFLOW_VERSION,
                "phase": self.phase, "message": self.message, "error": self.error,
                "connected": self.arm is not None, "calibrated": self.calibrated,
                "calibrating": self.calibrating,
                "calibration_target": self.calibration_target,
                "teaching_mode": self.teaching_mode,
                "leader": {"connected": self.leader is not None, "calibrated": self.leader_calibrated,
                           "position": deepcopy(self.leader_current), "torque": deepcopy(self.leader_torque),
                           "feedback_at": self.leader_feedback_at, "voltage": self.leader.voltage if self.leader else None,
                           "calibration": deepcopy(self.leader_calibration),
                           "calibration_id": m.fingerprint(self.leader_calibration)[:12] if self.leader_calibration else None,
                           "pose_reference_ready": leader_reference,
                           "pose_angles": pose_angles(self.leader_current, self.leader_calibration if self.leader_calibrated else None, leader_reference),
                           "motor_status": motor_status(self.leader_current, self.leader_torque, self.leader_calibration, self.ranges,
                                                        reference_ready=leader_reference, recording=leader_recording)},
                "leader_teaching": bool(self.teach) or (isinstance(self.controller, LeaderController) and self.controller.enabled),
                "leader_following": bool(self.teach and self.teach.mode in ("aligning", "following"))
                                    or (isinstance(self.controller, LeaderController) and self.controller.engaged),
                "teach": None if not self.teach else {
                    "mode": self.teach.mode, "recording": self.teach.frames is not None, "leader": self.teach.read_leader is not None,
                    "recorded_seconds": self.teach.recorded_seconds, "progress": self.teach.progress,
                    "warning": self.teach.warning, "played": self.teach_played, "roll_guard": self.teach.roll_guard,
                    "clipped_steps": self.teach.clipped_steps, "speed": self.teach.speed,
                    "points": [n for n in self.point_names(self.teach_points_for) if n in self.teach_points],
                    "sequence": self.teach_sequence_label,
                    "points_for": self.teach_points_for,
                    "home_saved": self.shared_teach_home() is not None, "returning": self.teach_returning,
                    "going_home": self.teach_going_home,
                    "home_saved_at": (self.teach_home_doc or {}).get("saved_at") if self.shared_teach_home() else None,
                    "rest_saved": self.shared_teach_rest() is not None, "going_rest": bool(self.teach_going_rest),
                    "rest_saved_at": (self.teach_rest_doc or {}).get("saved_at") if self.shared_teach_rest() else None},
                "leader_gripper_enabled": isinstance(self.controller, LeaderController) and self.controller.follow_gripper,
                "leader_limited": isinstance(self.controller, LeaderController) and self.controller.limited,
                "leader_boundary_joints": list(self.controller.boundary_joints) if isinstance(self.controller, LeaderController) else [],
                "home": deepcopy(self.home), "home_ready": self.home_ready,
                "home_error": m.distance(self.current, self.home["pose"]) if self.home_ready and self.current else None,
                "at_home": bool(self.home_ready and self.current and home.at_home(self.current, self.home["pose"])),
                "home_motion": bool(self.draft and self.draft.get("home_motion")),
                "home_move_progress": self.home_move_progress,
                "route_samples": len(self.route_approach or []) + len(self.route_return or []),
                "recording_error": self.recording_error,
                "simulated_leader_input": self.simulated_leader_input is not None,
                "calibration_id": m.fingerprint(self.calibration)[:12] if self.calibration else None,
                "discovery": deepcopy(self.discovery),
                "fixture": deepcopy(self.fixture), "selected": self.selected,
                "position": deepcopy(self.current), "torque": deepcopy(self.torque),
                "feedback_at": self.feedback_at, "heartbeat": time.time(),
                "motor_status": motor_status(self.current, self.torque, self.calibration, self.ranges,
                                             reference_ready=reference_ready, recording=self.calibrating and not leader_recording,
                                             target=target, diagnostics=self.diagnostics),
                "pose_angles": pose_angles(self.current, self.calibration if self.calibrated else None, reference_ready),
                "pose_reference_ready": reference_ready,
                "diagnostics_at": (self.diagnostics or {}).get("sampled_at"),
                "diagnostics_error": self.diagnostics_error,
                "voltage": self.arm.voltage if self.arm else None,
                "keys": self.key_statuses(), "required_trials": REQUIRED_TRIALS,
                "controls": self.statuses(self.controls), "catalog": deepcopy(CATALOG),
                "selected_control": deepcopy(self.control),
                "dial_reference": (self.draft or self.capture or {}).get("reference"),
                "dial_expected_effect": (self.draft or self.capture or {}).get("expected_effect"),
                "dial_return_error": (m.distance(self.capture["path"][0], self.current)
                                      if self.is_dial and self.capture and self.current else None),
                "trials": self.trials, "motion_stage": self.stage,
                "teach_settings": deepcopy(self.teach_settings),
                "key_check": deepcopy(self.key_check), "key_check_available": self.key_checker is not None,
                "tune_limits": {"speed": tune_rules.FIND_SPEED, "hardness": tune_rules.FIND_HARDNESS, "margin": tune_rules.TOUCH_MARGIN,
                                "depth": tune_rules.PRESS_DEPTH, "passes": tune_rules.VERIFY_PASSES, "limit_deg": tune_rules.LIMIT_DEG},
                "tune_queue": list(self.tune_queue), "tune_results": deepcopy(self.tune_results),
                "tune": None if not self.tune else {k: deepcopy(self.tune.get(k)) for k in (
                    "control", "name", "status", "phase", "trial", "passes", "speed", "hardness", "margin", "depth", "log", "message")},
                "poses_saved": {"home": bool(self.calibrated and self.shared_teach_home()),
                                "rest": bool(self.calibrated and self.shared_teach_rest())},
                "capture_samples": len(self.capture["path"]) if self.capture else 0,
                "range_motor": RANGE_MOTORS[self.range_index] if self.phase == "calibration_range" else None,
                "range_index": self.range_index, "ranges": deepcopy(self.ranges),
                "calibration": deepcopy(self.calibration), "events": deepcopy(self.events),
                "last_receipt": deepcopy(next(reversed(self.receipts.values()))) if self.receipts else None,
                "arm": self.arm_id, "owns": list(self.owns), "parked": parked[0], "parked_reason": parked[1],
                "calibration_foreign": self.calibration_foreign, "calibration_dependents": self.calibration_dependents(),
            }
        # Outside our lock: the rig reads this plain value, never our lock, so two arms cannot deadlock.
        self.parked_now = parked
        if self.rig:
            self.rig.update(self.arm_id, parked[0])

    def arm_summary(self):
        s = self.snapshot()
        return {"arm": self.arm_id, "phase": s["phase"], "connected": s["connected"], "calibrated": s["calibrated"],
                "message": s["message"], "parked": s["parked"], "parked_reason": s["parked_reason"],
                "pose_angles": s.get("pose_angles"), "leader": s["leader"]["connected"], "owns": s["owns"],
                "powered": any(v == 1 for v in (s.get("torque") or {}).values()),
                "tuning": bool(s.get("tune_queue")) or (s.get("tune") or {}).get("status") == "running"}

    def snapshot(self):
        with self.lock:
            return {**deepcopy(self.public), "pending": self.pending, "operator": self.owner,
                    "lease_live": self.lease_until > time.monotonic(),
                    "worker_alive": self.thread is None or self.thread.is_alive()}

    def export_snapshot(self):
        with self.lock:
            return deepcopy({"schema_version": 2, "application": "orchid-demo", "mode": self.mode,
                             "units": m.UNITS, "exported_at": m.stamp(), "calibration": self.calibration,
                             "leader_calibration": self.leader_calibration, "home": self.home,
                             "fixture": self.fixture, "keys": self.notes,
                             "controls": self.controls, "events": self.events})

    def heartbeat(self, owner, *, leader_visible=True):
        with self.lock:
            now = time.monotonic()
            m.require(self.owner in (None, owner) or self.lease_until <= now,
                      "Another browser is operating this arm. This window is read-only.")
            # Once a powered operation loses its owner, a new owner cannot revive it.
            if self.owner and self.lease_until <= now and (self.teach or (self.controller and self.controller.enabled)):
                self.stop_event.set()
            self.owner = owner
            self.lease_until = now + LEASE_SECONDS
            self.leader_visible_until = now + FOLLOW_LEASE if leader_visible else 0.0

    def submit(self, owner, command_id, action, revision, args):
        with self.lock:
            m.require(self.thread is None or self.thread.is_alive(),
                      "Motor worker is not running. Support the arm and restart the local app.")
            m.require(self.owner == owner and self.lease_until > time.monotonic(), "Acquire operator control first.")
            signature = m.fingerprint({"action": action, "revision": revision, "args": args})
            if command_id in self.receipts:
                prior = self.receipts[command_id]
                m.require(prior["signature"] == signature, "Command ID was reused with different contents.")
                return deepcopy(prior)
            m.require(action == "stop" or revision == self.revision, "The workflow changed. Refresh before trying again.")
            m.require(action == "stop" or not self.pending, "An operation is already in progress.")
            receipt = {"id": command_id, "action": action, "status": "queued", "signature": signature}
            self.receipts[command_id] = receipt
            while len(self.receipts) > 128:
                self.receipts.popitem(last=False)
            if action == "stop":
                self.stop_event.set()
                receipt["status"] = "accepted"
            else:
                self.pending = True
                self.commands.put_nowait({"id": command_id, "owner": owner, "action": action,
                                          "revision": revision, "args": args, "created": time.monotonic()})
            return deepcopy(receipt)

    def guard(self):
        m.require(not self.shutdown.is_set(), "Application is shutting down")
        m.require(not self.stop_event.is_set(), "Motion stopped by the operator")
        with self.lock:
            m.require(self.lease_until > time.monotonic(), "Operator connection lost. Motion has been stopped.")

    def supported(self, args):
        m.require(args.get("supported") is True, "Confirm that the arm's weight is supported.")

    def open_arm(self, device, role, port):
        try:
            device.open()
        except Exception as exc:
            # Preserve the role and the already-read values before pair cleanup
            # discards both device objects. Reporting never probes the motors.
            self.report_error("connection_failed", exc, {
                "failed_role": role, "failed_port": port,
                "connection_readings": deepcopy(getattr(device, "connection_readings", {})),
            })
            message = f"{role.title()} connection failed ({port}): {exc}"
            if "Input voltage error" in str(exc):
                message += (" Check this arm's motor voltage rating, power supply, and wiring before retrying. "
                            "No motor settings or torque commands were sent during connection.")
            raise m.SafetyError(message) from exc

    def require_phase(self, *phases):
        m.require(self.phase in phases, "That action is not available at this step.")

    def stable(self, enforce_limits=True):
        if isinstance(self.controller, LeaderController) and self.controller.enabled:
            m.require(not self.controller.engaged, "Pause following before capturing a waypoint.")
            samples = []
            for _ in range(7):
                samples.append(self.controller.tick(stage="leader_capture_hold"))
            m.require(all(max(p[n] for p in samples) - min(p[n] for p in samples) <= 2 for n in m.MOTORS),
                      "Follower did not settle within the capture window. Keep following paused and retry.")
            return samples[-1]
        samples = []
        for _ in range(7):
            self.guard()
            self.sample()
            if enforce_limits:
                self.arm.check_pose(self.current)
            samples.append(dict(self.leader_current if self.calibrating and self.calibration_target == "leader" else self.current))
            self.sleep(0.1)
        spread = {n: max(p[n] for p in samples) - min(p[n] for p in samples) for n in m.MOTORS}
        unsettled = [f"{n}: {span} ticks" for n, span in spread.items() if span > 2]
        m.require(not unsettled, "Encoder readings did not stay within the 2-tick capture window ("
                  + "; ".join(unsettled) + "). Keep supporting the arm and retry when steady.")
        return samples[-1]

    def simulate_pose(self, label):
        if self.mode != "simulation" or self.teaching_mode == "leader":
            return
        self.arm.require_torque(False)
        target = dict.fromkeys(m.MOTORS, 2047)
        if self.is_dial:
            target["shoulder_pan"] -= 240
            target["wrist_flex"] += 12 if label in ("dial_contact", "dial_turn") else 0
            if label in ("dial_turn", "dial_lift"):
                target["wrist_roll"] += 12 if self.control["direction"] == "cw" else -12
        else:
            target["shoulder_pan"] += (m.KEYS.index(self.selected) * 20 if self.selected in m.KEYS
                                       else -80 - group_members(self.selected).index(self.selected) * 20)
            target["wrist_flex"] += {"pressed": 36, "touch": 24, "clear": 0}[label]
        while self.arm.current != target:
            self.guard()
            for name in m.MOTORS:
                delta = target[name] - self.arm.current[name]
                self.arm.current[name] += max(-3, min(3, delta))
            self.sample()
            self.sleep(m.PERIOD)

    def sample(self):
        start = self.clock()
        if self.leader:
            self.sample_leader()
        previous = self.current
        torque = self.arm.torque_status()
        current = self.arm.read_raw()
        # Preserve the newest manual observation even when validation below fails.
        self.incidents.record({"phase": self.phase, "stage": "manual_read", "follower": current,
                               "torque": torque, "leader": self.leader_current, "observed_at": time.time()})
        if self.phase != "connected":
            self.arm.require_torque(False)
            m.require(all(v == 0 for v in torque.values()), "Unexpected enabled motor during manual teaching")
        now = self.clock()
        m.require(now - start <= m.MAX_IO_TIME, "Motor feedback is stale. No new movement will be issued.")
        recording = self.phase in ("note_hover", "note_touch", "dial_approach", "dial_contact", "dial_turned", "dial_lifted")
        if recording and self.last_read is not None:
            m.require(now - self.last_read <= m.MAX_IO_TIME, "Recording has a gap. Re-teach this control.")
        self.current, self.torque = current, torque
        self.last_read, self.feedback_at = now, time.time()
        if self.phase == "calibration_range":
            motor = RANGE_MOTORS[self.range_index]
            reading = self.leader_current if self.calibration_target == "leader" else current
            span = self.ranges.setdefault(motor, {"min": reading[motor], "max": reading[motor]})
            span["min"] = min(span["min"], reading[motor])
            span["max"] = max(span["max"], reading[motor])
        if recording:
            self.arm.check_pose(current)
            m.append_capture(self.capture, current, previous_observation=previous)
            if self.is_dial:
                # Validate even samples omitted by path decimation.
                dial.validate({**self.capture, "path": self.capture["path"] + [current]})
        self.publish()

    def sample_leader(self):
        start = self.clock()
        torque = self.leader.torque_status()
        current = self.leader.read_raw()
        if self.phase != "connected":
            m.require(all(v == 0 for v in torque.values()), "Leader must stay torque off.")
        m.require(self.clock() - start <= m.MAX_IO_TIME, "Leader feedback is stale.")
        self.leader_current, self.leader_torque = current, torque
        self.leader_feedback_at = time.time()

    def leader_feedback(self, current):
        self.leader_current = dict(current)
        self.leader_torque = dict.fromkeys(m.MOTORS, 0)
        self.leader_feedback_at = time.time()

    def record_leader_sample(self, current):
        if self.recording_error:
            return
        try:
            if self.phase in home.TRAVEL_PHASES:
                path = self.route_approach if self.phase == "home_approach" else self.route_return
                if path:
                    home.append(path, current, self.current)
            if self.capture and self.phase in TEACH_PHASES:
                m.append_capture(self.capture, current, previous_observation=self.current)
                self.validate_leader_recording(current)
        except m.SafetyError as exc:
            # A recording unsuitable for automatic playback is not a driver
            # failure. Keep the operator's clutch/control usable, invalidate the
            # recording, and prevent it being captured or handed to playback.
            self.recording_error = f"Recording cannot be used for playback: {exc} Following remains available; return to home, release and re-teach this route."
            self.event("recording_rejected", self.recording_error)

    def validate_leader_recording(self, target):
        if not self.capture:
            return
        # Recording validation uses measured samples only, never proposed goals.
        candidate = {**self.capture, "path": self.capture["path"] + [self.current, target]}
        m.validate_entry(candidate)
        if self.is_dial:
            dial.validate(candidate)

    def begin_press_capture(self, current):
        self.capture = m.teaching_entry(self.arm, True, self.fixture["label"], current)
        self.capture["contact_surface"] = self.fixture.get("tool", "padded_gripper")
        self.touch_index = None
        self.transition("note_hover", "Hover captured. Lower the pad to first contact without pressing the control, then capture.")

    def feedback(self, current, stage):
        self.current, self.stage = current, stage
        self.torque = dict.fromkeys(m.MOTORS, 1)
        self.feedback_at = time.time()
        self.record_telemetry()
        self.publish()

    def reset_note(self, *, keep_controller=False):
        self.simulated_leader_input = None
        self.capture = self.draft = None
        if not keep_controller:
            self.controller = None
            self.teach = None
            self.teach_played = None
        self.route_approach = self.route_return = None
        self.home_move_progress = None
        self.home_arrival_samples = 0
        self.recording_error = None
        self.touch_index = None
        self.trials = 0
        self.last_read = None
        self.stage = None
        if self.log and not keep_controller:
            self.log.close()
            self.log = None

    def clear_calibration_capture(self):
        self.offsets = None
        self.ranges, self.range_index = {}, 0
        # A reference change invalidates the previous encoder/3D sample.
        self.current = self.feedback_at = self.last_read = None
        self.leader_current = self.leader_feedback_at = None

    @property
    def calibration_arm(self):
        return self.leader if self.calibration_target == "leader" else self.arm

    @property
    def saved_calibration(self):
        return self.leader_calibration if self.calibration_target == "leader" else self.calibration

    def mark_calibrated(self, value):
        if self.calibration_target == "leader":
            self.leader_calibrated = value
        else:
            self.calibrated = value
            if value:
                self.calibration_foreign = False

    def calibration_dependents(self):
        """Taught controls recorded under this arm's saved calibration (they need it to stay valid)."""
        if not self.calibration:
            return 0
        signature = m.fingerprint(self.calibration)
        return sum(1 for entry in (*self.notes.values(), *self.controls.values())
                   if entry and entry.get("calibration_sha256") == signature)

    def begin_calibration(self):
        backup = self.calibration_arm.begin_calibration()
        self.calibrating = True
        prefix = "leader_" if self.calibration_target == "leader" else ""
        self.repo.put(prefix + "calibration_backup", {"at": m.stamp(), "hardware": backup, "saved": self.saved_calibration})

    def establish_hold(self, draft, planned, *, phase="holding", message=None):
        self.repo.put("draft", {"key": self.selected, "entry": draft})
        self.draft = draft
        previous = self.controller
        if isinstance(previous, LeaderController):
            m.require(not previous.engaged, "Pause following before switching to a test hold.")
            current = previous.tick(stage="leader_handover")
            if self.log:
                self.log.close()
        self.log = (self.repo.directory / f"trial-{uuid.uuid4().hex}.jsonl").open("x")
        motion_arm = previous.arm if isinstance(previous, LeaderController) else self.arm
        self.controller = GuardedController(motion_arm, planned, self.log, clock=self.clock, sleep=self.sleep,
                                            max_run_time=home.MAX_ROUTE_TIME if draft.get("home_motion") else m.MAX_RUN_TIME,
                                            guard=self.guard, feedback=self.feedback)
        self.controller.error_reporter = self.report_error
        self.transition("arming", "Keep hands clear while the follower maintains its powered hold." if isinstance(previous, LeaderController)
                        else "Keep supporting while the motors establish a hold at this captured position.")
        try:
            self.guard()
            if isinstance(previous, LeaderController):
                self.controller.enabled = True  # Already powered; never release/re-enable during handover.
                self.controller.check_start(current)
                self.controller.previous = dict(previous.previous)
                previous.enabled = False
            else:
                self.controller.arm_here()
            self.controller.tick(self.controller.previous, "hold_verified")
        except Exception as exc:
            self.fault(exc)
            raise
        self.transition(phase, message or "Holding position, torque ON. Gently clear your hands before testing.")

    def begin_home_return(self, draft):
        m.require(m.distance(self.route_approach[-1], draft["path"][0]) <= m.START_TOLERANCE,
                  "Lift back to the captured key clearance before recording the return home (within 6 ticks).")
        self.draft, self.capture = draft, None
        self.route_return = [dict(self.current)]
        self.controller.set_travel("return", self.current)
        self.transition("home_return", "Key motion captured. Use the leader to teach a clear return to home, pause, then finish the route.")

    def start_leader_hold(self, current, motion_arm, *, follow_gripper=False):
        previous, old_log = self.controller, self.log
        log = (self.repo.directory / f"leader-{uuid.uuid4().hex}.jsonl").open("x")
        try:
            controller = LeaderController(motion_arm, self.leader, current, log, guard=self.guard,
                                           permission=lambda: time.monotonic() < self.leader_visible_until,
                                           observe=self.record_leader_sample, leader_feedback=self.leader_feedback,
                                           feedback=self.feedback,
                                           clock=self.clock, sleep=self.sleep, follow_gripper=follow_gripper)
        except Exception:
            log.close()
            raise
        self.controller, self.log = controller, log
        self.controller.error_reporter = self.report_error
        if old_log:
            old_log.close()
        self.actuating = True
        if previous and previous.enabled:
            # Switching from a completed trial to teaching must never drop the
            # supported arm. Preserve the existing motor goals and powered hold.
            self.controller.previous = dict(previous.previous)
            self.controller.desired = dict(previous.previous)
            self.controller.gripper = previous.previous["gripper"]
            self.controller.enabled = True
            previous.enabled = False
            self.controller.tick(stage="leader_handover")
        else:
            self.controller.arm_here()

    def save_home_reference(self, current):
        reference = {"id": uuid.uuid4().hex, "pose": dict(current), "calibration_sha256": m.fingerprint(self.calibration),
                     "fixture_id": self.fixture["id"], "mode": self.mode, "saved_at": m.stamp()}
        if isinstance(self.controller, LeaderController) and self.controller.enabled:
            # The measured home can differ slightly from the holding goal under
            # gravity. Replay the same goal, and verify arrival against feedback.
            # This is a sent target, not a motor goal-register readback.
            reference["hold_target"] = dict(self.controller.previous)
        self.repo.put("home", reference)
        self.home = reference

    def read_follower_joints(self):
        raw, joints = self.arm.teleop_read()
        self.current = raw
        return dict(joints)

    def read_leader_joints(self):
        raw, joints = self.leader.teleop_read()
        self.leader_current = raw
        return dict(joints)

    @staticmethod
    def waypoint_problem(entry):
        """A saved key whose hover is not near its touch (or touch near press) would swing through that pose."""
        if entry.get("format") != WAYPOINT_FORMAT:
            return None
        points = entry["points"]
        dial = "grip" in points
        if dial and ("turn" in points or "lower" not in points):
            return "it was taught with the earlier dial steps; teach hover, open, lower and grip again"
        for before, point, ignore in STROKE_PAIRS["dial" if dial else "key"]:
            joint, gap = stroke_gap(points[point]["goal"], points[before]["goal"], ignore)
            if gap > teach_motion.STROKE_LIMIT:
                return f"its {point} is {gap:.0f}° from its {before} on {joint}, so its {before} was captured in the wrong place"
        return None

    def recording_entry(self, control):
        entry = self.notes.get(control) if control in m.KEYS else self.controls.get(control)
        status = {**self.key_statuses(), **self.statuses(self.controls)}[control]
        m.require(entry and entry.get("format") in RECORDING_FORMATS,
                  f"{CATALOG[control]['name']} has not been taught yet. Capture its points with the leader first.")
        problem = self.waypoint_problem(entry)
        m.require(not problem, f"{CATALOG[control]['name']} needs re-teaching: {problem}. Follow the leader, capture hover just above it, then touch and press.")
        m.require(status["recorded"], f"{CATALOG[control]['name']} was recorded under a different follower calibration. Record it again.")
        return entry

    def valid_pose(self, doc):
        if doc and doc.get("calibration_sha256") == m.fingerprint(self.calibration) and doc.get("mode") == self.mode:
            return doc["point"]
        return None

    def shared_teach_home(self):
        return self.valid_pose(self.teach_home_doc)

    def shared_teach_rest(self):
        return self.valid_pose(self.teach_rest_doc)

    def pose_doc(self, captured):
        return {"point": captured, "calibration_sha256": m.fingerprint(self.calibration), "mode": self.mode, "saved_at": m.stamp()}

    def go_to_pose(self, point):
        """Rate-limited move to one saved pose, through the same playback machinery as a key."""
        self.teach.play({"key": "pose", "frames": [{"t": 0.0, "goal": point["goal"], "follower": point["measured"]}]},
                        1.0, force=True, settle_s=0.0)

    @staticmethod
    def point_names(control):
        return teach_motion.DIAL_POINTS if control and CATALOG[control]["kind"] == "dial" else teach_motion.POINTS

    def shared_dial_points(self):
        doc = self.teach_dial_doc
        if doc and doc.get("calibration_sha256") == m.fingerprint(self.calibration) and doc.get("mode") == self.mode:
            return doc["points"]
        return {}

    def points_for(self, control):
        """This control's own steps (saved ones if still valid), the dial's shared hover/open/grip, and the arm's home."""
        if self.teach_points_for != control:
            entry = self.notes.get(control) if control in m.KEYS else self.controls.get(control)
            valid = {**self.key_statuses(), **self.statuses(self.controls)}[control]["recorded"]
            saved = deepcopy(entry["points"]) if entry and entry.get("format") == WAYPOINT_FORMAT and valid else {}
            if CATALOG[control]["kind"] == "dial":
                saved.update(deepcopy(self.shared_dial_points()))
            home_point = self.shared_teach_home() or saved.get("home")
            self.teach_points = {**saved, **({"home": home_point} if home_point else {})}
            if not home_point:
                self.teach_points.pop("home", None)
            self.teach_points_for = control
        return self.teach_points

    def save_teach_home(self, captured):
        self.teach_home_doc = self.pose_doc(captured)
        self.repo.put("teach_home", self.teach_home_doc)

    def save_dial_entries(self, points):
        """Both directions share the taught steps; each keeps its own turn angle."""
        for control, default in DIAL_DIRECTIONS.items():
            old = self.controls.get(control) or {}
            degrees = old.get("turn_degrees", default) if old.get("format") == WAYPOINT_FORMAT else default
            self.repo.save_control(control, {
                "format": WAYPOINT_FORMAT, "control_id": control, "points": deepcopy(points), "turn_degrees": degrees,
                "units": "lerobot_degrees_gripper_0_100", "calibration_sha256": m.fingerprint(self.calibration),
                "leader_calibration_sha256": m.fingerprint(self.leader_calibration), "fixture_id": self.fixture["id"],
                "mode": self.mode, "saved_at": m.stamp(), "verification": {"successful_trials": 0}})
        self.controls = self.repo.controls()
        self.events = self.repo.events()

    def save_control_entry(self, control, entry):
        if control in m.KEYS:
            self.repo.save_note(control, entry)
            self.notes = self.repo.notes()
        else:
            self.repo.save_control(control, entry)
            self.controls = self.repo.controls()

    def control_motion(self, control, args):
        """The full motion for a taught control. A press length or dial angle given here becomes its new default."""
        entry = self.recording_entry(control)
        if entry["format"] != WAYPOINT_FORMAT:
            return entry, "the recorded motion"
        home_point = self.shared_teach_home()
        dial = CATALOG[control]["kind"] == "dial"
        points = {**entry["points"], **(self.shared_dial_points() if dial else {}), **({"home": home_point} if home_point else {})}
        changed = {}
        try:
            if dial:
                degrees = args.get("turn_degrees", entry.get("turn_degrees", DIAL_DIRECTIONS[control]))
                m.require(type(degrees) in (int, float), "Enter the turn angle in degrees.")
                recording = teach_motion.dial_recording(points, float(degrees), control)
                changed = {"turn_degrees": float(degrees)} if degrees != entry.get("turn_degrees") else {}
                description = f"home → hover → open → lower → grip → turn {degrees:g}° → let go → raise → home"
            else:
                press_s = args.get("press_s", entry.get("press_s", self.teach_settings["press_s"]))
                m.require(type(press_s) in (int, float) and 0 <= press_s <= teach_motion.MAX_PRESS_S,
                          f"Choose a press length from 0 to {teach_motion.MAX_PRESS_S:g} seconds.")
                hardness = self.teach_settings["press_hardness"]
                recording = teach_motion.waypoint_recording(points, control, float(press_s), hardness)
                changed = {"press_s": float(press_s)} if "press_s" in args and press_s != entry.get("press_s") else {}
                description = f"home → hover → touch → press ({hardness:.0%} hardness, held {press_s:g} s) and back"
        except ValueError as exc:
            raise m.SafetyError(str(exc)) from exc
        if changed:
            self.save_control_entry(control, {**entry, **changed, "saved_at": m.stamp()})
        return recording, description

    def play_speed(self, args):
        speed = args.get("speed", self.teach_settings["speed"])
        m.require(type(speed) in (int, float) and 0.1 <= speed <= teach_motion.MAX_PLAY_SPEED,
                  f"Choose a speed from 0.1× to {teach_motion.MAX_PLAY_SPEED:g}×.")
        return float(speed)

    def start_playback(self, recording, speed, args):
        self.teach_played, self.teach_returning, self.teach_going_home, self.teach_going_rest = None, False, False, None
        self.play_steps = recording.get("steps") or [{"key": self.selected, "start": 0.0, "end": recording["frames"][-1]["t"],
                                                      "marks": recording.get("marks", {})}]
        # Faster playback also moves to the start faster, up to twice the usual ramp.
        self.teach.play(recording, speed, force=args.get("force") is True,
                        ramp_speed=teach_motion.RAMP_SPEED * min(2.0, max(1.0, speed)))

    def api_submit(self, action, args):
        """Programmatic commands act through the operator console's live session, never around it."""
        m.require(action in API_ACTIONS or action == "stop", "That action is not available through the API.")
        with self.lock:
            m.require(self.owner is not None and self.lease_until > time.monotonic(),
                      "Open the operator console (http://127.0.0.1:8081) and connect; the API only acts while it is in control.")
            # Moves need a holding follower, and only the console can start or restore that hold.
            if action in ("teach_play", "teach_sequence", "teach_go_home", "teach_go_rest") and self.phase not in TEACH_SESSION_PHASES:
                m.require(self.phase != "disconnected", "No arm is connected. Connect the follower in the operator console.")
                m.require(self.phase != "fault", f"The arm is stopped ({self.error or 'no reason recorded'}). In the operator console, "
                          "support the follower and press Hold & keep playing; the API cannot restart a stopped arm.")
                raise m.SafetyError(f"The follower is not holding (phase: {self.phase}). In the operator console, select a taught "
                                    "control and press Play, or ⌂ Home → Go to home, once; then the API can play.")
            owner, revision = self.owner, self.revision
        return self.submit(owner, uuid.uuid4().hex, action, revision, args)

    @property
    def owns(self):
        """The controls this follower plays: all of them, unless a second follower takes the chords and dial."""
        if self.fixed_owns:
            return self.fixed_owns
        return self.rig.owned(self.arm_id) if self.rig else tuple(CATALOG)

    def port_entry(self, arm):
        """A scanned arm, with which follower it is (from the calibration in its motors) when that is known."""
        identity = None
        for who, calibration in (self.rig.calibrations() if self.rig else {self.arm_id: self.calibration}).items():
            if arm.get("role") == "follower" and matches(calibration, arm.get("limits")):
                identity = who
        problem = follower_problem(arm)
        if not problem and identity not in (None, self.arm_id):
            problem = (f"This is arm {identity.upper()}'s follower: its motors carry arm {identity.upper()}'s calibration. "
                       f"Connect it as arm {identity.upper()}.")
        return {**arm, "path": arm["port"], "description": arm["role"] or "Unidentified arm", "arm": identity,
                "connectable": problem is None, "problem": problem,
                "leader_connectable": leader_problem(arm) is None, "leader_problem": leader_problem(arm)}

    def teach_control(self, args):
        selected = args.get("control", self.selected)
        m.require(isinstance(selected, str) and selected in CATALOG, "Choose an instrument control.")
        m.require(selected in self.owns, f"{CATALOG[selected]['name']} is played by the other arm.")
        self.selected = selected
        return CATALOG[selected]["name"]

    def teach_tick(self):
        event = self.teach.tick()
        self.torque = dict.fromkeys(m.MOTORS, 1)
        self.feedback_at = time.time()
        if self.teach.read_leader:
            # Leader torque was verified off at teach_begin; HardwareLeader refuses torque enable.
            self.leader_feedback_at = self.feedback_at
        self.incidents.record({"phase": self.phase, "stage": f"teach_{self.teach.mode}", "follower": self.current,
                               "leader": self.leader_current, "goal": dict(self.teach.goal),
                               "measured": dict(self.teach.measured), "observed_at": self.feedback_at})
        name = self.control["name"]
        if event == "aligned":
            upcoming = next((n for n in self.point_names(self.selected) if n not in self.points_for(self.selected)), None)
            self.transition("teach_follow", "FOLLOWING the leader 1:1. " + (
                f"Guide it to {upcoming}{STEP_HINTS.get(upcoming, '')} and capture it (Space)." if upcoming else
                "Select a step to retrain it, or Play."))
        elif event == "start_mismatch":
            self.transition("teach_hold", self.teach.warning)
            if self.tune and self.tune["status"] == "running":
                self.tune_end("failed", f"Tune-up stopped before pressing: {self.teach.warning} Nothing was saved.")
        elif event == "played" and self.teach_going_rest:
            self.teach_going_rest = None
            self.transition("teach_hold", "At rest, holding.")
        elif event == "played" and self.teach_going_home:
            self.teach_going_home = False
            self.transition("teach_hold", "At home, holding.")
        elif event == "played" and self.teach_returning:
            self.teach_returning = False
            self.transition("teach_hold", f"{name} taught: home → hover → touch → press, and back. The follower is holding at home. "
                            "Put the leader back at rest before following again. Play it to test.")
        elif event == "played" and self.teach_sequence_label:
            self.request_key_check()
            label, self.teach_sequence_label = self.teach_sequence_label, None
            self.transition("teach_hold", f"Played {label}; holding at home.")
        elif event == "played":
            self.request_key_check()
            self.teach_played = self.selected
            note = (f" {self.teach.clipped_steps} steps were limited to {teach_motion.FOLLOW_CAP:.0f}° from the measured "
                    "pose (the arm lagged or was blocked)." if self.teach.clipped_steps else "")
            # Between key presses the arm waits at home; rest is only visited on request (Go to rest).
            self.transition("teach_hold", f"Played {name}; holding at home. Space plays it again; pick another key to teach it.{note}")

    def request_key_check(self):
        """Compare what Orchid sent during this play with what was played, off the motor loop."""
        if not self.key_checker or not self.play_steps:
            return
        self.plays += 1
        self.key_check = {"play": self.plays, "status": "pending", "steps": [], "summary": "Checking the notes with Orchid Studio…"}
        self.key_checker.request({"play": self.plays, "started": self.teach.play_started, "speed": self.teach.speed,
                                  "steps": deepcopy(self.play_steps)})
        self.play_steps = None

    def collect_key_check(self):
        result = self.key_checker.poll() if self.key_checker else None
        if result and result["play"] == self.plays:  # an older play's late result is not this play's
            self.key_check = result
            self.event("key_check", result["summary"], {"status": result["status"], "steps": result["steps"]})
            if self.tune and self.tune["status"] == "running" and self.tune["waiting"]:
                self.tune_result(result)
            self.publish()

    # --- MIDI-guided tune-up (one key, operator beside the arm, console only) -----------------------------

    @property
    def tuning(self):
        return bool(self.tune and self.tune["status"] == "running") or bool(self.tune_queue)

    def tune_start(self, args):
        self.require_phase("teach_hold")
        m.require(args.get("beside_arm") is True, "Confirm you are beside the arm with Stop motion in reach.")
        m.require(self.key_checker is not None,
                  "The tune-up needs the note check: real hardware and Orchid Studio running with --sound-input Orchid.")
        controls = args.get("controls", [args.get("control")])
        m.require(isinstance(controls, list) and 1 <= len(controls) <= len(m.KEYS) and len(set(controls)) == len(controls),
                  "Choose the keys to tune.")
        for control in controls:  # all checked before anything moves
            self.tune_check(control)
        self.tune_queue = list(controls[1:])
        self.tune_begin(controls[0])

    def tune_check(self, control):
        m.require(isinstance(control, str) and control in CATALOG, "Choose a key to tune.")
        name = CATALOG[control]["name"]
        m.require(CATALOG[control]["kind"] == "key", "Only keyboard keys can be tuned: chord buttons and the dial send no notes to check.")
        entry = self.recording_entry(control)
        m.require(entry["format"] == WAYPOINT_FORMAT, f"{name} was recorded freely, not taught by points. Re-teach it with the leader first.")
        m.require(self.shared_teach_home(), "No home is set. Set home with the leader first.")
        return entry

    def tune_begin(self, control):
        entry = self.tune_check(control)
        name = self.teach_control({"control": control})
        points = {k: v for k, v in deepcopy(entry["points"]).items() if k in teach_motion.POINTS}
        # Finding starts from what was taught with the leader, so repeated calibrations cannot creep away from it.
        taught = {**points, **deepcopy(entry.get("taught_points") or {})}
        self.tune = {"control": self.selected, "name": name, "status": "running", "phase": "find", "trial": 0, "passes": 0,
                     "finds": [], "verify_trials": 0, "margin": tune_rules.TOUCH_MARGIN, "depth": tune_rules.PRESS_DEPTH,
                     "speed": tune_rules.FIND_SPEED, "hardness": tune_rules.FIND_HARDNESS, "log": [], "message": "Finding the trigger point",
                     "waiting": False, "points": deepcopy(taught), "taught": taught,
                     "press_s": entry.get("press_s", self.teach_settings["press_s"]), "next_at": self.clock()}
        self.event("tune", f"Calibration of {name} started", {"control": self.selected})
        after = f" ({len(self.tune_queue)} more after it)" if self.tune_queue else ""
        self.transition("teach_hold", f"Calibrating {name}{after}: finding where it triggers with gentle presses. "
                        "Stop motion ends it; nothing is saved unless it passes.")

    def tune_trial(self):
        tune = self.tune
        if self.rig:
            self.rig.claim(self.arm_id, "teach_play", {})
        self.selected = tune["control"]
        verify = tune["phase"] == "verify"
        speed = self.teach_settings["speed"] if verify else tune_rules.FIND_SPEED
        hardness = self.teach_settings["press_hardness"] if verify else tune_rules.FIND_HARDNESS
        points = {**tune["points"], "home": self.shared_teach_home()}
        recording = teach_motion.waypoint_recording(points, tune["control"], float(tune["press_s"]), hardness)
        tune.update(trial=tune["trial"] + 1, waiting=True, speed=speed, hardness=hardness,
                    play={"frames": recording["frames"], "bottom": recording["marks"]["press"]})
        tune["message"] = (f"Checking at your playing speed ({speed:g}×, {hardness:.0%} hardness)" if verify else
                           "Finding the trigger point (gentle press)")
        self.teach_sequence_label = None
        self.start_playback(recording, speed, {})
        self.transition("teach_play", f"Calibrating {tune['name']}: {tune['message'].lower()}. Stop motion ends it.")

    def tune_note(self, step):
        """Where on this press's commanded path the note sounded (recording time)."""
        return (step["t"] - self.teach.play_started) * self.teach.speed

    def tune_set(self, tune, note_t):
        """Touch margin deg before the trigger, press depth deg past it, on the found press's commanded path.
        Returns why not, if that press would go more than LIMIT_DEG past the taught press."""
        play, taught = tune["found_play"], tune["taught"]
        trigger, touch, press = tune_rules.around(play["frames"], note_t, play["bottom"], tune["margin"], tune["depth"])
        down = {j: taught["press"]["goal"][j] - taught["touch"]["goal"][j] for j in tune_rules.JOINTS}
        past = tune_rules.beyond(press, taught["press"], down)
        if past > tune_rules.LIMIT_DEG + 1e-9:
            return f"its press would go {past:.1f}° past where it was taught (limit {tune_rules.LIMIT_DEG:g}°)"
        tune["trigger"] = trigger
        tune["points"] = {**tune["found_points"], "touch": tune_rules.point(tune["found_points"]["touch"], touch),
                          "press": tune_rules.point(tune["found_points"]["press"], press)}
        return None

    def tune_result(self, result):
        tune = self.tune
        tune["waiting"] = False
        step = result["steps"][0] if result["steps"] else {"status": result["status"], "text": result["summary"]}
        status, text = step["status"], step.get("text") or step["status"]
        stop = None
        if tune["phase"] == "find":
            if status in ("ok", "repeated"):  # the first note is the trigger, even if it sounded twice
                tune["finds"].append(self.tune_note(step))
                trigger = tune_rules.goal_at(tune["play"]["frames"], tune["finds"][-1])
                tune.setdefault("find_goals", []).append(trigger)
                agree = len(tune["finds"]) >= tune_rules.FINDS and \
                    tune_rules.gap(tune["find_goals"][-1], tune["find_goals"][-2]) <= tune_rules.AGREE_DEG
                if agree:
                    tune.update(found_play=tune["play"], found_points=deepcopy(tune["points"]), phase="verify", passes=0)
                    problem = self.tune_set(tune, sum(tune["finds"][-2:]) / 2)
                    if problem:
                        stop = f"{tune['name']}: {problem}; re-teach it with the leader"
                    else:
                        taught_press = tune["taught"]["press"]["goal"]
                        text += (f"; trigger found. Touch set {tune['margin']:g}° before it, press {tune['depth']:g}° past it "
                                 f"(was {tune_rules.gap(tune['trigger'], taught_press):.1f}° past)")
                elif len(tune["finds"]) >= tune_rules.MAX_FINDS:
                    stop = f"{tune['name']} triggers at a different point each time; re-teach it with the leader"
            elif status == "missed":
                points, change = tune_rules.deeper(tune["points"], tune["taught"]["press"])
                if points is None:
                    stop = f"{tune['name']}: {change}; re-teach it with the leader"
                else:
                    tune.update(points=points, finds=[], find_goals=[])
                    text += f"; {change}"
            else:
                stop = f"{text}. Calibration cannot correct this; re-teach it with the leader"
            if not stop and tune["trial"] >= tune_rules.MAX_FINDS + int(tune_rules.LIMIT_DEG / tune_rules.STEP_DEG) and tune["phase"] == "find":
                stop = f"{tune['name']} did not give a steady trigger point; re-teach it with the leader"
        else:
            tune["verify_trials"] += 1
            if status == "ok":
                tune["passes"] += 1
                if tune["passes"] >= tune_rules.VERIFY_PASSES:
                    tune["log"].append({"trial": tune["trial"], "phase": "verify", "outcome": "pass", "text": text, "velocity": step.get("velocity")})
                    return self.tune_finish()
            else:
                tune["passes"] = 0
                note_t = sum(tune["finds"][-2:]) / 2
                if status == "repeated" and tune["margin"] + tune_rules.MARGIN_STEP <= tune_rules.MAX_MARGIN + 1e-9:
                    tune["margin"] += tune_rules.MARGIN_STEP
                    problem = self.tune_set(tune, note_t)
                    stop = problem and f"{tune['name']}: {problem}"
                    text += f"; touch now {tune['margin']:g}° before the trigger"
                elif status == "missed" and tune["depth"] + tune_rules.DEPTH_STEP <= tune_rules.MAX_DEPTH + 1e-9:
                    tune["depth"] += tune_rules.DEPTH_STEP
                    problem = self.tune_set(tune, note_t)
                    stop = problem and f"{tune['name']}: {problem}"
                    text += f"; press now {tune['depth']:g}° past the trigger"
                else:
                    stop = f"{text} at your playing speed; re-teach it with the leader"
            if not stop and tune["verify_trials"] >= tune_rules.MAX_VERIFY:
                stop = f"{tune['name']} did not press cleanly twice in a row at your playing speed"
        outcome = "stop" if stop else "pass" if status == "ok" else "adjust"
        tune["log"].append({"trial": tune["trial"], "phase": "find" if tune["phase"] == "find" or "trigger found" in text else "verify",
                            "outcome": outcome, "text": stop or text, "velocity": step.get("velocity")})
        self.event("tune", f"{tune['name']} try {tune['trial']}: {stop or text}", {"control": tune["control"], "outcome": outcome})
        if stop:
            return self.tune_end("failed", f"{stop}. Nothing was saved.")
        tune["message"] = text
        tune["next_at"] = self.clock() + tune_rules.PAUSE_S

    def tune_finish(self):
        tune = self.tune
        entry = self.recording_entry(tune["control"])
        trigger, taught_press = tune["trigger"], tune["taught"]["press"]["goal"]
        self.save_control_entry(tune["control"], {
            **entry, "points": {**entry["points"], "touch": tune["points"]["touch"], "press": tune["points"]["press"]},
            "saved_at": m.stamp(), "tuned_at": m.stamp(),
            "taught_points": entry.get("taught_points") or {k: entry["points"][k] for k in ("touch", "press")},
            "calibration": {"trigger": {j: round(trigger[j], 3) for j in tune_rules.JOINTS}, "touch_margin": tune["margin"],
                            "press_depth": tune["depth"], "verified_speed": tune["speed"], "verified_hardness": tune["hardness"],
                            "log": tune["log"]}})
        self.teach_points_for = None
        self.tune_end("done", f"{tune['name']} calibrated: touch {tune['margin']:g}° before its trigger point, press "
                      f"{tune['depth']:g}° past it (taught {tune_rules.gap(trigger, taught_press):.1f}° past). "
                      f"Verified at {tune['speed']:g}×. The taught points are kept.")

    def tune_end(self, status, message):
        self.tune.update(status=status, message=message, waiting=False)
        self.tune_results[self.tune["control"]] = {"status": status, "message": message, "trials": self.tune["trial"], "at": m.stamp()}
        self.event("tune", message, {"control": self.tune["control"], "status": status})
        if status == "stopped":
            self.tune_queue = []  # Stop ends the whole list
        elif self.tune_queue:
            self.tune_next_at = self.clock() + tune_rules.PAUSE_S
            message += f" Next: {CATALOG[self.tune_queue[0]]['name']}."
        if self.phase == "teach_hold":
            self.transition("teach_hold", message)

    def tune_stop(self, message):
        self.tune_queue = []
        if self.tune and self.tune["status"] == "running":
            self.tune_end("stopped", message)
        elif self.phase == "teach_hold":
            self.transition("teach_hold", "Tune-up stopped between keys; holding here.")

    def tune_due(self):
        if self.tune_queue and self.tune["status"] != "running" and self.phase == "teach_hold" and self.clock() >= self.tune_next_at:
            control = self.tune_queue.pop(0)
            try:
                self.tune_begin(control)
            except m.SafetyError as exc:  # e.g. re-taught or invalidated since the list was checked
                self.tune_results[control] = {"status": "failed", "message": str(exc), "trials": 0, "at": m.stamp()}
                self.tune_next_at = self.clock()
            return
        tune = self.tune
        if tune and tune["status"] == "running" and not tune["waiting"] and self.phase == "teach_hold" \
                and self.clock() >= tune["next_at"]:
            try:
                self.tune_trial()
            except (m.SafetyError, ValueError) as exc:
                self.tune_end("failed", f"Tune-up stopped: {exc}")

    def fault(self, exc):
        message = str(exc) or type(exc).__name__
        if self.tuning:
            self.tune_stop(f"Stopped by a fault: {message}. Nothing was saved.")
        context = self.error_context()
        if self.controller:
            self.controller.stop()
        if self.teach:
            try:
                self.teach.hold()
            except Exception as hold_error:
                self.report_error("hold_failed", hold_error)
            self.teach = None
        if self.calibrating:
            self.mark_calibrated(False)
            try:
                self.calibration_arm.abort_calibration()
                self.calibrating = False
            except Exception as restore_error:
                self.report_error("cleanup_failed", restore_error, {"operation": "calibration_rollback"})
                message += f" Calibration restoration unverified: {restore_error}"
        self.phase = "fault" if self.arm is not None else "disconnected"
        self.revision += 1
        self.error = message
        self.message = ("Stopped. Support the arm before releasing torque. Inspect the cause before retrying."
                        if self.arm is not None else "Stopped before connecting. Choose a follower to start again.")
        self.torque = None
        self.feedback_at = None
        self.leader_feedback_at = None
        self.stop_event.clear()
        self.event("fault", message, context, exc=exc)
        self.publish()

    def dispatch(self, action, args):
        if self.tuning:
            # Supported release and disconnect stay available; they end the tune-up like Stop motion.
            m.require(action in ("tune_stop", "release", "disconnect", "forget_connection"), "A tune-up is running. Stop it first.")
            if action != "tune_stop":
                self.tune_stop("Stopped: torque released or disconnected. Nothing was saved.")
        # Each arm is only taught and plays its own controls, whatever the command (teach, follow, capture, play,
        # sequence, settings, key calibration, the hand-guided flow).
        named = [args.get("control")] + [step.get("control") for step in args.get("steps") or () if isinstance(step, dict)] + \
            list(args.get("controls") or ())
        for control in named:
            if isinstance(control, str) and control in CATALOG and control not in self.owns:
                raise m.SafetyError(f"{CATALOG[control]['name']} is played by the other arm.")
        if self.rig:
            self.rig.claim(self.arm_id, action, args)
        if action == "tune_start":
            return self.tune_start(args)
        if action == "tune_stop":
            m.require(self.tuning, "No tune-up is running.")
            if self.phase == "teach_play":
                self.teach.hold()
                self.transition("teach_hold", "Holding here.")
            return self.tune_stop("Tune-up stopped; holding here. Nothing was saved for the key being tuned.")
        if self.teaching_mode == "leader" and (action.startswith("capture_") or action.startswith("dial_capture_")):
            m.require(not self.recording_error, self.recording_error)
        if self.teaching_mode == "leader" and action in ("capture_hover", "capture_pressed", "capture_touch", "retreat_from_press",
                "dial_capture_start", "dial_capture_contact", "dial_capture_turn", "dial_capture_lift", "dial_capture_return"):
            m.require(isinstance(self.controller, LeaderController) and self.controller.enabled,
                      "Establish a follower hold for leader teaching first.")
            m.require(not self.controller.engaged, "Pause following before capturing a waypoint.")
        if action in ("capture_key_clearance", "capture_home_return", "capture_leader_home"):
            m.require(isinstance(self.controller, LeaderController) and self.controller.enabled and not self.controller.engaged,
                      "Establish a hold and pause following before capturing the route.")
        if action in ("refresh_ports", "refresh_leader_ports"):
            adding_leader = action == "refresh_leader_ports"
            if adding_leader:
                self.require_phase("connected", "ready")
                m.require(self.arm is not None and self.leader is None, "Connect only the follower before finding a leader.")
                self.arm.require_torque(False)
            else:
                self.require_phase("disconnected")
                m.require(self.arm is None, "Disconnect the follower before scanning USB ports.")
            if self.mode == "simulation":
                return  # Never load the serial SDK or touch USB in practice mode.
            self.discovery = {"ports": [], "scanned_at": None, "scanning": True, "warnings": [], "error": None}
            self.publish()
            try:
                busy = ((self.follower_port,) if adding_leader else ()) + (self.rig.ports_in_use(self.arm_id) if self.rig else ())
                result = self.port_scanner(guard=self.guard, **({"exclude_ports": busy} if busy else {}))
                self.guard()
                self.discovery["ports"] = [self.port_entry(arm) for arm in result["arms"]]
                if self.rig:
                    self.rig.note_scan(self.discovery["ports"], by=self.arm_id)
                self.discovery["warnings"] = result["warnings"]
                self.discovery["scanned_at"] = time.time()
                self.error = None
            except Exception as exc:
                message = ("Install requirements-hardware.txt in the Python environment running this app."
                           if isinstance(exc, ImportError) else str(exc))
                self.discovery["error"] = message
                raise m.SafetyError(message) from exc
            finally:
                self.discovery["scanning"] = False
        elif action == "connect":
            self.require_phase("disconnected")
            m.require(args.get("prepared") is True, "Confirm the pad, mounting, clear workspace, and accessible power stop.")
            teaching_mode = args.get("teaching_mode", "manual")
            m.require(teaching_mode in ("manual", "leader"), "Choose manual or leader teaching.")
            if teaching_mode == "leader" and self.mode == "hardware":
                leader_port = args.get("leader_port")
                m.require(leader_port != args.get("port") and any(p["path"] == leader_port and p.get("leader_connectable") for p in self.discovery["ports"]),
                          "Select a separate detected leader with motor IDs 1–6 and low-voltage power.")
            label = str(args.get("fixture", "")).strip()
            m.require(1 <= len(label) <= 120, "Give this fixture a short placement name.")
            tool = args.get("tool", self.fixture.get("tool", "padded_gripper"))
            m.require(tool in ("padded_gripper", "rubber_gloved_tips"), "Choose the fitted gripper contact surface.")
            if not (args.get("fixture_unchanged") is True and label == self.fixture["label"]
                    and tool == self.fixture.get("tool", "padded_gripper")):
                self.fixture = {"label": label, "id": uuid.uuid4().hex, "tool": tool}
                self.repo.put("fixture", self.fixture)
            if self.mode == "hardware":
                port = args.get("port")
                m.require(any(p["path"] == port and p["connectable"] for p in self.discovery["ports"]),
                          "Refresh connections and choose a detected follower with motor IDs 1–6 and a verified voltage.")
                candidate = self.hardware_factory(port, self.calibration)
            else:
                candidate = SimulatedArm(self.calibration)
            try:
                self.open_arm(candidate, "follower", port if self.mode == "hardware" else "simulator")
                self.arm = candidate
                self.follower_port = port if self.mode == "hardware" else str(args.get("port") or "simulator")
                if teaching_mode == "leader":
                    if self.leader_store:  # the other follower may have calibrated the shared leader since
                        self.leader_calibration = self.leader_store.get()
                    self.leader = (self.leader_factory(leader_port, self.leader_calibration) if self.mode == "hardware"
                                   else SimulatedArm(self.leader_calibration))
                    if self.mode == "simulation":
                        self.leader.voltage = 5.2
                    self.leader_port = leader_port if self.mode == "hardware" else "simulator-leader"
                    self.open_arm(self.leader, "leader", self.leader_port)
                    self.leader_calibrated = bool(self.leader_calibration) and getattr(self.leader, "calibration_matches", True)
                self.teaching_mode = teaching_mode
                self.calibration_target = "follower"
                self.diagnostics = self.diagnostics_error = None
                self.calibrated = bool(self.calibration) and getattr(candidate, "calibration_matches", True)
                self.calibration_foreign = bool(self.calibration) and not self.calibrated
                self.transition("connected", "Follower connected. Check motor state, then calibrate or register notes.")
                self.sample()
            except Exception:
                if self.leader:
                    self.leader.close()
                    self.leader = None
                    self.leader_calibrated = False
                candidate.close()
                self.arm = None
                self.follower_port = None
                self.calibrated = False
                self.phase = "disconnected"
                raise
        elif action == "leader_detach":
            # Hands the one leader to the other follower: close it here, then Connect leader there. A follower holding
            # in a teaching session keeps holding, just without the leader (it can still play and go home).
            self.require_phase("connected", "ready", "teach_hold")
            m.require(self.leader is not None, "No leader is connected to this follower.")
            self.leader.require_torque(False)
            if self.teach:
                self.teach.read_leader = None
            if self.rig:
                self.rig.released_leader = self.leader_port
            self.leader.close()
            self.leader, self.leader_calibrated = None, False
            self.leader_current = self.leader_torque = self.leader_feedback_at = None
            self.teaching_mode = "manual"
            self.transition(self.phase, "Leader disconnected from this follower. Connect it to the other follower to teach it.")
        elif action == "connect_leader":
            # Also while holding in a teaching session (the leader follows the arm you select): opening the leader
            # only reads it, so the powered follower is left exactly as it is.
            self.require_phase("connected", "ready", "teach_hold")
            holding = self.phase == "teach_hold"
            m.require(self.arm is not None and self.leader is None, "A follower must be connected and the leader must be disconnected.")
            m.require(args.get("prepared") is True, "Confirm the leader is secure and clear of the instrument.")
            port = args.get("leader_port")
            handed_over = bool(self.rig) and port is not None and port == self.rig.released_leader  # just released by the other arm
            m.require(isinstance(port, str) and (handed_over or any(p["path"] == port and p.get("leader_connectable") for p in self.discovery["ports"])),
                      "Refresh leader connections and choose a detected low-voltage leader with motor IDs 1–6.")
            m.require(Path(port).resolve() != Path(self.follower_port).resolve(), "The leader must use a separate port from the connected follower.")
            self.guard()
            if not holding:
                self.arm.require_torque(False)
            candidate = None
            try:
                if self.leader_store:
                    self.leader_calibration = self.leader_store.get()
                candidate = self.leader_factory(port, self.leader_calibration) if self.mode == "hardware" else SimulatedArm(self.leader_calibration)
                if self.mode == "simulation":
                    candidate.voltage = 5.2
                self.open_arm(candidate, "leader", port)
                candidate.require_torque(False)
                candidate.read_raw()
                self.guard()
            except Exception as exc:
                if candidate is not None:
                    candidate.close()
                raise m.SafetyError(f"Leader connection failed; follower calibration is unchanged: {exc}") from exc
            self.leader, self.leader_port = candidate, port
            self.leader_calibrated = bool(self.leader_calibration) and getattr(candidate, "calibration_matches", True)
            self.teaching_mode = "leader"
            if holding:  # the session keeps holding; following can start once the leader is calibrated
                self.teach.read_leader = self.read_leader_joints if self.leader_calibrated else None
                self.transition("teach_hold", "Leader connected; still holding here. Follow the leader to teach." if self.leader_calibrated
                                else "Leader connected; still holding here. Calibrate the leader before teaching.")
            else:
                self.calibration_target = "leader"
                self.transition("connected", "Leader connected. Follower calibration retained. " + (
                    "Its saved calibration is valid: teach with it." if self.leader_calibrated else "Calibrate the leader before teaching."))
                self.sample()
        elif action == "refresh_diagnostics":
            self.require_phase("connected", "ready")
            self.guard()
            try:
                values = self.arm.read_diagnostics()
                self.diagnostics = {"sampled_at": time.time(), "motors": values}
                self.diagnostics_error = None
            except Exception as exc:
                self.diagnostics = None
                self.diagnostics_error = str(exc)
                raise m.SafetyError(f"Motor health read unavailable: {exc}") from exc
        elif action in ("calibrate", "calibration_reset", "calibration_reload"):
            self.require_phase(*(("connected", "ready") if action == "calibrate" else CALIBRATION_SETUP_PHASES))
            self.supported(args)
            target = args.get("target", self.calibration_target if action != "calibrate" else "follower")
            m.require(target in ("leader", "follower") and (target != "leader" or self.leader is not None), "Connect the requested arm first.")
            m.require(not self.calibrating or target == self.calibration_target, "Finish or release the current arm's calibration first.")
            self.calibration_target = target
            if target == "follower" and action in ("calibrate", "calibration_reset") and self.calibration_foreign:
                name = f"arm {self.arm_id.upper()}" if self.rig else "this arm"
                m.require(args.get("replace_calibration") is True,
                          f"This follower's motors do not carry {name}'s saved calibration, so it may be a different arm. "
                          f"Connect it as the other arm instead, or confirm replacing {name}'s calibration: "
                          f"{self.calibration_dependents()} taught controls recorded with it would need re-teaching.")
            reload_saved = action == "calibration_reload"
            if reload_saved:
                # Validate before releasing torque or discarding an active sweep.
                validate_calibration(self.saved_calibration)
                m.require(args.get("calibration_unchanged") is True,
                          "Confirm this is the same arm and no motors or joints have been replaced or reseated since saving.")
            self.guard()
            self.actuating = True
            self.arm.release()
            if self.leader:
                self.leader.release()
            self.mark_calibrated(False)
            if self.calibrating:
                self.calibration_arm.abort_calibration()
                self.calibrating = False
            self.reset_note()
            self.clear_calibration_capture()
            self.guard()
            self.begin_calibration()
            if reload_saved:
                self.calibration_arm.commit_calibration(self.saved_calibration, guard=self.guard)
                self.mark_calibrated(True)
                self.calibrating = False
                self.event("calibration_reloaded", "Saved calibration reloaded and verified on all six motors; torque remains off.")
                self.transition("ready", "Saved calibration is active. Registered motions retain their original fixture and calibration checks.")
            else:
                if action == "calibration_reset":
                    self.event("calibration_reset", "Calibration restarted at midpoint. Saved calibration and registrations retained.")
                self.transition("calibration_midpoint", "Support the arm. Center all six joints and half-open the gripper, then capture the midpoint.")
        elif action == "calibration_center":
            self.require_phase("calibration_midpoint")
            self.supported(args)
            self.stable(enforce_limits=False)
            self.guard()
            self.actuating = True
            self.offsets = self.calibration_arm.center(guard=self.guard, sleep=self.sleep)
            self.ranges, self.range_index = {}, 0
            self.transition("calibration_range", "Slowly move the base rotation through its usable travel in both directions. Do not force the stops.")
            self.sample()
        elif action == "simulate_sweep":
            self.require_phase("calibration_range")
            m.require(self.mode == "simulation", "Simulation actions cannot operate hardware.")
            motor = RANGE_MOTORS[self.range_index]
            for value in (2047, 1800, 1400, 1000, 1400, 2047, 2600, 3100, 2047):
                self.guard()
                self.calibration_arm.current[motor] = value
                self.sample()
                self.sleep(0.12)
        elif action == "calibration_next":
            self.require_phase("calibration_range")
            m.require(args.get("range_complete") is True, "Confirm both ends of this joint's usable travel were recorded.")
            motor = RANGE_MOTORS[self.range_index]
            span = self.ranges[motor]
            m.require(span["max"] - span["min"] > 2 * m.JOINT_MARGIN,
                      "Not enough travel recorded yet. Move this joint in both directions.")
            self.range_index += 1
            if self.range_index == len(RANGE_MOTORS):
                self.transition("calibration_review", "Review the five measured ranges. Wrist rotation uses the full encoder range; no cable-twisting sweep is required.")
            else:
                self.transition("calibration_range", "Record both directions of the highlighted joint, then confirm its range.")
                self.sample()
        elif action == "calibration_save":
            self.require_phase("calibration_review")
            m.require(args.get("range_complete") is True, "Confirm these measured ranges cover the intended usable travel.")
            new = {}
            for i, name in enumerate(m.MOTORS, 1):
                span = {"min": 0, "max": 4095} if name == "wrist_roll" else self.ranges[name]
                m.require(0 <= span["min"] < span["max"] <= 4095 and span["max"] - span["min"] > 2 * m.JOINT_MARGIN,
                          f"Invalid measured range for {name}")
                new[name] = {"id": i, "drive_mode": 0, "homing_offset": self.offsets[name],
                             "range_min": span["min"], "range_max": span["max"]}
            self.guard()
            self.actuating = True
            self.calibration_arm.commit_calibration(new, guard=self.guard)
            if self.calibration_target == "leader" and self.leader_store:
                self.leader_store.put(new)
            else:
                self.repo.put("leader_calibration" if self.calibration_target == "leader" else "calibration", new)
            if self.calibration_target == "leader":
                self.leader_calibration = new
            else:
                self.calibration = new
            self.mark_calibrated(True)
            self.calibrating = False
            self.event("calibration_saved", "All six motors calibrated; torque remains off.", new)
            remaining = "follower" if not self.calibrated else "leader" if self.teaching_mode == "leader" and not self.leader_calibrated else None
            self.transition("ready", f"Calibration verified. Next, calibrate the {remaining}." if remaining else
                            "Calibration verified. Both required references are ready; choose a control to teach.")
        elif action == "home_start":
            self.require_phase("connected", "ready")
            self.supported(args)
            m.require(self.teaching_mode == "leader" and self.calibrated and self.leader_calibrated,
                      "Connect and calibrate both arms before positioning home with the leader.")
            m.require(self.controller is None, "Release the existing hold before positioning a new home.")
            self.arm.require_torque(False)
            self.leader.require_torque(False)
            selected = args.get("control", self.selected)
            m.require(isinstance(selected, str) and selected in CATALOG, "Choose an instrument control.")
            current = self.stable(enforce_limits=False)
            # The initial hold starts at the current pose, independent of any
            # old home. Live following uses native driver position commands.
            motion_arm = home.HomeArm(self.arm, current)
            self.reset_note()
            self.selected = selected
            self.transition("home_positioning", "Establishing a hold at the current follower position. Keep supporting it.")
            self.start_leader_hold(current, motion_arm, follow_gripper=True)
            self.transition("home_positioning", "Follower holding here. Clear hands from the follower, engage the leader, and guide to your new home.")
        elif action == "capture_leader_home":
            self.require_phase("home_positioning", "home_arrival")
            m.require(args.get("path_clear") is True, "Confirm the new home and its approach are clear of Orchid.")
            current = self.stable()
            self.arm.check_calibrated_pose(current)
            self.save_home_reference(current)
            # Keep the established hold: capture never releases or re-enables
            # torque, and the next approach begins at this measured new home.
            self.controller.arm = home.HomeArm(self.arm, current)
            self.controller.set_travel("approach", current)
            self.route_approach = [dict(current)]
            self.transition("home_approach", f"New home saved. Follower remains holding here. Engage the leader to teach the approach to {self.control['name']}.")
        elif action == "capture_home":
            self.require_phase("connected", "ready")
            self.supported(args)
            m.require(self.teaching_mode == "leader" and self.calibrated and self.leader_calibrated,
                      "Connect and calibrate both arms before teaching home.")
            self.arm.require_torque(False)
            self.leader.require_torque(False)
            current = self.stable(enforce_limits=False)
            self.arm.check_calibrated_pose(current)
            self.save_home_reference(current)
            self.transition("ready", "Home saved with torque off. Every new leader-taught control will start and return here.")
        elif action in ("note_start", "control_start"):
            self.require_phase("connected", "ready", "saved")
            self.supported(args)
            m.require(self.calibrated, "Complete motor calibration first.")
            m.require(self.teaching_mode != "leader" or self.leader_calibrated, "Calibrate the leader before teaching.")
            if self.teaching_mode == "leader":
                m.require(self.home_ready, "Teach the shared home pose first.")
                m.require(not isinstance(self.controller, LeaderController) or not self.controller.engaged,
                          "Pause following before selecting another control.")
                self.leader.require_torque(False)
                # A connected arm can already be powered by another session.
                # Never silently release it or assume ownership of its hold.
                self.arm.require_torque(bool(self.controller and self.controller.enabled))
                current = self.arm.read_raw()
                self.arm.check_calibrated_pose(current)
                at_home = home.at_home(current, self.home["pose"])
            selected = args.get("control", args.get("key", self.selected))
            m.require(isinstance(selected, str) and selected in CATALOG, "Choose an instrument control.")
            if action == "note_start":
                m.require(selected in m.KEYS, "Choose one of the twelve notes.")
            if self.teaching_mode == "leader":
                motion_arm = home.HomeArm(self.arm, self.home["pose"] if at_home else current)
                motion_arm.check_pose(current)
                self.reset_note(keep_controller=True)
                self.selected = selected
                if at_home:
                    self.route_approach = [dict(current)]
                phase = "home_approach" if at_home else "home_prepare"
                self.transition(phase, "Establishing the follower hold here. Keep supporting it until the hold is verified.")
                self.start_leader_hold(current, motion_arm, follow_gripper=not at_home)
                self.transition(phase, "Follower holding at home. Clear your hands, then engage leader following to teach the approach." if at_home
                                else "Follower holding here. Clear hands and confirm the entire move to home is clear, then choose Move home & teach.")
            else:
                self.actuating = True
                self.arm.release()
                if self.leader:
                    self.leader.release()
                if self.controller:
                    self.controller.enabled = False
                self.reset_note()
                self.selected = selected
                self.transition("dial_ready" if self.is_dial else "note_ready", self.ready_message())
        elif action == "move_home":
            self.require_phase("home_prepare")
            m.require(self.home_ready and isinstance(self.controller, LeaderController) and self.controller.enabled,
                      "Establish the follower hold and verify its saved home first.")
            m.require(args.get("hands_clear") is True and args.get("path_clear") is True,
                      "Confirm hands and the entire swept path to home, including the gripper, are clear.")
            duration = args.get("duration", 0)
            m.require(type(duration) in (int, float) and (duration == 0 or 1 <= duration <= 60),
                      "Choose Direct (0) or a home move time from 1 to 60 seconds.")
            target = self.home.get("hold_target", self.home["pose"])
            self.arm.check_calibrated_pose(target)
            self.actuating = True
            self.home_move_progress = 0
            self.transition("home_moving", f"Moving to the saved home before teaching {self.control['name']}. Keep hands clear.")
            def progress(value):
                self.home_move_progress = value
                self.publish()
            self.controller.move_home(target, duration, progress)
            # Command completion is not proof of physical arrival. Allow native
            # leader positioning immediately, while keeping route capture tied
            # to a measured home (or an explicitly accepted replacement).
            self.controller.follow_gripper = False
            self.home_arrival_samples = 0
            self.transition("home_arrival", "Home command sent. Leader positioning is available now. If the held pose differs from saved home, confirm it as your new home before recording the approach.")
        elif action == "leader_hold":
            self.require_phase("home_approach")
            self.supported(args)
            m.require(self.teaching_mode == "leader" and self.leader and self.leader_calibrated and self.calibrated,
                      "Connect and calibrate both arms first.")
            m.require(self.controller is None, "A hold is already active.")
            self.arm.require_torque(False)
            self.leader.require_torque(False)
            m.require(self.home_ready, "Teach a home for this calibration and placement first.")
            current = self.stable(enforce_limits=False)
            motion_arm = home.HomeArm(self.arm, self.home["pose"])
            motion_arm.check_pose(current)
            m.require(home.at_home(current, self.home["pose"]), "Return to the taught home before establishing the hold.")
            self.route_approach = [dict(current)]
            self.start_leader_hold(current, motion_arm)
            self.transition(self.phase, "Follower holding its current pose. Clear hands from the follower, then engage leader following.")
        elif action == "capture_key_clearance":
            self.require_phase("home_approach")
            m.require(args.get("path_clear") is True, "Confirm the approach and clearance are clear of the instrument.")
            current = self.stable()
            self.controller.arm.check_clearance(current)
            home.append(self.route_approach, current, self.current, force=True)
            self.controller.set_local(current)
            if self.is_dial:
                self.transition("dial_ready", "Approach captured. Teach the local control motion here; pause before every capture.")
            else:
                self.begin_press_capture(current)
        elif action == "capture_home_return":
            self.require_phase("home_return")
            m.require(args.get("path_clear") is True, "Confirm the entire return route was clear of the instrument.")
            current = self.stable()
            m.require(home.at_home(current, self.home["pose"]), "Return to home within 6 ticks, with the same gripper opening.")
            home.append(self.route_return, current, self.current, force=True)
            draft = home.attach(self.draft, self.home, self.route_approach, self.route_return)
            home.validate(draft, self.home, self.controller.arm)
            self.establish_hold(draft, home.plan(draft))
        elif action in ("leader_resume", "leader_pause"):
            self.require_phase(*TEACH_PHASES)
            m.require(isinstance(self.controller, LeaderController) and self.controller.enabled, "Establish the supported follower hold first.")
            if action == "leader_resume":
                m.require(args.get("hands_clear") is True, "Confirm hands are clear of the follower and its path.")
                m.require(not self.controller.engaged, "Following is already engaged.")
            self.actuating = True
            if action == "leader_resume":
                self.controller.resume()
            else:
                self.controller.pause()
            grip = "gripper follows the leader" if self.controller.follow_gripper else "gripper fixed at home opening"
            self.transition(self.phase, f"Following leader joint positions with LeRobot SO101 defaults; {grip}. Pause before capture." if self.controller.engaged
                            else "Following paused. Follower holds here; reposition the leader or capture the waypoint.")
        elif action == "simulate_leader":
            self.require_phase(*TEACH_PHASES, "teach_follow", "teach_record")
            m.require(self.mode == "simulation" and (isinstance(self.controller, LeaderController) or self.teach),
                      "Simulation-only leader input.")
            m.require(self.simulated_leader_input is None, "The simulated hand movement is still in progress.")
            name, delta = args.get("motor"), args.get("delta")
            m.require(name in m.MOTORS and type(delta) is int and 0 < abs(delta) <= 96, "Choose a small simulated leader input.")
            value = self.leader.current[name] + delta
            m.require(0 <= value <= 4095, "Simulated leader is outside its encoder range.")
            # Human input unfolds over multiple worker ticks; no blocking motion command.
            self.simulated_leader_input = (name, delta)
        elif action == "dial_capture_start":
            self.require_phase("dial_ready")
            settings = {field: str(args.get(field, "")).strip() for field in ("reference", "expected_effect")}
            m.require(all(1 <= len(value) <= 160 for value in settings.values()),
                      "Describe the reference chord/view and expected voicing change.")
            m.require(args.get("fixed_pad") is True, "Confirm a fixed padded rim contact; no gripping or squeezing the dial.")
            self.simulate_pose("dial_start")
            current = self.stable()
            self.capture = {**m.teaching_entry(self.arm, True, self.fixture["label"], current), **settings,
                            "direction": self.control["direction"], "contact_method": "fixed_pad",
                            "contact_surface": self.fixture.get("tool", "padded_gripper")}
            self.transition("dial_approach", "Start clearance captured. Gently bring the fixed pad to the dial rim without rotating it.")
        elif action in ("dial_capture_contact", "dial_capture_turn", "dial_capture_lift"):
            expected_phase, marker, pose, following, message = {
                "dial_capture_contact": ("dial_approach", "contact_index", "dial_contact", "dial_contact",
                                         "Contact captured. Make a small turn in the selected direction, then hold steady."),
                "dial_capture_turn": ("dial_contact", "turn_index", "dial_turn", "dial_turned",
                                      "Turn captured. Lift the pad completely away from the rim without turning back."),
                "dial_capture_lift": ("dial_turned", "release_index", "dial_lift", "dial_lifted",
                                      "Lift-off captured. Stay clear of the dial and return near the initial clearance; watch the return error."),
            }[action]
            self.require_phase(expected_phase)
            if action == "dial_capture_turn":
                m.require(args.get("direction_verified") is True, "Confirm the selected direction and intended voicing change.")
            if action == "dial_capture_lift":
                m.require(args.get("rim_clear") is True, "Confirm the pad is completely clear of the dial rim.")
            self.simulate_pose(pose)
            current = self.stable()
            m.append_capture(self.capture, current, force=True)
            prior = 0 if marker == "contact_index" else self.capture["contact_index" if marker == "turn_index" else "turn_index"]
            m.require(m.distance(self.capture["path"][prior], current) > 0, "Move to the next dial stage before capturing.")
            candidate = {**self.capture, marker: len(self.capture["path"]) - 1}
            dial.validate(candidate)
            self.capture = candidate
            self.transition(following, message)
        elif action == "dial_capture_return":
            self.require_phase("dial_lifted")
            if self.teaching_mode == "leader":
                m.require(args.get("hands_clear") is True, "Keep hands clear of the powered follower.")
            else:
                self.supported(args)
            m.require(args.get("rim_clear") is True, "Confirm the entire return path stayed clear of the dial.")
            self.simulate_pose("dial_start")
            current = self.stable()
            m.append_capture(self.capture, current, force=True)
            draft = {**self.capture, "complete": True, "mode": self.mode, "fixture_id": self.fixture["id"],
                     "control_id": self.selected, "teaching_mode": "web_dial_forward_loop"}
            if self.teaching_mode == "leader":
                draft["teaching_mode"] = "web_leader_dial_forward_loop"
            if self.teaching_mode == "leader":
                self.begin_home_return(draft)
            else:
                self.establish_hold(draft, dial.plan(draft))
        elif action == "capture_hover":
            self.require_phase("note_ready")
            self.simulate_pose("clear")
            current = self.stable()
            self.begin_press_capture(current)
        elif action == "capture_touch":
            self.require_phase("note_hover")
            self.simulate_pose("touch")
            current = self.stable()
            m.require(m.distance(self.capture["path"][0], current) > 0, "Move from hover to first contact before capturing.")
            m.append_capture(self.capture, current, force=True)
            self.touch_index = len(self.capture["path"]) - 1
            self.capture["touch_index"] = self.touch_index
            self.transition("note_touch", "First contact captured. Press only until the control activates, then capture the press.")
        elif action == "capture_pressed":
            self.require_phase("note_touch")
            if self.teaching_mode != "leader":
                self.supported(args)
            self.simulate_pose("pressed")
            current = self.stable()
            m.append_capture(self.capture, current, force=True)
            draft = {**self.capture, "path": deepcopy(self.capture["path"]),
                     "complete": True, "mode": self.mode, "fixture_id": self.fixture["id"],
                     "control_id": self.selected, "teaching_mode": "web_leader_forward_path" if self.teaching_mode == "leader" else "web_forward_path"}
            m.validate_entry(draft, complete=True)
            planned = m.plan(draft)
            m.require(m.routine_budget(planned, .2) <= m.MAX_RUN_TIME, "This path is too long; retry with a smaller local stroke.")
            self.draft, self.capture = draft, None
            message = "Press captured; holding here. Clear hands and choose Retreat to hover. Only the reverse path will run; the press will not repeat."
            if self.teaching_mode == "leader":
                self.transition("note_pressed", message)
            else:
                self.establish_hold(draft, list(reversed(planned)), phase="note_pressed", message=message)
        elif action == "retreat_from_press":
            self.require_phase("note_pressed")
            m.require(args.get("hands_clear") is True, "Confirm hands are clear before retreating to hover.")
            m.require(self.controller is not None and self.controller.enabled, "Establish the press hold first.")
            self.actuating = True
            draft = self.draft
            planned = m.plan(draft)
            previous = self.controller
            if isinstance(previous, LeaderController):
                self.establish_hold(draft, list(reversed(planned)), phase="retreating",
                                    message="Retreating from the captured press through contact to hover. No repeat press.")
            else:
                self.controller.path = list(reversed(planned))
                self.transition("retreating", "Retreating from the captured press through contact to hover. No repeat press.")
            self.controller.retreat()
            if isinstance(previous, LeaderController):
                # Hand back the powered hover hold without releasing/rearming.
                previous.previous = dict(self.controller.previous)
                previous.desired = dict(previous.previous)
                previous.log = self.log
                previous.enabled = True
                self.controller.enabled = False
                self.controller = previous
                self.begin_home_return(draft)
            else:
                self.controller.path = planned
                self.transition("holding", "Retreated to hover; holding here. The press was not repeated. A full test runs only when you choose Test one press.")
        elif action == "test":
            self.require_phase("holding", "saved")
            m.require(args.get("hands_clear") is True, "Confirm hands are clear of the motion path.")
            if self.is_dial:
                m.require(args.get("reference_reset") is True, "Restore the reference chord and voicing before each dial trial.")
            self.transition("testing", f"Testing {self.control['name']}: " +
                            ("home → approach → control motion → return home." if self.draft.get("home_motion") else
                             "one forward nudge, lift-off, and clear return." if self.is_dial else "one press and release, then a hold at hover."))
            try:
                if self.draft.get("home_motion"):
                    home.validate(self.draft, self.home, self.controller.arm)
                    home.run(self.controller, self.draft)
                elif self.is_dial:
                    dial.run(self.controller, self.draft)
                else:
                    self.controller.run(.2, already_holding=True)
                self.controller.started = None
                self.transition("result", "Review the observed instrument response and clean release before accepting this trial.")
            except Exception as exc:
                self.fault(exc)
                raise
        elif action == "pass":
            self.require_phase("result")
            if self.is_dial:
                m.require(args.get("effect_verified") is True, "Verify the dial direction, expected effect, and clear return without slip.")
            self.trials += 1
            entry = {**self.draft, "saved_at": m.stamp(), "verification": {
                "successful_trials": self.trials,
                "method": "simulated operator trial" if self.mode == "simulation" else "operator observed powered trial"}}
            self.save_entry(entry)
            self.transition("saved", f"{self.selected}: {self.trials}/{REQUIRED_TRIALS} accepted trials. " +
                            ("Support and move to the next control when ready." if self.trials >= REQUIRED_TRIALS else "Test again from this held position."))
        elif action == "fail":
            self.require_phase("result")
            # A later failure invalidates previous passes from this attempt.
            self.trials = 0
            self.save_entry({**self.draft, "saved_at": m.stamp(), "verification": {"successful_trials": 0}})
            self.transition("failed", "Trial rejected. Support the arm, release, and re-teach this control.")
        elif action == "next":
            self.require_phase("saved")
            m.require(self.trials >= REQUIRED_TRIALS, "Accept three trials before moving to the next control.")
            self.supported(args)
            statuses = {**self.key_statuses(), **self.statuses(self.controls)}
            missing = [name for name in group_members(self.selected) if statuses[name]["status"] != "registered"]
            if self.teaching_mode == "leader":
                if missing:
                    self.dispatch("control_start", {**args, "control": missing[0]})
                else:
                    self.transition("ready", "This control group is registered. The follower is still holding at home; select another control or use Release torque while supported.")
                return
            self.actuating = True
            self.arm.release()
            self.controller.enabled = False
            self.reset_note()
            if missing:
                self.selected = missing[0]
                self.transition("home_approach" if self.teaching_mode == "leader" else "dial_ready" if self.is_dial else "note_ready", self.ready_message())
            else:
                self.transition("ready", "This control group is registered. Rest the arm safely or select another group on the instrument.")
        elif action == "forget_connection":
            self.require_phase("fault")
            m.require(self.arm is not None, "No connection needs recovery.")
            self.supported(args)
            m.require(args.get("motor_power_disconnected") is True,
                      "Disconnect motor power from BOTH arms before forgetting the connection. USB alone is not enough.")
            # The operator has physically removed motor power. A dead serial
            # connection cannot verify torque or roll back calibration. Close
            # handles only; never call stop(), release(), or abort_calibration().
            if self.controller:
                self.controller.enabled = False
            self.calibrating = self.calibrated = self.leader_calibrated = False
            failures = []
            for role, device in (("follower", self.arm), ("leader", self.leader)):
                if device:
                    try:
                        device.close()
                    except Exception as exc:
                        self.report_error("cleanup_failed", exc, {"operation": f"forget_{role}"})
                        failures.append(role)
            m.require(not failures, "Could not close the unavailable connection: " + ", ".join(failures)
                      + ". Keep motor power disconnected and restart the Python app.")
            self.reset_note()
            self.clear_calibration_capture()
            self.arm = self.leader = None
            self.follower_port = None
            self.torque = self.leader_torque = None
            self.diagnostics = self.diagnostics_error = None
            self.leader_visible_until = 0.0
            self.recent.clear()
            self.stop_event.clear()
            if self.mode == "hardware":
                self.discovery = {"ports": [], "scanned_at": None, "scanning": False, "warnings": [], "error": None}
            self.transition("disconnected", "Unavailable connection forgotten; no torque command was sent. "
                            "Saved records are retained. Refresh connections after reconnecting. "
                            "For a replacement arm or interrupted calibration, perform a full calibration and teach a new home.")
        elif action == "teach_begin":
            self.require_phase("connected", "ready", "saved", "failed", "fault")
            # Teaching follows the leader; playing a taught control drives only the follower.
            leader = self.teaching_mode == "leader" and self.leader is not None and self.leader_calibrated
            m.require(self.calibrated, "Calibrate the follower before playing or teaching.")
            m.require(leader or args.get("follow") is not True,
                      "Connect and calibrate the leader to teach by recording. Taught controls play with only the follower.")
            name = self.teach_control(args)
            pose = args.get("pose")
            m.require(pose in (None, "home", "rest"), "Choose home or rest.")
            if not leader:  # refuse before powering if there is nothing complete to play or go to
                self.teach_points_for = None
                if pose:
                    m.require((self.shared_teach_home if pose == "home" else self.shared_teach_rest)(),
                              f"No {pose} is set yet. Connect the leader to set it.")
                else:
                    self.control_motion(self.selected, {})
            powered = any(self.arm.torque_status().values())
            # LeRobot's configure() briefly turns torque off while it writes the gains.
            m.require(not powered or args.get("supported") is True,
                      "The follower is already powered. Support it with a hand: torque blinks off for a moment while the motor settings are applied.")
            if leader:
                self.leader_torque = self.leader.require_torque(False)
            self.reset_note()
            self.actuating = True
            self.arm.arm_for_teleop()
            self.teach = teach_motion.Session(self.read_follower_joints, self.arm.teleop_goal,
                                              self.read_leader_joints if leader else None, clock=self.clock)
            self.teach.send_follower(self.teach.goal)
            self.teach_points_for, self.teach_returning = None, False
            points = self.points_for(self.selected)
            home_note = ""
            if "home" not in points and leader:
                points["home"] = self.teach.capture()  # home = where the arm is when you first press Teach
                self.save_teach_home(points["home"])
                home_note = " Home saved here."
            if args.get("follow") is True:
                self.teach.follow()
                self.transition("teach_follow", f"Matching the leader at up to {teach_motion.RAMP_SPEED:.0f}°/s.{home_note} "
                                "Hands off the follower; hold the leader near its pose.")
            elif leader:
                self.transition("teach_hold", f"Follower holding here, torque ON.{home_note} Hands off the follower, then follow the leader to teach {name}.")
            else:
                self.transition("teach_hold", f"Follower holding here, torque ON. Hands off the follower; {name} is ready to play.")
        elif action == "teach_follow":
            self.require_phase("teach_hold")
            m.require(self.teach.read_leader is not None, "Connect and calibrate the leader to follow it.")
            if "control" in args:  # the key selected on the map becomes the one being taught
                self.teach_control(args)
            self.teach.follow()
            self.transition("teach_follow", f"Matching the leader at up to {teach_motion.RAMP_SPEED:.0f}°/s. Hold the leader near the follower's pose.")
        elif action == "teach_hold":
            self.require_phase("teach_follow", "teach_record", "teach_play")
            if "control" in args:
                self.teach_control(args)
            discarded = self.teach.frames is not None
            self.teach.hold()
            self.teach_returning = self.teach_going_home = False
            self.teach_going_rest = None
            self.transition("teach_hold", "Holding here." + (" The unfinished recording was discarded." if discarded else ""))
        elif action == "teach_set_home":
            self.require_phase("teach_hold", "teach_follow")
            m.require(self.teach.read_leader is not None, "Connect and calibrate the leader to capture poses.")
            try:
                captured = self.teach.capture()
            except RuntimeError as exc:
                raise m.SafetyError(str(exc)) from exc
            self.save_teach_home(captured)
            if self.teach_points_for:
                self.teach_points["home"] = captured
            self.transition(self.phase, "Home set here. Every key now starts and ends at this home.")
        elif action == "teach_go_home":
            self.require_phase("teach_hold", "teach_follow")
            home_point = self.shared_teach_home()
            m.require(home_point, "No home is set yet. Follow the leader to a clear pose and Set home here.")
            self.teach_played, self.teach_returning, self.teach_going_home, self.teach_going_rest = None, False, True, None
            self.go_to_pose(home_point)
            self.transition("teach_play", f"Moving to home at up to {teach_motion.RAMP_SPEED:.0f}°/s. Stop motion holds the arm.")
        elif action == "recenter_wrist_roll":
            # Both arms' wrist-roll zero moves half a turn so work happens mid-encoder, away from the -180/+180 wrap.
            # Every saved angle gets the same half turn, so taught motions keep pointing at the same physical poses.
            self.require_phase("connected", "ready")
            m.require(not self.leader_store, "Re-centring is not available while two followers share the leader: "
                      "it would turn the leader's wrist zero for both.")
            self.supported(args)
            m.require(self.teaching_mode == "leader" and self.leader is not None and self.calibrated and self.leader_calibrated,
                      "Connect and calibrate both arms first.")
            self.arm.require_torque(False)
            self.leader.require_torque(False)
            old_signature = m.fingerprint(self.calibration)
            shifts, backup = {}, {"at": m.stamp(), "calibration": deepcopy(self.calibration),
                                  "leader_calibration": deepcopy(self.leader_calibration)}
            self.repo.put("wrist_roll_recenter_backup", backup)
            self.reset_note()
            self.actuating = True
            for role, device, name in (("follower", self.arm, "calibration"), ("leader", self.leader, "leader_calibration")):
                calibration = deepcopy(getattr(self, name))
                old_offset, shift = calibration["wrist_roll"]["homing_offset"], 2048
                new_offset = (old_offset - shift + 2048) % 4096 - 2048  # Present = Actual - Offset
                if new_offset == -2048:  # outside the ±2047 register; a half turn less one tick is close enough
                    shift = 2047
                    new_offset = (old_offset - shift + 2048) % 4096 - 2048
                calibration["wrist_roll"]["homing_offset"] = new_offset
                self.guard()
                device.begin_calibration()
                device.commit_calibration(calibration, guard=self.guard)
                if isinstance(device, SimulatedArm):  # the simulator reports the new reference like a real encoder
                    device.current["wrist_roll"] = (device.current["wrist_roll"] + shift) % 4096
                self.repo.put(name, calibration)
                setattr(self, name, calibration)
                shifts[role] = shift
            signature, leader_signature = m.fingerprint(self.calibration), m.fingerprint(self.leader_calibration)

            def moved(pose, role="follower"):
                return {**pose, "wrist_roll": shift_roll(pose["wrist_roll"], shifts[role])} if "wrist_roll" in pose else pose

            def moved_point(point):
                return {"goal": moved(point["goal"]), "measured": moved(point["measured"])}

            updated, selected = 0, self.selected
            for control, entry in [*self.notes.items(), *self.controls.items()]:
                if not entry or entry.get("format") not in RECORDING_FORMATS or entry.get("calibration_sha256") != old_signature:
                    continue  # never revive a motion that was already stale
                entry = deepcopy(entry)
                if "points" in entry:
                    entry["points"] = {n: moved_point(pt) for n, pt in entry["points"].items()}
                if "frames" in entry:
                    entry["frames"] = [{**f, "goal": moved(f["goal"]), "follower": moved(f["follower"]),
                                        **({"leader": moved(f["leader"], "leader")} if "leader" in f else {})}
                                       for f in entry["frames"]]
                entry.update(calibration_sha256=signature, leader_calibration_sha256=leader_signature)
                self.selected = control
                self.save_entry(entry)
                updated += 1
            for doc_name, attr, field in (("teach_home", "teach_home_doc", "point"), ("teach_rest", "teach_rest_doc", "point"),
                                          ("teach_dial", "teach_dial_doc", "points")):
                doc = getattr(self, attr)
                if doc and doc.get("calibration_sha256") == old_signature:
                    doc = {**doc, "calibration_sha256": signature,
                           field: moved_point(doc[field]) if field == "point" else {n: moved_point(pt) for n, pt in doc[field].items()}}
                    self.repo.put(doc_name, doc)
                    setattr(self, attr, doc)
            self.selected, self.teach_points_for = selected, None
            self.event("wrist_roll_recentered", f"Wrist rotation re-centred on both arms ({shifts}); {updated} taught motions updated.",
                       {"shifts": shifts, "updated": updated})
            self.transition("ready", f"Wrist rotation re-centred on both arms. {updated} taught motions, home, rest and the dial "
                            "were updated to match, so nothing needs re-teaching. Nothing moved.")
        elif action == "teach_set_rest":
            self.require_phase("teach_hold", "teach_follow")
            m.require(self.teach.read_leader is not None, "Connect and calibrate the leader to capture poses.")
            try:
                captured = self.teach.capture()
            except RuntimeError as exc:
                raise m.SafetyError(str(exc)) from exc
            self.teach_rest_doc = self.pose_doc(captured)
            self.repo.put("teach_rest", self.teach_rest_doc)
            self.transition(self.phase, "Rest set here. Go to rest moves the arm here; playback waits at home between keys.")
        elif action == "teach_go_rest":
            self.require_phase("teach_hold", "teach_follow")
            rest_point = self.shared_teach_rest()
            m.require(rest_point, "No rest pose is set yet. Follow the leader to it and Set rest here.")
            self.teach_played, self.teach_returning, self.teach_going_home, self.teach_going_rest = None, False, False, "rest"
            self.go_to_pose(rest_point)
            self.transition("teach_play", f"Moving to rest at up to {teach_motion.RAMP_SPEED:.0f}°/s. Stop motion holds the arm.")
        elif action == "teach_capture":
            self.require_phase("teach_hold", "teach_follow")
            m.require(self.teach.read_leader is not None, "Connect and calibrate the leader to capture poses.")
            name = self.teach_control(args)
            points = self.points_for(self.selected)
            names = self.point_names(self.selected)
            final = names[-1]  # press for a key, turn for the dial
            point = args.get("point")
            expected = next((n for n in names if n not in points), None)
            m.require(point in names and (point == expected or point in points),
                      f"Capture {expected} next." if expected else "All steps are captured; select one to retrain it, or Play.")
            try:
                captured = self.teach.capture()
            except RuntimeError as exc:
                raise m.SafetyError(str(exc)) from exc
            # Retraining one step replaces only that step; the other steps are kept.
            for a, b, ignore in STROKE_PAIRS["dial" if self.is_dial else "key"]:
                if point in (a, b) and (b if point == a else a) in points:
                    other = b if point == a else a
                    joint, gap = stroke_gap(captured["goal"], points[other]["goal"], ignore)
                    m.require(gap <= teach_motion.STROKE_LIMIT,
                              f"{point.title()} would be {gap:.0f}° from {other} on {joint}; neighbouring steps should be close "
                              f"(hover just above, then a short move down). Nothing was saved. Guide the arm to the right "
                              f"place, or select {other.title()} and retrain it first.")
            retrained = point in points
            points[point] = captured
            if point == "home":
                self.save_teach_home(captured)
            if self.is_dial and point in DIAL_SHARED:
                shared = {**self.shared_dial_points(), point: captured}
                self.teach_dial_doc = {**self.pose_doc(None), "points": shared}
                self.teach_dial_doc.pop("point")
                self.repo.put("teach_dial", self.teach_dial_doc)
            complete = all(n in points for n in names)
            if point == "home" and expected != "home":
                self.transition(self.phase, "Home moved here. Every key now starts and ends at this home.")
            elif point == final or (complete and point != "home"):
                if self.is_dial:
                    self.save_dial_entries(points)
                else:
                    self.save_entry({"format": WAYPOINT_FORMAT, "control_id": self.selected, "points": deepcopy(points),
                                     "units": "lerobot_degrees_gripper_0_100",
                                     "calibration_sha256": m.fingerprint(self.calibration),
                                     "leader_calibration_sha256": m.fingerprint(self.leader_calibration),
                                     "fixture_id": self.fixture["id"], "mode": self.mode, "saved_at": m.stamp(),
                                     "verification": {"successful_trials": 0}})
                if point == final:
                    self.teach_played, self.teach_returning = None, True
                    back = teach_motion.dial_return_recording if self.is_dial else teach_motion.return_recording
                    self.teach.play(back(points, self.selected), 1.0, force=True, settle_s=teach_motion.RETURN_SETTLE_S)
                    self.transition("teach_play", (
                        "Grip captured. The follower lets go, raises to the open pose, then goes to hover and home on its own. "
                        "Set the turn angle for each direction and Play." if self.is_dial else
                        "Press captured. The follower returns on its own: press → touch → hover → home.")
                        + " Keep hands off the follower; you can leave the leader where it is.")
                else:
                    shared_note = " (shared by both dial directions)" if self.is_dial else ""
                    self.transition(self.phase, f"{point.title()} retrained for {name}{shared_note} and saved. Its other steps are unchanged.")
            else:
                upcoming = next((n for n in names if n not in points), None)
                self.transition(self.phase, f"{point.title()} {'re-captured' if retrained else 'captured'} for {name}. "
                                + (f"Next: guide to {upcoming}{STEP_HINTS.get(upcoming, '')}. Then capture it." if upcoming else ""))
        elif action == "teach_record":
            self.require_phase("teach_follow")
            name = self.teach_control(args)
            m.require(self.teach.mode == "following", "Wait for FOLLOWING before recording.")
            self.teach.start_recording()
            self.teach_played = None
            self.transition("teach_record", f"RECORDING {name}: rest → above it → press until it sounds → lift → back to rest. Then Stop & save.")
        elif action == "teach_save":
            self.require_phase("teach_record")
            try:
                frames = self.teach.stop_recording()
            except RuntimeError as exc:
                self.transition("teach_follow", str(exc))
                raise m.SafetyError(str(exc)) from exc
            entry = {"format": RECORDING_FORMAT, "control_id": self.selected, "fps": teach_motion.FPS,
                     "units": "lerobot_degrees_gripper_0_100", "frames": frames,
                     "calibration_sha256": m.fingerprint(self.calibration),
                     "leader_calibration_sha256": m.fingerprint(self.leader_calibration),
                     "fixture_id": self.fixture["id"], "mode": self.mode, "saved_at": m.stamp(),
                     "verification": {"successful_trials": 0}}
            self.save_entry(entry)
            self.transition("teach_follow", f"Saved {self.control['name']} ({frames[-1]['t']:.1f} s). Still following. "
                            "Return the arm to rest, then Play it back.")
        elif action == "teach_play":
            self.require_phase("teach_hold", "teach_follow")
            name = self.teach_control(args)
            speed = self.play_speed(args)
            recording, description = self.control_motion(self.selected, args)
            self.teach_sequence_label = None
            self.start_playback(recording, speed, args)
            self.transition("teach_play", f"Playing {name} at {speed:g}× speed: {description}. Stop motion holds the arm.")
        elif action == "teach_sequence":
            self.require_phase("teach_hold", "teach_follow")
            steps = args.get("steps")
            m.require(isinstance(steps, list) and 1 <= len(steps) <= 64 and all(isinstance(x, dict) for x in steps),
                      "Give 1 to 64 steps, each like {\"control\": \"C\"}.")
            speed = self.play_speed(args)
            parts = []
            for step in steps:
                control = step.get("control")
                m.require(isinstance(control, str) and control in CATALOG, f"Unknown control {control!r}.")
                m.require(((self.notes.get(control) if control in m.KEYS else self.controls.get(control)) or {}).get("format") == WAYPOINT_FORMAT,
                          f"{CATALOG[control]['name']} has no taught steps; sequences use taught home/hover/touch/press motions.")
                parts.append(self.control_motion(control, {k: v for k, v in step.items() if k != "control"})[0])
            self.teach_sequence_label = " → ".join(CATALOG[x["control"]]["label"] for x in steps)
            self.start_playback(teach_motion.sequence_recording(parts), speed, args)
            self.transition("teach_play", f"Playing {self.teach_sequence_label} at {speed:g}× speed, through home between "
                            "controls. Stop motion holds the arm.")
        elif action == "teach_settings":
            changes = {}
            if "speed" in args:
                changes["speed"] = self.play_speed(args)
            if "press_s" in args:
                press_s = args["press_s"]
                m.require(type(press_s) in (int, float) and 0 <= press_s <= teach_motion.MAX_PRESS_S,
                          f"Choose a press length from 0 to {teach_motion.MAX_PRESS_S:g} seconds.")
                changes["press_s"] = float(press_s)
            if "press_hardness" in args:
                hardness = args["press_hardness"]
                m.require(type(hardness) in (int, float) and teach_motion.MIN_PRESS_HARDNESS <= hardness <= 1,
                          f"Choose a press hardness from {teach_motion.MIN_PRESS_HARDNESS:.0%} to 100%.")
                changes["press_hardness"] = float(hardness)
            m.require(changes, "Give a speed, press_s and/or press_hardness.")
            self.teach_settings = {**self.teach_settings, **changes}
            self.repo.put("teach_settings", self.teach_settings)
            self.event("teach_settings", "Playback settings: " + ", ".join(f"{k} {v:g}" for k, v in self.teach_settings.items()))
        elif action == "teach_configure":
            control = args.get("control")
            m.require(isinstance(control, str) and control in CATALOG, "Choose an instrument control.")
            allowed = {"turn_degrees"} if CATALOG[control]["kind"] == "dial" else {"press_s"}
            m.require(set(args) - {"control"} == allowed,
                      "Give turn_degrees for a dial direction, or press_s for a key or chord button.")
            self.control_motion(control, args)  # validates and saves the new default
            self.event("teach_configure", f"{CATALOG[control]['name']}: " + ", ".join(f"{k} {v}" for k, v in args.items() if k != "control"))
        elif action == "teach_verify":
            self.require_phase("teach_hold")
            m.require(self.teach_played == self.selected, "Play this control back before marking it good.")
            entry = self.recording_entry(self.selected)
            trials = entry.get("verification", {}).get("successful_trials", 0) + 1
            self.save_entry({**entry, "verification": {"successful_trials": trials, "verified_at": m.stamp(),
                             "method": "simulated playback" if self.mode == "simulation" else "operator heard playback"}})
            self.teach_played = None
            self.transition("teach_hold", f"{self.control['name']} marked good. Pick another control above, or play it again.")
        elif action in ("release", "disconnect", "retry"):
            m.require(self.arm is not None, "No arm is connected")
            self.supported(args)
            self.actuating = True
            try:
                self.arm.release()
                if self.leader:
                    self.leader.release()
            except Exception as exc:
                raise m.SafetyError("Torque release could not be verified. If USB was unplugged or the arm was replaced, "
                                    "support BOTH arms, disconnect their motor power, then use "
                                    "01 · Connect the arm → Forget unavailable connection. "
                                    f"Connection error: {exc}") from exc
            if self.controller:
                self.controller.enabled = False
            if self.calibrating:
                self.calibration_arm.abort_calibration()
                self.calibrating = False
                self.mark_calibrated(bool(self.saved_calibration) and getattr(self.calibration_arm, "calibration_matches", True))
                self.clear_calibration_capture()
            self.reset_note()
            self.stop_event.clear()
            if action == "disconnect":
                if self.leader:
                    self.leader.close()
                    self.leader = None
                    self.leader_calibrated = False
                    self.leader_current = self.leader_torque = self.leader_feedback_at = None
                self.arm.close()
                self.arm = None
                self.follower_port = None
                self.current = self.torque = None
                self.feedback_at = None
                self.diagnostics = self.diagnostics_error = None
                self.calibrated = False
                self.transition("disconnected", "Follower disconnected after supported torque release. Saved registrations are retained.")
            else:
                retry = action == "retry" and self.calibrated and self.teaching_mode != "leader"
                self.transition(("home_approach" if self.teaching_mode == "leader" else "dial_ready" if self.is_dial else "note_ready") if retry else "connected",
                                self.ready_message() if retry else "Torque OFF. Rest the arm safely or continue setup.")
        else:
            raise m.SafetyError("Unknown action")

    def process(self, command):
        receipt = self.receipts[command["id"]]
        self.actuating = False
        self.incidents.record({"stage": "operator_command", "phase": self.phase,
                               "action": command["action"], "command_id": command["id"], "args": command["args"]})
        try:
            self.guard()
            m.require(command["owner"] == self.owner and time.monotonic() - command["created"] < 5,
                      "Command expired or the operator changed; no action taken.")
            m.require(command["revision"] == self.revision, "Stale workflow command rejected.")
            self.dispatch(command["action"], command["args"])
            receipt.update(status="complete", message=self.message)
        except Exception as exc:
            # Invalid requests stay on the same step. Hardware/protocol failures
            # stop; callbacks handling active motion already establish a hold.
            if (self.actuating or not isinstance(exc, m.SafetyError)) and self.arm is not None and self.phase != "fault":
                self.fault(exc)
            receipt.update(status="rejected", message=str(exc))
            self.error = str(exc)
            self.event("rejected", str(exc), {"action": command["action"], "command_id": command["id"]}, exc=exc)
        finally:
            with self.lock:
                self.pending = False
            self.publish()

    def step(self):
        if self.stop_event.is_set():
            if self.teach and self.phase in TEACH_SESSION_PHASES:
                # Stop motion = hold at a fresh measured pose. Teaching continues from the hold.
                self.stop_event.clear()
                try:
                    self.teach.hold()
                    self.teach_returning = self.teach_going_home = False
                    self.teach_going_rest = None
                    if self.tuning:
                        self.tune_stop("Stopped by Stop motion. Nothing was saved.")
                    self.transition("teach_hold", "Stopped; holding here. Start following or play again when ready.")
                except Exception as exc:
                    self.fault(exc)
            else:
                self.fault(m.SafetyError("Stop requested. No automatic retraction or torque release."))
        try:
            command = self.commands.get_nowait()
        except queue.Empty:
            command = None
        if command:
            self.process(command)
        self.collect_key_check()
        self.tune_due()
        if self.arm and self.phase not in ("fault", "disconnected"):
            try:
                if self.simulated_leader_input:
                    name, remaining = self.simulated_leader_input
                    delta = max(-4, min(4, remaining))
                    self.leader.current[name] += delta
                    remaining -= delta
                    self.simulated_leader_input = (name, remaining) if remaining else None
                if self.teach:
                    self.guard()
                    self.teach_tick()
                elif self.controller and self.controller.enabled:
                    self.guard()
                    self.controller.tick(self.controller.previous, "idle_hold")
                    if self.phase == "home_arrival" and not self.controller.engaged:
                        self.home_arrival_samples = self.home_arrival_samples + 1 if home.at_home(self.current, self.home["pose"]) else 0
                        if self.home_arrival_samples >= 3:
                            self.controller.arm = home.HomeArm(self.arm, self.home["pose"])
                            self.controller.set_travel("approach", self.current)
                            self.route_approach = [dict(self.current)]
                            self.home_move_progress = None
                            self.transition("home_approach", f"At home and holding. Engage the leader to teach {self.control['name']}.")
                else:
                    if self.calibrating:
                        self.guard()
                    self.sample()
            except Exception as exc:
                self.fault(exc)
        self.publish()

    def run(self):
        try:
            while not self.shutdown.is_set():
                started = self.clock()
                self.step()
                period = teach_motion.PERIOD if self.teach else m.PERIOD
                self.sleep(max(0, period - (self.clock() - started)))
        except Exception as exc:
            # Even a storage failure must leave a visible fault and enter cleanup.
            self.report_error("worker_failed", exc)
            self.phase, self.error = "fault", f"Motor worker stopped: {exc}"
            self.message = "Support the arm, inspect the local app, and restart before continuing."
            self.torque = self.feedback_at = None
            self.pending = False
            self.publish()
        finally:
            self.cleanup_devices()

    def cleanup_devices(self):
        operations = []
        if self.teach:
            operations.append(("hold_teach", self.teach.hold))
        if self.controller:
            operations.append(("stop_controller", self.controller.stop))
        if self.calibrating and self.arm:
            operations.append(("calibration_rollback", self.calibration_arm.abort_calibration))
        for role, device in (("follower", self.arm), ("leader", self.leader), ("motion_log", self.log)):
            if device:
                operations.append((f"close_{role}", device.close))
        for operation, close in operations:
            try:
                close()
            except Exception as exc:
                self.report_error("cleanup_failed", exc, {"operation": operation})

    def start(self):
        self.thread = threading.Thread(target=self.run, name="orchid-motor-owner", daemon=True)
        self.thread.start()

    def close(self):
        self.shutdown.set()
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=4)
            m.require(not self.thread.is_alive(), "Motor worker did not stop; use the physical stop if needed.")
        else:
            self.cleanup_devices()
        self.incidents.close()
        self.repo.close()
        self.event_log.close()

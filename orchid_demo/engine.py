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
from .controls import CATALOG, group_members
from .devices import HardwareArm, SimulatedArm, validate_calibration
from .discovery import discover_arms, follower_problem
from .storage import Repository
from .telemetry import motor_status, pose_angles

RANGE_MOTORS = tuple(name for name in m.MOTORS if name != "wrist_roll")
REQUIRED_TRIALS = 3
LEASE_SECONDS = 5.0
CALIBRATION_SETUP_PHASES = ("connected", "ready", "calibration_midpoint", "calibration_range", "calibration_review")


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
        current = m.stable_pose(self.arm, self.sleep)
        self.check_start(current)
        self.guard()
        self.enabled = True
        self.arm.arm_at_current(current)
        self.previous, self.last_tick, self.started = current, None, None
        return current


class Engine:
    def __init__(self, directory: Path, mode="simulation", *, clock=time.monotonic, sleep=time.sleep,
                 hardware_factory=HardwareArm, port_scanner=discover_arms):
        m.require(mode in ("simulation", "hardware"), "Unknown operating mode")
        self.mode, self.clock, self.sleep = mode, clock, sleep
        self.hardware_factory = hardware_factory
        self.port_scanner = port_scanner
        self.discovery = {"ports": [], "scanned_at": None, "scanning": False, "warnings": [], "error": None}
        if mode == "simulation":
            self.discovery["ports"] = [{"path": "simulator", "description": "Practice arm · no hardware",
                                        "role": "simulator", "motor_ids": list(range(1, 7)), "voltage": 12.0,
                                        "connectable": True, "problem": None}]
        self.repo = Repository(directory, mode)
        self.calibration = self.repo.get("calibration")
        self.fixture = self.repo.get("fixture", {"label": "Orchid demo", "id": ""})
        self.notes = self.repo.notes()
        self.controls = self.repo.controls()
        self.events = self.repo.events()
        self.instance_id = uuid.uuid4().hex
        self.revision, self.phase = 0, "disconnected"
        self.message = "Connect the practice arm to explore the complete workflow." if mode == "simulation" else "Secure the arm clear of Orchid, then choose its follower port."
        self.arm = self.controller = self.log = None
        self.current = self.torque = None
        self.feedback_at = None
        self.diagnostics = None
        self.diagnostics_error = None
        self.selected = "C"
        self.capture = self.draft = None
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

    def event(self, kind, message, detail=None):
        self.repo.event(kind, message, detail)
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
            valid = bool(entry and entry.get("calibration_sha256") == signature
                         and entry.get("fixture_id") == self.fixture["id"]
                         and entry.get("mode") == self.mode)
            trials = entry.get("verification", {}).get("successful_trials", 0) if valid else 0
            result[name] = {"status": "registered" if trials >= REQUIRED_TRIALS else "testing" if valid else "needs_reteach" if entry else "empty",
                            "trials": trials, "saved_at": entry.get("saved_at") if entry else None}
        return result

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
        if self.is_dial:
            return "Torque OFF. Start just clear of the large voicing dial; teach a small turn, lift-off, and clear return."
        if self.control["kind"] == "button":
            return f"Torque OFF. Support the arm and gently actuate {self.control['label']}. Observe Orchid's response; modifiers may not sound alone."
        return f"Torque OFF. Support the arm and gently press {self.selected} only until it sounds."

    def publish(self):
        with self.lock:
            reference_ready = self.calibrated or (self.calibrating and self.offsets is not None
                                                   and self.phase != "calibration_midpoint")
            target = (self.controller.previous if self.controller and self.controller.enabled
                      and self.phase != "fault" else None)
            self.export_data = {"schema_version": 2, "application": "orchid-demo", "mode": self.mode,
                                "units": m.UNITS, "exported_at": m.stamp(), "calibration": deepcopy(self.calibration),
                                "fixture": deepcopy(self.fixture), "keys": deepcopy(self.notes),
                                "controls": deepcopy(self.controls), "events": deepcopy(self.events)}
            self.public = {
                "instance_id": self.instance_id, "revision": self.revision, "mode": self.mode,
                "phase": self.phase, "message": self.message, "error": self.error,
                "connected": self.arm is not None, "calibrated": self.calibrated,
                "calibrating": self.calibrating,
                "calibration_id": m.fingerprint(self.calibration)[:12] if self.calibration else None,
                "discovery": deepcopy(self.discovery),
                "fixture": deepcopy(self.fixture), "selected": self.selected,
                "position": deepcopy(self.current), "torque": deepcopy(self.torque),
                "feedback_at": self.feedback_at, "heartbeat": time.time(),
                "motor_status": motor_status(self.current, self.torque, self.calibration, self.ranges,
                                             reference_ready=reference_ready, recording=self.calibrating,
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
                "capture_samples": len(self.capture["path"]) if self.capture else 0,
                "range_motor": RANGE_MOTORS[self.range_index] if self.phase == "calibration_range" else None,
                "range_index": self.range_index, "ranges": deepcopy(self.ranges),
                "calibration": deepcopy(self.calibration), "events": deepcopy(self.events),
                "last_receipt": deepcopy(next(reversed(self.receipts.values()))) if self.receipts else None,
            }

    def snapshot(self):
        with self.lock:
            return {**deepcopy(self.public), "pending": self.pending, "operator": self.owner,
                    "lease_live": self.lease_until > time.monotonic(),
                    "worker_alive": self.thread is None or self.thread.is_alive()}

    def export_snapshot(self):
        with self.lock:
            return deepcopy(self.export_data)

    def heartbeat(self, owner):
        with self.lock:
            now = time.monotonic()
            m.require(self.owner in (None, owner) or self.lease_until <= now,
                      "Another browser is operating this arm. This window is read-only.")
            # Once a powered operation loses its owner, a new owner cannot revive it.
            if self.owner and self.lease_until <= now and self.controller and self.controller.enabled:
                self.stop_event.set()
            self.owner = owner
            self.lease_until = now + LEASE_SECONDS

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

    def require_phase(self, *phases):
        m.require(self.phase in phases, "That action is not available at this step.")

    def stable(self, enforce_limits=True):
        samples = []
        for _ in range(7):
            self.guard()
            self.sample()
            if enforce_limits:
                self.arm.check_pose(self.current)
            samples.append(dict(self.current))
            self.sleep(0.1)
        spread = {n: max(p[n] for p in samples) - min(p[n] for p in samples) for n in m.MOTORS}
        unsettled = [f"{n}: {span} ticks" for n, span in spread.items() if span > 2]
        m.require(not unsettled, "Encoder readings did not stay within the 2-tick capture window ("
                  + "; ".join(unsettled) + "). Keep supporting the arm and retry when steady.")
        return samples[-1]

    def simulate_pose(self, label):
        if self.mode != "simulation":
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
        previous = self.current
        torque = self.arm.torque_status()
        current = self.arm.read_raw()
        if self.phase != "connected":
            self.arm.require_torque(False)
            m.require(all(v == 0 for v in torque.values()), "Unexpected enabled motor during manual teaching")
        now = self.clock()
        m.require(now - start <= m.MAX_IO_TIME, "Motor feedback is stale. No new movement will be issued.")
        recording = self.phase in ("note_pressed", "note_touch", "dial_approach", "dial_contact", "dial_turned", "dial_lifted")
        if recording and self.last_read is not None:
            m.require(now - self.last_read <= m.MAX_IO_TIME, "Recording has a gap. Re-teach this control.")
        self.current, self.torque = current, torque
        self.last_read, self.feedback_at = now, time.time()
        if self.phase == "calibration_range":
            motor = RANGE_MOTORS[self.range_index]
            span = self.ranges.setdefault(motor, {"min": current[motor], "max": current[motor]})
            span["min"] = min(span["min"], current[motor])
            span["max"] = max(span["max"], current[motor])
        if recording:
            self.arm.check_pose(current)
            m.append_capture(self.capture, current, previous_observation=previous)
            if self.is_dial:
                # Validate even samples omitted by path decimation.
                dial.validate({**self.capture, "path": self.capture["path"] + [current]})
        self.publish()

    def feedback(self, current, stage):
        self.current, self.stage = current, stage
        self.torque = dict.fromkeys(m.MOTORS, 1)
        self.feedback_at = time.time()
        self.publish()

    def reset_note(self):
        self.capture = self.draft = self.controller = None
        self.touch_index = None
        self.trials = 0
        self.last_read = None
        self.stage = None
        if self.log:
            self.log.close()
            self.log = None

    def clear_calibration_capture(self):
        self.offsets = None
        self.ranges, self.range_index = {}, 0
        # A reference change invalidates the previous encoder/3D sample.
        self.current = self.feedback_at = self.last_read = None

    def begin_calibration(self):
        backup = self.arm.begin_calibration()
        self.calibrating = True
        self.repo.put("calibration_backup", {"at": m.stamp(), "hardware": backup, "saved": self.calibration})

    def establish_hold(self, draft, planned):
        self.repo.put("draft", {"key": self.selected, "entry": draft})
        self.draft = draft
        self.log = (self.repo.directory / f"trial-{uuid.uuid4().hex}.jsonl").open("x")
        self.controller = GuardedController(self.arm, planned, self.log, clock=self.clock, sleep=self.sleep,
                                            guard=self.guard, feedback=self.feedback)
        self.transition("arming", "Keep supporting while the motors establish a hold at this captured position.")
        try:
            self.guard()
            self.controller.arm_here()
            self.controller.tick(self.controller.previous, "hold_verified")
        except Exception as exc:
            self.fault(exc)
            raise
        self.transition("holding", "Holding position, torque ON. Gently clear your hands before testing.")

    def fault(self, exc):
        message = str(exc) or type(exc).__name__
        if self.controller:
            self.controller.stop()
        if self.calibrating:
            self.calibrated = False
            try:
                self.arm.abort_calibration()
                self.calibrating = False
            except Exception as restore_error:
                message += f" Calibration restoration unverified: {restore_error}"
        self.phase = "fault" if self.arm is not None else "disconnected"
        self.revision += 1
        self.error = message
        self.message = ("Stopped. Support the arm before releasing torque. Inspect the cause before retrying."
                        if self.arm is not None else "Stopped before connecting. Choose a follower to start again.")
        self.torque = None
        self.feedback_at = None
        self.stop_event.clear()
        self.event("fault", message)
        self.publish()

    def dispatch(self, action, args):
        if action == "refresh_ports":
            self.require_phase("disconnected")
            m.require(self.arm is None, "Disconnect the follower before scanning USB ports.")
            if self.mode == "simulation":
                return  # Never load the serial SDK or touch USB in practice mode.
            self.discovery = {"ports": [], "scanned_at": None, "scanning": True, "warnings": [], "error": None}
            self.publish()
            try:
                result = self.port_scanner(guard=self.guard)
                self.guard()
                self.discovery["ports"] = [
                    {**arm, "path": arm["port"], "description": arm["role"] or "Unidentified arm",
                     "connectable": follower_problem(arm) is None, "problem": follower_problem(arm)}
                    for arm in result["arms"]
                ]
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
                candidate.open()
                self.arm = candidate
                self.diagnostics = self.diagnostics_error = None
                self.calibrated = bool(self.calibration) and getattr(candidate, "calibration_matches", True)
                self.transition("connected", "Follower connected. Check motor state, then calibrate or register notes.")
                self.sample()
            except Exception:
                candidate.close()
                self.arm = None
                self.calibrated = False
                self.phase = "disconnected"
                raise
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
            reload_saved = action == "calibration_reload"
            if reload_saved:
                # Validate before releasing torque or discarding an active sweep.
                validate_calibration(self.calibration)
                m.require(args.get("calibration_unchanged") is True,
                          "Confirm this is the same arm and no motors or joints have been replaced or reseated since saving.")
            self.guard()
            self.actuating = True
            self.arm.release()
            self.calibrated = False
            if self.calibrating:
                self.arm.abort_calibration()
                self.calibrating = False
            self.reset_note()
            self.clear_calibration_capture()
            self.guard()
            self.begin_calibration()
            if reload_saved:
                self.arm.commit_calibration(self.calibration, guard=self.guard)
                self.calibrated, self.calibrating = True, False
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
            self.offsets = self.arm.center(guard=self.guard, sleep=self.sleep)
            self.ranges, self.range_index = {}, 0
            self.transition("calibration_range", "Slowly move the base rotation through its usable travel in both directions. Do not force the stops.")
            self.sample()
        elif action == "simulate_sweep":
            self.require_phase("calibration_range")
            m.require(self.mode == "simulation", "Simulation actions cannot operate hardware.")
            motor = RANGE_MOTORS[self.range_index]
            for value in (2047, 1800, 1400, 1000, 1400, 2047, 2600, 3100, 2047):
                self.guard()
                self.arm.current[motor] = value
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
            self.arm.commit_calibration(new, guard=self.guard)
            self.repo.put("calibration", new)
            self.calibration, self.calibrated, self.calibrating = new, True, False
            self.event("calibration_saved", "All six motors calibrated; torque remains off.", new)
            self.transition("ready", "Calibration verified. Fix the padded gripper opening and begin with C.")
        elif action in ("note_start", "control_start"):
            self.require_phase("connected", "ready", "saved")
            self.supported(args)
            m.require(self.calibrated, "Complete motor calibration first.")
            selected = args.get("control", args.get("key", self.selected))
            m.require(isinstance(selected, str) and selected in CATALOG, "Choose an instrument control.")
            if action == "note_start":
                m.require(selected in m.KEYS, "Choose one of the twelve notes.")
            self.actuating = True
            self.arm.release()
            if self.controller:
                self.controller.enabled = False
            self.reset_note()
            self.selected = selected
            self.transition("dial_ready" if self.is_dial else "note_ready", self.ready_message())
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
            self.supported(args)
            m.require(args.get("rim_clear") is True, "Confirm the entire return path stayed clear of the dial.")
            self.simulate_pose("dial_start")
            current = self.stable()
            m.append_capture(self.capture, current, force=True)
            draft = {**self.capture, "complete": True, "mode": self.mode, "fixture_id": self.fixture["id"],
                     "control_id": self.selected, "teaching_mode": "web_dial_forward_loop"}
            self.establish_hold(draft, dial.plan(draft))
        elif action == "capture_pressed":
            self.require_phase("note_ready")
            self.simulate_pose("pressed")
            current = self.stable()
            self.capture = m.teaching_entry(self.arm, True, self.fixture["label"], current)
            self.capture["contact_surface"] = self.fixture.get("tool", "padded_gripper")
            self.transition("note_pressed", "Press captured. Slowly lift until the control has released but the pad still barely touches it.")
        elif action == "capture_touch":
            self.require_phase("note_pressed")
            self.simulate_pose("touch")
            current = self.stable()
            m.require(m.distance(self.capture["path"][0], current) > 0, "No release movement measured yet.")
            m.append_capture(self.capture, current, force=True)
            self.touch_index = len(self.capture["path"]) - 1
            self.transition("note_touch", "Contact captured. Lift to a small visible clearance. Keep supporting; the next capture enables a hold.")
        elif action == "capture_clear":
            self.require_phase("note_touch")
            self.supported(args)
            self.simulate_pose("clear")
            hover = self.stable()
            m.append_capture(self.capture, hover, force=True)
            path = list(reversed(self.capture["path"]))
            draft = {**self.capture, "path": path, "touch_index": len(path) - 1 - self.touch_index,
                     "complete": True, "mode": self.mode, "fixture_id": self.fixture["id"],
                     "control_id": self.selected, "teaching_mode": "web_release_path"}
            m.validate_entry(draft, complete=True)
            m.require(m.distance(path[0], path[draft["touch_index"]]) > 0, "Lift clear of the contact pose before capturing.")
            planned = m.plan(draft)
            m.require(m.routine_budget(planned, .2) <= m.MAX_RUN_TIME, "This path is too long; retry with a smaller local stroke.")
            self.establish_hold(draft, planned)
        elif action == "test":
            self.require_phase("holding", "saved")
            m.require(args.get("hands_clear") is True, "Confirm hands are clear of the motion path.")
            if self.is_dial:
                m.require(args.get("reference_reset") is True, "Restore the reference chord and voicing before each dial trial.")
            self.transition("testing", f"Testing {self.control['name']}: " +
                            ("one forward nudge, lift-off, and clear return." if self.is_dial else "one press and release, then a hold at hover."))
            try:
                if self.is_dial:
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
            self.actuating = True
            self.arm.release()
            self.controller.enabled = False
            self.reset_note()
            statuses = {**self.key_statuses(), **self.statuses(self.controls)}
            missing = [name for name in group_members(self.selected) if statuses[name]["status"] != "registered"]
            if missing:
                self.selected = missing[0]
                self.transition("dial_ready" if self.is_dial else "note_ready", self.ready_message())
            else:
                self.transition("ready", "This control group is registered. Rest the arm safely or select another group on the instrument.")
        elif action in ("release", "disconnect", "retry"):
            m.require(self.arm is not None, "No arm is connected")
            self.supported(args)
            self.actuating = True
            self.arm.release()
            if self.controller:
                self.controller.enabled = False
            if self.calibrating:
                self.arm.abort_calibration()
                self.calibrating = False
                self.calibrated = bool(self.calibration) and getattr(self.arm, "calibration_matches", True)
                self.clear_calibration_capture()
            self.reset_note()
            self.stop_event.clear()
            if action == "disconnect":
                self.arm.close()
                self.arm = None
                self.current = self.torque = None
                self.feedback_at = None
                self.diagnostics = self.diagnostics_error = None
                self.calibrated = False
                self.transition("disconnected", "Follower disconnected after supported torque release. Saved registrations are retained.")
            else:
                retry = action == "retry" and self.calibrated
                self.transition(("dial_ready" if self.is_dial else "note_ready") if retry else "connected",
                                self.ready_message() if retry else "Torque OFF. Rest the arm safely or continue setup.")
        else:
            raise m.SafetyError("Unknown action")

    def process(self, command):
        receipt = self.receipts[command["id"]]
        self.actuating = False
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
            self.event("rejected", str(exc), {"action": command["action"]})
        finally:
            with self.lock:
                self.pending = False
            self.publish()

    def step(self):
        if self.stop_event.is_set():
            self.fault(m.SafetyError("Stop requested. No automatic retraction or torque release."))
        try:
            command = self.commands.get_nowait()
        except queue.Empty:
            command = None
        if command:
            self.process(command)
        if self.arm and self.phase not in ("fault", "disconnected"):
            try:
                if self.controller and self.controller.enabled:
                    self.guard()
                    self.controller.tick(self.controller.previous, "idle_hold")
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
                self.sleep(max(0, m.PERIOD - (self.clock() - started)))
        except Exception as exc:
            # Even a storage failure must leave a visible fault and enter cleanup.
            self.phase, self.error = "fault", f"Motor worker stopped: {exc}"
            self.message = "Support the arm, inspect the local app, and restart before continuing."
            self.torque = self.feedback_at = None
            self.pending = False
            self.publish()
        finally:
            if self.controller:
                self.controller.stop()
            if self.arm:
                try:
                    if self.calibrating:
                        self.arm.abort_calibration()
                finally:
                    self.arm.close()
            if self.log:
                self.log.close()

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
            if self.controller:
                self.controller.stop()
            if self.arm:
                if self.calibrating:
                    self.arm.abort_calibration()
                self.arm.close()
            if self.log:
                self.log.close()
        self.repo.close()

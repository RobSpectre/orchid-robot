#!/usr/bin/env python3
"""One attended Orchid session, with commands relayed from this chat.

The human starts `serve` in the terminal that can see the follower. The agent
uses `status` and `send` through local files; neither opens a motor port. Voice
is supplied by the chat app, not microphone recognition in this program.
"""

from __future__ import annotations

import argparse
from collections import deque
import fcntl
import json
import math
from pathlib import Path
import sys
import time
import uuid

import orchid_key as key

ROOT = Path(__file__).resolve().parent
DEFAULT_SESSION = ROOT / "orchid_session"
COMMAND_LIFETIME = 10.0
HEARTBEAT_LIMIT = 2.0
ACTIONS = ("select", "pressed", "touch", "clear", "test", "pass", "fail",
           "next", "retry", "release", "stop", "quit")


def atomic_json(path, value):
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def read_status(folder, require_live=True):
    status = json.loads((folder / "status.json").read_text())
    if require_live:
        age = time.time() - status["heartbeat"]
        key.require(0 <= age <= HEARTBEAT_LIMIT and status["running"],
                    "No fresh live session. Start serve in the robot's terminal; no command sent.")
    return status


def send_command(folder, action, selected=None, supported=False):
    """Queue exactly one expiring command addressed to the current session/state."""
    status = read_status(folder)
    key.require(action in ACTIONS, "Unknown command.")
    key.require(action not in ("next", "retry", "release", "quit") or supported,
                "This command requires the human to say the arm is supported (--supported).")
    command = {"id": uuid.uuid4().hex, "session_id": status["session_id"],
               "revision": status["revision"], "action": action, "key": selected,
               "supported": supported, "created": time.time()}
    destination = folder / ("stop.json" if action == "stop" else f"commands/{command['id']}.json")
    atomic_json(destination, command)
    return command["id"]


class SessionController(key.Controller):
    def __init__(self, *args, check_stop, feedback, **kwargs):
        super().__init__(*args, **kwargs)
        self.check_stop = check_stop
        self.feedback = feedback

    def tick(self, target, stage):
        self.check_stop()
        current = super().tick(target, stage)
        self.feedback(current)
        return current


class Session:
    def __init__(self, arm, folder, map_path, fixture_note, log,
                 clock=time.monotonic, sleep=time.sleep, wall=time.time):
        self.arm, self.folder, self.map_path, self.log = arm, folder, map_path, log
        self.fixture_note = fixture_note
        self.clock, self.sleep, self.wall = clock, sleep, wall
        self.session_id = uuid.uuid4().hex
        self.revision = 0
        self.running = True
        self.phase = "ready"
        self.selected = "C"
        self.message = "Torque OFF. Support the arm; select a key, then gently press it and say pressed."
        self.error = None
        self.current = self.torque = None
        self.feedback_time = None
        self.recent = deque()
        self.previous_read = None
        self.capture = self.touch_index = self.draft = None
        self.controller = None
        self.completed_trials = 0
        self.last_ack = None
        self.attempt = None
        self.test_number = 0
        self.accepted_test = 0
        self.map_digest = key.fingerprint(key.load_store(map_path))
        self.next_publish = 0.0

    def event(self, **data):
        self.log.write(json.dumps({"time": self.clock(), "utc": key.stamp(),
                                   "session_id": self.session_id, **data}) + "\n")
        self.log.flush()

    def transition(self, phase, message):
        self.phase, self.message = phase, message
        self.revision += 1
        self.event(event="state", phase=phase, key=self.selected, message=message)
        key.announce(message)
        self.publish()

    def publish(self):
        store = key.load_store(self.map_path)
        saved = {name: ("tested" if value.get("verification", {}).get("successful_trials", 0)
                        else "captured") if value else "empty"
                 for name, value in store["keys"].items()}
        status = {"session_id": self.session_id, "revision": self.revision,
                  "heartbeat": self.wall(), "running": self.running,
                  "phase": self.phase, "key": self.selected, "message": self.message,
                  "error": self.error, "torque": self.torque, "position": self.current,
                  "feedback_time": self.feedback_time,
                  "keys": saved, "last_ack": self.last_ack,
                  "attempt": self.attempt, "completed_trials": self.completed_trials,
                  "capture_samples": len(self.capture["path"]) if self.capture else 0}
        atomic_json(self.folder / "status.json", status)
        self.next_publish = self.clock() + 0.25

    def check_stop(self):
        # Also publish during powered trials so the agent can observe progress.
        if self.clock() >= self.next_publish:
            self.publish()
        path = self.folder / "stop.json"
        if path.exists():
            command = json.loads(path.read_text())
            path.unlink()
            try:
                self.validate_command(command, revision=False)
            except Exception as exc:
                self.ack(command, False, str(exc))
                return
            self.ack(command, True, "Stop requested; interrupting the trial/hold.")
            raise key.SafetyError("Chat stop requested. No automatic retraction or torque release.")

    def powered_feedback(self, current):
        self.current = current
        self.torque = {m: 1 for m in key.MOTORS}  # Controller.tick verified all six
        self.feedback_time = self.wall()

    def validate_command(self, command, revision=True):
        key.require(command["session_id"] == self.session_id, "Command belongs to an older session.")
        key.require(0 <= self.wall() - command["created"] <= COMMAND_LIFETIME,
                    "Command expired; no action taken.")
        key.require(not revision or command["revision"] == self.revision,
                    "Session stage changed; stale command rejected.")
        key.require(command["action"] in ACTIONS, "Unknown command.")

    def ack(self, command, ok, message):
        response = {"id": command.get("id"), "action": command.get("action"),
                    "ok": ok, "message": message, "phase": self.phase,
                    "session_id": self.session_id, "revision": self.revision}
        self.last_ack = response
        # Filenames supplied by another process never become arbitrary paths.
        identifier = command.get("id", "")
        if len(identifier) == 32 and all(c in "0123456789abcdef" for c in identifier):
            atomic_json(self.folder / "responses" / f"{identifier}.json", response)
        self.event(event="command_result", **response)
        self.publish()

    def marker_pose(self):
        key.require(self.recent and self.clock() - self.recent[-1][0] <= key.MAX_IO_TIME,
                    "No fresh encoder samples yet; keep supporting and wait.")
        now = self.recent[-1][0]
        key.require(now - self.recent[0][0] >= key.AUTO_SETTLE_SECONDS,
                    "Hold still for a moment, then repeat the cue.")
        key.require(all(max(p[m] for _, p in self.recent) - min(p[m] for _, p in self.recent) <= 2
                        for m in key.MOTORS), "Still moving. Hold steady and repeat the cue.")
        current = dict(self.recent[-1][1])
        self.arm.check_pose(current)
        self.arm.require_torque(False)
        return current

    def observe(self):
        start = self.clock()
        self.check_stop()
        if self.controller and self.controller.enabled and self.phase != "fault":
            self.current = self.controller.tick(self.controller.previous, "session_hold")
            self.torque = {m: 1 for m in key.MOTORS}  # tick verified every flag
            self.recent.clear()
            self.previous_read = None
            return
        # A fault never silently re-arms or releases the arm.
        self.torque = self.arm.torque_status()
        self.current = self.arm.read_raw()
        self.feedback_time = self.wall()
        if self.phase == "fault":
            return
        self.arm.require_torque(False)
        key.require(all(v == 0 for v in self.torque.values()), "Unexpected enabled motor during teaching.")
        now = self.clock()
        key.require(now - start <= key.MAX_IO_TIME, "Stale encoder/torque feedback.")
        if self.previous_read is not None and now - self.previous_read > key.MAX_IO_TIME:
            self.recent.clear()
            if self.capture and self.phase in ("pressed", "touch"):
                self.capture_fault("Gap in recording; this attempt cannot be replayed. Say retry.")
        previous = self.recent[-1][1] if self.recent else None
        self.recent.append((now, dict(self.current)))
        while len(self.recent) > 1 and self.recent[1][0] <= now - key.AUTO_SETTLE_SECONDS:
            self.recent.popleft()
        self.previous_read = now
        self.event(event="encoder", phase=self.phase, key=self.selected,
                   position=self.current, torque=self.torque)
        if self.capture and self.phase in ("pressed", "touch"):
            try:
                self.arm.check_pose(self.current)
                key.append_capture(self.capture, self.current, previous_observation=previous)
            except key.SafetyError as exc:
                self.capture_fault(str(exc))
        self.sleep(max(0, key.PERIOD - (self.clock() - start)))

    def capture_fault(self, message):
        self.error = message
        self.transition("capture_error", "Torque OFF. Attempt paused: " + message)

    def fault(self, exc):
        self.error = str(exc) or type(exc).__name__
        if self.controller:
            self.controller.stop()
        self.torque = None  # Last normal read is not proof of torque state after a fault.
        self.feedback_time = None
        self.transition("fault", "Stopped: " + self.error + " Support the arm before requesting release.")

    def reset_capture(self):
        self.capture = self.touch_index = self.draft = self.controller = None
        self.completed_trials = self.test_number = self.accepted_test = 0
        self.error = None
        self.recent.clear()
        self.previous_read = None

    def finish_capture(self):
        hover = self.marker_pose()
        key.append_capture(self.capture, hover, force=True)
        path = list(reversed(self.capture["path"]))
        draft = {**self.capture, "path": path,
                 "touch_index": len(path) - 1 - self.touch_index, "complete": True,
                 "teaching_mode": "chat_release_path", "attempt": self.attempt,
                 "contact_marker": "operator confirmed light contact on release"}
        key.validate_entry(draft, complete=True)
        key.require(key.distance(path[0], path[draft["touch_index"]]) > 0,
                    "No clearance captured above contact; keep lifting gently before clear.")
        for pose in path:
            self.arm.check_pose(pose)
        trial_path = key.plan(draft)
        key.require(key.routine_budget(trial_path, 0.2) <= key.MAX_RUN_TIME,
                    "Path is too long for one trial. Say retry and teach a shorter local lift.")
        self.draft = draft
        atomic_json(self.folder / "drafts" / f"{self.attempt}.json", draft)
        # The saved hover IS the supported pose just captured. No search for an
        # old hover, no wider alignment tolerance, and no invented entry motion.
        self.controller = SessionController(self.arm, trial_path, self.log,
                                            clock=self.clock, sleep=self.sleep,
                                            check_stop=self.check_stop, feedback=self.powered_feedback)
        self.transition("arming", "Hold still and keep supporting. Enabling a hold at the captured clear pose.")
        try:
            self.check_stop()
            self.controller.arm_here()
            self.current = self.controller.tick(self.controller.previous, "hold_verified")
            self.torque = {m: 1 for m in key.MOTORS}
        except BaseException as exc:
            self.fault(exc)
            raise
        self.transition("holding", "HOLDING, torque ON. Gently clear your hands. Say test when ready for one press and release.")

    def save_pass(self):
        key.require(self.phase == "result" and self.test_number > self.accepted_test,
                    "A completed, unreviewed powered trial is required before pass.")
        current_map = key.load_store(self.map_path)
        key.require(key.fingerprint(current_map) == self.map_digest,
                    "Key map changed outside this session. Draft retained; refusing to overwrite it.")
        successful_trials = self.completed_trials + 1
        candidate = {**self.draft, "verification": {
            "method": "human observed powered trial", "at": key.stamp(),
            "successful_trials": successful_trials,
            "claim": "intended key sounded once and released", "session_id": self.session_id}}
        current_map["keys"][self.selected] = candidate
        key.save_store(self.map_path, current_map)
        self.map_digest = key.fingerprint(current_map)
        self.completed_trials = successful_trials
        self.accepted_test = self.test_number
        self.draft = candidate
        self.transition("saved", f"Saved {self.selected}: {self.completed_trials} successful supervised trial(s). "
                        "Say test to repeat, or support the arm with both hands and say next, supported.")

    def dispatch(self, command):
        action = command["action"]
        if action in ("next", "retry", "release", "quit"):
            key.require(command.get("supported") is True,
                        "The human must confirm physical support before torque releases.")
            if action == "next":
                key.require(self.phase == "saved", "Save a successful trial before next; use retry for a failed key.")
            try:
                self.arm.release()
            except BaseException as exc:
                self.fault(exc)
                raise
            self.torque = {m: 0 for m in key.MOTORS}
            if self.controller:
                self.controller.enabled = False
            self.reset_capture()
            if action == "quit":
                self.running = False
                self.transition("closed", "Torque OFF. Keep supporting until the arm is resting safely. Session closed.")
            elif action == "next" and self.selected == key.KEYS[-1]:
                self.transition("finished", "Last key saved; torque OFF. Rest the supported arm safely. All saved results are in the key map.")
            else:
                if action == "next":
                    self.selected = key.KEYS[key.KEYS.index(self.selected) + 1]
                self.transition("ready", f"Torque OFF. Support the arm. For {self.selected}, gently press only until it sounds, hold steady, and say pressed.")
            return
        key.require(self.phase != "fault", "Session fault: only supported release/retry/quit is available.")
        if action == "select":
            key.require(self.phase in ("ready", "finished"), "Finish or retry the current attempt before selecting another key.")
            key.require(command.get("key") in key.KEYS, "Select one of the 12 keys C through B.")
            self.selected = command["key"]
            self.reset_capture()
            self.transition("ready", f"Selected {self.selected}. Torque OFF. Support, press gently just until it sounds, hold steady, and say pressed.")
        elif action == "pressed":
            key.require(self.phase == "ready", "Pressed is accepted only at the start of a key.")
            current = self.marker_pose()
            self.attempt = uuid.uuid4().hex
            self.capture = key.teaching_entry(self.arm, True, self.fixture_note, current)
            self.transition("pressed", f"Captured {self.selected} pressed. Slowly lift until the key is fully released but the pad still barely touches it; hold and say touching.")
        elif action == "touch":
            key.require(self.phase == "pressed", "Capture pressed before touching.")
            current = self.marker_pose()
            key.require(key.distance(self.capture["path"][0], current) > 0, "No release movement measured yet.")
            key.append_capture(self.capture, current, force=True)
            self.touch_index = len(self.capture["path"]) - 1
            self.transition("touch", "Contact captured. Lift slowly to a small visible clearance above this key. Hold still and say clear; the motors will then hold that pose.")
        elif action == "clear":
            key.require(self.phase == "touch", "Capture touching before clear.")
            self.finish_capture()
        elif action == "test":
            key.require(self.phase in ("holding", "result", "saved"), "Test requires the captured hover to be held by the motors.")
            self.transition("testing", f"Testing {self.selected} once. Keep hands clear; physical power stop remains the immediate stop.")
            try:
                self.controller.run(0.2, already_holding=True)
                self.controller.started = None  # Continued hold has no stroke deadline.
                self.current = self.arm.read()
                self.test_number += 1
                self.transition("result", "Returned to hover, torque ON. Say pass only if the intended key sounded once and released; otherwise say fail.")
            except BaseException as exc:
                self.fault(exc)
                raise
        elif action == "pass":
            self.save_pass()
        elif action == "fail":
            key.require(self.phase == "result", "Fail reviews a completed trial.")
            self.transition("failed", "Trial marked unsuccessful; existing key map preserved. Support with both hands and say retry, supported.")
        else:
            raise key.SafetyError("Stop uses the separate stop request; unknown action here.")

    def process_commands(self):
        for path in sorted((self.folder / "commands").glob("*.json")):
            command = json.loads(path.read_text())
            # Consume before action: a restart cannot replay a physical command.
            path.unlink()
            try:
                self.validate_command(command)
                self.dispatch(command)
                self.ack(command, True, self.message)
            except Exception as exc:
                # Invalid/stale cues are recoverable and never widen a limit.
                self.ack(command, False, str(exc))
                print(f"Command rejected: {exc}", flush=True)
            if not self.running:
                break

    def run(self):
        self.publish()
        while self.running:
            try:
                self.observe()
            except (Exception, KeyboardInterrupt) as exc:
                if isinstance(exc, KeyboardInterrupt):
                    raise
                if self.phase != "fault":
                    self.fault(exc)
                self.sleep(0.05)
            self.process_commands()
            if self.clock() >= self.next_publish:
                self.publish()


def serve(args):
    key.require(args.contact_ready, "Fit the pad, fix the jaw opening, and use --contact-ready.")
    key.require(math.isfinite(args.setup_seconds) and 5 <= args.setup_seconds <= 60,
                "Setup countdown must be 5..60 seconds.")
    key.require(sys.stdin.isatty(), "Start serve in an attended terminal; never pipe ready.")
    key.load_store(args.file)
    args.session.mkdir(parents=True, exist_ok=True)
    for name in ("commands", "responses", "drafts", "logs"):
        (args.session / name).mkdir(exist_ok=True)
    with (args.session / "owner.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # Mark an old status inactive before touching hardware. Old command
        # files are rejected by the fresh session ID rather than executed.
        atomic_json(args.session / "status.json", {"running": False, "heartbeat": time.time(),
                    "message": "Starting; awaiting attended support confirmation."})
        arm = key.Arm(args.port, args.calibration)
        session = None
        try:
            arm.open(teaching=True)
            key.require_human(
                "Stop any other robot script. Confirm a fixed padded jaw, unchanged fixture, and accessible motor power stop.\n"
                "Rest the arm securely clear of Orchid. After ready, support it with BOTH hands before the countdown ends.\n"
                "Chat cues: pressed -> touching -> clear (motors HOLD) -> test (one stroke) -> pass/fail.\n"
                "No powered transfers between keys. Chat is not an emergency stop.", "ready")
            for remaining in range(math.ceil(args.setup_seconds), 0, -1):
                print(f"Support the arm: torque releases in {remaining} seconds.", flush=True)
                time.sleep(1)
            arm.release()
            with (args.session / "logs" / f"session-{uuid.uuid4().hex}.jsonl").open("x") as log:
                session = Session(arm, args.session, args.file, args.fixture_note, log)
                print("SESSION READY. Torque OFF. Keep supporting; tell the agent 'session ready'.", flush=True)
                try:
                    session.run()
                finally:
                    if session.controller:
                        session.controller.stop()
                    session.running = False
                    session.message = "Session ended. Torque was NOT automatically released. Support the arm before releasing or switching power off."
                    session.publish()
        finally:
            arm.close()
            print("Connection closed. Support/rest the arm safely; disconnect does not disable torque.", flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", type=Path, default=DEFAULT_SESSION)
    sub = parser.add_subparsers(dest="command", required=True)
    live = sub.add_parser("serve")
    live.add_argument("--port", required=True)
    live.add_argument("--calibration", type=Path, required=True)
    live.add_argument("--file", type=Path, default=ROOT / "orchid_keys.json")
    live.add_argument("--fixture-note", required=True)
    live.add_argument("--contact-ready", action="store_true")
    live.add_argument("--setup-seconds", type=float, default=10)
    sub.add_parser("status")
    send = sub.add_parser("send")
    send.add_argument("action", choices=ACTIONS)
    send.add_argument("--key", choices=key.KEYS)
    send.add_argument("--supported", action="store_true")
    send.add_argument("--wait", type=float, default=2.0, help="seconds to await acknowledgement, not a motion timeout")
    args = parser.parse_args(argv)
    if args.command == "serve":
        serve(args)
    elif args.command == "status":
        status = read_status(args.session, require_live=False)
        status["heartbeat_age_seconds"] = round(time.time() - status["heartbeat"], 2)
        print(json.dumps(status, indent=2))
    else:
        key.require(math.isfinite(args.wait) and 0 <= args.wait <= 60, "Wait must be 0..60 seconds.")
        identifier = send_command(args.session, args.action, args.key, args.supported)
        response = args.session / "responses" / f"{identifier}.json"
        deadline = time.monotonic() + args.wait
        while time.monotonic() < deadline and not response.exists():
            time.sleep(0.05)
        if response.exists():
            result = json.loads(response.read_text())
            print(json.dumps(result, indent=2))
            return 0 if result["ok"] else 1
        print(json.dumps({"id": identifier, "state": "queued", "response": str(response),
                          "message": "Check status/response before sending another command; queued does not mean executed."}))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (Exception, KeyboardInterrupt) as exc:
        print(f"Stopped: {exc or type(exc).__name__}", file=sys.stderr)
        sys.exit(1)

#!/usr/bin/env python
"""Teach the SO101 follower one Orchid key with the leader arm, then play it back.

    python teach_key.py ports                # which USB port is which arm (read-only)
    python teach_key.py sync-calibration     # copy the motors' calibration into LeRobot's files
    python teach_key.py record C             # leader teleop; Enter starts/stops recording
    python teach_key.py play C               # replay; Enter repeats, q quits
    python teach_key.py show C               # summarize a recording, no hardware
    python teach_key.py release              # release follower torque (support the arm first)

This runs LeRobot 0.6.1's own SO101Follower/SO101Leader drivers, so LeRobot's safety
baseline is unchanged: handshake (motor IDs/model/firmware), calibration-vs-EEPROM check,
firmware joint limits, soft position gains (P=16, D=32), gripper torque/current limits,
leader torque off, and max_relative_target-style clipping. It adds only what LeRobot's
teleoperate/record/replay scripts leave out:

* the follower's goal is set to its measured pose before torque turns on (no jump);
* moves to a starting pose (the leader's pose before teleop, the first frame before
  playback) are speed-limited ramps instead of LeRobot's full-speed jump;
* stopping holds the arm where it is and asks before releasing torque, instead of
  dropping it onto the instrument;
* playback refuses a recording made under a different follower calibration.

Nothing here senses contact force or collisions. Keep the motor power switch in reach.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import queue
import sys
import threading
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
KEYS_DIR = ROOT / "keys"
FOLLOWER_ID = "so101_follower"
LEADER_ID = "so101_leader"
# The control loop is shared with the web console (orchid_demo/teach.py) so both move the arm identically.
from orchid_demo.teach import (FOLLOW_CAP, FPS, MIN_RECORDING_S, RAMP_SPEED, SETTLE_S,  # noqa: E402,F401
                               START_TOLERANCE, Session, clip_to_measured, max_gap, step_toward)


class Inbox:
    """Lines typed in the terminal, read on a background thread so control loops never block.
    Closed input (EOF) reads as "q" everywhere and sets `closed`; it is never consent to release torque."""

    def __init__(self, stream=sys.stdin):
        self.lines: queue.Queue[str | None] = queue.Queue()
        self.closed = False
        threading.Thread(target=self._pump, args=(stream,), daemon=True).start()

    def _pump(self, stream):
        for line in stream:
            self.lines.put(line.strip().lower())
        self.lines.put(None)  # EOF

    def _take(self, line):
        if line is None:
            self.closed = True
            return "q"
        return line

    def poll(self) -> list[str]:
        out = []
        while not self.closed:
            try:
                out.append(self._take(self.lines.get_nowait()))
            except queue.Empty:
                break
        return out

    def ask(self, prompt: str) -> str:
        print(prompt, end="", flush=True)
        return "q" if self.closed else self._take(self.lines.get())


# --- small helpers ------------------------------------------------------------------------------


def calibration_digest(calibration: dict) -> str:
    plain = {name: asdict(value) if hasattr(value, "__dataclass_fields__") else dict(value)
             for name, value in calibration.items()}
    return hashlib.sha256(json.dumps(plain, sort_keys=True).encode()).hexdigest()


def key_path(key: str) -> Path:
    if not key.replace("#", "").replace("_", "").isalnum():
        raise SystemExit(f"Key name {key!r}: use letters, digits, '#' or '_' (e.g. C, C#, F_sharp).")
    return KEYS_DIR / f"{key}.json"


# --- devices ------------------------------------------------------------------------------------


def read_follower(robot) -> dict:
    return {k.removesuffix(".pos"): float(v) for k, v in robot.get_observation().items() if k.endswith(".pos")}


def read_leader(teleop) -> dict:
    return {k.removesuffix(".pos"): float(v) for k, v in teleop.get_action().items() if k.endswith(".pos")}


def send(robot, goal: dict) -> None:
    robot.send_action({f"{k}.pos": v for k, v in goal.items()})


def make_follower(port: str):
    from lerobot.robots.so_follower import SO101Follower, SO101FollowerConfig

    # Hold instead of drop if anything (including LeRobot's destructor) disconnects us.
    return SO101Follower(SO101FollowerConfig(port=port, id=FOLLOWER_ID, disable_torque_on_disconnect=False))


def make_leader(port: str):
    from lerobot.teleoperators.so_leader import SO101Leader, SO101LeaderConfig

    return SO101Leader(SO101LeaderConfig(port=port, id=LEADER_ID))


def require_calibrated(device, role: str) -> None:
    if not device.calibration:
        raise SystemExit(f"No LeRobot calibration file for the {role}: {device.calibration_fpath}\n"
                         "Run: python teach_key.py sync-calibration")
    if not device.bus.is_calibrated:
        raise SystemExit(f"The {role} motors' calibration differs from {device.calibration_fpath}.\n"
                         "Nothing was written. If the motors hold the calibration you want (the web app's\n"
                         "latest), run: python teach_key.py sync-calibration")


def claim_port(bus, role: str) -> None:
    """Take the exclusive lock the web console also takes, so two programs can never drive one arm.
    LeRobot opens the port without it; pyserial applies the flock when `exclusive` is set on an open port."""
    try:
        bus.port_handler.ser.exclusive = True
    except Exception as exc:
        bus.disconnect(disable_torque=False)
        raise SystemExit(f"The {role} port {bus.port} is already in use by another program (the web app, or another "
                         f"teach_key). Stop that program first; nothing was sent to the motors. ({exc})") from exc


def connect_follower(port: str, inbox: Inbox, robot=None):
    """SO101Follower.connect(), except it refuses (never prompts) on a calibration mismatch and
    seeds Goal_Position with the measured pose before configure() re-enables torque."""
    robot = robot or make_follower(port)
    robot.bus.connect()  # LeRobot handshake: IDs 1-6, model 777, matching firmware
    claim_port(robot.bus, "follower")
    try:
        require_calibrated(robot, "follower")
        torque = robot.bus.sync_read("Torque_Enable", normalize=False, num_retry=robot.config.num_read_retries)
        if any(torque.values()):
            # configure() turns torque off for ~0.1 s while it writes the gains, so the arm can sag.
            answer = inbox.ask("Follower torque is already ON (another program left it holding).\n"
                               "Support the follower with a hand, then press Enter (q to cancel): ")
            if answer == "q":
                raise SystemExit("Cancelled; follower left as it was.")
        present = robot.bus.sync_read("Present_Position", num_retry=robot.config.num_read_retries)
        robot.bus.sync_write("Goal_Position", present)
        robot.configure()  # LeRobot gains/limits; re-enables torque, holding the seeded pose
    except BaseException:
        robot.bus.disconnect(disable_torque=False)
        raise
    return robot


def connect_leader(port: str, teleop=None):
    teleop = teleop or make_leader(port)
    # SO101Leader.connect(calibrate=False), with the port lock taken before any write; never prompts.
    teleop.bus.connect()
    claim_port(teleop.bus, "leader")
    try:
        require_calibrated(teleop, "leader")
        teleop.configure()  # leader torque off
    except BaseException:
        teleop.disconnect()
        raise
    return teleop


def hold_here(robot) -> None:
    send(robot, read_follower(robot))


def finish(robot, inbox: Inbox) -> None:
    """Hold at the measured pose, then release torque only when the operator says so."""
    try:
        hold_here(robot)
        print("Follower HOLDING where it is.")
    except Exception as exc:  # the bus may be what failed
        print(f"Could not command a hold ({exc}). The last target may still be active; use the power switch.")
    try:
        answer = inbox.ask("Support the follower (or make sure it is resting), then press Enter to release "
                           "torque.\nType 'hold' + Enter to exit leaving it powered: ")
    except KeyboardInterrupt:
        answer = "hold"
    # Only a typed Enter releases torque. A closed terminal (EOF) or a second Ctrl+C keeps it holding.
    release = answer != "hold" and not getattr(inbox, "closed", False)
    try:
        robot.bus.disconnect(disable_torque=release)
        print("Torque released." if release else
              "Exited with the follower HOLDING. Later, support it and run: python teach_key.py release")
    except Exception as exc:
        print(f"Disconnect failed ({exc}). Support the arm and switch off motor power.")


def resolve_ports(args, need_leader: bool) -> tuple[str, str | None]:
    follower, leader = args.follower_port, getattr(args, "leader_port", None)
    if follower and (leader or not need_leader):
        return follower, leader
    from orchid_demo.discovery import discover_arms

    result = discover_arms()
    for warning in result["warnings"]:
        print(warning, file=sys.stderr)
    if result["warnings"]:
        print("A busy port usually means the web app (app.py) or another arm script is still running. "
              "Stop it first.", file=sys.stderr)
    by_role = {}
    for arm in result["arms"]:
        missing = sorted(set(range(1, 7)) - set(arm["motor_ids"]))
        if missing:
            problem = (f"{arm['port']} ({arm['role']}, {arm['voltage']} V): motor IDs {missing} not responding. "
                       "Check that motor's cable and the arm's power supply.")
            if arm["role"] == "follower" or (need_leader and arm["role"] == "leader"):
                raise SystemExit(problem)
            print(problem + " Not needed for this command.", file=sys.stderr)
            continue
        by_role[arm["role"]] = arm["port"]
    follower = follower or by_role.get("follower")
    leader = leader or by_role.get("leader")
    if not follower:
        raise SystemExit("No 12 V follower found. Check USB and the follower's power supply, or pass --follower-port.")
    if need_leader and not leader:
        raise SystemExit("No 5 V leader found. Check its USB cable, or pass --leader-port.")
    return follower, leader


# --- teaching -----------------------------------------------------------------------------------


def session_for(follower, leader=None, *, lock_gripper=False, clock=time.perf_counter):
    return Session(lambda: read_follower(follower), lambda goal: send(follower, goal),
                   (lambda: read_leader(leader)) if leader is not None else None,
                   lock_gripper=lock_gripper, clock=clock, ramp_speed=RAMP_SPEED, follow_cap=FOLLOW_CAP,
                   start_tolerance=START_TOLERANCE, settle_s=SETTLE_S)


def teleop(follower, leader, inbox: Inbox, *, lock_gripper=False, on_recording=lambda frames: None,
           clock=time.perf_counter, sleep=time.sleep) -> None:
    """Leader-follower teleop. Ramp to the leader's pose first, then follow 1:1 (LeRobot mapping).
    Enter toggles recording; each finished recording is handed to on_recording. q quits."""
    period = 1.0 / FPS
    session = session_for(follower, leader, lock_gripper=lock_gripper, clock=clock)
    session.follow()
    print(f"Moving the follower to the leader's pose at up to {RAMP_SPEED:.0f} deg/s. "
          "Hold the leader still, roughly matching the follower.")
    while True:
        tick = clock()
        if session.tick() == "aligned":
            print("FOLLOWING the leader. Put the arm at its rest pose, then press Enter to start recording.")
        for line in inbox.poll():
            if line == "q":
                if session.frames:
                    print("Discarding the recording that was still running (press Enter to stop one before q).")
                return
            if session.mode != "following":
                print("Still moving to the leader's pose; wait for FOLLOWING.")
            elif session.frames is None:
                session.start_recording()
                print("RECORDING: rest -> above the key -> press until it sounds -> lift -> rest. "
                      "Press Enter to stop.")
            else:
                try:
                    done = session.stop_recording()
                except RuntimeError:
                    print("Too short; not saved. Press Enter to record again.")
                else:
                    on_recording(done)
                    print("Keep following. Enter = record again (replaces it), q = finish.")
        sleep(max(0.0, period - (clock() - tick)))


def save_recording(key: str, frames: list, follower, *, lock_gripper: bool) -> Path:
    import lerobot

    path = key_path(key)
    KEYS_DIR.mkdir(exist_ok=True)
    if path.exists():
        path.replace(path.with_suffix(".prev.json"))
    data = {
        "key": key,
        "created": datetime.now(timezone.utc).isoformat(),
        "fps": FPS,
        "units": "degrees; gripper 0-100 (LeRobot SO101 use_degrees=True)",
        "lerobot_version": getattr(lerobot, "__version__", "unknown"),
        "follower_id": FOLLOWER_ID,
        "follower_calibration_sha256": calibration_digest(follower.calibration),
        "lock_gripper": lock_gripper,
        "frames": frames,
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1))
    tmp.replace(path)
    print(f"Saved {path}: {len(frames)} frames, {frames[-1]['t']:.1f} s.")
    return path


def load_recording(key: str) -> dict:
    path = key_path(key)
    if not path.exists():
        raise SystemExit(f"No recording for {key}. Run: python teach_key.py record {key}")
    data = json.loads(path.read_text())
    if not data.get("frames"):
        raise SystemExit(f"{path} has no frames.")
    return data


# --- playback -----------------------------------------------------------------------------------


def replay(robot, recording: dict, inbox: Inbox, *, speed=1.0, clock=time.perf_counter,
           sleep=time.sleep) -> bool:
    """Ramp to the first frame, then stream the taught goals at the taught timing (/speed)."""
    period = 1.0 / FPS
    frames = recording["frames"]
    session = session_for(robot, clock=clock)
    session.play(recording, speed)
    print(f"Moving to the taught start pose at up to {RAMP_SPEED:.0f} deg/s.")
    while True:
        tick = clock()
        event = session.tick()
        if event == "start_mismatch":
            if inbox.ask(session.warning.replace("Clear it, or play anyway.", "") +
                         "\nEnter = play anyway, q = cancel: ") == "q":
                return False
            session.play(recording, speed, force=True)
        elif event == "playing":
            inbox.poll()  # drop stale keypresses
            print(f"PLAYING {recording['key']} ({frames[-1]['t'] / speed:.1f} s). Ctrl+C or s+Enter stops.")
        elif event == "played":
            break
        if session.mode == "playing" and any(line in ("s", "q") for line in inbox.poll()):
            session.hold()
            print("Stopped; holding here.")
            return False
        sleep(max(0.0, period - (clock() - tick)))
    if session.clipped_steps:
        print(f"Note: {session.clipped_steps} steps were limited to {FOLLOW_CAP:.0f} deg from the measured pose "
              "(the arm lagged or was blocked).")
    print("Done; holding the final pose.")
    return True


# --- commands -----------------------------------------------------------------------------------


def cmd_ports(args) -> None:
    from orchid_demo.discovery import discover_arms

    result = discover_arms()
    for warning in result["warnings"]:
        print(warning)
    for arm in result["arms"]:
        print(f"{arm['port']}: {arm['role'] or 'unknown':<8} {arm['voltage']} V  motors {arm['motor_ids']}")
    if not result["arms"]:
        print("No arms found.")


def validate_eeprom_calibration(calibration: dict, role: str) -> None:
    problems = []
    for name, cal in calibration.items():
        if name != "wrist_roll" and (cal.range_min >= cal.range_max or (cal.range_min, cal.range_max) == (0, 4095)):
            problems.append(f"{name}: range {cal.range_min}..{cal.range_max}")
        if not -2047 <= cal.homing_offset <= 2047:
            problems.append(f"{name}: homing offset {cal.homing_offset}")
    if problems:
        raise SystemExit(f"The {role}'s motors do not hold a finished calibration ({'; '.join(problems)}).\n"
                         "That looks like an interrupted calibration. Calibrate it (lerobot-calibrate or the web app) "
                         "before teaching. Nothing was written.")


def sync_calibration(device, role: str) -> None:
    """Copy calibration from motor EEPROM (read-only on the motors) into LeRobot's JSON file."""
    device.bus.connect()
    claim_port(device.bus, role)
    try:
        actual = device.bus.read_calibration()
    finally:
        device.bus.disconnect(disable_torque=False)
    validate_eeprom_calibration(actual, role)
    if device.calibration == actual:
        print(f"{role}: {device.calibration_fpath} already matches the motors.")
        return
    if device.calibration_fpath.exists():
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = device.calibration_fpath.with_name(f"{device.calibration_fpath.stem}.before-{stamp}.json")
        device.calibration_fpath.replace(backup)
        print(f"{role}: backed up the old file to {backup}")
    for name, cal in actual.items():
        old = device.calibration.get(name)
        if old != cal:
            print(f"  {name}: offset {getattr(old, 'homing_offset', '-')} -> {cal.homing_offset}, "
                  f"range {getattr(old, 'range_min', '-')}..{getattr(old, 'range_max', '-')} -> "
                  f"{cal.range_min}..{cal.range_max}")
    device.calibration = actual
    device._save_calibration()
    print(f"{role}: wrote {device.calibration_fpath}")


def cmd_sync_calibration(args) -> None:
    follower, leader = resolve_ports(args, need_leader=False)
    sync_calibration(make_follower(follower), "follower")
    if leader:
        sync_calibration(make_leader(leader), "leader")
    print("Recordings made before a calibration change are refused by play; re-record them.")


def cmd_record(args) -> None:
    path = key_path(args.key)
    follower_port, leader_port = resolve_ports(args, need_leader=True)
    inbox = Inbox()
    leader = connect_leader(leader_port)
    try:
        follower = connect_follower(follower_port, inbox)
    except BaseException:
        leader.disconnect()
        raise
    try:
        if inbox.ask("Follower is holding still. Next it moves slowly to match the leader, then follows it.\n"
                     "Hands off the follower, then press Enter to start (q to quit): ") == "q":
            return
        teleop(follower, leader, inbox, lock_gripper=args.lock_gripper,
               on_recording=lambda frames: save_recording(args.key, frames, follower, lock_gripper=args.lock_gripper))
    except KeyboardInterrupt:
        print()
    finally:
        finish(follower, inbox)
        leader.disconnect()
    if path.exists():
        print(f"Next: python teach_key.py play {args.key} --speed 0.5")


def cmd_play(args) -> None:
    recording = load_recording(args.key)
    if not 0.1 <= args.speed <= 1.0:
        raise SystemExit("--speed must be between 0.1 and 1.0")
    follower_port, _ = resolve_ports(args, need_leader=False)
    robot = make_follower(follower_port)
    if calibration_digest(robot.calibration) != recording["follower_calibration_sha256"]:
        raise SystemExit(f"{args.key} was recorded under a different follower calibration. "
                         f"Re-record it: python teach_key.py record {args.key}")
    inbox = Inbox()
    connect_follower(follower_port, inbox, robot=robot)
    try:
        while True:
            replay(robot, recording, inbox, speed=args.speed)
            if inbox.ask("Enter = play again, q = quit: ") == "q":
                break
    except KeyboardInterrupt:
        print()
    finally:
        finish(robot, inbox)


def cmd_show(args) -> None:
    data = load_recording(args.key)
    frames = data["frames"]
    print(f"{data['key']}: {len(frames)} frames, {frames[-1]['t']:.1f} s, recorded {data['created']}")
    for name in frames[0]["goal"]:
        values = [f["goal"][name] for f in frames]
        print(f"  {name:<14} start {values[0]:7.1f}  min {min(values):7.1f}  max {max(values):7.1f}  "
              f"end {values[-1]:7.1f}")
    try:
        current = calibration_digest(make_follower("unused").calibration)
        same = current == data["follower_calibration_sha256"]
        print("  calibration: " + ("matches the current follower file" if same else "DIFFERENT; re-record"))
    except ImportError:
        pass


def cmd_release(args) -> None:
    follower_port, _ = resolve_ports(args, need_leader=False)
    robot = make_follower(follower_port)
    robot.bus.connect()
    claim_port(robot.bus, "follower")
    inbox = Inbox()
    answer = inbox.ask("Support the follower with a hand, then press Enter to release torque (q to cancel): ")
    robot.bus.disconnect(disable_torque=answer != "q")
    print("Torque released." if answer != "q" else "Left as it was.")


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    for name, needs_key in (("ports", False), ("sync-calibration", False), ("record", True), ("play", True),
                            ("show", True), ("release", False)):
        p = sub.add_parser(name)
        if needs_key:
            p.add_argument("key", help="key name, e.g. C")
        p.add_argument("--follower-port", help="default: auto-detect the 12 V arm")
        if name in ("record", "sync-calibration"):
            p.add_argument("--leader-port", help="default: auto-detect the 5 V arm")
        if name == "record":
            p.add_argument("--lock-gripper", action="store_true",
                           help="keep the follower gripper at its starting opening instead of following the leader")
        if name == "play":
            p.add_argument("--speed", type=float, default=1.0, help="playback speed, 0.1-1.0 (try 0.5 first)")
    args = parser.parse_args(argv)
    {"ports": cmd_ports, "sync-calibration": cmd_sync_calibration, "record": cmd_record, "play": cmd_play,
     "show": cmd_show, "release": cmd_release}[args.command](args)


if __name__ == "__main__":
    main()

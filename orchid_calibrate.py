#!/usr/bin/env python3
"""Attended full SO101 calibration without robot.connect()/torque enable.

Uses the installed LeRobot calibration routine. Only the motor bus is connected;
the usual robot.configure() step is never called. The agent can relay the human's
pose confirmations through this program's PTY. Do not fabricate confirmations.
"""

from dataclasses import asdict
import argparse
from pathlib import Path
import sys
import time
import uuid

import orchid_key as key
from orchid_session import atomic_json


class CalibrationRun:
    def __init__(self, robot, backup_dir, clock=time.monotonic, sleep=time.sleep):
        self.robot, self.bus = robot, robot.bus
        self.folder = backup_dir
        self.clock, self.sleep = clock, sleep
        self.stage = "connecting"
        self.original_sync_read = self.bus.sync_read
        self.original_homing = self.bus.set_half_turn_homings
        self.original_ranges = self.bus.record_ranges_of_motion
        self.original_save = robot._save_calibration
        self.old_calibration = None
        self.old_file = robot.calibration_fpath.read_bytes()
        self.old_locks = None
        self.homing_changed = False
        self.committed = False
        self.mins, self.maxes = {}, {}
        self.last_position = None
        self.next_report = 0

    def off(self):
        flags = self.original_sync_read("Torque_Enable", normalize=False, num_retry=0)
        key.require(set(flags) == set(key.MOTORS) and all(type(v) is int and v == 0 for v in flags.values()),
                    f"Calibration requires all six motors OFF; readback: {flags}")
        return flags

    def report(self, **extra):
        atomic_json(self.folder / "status.json", {
            "stage": self.stage, "heartbeat": time.time(),
            "position": self.last_position, "mins": self.mins, "maxes": self.maxes,
            "saved": self.committed, **extra})

    def checked_read(self, register, *args, **kwargs):
        if register != "Present_Position":
            return self.original_sync_read(register, *args, **kwargs)
        start = self.clock()
        self.off()
        kwargs["num_retry"] = 0
        values = self.original_sync_read(register, *args, **kwargs)
        self.off()
        key.require(self.clock() - start <= key.MAX_IO_TIME, "Stale calibration feedback; stopped.")
        key.require(values and all(type(v) is int and 0 <= v <= 4095 for v in values.values()),
                    "Encoder reading outside 0..4095; inspect midpoint/encoder wrap before retrying.")
        self.last_position = dict(values)
        if self.stage == "ranges":
            key.require(set(values) == set(key.MOTORS) - {"wrist_roll"}, "Incomplete range feedback.")
            for motor, value in values.items():
                self.mins[motor] = min(value, self.mins.get(motor, value))
                self.maxes[motor] = max(value, self.maxes.get(motor, value))
        if self.clock() >= self.next_report:
            self.report()
            self.next_report = self.clock() + 0.25
        return values

    def home(self, *args, **kwargs):
        self.stage = "centering"
        samples = [self.checked_read("Present_Position", normalize=False)]
        for _ in range(2):
            self.sleep(0.1)
            samples.append(self.checked_read("Present_Position", normalize=False))
        for sample in samples:
            key.pose_valid(sample)
        key.require(all(max(p[m] for p in samples) - min(p[m] for p in samples) <= 2 for m in key.MOTORS),
                    "Arm moved during midpoint capture; no new homing offsets written.")
        self.homing_changed = True  # Also covers partially successful writes.
        result = self.original_homing(*args, **kwargs)
        self.off()
        centered = self.checked_read("Present_Position", normalize=False)
        key.pose_valid(centered)
        key.require(all(abs(v - 2047) <= 3 for v in centered.values()),
                    "Could not verify all six centered encoders; hold still during capture.")
        self.report(homing_offsets=result)
        return result

    def ranges(self, motors, display_values=True):
        self.stage = "ranges"
        self.report()
        print("Live ranges are being recorded. The agent can read status.json. Keep supporting the arm; torque stays OFF.", flush=True)
        mins, maxes = self.original_ranges(motors, display_values=False)
        expected = set(key.MOTORS) - {"wrist_roll"}
        key.require(set(mins) == set(maxes) == expected, "Incomplete range recording.")
        for motor in expected:
            key.require(0 <= mins[motor] < maxes[motor] <= 4095 and
                        maxes[motor] - mins[motor] > 2 * key.JOINT_MARGIN,
                        f"No usable range recorded for {motor}; repeat the full calibration.")
        self.off()
        self.stage = "writing"
        self.report()
        return mins, maxes

    def save(self, *args, **kwargs):
        self.off()
        actual = self.bus.read_calibration()
        key.require(actual == self.robot.calibration, "Hardware calibration readback failed; file not saved.")
        key.require(self.robot.calibration_fpath.read_bytes() == self.old_file,
                    "Calibration file changed in another process; refusing to overwrite it.")
        data = {motor: asdict(value) for motor, value in actual.items()}
        atomic_json(self.folder / "new-calibration.json", data)
        atomic_json(self.robot.calibration_fpath, data)
        self.committed = True

    def run(self):
        self.folder.mkdir(parents=True, exist_ok=False)
        (self.folder / "previous-calibration.json").write_bytes(self.old_file)
        self.report()
        try:
            self.bus.connect()  # Intentionally never robot.connect()/configure().
            volts = self.original_sync_read("Present_Voltage", normalize=False, num_retry=0)
            key.require(set(volts) == set(key.MOTORS) and all(v >= 80 for v in volts.values()),
                        "Expected six powered follower motors; refusing missing/leader/undervoltage readings.")
            key.print_torque(self.off())
            modes = self.original_sync_read("Operating_Mode", normalize=False, num_retry=0)
            key.require(set(modes) == set(key.MOTORS) and all(v == 0 for v in modes.values()),
                        "All motors must already be in position mode.")
            self.old_calibration = self.bus.read_calibration()
            self.old_locks = self.original_sync_read("Lock", normalize=False, num_retry=0)
            key.require(set(self.old_locks) == set(key.MOTORS), "Incomplete EEPROM-lock readback.")
            atomic_json(self.folder / "previous-hardware.json", {
                "calibration": {m: asdict(c) for m, c in self.old_calibration.items()},
                "locks": self.old_locks})
            self.bus.sync_read = self.checked_read
            self.bus.set_half_turn_homings = self.home
            self.bus.record_ranges_of_motion = self.ranges
            self.robot._save_calibration = self.save
            self.stage = "waiting_for_calibration"
            self.report()
            print(f"Backup: {self.folder}\nAll six motors verified OFF. No position goals will be sent.", flush=True)
            self.robot.calibrate()
            key.require(self.committed, "Fresh calibration was not completed; saved file unchanged.")
            self.off()
            self.stage = "complete"
            self.report()
            print("FULL CALIBRATION VERIFIED AND SAVED. Torque remains OFF. Existing Orchid key motions need re-teaching.", flush=True)
        except BaseException as exc:
            self.stage = "stopped"
            rollback = "No homing changes made."
            if self.homing_changed and not self.committed and self.old_calibration is not None:
                try:
                    self.off()
                    self.bus.write_calibration(self.old_calibration)
                    key.require(self.bus.read_calibration() == self.old_calibration, "Rollback readback mismatch.")
                    rollback = "Previous hardware calibration restored; saved file unchanged."
                except Exception as failure:
                    rollback = f"Could not verify restoration: {failure}. Do not run playback; finish calibration after restoring communication."
            self.report(error=str(exc) or type(exc).__name__, recovery=rollback)
            print(rollback, flush=True)
            raise
        finally:
            try:
                if self.bus.is_connected and self.old_locks is not None:
                    self.off()
                    for motor, value in self.old_locks.items():
                        self.bus.write("Lock", motor, value, normalize=False, num_retry=0)
            finally:
                if self.bus.is_connected:
                    self.bus.disconnect(disable_torque=False)
                self.bus.sync_read = self.original_sync_read
                self.bus.set_half_turn_homings = self.original_homing
                self.bus.record_ranges_of_motion = self.original_ranges
                self.robot._save_calibration = self.original_save


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", required=True)
    parser.add_argument("--calibration", type=Path, required=True)
    parser.add_argument("--backup-dir", type=Path, default=Path(__file__).with_name("orchid_runs") / f"full-calibration-{uuid.uuid4().hex}")
    args = parser.parse_args()
    key.require(sys.stdin.isatty(), "Full calibration requires an attended PTY.")
    from lerobot.robots.so_follower import SO101Follower, SO101FollowerConfig
    config = SO101FollowerConfig(port=args.port, id=args.calibration.stem,
                                calibration_dir=args.calibration.parent,
                                disable_torque_on_disconnect=False)
    CalibrationRun(SO101Follower(config), args.backup_dir).run()


if __name__ == "__main__":
    try:
        main()
    except (Exception, KeyboardInterrupt) as exc:
        print(f"Stopped: {exc or type(exc).__name__}", file=sys.stderr)
        sys.exit(1)

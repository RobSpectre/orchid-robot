"""Read-only SO100/SO101 discovery shared by the console and find_ports.py."""
from __future__ import annotations

import glob
from pathlib import Path
import time

FOLLOWER_MIN_VOLTAGE = 8.0
MAX_MOTOR_ID = 20
EXPECTED_MOTOR_IDS = list(range(1, 7))


# USB arm adapters only; never the host's built-in ttyS ports. Linux: ttyACM/ttyUSB. macOS: the tty.* call-in
# devices LeRobot uses (one per adapter; the matching cu.* device is the same port), including WCH's CH34x driver.
SERIAL_PATTERNS = ("/dev/ttyACM*", "/dev/ttyUSB*", "/dev/tty.usbmodem*", "/dev/tty.usbserial*", "/dev/tty.wchusbserial*")


def serial_candidates():
    return sorted({path for pattern in SERIAL_PATTERNS for path in glob.glob(pattern)})


def probe(port: str, *, guard=lambda: None) -> dict | None:
    import scservo_sdk as scs
    import serial

    class ExclusivePort(scs.PortHandler):
        def setupPort(self, _):
            # Acquire pyserial's lock before buffer resets or packets. The SDK's
            # default opener cannot request exclusivity until after opening.
            self.ser = serial.Serial(port=self.port_name, baudrate=self.baudrate,
                                     timeout=0, write_timeout=0.2, exclusive=True)
            self.is_open = True
            self.tx_time_per_byte = 10_000 / self.baudrate
            self.ser.reset_input_buffer()
            return True

        def getCurrentTime(self):
            # Keep the SDK's packet deadlines independent of clock adjustments.
            return time.monotonic() * 1000

    handler = ExclusivePort(port)
    packet = scs.PacketHandler(0)
    guard()
    try:
        if not handler.setBaudRate(1_000_000):
            return None
        ids = []
        for motor_id in range(1, MAX_MOTOR_ID + 1):
            guard()
            if packet.ping(handler, motor_id)[1] == scs.COMM_SUCCESS:
                ids.append(motor_id)
        if not ids:
            return None
        guard()
        raw, result, error = packet.read1ByteTxRx(handler, ids[0], 62)  # Present_Voltage, 0.1 V
        voltage = raw / 10.0 if result == scs.COMM_SUCCESS and not error and 0 < raw <= 255 else None
        role = None if voltage is None else ("follower" if voltage >= FOLLOWER_MIN_VOLTAGE else "leader")
        limits = {}
        if ids == EXPECTED_MOTOR_IDS:
            # Calibration writes each motor's range into its position limits, so they identify the physical arm.
            for motor_id in ids:
                guard()
                low, result_low, error_low = packet.read2ByteTxRx(handler, motor_id, 9)  # Min_Position_Limit
                high, result_high, error_high = packet.read2ByteTxRx(handler, motor_id, 11)  # Max_Position_Limit
                if result_low != scs.COMM_SUCCESS or result_high != scs.COMM_SUCCESS or error_low or error_high:
                    limits = {}
                    break
                limits[str(motor_id)] = [low, high]
        return {"port": port, "motor_ids": ids, "voltage": voltage, "role": role, "limits": limits or None,
                "voltage_motor_id": ids[0], "sampled_at": time.time()}
    finally:
        if handler.ser is not None:
            handler.closePort()


def discover_arms(*, guard=lambda: None, exclude_ports=()):
    # Fail clearly even when no USB ports exist and hardware dependencies are absent.
    import scservo_sdk  # noqa: F401
    import serial  # noqa: F401

    arms, warnings = [], []
    excluded = {Path(port).resolve() for port in exclude_ports}
    for port in serial_candidates():
        if Path(port).resolve() in excluded:
            continue
        guard()
        try:
            info = probe(port, guard=guard)
        except OSError as exc:
            warnings.append(f"{port}: unavailable (busy, unplugged, or permission denied): {exc}")
            continue
        if info:
            arms.append(info)
    return {"arms": arms, "warnings": warnings}


def matches(calibration, limits):
    """Whether a scanned arm's position limits are those of this saved calibration (read-only identity)."""
    return bool(calibration and limits) and all(
        limits.get(str(c["id"])) == [c["range_min"], c["range_max"]] for c in calibration.values())


def follower_problem(arm):
    if arm["motor_ids"] != EXPECTED_MOTOR_IDS:
        return "Needs motor IDs 1–6. Check motor connections and power."
    if arm["voltage"] is None:
        return "Voltage unavailable. Refresh before connecting."
    if arm["role"] != "follower":
        return "Leader / low-voltage bus. Select the 12 V follower."
    return None


def leader_problem(arm):
    if arm["motor_ids"] != EXPECTED_MOTOR_IDS:
        return "Leader needs motor IDs 1–6."
    if arm["voltage"] is None or not 4 <= arm["voltage"] < FOLLOWER_MIN_VOLTAGE or arm["role"] != "leader":
        return "Select a powered low-voltage leader, not the 12 V follower."
    return None

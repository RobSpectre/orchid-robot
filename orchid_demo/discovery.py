"""Read-only SO100/SO101 discovery shared by the console and find_ports.py."""
from __future__ import annotations

import glob
import time

FOLLOWER_MIN_VOLTAGE = 8.0
MAX_MOTOR_ID = 20
EXPECTED_MOTOR_IDS = list(range(1, 7))


def serial_candidates():
    # Do not probe the host's built-in ttyS ports. These are USB arm adapters.
    return sorted({path for pattern in ("/dev/ttyACM*", "/dev/ttyUSB*", "/dev/tty.usbmodem*", "/dev/tty.usbserial*")
                   for path in glob.glob(pattern)})


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
        return {"port": port, "motor_ids": ids, "voltage": voltage, "role": role,
                "voltage_motor_id": ids[0], "sampled_at": time.time()}
    finally:
        if handler.ser is not None:
            handler.closePort()


def discover_arms(*, guard=lambda: None):
    # Fail clearly even when no USB ports exist and hardware dependencies are absent.
    import scservo_sdk  # noqa: F401
    import serial  # noqa: F401

    arms, warnings = [], []
    for port in serial_candidates():
        guard()
        try:
            info = probe(port, guard=guard)
        except OSError as exc:
            warnings.append(f"{port}: unavailable (busy, unplugged, or permission denied): {exc}")
            continue
        if info:
            arms.append(info)
    return {"arms": arms, "warnings": warnings}


def follower_problem(arm):
    if arm["motor_ids"] != EXPECTED_MOTOR_IDS:
        return "Needs motor IDs 1–6. Check motor connections and power."
    if arm["voltage"] is None:
        return "Voltage unavailable. Refresh before connecting."
    if arm["role"] != "follower":
        return "Leader / low-voltage bus. Select the 12 V follower."
    return None

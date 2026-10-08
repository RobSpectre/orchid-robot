#!/usr/bin/env python
"""Preview or repair voltage limits on confirmed 7.4 V SO101 leader motors.

Motor labels must say 7.4 V; model number 777 alone cannot establish this.
The repair uses 5.0–5.5 V protection limits for a 5 V leader
supply. These are deliberately chosen limits, not a factory reset.

    python repair_leader_voltage.py --port /dev/ttyACM0 --motor-ids 6
    python repair_leader_voltage.py --port /dev/ttyACM0 --motor-ids 6 --apply
    python repair_leader_voltage.py --port /dev/ttyACM0 --motor-ids 1 2 3 4 5 6 --min-volts 4.5 --max-volts 8.0

Applying saves the original settings to JSON, disables torque, writes and
verifies the limits, then locks EEPROM. Support the arm before applying;
torque stays off. No position commands are sent.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time
import uuid

MIN_VOLTAGE = 50  # 0.1 V units
MAX_VOLTAGE = 55


def repair(port: str, motor_ids: list[int], *, apply: bool = False, backup: Path | None = None,
           min_voltage: int = MIN_VOLTAGE, max_voltage: int = MAX_VOLTAGE) -> None:
    import scservo_sdk as scs

    if not motor_ids or len(set(motor_ids)) != len(motor_ids) or any(i not in range(1, 7) for i in motor_ids):
        raise ValueError("Select unique SO101 motor IDs between 1 and 6.")

    handler = scs.PortHandler(port)
    packet = scs.PacketHandler(0)
    limits_text = f"{min_voltage / 10:.1f}–{max_voltage / 10:.1f} V"
    if not handler.openPort():
        raise RuntimeError(f"Cannot open {port}")
    # PacketHandler resets its timeout for each request. Allow USB/EEPROM
    # latency beyond the SDK's short default timeout.
    handler.setPacketTimeout = lambda packet_length: handler.setPacketTimeoutMillis(200)

    def check(comm: int, error: int, context: str) -> None:
        if comm != scs.COMM_SUCCESS:
            raise RuntimeError(f"{context}: {packet.getTxRxResult(comm)}")
        # The known voltage fault can remain set until the limits are fixed.
        # Any additional motor fault must stop the repair.
        if error & ~0x01:
            raise RuntimeError(f"{context}: unexpected motor error 0x{error:02x}")

    def read(motor_id: int, address: int) -> int:
        value, comm, error = packet.read1ByteTxRx(handler, motor_id, address)
        check(comm, error, f"Motor {motor_id}, read address {address}")
        return value

    def write(motor_id: int, address: int, value: int) -> None:
        if read(motor_id, address) == value:
            return
        comm, error = packet.write1ByteTxRx(handler, motor_id, address, value)
        if comm != scs.COMM_RX_TIMEOUT:
            check(comm, error, f"Motor {motor_id}, write address {address}")
        # Let the servo commit the setting. Retry reads, without resending the
        # write, if its first replies still report the previous value. A lost
        # write acknowledgement is accepted only when readback confirms it.
        for _ in range(8):
            time.sleep(0.025)
            actual, read_comm, read_error = packet.read1ByteTxRx(handler, motor_id, address)
            if read_comm == scs.COMM_RX_TIMEOUT:
                continue
            check(read_comm, read_error, f"Motor {motor_id}, verify address {address}")
            if actual == value:
                return
        raise RuntimeError(f"Motor {motor_id}: address {address} expected {value}, read {actual}")

    try:
        # Preflight every selected motor before any motor settings are changed.
        original = {}
        for motor_id in motor_ids:
            model, comm, error = packet.ping(handler, motor_id)
            check(comm, error, f"Motor {motor_id}, ping")
            if model != 777:
                raise RuntimeError(f"Motor {motor_id}: expected STS3215 model 777, got {model}")
            original[motor_id] = {
                "model_number": model,
                "present_voltage": read(motor_id, 62),
                "minimum_voltage": read(motor_id, 15),
                "maximum_voltage": read(motor_id, 14),
                "torque_enable": read(motor_id, 40),
                "lock": read(motor_id, 55),
            }
            state = original[motor_id]
            print(
                f"id={motor_id}: actual={state['present_voltage'] / 10:.1f} V, "
                f"limits={state['minimum_voltage'] / 10:.1f}–{state['maximum_voltage'] / 10:.1f} V "
                f"-> {limits_text}"
            )
            if apply and not min_voltage <= state["present_voltage"] <= max_voltage:
                raise RuntimeError(f"Motor {motor_id}: measured voltage must be within {limits_text}")

        if not apply:
            print("Preview only. Use --apply after confirming every selected motor label says 7.4 V.")
            return

        backup = backup or Path(f"leader-voltage-before-{uuid.uuid4().hex}.json")
        with backup.open("x") as output:
            json.dump(
                {"port": port, "saved_at": datetime.now(timezone.utc).isoformat(), "motors": original},
                output,
                indent=2,
            )
            output.write("\n")
        print(f"Original settings saved to {backup.resolve()}", flush=True)

        # Clearing the fault can allow torque to resume. Disable it on all
        # selected motors before correcting any voltage limits.
        for motor_id in motor_ids:
            write(motor_id, 40, 0)

        for motor_id in motor_ids:
            if (read(motor_id, 15), read(motor_id, 14)) == (min_voltage, max_voltage):
                write(motor_id, 55, 1)
                continue
            try:
                write(motor_id, 55, 0)
                write(motor_id, 15, min_voltage)
                write(motor_id, 14, max_voltage)
            except BaseException as original_error:
                try:
                    write(motor_id, 55, 1)
                except Exception as lock_error:
                    raise RuntimeError(f"{original_error}; EEPROM relock also failed: {lock_error}") from original_error
                raise
            else:
                write(motor_id, 55, 1)

        for motor_id in motor_ids:
            final_values = tuple(read(motor_id, address) for address in (15, 14, 40, 55))
            if final_values != (min_voltage, max_voltage, 0, 1):
                raise RuntimeError(f"Motor {motor_id}: final limits, torque, or lock failed verification")
            model, comm, error = packet.ping(handler, motor_id)
            check(comm, error, f"Motor {motor_id}, final ping")
            print(f"id={motor_id}: model={model} error=0x{error:02x} limits={limits_text} torque=off locked=yes")
            if error or model != 777:
                raise RuntimeError(f"Motor {motor_id}: limits were written but the fault has not cleared")
    finally:
        handler.closePort()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", required=True, help="explicit leader serial port")
    parser.add_argument("--motor-ids", type=int, nargs="+", required=True, help="IDs whose labels have been checked")
    parser.add_argument("--apply", action="store_true", help="save limits to confirmed 7.4 V motors; support the arm")
    parser.add_argument("--backup", type=Path, help="new JSON file for original settings (must not already exist)")
    parser.add_argument("--min-volts", type=float, default=MIN_VOLTAGE / 10, help="minimum-voltage limit (default 5.0)")
    parser.add_argument("--max-volts", type=float, default=MAX_VOLTAGE / 10, help="maximum-voltage limit (default 5.5)")
    args = parser.parse_args()
    if not 4.0 <= args.min_volts < args.max_volts <= 8.4:
        parser.error("limits must satisfy 4.0 <= --min-volts < --max-volts <= 8.4 for 7.4 V motors")
    try:
        repair(args.port, args.motor_ids, apply=args.apply, backup=args.backup,
               min_voltage=round(args.min_volts * 10), max_voltage=round(args.max_volts * 10))
    except Exception as exc:
        print(f"Stopped: {exc}", file=sys.stderr)
        if args.apply:
            print("If a backup path was printed, changes may have started; inspect the motors before retrying.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()

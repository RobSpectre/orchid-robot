"""Read-only presentation of measured joint state. Never produces motor goals."""
import math

from . import motion as m


def motor_status(position, torque, calibration, ranges, *, reference_ready=False,
                 recording=False, target=None, diagnostics=None):
    result = {}
    for index, name in enumerate(m.MOTORS, 1):
        raw = (position or {}).get(name)
        saved = (calibration or {}).get(name) if not recording else None
        measured = ranges.get(name) if recording else None
        low = measured["min"] if measured else saved["range_min"] if saved and reference_ready else None
        high = measured["max"] if measured else saved["range_max"] if saved and reference_ready else None
        status = ("recording" if measured else "pending_range") if recording else "unreferenced"
        if not recording and reference_ready and raw is not None and low is not None:
            status = ("outside" if raw < low or raw > high else "near_limit"
                      if raw < low + m.JOINT_MARGIN or raw > high - m.JOINT_MARGIN else "within")
        goal = (target or {}).get(name)
        flags = (torque or {}).get(name)
        health = ((diagnostics or {}).get("motors") or {}).get(name, {})
        result[name] = {
            "id": index, "position_ticks": raw,
            "torque_enabled": bool(flags) if flags in (0, 1) else None,
            "degrees_from_midpoint": round((raw - 2047) * 360 / 4096, 2)
            if reference_ready and raw is not None else None,
            "range_min": low, "range_max": high, "limit_status": status,
            "target_ticks": goal, "tracking_error_ticks": raw - goal
            if raw is not None and goal is not None else None,
            "voltage_v": health.get("voltage_v"), "temperature_c": health.get("temperature_c"),
        }
    return result


def pose_angles(position, calibration, reference_ready):
    """Nominal URDF angles relative to captured midpoint; not world alignment."""
    if not reference_ready or not position or set(position) != set(m.MOTORS):
        return None
    angles = {name: (raw - 2047) * math.tau / 4096 for name, raw in position.items()}
    # The model's jaw zero is almost closed; the captured encoder midpoint is half open.
    jaw = (calibration or {}).get("gripper")
    if jaw and jaw["range_max"] > jaw["range_min"]:
        fraction = (position["gripper"] - jaw["range_min"]) / (jaw["range_max"] - jaw["range_min"])
        angles["gripper"] = -0.174533 + max(0, min(1, fraction)) * (1.74533 + 0.174533)
    else:
        angles["gripper"] += math.pi / 4
    return angles
